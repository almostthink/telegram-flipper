"""WebSocket для live-обновлений UI.

На Этапе 0 умеет только heartbeat и эхо — но канал уже поднят, поэтому
сканер и торговый движок на следующих этапах просто начнут в него писать.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)
router = APIRouter()

HEARTBEAT_SEC = 15


class Hub:
    """Реестр подключённых клиентов с широковещательной рассылкой."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def leave(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, event: str, payload: dict) -> None:
        message = json.dumps({"event": event, "payload": payload}, ensure_ascii=False)
        async with self._lock:
            targets = list(self._clients)

        dead = []
        for client in targets:
            try:
                await client.send_text(message)
            except (WebSocketDisconnect, RuntimeError):
                dead.append(client)

        if dead:
            async with self._lock:
                self._clients.difference_update(dead)


hub = Hub()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await hub.join(ws)
    heartbeat = asyncio.create_task(_heartbeat(ws))
    try:
        while True:
            text = await ws.receive_text()
            # Эхо нужно фронту, чтобы измерять задержку соединения.
            await ws.send_text(json.dumps({"event": "echo", "payload": text}))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WebSocket упал с ошибкой")
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await hub.leave(ws)


async def _heartbeat(ws: WebSocket) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SEC)
        await ws.send_text(json.dumps({"event": "heartbeat", "payload": {}}))
