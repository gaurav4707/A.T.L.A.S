---
agent: agent
description: Build ATLAS v1 Phase 1 — Core Pipeline
---

Build Phase 1 of ATLAS v1. Follow #file:../copilot-instructions.md exactly.

WHAT TO BUILD:

MODULE 1 — settings.py
Load and validate config.json. If missing: create with these defaults:
  model, voice_input (false), voice_output (false), voice_key (right_ctrl),
  voice_speed (1.0), session_memory (false), session_memory_turns (8),
  allowed_paths ([user home]), blocked_paths ([C:/Windows, C:/Program Files]),
  pin_hash (""), trash_dir (.atlas_trash), trash_retention_days (7),
  log_file (history.db), api_token (generate random UUID on first run)
If pin_hash is empty: this is first-run — flag it for PIN setup.
Expose: load() → dict, save(config: dict), get(key: str)

MODULE 2 — llm_engine.py
Wrap Ollama Python client for Mistral 7B.
Every call uses llama.cpp GBNF grammar to force this exact JSON:
  {"intent": str, "action": str, "params": dict, "response": str, "risk": str}
  risk must be exactly one of: "low" | "medium" | "high" | "critical"
If LLM returns malformed JSON: retry once. On second failure: return this safe dict:
  {"intent":"unknown","action":"unknown","params":{},
   "response":"I did not understand that. Could you rephrase?","risk":"low"}
Session memory: if session_context list is non-empty, prepend it to the prompt.
Model name comes from settings.get('model') — never hardcoded.
Expose: query(prompt: str, session_context: list) → dict

MODULE 3 — classifier.py
Fast regex + keyword matcher. Returns same JSON shape as llm_engine or None.
Must handle (case-insensitive, extra spaces OK):
  open {app}          → open_app, risk: low
  close {app}         → close_app, risk: low
  search {query}      → web_search, risk: low
  volume {n}          → set_volume, params: {level: int}, risk: low
  set volume {n}      → same
  mute                → mute_volume, risk: low
  shutdown            → shutdown_pc, risk: high
  restart             → restart_pc, risk: high
  sleep               → sleep_pc, risk: low
  open {url}          → open_url, risk: low
  copy {text}         → clipboard_write, risk: low
  what time is it     → get_time, risk: low
  run macro {name}    → run_macro, params: {name: str}, risk: low
Return None if nothing matches — never raise an exception.
Expose: classify(text: str) → dict | None

MODULE 4 — main.py (Phase 1 — simple loop only, no FastAPI)
  while True:
    text = input("ATLAS > ")
    result = classifier.classify(text) or llm_engine.query(text, session_ctx)
    print(result)
Session context: Python list. If session_memory enabled: append each exchange,
trim to last session_memory_turns. If disabled: pass empty list.
If pin_hash is empty in config: print setup instructions and exit.

After building, install dependencies and run:
  pip install ollama rich bcrypt fastapi uvicorn
  python main.py

PHASE 1 END TESTS — run these and confirm:
1. python main.py starts with no errors, config.json created
2. Type "open chrome" → classifier matches, NO Ollama call (add a debug print to confirm)
3. Type "VOLUME 70" → classifier matches case-insensitively, returns set_volume
4. Type "what is the meaning of life" → falls through to Mistral 7B → valid JSON returned
5. Type "delete my file" → Mistral returns risk: "high"
6. Type something ambiguous → classifier returns None → LLM called → valid JSON