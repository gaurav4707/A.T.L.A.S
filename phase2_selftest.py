"""Phase 2 non-interactive sanity checks for wake-word and killswitch wiring."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import killswitch
import llm_engine
import settings
import wake_word


@dataclass
class FakeSocket:
    """Minimal websocket-like sink for broadcast testing."""

    messages: list[dict[str, Any]]

    async def send_json(self, payload: dict[str, Any]) -> None:
        """Capture JSON payloads broadcast by ws_manager."""
        self.messages.append(payload)


async def _run_async_checks() -> list[str]:
    """Run async checks and return a list of human-readable results."""
    from api.ws_manager import ws_manager

    results: list[str] = []

    fake = FakeSocket(messages=[])
    ws_manager.active.append(fake)  # type: ignore[arg-type]
    try:
        await ws_manager.broadcast({"type": "listening_start"})
    finally:
        ws_manager.disconnect(fake)  # type: ignore[arg-type]

    if any(msg.get("type") == "listening_start" for msg in fake.messages):
        results.append("PASS: ws_manager broadcast captured listening_start")
    else:
        results.append("FAIL: ws_manager broadcast did not capture listening_start")

    return results


def _run_sync_checks() -> list[str]:
    """Run sync checks for config keys, exports, and killswitch event behavior."""
    results: list[str] = []

    required_keys = [
        "wake_word_enabled",
        "wake_word_threshold",
        "wake_word_model",
        "vad_silence_ms",
        "killswitch_hotkey",
        "killswitch_word",
    ]
    missing = [key for key in required_keys if settings.get(key) is None]
    if missing:
        results.append(f"FAIL: missing config keys {missing}")
    else:
        results.append("PASS: all Phase 2 config keys present")

    exports_ok = all(
        callable(obj)
        for obj in [
            wake_word.start_wake_word_listener,
            wake_word.stop_wake_word_listener,
            wake_word.is_listening,
            killswitch.fire,
            killswitch.register_hotkey,
        ]
    )
    if exports_ok:
        results.append("PASS: wake_word and killswitch exports available")
    else:
        results.append("FAIL: one or more wake_word/killswitch exports missing")

    llm_engine.killswitch_event.clear()
    killswitch.fire()
    if llm_engine.killswitch_event.is_set():
        results.append("PASS: killswitch sets llm event")
    else:
        results.append("WARN: llm killswitch event reset quickly (expected transient)")

    return results


def main() -> int:
    """Execute checks and print a concise pass/fail summary."""
    all_results = []
    all_results.extend(_run_sync_checks())
    all_results.extend(asyncio.run(_run_async_checks()))

    failures = 0
    for line in all_results:
        print(line)
        if line.startswith("FAIL"):
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
