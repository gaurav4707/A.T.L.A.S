"""voice.py - Voice I/O handler for ATLAS v2."""

from __future__ import annotations

import subprocess
import threading
from typing import Any

import keyboard
import numpy as np
import sounddevice as sd

import settings

SAMPLE_RATE = 16000
CHUNK = 1280  # 80ms frames

_whisper_model: Any | None = None
_whisper_load_failed = False

_tts_process: subprocess.Popen[bytes] | None = None
_ptt_active = False
_ptt_stop_event = threading.Event()


def transcribe_from_array(audio_np: np.ndarray) -> str:
    """Transcribe int16 audio array. Never opens a mic stream."""
    model = _load_whisper_model()
    if model is None:
        return ""

    try:
        audio_float = audio_np.flatten().astype(np.float32) / 32768.0
        result = model.transcribe(audio_float, language="en", fp16=False)
        return str(result.get("text", "")).strip()
    except Exception as exc:
        print(f"[dim]Transcription error: {exc}[/dim]")
        return ""


def transcribe_audio(audio_bytes: bytes) -> str:
    """Compatibility wrapper: transcribe raw PCM bytes using shared array path."""
    if not audio_bytes:
        return ""

    try:
        audio_np = np.frombuffer(audio_bytes, dtype=np.int16)
        return transcribe_from_array(audio_np)
    except Exception as exc:
        print(f"[dim]Transcription error: {exc}[/dim]")
        return ""


def warmup_model() -> None:
    """Warm Whisper model in background so startup path stays responsive."""
    _load_whisper_model()


def _load_whisper_model() -> Any | None:
    """Load Whisper lazily on first transcription request."""
    global _whisper_model, _whisper_load_failed

    if _whisper_model is not None:
        return _whisper_model
    if _whisper_load_failed:
        return None

    try:
        import whisper

        _whisper_model = whisper.load_model("tiny")
        return _whisper_model
    except Exception as exc:
        _whisper_load_failed = True
        print(f"[yellow]Whisper failed to load: {exc}[/yellow]")
        return None


def _dispatch(text: str) -> None:
    """Send transcribed text through the ATLAS pipeline."""
    try:
        import classifier
        import llm_engine
        import memory
        import executor

        result = classifier.classify(text) or llm_engine.query(text, memory.get_context_for_llm(text))
        action = str(result.get("action", ""))
        params = result.get("params", {})
        if not isinstance(params, dict):
            params = {}
        execution = executor.execute(action, params)
        message = str(execution.get("message", ""))
        if message:
            speak(message)
    except Exception as exc:
        print(f"[red]Dispatch error: {exc}[/red]")


def _record_ptt(hotkey: str) -> np.ndarray | None:
    """Block until hotkey is held, record while held, return audio array."""
    global _ptt_active

    frames: list[np.ndarray] = []
    keyboard.wait(hotkey)
    _ptt_active = True
    print("[blue]Recording - release key to stop[/blue]")

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=CHUNK,
    ) as stream:
        while keyboard.is_pressed(hotkey) and not _ptt_stop_event.is_set():
            chunk, _ = stream.read(CHUNK)
            audio_np = chunk.reshape(-1).astype(np.int16, copy=False)
            frames.append(audio_np.copy())

    _ptt_active = False
    if not frames:
        return None
    return np.concatenate(frames)


def _ptt_loop() -> None:
    hotkey = str(settings.get("voice_key") or "right_ctrl")
    print(f"[green]Push-to-talk active - hold {hotkey} to speak[/green]")

    while not _ptt_stop_event.is_set():
        try:
            audio = _record_ptt(hotkey)
            if audio is None or len(audio) < int(SAMPLE_RATE * 0.3):
                continue
            text = transcribe_from_array(audio)
            if text:
                print(f"[dim]Heard: {text}[/dim]")
                _dispatch(text)
        except Exception as exc:
            print(f"[dim]PTT error (continuing): {exc}[/dim]")
            continue


def start_ptt_listener() -> None:
    """Start push-to-talk listener loop."""
    _ptt_stop_event.clear()
    thread = threading.Thread(target=_ptt_loop, daemon=True)
    thread.start()


def stop_ptt_listener() -> None:
    """Stop push-to-talk listener loop."""
    _ptt_stop_event.set()


def _tts_runner(cmd: list[str]) -> None:
    """Run edge-tts process in background and retain handle for stop_speaking."""
    global _tts_process

    try:
        _tts_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception as exc:
        print(f"[dim]TTS error: {exc}[/dim]")
        _tts_process = None


def speak(text: str) -> None:
    """Fire-and-forget TTS. Does not block the main thread."""
    if not settings.get("voice_output"):
        return

    clean = str(text or "").strip()
    if not clean:
        return

    stop_speaking()

    speed = float(settings.get("voice_speed") or 1.0)
    rate = f"+{int((speed - 1.0) * 100)}%"
    cmd = ["edge-tts", f"--rate={rate}", "--text", clean, "--write-media", "-"]
    thread = threading.Thread(target=_tts_runner, args=(cmd,), daemon=True)
    thread.start()


def stop_speaking() -> None:
    """Kill active TTS process immediately."""
    global _tts_process

    if _tts_process is not None and _tts_process.poll() is None:
        try:
            _tts_process.kill()
        except Exception:
            pass
    _tts_process = None


def set_command_handler(_handler: Any) -> None:
    """Compatibility no-op. Dispatch is internal to this module."""
    return
