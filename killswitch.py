"""ATLAS emergency stop controls for voice output and streaming responses."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import keyboard as kb

from api.ws_manager import ws_manager
import settings
import voice

_stop_event = threading.Event()
_current_tts_process: Any | None = None


def _broadcast_killswitch() -> None:
    """Notify connected HUD clients that a kill event was fired."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(ws_manager.broadcast({"type": "killswitch"}))
        return
    except RuntimeError:
        pass

    try:
        asyncio.run(ws_manager.broadcast({"type": "killswitch"}))
    except Exception:
        pass


def _set_llm_kill_event() -> None:
    """Set llm_engine kill signal if the module exposes a kill event."""
    try:
        import llm_engine

        event_obj = getattr(llm_engine, "killswitch_event", None)
        if isinstance(event_obj, threading.Event):
            event_obj.set()
    except Exception:
        pass


def _clear_llm_kill_event() -> None:
    """Clear llm_engine kill signal if it exists."""
    try:
        import llm_engine

        event_obj = getattr(llm_engine, "killswitch_event", None)
        if isinstance(event_obj, threading.Event):
            event_obj.clear()
    except Exception:
        pass


def fire() -> None:
    """Immediately stop speech output and signal all active streaming paths."""
    voice.stop_speaking()
    _set_llm_kill_event()
    _stop_event.set()
    _broadcast_killswitch()
    print("\n[red]Stopped.[/red]", flush=True)

    def _reset_flags() -> None:
        _clear_llm_kill_event()
        _stop_event.clear()

    threading.Timer(0.2, _reset_flags).start()


def register_hotkey() -> None:
    """Register global keyboard shortcut for emergency stop."""
    hotkey = str(settings.get("killswitch_hotkey") or "ctrl+shift+k")
    kb.add_hotkey(hotkey, fire)


def is_triggered() -> bool:
    """Return whether the transient kill event is currently set."""
    return _stop_event.is_set()
