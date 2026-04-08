"""Voice input/output helpers for ATLAS push-to-talk and spoken responses."""

from __future__ import annotations

import subprocess
import time
from typing import Callable

import keyboard
import sounddevice as sd
import whisper

import settings

_SAMPLE_RATE = 16000
_CHANNELS = 1
_CURRENT_TTS_PROCESS: subprocess.Popen[str] | None = None
_RECORDING_ACTIVE = False
_RECORDING_BUFFER: list = []
_RECORDING_STREAM: sd.InputStream | None = None
_MODEL: whisper.Whisper | None = None
_PTT_HOOKED = False
_COMMAND_HANDLER: Callable[[str], None] | None = None


def _load_whisper_model() -> whisper.Whisper:
    """Load and cache Whisper tiny model lazily on first use."""
    global _MODEL
    if _MODEL is None:
        _MODEL = whisper.load_model("tiny")
    return _MODEL


def set_command_handler(handler: Callable[[str], None]) -> None:
    """Register callback invoked for transcribed voice commands."""
    global _COMMAND_HANDLER
    _COMMAND_HANDLER = handler


def stop_speaking() -> None:
    """Stop the active speech subprocess immediately when running."""
    global _CURRENT_TTS_PROCESS
    if _CURRENT_TTS_PROCESS is not None and _CURRENT_TTS_PROCESS.poll() is None:
        _CURRENT_TTS_PROCESS.kill()
    _CURRENT_TTS_PROCESS = None


def _on_escape(_: keyboard.KeyboardEvent) -> None:
    """Global escape hotkey callback to stop speech output quickly."""
    stop_speaking()


def _start_recording() -> None:
    """Start microphone recording for push-to-talk capture."""
    global _RECORDING_ACTIVE, _RECORDING_BUFFER, _RECORDING_STREAM
    if _RECORDING_ACTIVE:
        return
    _RECORDING_ACTIVE = True
    _RECORDING_BUFFER = []

    def _audio_callback(indata, _frames, _time_info, _status) -> None:
        if _RECORDING_ACTIVE:
            _RECORDING_BUFFER.append(indata.copy())

    _RECORDING_STREAM = sd.InputStream(
        samplerate=_SAMPLE_RATE,
        channels=_CHANNELS,
        callback=_audio_callback,
    )
    _RECORDING_STREAM.start()


def _stop_recording_and_transcribe() -> str:
    """Stop recording and transcribe buffered audio into text."""
    global _RECORDING_ACTIVE, _RECORDING_STREAM
    _RECORDING_ACTIVE = False

    if _RECORDING_STREAM is not None:
        try:
            _RECORDING_STREAM.stop()
            _RECORDING_STREAM.close()
        except Exception:
            pass
        _RECORDING_STREAM = None

    time.sleep(0.05)

    if not _RECORDING_BUFFER:
        return ""

    try:
        import numpy as np

        audio_np = np.concatenate(_RECORDING_BUFFER, axis=0).squeeze()
        model = _load_whisper_model()
        transcribed = model.transcribe(audio_np, fp16=False, language="en")
        return str(transcribed.get("text", "")).strip()
    except Exception:
        return ""


def transcribe_from_mic() -> str:
    """Record while configured key is held and return transcribed text."""
    if not bool(settings.get("voice_input")):
        return ""

    voice_key = str(settings.get("voice_key") or "right_ctrl")
    keyboard.wait(voice_key)
    _start_recording()
    keyboard.wait(voice_key, suppress=False, trigger_on_release=True)
    return _stop_recording_and_transcribe()


def _on_key_press(_: keyboard.KeyboardEvent) -> None:
    """Handle push-to-talk key press events."""
    _start_recording()


def _on_key_release(_: keyboard.KeyboardEvent) -> None:
    """Handle push-to-talk key release, then dispatch command callback."""
    text = _stop_recording_and_transcribe()
    if text:
        print(f"[voice] Heard: {text}")
        if _COMMAND_HANDLER is not None:
            _COMMAND_HANDLER(text)


def start_ptt_listener() -> None:
    """Start background push-to-talk and escape listeners for voice mode."""
    global _PTT_HOOKED
    if _PTT_HOOKED or not bool(settings.get("voice_input")):
        return

    voice_key = str(settings.get("voice_key") or "right_ctrl")
    keyboard.on_press_key(voice_key, _on_key_press)
    keyboard.on_release_key(voice_key, _on_key_release)
    keyboard.on_press_key("esc", _on_escape)
    _PTT_HOOKED = True


def speak(text: str) -> subprocess.Popen[str] | None:
    """Speak text using edge-tts subprocess and return process handle."""
    global _CURRENT_TTS_PROCESS

    if not bool(settings.get("voice_output")):
        return None

    speed = float(settings.get("voice_speed") or 1.0)
    rate_percent = int((speed - 1.0) * 100)
    rate = f"{rate_percent:+d}%"

    stop_speaking()

    command = [
        "python",
        "-m",
        "edge_tts",
        "--text",
        text,
        "--rate",
        rate,
    ]

    try:
        _CURRENT_TTS_PROCESS = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return _CURRENT_TTS_PROCESS
    except Exception:
        _CURRENT_TTS_PROCESS = None
        return None
