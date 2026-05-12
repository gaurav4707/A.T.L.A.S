"""WebSocket connection manager for ATLAS broadcast events."""

from __future__ import annotations

from fastapi import WebSocket


class WSManager:
    """Track active WebSocket clients and fan out JSON messages."""

    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        """Accept and register a new WebSocket connection."""
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        """Remove a WebSocket connection if it is still tracked."""
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict) -> None:
        """Send one JSON message to every connected client."""
        disconnected: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.append(ws)

        for ws in disconnected:
            self.disconnect(ws)


ws_manager = WSManager()