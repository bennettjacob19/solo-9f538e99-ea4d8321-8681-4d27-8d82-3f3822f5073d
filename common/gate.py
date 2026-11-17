"""Release gate: issue deployment tickets and confirm them atomically.

A release engineer selects existing alert fingerprints on the console; a
ticket is issued only when every selected alert is recovered and no global
silence is active. The ticket snapshots each fingerprint's (seq, round) plus
the global silence epoch. The FIRST confirm evaluates the ticket inside one
write transaction — any fingerprint whose seq/round moved, or any silence
set/clear since issuance, invalidates it — and pins the resulting
released/rejected decision together with the calling job ID. Retries of the
same job replay the pinned decision; a different job may never reuse the
ticket. Decisions are immutable: faults that appear after a confirm do not
rewrite the stored conclusion, and everything lives in SQLite so tickets and
outcomes survive restarts.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

from .db import transaction

TICKET_PREFIX = "GT-"


class GateReject(Exception):
    """A gate request that violates a rule; carries its own HTTP status."""

    def __init__(self, code: str, message: str, http_status: int = 409,
                 details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.details}


# --------------------------------------------------------------------------- #
# Shared state helpers (always called inside an open transaction)
# --------------------------------------------------------------------------- #
def _ensure_state_row(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO gate_state(id, silence_epoch, ticket_seq) VALUES (1, 0, 0)"
    )


def bump_silence_epoch(conn: sqlite3.Connection) -> None:
    """Record a silence set/clear. MUST run inside the same transaction as the
    silence write so confirm never observes one without the other."""
    _ensure_state_row(conn)
    conn.execute("UPDATE gate_state SET silence_epoch=silence_epoch+1 WHERE id=1")


def _silence_epoch(conn: sqlite3.Connection) -> int:
    _ensure_state_row(conn)
    row = conn.execute("SELECT silence_epoch FROM gate_state WHERE id=1").fetchone()
    return int(row["silence_epoch"])


def _silence_active(conn: sqlite3.Connection, now: float) -> bool:
    row = conn.execute("SELECT until_ts FROM silences WHERE id=1").fetchone()
    return row is not None and row["until_ts"] > now


def _alerts_by_fp(conn: sqlite3.Connection, fingerprints: list[str]) -> dict:
    rows = conn.execute(
        "SELECT fingerprint, current_seq, status, round FROM alerts "
        f"WHERE fingerprint IN ({','.join('?' * len(fingerprints))})",
        fingerprints,
    ).fetchall()
    return {r["fingerprint"]: r for r in rows}


# --------------------------------------------------------------------------- #
# Ticket issuance
# --------------------------------------------------------------------------- #
def _normalize_fingerprints(fingerprints: Any) -> list[str]:
    if not isinstance(fingerprints, list) or not fingerprints:
        raise ValueError("fingerprints must be a non-empty JSON array")
    ordered: list[str] = []
    for fp in fingerprints:
        if not isinstance(fp, str) or not fp.strip():
            raise ValueError("every fingerprint must be a non-empty string")
        if fp not in ordered:  # dedup, keep request order
            ordered.append(fp)
    return ordered


def issue_ticket(conn: sqlite3.Connection, fingerprints: Any,
                 now: Optional[float] = None) -> dict:
    """Issue a release ticket over existing, fully recovered fingerprints.

    Raises ValueError for malformed input (HTTP 422) and GateReject (HTTP 409)
    when the preconditions — all recovered, no active silence — are unmet.
    """
    now = time.time() if now is None else now
    ordered = _normalize_fingerprints(fingerprints)

    with transaction(conn):
        _ensure_state_row(conn)
        reasons: list[dict] = []
        if _silence_active(conn, now):
            row = conn.execute("SELECT until_ts FROM silences WHERE id=1").fetchone()
            reasons.append({"reason": "silence_active", "until_ts": row["until_ts"]})
        by_fp = _alerts_by_fp(conn, ordered)
        for fp in ordered:
            alert = by_fp.get(fp)
            if alert is None:
                reasons.append({"reason": "unknown_fingerprint", "fingerprint": fp})
            elif alert["status"] != "recovered":
                reasons.append({"reason": "not_recovered", "fingerprint": fp,
                                "status": alert["status"]})
        if reasons:
            raise GateReject("issue_rejected",
                             "ticket preconditions are not met", 409,
                             {"reasons": reasons})

        baseline = {fp: {"seq": by_fp[fp]["current_seq"], "round": by_fp[fp]["round"]}
                    for fp in ordered}
        epoch = _silence_epoch(conn)
        conn.execute("UPDATE gate_state SET ticket_seq=ticket_seq+1 WHERE id=1")
        seq_no = conn.execute(
            "SELECT ticket_seq FROM gate_state WHERE id=1"
        ).fetchone()["ticket_seq"]
        ticket_no = f"{TICKET_PREFIX}{int(seq_no):06d}"
        conn.execute(
            "INSERT INTO gate_tickets(ticket_no, created_at, fingerprints, baseline, "
            "silence_epoch, status) VALUES (?,?,?,?,?, 'open')",
            (ticket_no, now, json.dumps(ordered), json.dumps(baseline), epoch),
        )

    return {
        "ticket_no": ticket_no,
        "created_at": now,
        "fingerprints": ordered,
        "baseline": baseline,
        "silence_epoch": epoch,
        "status": "open",
        "job_id": None,
        "reasons": None,
        "decided_at": None,
        "valid_now": True,
        "current_reasons": [],
    }


# --------------------------------------------------------------------------- #
# Confirm
# --------------------------------------------------------------------------- #
def _invalidation_reasons(conn: sqlite3.Connection, ticket: sqlite3.Row,
                          now: float) -> list[dict]:
    """Diff the ticket's baseline against the current committed state."""
    reasons: list[dict] = []
    epoch = _silence_epoch(conn)
    if epoch != ticket["silence_epoch"]:
        reasons.append({"reason": "silence_changed",
                        "baseline_epoch": ticket["silence_epoch"],
                        "current_epoch": epoch})
    if _silence_active(conn, now):
        reasons.append({"reason": "silence_active"})
    fingerprints = json.loads(ticket["fingerprints"])
    baseline = json.loads(ticket["baseline"])
    by_fp = _alerts_by_fp(conn, fingerprints)
    for fp in fingerprints:
        base = baseline[fp]
        alert = by_fp.get(fp)
        if alert is None:
            reasons.append({"reason": "alert_missing", "fingerprint": fp})
            continue
        if alert["current_seq"] != base["seq"]:
            reasons.append({"reason": "seq_changed", "fingerprint": fp,
                            "baseline": base["seq"], "current": alert["current_seq"]})
        if alert["round"] != base["round"]:
            reasons.append({"reason": "round_changed", "fingerprint": fp,
                            "baseline": base["round"], "current": alert["round"]})
        if alert["status"] != "recovered":
            reasons.append({"reason": "not_recovered", "fingerprint": fp,
                            "status": alert["status"]})
    return reasons


def _confirm_result(ticket: sqlite3.Row, replayed: bool) -> dict:
    return {
        "ticket_no": ticket["ticket_no"],
        "job_id": ticket["job_id"],
        "decision": ticket["status"],
        "reasons": json.loads(ticket["reasons"]) if ticket["reasons"] else [],
        "decided_at": ticket["decided_at"],
        "replayed": replayed,
    }


def confirm_ticket(conn: sqlite3.Connection, ticket_no: Any, job_id: Any,
                   now: Optional[float] = None) -> dict:
    """Confirm a ticket for a deployment job.

    The first confirm decides and pins (decision, job_id) atomically in the
    same transaction that reads the alert/silence state, so interleaved event
    ingestion or silence changes are either fully before or fully after the
    decision. Same-job retries replay the pinned decision; other jobs are
    rejected with 409.
    """
    now = time.time() if now is None else now
    if not isinstance(ticket_no, str) or not ticket_no.strip():
        raise ValueError("ticket_no must be a non-empty string")
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job_id must be a non-empty string")

    with transaction(conn):
        _ensure_state_row(conn)
        ticket = conn.execute(
            "SELECT * FROM gate_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if ticket is None:
            raise GateReject("ticket_not_found",
                             f"ticket {ticket_no} does not exist", 404)
        if ticket["status"] != "open":
            if ticket["job_id"] == job_id:
                return _confirm_result(ticket, replayed=True)
            raise GateReject(
                "job_mismatch",
                f"ticket {ticket_no} is already bound to job {ticket['job_id']!r}",
                409,
                {"ticket_no": ticket_no, "bound_job_id": ticket["job_id"],
                 "decision": ticket["status"], "decided_at": ticket["decided_at"]},
            )
        reasons = _invalidation_reasons(conn, ticket, now)
        decision = "rejected" if reasons else "released"
        conn.execute(
            "UPDATE gate_tickets SET status=?, job_id=?, reasons=?, decided_at=? "
            "WHERE ticket_no=?",
            (decision, job_id, json.dumps(reasons), now, ticket_no),
        )

    return {"ticket_no": ticket_no, "job_id": job_id, "decision": decision,
            "reasons": reasons, "decided_at": now, "replayed": False}


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def _ticket_view(conn: sqlite3.Connection, ticket: sqlite3.Row, now: float) -> dict:
    view = {
        "ticket_no": ticket["ticket_no"],
        "created_at": ticket["created_at"],
        "fingerprints": json.loads(ticket["fingerprints"]),
        "baseline": json.loads(ticket["baseline"]),
        "silence_epoch": ticket["silence_epoch"],
        "status": ticket["status"],
        "job_id": ticket["job_id"],
        "reasons": json.loads(ticket["reasons"]) if ticket["reasons"] else None,
        "decided_at": ticket["decided_at"],
    }
    if ticket["status"] == "open":
        # Read-only preview against the current committed state; the binding
        # evaluation still happens inside the confirm transaction.
        current = _invalidation_reasons(conn, ticket, now)
        view["valid_now"] = not current
        view["current_reasons"] = current
    return view


def list_tickets(conn: sqlite3.Connection, now: Optional[float] = None,
                 limit: int = 200) -> list[dict]:
    now = time.time() if now is None else now
    with transaction(conn):
        rows = conn.execute(
            "SELECT * FROM gate_tickets ORDER BY created_at DESC, ticket_no DESC "
            "LIMIT ?", (limit,),
        ).fetchall()
        return [_ticket_view(conn, row, now) for row in rows]


def get_ticket(conn: sqlite3.Connection, ticket_no: str,
               now: Optional[float] = None) -> Optional[dict]:
    now = time.time() if now is None else now
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM gate_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        return None if row is None else _ticket_view(conn, row, now)
