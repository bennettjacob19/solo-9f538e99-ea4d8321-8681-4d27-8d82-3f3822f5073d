"""SQLite connection helpers and schema management."""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterator

# One re-entrant lock per live connection. sqlite3.Connection supports neither
# attribute assignment nor weak references, so key by id() and clean up on
# close(). Connections in this project are long-lived singletons.
_LOCKS: dict[int, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS events (
    fingerprint TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('firing', 'recovery')),
    summary     TEXT NOT NULL,
    received_at REAL NOT NULL,
    PRIMARY KEY (fingerprint, seq)
);

CREATE TABLE IF NOT EXISTS alerts (
    fingerprint     TEXT PRIMARY KEY,
    current_seq     INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('firing', 'recovered')),
    summary         TEXT NOT NULL,
    round           INTEGER NOT NULL DEFAULT 0,
    last_event_at   REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_no TEXT NOT NULL UNIQUE,
    fingerprint     TEXT NOT NULL,
    round           INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('firing', 'recovery')),
    summary         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_transit', 'delivered', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_retry_at   REAL NOT NULL DEFAULT 0,
    enqueued_at     REAL NOT NULL,
    delivered_at    REAL,
    last_error      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_notifications_fp_round_kind
    ON notifications(fingerprint, round, kind);
CREATE INDEX IF NOT EXISTS ix_notifications_dispatch
    ON notifications(status, next_retry_at);

CREATE TABLE IF NOT EXISTS silences (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    until_ts   REAL NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL keeps readers working while the worker writes; foreign keys and
    # explicit transactions give us the atomicity guarantees we rely on.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    with _LOCKS_GUARD:
        _LOCKS[id(conn)] = threading.RLock()
    return conn


def close(conn: sqlite3.Connection) -> None:
    with _LOCKS_GUARD:
        _LOCKS.pop(id(conn), None)
    conn.close()


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        close(conn)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialized write transaction with lock-retry.

    The connection's RLock makes multi-threaded ingestion on one connection
    safe; BEGIN IMMEDIATE plus the busy timeout/retry backstop handles
    multi-process contention. Application-level checks stay inside the
    transaction, making each read-check-write sequence atomic.
    """
    lock = _LOCKS.get(id(conn))
    if lock is not None:
        lock.acquire()
    attempt = 0
    try:
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= 50:
                    raise
                attempt += 1
                time.sleep(0.02 * attempt)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        if lock is not None:
            lock.release()
