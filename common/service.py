"""Core alerting logic: event ingestion, silences and the notification outbox."""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable, Optional

from .db import transaction

VALID_KINDS = ("firing", "recovery")


class Reject(Exception):
    """A request that violates the ordering/idempotency rules (HTTP 409)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_event(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise Reject("invalid_payload", "event body must be a JSON object")
    fingerprint = payload.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise Reject("invalid_payload", "fingerprint must be a non-empty string")
    seq = payload.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise Reject("invalid_payload", "seq must be a non-negative integer")
    kind = payload.get("kind")
    if kind not in VALID_KINDS:
        raise Reject("invalid_payload", "kind must be 'firing' or 'recovery'")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise Reject("invalid_payload", "summary must be a non-empty string")
    return {"fingerprint": fingerprint, "seq": seq, "kind": kind, "summary": summary}


def notification_no(fingerprint: str, round_no: int, kind: str) -> str:
    # Stable and human-readable: retries reuse the exact same number, and the
    # unique index on (fingerprint, round, kind) guarantees global uniqueness.
    return f"{fingerprint}#r{round_no}:{kind}"


# --------------------------------------------------------------------------- #
# Event ingestion
# --------------------------------------------------------------------------- #
def ingest_event(conn: sqlite3.Connection, payload: dict, now: Optional[float] = None) -> dict:
    """Apply one trigger/recovery event.

    Returns a result dictionary. Raises Reject for stale/conflicting events.
    Same (fingerprint, seq, body) replays the original result; same seq with a
    different body is rejected; an older seq can never rewrite current state.
    """
    event = validate_event(payload)
    now = time.time() if now is None else now
    fp, seq, kind, summary = event["fingerprint"], event["seq"], event["kind"], event["summary"]

    with transaction(conn):
        existing = conn.execute(
            "SELECT kind, summary FROM events WHERE fingerprint=? AND seq=?", (fp, seq)
        ).fetchone()
        if existing is not None:
            if existing["kind"] != kind or existing["summary"] != summary:
                raise Reject(
                    "conflicting_event",
                    f"event {fp} seq={seq} was already recorded with a different body",
                )
            # Identical retry: the original application stands, no state change.
            return {"result": "duplicate_replayed", "event": event, "alert": _get_alert(conn, fp)}

        alert_row = conn.execute(
            "SELECT * FROM alerts WHERE fingerprint=?", (fp,)
        ).fetchone()

        if alert_row is None:
            if kind == "recovery":
                raise Reject("stale_event", "recovery event has no active alert")
            round_no = 1
            conn.execute(
                "INSERT INTO alerts(fingerprint, current_seq, status, summary, round, "
                "last_event_at, updated_at) VALUES (?,?, 'firing', ?, ?, ?, ?)",
                (fp, seq, summary, round_no, now, now),
            )
        else:
            if seq <= alert_row["current_seq"]:
                raise Reject(
                    "stale_event",
                    f"seq={seq} is not newer than current seq={alert_row['current_seq']}",
                )
            if kind == "firing":
                if alert_row["status"] == "recovered":
                    # Re-trigger after recovery starts a brand-new round.
                    round_no = alert_row["round"] + 1
                    conn.execute(
                        "UPDATE alerts SET status='firing', round=?, summary=?, "
                        "current_seq=?, last_event_at=?, updated_at=? WHERE fingerprint=?",
                        (round_no, summary, seq, now, now, fp),
                    )
                else:
                    # Newer trigger while still firing: same round, text refresh only.
                    round_no = alert_row["round"]
                    conn.execute(
                        "UPDATE alerts SET summary=?, current_seq=?, last_event_at=?, "
                        "updated_at=? WHERE fingerprint=?",
                        (summary, seq, now, now, fp),
                    )
            else:  # recovery
                round_no = alert_row["round"]
                conn.execute(
                    "UPDATE alerts SET status='recovered', summary=?, current_seq=?, "
                    "last_event_at=?, updated_at=? WHERE fingerprint=?",
                    (summary, seq, now, now, fp),
                )

        conn.execute(
            "INSERT INTO events(fingerprint, seq, kind, summary, received_at) "
            "VALUES (?,?,?,?,?)",
            (fp, seq, kind, summary, now),
        )

        notifications = _apply_notification_policy(conn, fp, round_no, kind, summary, now)
        alert = _get_alert(conn, fp)

    return {
        "result": "accepted",
        "event": event,
        "alert": alert,
        "notifications_enqueued": notifications,
    }


def _silence_active(conn: sqlite3.Connection, now: float) -> bool:
    row = conn.execute("SELECT until_ts FROM silences WHERE id=1").fetchone()
    return row is not None and row["until_ts"] > now


def _enqueue_notification(
    conn: sqlite3.Connection, fp: str, round_no: int, kind: str, summary: str, now: float
) -> bool:
    """Insert an outbox row. Returns False if an identical one already exists."""
    no = notification_no(fp, round_no, kind)
    cur = conn.execute(
        "INSERT OR IGNORE INTO notifications(notification_no, fingerprint, round, kind, "
        "summary, status, next_retry_at, enqueued_at) VALUES (?,?,?,?,?, 'pending', ?, ?)",
        (no, fp, round_no, kind, summary, now, now),
    )
    return cur.rowcount > 0


def _apply_notification_policy(
    conn: sqlite3.Connection, fp: str, round_no: int, kind: str, summary: str, now: float
) -> list[dict]:
    """Decide which notifications a newly applied event produces."""
    enqueued: list[dict] = []
    silenced = _silence_active(conn, now)

    if kind == "firing":
        # During a silence a new trigger only updates the alert; it is parked in
        # the outbox later when the silence expires (see sweep_silences).
        if not silenced:
            if _enqueue_notification(conn, fp, round_no, "firing", summary, now):
                enqueued.append(
                    {"notification_no": notification_no(fp, round_no, "firing"),
                     "kind": "firing"}
                )
    else:  # recovery
        # If the fault was ever announced (a firing notification exists for this
        # round, even if delivery is still pending), its recovery MUST be sent —
        # silence does not suppress it. The outbox worker preserves order.
        firing_row = conn.execute(
            "SELECT 1 FROM notifications WHERE fingerprint=? AND round=? AND kind='firing'",
            (fp, round_no),
        ).fetchone()
        if firing_row is not None:
            if _enqueue_notification(conn, fp, round_no, "recovery", summary, now):
                enqueued.append(
                    {"notification_no": notification_no(fp, round_no, "recovery"),
                     "kind": "recovery"}
                )
    return enqueued


def _get_alert(conn: sqlite3.Connection, fp: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM alerts WHERE fingerprint=?", (fp,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Silence management
# --------------------------------------------------------------------------- #
def set_silence(
    conn: sqlite3.Connection, until_ts: float, reason: str, now: Optional[float] = None
) -> dict:
    now = time.time() if now is None else now
    with transaction(conn):
        conn.execute(
            "INSERT INTO silences(id, until_ts, reason, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET until_ts=excluded.until_ts, "
            "reason=excluded.reason, updated_at=excluded.updated_at",
            (until_ts, reason, now),
        )
    return get_silence(conn, now)


def clear_silence(conn: sqlite3.Connection, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    with transaction(conn):
        conn.execute("DELETE FROM silences WHERE id=1")
    return {"active": False, "until_ts": None, "reason": None, "now": now}


def get_silence(conn: sqlite3.Connection, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    row = conn.execute("SELECT * FROM silences WHERE id=1").fetchone()
    if row is None or row["until_ts"] <= now:
        return {"active": False, "until_ts": None, "reason": None, "now": now}
    return {
        "active": True,
        "until_ts": row["until_ts"],
        "reason": row["reason"],
        "now": now,
        "remaining_seconds": round(row["until_ts"] - now, 3),
    }


def sweep_silences(conn: sqlite3.Connection, now: Optional[float] = None) -> list[dict]:
    """Expire silences: re-issue parked firing notifications.

    When the silence window ends, every alert that is *still firing* and whose
    current round was never announced gets its firing notification enqueued.
    Alerts that already recovered stay silent for that round.
    """
    now = time.time() if now is None else now
    released: list[dict] = []
    with transaction(conn):
        if _silence_active(conn, now):
            return released
        rows = conn.execute(
            "SELECT a.fingerprint, a.round, a.summary FROM alerts a "
            "WHERE a.status='firing'"
        ).fetchall()
        for row in rows:
            exists = conn.execute(
                "SELECT 1 FROM notifications WHERE fingerprint=? AND round=? AND kind='firing'",
                (row["fingerprint"], row["round"]),
            ).fetchone()
            if exists is None:
                if _enqueue_notification(
                    conn, row["fingerprint"], row["round"], "firing", row["summary"], now
                ):
                    released.append(
                        {"fingerprint": row["fingerprint"], "round": row["round"]}
                    )
    return released


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def list_alerts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.*,
               fn.status AS firing_notify_status,
               fn.notification_no AS firing_notification_no,
               rn.status AS recovery_notify_status,
               rn.notification_no AS recovery_notification_no
        FROM alerts a
        LEFT JOIN notifications fn
            ON fn.fingerprint=a.fingerprint AND fn.round=a.round AND fn.kind='firing'
        LEFT JOIN notifications rn
            ON rn.fingerprint=a.fingerprint AND rn.round=a.round AND rn.kind='recovery'
        ORDER BY a.updated_at DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def list_notifications(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_events(conn: sqlite3.Connection, fingerprint: str, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM events WHERE fingerprint=? ORDER BY seq DESC LIMIT ?",
        (fingerprint, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Outbox delivery worker
# --------------------------------------------------------------------------- #
SendFunc = Callable[[dict], tuple[bool, Optional[str]]]


def _backoff_seconds(attempts: int) -> float:
    return min(float(attempts), 30.0)


def dispatch_pending(
    conn: sqlite3.Connection, send: SendFunc, now: Optional[float] = None
) -> list[dict]:
    """Deliver due outbox rows, preserving per-fingerprint ordering.

    A notification is only sent when every earlier notification of the same
    fingerprint has already been delivered. Lost receipts keep the row in
    'in_transit' and the same notification number is retried later. HTTP calls
    happen *outside* write transactions so ingestion never blocks on the wire.
    """
    now = time.time() if now is None else now
    results: list[dict] = []

    while True:
        with transaction(conn):
            row = conn.execute(
                "SELECT * FROM notifications WHERE status IN ('pending','in_transit') "
                "AND next_retry_at<=? ORDER BY id",
                (now,),
            ).fetchone()
            if row is None:
                break
            blocker = conn.execute(
                "SELECT 1 FROM notifications WHERE fingerprint=? AND id<? AND "
                "status!='delivered' LIMIT 1",
                (row["fingerprint"], row["id"]),
            ).fetchone()
            if blocker is not None:
                # Head-of-line for this fingerprint: park it briefly; rows of
                # other fingerprints are picked up on the next worker tick.
                conn.execute(
                    "UPDATE notifications SET next_retry_at=? WHERE id=?",
                    (now + 0.2, row["id"]),
                )
                break
            conn.execute(
                "UPDATE notifications SET status='in_transit', attempts=attempts+1, "
                "last_error=NULL WHERE id=?",
                (row["id"],),
            )
            payload = {
                "notification_no": row["notification_no"],
                "fingerprint": row["fingerprint"],
                "round": row["round"],
                "kind": row["kind"],
                "summary": row["summary"],
            }

        # Network I/O happens outside any write transaction so event ingestion
        # never blocks waiting on the notification endpoint.
        ok, error = send(payload)

        with transaction(conn):
            if ok:
                conn.execute(
                    "UPDATE notifications SET status='delivered', delivered_at=?, "
                    "last_error=NULL WHERE id=?",
                    (now, row["id"]),
                )
                results.append({"notification_no": row["notification_no"], "delivered": True})
            else:
                retry_at = now + _backoff_seconds(row["attempts"] + 1)
                conn.execute(
                    "UPDATE notifications SET next_retry_at=?, last_error=? WHERE id=?",
                    (retry_at, error, row["id"]),
                )
                results.append(
                    {
                        "notification_no": row["notification_no"],
                        "delivered": False,
                        "error": error,
                        "next_retry_at": retry_at,
                    }
                )
    return results
