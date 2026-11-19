"""Связь кнопки веб-чата с нативным портретом панели рабочего стола.

Панель сообщает состояние и забирает команду даже при убранном портрете.
Команда остаётся до подтверждения; просроченные команды не исполняются.
"""

import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.security import PersonaId

HEARTBEAT_TTL = 8.0
COMMAND_TTL = 15.0


class DesktopPortrait:
    def __init__(self):
        self._lock = threading.Lock()
        self._seen: float | None = None
        self._shown = False
        self._persona = ""
        self._command: dict | None = None
        self._command_at = 0.0

    def _status(self, now: float) -> dict:
        connected = self._seen is not None and now - self._seen < HEARTBEAT_TTL
        if not connected or now - self._command_at >= COMMAND_TTL:
            self._command = None
        return {"connected": connected, "shown": connected and self._shown,
                "persona": self._persona if connected else "",
                "pending": dict(self._command) if self._command else None}

    def status(self) -> dict:
        with self._lock:
            return self._status(time.monotonic())

    def set(self, persona: str, shown: bool) -> dict:
        with self._lock:
            now = time.monotonic()
            if not self._status(now)["connected"]:
                raise RuntimeError("desktop_unavailable")
            self._command = {"id": uuid.uuid4().hex, "persona": persona, "shown": shown}
            self._command_at = now
            return self._status(now)

    def report(self, persona: str, shown: bool, applied: str) -> dict:
        with self._lock:
            now = time.monotonic()
            # Проверяем срок до обновления heartbeat: команда прежней панели
            # не должна всплыть при её следующем запуске.
            self._status(now)
            self._seen = now
            self._persona, self._shown = persona, shown
            if self._command and self._command["id"] == applied:
                self._command = None
            return self._status(now)


bridge = DesktopPortrait()


class PortraitSet(BaseModel):
    persona: PersonaId
    shown: bool


class PortraitReport(BaseModel):
    persona: str = Field("", max_length=80, pattern=r"^[A-Za-z0-9_-]*$")
    shown: bool
    applied: str = Field("", max_length=64)


def create_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/api/desktop/portrait", dependencies=[Depends(require_auth)])

    @router.get("")
    async def status():
        return bridge.status()

    @router.post("")
    async def set_portrait(req: PortraitSet):
        import asyncio
        from app.api import runtime
        if await asyncio.to_thread(runtime.get_persona_info, req.persona) is None:
            raise HTTPException(status_code=404, detail="persona_not_found")
        try:
            return bridge.set(req.persona, req.shown)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/report")
    async def report(req: PortraitReport):
        return bridge.report(req.persona, req.shown, req.applied)

    return router
