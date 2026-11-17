"""End-to-end key-path tests using the stdlib runner (no third-party deps).

Run:  python3 -m tests.e2e
Both services are started in-process on real HTTP ports with isolated SQLite
databases; assertions cover every rule from the requirements.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runner.http_runner import run_alert_service, run_notifier_service  # noqa: E402

ALERT_PORT = 18080
NOTIFIER_PORT = 18081
BASE = f"http://127.0.0.1:{ALERT_PORT}"
NOTIFIER = f"http://127.0.0.1:{NOTIFIER_PORT}"


def http(method: str, url: str, body=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode())
            status = resp.status
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode())
        status = exc.code
    if expect is not None:
        assert status == expect, f"{method} {url} -> {status} (want {expect}): {payload}"
    return status, payload


def wait_until(desc, predicate, timeout=10.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for: {desc} (last={last!r})")


def alert_status(fp):
    _, data = http("GET", f"{BASE}/api/alerts")
    for a in data["alerts"]:
        if a["fingerprint"] == fp:
            return a
    return None


def outbox_rows(fp):
    _, data = http("GET", f"{BASE}/api/notifications")
    return [n for n in data["notifications"] if n["fingerprint"] == fp]


def received_nos():
    _, data = http("GET", f"{NOTIFIER}/api/notifications")
    return {n["notification_no"]: n for n in data["notifications"]}


def wait_delivered(fp, expected_count, timeout=10.0):
    def check():
        rows = sorted(outbox_rows(fp), key=lambda r: r["id"])
        if len(rows) != expected_count:
            return None
        return rows if all(r["status"] == "delivered" for r in rows) else None
    return wait_until(f"{fp} {expected_count} delivered", check, timeout)


class E2ETestCase(unittest.TestCase):
    tmpdir: str
    alert_server = None
    notifier_server = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="alert-e2e-")
        os.environ["ALERT_DB_PATH"] = os.path.join(cls.tmpdir, "alerts.db")
        os.environ["NOTIFIER_DB_PATH"] = os.path.join(cls.tmpdir, "notifier.db")
        os.environ["NOTIFIER_URL"] = NOTIFIER
        os.environ["WORKER_INTERVAL_SECONDS"] = "0.1"
        cls.notifier_server = run_notifier_service("127.0.0.1", NOTIFIER_PORT)
        cls.alert_server = run_alert_service("127.0.0.1", ALERT_PORT)
        wait_until("services healthy", lambda: (
            http("GET", f"{BASE}/health")[0] == 200
            and http("GET", f"{NOTIFIER}/health")[0] == 200
        ))

    @classmethod
    def tearDownClass(cls):
        cls.alert_server.stop_event.set()
        cls.alert_server.shutdown()
        cls.alert_server.server_close()
        cls.notifier_server.shutdown()
        cls.notifier_server.server_close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_01_health_and_pages(self):
        self.assertEqual(http("GET", f"{BASE}/health", expect=200)[1]["service"],
                         "alert-console")
        self.assertEqual(http("GET", f"{NOTIFIER}/health", expect=200)[1]["service"],
                         "local-notifier")
        for url in (f"{BASE}/", f"{NOTIFIER}/"):
            with urllib.request.urlopen(url, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn(b"text/html", resp.headers["Content-Type"].encode())
        with urllib.request.urlopen(f"{BASE}/static/app.js", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn(b"javascript", resp.headers["Content-Type"].encode())
        with urllib.request.urlopen(f"{NOTIFIER}/static/notifier.js", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        # path traversal must not escape the static directory (403 or 404 both safe)
        traversal_status, _ = http("GET", f"{BASE}/static/../../common/config.py")
        self.assertIn(traversal_status, (403, 404))

    def test_02_basic_firing_recovery_and_lost_receipt(self):
        fp = "svc/basic"
        s, r1 = http("POST", f"{BASE}/api/events",
                     {"fingerprint": fp, "seq": 1, "kind": "firing", "summary": "cpu high"},
                     expect=200)
        self.assertEqual(r1["result"], "accepted")
        self.assertEqual(r1["alert"]["status"], "firing")
        self.assertEqual(r1["alert"]["round"], 1)

        # identical replay keeps the same result, creates nothing new
        s, r2 = http("POST", f"{BASE}/api/events",
                     {"fingerprint": fp, "seq": 1, "kind": "firing", "summary": "cpu high"},
                     expect=200)
        self.assertEqual(r2["result"], "duplicate_replayed")

        # same seq different body -> rejected
        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp, "seq": 1, "kind": "firing", "summary": "changed text"},
             expect=409)

        # recovery
        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp, "seq": 2, "kind": "recovery", "summary": "cpu back"},
             expect=200)
        self.assertEqual(alert_status(fp)["status"], "recovered")

        # stale seq cannot rewrite state
        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp, "seq": 2, "kind": "firing", "summary": "late trigger"},
             expect=409)
        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp, "seq": 1, "kind": "recovery", "summary": "old recovery"},
             expect=409)
        self.assertEqual(alert_status(fp)["status"], "recovered")

        rows = wait_delivered(fp, 2)
        kinds = [r["kind"] for r in rows]
        self.assertEqual(kinds, ["firing", "recovery"])

        # notifier: each number stored exactly once despite retried receipts
        recv = received_nos()
        firing_no = next(r["notification_no"] for r in rows if r["kind"] == "firing")
        rec_no = next(r["notification_no"] for r in rows if r["kind"] == "recovery")
        self.assertIn(firing_no, recv)
        self.assertIn(rec_no, recv)
        self.assertGreaterEqual(recv[firing_no]["attempts_seen"], 1)
        self.assertEqual(recv[firing_no]["status"], "accepted")
        # ordering on the receiver side: firing arrived before recovery
        self.assertLess(recv[firing_no]["first_seen_at"], recv[rec_no]["first_seen_at"])

        # outbox attempts show the lost receipt was retried with the same number
        firing_row = next(r for r in rows if r["kind"] == "firing")
        self.assertGreaterEqual(firing_row["attempts"], 2)

    def test_03_silence_parks_new_firing_but_recovery_still_notifies(self):
        fp = "svc/silenced"
        # fault announced before the maintenance window
        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp, "seq": 1, "kind": "firing", "summary": "disk full"},
             expect=200)
        wait_delivered(fp, 1)

        # enable silence
        _, sil = http("POST", f"{BASE}/api/silence",
                      {"duration_seconds": 30, "reason": "maintenance"}, expect=200)
        self.assertTrue(sil["active"])

        # new trigger during silence: alert updates, no notification enqueued
        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp, "seq": 2, "kind": "firing", "summary": "disk fuller"},
             expect=200)
        a = alert_status(fp)
        self.assertEqual(a["status"], "firing")
        self.assertEqual(a["summary"], "disk fuller")
        time.sleep(0.3)
        self.assertEqual(len(outbox_rows(fp)), 1)

        # recovery of an already-announced fault MUST still be notified
        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp, "seq": 3, "kind": "recovery", "summary": "disk ok"},
             expect=200)
        rows = wait_delivered(fp, 2)
        self.assertEqual([r["kind"] for r in rows], ["firing", "recovery"])

        # receiver shows both; no extra firing notification appeared
        recv = received_nos()
        self.assertEqual(
            sorted(k for k in recv if k.startswith(fp)),
            sorted(r["notification_no"] for r in rows),
        )
        http("DELETE", f"{BASE}/api/silence", expect=200)

    def test_04_silence_expiry_replays_only_still_firing_unannounced(self):
        fp = "svc/parked"
        http("POST", f"{BASE}/api/silence",
             {"duration_seconds": 1, "reason": "short window"}, expect=200)

        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp, "seq": 1, "kind": "firing", "summary": "latency"},
             expect=200)
        # also a second fingerprint that recovers during the silence
        fp2 = "svc/parked-recovered"
        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp2, "seq": 1, "kind": "firing", "summary": "temp spike"},
             expect=200)
        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp2, "seq": 2, "kind": "recovery", "summary": "spike gone"},
             expect=200)

        time.sleep(0.3)
        # nothing announced yet: fp2 recovered without ever being notified,
        # so it must never produce a notification for that round
        self.assertEqual(outbox_rows(fp), [])
        self.assertEqual(outbox_rows(fp2), [])

        # wait for the silence to expire and the worker sweep to release it
        rows = wait_delivered(fp, 1, timeout=8)
        self.assertEqual(rows[0]["kind"], "firing")
        time.sleep(0.3)
        self.assertEqual(outbox_rows(fp2), [], "recovered-during-silence round stays unannounced")

    def test_05_retrigger_after_recovery_is_a_new_round(self):
        fp = "svc/rounds"
        for seq, kind, text in [
            (1, "firing", "round1 fire"),
            (2, "recovery", "round1 rec"),
            (3, "firing", "round2 fire"),
            (4, "recovery", "round2 rec"),
        ]:
            http("POST", f"{BASE}/api/events",
                 {"fingerprint": fp, "seq": seq, "kind": kind, "summary": text}, expect=200)

        a = alert_status(fp)
        self.assertEqual(a["round"], 2)
        self.assertEqual(a["status"], "recovered")
        rows = wait_delivered(fp, 4)
        self.assertEqual([(r["round"], r["kind"]) for r in rows],
                         [(1, "firing"), (1, "recovery"), (2, "firing"), (2, "recovery")])
        # notification numbers are unique per round/kind
        self.assertEqual(len({r["notification_no"] for r in rows}), 4)

    def test_06_per_fingerprint_ordering_under_loss(self):
        fp = "svc/order"
        for seq, kind, text in [
            (1, "firing", "f1"),
            (2, "recovery", "r1"),
            (3, "firing", "f2"),
        ]:
            http("POST", f"{BASE}/api/events",
                 {"fingerprint": fp, "seq": seq, "kind": kind, "summary": text}, expect=200)
        rows = wait_delivered(fp, 3)
        self.assertEqual([r["kind"] for r in rows], ["firing", "recovery", "firing"])
        recv = received_nos()
        # Receiver first-seen timestamps must follow the outbox (id) order.
        times = [recv[r["notification_no"]]["first_seen_at"] for r in rows]
        self.assertEqual(times, sorted(times))

    def test_07_invalid_payloads(self):
        # stdlib runner maps validation rejects to 409; the FastAPI app uses
        # Pydantic and answers 422 for the same malformed input.
        http("POST", f"{BASE}/api/events", {"seq": 1, "kind": "firing", "summary": "x"},
             expect=409)
        http("POST", f"{BASE}/api/events",
             {"fingerprint": "x", "seq": -1, "kind": "firing", "summary": "x"}, expect=409)
        http("POST", f"{BASE}/api/events",
             {"fingerprint": "x", "seq": 1, "kind": "explode", "summary": "x"}, expect=409)
        http("POST", f"{BASE}/api/silence", {"duration_seconds": -3}, expect=422)

    def test_08_restart_does_not_lose_or_reorder(self):
        # Persist a partially delivered outbox, restart both servers, verify
        # everything converges with no duplicates on the receiver.
        fp = "svc/restart"
        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp, "seq": 1, "kind": "firing", "summary": "before restart"},
             expect=200)
        wait_delivered(fp, 1)
        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp, "seq": 2, "kind": "firing", "summary": "still firing text"},
             expect=200)  # same round refresh, no new notification

        before = received_nos()
        before_nos = {k: v["attempts_seen"] for k, v in before.items() if k.startswith(fp)}

        # restart both processes on the same database files
        self.alert_server.stop_event.set()
        self.alert_server.shutdown()
        self.alert_server.server_close()
        self.notifier_server.shutdown()
        self.notifier_server.server_close()
        time.sleep(0.2)
        type(self).notifier_server = run_notifier_service("127.0.0.1", NOTIFIER_PORT)
        type(self).alert_server = run_alert_service("127.0.0.1", ALERT_PORT)
        wait_until("services healthy after restart", lambda: (
            http("GET", f"{BASE}/health")[0] == 200
            and http("GET", f"{NOTIFIER}/health")[0] == 200
        ))

        # state survived
        self.assertEqual(alert_status(fp)["summary"], "still firing text")

        # post a recovery after restart; it must flow through and no duplicate
        # firing notification may be produced
        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp, "seq": 3, "kind": "recovery", "summary": "after restart rec"},
             expect=200)
        rows = wait_delivered(fp, 2)
        self.assertEqual([r["kind"] for r in rows], ["firing", "recovery"])

        after = received_nos()
        for no, seen in before_nos.items():
            # the pre-restart notification number is still a single stored row
            self.assertIn(no, after)
            self.assertEqual(after[no]["first_seen_at"], before[no]["first_seen_at"])

        # a late, older trigger must not overwrite the recovered state
        http("POST", f"{BASE}/api/events",
             {"fingerprint": fp, "seq": 2, "kind": "firing", "summary": "late again"},
             expect=409)
        self.assertEqual(alert_status(fp)["status"], "recovered")


if __name__ == "__main__":
    unittest.main(verbosity=2)
