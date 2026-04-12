"""Wake-word listener for ATLAS using OpenWakeWord (fully offline, no API key required)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

import numpy as np
import sounddevice

# Lazy import for openwakeword - only loaded if wake word is actually enabled
# from openwakeword.model import Model
Model = None  # Placeholder for lazy loading

from api.ws_manager import ws_manager
import classifier
import executor
import llm_engine
import memory
import settings
import voice

_LISTENER_THREAD: threading.Thread | None = None
_WATCHDOG_THREAD: threading.Thread | None = None
_STOP_EVENT = threading.Event()
_IS_LISTENING = False
_OWW_MODEL: Any | None = None
_WAKE_LOCK = threading.Lock()


def _broadcast_event(message: dict[str, Any]) -> None:
    """Broadcast websocket events from sync code regardless of loop state."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(ws_manager.broadcast(message))
        return
    except RuntimeError:
        pass

    try:
        asyncio.run(ws_manager.broadcast(message))
    except Exception:
        pass


def _get_wakeword_model() -> str:
    """Return the OpenWakeWord model to load from config."""
    configured = str(settings.get("wake_word_model") or "hey_atlas").strip().lower()
    # `hey_atlas` is the user-facing alias. Backend model is `hey_jarvis`.
    if configured == "hey_atlas":
        return "hey_jarvis"
    return configured


def _get_wakeword_phrase() -> str:
    """Return the user-facing wake phrase from config."""
    configured = str(settings.get("wake_word_model") or "hey_atlas").strip().lower()
    return configured.replace("_", " ")


def _prepare_model_assets(model_name: str) -> None:
    """Download missing OpenWakeWord assets for the configured model once."""
    from openwakeword import utils as oww_utils

    oww_utils.download_models(model_names=[model_name])


def _record_until_silence(stream: Any, silence_ms: int, frame_length: int) -> np.ndarray:
    """Capture speech after wake word using the already-open microphone stream."""
    sample_rate = 16000
    max_silence_frames = max(1, int((silence_ms / 1000.0) * sample_rate / frame_length))

    silent_frames = 0
    pcm_chunks: list[np.ndarray] = []

    started = False
    while not _STOP_EVENT.is_set():
        frame, _ = stream.read(frame_length)
        frame_i16 = frame.reshape(-1).astype(np.int16, copy=False)
        pcm_chunks.append(frame_i16.copy())

        energy = int(np.max(np.abs(frame_i16))) if frame_i16.size else 0
        is_speech = energy > 700

        if is_speech:
            started = True
            silent_frames = 0
        elif started:
            silent_frames += 1
            if silent_frames >= max_silence_frames:
                break

    if not pcm_chunks:
        return np.array([], dtype=np.int16)
    return np.concatenate(pcm_chunks)


def _on_wake_word(stream: Any, frame_length: int) -> None:
    """Handle wake-word detection and execute the recognized command."""
    print("\n[blue]ATLAS: Listening...[/blue]", flush=True)
    _broadcast_event({"type": "listening_start"})

    silence_ms = int(settings.get("vad_silence_ms") or 1500)
    audio_i16 = _record_until_silence(stream=stream, silence_ms=silence_ms, frame_length=frame_length)
    text = voice.transcribe_from_array(audio_i16)
    normalized = text.strip().lower()

    if not normalized:
        return

    print(f"[dim]Heard: {normalized}[/dim]", flush=True)
    if normalized == str(settings.get("killswitch_word") or "stop").strip().lower():
        try:
            import killswitch

            killswitch.fire()
        except Exception:
            pass
        return

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


def _listen_loop() -> None:
    """Continuously process OpenWakeWord frames until stop is requested."""
    global _IS_LISTENING

    _IS_LISTENING = True
    chunk_size = 1280  # 80ms at 16kHz — OpenWakeWord's expected frame size
    while not _STOP_EVENT.is_set():
        try:
            if _OWW_MODEL is None:
                time.sleep(0.2)
                continue

            with sounddevice.InputStream(
                samplerate=16000,
                channels=1,
                dtype="int16",
                blocksize=chunk_size,
            ) as stream:
                while not _STOP_EVENT.is_set():
                    try:
                        frame, _ = stream.read(chunk_size)
                        if _OWW_MODEL is None:
                            continue

                        frame_i16 = frame.reshape(-1).astype(np.int16, copy=False)
                        audio_np = frame_i16.astype(np.float32) / 32768.0

                        prediction = _OWW_MODEL.predict(audio_np)
                        if not isinstance(prediction, dict):
                            continue

                        threshold = float(settings.get("wake_word_threshold") or 0.35)
                        if any(float(score) > threshold for score in prediction.values()):
                            if _WAKE_LOCK.acquire(blocking=False):
                                try:
                                    _on_wake_word(stream=stream, frame_length=chunk_size)
                                except Exception as exc:
                                    logging.warning("Wake-word handler error: %s", exc)
                                finally:
                                    _WAKE_LOCK.release()
                    except Exception as exc:
                        logging.warning("Wake-word frame error: %s", exc)
                        continue
        except Exception as exc:
            logging.warning("Wake-word stream error: %s", exc)
            time.sleep(0.25)
            continue

    _IS_LISTENING = False


def _spawn_listener_thread() -> None:
    """Spawn wake-word listener thread."""
    global _LISTENER_THREAD
    _LISTENER_THREAD = threading.Thread(target=_listen_loop, name="atlas-wake-word", daemon=True)
    _LISTENER_THREAD.start()


def _watchdog_loop() -> None:
    """Keep wake-word listener alive when enabled."""
    while not _STOP_EVENT.is_set():
        try:
            if bool(settings.get("wake_word_enabled")) and _OWW_MODEL is not None:
                if _LISTENER_THREAD is None or not _LISTENER_THREAD.is_alive():
                    logging.warning("Wake-word listener died; restarting")
                    _spawn_listener_thread()
        except Exception as exc:
            logging.warning("Wake-word watchdog error: %s", exc)
        _STOP_EVENT.wait(1.0)


def start_wake_word_listener() -> bool:
    """Start wake-word listener thread if enabled."""
    global _WATCHDOG_THREAD, _OWW_MODEL

    if _LISTENER_THREAD is not None and _LISTENER_THREAD.is_alive():
        return True

    if not bool(settings.get("wake_word_enabled")):
        return False

    _STOP_EVENT.clear()

    try:
        from openwakeword.model import Model as OWWModel
        model_name = _get_wakeword_model()
        _prepare_model_assets(model_name)
        _OWW_MODEL = OWWModel(
            wakeword_models=[model_name],
            inference_framework="onnx",
        )
        logging.info("OpenWakeWord initialized (model=%s)", model_name)
    except Exception as exc:
        error_msg = str(exc)
        logging.error("OpenWakeWord initialization failed: %s", exc)
        print(f"[yellow]Wake word unavailable: {error_msg[:80]}[/yellow]", flush=True)
        print("[dim]Set wake_word_model to 'hey_jarvis' and ensure internet on first run for model download.[/dim]", flush=True)
        print("[dim]Using push-to-talk fallback instead[/dim]", flush=True)
        _OWW_MODEL = None
        return False

    _spawn_listener_thread()
    if _WATCHDOG_THREAD is None or not _WATCHDOG_THREAD.is_alive():
        _WATCHDOG_THREAD = threading.Thread(target=_watchdog_loop, name="atlas-wake-watchdog", daemon=True)
        _WATCHDOG_THREAD.start()

    print(f"[voice] Wake word active - listening for '{_get_wakeword_phrase()}'", flush=True)
    return True


def stop_wake_word_listener() -> None:
    """Stop wake-word listener and release OpenWakeWord resources."""
    global _LISTENER_THREAD, _WATCHDOG_THREAD, _OWW_MODEL

    _STOP_EVENT.set()
    if _LISTENER_THREAD is not None:
        _LISTENER_THREAD.join(timeout=1.5)
    _LISTENER_THREAD = None
    if _WATCHDOG_THREAD is not None:
        _WATCHDOG_THREAD.join(timeout=1.5)
    _WATCHDOG_THREAD = None

    if _OWW_MODEL is not None:
        try:
            _OWW_MODEL = None
        except Exception:
            pass


def is_listening() -> bool:
    """Return whether listener thread is currently active."""
    return bool(_IS_LISTENING and _LISTENER_THREAD is not None and _LISTENER_THREAD.is_alive())


def calibrate_wake_word(seconds: float = 3.0) -> dict[str, float | str]:
    """Measure ambient wake-word scores and suggest a threshold."""
    if _OWW_MODEL is None:
        return {"error": "wake-word model not initialized"}

    frame_length = 1280
    max_score = 0.0
    total = 0
    end_time = time.time() + max(1.0, float(seconds))

    with sounddevice.InputStream(
        samplerate=16000,
        channels=1,
        dtype="int16",
        blocksize=frame_length,
    ) as stream:
        while time.time() < end_time:
            frame, _ = stream.read(frame_length)
            frame_i16 = frame.reshape(-1).astype(np.int16, copy=False)
            audio_np = frame_i16.astype(np.float32) / 32768.0
            prediction = _OWW_MODEL.predict(audio_np)
            if isinstance(prediction, dict) and prediction:
                max_score = max(max_score, max(float(value) for value in prediction.values()))
            total += 1

    recommended = min(0.8, max(0.2, max_score + 0.08))
    return {
        "model": _get_wakeword_model(),
        "phrase": _get_wakeword_phrase(),
        "ambient_max_score": round(max_score, 4),
        "recommended_threshold": round(recommended, 4),
        "samples": float(total),
    }
