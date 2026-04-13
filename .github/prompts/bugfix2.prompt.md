---
agent: agent
description: ATLAS Bug Fix — Transcription Hearing Incorrectly (PTT + Wake Word)
---

Read #file:../copilot-instructions.md before starting.
Do NOT modify: executor.py, validator.py, security.py, verifier.py,
rollback.py, pc_control.py, classifier.py, api/server.py, history.py,
context_pruner.py, memory.py, settings.py, macros.py.
Run 'atlas --status' after every file change.

---

CONTEXT:
Both push-to-talk and wake word transcription hear commands incorrectly.
Root causes:

1. Whisper model is "base" — too large and slow, causing truncation on short commands
2. VAD silence stripping in \_strip_silence_with_vad() is too aggressive,
   cutting off consonants at the start/end of words
3. The energy threshold 1500 used in wake_word.py \_on_wake_word() is too high
   for quieter microphones
4. Whisper prompt does not include enough command vocabulary to bias recognition

---

FIX 1 — voice.py: Fix Whisper model and transcription settings

CHANGE \_load_whisper_model():
def \_load_whisper_model() -> whisper.Whisper:
global \_MODEL
if \_MODEL is None: # "small" gives better accuracy than "base" for command recognition # and is fast enough on CPU for PTT use (2-4s transcription)
\_MODEL = whisper.load_model("small")
return \_MODEL

CHANGE warmup_model() to call \_load_whisper_model() with the new model name:
No change needed — it already calls \_load_whisper_model() generically.

CHANGE the transcribe call in \_stop_recording_and_transcribe():
Find BOTH places where model.transcribe() is called and update the prompt
and settings in BOTH to:
transcribed = model.transcribe(
audio_np,
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

Apply the same transcribe() settings change to transcribe_from_array() as well
(this is used by wake word path). Find model.transcribe() in that function
and replace with the same parameters above.

CHANGE \_strip_silence_with_vad() — reduce aggressiveness:
vad = webrtcvad.Vad(1) # was 2 — less aggressive, keeps more speech frames

CHANGE MIN_RECORD_SECONDS at the top of voice.py:
MIN_RECORD_SECONDS = 0.3 # was 0.18 — gives mic time to stabilize

---

FIX 2 — wake_word.py: Fix \_on_wake_word() stream and energy threshold

CHANGE the energy threshold from 1500 to 800 in \_on_wake_word():
The fresh capture stream section has:
if energy > 1500:
Change to:
if energy > 800:

And the silence detection section has:
if energy < 1500:
Change to:
if energy < 800:

This makes the system work on quieter microphones while still rejecting
pure background noise.

ALSO: Add a 200ms drain delay after wake word fires before opening
the capture stream, to let the mic buffer flush detection audio:

In \_on_wake_word(), BEFORE opening the fresh capture stream, add:

# Drain mic buffer — prevents detection audio bleeding into capture

time.sleep(0.2)

---

FIX 3 — wake_word.py: Replace \_on_wake_word() call signature

The current \_listen_loop() calls:
\_on_wake_word(stream=stream, frame_length=chunk_size)

CHANGE to:
\_on_wake_word()

AND update \_on_wake_word() signature to take no arguments:
def \_on_wake_word() -> None:

This is required because Fix 1 in the previous bugfix prompt replaced
the body to open its own fresh stream. Confirm the function signature
and the call site both match — no arguments.

---

FIX 4 — voice.py: Add pre-roll buffer to PTT to avoid clipping first word

PROBLEM: When the user presses F8 and immediately speaks, the first
syllable is cut off because the stream takes ~50-100ms to stabilize.

In \_start_recording():
After \_RECORDING_STREAM.start(), add a 100ms pre-roll read:
try:
import time as \_time
\_time.sleep(0.1)
except Exception:
pass

This gives the sounddevice stream time to stabilize before audio
is meaningful.

---

END TESTS — run after all fixes applied:

1. PTT test (run terminal as admin):
   - Hold F8, say "open notepad" clearly and slowly, release F8
   - Expected: "[voice] Heard: open notepad" printed, Notepad opens
   - If heard wrong: try speaking slightly slower with a pause after pressing F8

2. PTT short command test:
   - Hold F8, say "what time is it", release
   - Expected: time returned correctly

3. Wake word test (after Python 3.11 migration):
   - Say "hey jarvis" then wait 0.5 seconds then say "open chrome"
   - Expected: Chrome opens

4. Noise test:
   - Hold F8, say nothing, release after 1 second
   - Expected: "[voice] No speech detected" printed, nothing executes

5. atlas --status → clean output, no errors
6. atlas 'open notepad' → typed command still works (regression check)
