"""Launch both services in one process using the stdlib runner (local/dev only).

Environment:
  ALERT_PORT      (default 8000)
  NOTIFIER_PORT   (default 8001)
  NOTIFIER_URL    must point at the notifier for the outbox worker
"""
from __future__ import annotations

import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runner.http_runner import run_alert_service, run_notifier_service


def main() -> None:
    alert_port = int(os.environ.get("ALERT_PORT", "8000"))
    notifier_port = int(os.environ.get("NOTIFIER_PORT", "8001"))
    os.environ.setdefault("NOTIFIER_URL", f"http://127.0.0.1:{notifier_port}")

    notifier_server = run_notifier_service("0.0.0.0", notifier_port)
    alert_server = run_alert_service("0.0.0.0", alert_port)
    print(f"alert console:  http://127.0.0.1:{alert_port}/", flush=True)
    print(f"notifier:       http://127.0.0.1:{notifier_port}/", flush=True)

    stop = False

    def _handle(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    try:
        while not stop:
            time.sleep(0.2)
    finally:
        alert_server.stop_event.set()  # type: ignore[attr-defined]
        alert_server.shutdown()
        notifier_server.shutdown()


if __name__ == "__main__":
    main()
