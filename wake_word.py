"""wake_word.py - Wake word detection for ATLAS v2 using openWakeWord."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

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
_oww_model: Any | None = None
_available: bool | None = None
_active_backend_model: str = ""


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
    """Return the phrase the user should actually say.

    FIX BUG 5: The original always returned the *configured* name
    (e.g. 'hey atlas') even when the backend fell back to a completely
    different model (e.g. 'alexa'). The user would say 'hey atlas' while
    the model was listening for 'alexa' — wake word never triggered.

    Now returns the real backend model phrase so the startup message is
    truthful and actionable.
    """
    if _active_backend_model and _active_backend_model not in ("", "auto"):
        # Show what the model actually responds to
        return _active_backend_model.replace("_", " ")
    configured = str(settings.get("wake_word_model") or "hey_atlas").strip().lower()
    return configured.replace("_", " ")


def _candidate_backend_models() -> list[str]:
    """Return ordered backend model candidates for resilient startup."""
    preferred = _resolve_wakeword_model_name()
    candidates = [preferred]
    if preferred == "hey_jarvis":
        # Some openWakeWord builds ship without hey_jarvis; try common built-ins.
        candidates.extend(["hey_mycroft", "alexa", "computer"])

    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def _load_openwakeword_model() -> bool:
    """Lazy-load OpenWakeWord model with fallbacks for missing packaged models."""
    global _oww_model, _available, _active_backend_model

    if _available is True and _oww_model is not None:
        return True
    if _available is False:
        return False

    try:
        from openwakeword.model import Model as OWWModel
    except Exception as exc:
        _oww_model = None
        _available = False
        print(f"[yellow]openWakeWord import failed: {exc}[/yellow]")
        return False

    preferred = _resolve_wakeword_model_name()
    last_error: Exception | None = None

    for model_name in _candidate_backend_models():
        try:
            _oww_model = OWWModel(wakeword_models=[model_name], inference_framework="onnx")
            _active_backend_model = model_name
            _available = True
            if model_name != preferred:
                print(
                    f"[yellow]Wake model '{preferred}' unavailable; using '{model_name}' instead.[/yellow]",
                    flush=True,
                )
                print(
                    f"[yellow]Say '{model_name.replace('_', ' ')}' to activate ATLAS.[/yellow]",
                    flush=True,
                )
            return True
        except Exception as exc:
            last_error = exc

    try:
        _oww_model = OWWModel(inference_framework="onnx")
        _active_backend_model = "auto"
        _available = True
        print("[yellow]Using auto-discovered openWakeWord model set.[/yellow]", flush=True)
        return True
    except Exception as exc:
        last_error = exc

    _oww_model = None
    _available = False
    print(f"[yellow]openWakeWord not available: {last_error}[/yellow]")
    return False


def is_available() -> bool:
    """Return whether wake-word backend is available."""
    return _load_openwakeword_model()


def _listen_loop() -> None:
    threshold = float(settings.get("wake_word_threshold") or 0.35)
    silence_ms = int(settings.get("vad_silence_ms") or 1500)
    max_silent = max(1, int((silence_ms / 1000.0) * SAMPLE_RATE / CHUNK))

    device = settings.get("voice_input_device")
    device_index = int(device) if device is not None else None

    # FIX BUG 6: Track highest score seen per session so user can tune threshold
    _session_peak: float = 0.0

    while not _stop_event.is_set():
        kwargs: dict[str, Any] = {
            "samplerate": SAMPLE_RATE,
            "channels": 1,
            "dtype": "int16",
            "blocksize": CHUNK,
        }
        if device_index is not None:
            kwargs["device"] = device_index

        try:
            with sd.InputStream(**kwargs) as stream:
                print(
                    f"[green]Wake word active — say '{_wake_phrase()}' "
                    f"(threshold: {threshold:.2f})[/green]",
                    flush=True,
                )
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

                        # FIX BUG 6: Log scores above a debug floor so the user
                        # can see what the model is actually detecting, making
                        # threshold tuning possible without guessing.
                        for model_name, score in pred.items():
                            score_f = float(score)
                            if score_f > 0.05:
                                logging.debug(
                                    "OWW %s score=%.3f threshold=%.2f",
                                    model_name, score_f, threshold,
                                )
                            if score_f > _session_peak:
                                _session_peak = score_f
                                # Print when a new peak is reached above 0.1 —
                                # helps diagnose "model never triggers" issues.
                                if score_f > 0.1:
                                    print(
                                        f"[dim]Wake peak: {score_f:.3f} "
                                        f"(need >{threshold:.2f} to trigger)[/dim]",
                                        flush=True,
                                    )

                        if not any(float(s) > threshold for s in pred.values()):
                            continue

                        # Wake word detected — enter capture mode
                        if not _WAKE_LOCK.acquire(blocking=False):
                            continue

                        _CAPTURING.set()
                        try:
                            print("\n[blue]ATLAS: Listening...[/blue]", flush=True)
                            _broadcast_event({"type": "listening_start"})

                            # Drain 200ms of post-wakeword audio from buffer
                            for _ in range(int(0.2 * SAMPLE_RATE / CHUNK)):
                                if _stop_event.is_set():
                                    break
                                stream.read(CHUNK)

                            # Capture command on the SAME stream (no second open)
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
                                        print("[dim]No speech after wake — returning to detection.[/dim]", flush=True)
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
            message = str(exc)

            if device_index is not None:
                logging.warning("Wake word input device %s failed: %s", device_index, message)
                print(
                    f"[yellow]Wake word input device {device_index} failed: {message}[/yellow]",
                    flush=True,
                )
                print("[yellow]Retrying wake word with system default input device...[/yellow]", flush=True)
                device_index = None
                continue

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
            print("[yellow]Wake word listener died — restarting[/yellow]")
            _start_thread()


def start_wake_word_listener() -> bool:
    """Start wake-word listener and watchdog."""
    global _watchdog_thread

    if not _load_openwakeword_model():
        print("[yellow]Wake word backend unavailable — wake word disabled[/yellow]")
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
