"""WebSocket hub for live notifications.

Pushes event, plan, proposal and approval notifications to every connected client. This
is a notification channel only: no command deck state is mutated here, and nothing an
assistant or client sends over the socket can activate a plan.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.state_hub import ADVISORY_NOTICE, SYNTHETIC_NOTICE

router = APIRouter()


@dataclass(slots=True)
class LiveMessage:
    kind: str
    payload: dict
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0))

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "occurred_at": self.occurred_at.isoformat().replace("+00:00", "Z"),
            "synthetic_data_notice": SYNTHETIC_NOTICE,
            "advisory_notice": ADVISORY_NOTICE,
            "payload": self.payload,
        }


class ConnectionHub:
    """Fan-out to all connected sockets. Keeps no demo state."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, message: LiveMessage) -> None:
        payload = message.as_dict()
        async with self._lock:
            targets = list(self._clients)
        dead: list[WebSocket] = []
        for client in targets:
            try:
                await client.send_json(payload)
            except Exception:  # pragma: no cover - client vanished mid-send
                dead.append(client)
        if dead:
            async with self._lock:
                for client in dead:
                    self._clients.discard(client)

    def broadcast_nowait(self, message: LiveMessage) -> None:
        """Fire-and-forget publish, safe to call from synchronous request handlers.

        Deliberately a plain ``def``: the route handlers that publish live messages are
        ``def`` (not ``async def``) so they run in FastAPI's thread pool. Making this a
        coroutine would mean every call site builds a coroutine that is never awaited,
        and no client is ever notified.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop (e.g. unit test)
            return
        loop.create_task(self.broadcast(message))


hub = ConnectionHub()


@router.websocket("/ws/live")
async def live_socket(websocket: WebSocket) -> None:
    """Live notification stream. Clients cannot change state through this channel."""
    await hub.connect(websocket)
    try:
        await websocket.send_json(
            LiveMessage(
                kind="connected",
                payload={
                    "message": "SOJOURNER live feed connected (synthetic demonstration data).",
                    "client_count": hub.client_count,
                },
            ).as_dict()
        )
        while True:
            # The socket is receive-driven so disconnects are detected promptly. Any
            # inbound text is acknowledged and ignored: this channel never mutates state.
            message = await websocket.receive_text()
            await websocket.send_json(
                LiveMessage(
                    kind="ack",
                    payload={
                        "received": message[:200],
                        "notice": "This channel is read-only; use the REST API for demo actions.",
                    },
                ).as_dict()
            )
    except WebSocketDisconnect:
        await hub.disconnect(websocket)
    except Exception:  # pragma: no cover - abnormal close
        await hub.disconnect(websocket)


__all__ = ["ConnectionHub", "LiveMessage", "hub", "live_socket", "router"]