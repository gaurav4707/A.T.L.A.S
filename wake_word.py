"""wake_word.py - Wake word detection for ATLAS v2 using openWakeWord."""

from __future__ import annotations

import atexit
import json
import logging
import keyboard
import queue
import threading
import time
from collections import deque
from pathlib import Path
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
_producer_thread: threading.Thread | None = None
_watchdog_thread: threading.Thread | None = None
_pre_roll: deque[np.ndarray] = deque(maxlen=PREROLL_CHUNKS)
_WAKE_LOCK = threading.Lock()
_CAPTURING = threading.Event()
_oww_model: Any | None = None
_available: bool | None = None
_active_backend_model: str = ""
_wake_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
_ptt_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=512)
_state_lock = threading.Lock()
_is_ptt_active = False
_score_history: deque[float] = deque(maxlen=400)
_last_tuning_report_frame = 0

TUNING_THRESHOLDS = (0.10, 0.12, 0.15, 0.18, 0.20, 0.25, 0.30, 0.35)


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


def _handle_command(normalized: str) -> None:
    """Dispatch a transcribed command through the ATLAS pipeline."""
    ks = str(settings.get("killswitch_word") or "stop").lower()
    if normalized == ks:
        try:
            import killswitch as _ks

            _ks.fire()
        except Exception:
            pass
        return

    try:
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
    except Exception as exc:
        logging.error("Command dispatch error: %s", exc)


def _set_ptt_active(value: bool) -> None:
    """Update PTT routing state in a thread-safe way."""
    global _is_ptt_active
    with _state_lock:
        _is_ptt_active = value


def _get_ptt_active() -> bool:
    """Read current PTT routing state."""
    with _state_lock:
        return _is_ptt_active


def _flush_queue(target: queue.Queue[np.ndarray]) -> None:
    """Drain all pending frames from a queue."""
    while True:
        try:
            target.get_nowait()
        except queue.Empty:
            break


def _put_frame(target: queue.Queue[np.ndarray], frame_i16: np.ndarray) -> None:
    """Put frame in queue with drop-oldest overflow policy."""
    try:
        target.put_nowait(frame_i16)
    except queue.Full:
        try:
            target.get_nowait()
        except queue.Empty:
            pass
        try:
            target.put_nowait(frame_i16)
        except queue.Full:
            logging.warning("Audio queue overflow; dropping frame")


def _telemetry_log_path() -> Path:
    """Return telemetry log path for wake score diagnostics."""
    configured = str(settings.get("wake_telemetry_log") or "wake_telemetry.jsonl").strip()
    return Path(configured)


def _append_telemetry(
    *,
    frame_index: int,
    best_model: str,
    best_score: float,
    threshold: float,
    triggered: bool,
) -> None:
    """Append one wake-score telemetry row as JSONL."""
    payload = {
        "ts": round(time.time(), 3),
        "frame": frame_index,
        "model": best_model,
        "score": round(best_score, 6),
        "threshold": round(threshold, 6),
        "triggered": triggered,
        "ptt_active": _get_ptt_active(),
    }
    try:
        with _telemetry_log_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception as exc:
        logging.debug("Telemetry write skipped: %s", exc)


def _print_threshold_guidance(frame_index: int, threshold: float) -> None:
    """Periodically print threshold-sweep guidance from recent wake scores."""
    global _last_tuning_report_frame

    if len(_score_history) < 40:
        return
    if frame_index - _last_tuning_report_frame < 200:
        return

    scores = np.array(_score_history, dtype=np.float32)
    p90 = float(np.percentile(scores, 90))
    p95 = float(np.percentile(scores, 95))
    p99 = float(np.percentile(scores, 99))

    suggested = max(0.08, min(0.45, p95 * 0.90))
    sweep_parts = []
    for value in TUNING_THRESHOLDS:
        above = int(np.sum(scores > value))
        ratio = above / float(len(scores))
        sweep_parts.append(f"{value:.2f}:{ratio:.2f}")

    print(
        "[dim][OWW Tune] "
        + f"p90={p90:.3f} p95={p95:.3f} p99={p99:.3f} "
        + f"now={threshold:.2f} suggested={suggested:.2f} "
        + "sweep="
        + ",".join(sweep_parts)
        + "[/dim]",
        flush=True,
    )
    _last_tuning_report_frame = frame_index


def _producer_loop() -> None:
    """Continuously capture audio and route frames to wake/PTT queues."""
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
            if device_index is not None:
                try:
                    info = sd.query_devices(device_index)
                    print(
                        f"[dim][Audio] Using input device: {info.get('name', 'unknown')}[/dim]",
                        flush=True,
                    )
                except Exception:
                    pass
            else:
                try:
                    info = sd.query_devices(kind="input")
                    print(
                        f"[dim][Audio] Using input device: {info.get('name', 'unknown')}[/dim]",
                        flush=True,
                    )
                except Exception:
                    pass

            with sd.InputStream(**kwargs) as stream:
                while not _stop_event.is_set():
                    frame, _ = stream.read(CHUNK)
                    frame_i16 = np.asarray(frame, dtype=np.int16).reshape(-1)
                    if frame_i16.size != CHUNK:
                        continue
                    frame_copy = frame_i16.copy()
                    if _get_ptt_active():
                        _put_frame(_ptt_queue, frame_copy)
                    else:
                        _put_frame(_wake_queue, frame_copy)
        except Exception as exc:
            message = str(exc)
            if _stop_event.is_set():
                break
            if device_index is not None:
                print(
                    f"[yellow]Audio device {device_index} failed: {message}. Retrying with system default...[/yellow]",
                    flush=True,
                )
                device_index = None
                continue

            logging.warning("Audio producer warning: %s", exc)
            print(f"[yellow]Audio loop error (retrying in 2s): {message}[/yellow]", flush=True)
            time.sleep(2.0)
            continue


def _listen_loop() -> None:
    """Consume wake/PTT queues with state-based routing."""
    threshold = float(settings.get("wake_word_threshold") or 0.35)
    silence_ms = int(settings.get("vad_silence_ms") or 1500)
    max_silent = max(1, int((silence_ms / 1000.0) * SAMPLE_RATE / CHUNK))
    ptt_key = str(settings.get("voice_key") or "f8")
    ptt_enabled = bool(settings.get("voice_input"))
    wake_enabled = bool(settings.get("wake_word_enabled"))

    _frame_count = 0
    _last_peak_score: float = 0.0
    _ptt_was_held = False
    _ptt_frames: list[np.ndarray] = []
    print(
        f"[green]Audio loop active — wake word: '{_wake_phrase()}' "
        f"(threshold {threshold:.2f})"
        + (f" | PTT key: '{ptt_key}'" if ptt_enabled else "")
        + "[/green]",
        flush=True,
    )

    while not _stop_event.is_set():
        try:
            ptt_held = False
            if ptt_enabled:
                try:
                    ptt_held = keyboard.is_pressed(ptt_key)
                except Exception:
                    ptt_held = False

            if ptt_held and not _ptt_was_held:
                _ptt_was_held = True
                _set_ptt_active(True)
                _flush_queue(_wake_queue)
                _pre_roll.clear()
                _ptt_frames.clear()
                print("[blue]PTT: Recording...[/blue]", flush=True)

            if not ptt_held and _ptt_was_held:
                _ptt_was_held = False
                _set_ptt_active(False)
                _flush_queue(_ptt_queue)
                if _ptt_frames:
                    audio_i16 = np.concatenate(_ptt_frames)
                    _ptt_frames.clear()
                    if len(audio_i16) >= int(SAMPLE_RATE * 0.3):
                        text = voice.transcribe_from_array(audio_i16)
                        normalized = text.strip()
                        if normalized:
                            print(f"[dim]PTT heard: {normalized}[/dim]", flush=True)
                            _handle_command(normalized)
                        else:
                            print("[dim]PTT: no speech detected.[/dim]", flush=True)
                    else:
                        print("[dim]PTT: too short, ignored.[/dim]", flush=True)
                continue

            if _ptt_was_held:
                try:
                    ptt_frame = _ptt_queue.get(timeout=0.25)
                except queue.Empty:
                    continue
                ptt_frame_i16 = np.asarray(ptt_frame, dtype=np.int16).reshape(-1)
                if ptt_frame_i16.size != CHUNK:
                    continue
                _ptt_frames.append(ptt_frame_i16.copy())
                continue

            if not wake_enabled or _oww_model is None:
                time.sleep(0.05)
                continue

            try:
                frame = _wake_queue.get(timeout=0.25)
            except queue.Empty:
                continue

            frame_i16 = np.asarray(frame, dtype=np.int16).reshape(-1)
            if frame_i16.size != CHUNK:
                continue

            _frame_count += 1
            energy = int(np.max(np.abs(frame_i16))) if frame_i16.size else 0
            if len(_pre_roll) == _pre_roll.maxlen:
                _pre_roll.popleft()
            _pre_roll.append(frame_i16.copy())

            if _frame_count % 50 == 0:
                score_part = ""
                try:
                    test_pred = _oww_model.predict(frame_i16.astype(np.float32) / 32768.0)
                    if isinstance(test_pred, dict) and test_pred:
                        best = max(float(v) for v in test_pred.values())
                        _last_peak_score = best
                        score_part = f", oww_peak={best:.3f}"
                except Exception:
                    pass
                print(
                    f"[dim][Audio] energy={energy}" + score_part + f", frames={_frame_count}[/dim]",
                    flush=True,
                )
                if energy == 0 and _frame_count > 100:
                    print(
                        "[yellow][Audio] WARNING: mic energy is 0 — check that the correct input device is selected.[/yellow]",
                        flush=True,
                    )

            audio_f = frame_i16.astype(np.float32) / 32768.0
            try:
                pred = _oww_model.predict(audio_f)
            except Exception as exc:
                logging.warning("OWW predict error: %s", exc)
                continue
            if not isinstance(pred, dict):
                continue

            best_model = ""
            best_score = 0.0

            for model_name, score in pred.items():
                score_f = float(score)
                if score_f > best_score:
                    best_score = score_f
                    best_model = model_name
                if score_f > 0.05:
                    logging.debug(
                        "OWW %s score=%.3f threshold=%.2f",
                        model_name, score_f, threshold,
                    )
                if score_f > _last_peak_score:
                    _last_peak_score = score_f
                    if score_f > 0.1:
                        print(
                            f"[dim]Wake peak: {score_f:.3f} (need >{threshold:.2f} to trigger)[/dim]",
                            flush=True,
                        )

            _score_history.append(best_score)
            _print_threshold_guidance(_frame_count, threshold)

            triggered = any(float(s) > threshold for s in pred.values())
            if best_score > 0.02 or triggered:
                _append_telemetry(
                    frame_index=_frame_count,
                    best_model=best_model,
                    best_score=best_score,
                    threshold=threshold,
                    triggered=triggered,
                )

            if not triggered:
                continue

            if not _WAKE_LOCK.acquire(blocking=False):
                continue

            _CAPTURING.set()
            try:
                print("\n[blue]ATLAS: Listening...[/blue]", flush=True)
                _broadcast_event({"type": "listening_start"})

                # Drain 200ms of post-wakeword audio.
                drain_chunks = int(0.2 * SAMPLE_RATE / CHUNK)
                for _ in range(drain_chunks):
                    if _stop_event.is_set():
                        break
                    try:
                        _wake_queue.get(timeout=0.25)
                    except queue.Empty:
                        break

                chunks: list[np.ndarray] = []
                chunks.extend(frame.copy() for frame in _pre_roll)
                started = False
                silent = 0
                deadline = time.time() + 4.0

                while not _stop_event.is_set():
                    try:
                        cf = _wake_queue.get(timeout=0.5)
                    except queue.Empty:
                        if time.time() > deadline and not started:
                            print(
                                "[dim]Wake: no speech after trigger — returning to detection.[/dim]",
                                flush=True,
                            )
                            break
                        continue

                    ci = np.asarray(cf, dtype=np.int16).reshape(-1)
                    if ci.size != CHUNK:
                        continue
                    chunk_energy = int(np.max(np.abs(ci))) if ci.size else 0

                    if not started:
                        if chunk_energy > 600:
                            started = True
                            chunks.append(ci.copy())
                        elif time.time() > deadline:
                            print(
                                "[dim]Wake: no speech after trigger — returning to detection.[/dim]",
                                flush=True,
                            )
                            break
                    else:
                        chunks.append(ci.copy())
                        if chunk_energy < 600:
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
                    print("[dim]Wake: no speech detected.[/dim]", flush=True)
                    continue

                print(f"[dim]Wake heard: {normalized}[/dim]", flush=True)
                _handle_command(normalized)

            finally:
                _CAPTURING.clear()
                _WAKE_LOCK.release()

        except Exception as exc:
            logging.warning("Audio consumer error: %s", exc)
            continue


def _start_thread() -> None:
    """Start producer and listener threads."""
    global _listener_thread, _producer_thread

    _producer_thread = threading.Thread(target=_producer_loop, daemon=True)
    _producer_thread.start()
    _listener_thread = threading.Thread(target=_listen_loop, daemon=True)
    _listener_thread.start()


def _watchdog() -> None:
    """Restart dead producer/listener threads while enabled."""
    while not _stop_event.is_set():
        _stop_event.wait(timeout=15)
        if _stop_event.is_set():
            break
        producer_dead = _producer_thread is None or not _producer_thread.is_alive()
        listener_dead = _listener_thread is None or not _listener_thread.is_alive()
        if producer_dead or listener_dead:
            print("[yellow]Wake word audio threads died — restarting[/yellow]")
            _start_thread()


def start_wake_word_listener() -> bool:
    """Start the unified audio loop for wake word and/or PTT."""
    global _watchdog_thread

    wake_wanted = bool(settings.get("wake_word_enabled"))
    ptt_wanted = bool(settings.get("voice_input"))

    if wake_wanted:
        if not _load_openwakeword_model():
            print(
                "[yellow]Wake word model unavailable — starting in PTT-only mode.[/yellow]",
                flush=True,
            )
            if not ptt_wanted:
                return False
    elif not ptt_wanted:
        return False

    _set_ptt_active(False)
    _flush_queue(_wake_queue)
    _flush_queue(_ptt_queue)
    _pre_roll.clear()

    _stop_event.clear()
    _start_thread()

    if _watchdog_thread is None or not _watchdog_thread.is_alive():
        _watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
        _watchdog_thread.start()

    return True


def stop_wake_word_listener() -> None:
    """Stop wake-word listener and watchdog."""
    current = threading.current_thread()
    _stop_event.set()
    for thread in (_producer_thread, _listener_thread, _watchdog_thread):
        if thread is None:
            continue
        if thread is current:
            continue
        if thread.is_alive():
            thread.join(timeout=1.0)


atexit.register(stop_wake_word_listener)


def is_listening() -> bool:
    """Return true when listener thread is alive and not stopped."""
    return _listener_thread is not None and _listener_thread.is_alive() and not _stop_event.is_set()
