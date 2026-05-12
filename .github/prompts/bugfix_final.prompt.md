---
agent: agent
description: ATLAS Final Fix — Voice Output + PTT + Wake Word Single Stream
---

Read #file:../copilot-instructions.md before starting.
Do NOT modify: executor.py, validator.py, security.py, verifier.py,
rollback.py, pc_control.py, classifier.py, api/server.py, history.py,
context_pruner.py, settings.py, macros.py.
Run 'atlas --status' after every file change.

---

FIX 1 — voice.py: Complete rewrite of speak(), start_ptt_listener(), \_ptt_loop()

REWRITE speak() — use temp file + ffplay:

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
              import tempfile, os
              tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
              tmp_path = tmp.name
              tmp.close()

              speed = float(settings.get("voice_speed") or 1.0)
              rate = f"+{int((speed - 1.0) * 100)}%"

              result = subprocess.run(
                  ["edge-tts", f"--rate={rate}", "--text", clean,
                   "--write-media", tmp_path],
                  capture_output=True,
              )
              if result.returncode != 0:
                  return

              _tts_process = subprocess.Popen(
                  ["ffplay", "-nodisp", "-autoexit",
                   "-loglevel", "quiet", tmp_path],
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

REWRITE PTT — replace \_record_ptt(), \_ptt_loop(), start_ptt_listener():

ADD these module-level variables after existing ones:
\_ptt_recording = threading.Event()
\_ptt_frames: list = []

REPLACE \_record_ptt() and \_ptt_loop() with:

def \_on_ptt_press(\_event: Any) -> None:
if not \_ptt_recording.is_set():
\_ptt_frames.clear()
\_ptt_recording.set()
print("[blue]Recording...[/blue]", flush=True)

def \_on_ptt_release(\_event: Any) -> None:
\_ptt_recording.clear()

def \_ptt_capture_loop() -> None:
device = settings.get("voice_input_device")
device_index = int(device) if device is not None else None
kwargs: dict = dict(
samplerate=SAMPLE_RATE, channels=1,
dtype="int16", blocksize=CHUNK,
)
if device_index is not None:
kwargs["device"] = device_index

      with sd.InputStream(**kwargs) as stream:
          was_recording = False
          while not _ptt_stop_event.is_set():
              frame, _ = stream.read(CHUNK)
              if _ptt_recording.is_set():
                  was_recording = True
                  _ptt_frames.append(
                      frame.reshape(-1).astype(np.int16, copy=False).copy()
                  )
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

REPLACE start_ptt_listener():

def start_ptt_listener() -> None:
import ctypes
try:
is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
except Exception:
is_admin = False
if not is_admin:
print(
"[yellow]PTT WARNING: Run terminal as administrator.[/yellow]",
flush=True,
)
hotkey = str(settings.get("voice_key") or "f8")
\_ptt_stop_event.clear()
keyboard.on_press_key(hotkey, \_on_ptt_press)
keyboard.on_release_key(hotkey, \_on_ptt_release)
thread = threading.Thread(target=\_ptt_capture_loop, daemon=True)
thread.start()
print(f"[green]Push-to-talk active - hold {hotkey} to speak[/green]",
flush=True)

REPLACE stop_ptt_listener():

def stop_ptt_listener() -> None:
\_ptt_stop_event.set()
try:
keyboard.unhook_key(str(settings.get("voice_key") or "f8"))
except Exception:
pass

---

FIX 2 — wake_word.py: Full rewrite of \_listen_loop() — single stream only

DELETE \_on_wake_word() function entirely.
DELETE \_record_until_silence() function entirely.

REPLACE \_listen_loop() with exactly this — ONE stream, inline capture:

def \_listen_loop() -> None:
threshold = float(settings.get("wake_word_threshold") or 0.35)
silence_ms = int(settings.get("vad_silence_ms") or 1500)
max_silent = max(1, int((silence_ms / 1000.0) \* SAMPLE_RATE / CHUNK))
device = settings.get("voice_input_device")
device_index = int(device) if device is not None else None

      while not _stop_event.is_set():
          try:
              kwargs: dict = dict(
                  samplerate=SAMPLE_RATE, channels=1,
                  dtype="int16", blocksize=CHUNK,
              )
              if device_index is not None:
                  kwargs["device"] = device_index

              with sd.InputStream(**kwargs) as stream:
                  print(
                      f"[green]Wake word active - say '{_wake_phrase()}'[/green]",
                      flush=True,
                  )
                  while not _stop_event.is_set():
                      try:
                          frame, _ = stream.read(CHUNK)
                          frame_i16 = frame.reshape(-1).astype(np.int16, copy=False)
                          audio_f = frame_i16.astype(np.float32) / 32768.0

                          if _oww_model is None:
                              continue

                          pred = _oww_model.predict(audio_f)
                          if not isinstance(pred, dict):
                              continue
                          if not any(float(s) > threshold for s in pred.values()):
                              continue

                          if not _WAKE_LOCK.acquire(blocking=False):
                              continue

                          _CAPTURING.set()
                          try:
                              print("\n[blue]ATLAS: Listening...[/blue]",
                                    flush=True)
                              _broadcast_event({"type": "listening_start"})

                              # Drain 200ms buffer on same stream
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
                                  energy = int(np.max(np.abs(ci))) if ci.size else 0

                                  if not started:
                                      if energy > 800:
                                          started = True
                                          chunks.append(ci.copy())
                                      elif time.time() > deadline:
                                          break
                                  else:
                                      chunks.append(ci.copy())
                                      if energy < 800:
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
                                  print("[dim]No speech detected.[/dim]",
                                        flush=True)
                                  continue

                              print(f"[dim]Heard: {normalized}[/dim]",
                                    flush=True)

                              ks = str(
                                  settings.get("killswitch_word") or "stop"
                              ).lower()
                              if normalized == ks:
                                  try:
                                      import killswitch as _ks
                                      _ks.fire()
                                  except Exception:
                                      pass
                                  continue

                              ctx = memory.get_context_for_llm(normalized)
                              parsed = (
                                  classifier.classify(normalized)
                                  or llm_engine.query(normalized, ctx)
                              )
                              action = str(parsed.get("action", ""))
                              params = parsed.get("params", {})
                              if not isinstance(params, dict):
                                  params = {}
                              result = executor.execute(action, params)
                              resp = str(result.get("message", "Done."))
                              memory.add_to_sliding("user", normalized)
                              memory.add_to_sliding("assistant", resp)
                              voice.speak(resp)

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

---

FIX 3 — memory.py: Suppress BertModel warning permanently

ADD these two lines at the very top of memory.py before all imports:
import os
os.environ["TRANSFORMERS_NO_ADVISORY_WARNINGS"] = "1"

ALSO add this import at the top:
import logging as \_logging
\_logging.getLogger("sentence_transformers").setLevel(\_logging.ERROR)

CHANGE \_load_encoder() to suppress the load report:
def \_load_encoder() -> Any:
try:
import logging as \_lg
\_lg.getLogger("sentence_transformers").setLevel(\_lg.ERROR)
\_lg.getLogger("transformers").setLevel(\_lg.ERROR)
from sentence_transformers import SentenceTransformer
return SentenceTransformer("all-MiniLM-L6-v2")
except Exception:
return \_FallbackEncoder()

---

END TESTS:

1. atlas 'what time is it' → time printed AND spoken aloud
2. PTT (as admin, hold F8): say "open notepad" → "[dim]Heard: open notepad[/dim]"
   printed → Notepad opens
3. Wake word: say "hey atlas" → "[blue]ATLAS: Listening...[/blue]" printed →
   say "what time is it" → time spoken
4. atlas --status → zero stream warnings
5. NO BertModel LOAD REPORT in output
