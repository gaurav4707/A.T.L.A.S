"""wake_word.py - Wake word detection for ATLAS v2 using openWakeWord."""

from __future__ import annotations

import threading
from collections import deque

import numpy as np
import sounddevice as sd

import settings
from voice import transcribe_from_array, _dispatch

SAMPLE_RATE = 16000
CHUNK = 1280  # 80ms required by openWakeWord
PREROLL_CHUNKS = 19  # ~1.5s pre-roll

_stop_event = threading.Event()
_listener_thread: threading.Thread | None = None
_watchdog_thread: threading.Thread | None = None
_pre_roll: deque[np.ndarray] = deque(maxlen=PREROLL_CHUNKS)


def _resolve_wakeword_model_name() -> str:
    """Map user-facing wake phrase model names to OpenWakeWord model IDs."""
    configured = str(settings.get("wake_word_model") or "hey_atlas").strip().lower()
    if configured == "hey_atlas":
        return "hey_jarvis"
    return configured


def _wake_phrase() -> str:
    """Return user-facing wake phrase from settings."""
    configured = str(settings.get("wake_word_model") or "hey_atlas").strip().lower()
    return configured.replace("_", " ")

try:
    from openwakeword.model import Model as OWWModel

    _oww_model = OWWModel(wakeword_models=[_resolve_wakeword_model_name()], inference_framework="onnx")
    _available = True
except Exception as exc:
    _oww_model = None
    _available = False
    print(f"[yellow]openWakeWord not available: {exc}[/yellow]")


def is_available() -> bool:
    """Return whether wake-word backend is available."""
    return _available


def _record_command(stream: sd.InputStream) -> np.ndarray | None:
    """Read from existing stream until silence, including pre-roll."""
    silence_ms = int(settings.get("vad_silence_ms") or 1500)
    silence_chunks_needed = max(1, int(silence_ms / 80))
    max_chunks = int(SAMPLE_RATE * 10 / CHUNK)

    frames = list(_pre_roll)
    consecutive_silence = 0

    for _ in range(max_chunks):
        try:
            chunk, _ = stream.read(CHUNK)
        except Exception:
            break

        audio_np = chunk.reshape(-1).astype(np.int16, copy=False)
        frames.append(audio_np.copy())

        energy = float(np.abs(audio_np).mean())
        if energy < 200:
            consecutive_silence += 1
        else:
            consecutive_silence = 0

        if consecutive_silence >= silence_chunks_needed:
            break

    if len(frames) < 3:
        return None
    return np.concatenate(frames)


def _on_wake_word(stream: sd.InputStream) -> None:
    """Handle wake-word trigger using already-open stream."""
    try:
        from api.ws_manager import ws_manager
        import asyncio

        asyncio.run(ws_manager.broadcast({"type": "listening_start"}))
    except Exception:
        pass

    print("\n[blue]ATLAS: Listening...[/blue]")

    audio = _record_command(stream)
    if audio is None:
        return

    text = transcribe_from_array(audio)
    if not text:
        return

    print(f"[dim]Heard: {text}[/dim]")
    _dispatch(text)


def _listen_loop() -> None:
    """Main wake-word listener loop. Exceptions continue and never break loop."""
    threshold = float(settings.get("wake_word_threshold") or 0.35)
    print(f"[green]Wake word active - say '{_wake_phrase()}'[/green]")

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=CHUNK,
    ) as stream:
        while not _stop_event.is_set():
            try:
                chunk, _ = stream.read(CHUNK)
                audio_np = chunk.reshape(-1).astype(np.int16, copy=False)
                _pre_roll.append(audio_np.copy())

                pred = _oww_model.predict(audio_np)
                if any(float(score) > threshold for score in pred.values()):
                    try:
                        _oww_model.reset()
                    except Exception:
                        pass
                    _on_wake_word(stream)
            except Exception as exc:
                print(f"[dim]Wake word loop warning: {exc}[/dim]")
                continue


def _start_thread() -> None:
    """Start listener thread."""
    global _listener_thread

    _listener_thread = threading.Thread(target=_listen_loop, daemon=True)
    _listener_thread.start()


def _watchdog() -> None:
    """Restart dead listener every 15 seconds while enabled."""
    while not _stop_event.is_set():
        _stop_event.wait(timeout=15)
        if _stop_event.is_set():
            break
        if _listener_thread is not None and not _listener_thread.is_alive():
            print("[yellow]Wake word listener died - restarting[/yellow]")
            _start_thread()


def start_wake_word_listener() -> bool:
    """Start wake-word listener and watchdog."""
    global _watchdog_thread

    if not _available:
        print("[yellow]openWakeWord not installed - wake word disabled[/yellow]")
        return False

    _stop_event.clear()
    _start_thread()

    if _watchdog_thread is None or not _watchdog_thread.is_alive():
        _watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
        _watchdog_thread.start()

    return True


def stop_wake_word_listener() -> None:
    """Stop wake-word listener and watchdog."""
    _stop_event.set()


def is_listening() -> bool:
    """Return true when listener thread is alive and not stopped."""
    return _listener_thread is not None and _listener_thread.is_alive() and not _stop_event.is_set()
