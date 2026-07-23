"""Unified broadcaster for ATLAS events."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any


def broadcast_sync(payload: dict[str, Any]) -> None:
    """
    Broadcast a message to all WebSocket clients from a synchronous context.
    Attempts to use the running event loop or creates a temporary one.
    """
    from api.ws_manager import ws_manager

    async def _do_broadcast():
        await ws_manager.broadcast(payload)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If we're in a thread with a running loop (like FastAPI), schedule it.
            loop.create_task(_do_broadcast())
        else:
            # If the loop exists but isn't running, run it until complete.
            loop.run_until_complete(_do_broadcast())
    except RuntimeError:
        # No event loop in this thread, create a new temporary one.
        asyncio.run(_do_broadcast())
    except Exception as exc:
        logging.error("Broadcast failed: %s", exc)


async def broadcast_async(payload: dict[str, Any]) -> None:
    """Broadcast a message to all WebSocket clients from an async context."""
    from api.ws_manager import ws_manager
    await ws_manager.broadcast(payload)
