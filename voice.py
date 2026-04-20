"""voice.py - Voice I/O handler for ATLAS v2."""

from __future__ import annotations
import keyboard
import os
import subprocess
import tempfile
import threading
from typing import Any

import numpy as np

import settings

SAMPLE_RATE = 16000
CHUNK = 1280  # 80ms frames

_whisper_model: Any | None = None
_whisper_load_failed = False

_tts_process: subprocess.Popen[bytes] | None = None
_ptt_stop_event = threading.Event()
_ptt_recording = threading.Event()
_ptt_frames: list[bytes] = []
_ptt_press_listener: Any | None = None
_ptt_release_listener: Any | None = None


def transcribe_from_array(audio_np: np.ndarray) -> str:
    """Transcribe int16 audio array. Never opens a mic stream."""
    model = _load_whisper_model()
    if model is None:
        return ""

    try:
        audio_float = audio_np.flatten().astype(np.float32) / 32768.0

        # Guard: Whisper needs at least ~0.1s of audio; pad if too short
        min_samples = int(SAMPLE_RATE * 0.5)
        if len(audio_float) < min_samples:
            audio_float = np.pad(audio_float, (0, min_samples - len(audio_float)))

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
            # FIX: 0.6 is Whisper's own default — 0.5 was too aggressive
            no_speech_threshold=0.6,
            # FIX: -1.0 caused empty results on short/quiet commands;
            # -2.0 is far more permissive while still blocking garbage
            logprob_threshold=-2.0,
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


def start_ptt_listener() -> bool:
    """
    Register PTT key hooks.

    Starts the standalone _ptt_capture_loop ONLY when the wake word consumer
    is NOT running (i.e. wake word is disabled).  When wake word is active,
    the consumer already owns the mic and handles PTT via ptt_active.
    """
    import ctypes
    global _ptt_press_listener, _ptt_release_listener

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

    # Check whether the wake word consumer is already running
    wake_running = False
    try:
        import wake_word as _ww
        wake_running = _ww.is_listening()
    except Exception:
        pass

    if wake_running:
        # Consumer owns the mic — PTT is just a key signal
        print(
            f"[green]Push-to-talk active — hold {hotkey} to speak "
            f"(routed through wake word consumer)[/green]",
            flush=True,
        )
    else:
        # Wake word not running — start the standalone capture loop
        from voice import _ptt_capture_loop
        thread = threading.Thread(target=_ptt_capture_loop, daemon=True)
        thread.start()
        print(
            f"[green]Push-to-talk active — hold {hotkey} to speak "
            f"(standalone stream mode)[/green]",
            flush=True,
        )

    return True


def stop_ptt_listener() -> None:
    """No-op stub — PTT is managed by wake_word._listen_loop()."""
    pass


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
                err_msg = result.stderr.decode(errors="ignore")[:200].strip()
                print(f"[dim]edge-tts failed (code {result.returncode}): {err_msg}[/dim]", flush=True)
                return

            _tts_process = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _tts_process.wait()
        except FileNotFoundError as exc:
            if "ffplay" in str(exc):
                print(
                    "[red]Voice output requires ffplay. Install ffmpeg and add its bin/ folder to PATH.[/red]",
                    flush=True,
                )
            else:
                print(f"[red]TTS error: {exc}[/red]", flush=True)
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


def _ptt_capture_loop() -> None:
    """Fallback PTT capture loop placeholder when wake word is disabled."""
    while not _ptt_stop_event.is_set():
        _ptt_stop_event.wait(0.1)


def _on_ptt_press(_event: Any) -> None:
    """
    PTT key pressed.

    If the wake word consumer is running, delegate to it — no stream needed.
    Otherwise fall back to the existing standalone PTT path.
    """
    try:
        import wake_word as _ww
        if _ww.is_listening():
            _ww.ptt_active.set()
            return          # consumer handles capture and dispatch
    except Exception:
        pass

    # Standalone path (wake word not running)
    if not _ptt_recording.is_set():
        _ptt_frames.clear()
        _ptt_recording.set()
        print("[blue]PTT recording...[/blue]", flush=True)


def _on_ptt_release(_event: Any) -> None:
    """
    PTT key released.

    Mirrors _on_ptt_press: if wake word consumer is active, clear its flag.
    Consumer finishes capture and dispatches automatically.
    """
    try:
        import wake_word as _ww
        if _ww.is_listening():
            _ww.ptt_active.clear()
            return
    except Exception:
        pass

    # Standalone path
    _ptt_recording.clear()



