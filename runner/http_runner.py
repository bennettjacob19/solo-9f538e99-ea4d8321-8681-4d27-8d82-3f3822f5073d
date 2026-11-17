"""Stdlib-only HTTP runner reusing the exact same core logic as the FastAPI app.

This exists so the system can be exercised end-to-end without third-party
packages. In Docker the real FastAPI applications are used; the business rules
live in ``common`` and are identical in both paths.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from common import notifier_core, service
from common.config import AppConfig, NotifierConfig
from common.db import connect, init_db


class _ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class JsonRouter(BaseHTTPRequestHandler):
    routes: list[tuple[str, str, Callable[..., Any]]] = []
    static_index: str = ""
    static_dir: str = ""

    _CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json",
        ".svg": "image/svg+xml",
    }

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        pass

    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: str, content_type: str) -> None:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            self._json(404, {"error": "not_found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid_json"})
            return None

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._file(self.static_index, "text/html; charset=utf-8")
            return
        if path.startswith("/static/") and self.static_dir:
            # Resolve inside static_dir only; reject traversal attempts.
            base = os.path.realpath(self.static_dir)
            target = os.path.realpath(os.path.join(base, path[len("/static/"):]))
            if not (target == base or target.startswith(base + os.sep)):
                self._json(403, {"error": "forbidden"})
                return
            ext = os.path.splitext(target)[1].lower()
            self._file(target, self._CONTENT_TYPES.get(ext, "application/octet-stream"))
            return
        for method, prefix, handler in self.routes:
            if method == "GET" and path == prefix:
                self._dispatch(handler, {})
                return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._read_json()
        if body is None:
            return
        for method, prefix, handler in self.routes:
            if method == "POST" and path == prefix:
                self._dispatch(handler, body)
                return
        self._json(404, {"error": "not_found"})

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        for method, prefix, handler in self.routes:
            if method == "DELETE" and path == prefix:
                self._dispatch(handler, {})
                return
        self._json(404, {"error": "not_found"})

    def _dispatch(self, handler: Callable[..., Any], body: Any) -> None:
        try:
            result = handler(body)
        except (service.Reject, notifier_core.NotifierReject) as exc:
            status = 409 if isinstance(exc, service.Reject) else 422
            self._json(status, {"error": exc.code, "message": exc.message})
            return
        except ValueError as exc:
            self._json(422, {"error": "invalid_payload", "message": str(exc)})
            return
        if isinstance(result, tuple):
            status, payload = result
            self._json(status, payload)
        else:
            self._json(200, result)


# --------------------------------------------------------------------------- #
# HTTP client used by the stdlib worker
# --------------------------------------------------------------------------- #
def http_post_json(url: str, payload: dict, timeout: float = 3.0) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url, data=data, method="POST",
                             headers={"Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except json.JSONDecodeError:
            return exc.code, {"error": exc.reason}
    except URLError as exc:
        return 0, {"error": f"URLError: {exc.reason}"}


# --------------------------------------------------------------------------- #
# Alert service runner
# --------------------------------------------------------------------------- #
def run_alert_service(host: str, port: int) -> ThreadingHTTPServer:
    cfg = AppConfig()
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    stop = threading.Event()

    def send(payload: dict) -> tuple[bool, str | None]:
        status, _ = http_post_json(f"{cfg.notifier_url}/api/notifications", payload,
                                   cfg.notify_timeout_seconds)
        if status == 200:
            return True, None
        return False, f"HTTP {status}"

    def worker_loop() -> None:
        while not stop.is_set():
            try:
                service.dispatch_pending(conn, send)
                service.sweep_silences(conn)
            except Exception as exc:  # pragma: no cover - resilience
                print(f"[worker] tick failed: {exc!r}", flush=True)
            stop.wait(cfg.worker_interval_seconds)

    threading.Thread(target=worker_loop, daemon=True, name="outbox-worker").start()

    def h_health(_: Any) -> dict:
        return {"ok": True, "service": "alert-console"}

    def h_events(body: Any) -> dict:
        return service.ingest_event(conn, body)

    def h_alerts(_: Any) -> dict:
        return {"alerts": service.list_alerts(conn), "silence": service.get_silence(conn)}

    def h_notifications(_: Any) -> dict:
        return {"notifications": service.list_notifications(conn)}

    def h_silence_get(_: Any) -> dict:
        return service.get_silence(conn)

    def h_silence_set(body: Any) -> dict:
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        now = time.time()
        if body.get("until_ts") is not None:
            until_ts = float(body["until_ts"])
        elif body.get("duration_seconds") is not None:
            until_ts = now + float(body["duration_seconds"])
        else:
            raise ValueError("either duration_seconds or until_ts is required")
        if until_ts <= now:
            raise ValueError("silence must end in the future")
        return service.set_silence(conn, until_ts, str(body.get("reason", "")), now)

    def h_silence_clear(_: Any) -> dict:
        result = service.clear_silence(conn)
        result["released"] = service.sweep_silences(conn)
        return result

    import os
    web_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
    web_index = os.path.join(web_root, "index.html")

    handler = type("AlertHandler", (JsonRouter,), {
        "routes": [
            ("GET", "/health", h_health),
            ("POST", "/api/events", h_events),
            ("GET", "/api/alerts", h_alerts),
            ("GET", "/api/notifications", h_notifications),
            ("GET", "/api/silence", h_silence_get),
            ("POST", "/api/silence", h_silence_set),
            ("DELETE", "/api/silence", h_silence_clear),
        ],
        "static_index": web_index,
        "static_dir": os.path.join(web_root, "static"),
    })
    server = _ReusableHTTPServer((host, port), handler)
    server.stop_event = stop  # type: ignore[attr-defined]
    server.db_conn = conn  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------- #
# Notifier runner
# --------------------------------------------------------------------------- #
def run_notifier_service(host: str, port: int) -> ThreadingHTTPServer:
    cfg = NotifierConfig()
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    notifier_core.init_notifier_db(conn)

    def h_health(_: Any) -> dict:
        return {"ok": True, "service": "local-notifier", "lose_first_n": cfg.lose_first_n}

    def h_receive(body: Any) -> tuple[int, dict]:
        return notifier_core.receive_notification(conn, body, cfg.lose_first_n)

    def h_list(_: Any) -> dict:
        return {"notifications": notifier_core.list_received(conn)}

    import os
    web_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
    web_index = os.path.join(web_root, "notifier.html")

    handler = type("NotifierHandler", (JsonRouter,), {
        "routes": [
            ("GET", "/health", h_health),
            ("POST", "/api/notifications", h_receive),
            ("GET", "/api/notifications", h_list),
        ],
        "static_index": web_index,
        "static_dir": os.path.join(web_root, "static"),
    })
    server = _ReusableHTTPServer((host, port), handler)
    server.db_conn = conn  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
