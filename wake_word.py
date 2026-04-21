"""wake_word.py — SPMC audio pipeline for ATLAS wake word and PTT."""

# Architecture: one producer thread owns the mic and puts raw int16 frames
# into _audio_queue.  One consumer thread reads from that queue and runs a
# state machine: DETECTING → CAPTURING → back to DETECTING.  PTT pauses wake
# and redirects captured frames through the same Whisper/dispatch path.
# This eliminates the PaErrorCode -9983 crash that occurred when a second
# InputStream was opened while the first was still alive.

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from enum import Enum, auto
from typing import Any

import numpy as np
import sounddevice as sd
from vosk import KaldiRecognizer, Model as VoskModel

import classifier
import executor
import llm_engine
import memory
import settings
import voice

SAMPLE_RATE = 16_000
CHUNK = 1_280                # 80 ms

COOLDOWN = 2.0               # minimum seconds between triggers
SPEECH_ENERGY = 800          # int16 peak to consider "speech started / still speaking"
MAX_SILENT_CHUNKS = 19       # ~1.5 s of silence → end of utterance
MAX_CAPTURE_SECONDS = 5.0    # Hard cap on command capture window
WAKE_DRAIN_CHUNKS = 4        # Frames to discard after trigger (~320 ms of wake audio)


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

_vosk_model: VoskModel | None = None
_vosk_loaded: bool = False
_last_trigger_time: float = 0.0


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


def _wake_phrase() -> str:
    return str(settings.get("wake_word_phrase") or "hey atlas")


# ---------------------------------------------------------------------------
# Vosk model loading
# ---------------------------------------------------------------------------

def _load_vosk_model() -> bool:
    global _vosk_model, _vosk_loaded

    if _vosk_loaded and _vosk_model is not None:
        return True
    if _vosk_loaded and _vosk_model is None:
        return False

    model_path = str(settings.get("vosk_model_path") or "vosk-model-small-en-us-0.15")

    try:
        import os
        if not os.path.isdir(model_path):
            print(
                f"[red]Vosk model not found at '{model_path}'.[/red]\n"
                f"[yellow]Download from https://alphacephei.com/vosk/models[/yellow]\n"
                f"[yellow]Extract and place the folder in your atlas/ directory.[/yellow]",
                flush=True,
            )
            _vosk_loaded = True
            _vosk_model = None
            return False

        import logging
        logging.getLogger("vosk").setLevel(logging.ERROR)
        _vosk_model = VoskModel(model_path)
        _vosk_loaded = True
        phrase = str(settings.get("wake_word_phrase") or "hey atlas")
        print(f"[green]Vosk loaded — listening for '{phrase}'[/green]", flush=True)
        return True

    except Exception as exc:
        print(f"[red]Vosk model failed to load: {exc}[/red]", flush=True)
        _vosk_model = None
        _vosk_loaded = True
        return False


def is_available() -> bool:
    return _load_vosk_model()


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
    DETECTING     → Vosk grammar spotting for wake phrase
                  → phrase hit AND cooldown expired
                  → CAPTURING  (drains WAKE_DRAIN_CHUNKS)

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
    global _last_trigger_time

    state = _State.DETECTING

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
        # PTT PRIORITY: preempts wake detection in any state
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
        # DETECTING — run Vosk grammar spotting
        # ------------------------------------------------------------------
        if state == _State.DETECTING:
            if _vosk_model is None:
                continue

            # Build a fresh recognizer each time we enter DETECTING from scratch.
            # KaldiRecognizer is stateful — resetting between captures prevents
            # old audio state bleeding into the next detection window.
            phrase = _wake_phrase().lower()
            grammar = json.dumps([phrase, "[unk]"])
            recognizer = KaldiRecognizer(_vosk_model, SAMPLE_RATE, grammar)
            recognizer.SetWords(False)

            pending_frame: np.ndarray | None = frame

            # Detection inner loop — exits when triggered or _stop_event fires
            while not _stop_event.is_set() and not ptt_active.is_set():
                detect_frame = pending_frame if pending_frame is not None else _get_frame()
                pending_frame = None
                if detect_frame is None:
                    continue

                # PTT preempt check (same as outer loop)
                if ptt_active.is_set():
                    break

                pcm_bytes = detect_frame.tobytes()
                accepted = recognizer.AcceptWaveform(pcm_bytes)

                if accepted:
                    result = json.loads(recognizer.Result())
                    text = result.get("text", "").lower().strip()
                else:
                    # Partial result check — catches phrase before utterance ends
                    partial = json.loads(recognizer.PartialResult())
                    text = partial.get("partial", "").lower().strip()

                if phrase in text:
                    now = time.time()
                    if now - _last_trigger_time < COOLDOWN:
                        # Debounce — same utterance still scoring, reset recognizer
                        recognizer = KaldiRecognizer(_vosk_model, SAMPLE_RATE, grammar)
                        recognizer.SetWords(False)
                        continue

                    _last_trigger_time = now

                    print("\n[blue]ATLAS: Listening...[/blue]", flush=True)
                    _broadcast_event({"type": "listening_start"})

                    # Transition to CAPTURING — reset recognizer so wake audio is not
                    # carried into the Whisper capture buffer via lingering state.
                    state = _State.CAPTURING
                    cap, speech_started, silent_count = [], False, 0
                    drain_remaining = WAKE_DRAIN_CHUNKS
                    capture_deadline = time.time() + MAX_CAPTURE_SECONDS
                    break   # exit detection inner loop, outer loop continues in CAPTURING

            # If ptt_active broke us out, outer loop handles PTT_RECORDING transition
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

    if not _load_vosk_model():
        print("[yellow]Vosk backend unavailable — wake word disabled[/yellow]", flush=True)
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
