---
agent: agent
description: Build ATLAS v1 Phase 4 — Voice + Polish
---

Build Phase 4 of ATLAS v1. Read #file:../copilot-instructions.md.
Phases 1-3 complete. Do NOT modify: api/server.py, executor.py, security.py,
validator.py, classifier.py, llm_engine.py.

MODULE 1 — voice.py
All voice features off by default. Check config before doing anything.

VOICE INPUT (push-to-talk only — no wake word, no always-on):
Triggered by holding the key in settings.get('voice_key') (default: right_ctrl)
Use keyboard library to detect key hold
On key down: start recording via sounddevice
On key up: stop recording → pass audio to Whisper tiny for transcription
Whisper model: load with whisper.load_model('tiny') ONCE on first use, then cache
Print transcribed text to terminal before executing (so user sees what was heard)
Pass transcribed text through the same classifier → executor pipeline as typed input

VOICE OUTPUT (edge-tts):
After every executor result: speak the 'response' field only (not technical JSON)
Run edge-tts as a subprocess (so it can be killed)
Killswitch: keyboard listener watches for Escape key
On Escape: call process.kill() on the edge-tts subprocess
Target: < 200ms from key press to silence
Speed from settings.get('voice_speed')

Expose:
start_ptt_listener() → starts keyboard listener for push-to-talk
transcribe_from_mic() → str → record while key held, return transcribed string
speak(text: str) → start edge-tts subprocess, return process handle
stop_speaking() → kill current subprocess immediately

install: pip install sounddevice openai-whisper keyboard edge-tts

MODULE 2 — Session memory (wire it fully in main.py + api/server.py)
In main.py REPL:
if settings.get('session_memory'):
session_ctx = deque(maxlen=settings.get('session_memory_turns') \* 2)
after each exchange: session_ctx.append(user_msg); session_ctx.append(assistant_msg)
pass list(session_ctx) to llm_engine.query()
else:
pass [] to llm_engine.query()
system prompt when off: "You have no memory of previous sessions.
Treat each command as independent."
In api/server.py /command endpoint: same logic, but session_ctx is per-process
(shared across API calls within the same running session).

MODULE 3 — Polish (quality-of-life — not new features)
Startup sequence in main.py (this exact order):

1. settings.load() → if malformed JSON: print "config.json is broken.
   Delete it and run atlas --setup." → sys.exit(1)
2. rollback.auto_purge() → silent
3. Check Ollama: requests.get('http://localhost:11434', timeout=2)
   If fails: print "Ollama is not running. Start it with: ollama serve" → sys.exit(1)
4. if not settings.get('pin_hash'): security.setup_pin()
5. Start FastAPI background thread
6. Poll FastAPI ready (max 10s)
7. if voice_input enabled: voice.start_ptt_listener()
8. Print startup banner (rich Panel):
   "ATLAS v1 | Model: [model] | Voice: [on/off] | Memory: [on/off] | --help for commands"

Error handling:
Wrap entire REPL loop in try/except Exception as e:
print(f"[red]Something went wrong: {e}[/red]")
(never show traceback in normal operation)
Log unexpected errors to error.log (append mode)

Rich formatting:
Success results: green Panel
Errors: red Panel
Warnings: yellow Panel
Dry-run: yellow Panel with dashed border
History: Table with timestamp, command, action, success columns
Startup banner: blue Panel

Ollama model hint:
After startup: check 'ollama list' output
If 'llama3' available but config model is 'mistral':
print "[dim]Tip: LLaMA 3 8B is available. Set model: llama3 in config.json[/dim]"

--help output: rich Panel with two columns — flag name and description

PHASE 4 END TESTS (final v1 daily-driver acceptance):

1. python main.py → banner appears in < 5 seconds
2. atlas 'what time is it' → classifier handles it, no Ollama call
3. Enable voice_output in config → atlas 'open notepad' → response spoken aloud
4. Press Escape mid-speech → audio stops < 200ms
5. Enable voice_input → hold Right Ctrl → speak "open notepad" → transcribed text
   shown → Notepad opens
6. atlas 'delete fakefile.txt' → clean English error ("file not found"), no traceback
7. Start without Ollama → clean message → exits
8. Session memory OFF → two related commands → second has no context of first
9. Session memory ON → enable in config → two related commands → second uses context
10. atlas --help → full reference card displayed

DAILY-DRIVER SIGN-OFF CHECKLIST (confirm all before calling v1 done):
☑ Open an app by voice
☑ Search the web by typing
☑ Delete a file safely: PIN + soft-delete to .atlas_trash + verify()
☑ Try to delete a file open in VS Code with unsaved changes → E-05 blocks it
☑ Run the 'dev' macro → all 3 steps execute
☑ Re-run a command from history
☑ Dry-run a delete command → no execution
☑ Restart ATLAS → session_memory cleared (if it was enabled)
☑ All 25 v1 features confirmed working
