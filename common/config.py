"""Runtime configuration sourced from environment variables (read at use time)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass
class AppConfig:
    # Plain field defaults so values are re-read from the environment whenever
    # an AppConfig is constructed (tests swap env vars between runs).
    db_path: str = ""
    notifier_url: str = ""
    worker_interval_seconds: float = 0.0
    notify_timeout_seconds: float = 0.0

    def __post_init__(self) -> None:
        self.db_path = self.db_path or _env("ALERT_DB_PATH", "/data/alerts.db")
        self.notifier_url = self.notifier_url or _env(
            "NOTIFIER_URL", "http://notifier:8001"
        )
        # How often the background worker delivers pending notifications and
        # re-checks silences that are about to expire.
        self.worker_interval_seconds = self.worker_interval_seconds or float(
            _env("WORKER_INTERVAL_SECONDS", "0.5")
        )
        self.notify_timeout_seconds = self.notify_timeout_seconds or float(
            _env("NOTIFY_TIMEOUT_SECONDS", "3")
        )


@dataclass
class NotifierConfig:
    db_path: str = ""
    host: str = ""
    port: int = 0
    # Number of times a freshly seen notification number simulates a lost
    # receipt (HTTP 500). Retries carrying the same number are accepted and
    # deduplicated. 0 disables the simulation.
    lose_first_n: int = -1

    def __post_init__(self) -> None:
        self.db_path = self.db_path or _env("NOTIFIER_DB_PATH", "/data/notifier.db")
        self.host = self.host or _env("NOTIFIER_HOST", "0.0.0.0")
        self.port = self.port or int(_env("NOTIFIER_PORT", "8001"))
        if self.lose_first_n < 0:
            self.lose_first_n = int(_env("LOSE_FIRST_N", "1"))
