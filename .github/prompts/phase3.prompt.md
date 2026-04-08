---
agent: agent
description: Build ATLAS v1 Phase 3 — CLI + FastAPI + History + Macros
---

Build Phase 3 of ATLAS v1. Read #file:../copilot-instructions.md.
Phases 1 and 2 are working. Do NOT modify executor.py, security.py, validator.py,
verifier.py, rollback.py, or pc_control.py.

MODULE 1 — history.py
SQLite file: settings.get('log_file') — NOT hardcoded as 'history.db'
Table: commands(id INTEGER PK AUTOINCREMENT, timestamp TEXT, raw_command TEXT,
parsed_action TEXT, params TEXT, success INTEGER, latency_ms INTEGER, risk_tier TEXT)

log(raw, action, params, success, latency_ms, risk):
open connection, INSERT row, commit, close — do NOT keep persistent connection

list_recent(n: int = 20) → list[dict]:
SELECT last n rows ORDER BY id DESC — return list, NEVER print

search(keyword: str) → list[dict]:
SELECT WHERE raw_command LIKE '%keyword%'

get_by_id(n: int) → dict | None:
SELECT WHERE id = n

rerun(n: int):
row = get_by_id(n)
text = row['raw_command']
result = classifier.classify(text) or llm_engine.query(text, [])
return executor.execute(result['action'], result['params'])

MODULE 2 — macros.py
Load macros.json from project root. If missing: create with examples:
{"dev":["open vscode .","open chrome localhost:3000","open terminal"],
"notes":"open vscode C:/Users/Gaurav/notes.md",
"vol50":"set volume 50",
"search":"open chrome google.com/search?q={input}"}

run(name: str, input_val: str = "") → dict:
load macros.json
find name — if missing: return error dict
normalize to list (single string becomes [string])
for each step: substitute {input} with input_val
time.sleep(0.5) between steps
run through executor.execute() — full security pipeline
if step fails: stop, return dict with failed_step info — NO auto-rollback

list(): return macros.json contents as dict (main.py renders it)
add(): os.startfile('macros.json') — opens in default editor
Expose: run(name, input_val), list(), add()

MODULE 3 — api/server.py
FastAPI app on localhost:8000. ALL endpoints async.
Auth: check header X-ATLAS-Token == settings.get('api_token') on every endpoint.
Return 401 if missing or wrong. Exception: GET /docs (auto-docs, no auth needed)
Rate limit: 60 req/min via slowapi on POST /command especially.

Endpoints:
POST /command
body: {"text": str, "source": "cli"|"voice"|"api"}
→ classifier.classify(text) or llm_engine.query(text, [])
→ executor.execute(action, params)
→ return: {"action": str, "result": str, "verified": bool, "latency_ms": int}

GET /history?n=20&q=
→ history.list_recent(n) or history.search(q) if q provided
→ return list of command dicts

GET /macros
→ macros.list() — return macros.json dict

POST /macros/run
body: {"name": str, "input": str}
→ macros.run(name, input)

GET /status
→ {"model": settings.get('model'), "voice_input": bool, "voice_output": bool,
"pin_set": bool(settings.get('pin_hash')), "session_memory": bool,
"uptime_s": int(time.time() - START_TIME)}

GET /dry-run
body or query: {"text": str}
→ run classifier/LLM only, return JSON intent WITHOUT calling executor

MODULE 4 — main.py (complete CLI wiring)
argparse for all flags. REPL when no args given.

All CLI flags:
atlas 'cmd' → single command → executor → print → exit
atlas → REPL loop, rich prompt "ATLAS > ", Ctrl+C exits cleanly
atlas --dry 'cmd' → classify/LLM → print dry-run block → exit (no execute)
atlas --history → history.list_recent(20) → render as rich Table
atlas --history search 'kw' → history.search(kw) → render as rich Table
atlas --rerun N → history.rerun(N) → print result
atlas --macro list → macros.list() → render as rich Table
atlas --macro run NAME → macros.run(NAME, '') → print each step result
atlas --macro run NAME input → macros.run(NAME, input)
atlas --macro add → macros.add() → opens editor
atlas --status → GET /status → rich Panel display
atlas --setup → PIN wizard + Ollama connection test
atlas --install-cli → add entry_points console_scripts in setup.py/pyproject.toml
atlas --help → rich Panel with all flags and examples

Dry-run output format (exactly this):
┌─ Dry Run ──────────────────────────────┐
│ ACTION: delete_file │
│ TARGET: C:/Users/Gaurav/notes.txt │
│ RISK: High │
│ GATE: PIN required │
│ → Not executed. Remove --dry to run. │
└────────────────────────────────────────┘

FastAPI start in main.py:
START_TIME = time.time()
thread = threading.Thread(
target=uvicorn.run, args=(app,),
kwargs={"host":"127.0.0.1","port":8000,"log_level":"error"},
daemon=True)
thread.start()

# poll until ready (max 10s):

for \_ in range(20):
try: requests.get("http://localhost:8000/status"); break
except: time.sleep(0.5)

REPL loop:
try: pyreadline3 for up-arrow history on Windows
Ctrl+C → print "Goodbye." → sys.exit(0) — no traceback
Wrap loop body in try/except Exception as e → print clean message

install new deps:
pip install slowapi requests pyreadline3

PHASE 3 END TESTS:

1. atlas --setup → PIN wizard, Ollama test, success message
2. atlas --install-cli → type 'atlas --status' from a different directory → works
3. atlas 'open notepad' → Notepad opens, rich green Panel shown
4. atlas --dry 'delete notes.txt' → dry-run Panel shown, no execution, no PIN
5. atlas --history → rich Table of past commands
6. atlas --history search 'notepad' → filtered results only
7. atlas --rerun 1 → re-executes the first command in history
8. atlas --macro run vol50 → volume set to 50
9. atlas --macro run search "python tutorial" → browser opens with search
10. curl http://localhost:8000/status -H "X-ATLAS-Token: [your_token]" → JSON response
11. curl http://localhost:8000/status → 401 Unauthorized (no token)
12. Ctrl+C in REPL → "Goodbye." prints, no Python traceback
