"""Voice input/output helpers for ATLAS push-to-talk and spoken responses."""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from threading import Event
from typing import Callable

import edge_tts
import keyboard
import sounddevice as sd
import whisper

try:
    import webrtcvad

    _HAS_WEBRTCVAD = True
except Exception:
    webrtcvad = None
    _HAS_WEBRTCVAD = False

import settings

MIN_RECORD_SECONDS = 0.18
SILENCE_RMS_THRESHOLD = 0.001
SILENCE_HARD_FLOOR = 0.00015
MAX_AUTO_GAIN = 80.0
RECORD_RELEASE_GRACE_SECONDS = 0.12
_SAMPLE_RATE = 16000
_CHANNELS = 1
_CURRENT_TTS_PROCESS: subprocess.Popen[str] | None = None
_RECORDING_ACTIVE = False
_RECORDING_BUFFER: list = []
_RECORDING_STREAM: sd.InputStream | None = None
_RECORDING_STARTED_AT: float = 0.0
_MODEL: whisper.Whisper | None = None
_PTT_HOOKED = False
_COMMAND_HANDLER: Callable[[str], None] | None = None
_LAST_CAPTURE_INFO = ""
_INPUT_DEVICE_INDEX: int | None = None
_INPUT_DEVICE_NAME = "unknown"
_STOP_TTS_EVENT = Event()
_VAD_WARNING_PRINTED = False


def _set_killswitch_tts_process(process: subprocess.Popen[Any] | None) -> None:
    """Share current TTS process with killswitch module when available."""
    try:
        import killswitch

        killswitch._current_tts_process = process
    except Exception:
        pass


def _resolve_ffplay_path() -> str | None:
    """Resolve ffplay executable path from PATH or common Windows install locations."""
    on_path = shutil.which("ffplay")
    if on_path:
        return on_path

    winget_root = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft",
        "WinGet",
        "Packages",
        "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe",
    )
    winget_matches = sorted(glob.glob(os.path.join(winget_root, "ffmpeg-*", "bin", "ffplay.exe")))
    if winget_matches:
        return winget_matches[-1]

    candidates = [
        "C:/ffmpeg/bin/ffplay.exe",
        "C:/Program Files/ffmpeg/bin/ffplay.exe",
        "C:/Program Files (x86)/ffmpeg/bin/ffplay.exe",
        "C:/ProgramData/chocolatey/bin/ffplay.exe",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def _load_whisper_model() -> whisper.Whisper:
    """Load and cache Whisper base model lazily on first use."""
    global _MODEL
    if _MODEL is None:
        _MODEL = whisper.load_model("base")
    return _MODEL


def _strip_silence_with_vad(audio_int16, sample_rate: int) -> tuple:
    """Keep only speech frames using WebRTC VAD (30ms frames, aggressiveness=2)."""
    import numpy as np

    global _VAD_WARNING_PRINTED

    if not _HAS_WEBRTCVAD:
        if not _VAD_WARNING_PRINTED:
            logging.warning("webrtcvad unavailable; proceeding without VAD silence stripping")
            _VAD_WARNING_PRINTED = True
        frame_size = int(sample_rate * 30 / 1000)
        total_frames = len(audio_int16) // max(1, frame_size)
        return audio_int16, total_frames, total_frames

    vad = webrtcvad.Vad(2)
    frame_ms = 30
    frame_size = int(sample_rate * frame_ms / 1000)
    if frame_size <= 0 or len(audio_int16) < frame_size:
        return np.array([], dtype=np.int16), 0, 0

    speech_frames: list[np.ndarray] = []
    total_frames = 0
    speech_count = 0

    for start in range(0, len(audio_int16) - frame_size + 1, frame_size):
        frame = audio_int16[start : start + frame_size]
        total_frames += 1
        if vad.is_speech(frame.tobytes(), sample_rate):
            speech_frames.append(frame)
            speech_count += 1

    if not speech_frames:
        return np.array([], dtype=np.int16), total_frames, speech_count

    return np.concatenate(speech_frames), total_frames, speech_count


def _is_transcription_noise(text: str) -> bool:
    """Return True when transcript is too short or non-word punctuation noise."""
    cleaned = text.strip()
    if len(cleaned) < 3:
        return True
    return re.fullmatch(r"[\W_]+", cleaned) is not None


def warmup_model() -> None:
    """Warm up Whisper once during startup to reduce first-command delay."""
    start = time.perf_counter()
    _load_whisper_model()
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    logging.info("Whisper warmup completed in %d ms", elapsed_ms)


def set_command_handler(handler: Callable[[str], None]) -> None:
    """Register callback invoked for transcribed voice commands."""
    global _COMMAND_HANDLER
    _COMMAND_HANDLER = handler


def stop_speaking() -> None:
    """Stop the active speech subprocess immediately when running."""
    global _CURRENT_TTS_PROCESS
    _STOP_TTS_EVENT.set()

    if _CURRENT_TTS_PROCESS is not None and _CURRENT_TTS_PROCESS.poll() is None:
        try:
            _CURRENT_TTS_PROCESS.kill()
        except Exception:
            pass
    _CURRENT_TTS_PROCESS = None
    _set_killswitch_tts_process(None)

    try:
        import pygame

        if pygame.mixer.get_init():
            pygame.mixer.music.stop()
            pygame.mixer.quit()
    except Exception:
        pass


def transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe raw 16kHz mono int16 PCM bytes produced by wake-word capture."""
    if not audio_bytes:
        return ""

    try:
        import numpy as np

        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        return transcribe_from_array(audio_int16)
    except Exception as exc:
        logging.error("Wake-word transcription failed: %s", exc)
        return ""


def transcribe_from_array(audio_int16) -> str:
    """Transcribe a 16kHz mono int16 numpy-like array."""
    try:
        import numpy as np

        if not isinstance(audio_int16, np.ndarray):
            audio_int16 = np.asarray(audio_int16, dtype=np.int16)

        if len(audio_int16) == 0:
            return ""

        speech_int16, _total_frames, _speech_frames = _strip_silence_with_vad(audio_int16, _SAMPLE_RATE)
        if len(speech_int16) == 0:
            return ""

        audio_np = speech_int16.astype(np.float32) / 32768.0
        peak = float(np.max(np.abs(audio_np))) if len(audio_np) else 0.0
        if 0.0 < peak < 0.60:
            gain = min(MAX_AUTO_GAIN, 0.60 / peak)
            audio_np = np.clip(audio_np * gain, -1.0, 1.0)

        model = _load_whisper_model()
        transcribed = model.transcribe(
            audio_np,
            language="en",
            prompt="Open app, search, volume, delete file, what time",
            fp16=False,
            temperature=0.0,
            condition_on_previous_text=False,
        )
        text = _normalize_transcribed_text(str(transcribed.get("text", "")))
        if _is_transcription_noise(text):
            return ""
        return text
    except Exception as exc:
        logging.error("Array transcription failed: %s", exc)
        return ""


def _on_escape(_: keyboard.KeyboardEvent) -> None:
    """Global escape hotkey callback to stop speech output quickly."""
    stop_speaking()


def _select_input_device() -> int | None:
    """Pick a usable microphone device, preferring explicit config then real mic names."""
    configured = settings.get("voice_input_device")

    devices = sd.query_devices()
    if configured is not None:
        try:
            configured_index = int(configured)
            if 0 <= configured_index < len(devices) and int(devices[configured_index].get("max_input_channels", 0)) > 0:
                return configured_index
        except Exception:
            configured_name = str(configured).strip().lower()
            if configured_name:
                for index, device in enumerate(devices):
                    if int(device.get("max_input_channels", 0)) <= 0:
                        continue
                    name = str(device.get("name", "")).lower()
                    if configured_name in name:
                        return index

    try:
        default_input = sd.default.device[0]
        if isinstance(default_input, int) and 0 <= default_input < len(devices):
            if int(devices[default_input].get("max_input_channels", 0)) > 0:
                return default_input
    except Exception:
        pass

    best_index: int | None = None
    for index, device in enumerate(devices):
        if int(device.get("max_input_channels", 0)) <= 0:
            continue
        name = str(device.get("name", "")).lower()
        if "microphone" in name and "sound mapper" not in name and "primary sound capture" not in name:
            return index
        if best_index is None:
            best_index = index
    return best_index


def _start_recording() -> None:
    """Start microphone recording for push-to-talk capture."""
    global _RECORDING_ACTIVE, _RECORDING_BUFFER, _RECORDING_STREAM, _RECORDING_STARTED_AT
    global _INPUT_DEVICE_INDEX, _INPUT_DEVICE_NAME
    if _RECORDING_ACTIVE:
        return
    _RECORDING_ACTIVE = True
    _RECORDING_BUFFER = []
    _RECORDING_STARTED_AT = time.time()

    if _INPUT_DEVICE_INDEX is None:
        _INPUT_DEVICE_INDEX = _select_input_device()
        try:
            if _INPUT_DEVICE_INDEX is not None:
                _INPUT_DEVICE_NAME = str(sd.query_devices(_INPUT_DEVICE_INDEX).get("name", "unknown"))
            else:
                _INPUT_DEVICE_NAME = "default"
        except Exception:
            _INPUT_DEVICE_NAME = "default"

    def _audio_callback(indata, _frames, _time_info, _status) -> None:
        if _RECORDING_ACTIVE:
            _RECORDING_BUFFER.append(indata.copy())

    try:
        _RECORDING_STREAM = sd.InputStream(
            device=_INPUT_DEVICE_INDEX,
            samplerate=_SAMPLE_RATE,
            channels=_CHANNELS,
            dtype="int16",
            callback=_audio_callback,
        )
        _RECORDING_STREAM.start()
        print(f"[voice] Recording... (mic: {_INPUT_DEVICE_NAME})", flush=True)
    except Exception as exc:
        _RECORDING_ACTIVE = False
        _RECORDING_STREAM = None
        logging.error("Microphone start failed: %s", exc)


def _stop_recording_and_transcribe() -> str:
    """Stop recording and transcribe buffered audio into text."""
    global _RECORDING_ACTIVE, _RECORDING_STREAM, _RECORDING_STARTED_AT, _LAST_CAPTURE_INFO
    _RECORDING_ACTIVE = False

    if _RECORDING_STREAM is not None:
        try:
            _RECORDING_STREAM.stop()
            _RECORDING_STREAM.close()
        except Exception:
            pass
        _RECORDING_STREAM = None

    time.sleep(RECORD_RELEASE_GRACE_SECONDS)

    if not _RECORDING_BUFFER:
        _LAST_CAPTURE_INFO = "empty-buffer"
        return ""

    try:
        import numpy as np

        audio_np = np.concatenate(_RECORDING_BUFFER, axis=0)
        if audio_np.ndim > 1:
            audio_np = audio_np.mean(axis=1)
        audio_int16 = audio_np.astype(np.int16)

        duration_sec = len(audio_int16) / _SAMPLE_RATE
        if duration_sec < MIN_RECORD_SECONDS:
            _LAST_CAPTURE_INFO = f"too-short {duration_sec:.2f}s"
            return ""

        speech_int16, total_frames, speech_frames = _strip_silence_with_vad(audio_int16, _SAMPLE_RATE)
        if len(speech_int16) == 0:
            _LAST_CAPTURE_INFO = f"vad-no-speech frames={speech_frames}/{total_frames}"
            return ""

        audio_np = speech_int16.astype(np.float32) / 32768.0

        rms = float(np.sqrt(np.mean(audio_np ** 2)))
        peak = float(np.max(np.abs(audio_np)))

        if 0.0 < peak < 0.60:
            gain = min(MAX_AUTO_GAIN, 0.60 / peak)
            audio_np = np.clip(audio_np * gain, -1.0, 1.0)

        tuned_rms = float(np.sqrt(np.mean(audio_np ** 2)))
        tuned_peak = float(np.max(np.abs(audio_np)))
        if tuned_rms < SILENCE_HARD_FLOOR:
            _LAST_CAPTURE_INFO = f"too-quiet rms={tuned_rms:.4f} peak={tuned_peak:.4f}"
            return ""

        model = _load_whisper_model()
        transcribed = model.transcribe(
            audio_np,
            language="en",
            prompt="Open app, search, volume, delete file, what time",
            fp16=False,
            temperature=0.0,
            condition_on_previous_text=False,
        )
        text = str(transcribed.get("text", "")).strip()
        if _is_transcription_noise(text):
            _LAST_CAPTURE_INFO = f"noise-text dur={duration_sec:.2f}s rms={tuned_rms:.4f}"
            _RECORDING_STARTED_AT = 0.0
            return ""
        if text:
            _LAST_CAPTURE_INFO = f"ok dur={duration_sec:.2f}s rms={tuned_rms:.4f}"
            _RECORDING_STARTED_AT = 0.0
            return text

        transcribed_retry = model.transcribe(
            audio_np,
            language="en",
            prompt="Open app, search, volume, delete file, what time",
            fp16=False,
            temperature=0.0,
            condition_on_previous_text=False,
        )
        retry_text = str(transcribed_retry.get("text", "")).strip()
        if _is_transcription_noise(retry_text):
            _LAST_CAPTURE_INFO = f"noise-text-retry dur={duration_sec:.2f}s rms={tuned_rms:.4f}"
            _RECORDING_STARTED_AT = 0.0
            return ""
        if retry_text:
            _LAST_CAPTURE_INFO = f"ok-retry dur={duration_sec:.2f}s rms={tuned_rms:.4f}"
            _RECORDING_STARTED_AT = 0.0
            return retry_text

        if tuned_rms < SILENCE_RMS_THRESHOLD:
            _LAST_CAPTURE_INFO = f"empty-text very-quiet dur={duration_sec:.2f}s rms={tuned_rms:.4f}"
        else:
            _LAST_CAPTURE_INFO = f"empty-text dur={duration_sec:.2f}s rms={tuned_rms:.4f}"
        _RECORDING_STARTED_AT = 0.0
        return ""
    except Exception as exc:
        logging.error("Voice transcription failed: %s", exc)
        _LAST_CAPTURE_INFO = "transcription-error"
        _RECORDING_STARTED_AT = 0.0
        return ""


def transcribe_from_mic() -> str:
    """Record while configured key is held and return transcribed text."""
    if not bool(settings.get("voice_input")):
        return ""

    voice_key = str(settings.get("voice_key") or "right_ctrl")
    keyboard.wait(voice_key)
    _start_recording()
    keyboard.wait(voice_key, suppress=False, trigger_on_release=True)
    text = _stop_recording_and_transcribe()
    text = _normalize_transcribed_text(text)
    if text:
        print(f"[voice] Heard: {text}", flush=True)
    return text


def _on_key_press(_: keyboard.KeyboardEvent) -> None:
    """Handle push-to-talk key press events."""
    _start_recording()


def _normalize_transcribed_text(text: str) -> str:
    """Normalize Whisper text for consistent command matching."""
    normalized = text.strip().lower()
    normalized = normalized.rstrip(".,!?")
    normalized = " ".join(normalized.split())
    return normalized


def _on_key_release(_: keyboard.KeyboardEvent) -> None:
    """Handle push-to-talk key release, then dispatch command callback."""
    text = _stop_recording_and_transcribe()
    text = _normalize_transcribed_text(text)
    if text:
        print(f"[voice] Heard: {text}", flush=True)
        if _COMMAND_HANDLER is not None:
            _COMMAND_HANDLER(text)
    else:
        print(f"[voice] No speech detected ({_LAST_CAPTURE_INFO}).", flush=True)


def start_ptt_listener() -> None:
    """Start background push-to-talk and escape listeners for voice mode."""
    global _PTT_HOOKED
    if _PTT_HOOKED or not bool(settings.get("voice_input")):
        return

    voice_key = str(settings.get("voice_key") or "right_ctrl")
    try:
        keyboard.on_press_key(voice_key, _on_key_press)
        keyboard.on_release_key(voice_key, _on_key_release)
        keyboard.on_press_key("esc", _on_escape)
        _PTT_HOOKED = True
        print(f"[voice] Push-to-talk - hold {voice_key}", flush=True)
    except Exception as exc:
        logging.error("PTT listener setup failed for key '%s': %s", voice_key, exc)


async def _speak_with_ffplay(text: str, voice_name: str, rate: str) -> bool:
    """Stream edge-tts audio directly to ffplay stdin when ffplay is available."""
    global _CURRENT_TTS_PROCESS

    ffplay_path = _resolve_ffplay_path()
    if not ffplay_path:
        return False

    communicate = edge_tts.Communicate(text=text, voice=voice_name, rate=rate)
    process: subprocess.Popen[bytes] | None = None

    try:
        process = subprocess.Popen(
            [ffplay_path, "-nodisp", "-autoexit", "-loglevel", "quiet", "-i", "pipe:0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _CURRENT_TTS_PROCESS = process
        _set_killswitch_tts_process(process)

        async for chunk in communicate.stream():
            if _STOP_TTS_EVENT.is_set() or process.poll() is not None:
                break

            if chunk.get("type") == "audio":
                data = chunk.get("data")
                if isinstance(data, bytes) and process.stdin is not None:
                    process.stdin.write(data)

        if process.stdin is not None:
            process.stdin.close()

        if not _STOP_TTS_EVENT.is_set() and process.poll() is None:
            process.wait()
        return True
    except Exception as exc:
        logging.error("ffplay streaming failed: %s", exc)
        return False
    finally:
        if process is not None and _STOP_TTS_EVENT.is_set() and process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass
        _CURRENT_TTS_PROCESS = None
        _set_killswitch_tts_process(None)


async def _speak_with_pygame(text: str, voice_name: str, rate: str) -> bool:
    """Fallback path: synthesize to temp mp3, then play via pygame mixer."""
    tmp_path = ""

    try:
        import pygame
    except Exception:
        return await _speak_with_windows_media_player(text=text, voice_name=voice_name, rate=rate)

    try:
        communicate = edge_tts.Communicate(text=text, voice=voice_name, rate=rate)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_file:
            tmp_path = temp_file.name

        await communicate.save(tmp_path)

        pygame.mixer.init()
        pygame.mixer.music.load(tmp_path)
        pygame.mixer.music.play()

        while pygame.mixer.music.get_busy():
            if _STOP_TTS_EVENT.is_set():
                pygame.mixer.music.stop()
                break
            await asyncio.sleep(0.05)

        pygame.mixer.quit()
        return True
    except Exception as exc:
        logging.error("pygame fallback failed: %s", exc)
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def _speak_with_windows_media_player(text: str, voice_name: str, rate: str) -> bool:
    """Final fallback: synthesize to temp mp3 and play with Windows MediaPlayer COM."""
    global _CURRENT_TTS_PROCESS

    tmp_path = ""
    process: subprocess.Popen[str] | None = None

    try:
        communicate = edge_tts.Communicate(text=text, voice=voice_name, rate=rate)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_file:
            tmp_path = temp_file.name

        await communicate.save(tmp_path)

        media_path = tmp_path.replace("'", "''")
        play_script = (
            "Add-Type -AssemblyName PresentationCore;"
            "$player = New-Object System.Windows.Media.MediaPlayer;"
            f"$player.Open([System.Uri]::new('{media_path}'));"
            "while (-not $player.NaturalDuration.HasTimeSpan) { Start-Sleep -Milliseconds 50 };"
            "$player.Play();"
            "$ms = [int]$player.NaturalDuration.TimeSpan.TotalMilliseconds + 250;"
            "Start-Sleep -Milliseconds $ms;"
            "$player.Close();"
            "exit 0"
        )

        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", play_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        _CURRENT_TTS_PROCESS = process
        _set_killswitch_tts_process(process)

        while process.poll() is None:
            if _STOP_TTS_EVENT.is_set():
                try:
                    process.kill()
                except Exception:
                    pass
                break
            await asyncio.sleep(0.05)

        return True
    except Exception as exc:
        logging.error("Windows media player fallback failed: %s", exc)
        return False
    finally:
        _CURRENT_TTS_PROCESS = None
        _set_killswitch_tts_process(None)
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def _speak_async(text: str, voice_name: str, rate: str) -> None:
    """Try ffplay streaming first, then fallback to pygame temp-file playback."""
    streamed = await _speak_with_ffplay(text=text, voice_name=voice_name, rate=rate)
    if streamed:
        return
    await _speak_with_pygame(text=text, voice_name=voice_name, rate=rate)


def speak(text: str) -> subprocess.Popen[str] | None:
    """Speak text using edge-tts with ffplay streaming and pygame fallback."""
    global _CURRENT_TTS_PROCESS

    if not bool(settings.get("voice_output")):
        return None

    if not text.strip():
        return None

    speed = float(settings.get("voice_speed") or 1.0)
    rate_percent = int((speed - 1.0) * 100)
    rate = f"{rate_percent:+d}%"
    voice_name = str(settings.get("voice_name") or "en-US-EmmaMultilingualNeural")

    _STOP_TTS_EVENT.clear()

    try:
        asyncio.run(_speak_async(text=text, voice_name=voice_name, rate=rate))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_speak_async(text=text, voice_name=voice_name, rate=rate))
        finally:
            loop.close()
    except Exception as exc:
        logging.error("edge-tts launch failed: %s", exc)
        _CURRENT_TTS_PROCESS = None
        return None

    return _CURRENT_TTS_PROCESS
