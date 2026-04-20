"""wake_word.py — SPMC audio pipeline for ATLAS wake word and PTT."""

# Architecture: one producer thread owns the mic and puts raw int16 frames
# into _audio_queue.  One consumer thread reads from that queue and runs a
# state machine: DETECTING → CAPTURING → back to DETECTING.  PTT pauses OWW
# and redirects captured frames through the same Whisper/dispatch path.
# This eliminates the PaErrorCode -9983 crash that occurred when a second
# InputStream was opened while the first was still alive.

from __future__ import annotations

import logging
import queue
import threading
import time
from enum import Enum, auto
from typing import Any

import numpy as np
import sounddevice as sd

import classifier
import executor
import llm_engine
import memory
import settings
import voice

SAMPLE_RATE = 16_000
CHUNK = 1_280                # 80 ms — required by openWakeWord

COOLDOWN_SECONDS = 2.0       # Minimum gap between consecutive wake triggers
SPEECH_ENERGY = 800          # int16 peak to consider "speech started / still speaking"
MAX_SILENT_CHUNKS = 19       # ~1.5 s of silence → end of utterance
MAX_CAPTURE_SECONDS = 5.0    # Hard cap on command capture window
WAKE_DRAIN_CHUNKS = 4        # Frames to discard after trigger (~320 ms of wake audio)


class _State(Enum):
    DETECTING = auto()       # OWW running; looking for wake phrase
    CAPTURING = auto()       # Wake triggered; collecting command audio
    PTT_RECORDING = auto()   # PTT key held; collecting audio for push-to-talk


# ---------------------------------------------------------------------------
# Shared state (module-level singletons)
# ---------------------------------------------------------------------------

_audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)
_stop_event = threading.Event()

# Exported — voice.py sets/clears this when the PTT key is pressed/released
ptt_active = threading.Event()

_producer_thread: threading.Thread | None = None
_consumer_thread: threading.Thread | None = None
_watchdog_thread: threading.Thread | None = None

_oww_model: Any | None = None
_available: bool | None = None
_active_backend_model: str = ""
_cooldown_until: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _broadcast_event(payload: dict[str, str]) -> None:
    """Best-effort WebSocket broadcast for HUD state updates."""
    try:
        from api.ws_manager import ws_manager
        import asyncio
        asyncio.run(ws_manager.broadcast(payload))
    except Exception:
        pass


def _resolve_backend_model() -> str:
    configured = str(settings.get("wake_word_model") or "hey_atlas").strip().lower()
    return "hey_jarvis" if configured == "hey_atlas" else configured


def _wake_phrase() -> str:
    """Return the phrase the model actually listens for (not just config value)."""
    if _active_backend_model and _active_backend_model not in ("", "auto"):
        return _active_backend_model.replace("_", " ")
    return str(settings.get("wake_word_model") or "hey_atlas").replace("_", " ")


def _candidate_models() -> list[str]:
    preferred = _resolve_backend_model()
    extras = ["hey_mycroft", "alexa", "computer"] if preferred == "hey_jarvis" else []
    seen: set[str] = set()
    result: list[str] = []
    for m in [preferred] + extras:
        if m not in seen:
            result.append(m)
            seen.add(m)
    return result


# ---------------------------------------------------------------------------
# OWW model loading
# ---------------------------------------------------------------------------

def _load_model() -> bool:
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
        print(f"[yellow]openWakeWord import failed: {exc}[/yellow]", flush=True)
        return False

    for name in _candidate_models():
        try:
            _oww_model = OWWModel(wakeword_models=[name], inference_framework="onnx")
            _active_backend_model = name
            _available = True
            preferred = _resolve_backend_model()
            if name != preferred:
                print(
                    f"[yellow]Wake model '{preferred}' unavailable — using '{name}'.[/yellow]\n"
                    f"[yellow]Say '{name.replace('_', ' ')}' to activate ATLAS.[/yellow]",
                    flush=True,
                )
            return True
        except Exception:
            continue

    try:
        _oww_model = OWWModel(inference_framework="onnx")
        _active_backend_model = "auto"
        _available = True
        return True
    except Exception as exc:
        _oww_model = None
        _available = False
        print(f"[yellow]openWakeWord failed to load any model: {exc}[/yellow]", flush=True)
        return False


def is_available() -> bool:
    """Return True when the OWW backend can be loaded."""
    return _load_model()


# ---------------------------------------------------------------------------
# Producer: single mic owner
# ---------------------------------------------------------------------------

def _producer_loop() -> None:
    """
    Owns the microphone exclusively.

    Reads CHUNK-sized int16 frames from sounddevice and puts copies into
    _audio_queue.  Never opened concurrently with any other InputStream.
    If the queue is full the oldest frame is discarded (real-time priority).
    """
    device = settings.get("voice_input_device")
    device_index = int(device) if device is not None else None

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
                threshold = float(settings.get("wake_word_threshold") or 0.35)
                print(
                    f"[green]Mic open — say '{_wake_phrase()}' "
                    f"(threshold: {threshold:.2f})[/green]",
                    flush=True,
                )
                while not _stop_event.is_set():
                    frame, _ = stream.read(CHUNK)
                    arr: np.ndarray = frame.reshape(-1).astype(np.int16, copy=False).copy()
                    try:
                        _audio_queue.put_nowait(arr)
                    except queue.Full:
                        try:
                            _audio_queue.get_nowait()   # drop oldest frame
                        except queue.Empty:
                            pass
                        _audio_queue.put_nowait(arr)

        except Exception as exc:
            if device_index is not None:
                print(
                    f"[yellow]Mic device {device_index} failed — retrying with default: {exc}[/yellow]",
                    flush=True,
                )
                device_index = None
                continue
            logging.warning("Producer loop error: %s", exc)
            time.sleep(1.0)


# ---------------------------------------------------------------------------
# Consumer: state machine
# ---------------------------------------------------------------------------

def _get_frame() -> np.ndarray | None:
    try:
        return _audio_queue.get(timeout=0.1)
    except queue.Empty:
        return None


def _dispatch_command(audio_i16: np.ndarray) -> None:
    """Transcribe and execute one command.  Runs in a disposable thread."""
    text = voice.transcribe_from_array(audio_i16)
    normalized = text.strip().lower()

    if not normalized:
        print("[dim]No speech detected.[/dim]", flush=True)
        return

    print(f"[dim]Heard: {normalized}[/dim]", flush=True)

    ks_word = str(settings.get("killswitch_word") or "stop").lower()
    if normalized == ks_word:
        try:
            import killswitch as _ks
            _ks.fire()
        except Exception:
            pass
        return

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


def _fire_dispatch(chunks: list[np.ndarray]) -> None:
    """Concatenate captured chunks and dispatch in a background thread."""
    if not chunks:
        return
    audio = np.concatenate(chunks)
    threading.Thread(target=_dispatch_command, args=(audio,), daemon=True).start()


def _consumer_loop() -> None:
    """
    Single consumer.  Routes audio frames via a strict state machine.

    State transitions
    -----------------
    DETECTING     → OWW.predict() on every frame
                  → score > threshold AND cooldown expired
                  → CAPTURING  (sets _cooldown_until, drains WAKE_DRAIN_CHUNKS)

    CAPTURING     → skip WAKE_DRAIN_CHUNKS frames (contain wake-word audio,
                    feeding them to Whisper causes phonetic hallucinations)
                  → collect frames until silence or hard-cap
                  → _fire_dispatch() → back to DETECTING

    PTT_RECORDING → entered whenever ptt_active is set (any previous state)
                  → collect frames until ptt_active clears
                  → _fire_dispatch() → back to DETECTING

    PTT preempts everything: if ptt_active is set mid-capture the in-progress
    wake-word capture is discarded and PTT collection starts fresh.
    """
    global _cooldown_until

    state = _State.DETECTING
    threshold = float(settings.get("wake_word_threshold") or 0.35)
    session_peak: float = 0.0

    cap: list[np.ndarray] = []
    speech_started = False
    silent_count = 0
    capture_deadline: float = 0.0
    drain_remaining = 0

    while not _stop_event.is_set():
        frame = _get_frame()
        if frame is None:
            # Queue timeout — check if PTT was released while idle
            if state == _State.PTT_RECORDING and not ptt_active.is_set():
                _fire_dispatch(cap)
                cap, speech_started, silent_count = [], False, 0
                state = _State.DETECTING
            continue

        # ------------------------------------------------------------------
        # PTT PRIORITY: preempts OWW in any state
        # ------------------------------------------------------------------
        if ptt_active.is_set():
            if state != _State.PTT_RECORDING:
                # Discard any in-progress wake-word capture cleanly
                cap, speech_started, silent_count = [], False, 0
                drain_remaining = 0
                state = _State.PTT_RECORDING
            cap.append(frame.copy())
            continue

        # PTT key was released → finalise
        if state == _State.PTT_RECORDING:
            _fire_dispatch(cap)
            cap, speech_started, silent_count = [], False, 0
            state = _State.DETECTING
            continue

        # ------------------------------------------------------------------
        # DETECTING — run OWW on every frame
        # ------------------------------------------------------------------
        if state == _State.DETECTING:
            if _oww_model is None:
                continue

            audio_f = frame.astype(np.float32) / 32_768.0
            try:
                pred = _oww_model.predict(audio_f)
            except Exception as exc:
                logging.warning("OWW predict error: %s", exc)
                continue

            if not isinstance(pred, dict):
                continue

            for model_name, score in pred.items():
                score_f = float(score)
                if score_f > 0.05:
                    logging.debug(
                        "OWW %s score=%.3f threshold=%.2f",
                        model_name, score_f, threshold,
                    )
                if score_f > session_peak:
                    session_peak = score_f
                    if score_f > 0.10:
                        print(
                            f"[dim]Wake peak: {score_f:.3f} "
                            f"(need >{threshold:.2f} to trigger)[/dim]",
                            flush=True,
                        )

            now = time.time()
            triggered = any(float(s) > threshold for s in pred.values())
            if triggered and now >= _cooldown_until:
                # Set cooldown BEFORE transitioning — prevents re-entry
                _cooldown_until = now + COOLDOWN_SECONDS

                print("\n[blue]ATLAS: Listening...[/blue]", flush=True)
                _broadcast_event({"type": "listening_start"})

                state = _State.CAPTURING
                cap, speech_started, silent_count = [], False, 0
                drain_remaining = WAKE_DRAIN_CHUNKS
                capture_deadline = time.time() + MAX_CAPTURE_SECONDS
            continue

        # ------------------------------------------------------------------
        # CAPTURING — skip drain frames then collect command audio
        # ------------------------------------------------------------------
        if state == _State.CAPTURING:
            # Discard frames that contain the wake word itself.
            # Feeding these to Whisper causes it to transcribe the wake
            # phrase phonetically (e.g. "hey jarvis" → "game jarvis").
            if drain_remaining > 0:
                drain_remaining -= 1
                continue

            energy = int(np.max(np.abs(frame))) if frame.size else 0

            if not speech_started:
                if energy > SPEECH_ENERGY:
                    speech_started = True
                    cap.append(frame.copy())
                elif time.time() > capture_deadline:
                    print("[dim]No speech after wake — returning to detection.[/dim]", flush=True)
                    cap, speech_started, silent_count = [], False, 0
                    state = _State.DETECTING
                continue

            # Speech in progress
            cap.append(frame.copy())
            if energy < SPEECH_ENERGY:
                silent_count += 1
                if silent_count >= MAX_SILENT_CHUNKS:
                    _fire_dispatch(cap)
                    cap, speech_started, silent_count = [], False, 0
                    state = _State.DETECTING
            else:
                silent_count = 0

            # Hard-cap reached
            if time.time() > capture_deadline:
                _fire_dispatch(cap)
                cap, speech_started, silent_count = [], False, 0
                state = _State.DETECTING


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

def _watchdog_loop() -> None:
    """Restart dead producer or consumer every 15 seconds."""
    while not _stop_event.is_set():
        _stop_event.wait(timeout=15)
        if _stop_event.is_set():
            break
        global _producer_thread, _consumer_thread
        if _producer_thread is not None and not _producer_thread.is_alive():
            print("[yellow]Producer died — restarting[/yellow]", flush=True)
            _producer_thread = threading.Thread(
                target=_producer_loop, daemon=True, name="atlas-mic-producer"
            )
            _producer_thread.start()
        if _consumer_thread is not None and not _consumer_thread.is_alive():
            print("[yellow]Consumer died — restarting[/yellow]", flush=True)
            _consumer_thread = threading.Thread(
                target=_consumer_loop, daemon=True, name="atlas-wake-consumer"
            )
            _consumer_thread.start()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_wake_word_listener() -> bool:
    """Start producer, consumer, and watchdog threads."""
    global _producer_thread, _consumer_thread, _watchdog_thread

    if not _load_model():
        print("[yellow]Wake word backend unavailable — wake word disabled[/yellow]", flush=True)
        return False

    _stop_event.clear()

    _producer_thread = threading.Thread(
        target=_producer_loop, daemon=True, name="atlas-mic-producer"
    )
    _producer_thread.start()

    _consumer_thread = threading.Thread(
        target=_consumer_loop, daemon=True, name="atlas-wake-consumer"
    )
    _consumer_thread.start()

    if _watchdog_thread is None or not _watchdog_thread.is_alive():
        _watchdog_thread = threading.Thread(
            target=_watchdog_loop, daemon=True, name="atlas-watchdog"
        )
        _watchdog_thread.start()

    return True


def stop_wake_word_listener() -> None:
    """Signal all threads to stop."""
    _stop_event.set()


def is_listening() -> bool:
    """Return True when the producer is alive and not stopped."""
    return (
        _producer_thread is not None
        and _producer_thread.is_alive()
        and not _stop_event.is_set()
    )
