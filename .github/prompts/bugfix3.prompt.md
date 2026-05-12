---
agent: agent
description: ATLAS Bug Fix — Wake Word PaErrorCode -9983 Stream Conflict
---

Read #file:../.github/copilot-instructions.md before starting.
Do NOT modify: executor.py, validator.py, security.py, verifier.py,
rollback.py, pc_control.py, classifier.py, api/server.py, history.py,
context_pruner.py, memory.py, settings.py, macros.py.

---

PROBLEM:
wake_word.py \_listen_loop() opens a sounddevice.InputStream for detection.
When \_on_wake_word() fires, it tries to open a SECOND sounddevice.InputStream
for command capture. Windows PortAudio cannot handle two simultaneous input
streams on the same device — the detection stream dies with PaErrorCode -9983,
the watchdog restarts it, it dies again, infinitely.

SOLUTION:
Use a SINGLE shared stream for both detection and capture. When wake word is
detected, pause detection (stop calling OWW model.predict) and switch the
same stream into capture mode. After capture is done, resume detection.

---

REWRITE wake_word.py completely with these changes:

MODULE-LEVEL STATE — keep existing, add:
\_CAPTURING = threading.Event() # set while command capture is in progress

REMOVE \_on_wake_word() entirely as a separate function.
REMOVE \_record_until_silence() entirely.

REWRITE \_listen_loop() to handle both detection AND capture inline:

def \_listen_loop() -> None:
global \_IS_LISTENING
\_IS_LISTENING = True
chunk_size = 1280
sample_rate = 16000
silence_ms = int(settings.get("vad_silence_ms") or 1500)
max_silent_chunks = max(1, int((silence_ms / 1000.0) \* sample_rate / chunk_size))

    while not _STOP_EVENT.is_set():
        try:
            with sounddevice.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=chunk_size,
            ) as stream:
                while not _STOP_EVENT.is_set():
                    try:
                        frame, _ = stream.read(chunk_size)
                        if _OWW_MODEL is None:
                            continue

                        frame_i16 = frame.reshape(-1).astype(np.int16, copy=False)
                        audio_np = frame_i16.astype(np.float32) / 32768.0

                        # --- DETECTION MODE ---
                        prediction = _OWW_MODEL.predict(audio_np)
                        if not isinstance(prediction, dict):
                            continue

                        threshold = float(settings.get("wake_word_threshold") or 0.35)
                        if not any(float(score) > threshold for score in prediction.values()):
                            continue

                        # Wake word detected — switch to CAPTURE MODE on same stream
                        if not _WAKE_LOCK.acquire(blocking=False):
                            continue

                        try:
                            print("\n[blue]ATLAS: Listening...[/blue]", flush=True)
                            _broadcast_event({"type": "listening_start"})

                            # Drain 200ms of post-wakeword audio from stream buffer
                            drain_chunks = int(0.2 * sample_rate / chunk_size)
                            for _ in range(drain_chunks):
                                if _STOP_EVENT.is_set():
                                    break
                                stream.read(chunk_size)

                            # --- CAPTURE LOOP on same stream ---
                            audio_chunks: list[np.ndarray] = []
                            speech_started = False
                            silent_count = 0
                            deadline = time.time() + 4.0  # max 4s to start speaking

                            while not _STOP_EVENT.is_set():
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
                                continue

                            print(f"[dim]Heard: {normalized}[/dim]", flush=True)

                            killswitch_word = str(
                                settings.get("killswitch_word") or "stop"
                            ).strip().lower()
                            if normalized == killswitch_word:
                                try:
                                    import killswitch as ks
                                    ks.fire()
                                except Exception:
                                    pass
                                continue

                            context_text = memory.get_context_for_llm(normalized)
                            parsed = classifier.classify(normalized) or llm_engine.query(
                                normalized, context_text
                            )
                            action = str(parsed.get("action", ""))
                            params = parsed.get("params", {})
                            if not isinstance(params, dict):
                                params = {}
                            execution = executor.execute(action, params)
                            response = str(execution.get("message", "Done."))
                            memory.add_to_sliding("user", normalized)
                            memory.add_to_sliding("assistant", response)
                            voice.speak(response)

                        finally:
                            _WAKE_LOCK.release()

                    except Exception as exc:
                        logging.warning("Wake word frame error: %s", exc)
                        continue

        except Exception as exc:
            logging.warning("Wake word loop warning: %s", exc)
            time.sleep(0.5)
            continue

    _IS_LISTENING = False

Keep all other functions unchanged:
start_wake_word_listener() — unchanged
stop_wake_word_listener() — unchanged
is_listening() — unchanged
calibrate_wake_word() — unchanged
\_get_wakeword_model() — unchanged
\_get_wakeword_phrase() — unchanged
\_prepare_model_assets() — unchanged
\_broadcast_event() — unchanged
\_spawn_listener_thread() — unchanged
\_watchdog_loop() — unchanged

---

END TESTS:

1. atlas --status → NO "Stream is stopped" warnings appear
2. Say "hey atlas" → ATLAS prints "ATLAS: Listening..."
3. Say "open notepad" after wake word → Notepad opens
4. atlas 'open notepad' typed → still works (regression check)
5. atlas --status → clean status panel with no stream errors
