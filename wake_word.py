"""wake_word.py - Wake word detection for ATLAS v2 using openWakeWord."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd

import classifier
import executor
import llm_engine
import memory
import settings
import voice

SAMPLE_RATE = 16000
CHUNK = 1280  # 80ms required by openWakeWord
PREROLL_CHUNKS = 19  # ~1.5s pre-roll

_stop_event = threading.Event()
_listener_thread: threading.Thread | None = None
_watchdog_thread: threading.Thread | None = None
_pre_roll: deque[np.ndarray] = deque(maxlen=PREROLL_CHUNKS)
_WAKE_LOCK = threading.Lock()
_CAPTURING = threading.Event()


def _broadcast_event(payload: dict[str, str]) -> None:
    """Best-effort websocket event broadcast for HUD state updates."""
    try:
        from api.ws_manager import ws_manager
        import asyncio

        asyncio.run(ws_manager.broadcast(payload))
    except Exception:
        pass


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


def _listen_loop() -> None:
    """Main wake-word listener loop. Exceptions continue and never break loop."""
    print(f"[green]Wake word active - say '{_wake_phrase()}'[/green]")
    chunk_size = CHUNK
    sample_rate = SAMPLE_RATE
    silence_ms = int(settings.get("vad_silence_ms") or 1500)
    max_silent_chunks = max(1, int((silence_ms / 1000.0) * sample_rate / chunk_size))

    while not _stop_event.is_set():
        try:
            with sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=chunk_size,
            ) as stream:
                while not _stop_event.is_set():
                    try:
                        frame, _ = stream.read(chunk_size)
                        if _oww_model is None:
                            continue

                        frame_i16 = frame.reshape(-1).astype(np.int16, copy=False)
                        _pre_roll.append(frame_i16.copy())
                        audio_np = frame_i16.astype(np.float32) / 32768.0

                        # Detection mode on shared stream.
                        prediction = _oww_model.predict(audio_np)
                        if not isinstance(prediction, dict):
                            continue

                        threshold = float(settings.get("wake_word_threshold") or 0.35)
                        if not any(float(score) > threshold for score in prediction.values()):
                            continue

                        # Wake word detected: switch to capture mode on same stream.
                        if not _WAKE_LOCK.acquire(blocking=False):
                            continue

                        _CAPTURING.set()
                        try:
                            print("\n[blue]ATLAS: Listening...[/blue]", flush=True)
                            _broadcast_event({"type": "listening_start"})

                            drain_chunks = int(0.2 * sample_rate / chunk_size)
                            for _ in range(drain_chunks):
                                if _stop_event.is_set():
                                    break
                                stream.read(chunk_size)

                            audio_chunks: list[np.ndarray] = []
                            speech_started = False
                            silent_count = 0
                            deadline = time.time() + 4.0

                            while not _stop_event.is_set():
                                cap_frame, _ = stream.read(chunk_size)
                                cap_i16 = cap_frame.reshape(-1).astype(np.int16, copy=False)
                                energy = int(np.max(np.abs(cap_i16))) if cap_i16.size else 0

                                if not speech_started:
                                    if energy > 800:
                                        speech_started = True
                                        audio_chunks.append(cap_i16.copy())
                                    elif time.time() > deadline:
                                        break
                                else:
                                    audio_chunks.append(cap_i16.copy())
                                    if energy < 800:
                                        silent_count += 1
                                        if silent_count >= max_silent_chunks:
                                            break
                                    else:
                                        silent_count = 0

                            if not audio_chunks:
                                continue

                            audio_i16 = np.concatenate(audio_chunks)
                            text = voice.transcribe_from_array(audio_i16)
                            normalized = text.strip().lower()
                            if not normalized:
                                continue

                            print(f"[dim]Heard: {normalized}[/dim]", flush=True)

                            killswitch_word = str(settings.get("killswitch_word") or "stop").strip().lower()
                            if normalized == killswitch_word:
                                try:
                                    import killswitch as ks

                                    ks.fire()
                                except Exception:
                                    pass
                                continue

                            context_text = memory.get_context_for_llm(normalized)
                            parsed = classifier.classify(normalized) or llm_engine.query(normalized, context_text)
                            action = str(parsed.get("action", ""))
                            params = parsed.get("params", {})
                            if not isinstance(params, dict):
                                params = {}
                            execution = executor.execute(action, params)
                            response = str(execution.get("message", "Done."))
                            memory.add_to_sliding("user", normalized)
                            memory.add_to_sliding("assistant", response)
                            voice.speak(response)
                        finally:
                            _CAPTURING.clear()
                            _WAKE_LOCK.release()
                    except Exception as exc:
                        logging.warning("Wake word frame error: %s", exc)
                        continue
        except Exception as exc:
            logging.warning("Wake word loop warning: %s", exc)
            time.sleep(0.5)
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
