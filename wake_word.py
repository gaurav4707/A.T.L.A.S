"""wake_word.py — SPMC audio pipeline for ATLAS wake word and PTT."""

# Architecture: one producer thread owns the mic and puts raw int16 frames
# into _audio_queue. One consumer thread reads from that queue and runs a
# state machine: DETECTING → CAPTURING → back to DETECTING. PTT pauses wake
# and redirects captured frames through the same Whisper/dispatch path.

from __future__ import annotations

import logging
import queue
import threading
import time
from enum import Enum, auto
from typing import Any

import numpy as np
import sounddevice as sd
from openwakeword.model import Model

import classifier
import executor
import llm_engine
import memory
import settings
import voice

SAMPLE_RATE = 16_000
CHUNK = 1_280                # 80 ms (OpenWakeWord expects 80ms chunks for optimal performance)

COOLDOWN = 2.0               # minimum seconds between triggers
SPEECH_ENERGY = 800          # int16 peak to consider "speech started / still speaking"
MAX_SILENT_CHUNKS = 19       # ~1.5 s of silence → end of utterance
MAX_CAPTURE_SECONDS = 7.0    # Hard cap on command capture window
WAKE_DRAIN_CHUNKS = 0        # OWW doesn't need to drain as much as Vosk


class _State(Enum):
    DETECTING = auto()       # Wake detector running; looking for wake phrase
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

_OWW_MODEL: Model | None = None
_last_trigger_time: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _broadcast_event(payload: dict[str, Any]) -> None:
    """Best-effort WebSocket broadcast for HUD state updates."""
    try:
        from api.ws_manager import ws_manager
        import asyncio
        # We check if a loop is already running to avoid issues in some environments
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(ws_manager.broadcast(payload))
            else:
                loop.run_until_complete(ws_manager.broadcast(payload))
        except RuntimeError:
            asyncio.run(ws_manager.broadcast(payload))
    except Exception:
        pass


def _get_wakeword_model() -> Model | None:
    """Initialize OpenWakeWord model from settings."""
    global _OWW_MODEL
    if _OWW_MODEL is not None:
        return _OWW_MODEL

    model_name = str(settings.get("wake_word_model") or "hey_jarvis")
    try:
        # OpenWakeWord will download models automatically on first run
        _OWW_MODEL = Model(wakeword_models=[model_name], inference_framework="onnx")
        print(f"[green]OpenWakeWord loaded — listening for '{model_name}'[/green]", flush=True)
        return _OWW_MODEL
    except Exception as exc:
        print(f"[red]OpenWakeWord failed to load: {exc}[/red]", flush=True)
        return None


def is_available() -> bool:
    return _get_wakeword_model() is not None


# ---------------------------------------------------------------------------
# Producer: single mic owner
# ---------------------------------------------------------------------------

def _producer_loop() -> None:
    """
    Owns the microphone exclusively.

    Reads CHUNK-sized int16 frames from sounddevice and puts copies into
    _audio_queue. Never opened concurrently with any other InputStream.
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
                model_name = str(settings.get("wake_word_model") or "hey_jarvis")
                threshold = float(settings.get("wake_word_threshold") or 0.5)
                print(
                    f"[green]Mic open — say '{model_name}' "
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
    """Transcribe and execute one command."""
    text = voice.transcribe_from_array(audio_i16)
    normalized = text.strip().lower()

    if not normalized:
        print("[dim]No speech detected.[/dim]", flush=True)
        return

    print(f"[dim]Heard: {normalized}[/dim]", flush=True)
    
    import dispatcher
    dispatcher.execute_text_command(normalized, memory.get_context_for_llm(normalized))


def _fire_dispatch(chunks: list[np.ndarray]) -> None:
    """Concatenate captured chunks and dispatch in a background thread."""
    if not chunks:
        return
    audio = np.concatenate(chunks)
    threading.Thread(target=_dispatch_command, args=(audio,), daemon=True).start()


def _consumer_loop() -> None:
    """
    Single consumer. Routes audio frames via a strict state machine.
    """
    global _last_trigger_time

    model = _get_wakeword_model()
    if model is None:
        print("[red]Consumer loop aborted: Model not loaded.[/red]", flush=True)
        return

    state = _State.DETECTING
    cap: list[np.ndarray] = []
    speech_started = False
    silent_count = 0
    capture_deadline: float = 0.0
    drain_remaining = 0

    model_name = str(settings.get("wake_word_model") or "hey_jarvis")
    threshold = float(settings.get("wake_word_threshold") or 0.5)

    while not _stop_event.is_set():
        frame = _get_frame()
        if frame is None:
            if state == _State.PTT_RECORDING and not ptt_active.is_set():
                _fire_dispatch(cap)
                cap, speech_started, silent_count = [], False, 0
                state = _State.DETECTING
            continue

        # PTT PRIORITY
        if ptt_active.is_set():
            if state != _State.PTT_RECORDING:
                cap, speech_started, silent_count = [], False, 0
                drain_remaining = 0
                state = _State.PTT_RECORDING
            cap.append(frame.copy())
            continue

        if state == _State.PTT_RECORDING:
            _fire_dispatch(cap)
            cap, speech_started, silent_count = [], False, 0
            state = _State.DETECTING
            continue

        # DETECTING
        if state == _State.DETECTING:
            if _OWW_MODEL is None:
                continue

            # Normalize frame for OpenWakeWord (float32, [-1, 1])
            frame_normalized = frame.astype(np.float32) / 32768.0
            
            # Predict
            prediction = _OWW_MODEL.predict(frame_normalized)
            
            # OpenWakeWord return a dict of scores for each model
            score = prediction.get(model_name, 0)
            
            if score > threshold:
                now = time.time()
                if now - _last_trigger_time > COOLDOWN:
                    _last_trigger_time = now
                    print("\n[blue]ATLAS: Listening...[/blue]", flush=True)
                    _broadcast_event({"type": "listening_start"})
                    
                    state = _State.CAPTURING
                    cap, speech_started, silent_count = [], False, 0
                    drain_remaining = WAKE_DRAIN_CHUNKS
                    capture_deadline = time.time() + MAX_CAPTURE_SECONDS
            continue

        # CAPTURING
        if state == _State.CAPTURING:
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

            cap.append(frame.copy())
            if energy < SPEECH_ENERGY:
                silent_count += 1
                if silent_count >= MAX_SILENT_CHUNKS:
                    _fire_dispatch(cap)
                    cap, speech_started, silent_count = [], False, 0
                    state = _State.DETECTING
            else:
                silent_count = 0

            if time.time() > capture_deadline:
                _fire_dispatch(cap)
                cap, speech_started, silent_count = [], False, 0
                state = _State.DETECTING


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

def _watchdog_loop() -> None:
    """Restart dead producer or consumer."""
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

    if not settings.get("wake_word_enabled"):
        return False

    if not is_available():
        print("[yellow]OpenWakeWord backend unavailable — wake word disabled[/yellow]", flush=True)
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
