---
agent: agent
description: ATLAS v2 Phase 2 — Porcupine Wake Word + Killswitch
---

Build Phase 2 of ATLAS v2. Read #file:../copilot-instructions.md.
Do NOT modify: executor.py, security.py, validator.py, verifier.py,
rollback.py, pc_control.py, history.py, classifier.py, llm_engine.py,
memory.py, context_pruner.py, api/ws_manager.py.
Test 'atlas --status' and 'atlas open notepad' after every file change.

MODULE 1 — wake_word.py (NEW FILE)
import pvporcupine, sounddevice, webrtcvad, struct, threading

Porcupine setup:
porcupine = pvporcupine.create(
access_key=settings.get('porcupine_key'),
keywords=['hey atlas'] # or closest available keyword from Porcupine free tier
)

Dedicated listener thread (CPU target: < 2%):
def _listen_loop():
vad = webrtcvad.Vad(2) # aggressiveness 0-3
with sounddevice.InputStream(
samplerate=porcupine.sample_rate, channels=1,
dtype='int16', blocksize=porcupine.frame_length) as stream:
while not \_stop_event.is_set():
pcm_data, _ = stream.read(porcupine.frame_length)
pcm = struct.unpack_from("h" \* porcupine.frame_length, pcm_data)
result = porcupine.process(pcm)
if result >= 0:
\_on_wake_word()

def \_on_wake_word():
print("\n[blue]ATLAS: Listening...[/blue]")
ws_manager.broadcast({"type":"listening_start"}) # async-safe broadcast

# record until silence (webrtcvad)

audio = \_record_until_silence(vad_aggressiveness=2, silence_ms=1500)
text = voice.transcribe_audio(audio) # reuse voice.py's Whisper call
if text.strip():
print(f"[dim]Heard: {text}[/dim]")
result = classifier.classify(text) or llm_engine.query(
text, memory.get_context_for_llm(text))
executor.execute(result['action'], result['params'])

def \_record_until_silence(vad_aggressiveness, silence_ms):

# record frames until silence_ms of silence detected using webrtcvad

# return raw audio bytes for Whisper

config.json additions (add in settings.py defaults):
"porcupine_key": "" (user gets free key from picovoice.io)
"wake_word": "hey atlas"
"wake_word_enabled": false (off by default, enabled after key is set)
"vad_silence_ms": 1500

Guard in start_wake_word_listener():
if not settings.get('porcupine_key'):
print("[yellow]Wake word disabled: set porcupine_key in config.json[/yellow]")
print("[dim]Get a free key at: picovoice.io[/dim]")
return
if not settings.get('wake_word_enabled'):
return

# start thread

Expose: start_wake_word_listener(), stop_wake_word_listener(), is_listening() → bool
Start in main.py startup (after FastAPI ready, before banner):
wake_word.start_wake_word_listener()

MODULE 2 — killswitch.py (NEW FILE)
Two triggers: spoken stop word (second Porcupine keyword) + keyboard hotkey.
Target: < 200ms from trigger to silence.

import threading, keyboard as kb

\_stop_event = threading.Event()
\_current_tts_process = None # reference to current edge-tts subprocess

def fire():
voice.stop_speaking() # kills TTS subprocess immediately
\_stop_event.set() # signals llm_engine streaming to stop

# broadcast to HUD

import asyncio
try: asyncio.get_event_loop().run_until_complete(
ws_manager.broadcast({"type":"killswitch"}))
except: pass
print("\n[red]⏹ Stopped.[/red]")
\_stop_event.clear() # reset for next command

def register_hotkey():
hotkey = settings.get('killswitch_hotkey', 'ctrl+shift+k')
kb.add_hotkey(hotkey, fire)

Expose: fire(), register_hotkey()

In main.py startup: killswitch.register_hotkey()

Add to llm_engine.py (minimal change):
Add module-level: killswitch_event = threading.Event()
In streaming loop: check killswitch_event.is_set() between chunks
killswitch.fire() sets this event: from llm_engine import killswitch_event; killswitch_event.set()

config.json additions:
"killswitch_hotkey": "ctrl+shift+k"
"killswitch_word": "stop"

Update voice.py:
keep all push-to-talk code intact
In startup print: "Wake word active — say 'Hey ATLAS'" or "Push-to-talk — hold Right Ctrl"
Register killswitch in speak(): store subprocess as killswitch.\_current_tts_process

install: pip install pvporcupine webrtcvad

PHASE 2 END TESTS:

1. Add porcupine_key to config.json, set wake_word_enabled: true
   Say "Hey ATLAS open notepad" → Notepad opens (full wake word flow working)
2. Say "Hey ATLAS" → ATLAS prints "Listening..." → stay silent 1.5s → returns to idle
   (webrtcvad silence detection working)
3. Start a long ATLAS voice response → say "stop" → TTS stops < 200ms
4. Press Ctrl+Shift+K mid-response → same result as "stop"
5. push-to-talk still works: hold Right Ctrl → speak → executes (not broken)
6. No porcupine_key in config → clean message, no crash, push-to-talk still works
7. atlas --status → "Wake Word: active" or "Push-to-talk mode" shown correctly
8. wscat connected → speak wake word command → receives {"type":"listening_start"}
   then {"type":"done",...} after execution
