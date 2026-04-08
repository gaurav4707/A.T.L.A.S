# ATLAS — Copilot Instructions

## Project
ATLAS v1 (Almost Thinking Local AI System). A local, offline, CLI-first
personal assistant. Python 3.11+, Windows 10/11.

## What v1 Is
Type or speak a command → ATLAS understands it → executes it on the PC → 
speaks the result (optional). CLI only. No GUI. No always-on mic. 
No persistent memory across sessions. No background threads.

## Hard Rules — Never Violate
1. No LLM text EVER reaches os.system() or subprocess directly.
   All execution goes through executor.ACTION_MAP only.
2. Every Action class implements execute() AND verify().
3. Type hints on every Python function.
4. Docstring on every file and class.
5. async/await for all I/O (FastAPI endpoints, voice, file ops).
6. Run existing tests before writing new code.
7. Commit after every working vertical slice.

## What v1 Does NOT Have (deferred — do not add)
- No Tauri/React HUD
- No Porcupine wake word or always-on microphone
- No ChromaDB or any persistent memory store
- No background threads of any kind
- No WebSocket streaming
- No task chains with rollback (macros.json only)
- No VS Code extension, per-app profiles, or RAG knowledge base

## 14 Modules — Exact Filenames
main.py, api/server.py, classifier.py, llm_engine.py,
validator.py, security.py, executor.py, verifier.py,
rollback.py, voice.py, pc_control.py, macros.py,
history.py, settings.py

## Tech Stack (12 entries only)
Ollama + Mistral 7B, llama.cpp GBNF grammar,
custom regex pre-classifier, FastAPI + Uvicorn (localhost:8000),
rich (CLI formatting), SpeechRecognition + Whisper tiny, edge-tts,
pywinauto + Playwright + subprocess, bcrypt,
Python list (session memory), sqlite3, json stdlib

## Security Model
Three rings:
  Ring 1 — validator.py: action whitelist + risk tier lookup + E-02 + E-05
  Ring 2 — security.py: Low=pass, Medium=type yes, High=PIN, Critical=blocked
  Ring 3 — executor.py: pre-approved function dispatch map, no raw shell

## Macros (v1 only — not chains)
macros.json: name → list of command strings.
500ms gap between steps. Stop on first failure. No rollback.
Each step goes through executor.execute() — same security pipeline.

## CLI Commands
atlas 'open chrome'        single command
atlas                      REPL loop
atlas --dry 'delete x'    preview without executing
atlas --history            last 20 commands
atlas --rerun 5            re-execute command #5
atlas --macro list/run/add macro management
atlas --status             show model, voice, PIN, uptime
atlas --setup              first-run wizard
atlas --install-cli        register atlas on PATH