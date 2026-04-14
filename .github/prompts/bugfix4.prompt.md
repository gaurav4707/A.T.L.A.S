---
agent: agent
description: ATLAS Bug Fix — Wake Word Stream Conflict + PTT + Voice Output
---

Read #file:../copilot-instructions.md before starting.
Do NOT modify: executor.py, validator.py, security.py, verifier.py,
rollback.py, pc_control.py, classifier.py, api/server.py, history.py,
context_pruner.py, memory.py, settings.py, macros.py.
Run 'atlas --status' after every file change.

---

FIX 1 — wake_word.py: Full rewrite — single stream, inline capture

PROBLEM: \_on_wake_word() opens a second sounddevice stream while \_listen_loop()
already has one open. Windows PortAudio crashes with PaErrorCode -9983.

REWRITE wake_word.py completely. Keep all imports and module-level state.
Replace \_listen_loop() and remove \_on_wake_word() entirely.

The new \_listen_loop() must:

1. Open ONE sounddevice.InputStream for the entire function lifetime
2. In the same loop: run OWW detection AND capture on that same stream
3. Never open a second stream anywhere in this file

Here is the exact implementation:

def \_listen_loop() -> None:
chunk_size = CHUNK
sample_rate = SAMPLE_RATE
silence_ms = int(settings.get("vad_silence_ms") or 1500)
max_silent_chunks = max(1, int((silence_ms / 1000.0) \* sample_rate / chunk_size))
threshold = float(settings.get("wake_word_threshold") or 0.35)

    device = settings.get("voice_input_device")
    device_index = int(device) if device is not None else None

    while not _stop_event.is_set():
        try:
            stream_kwargs = dict(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=chunk_size,
            )
            if device_index is not None:
                stream_kwargs["device"] = device_index

            with sd.InputStream(**stream_kwargs) as stream:
                print(f"[green]Wake word active - say '{_wake_phrase()}'[/green]", flush=True)
                while not _stop_event.is_set():
                    try:
                        frame, _ = stream.read(chunk_size)
                        frame_i16 = frame.reshape(-1).astype(np.int16, copy=False)
                        audio_float = frame_i16.astype(np.float32) / 32768.0

                        if _oww_model is None:
                            continue

                        prediction = _oww_model.predict(audio_float)
                        if not isinstance(prediction, dict):
                            continue
                        if not any(float(s) > threshold for s in prediction.values()):
                            continue

                        # Wake word detected
                        if not _WAKE_LOCK.acquire(blocking=False):
                            continue

                        _CAPTURING.set()
                        try:
                            print("\n[blue]ATLAS: Listening...[/blue]", flush=True)
                            _broadcast_event({"type": "listening_start"})

                            # Drain 200ms buffer
                            for _ in range(int(0.2 * sample_rate / chunk_size)):
                                if _stop_event.is_set():
                                    break
                                stream.read(chunk_size)

                            # Capture command on SAME stream
                            audio_chunks: list[np.ndarray] = []
                            speech_started = False
                            silent_count = 0
                            deadline = time.time() + 4.0

                            while not _stop_event.is_set():
                                cap_frame, _ = stream.read(chunk_size)
                                cap_i16 = cap_frame.reshape(-1).astype(np.int16, copy=False)
                                energy = int(np.max(np.abs(cap_i16))) if cap_i16.size else 0

                                if not speech_started:
                                    if energy > 800:
                                        speech_started = True
                                        audio_chunks.append(cap_i16.copy())
                                    elif time.time() > deadline:
                                        break
                                else:
                                    audio_chunks.append(cap_i16.copy())
                                    if energy < 800:
                                        silent_count += 1
                                        if silent_count >= max_silent_chunks:
                                            break
                                    else:
                                        silent_count = 0

                            if not audio_chunks:
                                continue

                            audio_i16 = np.concatenate(audio_chunks)
                            text = voice.transcribe_from_array(audio_i16)
                            normalized = text.strip().lower()
                            if not normalized:
                                print("[dim]No speech detected.[/dim]", flush=True)
                                continue

                            print(f"[dim]Heard: {normalized}[/dim]", flush=True)

                            ks_word = str(settings.get("killswitch_word") or "stop").lower()
                            if normalized == ks_word:
                                try:
                                    import killswitch as ks
                                    ks.fire()
                                except Exception:
                                    pass
                                continue

                            ctx = memory.get_context_for_llm(normalized)
                            parsed = classifier.classify(normalized) or llm_engine.query(normalized, ctx)
                            action = str(parsed.get("action", ""))
                            params = parsed.get("params", {})
                            if not isinstance(params, dict):
                                params = {}
                            result = executor.execute(action, params)
                            response = str(result.get("message", "Done."))
                            memory.add_to_sliding("user", normalized)
                            memory.add_to_sliding("assistant", response)
                            voice.speak(response)

                        finally:
                            _CAPTURING.clear()
                            _WAKE_LOCK.release()

                    except Exception as exc:
                        logging.warning("Wake word frame error: %s", exc)
                        continue

        except Exception as exc:
            logging.warning("Wake word loop warning: %s", exc)
            time.sleep(1.0)
            continue

Keep start_wake_word_listener(), stop_wake_word_listener(), is_listening(),
is_available(), \_broadcast_event(), \_resolve_wakeword_model_name(),
\_wake_phrase() all unchanged.

---

FIX 2 — voice.py: Fix speak() — edge-tts must write to temp file then play

PROBLEM: speak() uses --write-media - which writes audio to stdout but
nothing plays it. Voice output is completely silent.

REPLACE speak() entirely with this implementation:

import tempfile
import os

def speak(text: str) -> None:
"""Synthesize text to a temp file and play it. Non-blocking."""
if not settings.get("voice_output"):
return
clean = str(text or "").strip()
if not clean:
return

    stop_speaking()

    def _run() -> None:
        global _tts_process
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp_path = tmp.name
            tmp.close()

            speed = float(settings.get("voice_speed") or 1.0)
            rate = f"+{int((speed - 1.0) * 100)}%"

            # Step 1: generate audio file
            gen = subprocess.run(
                ["edge-tts", f"--rate={rate}", "--text", clean,
                 "--write-media", tmp_path],
                capture_output=True,
            )
            if gen.returncode != 0:
                return

            # Step 2: play with ffplay (silent, no window)
            _tts_process = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _tts_process.wait()
        except Exception as exc:
            print(f"[dim]TTS error: {exc}[/dim]")
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()

---

FIX 3 — voice.py: Fix PTT — use keyboard hooks instead of keyboard.wait()

PROBLEM: keyboard.wait() blocks in a daemon thread and misses key events
on Windows. Replace with proper press/release hooks.

REPLACE \_ptt_loop() and \_record_ptt() with this implementation:

\_ptt_recording = threading.Event()
\_ptt_frames: list[np.ndarray] = []
\_ptt_stream: sd.InputStream | None = None

def \_on_ptt_press(\_event: Any) -> None:
"""Called when PTT key is pressed."""
if \_ptt_recording.is_set():
return
\_ptt_recording.set()
\_ptt_frames.clear()
print("[blue]Recording...[/blue]", flush=True)

def \_on_ptt_release(\_event: Any) -> None:
"""Called when PTT key is released."""
if not \_ptt_recording.is_set():
return
\_ptt_recording.clear()

def \_ptt_capture_loop() -> None:
"""Capture audio while \_ptt_recording is set, dispatch on release."""
device = settings.get("voice_input_device")
device_index = int(device) if device is not None else None

    stream_kwargs = dict(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=CHUNK,
    )
    if device_index is not None:
        stream_kwargs["device"] = device_index

    with sd.InputStream(**stream_kwargs) as stream:
        was_recording = False
        while not _ptt_stop_event.is_set():
            frame, _ = stream.read(CHUNK)
            if _ptt_recording.is_set():
                was_recording = True
                frame_i16 = frame.reshape(-1).astype(np.int16, copy=False)
                _ptt_frames.append(frame_i16.copy())
            elif was_recording:
                # Just released — transcribe
                was_recording = False
                if len(_ptt_frames) > 0:
                    audio = np.concatenate(_ptt_frames)
                    _ptt_frames.clear()
                    if len(audio) >= int(SAMPLE_RATE * 0.3):
                        text = transcribe_from_array(audio)
                        normalized = text.strip().lower()
                        if normalized:
                            print(f"[dim]Heard: {normalized}[/dim]", flush=True)
                            _dispatch(normalized)
                        else:
                            print("[dim]No speech detected.[/dim]", flush=True)

def start_ptt_listener() -> None:
"""Register PTT key hooks and start capture loop thread."""
import ctypes
try:
is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
except Exception:
is_admin = False
if not is_admin:
print(
"[yellow][voice] WARNING: PTT requires admin rights.[/yellow]\n"
"[dim]Run terminal as administrator.[/dim]",
flush=True,
)

    hotkey = str(settings.get("voice_key") or "f8")
    _ptt_stop_event.clear()

    keyboard.on_press_key(hotkey, _on_ptt_press)
    keyboard.on_release_key(hotkey, _on_ptt_release)

    thread = threading.Thread(target=_ptt_capture_loop, daemon=True)
    thread.start()
    print(f"[green]Push-to-talk active - hold {hotkey} to speak[/green]", flush=True)

def stop_ptt_listener() -> None:
"""Unregister PTT hooks and stop capture thread."""
\_ptt_stop_event.set()
try:
hotkey = str(settings.get("voice_key") or "f8")
keyboard.unhook_key(hotkey)
except Exception:
pass

---

FIX 4 — Install ffplay for voice output playback

After applying code fixes, run in terminal:
winget install ffmpeg

Then verify:
ffplay -version

If winget is not available, download ffmpeg from https://ffmpeg.org/download.html
and add the bin/ folder to your system PATH.

---

END TESTS:

1. atlas 'open notepad' → typed command works, green panel
2. atlas 'what time is it' → ATLAS speaks the time aloud (voice output working)
3. PTT (as admin): hold F8, say "open notepad", release → Notepad opens,
   "[dim]Heard: open notepad[/dim]" printed
4. Wake word: say "hey atlas" → "[blue]ATLAS: Listening...[/blue]" printed,
   say "what time is it" → time spoken back
5. atlas --status → NO stream warnings, clean output
