---
agent: agent
description: ATLAS Wake Word — Replace OpenWakeWord with Vosk Grammar Spotting
---

Read #file:../copilot-instructions.md before starting.
Do NOT modify: executor.py, validator.py, security.py, verifier.py,
rollback.py, pc_control.py, classifier.py, api/server.py, history.py,
context_pruner.py, memory.py, macros.py, llm_engine.py, voice.py.
Run 'atlas --status' after every file change.

---

GOAL:
Replace the OpenWakeWord detection engine in wake_word.py with Vosk
grammar-based keyword spotting. The SPMC architecture is preserved exactly:
one producer thread owns the mic, one consumer thread runs the state machine.
Only the detection logic inside the consumer's DETECTING state changes.

---

MODULE CHANGES — wake_word.py

REMOVE these imports:
from openwakeword.model import Model as OWWModel (and all references)

ADD these imports:
import json
from vosk import Model as VoskModel, KaldiRecognizer

REMOVE these module-level variables:
\_oww_model
\_available
\_active_backend_model
\_cooldown_until (replaced by \_last_trigger_time)
COOLDOWN_SECONDS (replaced by COOLDOWN = 2.0)

ADD these module-level variables:
\_vosk_model: VoskModel | None = None
\_vosk_loaded: bool = False
\_last_trigger_time: float = 0.0
COOLDOWN: float = 2.0 # minimum seconds between triggers

# Vosk recognizer is created fresh per consumer loop iteration

# because KaldiRecognizer is not thread-safe to share across resets

---

REPLACE \_load_model() → \_load_vosk_model():

def \_load_vosk_model() -> bool:
global \_vosk_model, \_vosk_loaded
if \_vosk_loaded and \_vosk_model is not None:
return True
if \_vosk_loaded and \_vosk_model is None:
return False # already failed, don't retry

    model_path = str(settings.get("vosk_model_path") or "vosk-model-small-en-us-0.15")

    try:
        import os
        if not os.path.isdir(model_path):
            print(
                f"[red]Vosk model not found at '{model_path}'.[/red]\n"
                f"[yellow]Download from https://alphacephei.com/vosk/models[/yellow]\n"
                f"[yellow]Extract and place the folder in your atlas/ directory.[/yellow]",
                flush=True,
            )
            _vosk_loaded = True
            _vosk_model = None
            return False

        import logging
        logging.getLogger("vosk").setLevel(logging.ERROR)   # suppress Vosk startup chatter
        _vosk_model = VoskModel(model_path)
        _vosk_loaded = True
        phrase = str(settings.get("wake_word_phrase") or "hey atlas")
        print(f"[green]Vosk loaded — listening for '{phrase}'[/green]", flush=True)
        return True

    except Exception as exc:
        print(f"[red]Vosk model failed to load: {exc}[/red]", flush=True)
        _vosk_model = None
        _vosk_loaded = True
        return False

---

REPLACE is_available():

def is_available() -> bool:
return \_load_vosk_model()

---

REPLACE \_wake_phrase():

def \_wake_phrase() -> str:
return str(settings.get("wake_word_phrase") or "hey atlas")

---

REPLACE the DETECTING state block inside \_consumer_loop():

The consumer loop structure is unchanged. Only the DETECTING block changes.
Replace the entire DETECTING block (the OWW predict section) with this:

# Build a fresh recognizer each time we enter DETECTING from scratch.

# KaldiRecognizer is stateful — resetting between captures prevents

# old audio state bleeding into the next detection window.

phrase = \_wake_phrase().lower()
grammar = json.dumps([phrase, "[unk]"])
recognizer = KaldiRecognizer(\_vosk_model, SAMPLE_RATE, grammar)
recognizer.SetWords(False)

# Detection inner loop — exits when triggered or \_stop_event fires

while not \_stop_event.is_set() and not ptt_active.is_set():
frame = \_get_frame()
if frame is None:
continue

      # PTT preempt check (same as outer loop)
      if ptt_active.is_set():
          break

      pcm_bytes = frame.tobytes()
      accepted = recognizer.AcceptWaveform(pcm_bytes)

      if accepted:
          result = json.loads(recognizer.Result())
          text = result.get("text", "").lower().strip()
      else:
          # Partial result check — catches phrase before utterance ends
          partial = json.loads(recognizer.PartialResult())
          text = partial.get("partial", "").lower().strip()

      if phrase in text:
          now = time.time()
          if now - _last_trigger_time < COOLDOWN:
              # Debounce — same utterance still scoring, reset recognizer
              recognizer = KaldiRecognizer(_vosk_model, SAMPLE_RATE, grammar)
              recognizer.SetWords(False)
              continue

          _last_trigger_time = now

          print("\n[blue]ATLAS: Listening...[/blue]", flush=True)
          _broadcast_event({"type": "listening_start"})

          # Transition to CAPTURING — reset recognizer so wake audio is not
          # carried into the Whisper capture buffer via lingering state.
          state = _State.CAPTURING
          cap, speech_started, silent_count = [], False, 0
          drain_remaining = WAKE_DRAIN_CHUNKS
          capture_deadline = time.time() + MAX_CAPTURE_SECONDS
          break   # exit detection inner loop, outer loop continues in CAPTURING

# If ptt_active broke us out, outer loop handles PTT_RECORDING transition

---

REMOVE entirely from wake_word.py:
\_resolve_backend_model()
\_candidate_models()
session_peak tracking (the "Wake peak: X.XX" print lines)
All references to \_oww_model, \_available, \_active_backend_model

---

UPDATE start_wake_word_listener():

Replace \_load_model() call with \_load_vosk_model():

def start_wake_word_listener() -> bool:
global \_producer_thread, \_consumer_thread, \_watchdog_thread

    if not _load_vosk_model():
        print("[yellow]Vosk backend unavailable — wake word disabled[/yellow]", flush=True)
        return False

    _stop_event.clear()

    _producer_thread = threading.Thread(
        target=_producer_loop, daemon=True, name="atlas-mic-producer"
    )
    _producer_thread.start()

    _consumer_thread = threading.Thread(
        target=_consumer_loop, daemon=True, name="atlas-wake-consumer"
    )
    _consumer_thread.start()

    if _watchdog_thread is None or not _watchdog_thread.is_alive():
        _watchdog_thread = threading.Thread(
            target=_watchdog_loop, daemon=True, name="atlas-watchdog"
        )
        _watchdog_thread.start()

    return True

---

UPDATE is_listening():

def is_listening() -> bool:
return (
\_producer_thread is not None
and \_producer_thread.is_alive()
and not \_stop_event.is_set()
)

---

DO NOT CHANGE anything else:
\_producer_loop() — unchanged, still owns the mic
\_consumer_loop() — structure unchanged, only DETECTING block replaced
\_dispatch_command() — unchanged
\_fire_dispatch() — unchanged
\_watchdog_loop() — unchanged
stop_wake_word_listener() — unchanged
ptt_active — unchanged, voice.py still sets/clears it
\_broadcast_event() — unchanged

---

END TESTS:

1. python -c "from vosk import Model; m = Model('vosk-model-small-en-us-0.15'); print('OK')"
   → prints OK. If it fails: model path wrong or pip install vosk missing.

2. atlas → startup banner appears, then:
   "[green]Vosk loaded — listening for 'hey atlas'[/green]"
   NO "openWakeWord" references in output.

3. Say "hey atlas" clearly → "[blue]ATLAS: Listening...[/blue]" printed ONCE.
   Say it again immediately → second trigger blocked by 2-second cooldown.

4. After listening prompt → say "what time is it" → time returned and spoken.

5. Say "hey atlas open chrome" (phrase + command in one breath):
   → listening prompt fires
   → "open chrome" captured and executed
   (Vosk grammar discards non-phrase audio, Whisper handles command audio)

6. Hold PTT key mid-session → PTT recording works alongside wake word.
   NO PaErrorCode stream errors.

7. atlas 'open notepad' typed → still works (regression check).

8. atlas --status → clean output, no Vosk warnings.

9. Unrelated speech (normal conversation near mic) → does NOT trigger.
   Only "hey atlas" triggers. Grammar mode enforces this at the recognizer level.
