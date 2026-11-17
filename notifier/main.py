"""FastAPI local notification endpoint with simulated lost receipts."""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, Literal

from common import notifier_core
from common.config import NotifierConfig
from common.db import connect

config = NotifierConfig()
_conn = connect(config.db_path)
notifier_core.init_notifier_db(_conn)

app = FastAPI(title="Local Notifier", version="1.0.0")


class NotificationIn(BaseModel):
    notification_no: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    round: int = Field(ge=1)
    kind: Literal["firing", "recovery"]
    summary: str = Field(min_length=1)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "local-notifier", "lose_first_n": config.lose_first_n}


@app.post("/api/notifications")
def receive(body: NotificationIn) -> JSONResponse:
    status, payload = notifier_core.receive_notification(
        _conn, body.model_dump(), config.lose_first_n
    )
    return JSONResponse(status_code=status, content=payload)


@app.get("/api/notifications")
def list_all() -> dict:
    return {"notifications": notifier_core.list_received(_conn)}


_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(_WEB_DIR, "notifier.html"))


app.mount("/static", StaticFiles(directory=os.path.join(_WEB_DIR, "static")), name="static")
