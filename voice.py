"""voice.py - Voice I/O handler for ATLAS v2."""

from __future__ import annotations

import subprocess
import threading
import tempfile
import os
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
_ptt_stop_event = threading.Event()
_ptt_recording = threading.Event()
_ptt_frames: list = []
_ptt_stream: sd.InputStream | None = None
_ptt_press_listener: Any | None = None
_ptt_release_listener: Any | None = None


def transcribe_from_array(audio_np: np.ndarray) -> str:
    """Transcribe int16 audio array. Never opens a mic stream."""
    model = _load_whisper_model()
    if model is None:
        return ""

    try:
        audio_float = audio_np.flatten().astype(np.float32) / 32768.0
        result = model.transcribe(
            audio_float,
            language="en",
            prompt=(
                "ATLAS command assistant. Commands include: open chrome, open notepad, "
                "open VS Code, close app, search web, set volume, mute, what time is it, "
                "delete file, shutdown, restart, sleep, copy text."
            ),
            fp16=False,
            temperature=0.0,
            condition_on_previous_text=False,
            word_timestamps=False,
            no_speech_threshold=0.5,
            logprob_threshold=-1.0,
        )
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

        # "small" gives better accuracy than "base" for command recognition
        # and is fast enough on CPU for PTT use (2-4s transcription)
        _whisper_model = whisper.load_model("small")
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


def _on_ptt_press(_event: Any) -> None:
    if not _ptt_recording.is_set():
        _ptt_frames.clear()
        _ptt_recording.set()
        print("[blue]Recording...[/blue]", flush=True)


def _on_ptt_release(_event: Any) -> None:
    _ptt_recording.clear()


def _ptt_capture_loop() -> None:
    device = settings.get("voice_input_device")
    preferred_device_index = int(device) if device is not None else None

    while not _ptt_stop_event.is_set():
        kwargs: dict = {
            "samplerate": SAMPLE_RATE,
            "channels": 1,
            "dtype": "int16",
            "blocksize": CHUNK,
        }
        if preferred_device_index is not None:
            kwargs["device"] = preferred_device_index

        try:
            with sd.InputStream(**kwargs) as stream:
                was_recording = False
                while not _ptt_stop_event.is_set():
                    frame, _ = stream.read(CHUNK)
                    if _ptt_recording.is_set():
                        was_recording = True
                        _ptt_frames.append(frame.reshape(-1).astype(np.int16, copy=False).copy())
                    elif was_recording:
                        was_recording = False
                        if _ptt_frames:
                            audio = np.concatenate(_ptt_frames)
                            _ptt_frames.clear()
                            if len(audio) >= int(SAMPLE_RATE * 0.3):
                                text = transcribe_from_array(audio)
                                normalized = text.strip()
                                if normalized:
                                    print(f"[dim]Heard: {normalized}[/dim]", flush=True)
                                    _dispatch(normalized)
                                else:
                                    print("[dim]No speech detected.[/dim]", flush=True)
        except Exception as exc:
            message = str(exc)

            # First failure path: fallback from configured device to default device.
            if preferred_device_index is not None:
                print(
                    f"[yellow]PTT input device {preferred_device_index} failed: {message}[/yellow]",
                    flush=True,
                )
                print("[yellow]Retrying PTT with system default input device...[/yellow]", flush=True)
                preferred_device_index = None
                continue

            print(f"[red]PTT disabled: {message}[/red]", flush=True)
            print("[yellow]Tip: switch Windows input device or disable WDM-KS for this mic.[/yellow]", flush=True)
            return


def start_ptt_listener() -> bool:
    global _ptt_press_listener, _ptt_release_listener
    import ctypes

    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        is_admin = False
    if not is_admin:
        print("[yellow]PTT WARNING: Run terminal as administrator.[/yellow]", flush=True)

    hotkey = str(settings.get("voice_key") or "f8")
    _ptt_stop_event.clear()

    try:
        _ptt_press_listener = keyboard.on_press_key(hotkey, _on_ptt_press)
        _ptt_release_listener = keyboard.on_release_key(hotkey, _on_ptt_release)
    except Exception as exc:
        print(f"[red]PTT failed to register hotkey '{hotkey}': {exc}[/red]", flush=True)
        return False

    thread = threading.Thread(target=_ptt_capture_loop, daemon=True)
    thread.start()
    print(f"[green]Push-to-talk active - hold {hotkey} to speak[/green]", flush=True)
    return True


def stop_ptt_listener() -> None:
    global _ptt_press_listener, _ptt_release_listener
    _ptt_stop_event.set()
    try:
        if _ptt_press_listener is not None:
            keyboard.remove_hotkey(_ptt_press_listener)
        if _ptt_release_listener is not None:
            keyboard.remove_hotkey(_ptt_release_listener)
    except Exception:
        pass
    _ptt_press_listener = None
    _ptt_release_listener = None


def _tts_runner(cmd: list[str]) -> None:
    """Run edge-tts process in background and retain handle for stop_speaking."""
    global _tts_process

    try:
        _tts_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception as exc:
        print(f"[dim]TTS error: {exc}[/dim]")
        _tts_process = None


def speak(text: str) -> None:
    """Generate TTS to temp file and play with ffplay. Non-blocking."""
    if not settings.get("voice_output"):
        return
    clean = str(text or "").strip()
    if not clean:
        return

    stop_speaking()

    def _run() -> None:
        global _tts_process
        tmp_path = None
        try:
            import tempfile
            import os

            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp_path = tmp.name
            tmp.close()

            speed = float(settings.get("voice_speed") or 1.0)
            rate = f"+{int((speed - 1.0) * 100)}%"

            result = subprocess.run(
                ["edge-tts", f"--rate={rate}", "--text", clean, "--write-media", tmp_path],
                capture_output=True,
            )
            if result.returncode != 0:
                return

            _tts_process = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _tts_process.wait()
        except Exception as exc:
            print(f"[dim]TTS error: {exc}[/dim]")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    threading.Thread(target=_run, daemon=True).start()


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
