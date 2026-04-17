---
agent: agent
description: ATLAS Fix — Unified Audio Loop, PTT Polling, Wake Word Diagnostics
---

Read #file:../copilot-instructions.md before starting.
Do NOT modify: executor.py, validator.py, security.py, verifier.py,
rollback.py, pc_control.py, classifier.py, api/server.py, history.py,
context_pruner.py, memory.py, settings.py, macros.py.
Run 'atlas --status' after every file change.

---

DIAGNOSIS:

1. wake_word_enabled: true means wake word takes priority. If the listener thread
   starts but silently scores 0 on everything (dead mic, wrong model), PTT is
   never started and the user has zero input with zero feedback.
2. keyboard.on_press_key("left ctrl", ...) is unreliable for modifier keys without
   admin rights. keyboard.is_pressed() polling works reliably for all keys.
3. PTT and wake word each try to open separate sounddevice streams. On Windows WDM,
   two simultaneous capture streams fail silently.
4. No per-frame diagnostic output means it is impossible to distinguish a dead mic
   from a misconfigured model from a threshold that is too high.

SOLUTION:
Merge PTT detection into \_listen_loop() using keyboard.is_pressed() polling.
One stream handles both modes. Add per-frame diagnostics.
Update voice.py and main.py to route through the unified loop.

---

FIX 1 — wake_word.py: Complete rewrite of \_listen_loop()

Add this import at the top of wake_word.py if not already present:
import keyboard

Replace \_listen_loop() with exactly this implementation.
Keep every other function (start_wake_word_listener, stop_wake_word_listener,
is_listening, \_load_openwakeword_model, \_broadcast_event, \_wake_phrase,
\_resolve_wakeword_model_name, \_candidate_backend_models, \_start_thread,
\_watchdog) UNCHANGED.

def \_listen_loop() -> None:
"""Unified audio loop: handles both wake word detection and PTT in one stream."""
threshold = float(settings.get("wake_word_threshold") or 0.35)
silence_ms = int(settings.get("vad_silence_ms") or 1500)
max_silent = max(1, int((silence_ms / 1000.0) \* SAMPLE_RATE / CHUNK))
ptt_key = str(settings.get("voice_key") or "f8")
ptt_enabled = bool(settings.get("voice_input"))

    device = settings.get("voice_input_device")
    device_index = int(device) if device is not None else None

    # --- Diagnostic counters ---
    _frame_count = 0
    _last_peak_score: float = 0.0
    _last_energy: int = 0
    _ptt_was_held = False
    _ptt_frames: list = []

    while not _stop_event.is_set():
        kwargs: dict = {
            "samplerate": SAMPLE_RATE,
            "channels": 1,
            "dtype": "int16",
            "blocksize": CHUNK,
        }
        if device_index is not None:
            kwargs["device"] = device_index

        try:
            # Print available devices once on first loop so user can see which is used
            if _frame_count == 0:
                import sounddevice as _sd
                try:
                    info = _sd.query_devices(kind="input")
                    print(
                        f"[dim][Audio] Using input device: {info.get('name', 'unknown')}[/dim]",
                        flush=True,
                    )
                except Exception:
                    pass

            with sd.InputStream(**kwargs) as stream:
                print(
                    f"[green]Audio loop active — wake word: '{_wake_phrase()}' "
                    f"(threshold {threshold:.2f})"
                    + (f" | PTT key: '{ptt_key}'" if ptt_enabled else "")
                    + "[/green]",
                    flush=True,
                )

                while not _stop_event.is_set():
                    try:
                        frame, _ = stream.read(CHUNK)
                        frame_i16 = frame.reshape(-1).astype(np.int16, copy=False)
                        _frame_count += 1

                        # --- DIAGNOSTIC: print energy + OWW scores every ~4 seconds ---
                        energy = int(np.max(np.abs(frame_i16))) if frame_i16.size else 0
                        _last_energy = energy
                        if _frame_count % 50 == 0:
                            score_part = ""
                            if _oww_model is not None:
                                try:
                                    _test_pred = _oww_model.predict(
                                        frame_i16.astype(np.float32) / 32768.0
                                    )
                                    if isinstance(_test_pred, dict) and _test_pred:
                                        best = max(float(v) for v in _test_pred.values())
                                        _last_peak_score = best
                                        score_part = f", oww_peak={best:.3f}"
                                except Exception:
                                    pass
                            print(
                                f"[dim][Audio] energy={energy}"
                                + score_part
                                + f", frames={_frame_count}[/dim]",
                                flush=True,
                            )
                            if energy == 0 and _frame_count > 100:
                                print(
                                    "[yellow][Audio] WARNING: mic energy is 0 — "
                                    "check that the correct input device is selected.[/yellow]",
                                    flush=True,
                                )

                        # ==================== PTT MODE ====================
                        if ptt_enabled:
                            ptt_held = False
                            try:
                                ptt_held = keyboard.is_pressed(ptt_key)
                            except Exception:
                                pass

                            if ptt_held and not _ptt_was_held:
                                # Key just pressed
                                _ptt_was_held = True
                                _ptt_frames.clear()
                                print("[blue]PTT: Recording...[/blue]", flush=True)

                            if ptt_held and _ptt_was_held:
                                # Key held — capture audio
                                _ptt_frames.append(frame_i16.copy())
                                continue  # skip OWW while PTT is active

                            if not ptt_held and _ptt_was_held:
                                # Key just released — transcribe
                                _ptt_was_held = False
                                if _ptt_frames:
                                    audio_i16 = np.concatenate(_ptt_frames)
                                    _ptt_frames.clear()
                                    if len(audio_i16) >= int(SAMPLE_RATE * 0.3):
                                        text = voice.transcribe_from_array(audio_i16)
                                        normalized = text.strip()
                                        if normalized:
                                            print(
                                                f"[dim]PTT heard: {normalized}[/dim]",
                                                flush=True,
                                            )
                                            _handle_command(normalized)
                                        else:
                                            print(
                                                "[dim]PTT: no speech detected.[/dim]",
                                                flush=True,
                                            )
                                    else:
                                        print(
                                            "[dim]PTT: too short, ignored.[/dim]",
                                            flush=True,
                                        )
                                continue

                        # ==================== WAKE WORD MODE ====================
                        if _oww_model is None:
                            continue

                        audio_f = frame_i16.astype(np.float32) / 32768.0
                        try:
                            pred = _oww_model.predict(audio_f)
                        except Exception as exc:
                            logging.warning("OWW predict error: %s", exc)
                            continue

                        if not isinstance(pred, dict):
                            continue

                        # Log high scores (debug aid)
                        for model_name, score in pred.items():
                            score_f = float(score)
                            if score_f > 0.05:
                                logging.debug(
                                    "OWW %s score=%.3f threshold=%.2f",
                                    model_name, score_f, threshold,
                                )
                            if score_f > _last_peak_score:
                                _last_peak_score = score_f
                                if score_f > 0.1:
                                    print(
                                        f"[dim]Wake peak: {score_f:.3f} "
                                        f"(need >{threshold:.2f} to trigger)[/dim]",
                                        flush=True,
                                    )

                        if not any(float(s) > threshold for s in pred.values()):
                            continue

                        # Wake word detected — acquire lock and capture command
                        if not _WAKE_LOCK.acquire(blocking=False):
                            continue

                        _CAPTURING.set()
                        try:
                            print("\n[blue]ATLAS: Listening...[/blue]", flush=True)
                            _broadcast_event({"type": "listening_start"})

                            # Drain 200ms buffer (same stream)
                            for _ in range(int(0.2 * SAMPLE_RATE / CHUNK)):
                                if _stop_event.is_set():
                                    break
                                stream.read(CHUNK)

                            # Capture command on same stream
                            chunks: list = []
                            started = False
                            silent = 0
                            deadline = time.time() + 4.0

                            while not _stop_event.is_set():
                                cf, _ = stream.read(CHUNK)
                                ci = cf.reshape(-1).astype(np.int16, copy=False)
                                e = int(np.max(np.abs(ci))) if ci.size else 0
                                if not started:
                                    if e > 600:
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
                                    if e < 600:
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
                        logging.warning("Audio frame error: %s", exc)
                        continue

        except Exception as exc:
            message = str(exc)
            if device_index is not None:
                print(
                    f"[yellow]Audio device {device_index} failed: {message}. "
                    f"Retrying with system default...[/yellow]",
                    flush=True,
                )
                device_index = None
                continue
            logging.warning("Audio loop warning: %s", exc)
            print(f"[yellow]Audio loop error (retrying in 2s): {message}[/yellow]", flush=True)
            time.sleep(2.0)
            continue

Add this helper function to wake_word.py (before \_listen_loop):

def \_handle_command(normalized: str) -> None:
"""Dispatch a transcribed command through the ATLAS pipeline."""
ks = str(settings.get("killswitch_word") or "stop").lower()
if normalized == ks:
try:
import killswitch as \_ks
\_ks.fire()
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

---

FIX 2 — voice.py: Make start_ptt_listener() a no-op stub

PTT is now handled inside wake_word.\_listen_loop() via keyboard.is_pressed() polling.
voice.py PTT functions must remain as stubs so imports don't break.

REPLACE start_ptt_listener() with:

def start_ptt_listener() -> bool:
"""PTT is now handled inside wake_word.\_listen_loop(). This is a no-op stub."""
key = str(settings.get("voice_key") or "f8")
print(
f"[dim]PTT mode configured (key: '{key}'). "
f"Audio loop handles PTT alongside wake word.[/dim]",
flush=True,
)
return True

REPLACE stop_ptt_listener() with:

def stop_ptt_listener() -> None:
"""No-op stub — PTT is managed by wake_word.\_listen_loop()."""
pass

Keep every other function in voice.py UNCHANGED:
speak(), stop_speaking(), transcribe_from_array(), transcribe_audio(),
warmup_model(), \_dispatch(), \_load_whisper_model(), set_command_handler()

---

FIX 3 — main.py: Always start the unified audio loop when any voice mode is enabled

REPLACE the entire voice startup block in main.py (the block starting with
"\_wake_enabled = ..." down to the end of the elif/else chain) with this:

    # Unified audio loop: handles both wake word and PTT in one stream.
    # Start it if either wake word or PTT voice input is enabled.
    _wake_available = wake_word.is_available()
    _wake_wanted = bool(settings.get("wake_word_enabled"))
    _ptt_wanted = bool(settings.get("voice_input"))

    if _wake_wanted or _ptt_wanted:
        if _wake_wanted and not _wake_available:
            print(
                "[yellow]Wake word model unavailable. "
                "PTT only (if voice_input: true in config).[/yellow]",
                flush=True,
            )
        started = wake_word.start_wake_word_listener()
        if started:
            mode_parts = []
            if _wake_wanted and _wake_available:
                mode_parts.append(f"wake word ('{wake_word._wake_phrase()}')")
            if _ptt_wanted:
                mode_parts.append(
                    f"PTT (hold '{settings.get('voice_key') or 'f8'}')"
                )
            print(
                f"[green][voice] Active: {' + '.join(mode_parts)}[/green]",
                flush=True,
            )
        else:
            print(
                "[red][voice] Audio loop failed to start. "
                "Check microphone and run as administrator.[/red]",
                flush=True,
            )
    else:
        print("[dim]Voice input disabled — text mode only.[/dim]", flush=True)

---

FIX 4 — wake_word.py: Update start_wake_word_listener() to start even without OWW
when PTT-only mode is needed.

REPLACE start_wake_word_listener() with:

def start_wake_word_listener() -> bool:
"""Start the unified audio loop for wake word and/or PTT."""
global \_watchdog_thread

    wake_wanted = bool(settings.get("wake_word_enabled"))
    ptt_wanted = bool(settings.get("voice_input"))

    if wake_wanted:
        if not _load_openwakeword_model():
            print(
                "[yellow]Wake word model unavailable — "
                "starting in PTT-only mode.[/yellow]",
                flush=True,
            )
            if not ptt_wanted:
                return False
            # PTT-only: continue without OWW
    elif not ptt_wanted:
        return False

    _stop_event.clear()
    _start_thread()

    if _watchdog_thread is None or not _watchdog_thread.is_alive():
        _watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
        _watchdog_thread.start()

    return True

---

END TESTS — run in order and confirm each:

1. python main.py (or atlas)
   → Printed: "Using input device: [name of your mic]"
   → Printed: "Audio loop active — wake word: '...' | PTT key: 'left ctrl'"
   → Every 4 seconds printed: "[Audio] energy=XXXX, oww_peak=X.XXX, frames=XXX"

2. If energy=0 is printed: your mic is wrong device.
   Fix: Run python -c "import sounddevice; print(sounddevice.query_devices())"
   Find the index of your real microphone. Add to config.json:
   "voice_input_device": N (replace N with the correct integer index)
   Restart atlas.

3. If energy > 0 but oww_peak never exceeds 0.05:
   OWW model is wrong. Check what phrase \_wake_phrase() returns in the startup message.
   Say EXACTLY that phrase close to the mic.
   If scores stay at 0, run: python test_audio.py (existing file) to verify OWW detection.

4. If oww_peak prints values like 0.12-0.18 but never triggers:
   Your threshold (0.20) is too high. Lower it:
   "wake_word_threshold": 0.12
   Restart and test again.

5. PTT test: hold the configured voice_key (left ctrl), speak "what time is it", release.
   Expected: "[PTT: Recording...]" → "[PTT heard: what time is it]" → time returned.
   If "[PTT: Recording...]" never prints: the keyboard library cannot see the key.
   Try running the terminal as Administrator (right-click → Run as administrator).

6. atlas 'open notepad' → typed command still works (regression check)

7. atlas --status → clean output, no errors
