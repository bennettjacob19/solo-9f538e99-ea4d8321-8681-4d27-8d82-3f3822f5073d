"""Local notification endpoint core: receipt-loss simulation and dedup store."""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

from .db import transaction

NOTIFIER_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS received (
    notification_no TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL,
    round           INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    summary         TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempts_seen   INTEGER NOT NULL DEFAULT 0,
    first_seen_at   REAL NOT NULL,
    last_seen_at    REAL NOT NULL
);
"""


class NotifierReject(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def init_notifier_db(conn: sqlite3.Connection) -> None:
    conn.executescript(NOTIFIER_SCHEMA)
    conn.commit()


def validate_notification(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise NotifierReject("invalid_payload", "body must be a JSON object")
    no = payload.get("notification_no")
    if not isinstance(no, str) or not no.strip():
        raise NotifierReject("invalid_payload", "notification_no must be a non-empty string")
    fp = payload.get("fingerprint")
    if not isinstance(fp, str) or not fp.strip():
        raise NotifierReject("invalid_payload", "fingerprint must be a non-empty string")
    kind = payload.get("kind")
    if kind not in ("firing", "recovery"):
        raise NotifierReject("invalid_payload", "kind must be 'firing' or 'recovery'")
    round_no = payload.get("round")
    if isinstance(round_no, bool) or not isinstance(round_no, int) or round_no < 1:
        raise NotifierReject("invalid_payload", "round must be a positive integer")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise NotifierReject("invalid_payload", "summary must be a non-empty string")
    return {
        "notification_no": no,
        "fingerprint": fp,
        "round": round_no,
        "kind": kind,
        "summary": summary,
    }


def receive_notification(
    conn: sqlite3.Connection, payload: dict, lose_first_n: int, now: Optional[float] = None
) -> tuple[int, dict]:
    """Process one delivery attempt.

    Returns (http_status, body). The first ``lose_first_n`` attempts of a *new*
    notification number get HTTP 500 (receipt lost on the way back). Retries
    carrying the same number always succeed, so the endpoint deduplicates by
    number and every notification converges to one stored final state.
    """
    item = validate_notification(payload)
    no = item["notification_no"]
    now = time.time() if now is None else now

    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM received WHERE notification_no=?", (no,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO received(notification_no, fingerprint, round, kind, summary, "
                "status, attempts_seen, first_seen_at, last_seen_at) "
                "VALUES (?,?,?,?,?, 'accepted', 1, ?, ?)",
                (no, item["fingerprint"], item["round"], item["kind"], item["summary"],
                 now, now),
            )
            if lose_first_n > 0:
                # Message is stored, but the caller's receipt is "lost".
                return 500, {
                    "ok": False,
                    "error": "simulated lost receipt",
                    "notification_no": no,
                    "stored": True,
                    "duplicate": False,
                }
            return 200, {
                "ok": True,
                "notification_no": no,
                "duplicate": False,
                "status": "accepted",
            }

        # Same number retried: idempotent acceptance. Detect body drift so a
        # colliding number cannot silently overwrite a different notification.
        mismatch = (
            row["fingerprint"] != item["fingerprint"]
            or row["round"] != item["round"]
            or row["kind"] != item["kind"]
            or row["summary"] != item["summary"]
        )
        if mismatch:
            return 409, {
                "ok": False,
                "error": "notification_no reused with a different body",
                "notification_no": no,
            }
        conn.execute(
            "UPDATE received SET attempts_seen=attempts_seen+1, last_seen_at=? "
            "WHERE notification_no=?",
            (now, no),
        )
        return 200, {
            "ok": True,
            "notification_no": no,
            "duplicate": True,
            "status": row["status"],
        }


def list_received(conn: sqlite3.Connection, limit: int = 500) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM received ORDER BY first_seen_at DESC, notification_no DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
