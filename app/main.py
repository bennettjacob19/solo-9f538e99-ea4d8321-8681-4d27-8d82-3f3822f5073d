"""FastAPI alert service: event ingestion, silence console, outbox worker."""
from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, Literal

from common import service
from common.config import AppConfig
from common.db import connect, init_db

config = AppConfig()
_conn = connect(config.db_path)


# --------------------------------------------------------------------------- #
# Background worker: outbox delivery + silence expiry sweep
# --------------------------------------------------------------------------- #
class Worker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="outbox-worker")
        self._stop = threading.Event()
        self._client = httpx.Client(timeout=config.notify_timeout_seconds)

    def stop(self) -> None:
        self._stop.set()

    def _send(self, payload: dict) -> tuple[bool, Optional[str]]:
        try:
            resp = self._client.post(f"{config.notifier_url}/api/notifications", json=payload)
            if resp.status_code == 200:
                return True, None
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                service.dispatch_pending(_conn, self._send)
                service.sweep_silences(_conn)
            except Exception as exc:  # worker must never die
                print(f"[worker] tick failed: {exc!r}", flush=True)
            self._stop.wait(config.worker_interval_seconds)
        self._client.close()


_worker: Optional[Worker] = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _worker
    init_db(config.db_path)
    _worker = Worker()
    _worker.start()
    yield
    assert _worker is not None
    _worker.stop()
    _worker.join(timeout=5)


app = FastAPI(title="Alert Console", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class EventIn(BaseModel):
    fingerprint: str = Field(min_length=1)
    seq: int = Field(ge=0)
    kind: Literal["firing", "recovery"]
    summary: str = Field(min_length=1)


class SilenceIn(BaseModel):
    duration_seconds: Optional[float] = Field(default=None, ge=0)
    until_ts: Optional[float] = None
    reason: str = ""


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "alert-console"}


@app.post("/api/events")
def post_event(event: EventIn) -> dict:
    try:
        return service.ingest_event(_conn, event.model_dump())
    except service.Reject as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": exc.message})


@app.get("/api/alerts")
def get_alerts() -> dict:
    return {"alerts": service.list_alerts(_conn), "silence": service.get_silence(_conn)}


@app.get("/api/alerts/{fingerprint}/events")
def get_alert_events(fingerprint: str) -> dict:
    return {"fingerprint": fingerprint, "events": service.list_events(_conn, fingerprint)}


@app.get("/api/notifications")
def get_notifications() -> dict:
    return {"notifications": service.list_notifications(_conn)}


@app.get("/api/silence")
def get_silence_state() -> dict:
    return service.get_silence(_conn)


@app.post("/api/silence")
def set_silence_state(body: SilenceIn) -> dict:
    now = time.time()
    if body.until_ts is not None:
        until_ts = body.until_ts
    elif body.duration_seconds is not None:
        until_ts = now + body.duration_seconds
    else:
        raise HTTPException(status_code=422,
                            detail="either duration_seconds or until_ts is required")
    if until_ts <= now:
        raise HTTPException(status_code=422, detail="silence must end in the future")
    return service.set_silence(_conn, until_ts, body.reason, now)


@app.delete("/api/silence")
def delete_silence() -> dict:
    result = service.clear_silence(_conn)
    # Releasing the silence immediately parks the still-firing alerts into
    # the outbox instead of waiting for the next worker tick.
    result["released"] = service.sweep_silences(_conn)
    return result


# --------------------------------------------------------------------------- #
# Web UI
# --------------------------------------------------------------------------- #
_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(_WEB_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(_WEB_DIR, "static")), name="static")
