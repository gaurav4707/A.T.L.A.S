# ATLAS — Copilot Instructions (v2)

## Project

ATLAS v2 (Almost Thinking Local AI System). Builds on v1.
Python 3.11+, Windows 10/11, Node 20+, Rust (Tauri).

## v1 Still Works

All 14 v1 modules are unchanged unless explicitly upgraded in the current phase.
The 'atlas' CLI command works throughout all v2 phases.

## What v2 Adds

1. ChromaDB semantic memory (replaces Python list in main.py)
2. Background context pruner (Mistral 7B thread → ChromaDB)
3. FastAPI WebSocket streaming endpoint
4. Porcupine wake word (replaces push-to-talk)
5. Tauri + React HUD connecting to same FastAPI backend
6. Task chains (extends macros — macros.json still loads)

## What v2 Does NOT Add (deferred to v3)

- LlamaIndex RAG knowledge base
- VS Code extension
- Per-app profiles
- Coding assistant / CP coach / debug assistant
- Sprint mode / automation recorder
- Backup / export bundle
- Screen awareness (LLaVA)
- Plugin system

## Hard Rules — Same as v1, Plus These

1. No LLM text EVER reaches os.system() or subprocess directly.
2. Every Action class implements execute() AND verify().
3. Type hints on every Python function.
4. Docstring on every file and class.
5. async/await for all I/O.
6. Run existing tests before writing new code.
7. Commit after every working vertical slice.
8. v1 CLI must still work after every single module change — test it.
9. EXACTLY 3 memory systems: ChromaDB (facts+summaries), sliding window, pruner.
   Never add a 4th store. Never write to a JSON history file.
10. HUD connects to the SAME FastAPI backend as CLI — no separate backend.
11. Chains go through executor.ACTION_MAP — same security pipeline, no shortcuts.

## v2 Module Changes vs v1

UPGRADED: memory.py (NEW — replaces Python list in main.py)
voice.py (wake_word.py added alongside, push-to-talk kept)
api/server.py (add WebSocket endpoint, keep all REST endpoints)
macros.py → chains.py (superset, macros.json still loads)
NEW: context_pruner.py, wake_word.py, killswitch.py
NEW: hud/ (Tauri + React project, separate from Python)
NEW: api/ws_manager.py
UNCHANGED: classifier.py, llm_engine.py, validator.py, security.py,
executor.py, verifier.py, rollback.py, pc_control.py,
history.py, settings.py

## New Tech for v2

chromadb, sentence-transformers, openWakeWord (fully open-source, no API key), webrtcvad,
Tauri (cargo), React + TypeScript, react-markdown, Prism.js
