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
    threshold = float(settings.get("wake_word_threshold") or 0.35)
    silence_ms = int(settings.get("vad_silence_ms") or 1500)
    max_silent = max(1, int((silence_ms / 1000.0) * SAMPLE_RATE / CHUNK))

    device = settings.get("voice_input_device")
    device_index = int(device) if device is not None else None

    while not _stop_event.is_set():
        try:
            kwargs: dict = dict(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=CHUNK,
            )
            if device_index is not None:
                kwargs["device"] = device_index

            with sd.InputStream(**kwargs) as stream:
                print(f"[green]Wake word active - say '{_wake_phrase()}'[/green]", flush=True)
                while not _stop_event.is_set():
                    try:
                        frame, _ = stream.read(CHUNK)
                        frame_i16 = frame.reshape(-1).astype(np.int16, copy=False)
                        audio_f = frame_i16.astype(np.float32) / 32768.0

                        if _oww_model is None:
                            continue

                        pred = _oww_model.predict(audio_f)
                        if not isinstance(pred, dict):
                            continue
                        if not any(float(s) > threshold for s in pred.values()):
                            continue

                        if not _WAKE_LOCK.acquire(blocking=False):
                            continue

                        _CAPTURING.set()
                        try:
                            print("\n[blue]ATLAS: Listening...[/blue]", flush=True)
                            _broadcast_event({"type": "listening_start"})

                            # Drain 200ms buffer on same stream
                            for _ in range(int(0.2 * SAMPLE_RATE / CHUNK)):
                                if _stop_event.is_set():
                                    break
                                stream.read(CHUNK)

                            # Capture command on same stream
                            chunks: list = []
                            started = False
                            silent = 0
                            deadline = time.time() + 4.0

                            while not _stop_event.is_set():
                                cf, _ = stream.read(CHUNK)
                                ci = cf.reshape(-1).astype(np.int16, copy=False)
                                energy = int(np.max(np.abs(ci))) if ci.size else 0

                                if not started:
                                    if energy > 800:
                                        started = True
                                        chunks.append(ci.copy())
                                    elif time.time() > deadline:
                                        break
                                else:
                                    chunks.append(ci.copy())
                                    if energy < 800:
                                        silent += 1
                                        if silent >= max_silent:
                                            break
                                    else:
                                        silent = 0

                            if not chunks:
                                continue

                            audio_i16 = np.concatenate(chunks)
                            text = voice.transcribe_from_array(audio_i16)
                            normalized = text.strip().lower()
                            if not normalized:
                                print("[dim]No speech detected.[/dim]", flush=True)
                                continue

                            print(f"[dim]Heard: {normalized}[/dim]", flush=True)

                            ks = str(settings.get("killswitch_word") or "stop").lower()
                            if normalized == ks:
                                try:
                                    import killswitch as _ks
                                    _ks.fire()
                                except Exception:
                                    pass
                                continue

                            ctx = memory.get_context_for_llm(normalized)
                            parsed = classifier.classify(normalized) or llm_engine.query(normalized, ctx)
                            action = str(parsed.get("action", ""))
                            params = parsed.get("params", {})
                            if not isinstance(params, dict):
                                params = {}
                            result = executor.execute(action, params)
                            resp = str(result.get("message", "Done."))
                            memory.add_to_sliding("user", normalized)
                            memory.add_to_sliding("assistant", resp)
                            voice.speak(resp)

                        finally:
                            _CAPTURING.clear()
                            _WAKE_LOCK.release()

                    except Exception as exc:
                        logging.warning("Wake word frame error: %s", exc)
                        continue

        except Exception as exc:
            logging.warning("Wake word loop warning: %s", exc)
            time.sleep(1.0)
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
