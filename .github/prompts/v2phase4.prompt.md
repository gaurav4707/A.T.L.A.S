---
agent: agent
description: ATLAS v2 Phase 4 — Task Chains + Full Integration + Polish
---

Build Phase 4 of ATLAS v2. Read #file:../copilot-instructions.md.
Do NOT modify: executor.py, validator.py, security.py, verifier.py,
rollback.py, pc_control.py, memory.py, context_pruner.py, classifier.py.

MODULE 1 — chains.py (UPGRADE from macros.py — macros.json still loads)
chains.json format (create if missing):
{
"dev": {
"steps": [
{"name":"open_editor", "command":"open vscode ."},
{"name":"open_browser", "command":"open chrome localhost:3000"},
{"name":"open_terminal","command":"open terminal"}
],
"rollback_on_fail": true,
"pinned": false
},
"morning": {
"steps": [
{"name":"browser", "command":"open chrome"},
{"name":"volume", "command":"set volume 40"}
],
"rollback_on_fail": false,
"pinned": true
}
}

run_chain(name: str, input_val: str = "") → ChainResult:

1. Load from chains.json. If not found: try macros.json (backwards compat).
   If in macros.json: run as v1 macro, print legacy warning.
2. Resolve {input} substitution in all step commands
3. Print preview of ALL steps BEFORE step 1 executes:
   for each step: print " STEP N: [command] | RISK: [risk] | GATE: [gate]"
4. input("Run all N steps? (yes/no) > ") — require 'yes'
5. completed_steps = []
6. for each step:
   result = executor.execute(parsed_action, parsed_params)
   if result['success']:
   completed_steps.append((action, params))
   else:
   if rollback_on_fail:
   for (act, par) in reversed(completed_steps):
   rollback.log_step(act, str(par), "chain_rollback", False) # attempt reverse where possible (close what was opened etc.)
   print(f"Step {n} failed. Rolled back {len(completed_steps)} steps.")
   else:
   print(f"Step {n} failed. Stopping (no rollback configured).")
   return ChainResult(success=False, failed_step=n, rollback_done=rollback_on_fail)
7. return ChainResult(success=True, steps_completed=len(steps))

test_chain(name: str) → ChainTestReport:
Simulate: for each step, check if action is in executor.ACTION_MAP,
check if path is in allowed_paths, determine risk tier.
Return report dict: [{"step":n,"command":cmd,"action":action,"status":"PASS"|"BLOCKED"|"PIN_REQUIRED"}]
Does NOT execute anything — pure simulation.

CLI additions (update main.py):
atlas --chain list → list chains.json + macros.json merged
atlas --chain run dev → run_chain('dev')
atlas --chain run dev input → run_chain('dev', 'input')
atlas --chain test dev → test_chain('dev') → render as rich Table
atlas --chain add → os.startfile('chains.json')

FastAPI additions (update api/server.py):
POST /chains/run body: {"name":str,"input":str}
GET /chains returns merged chains + macros list
POST /chains/test body: {"name":str} → returns ChainTestReport

HUD: when a chain runs, broadcast each step completion:
{"type":"chain_step","data":{"step":n,"name":step_name,"status":"running"|"done"|"failed"}}
ChatPanel shows step progress inline.

MODULE 2 — HUD polish additions (update React components)

Shortcuts bar (add to App.tsx bottom):
On mount: fetch GET /chains → filter where pinned: true
Render as horizontal chip row at bottom of HUD
Click chip: POST /chains/run → chain executes with full security pipeline
If PIN required mid-chain: render PIN keypad modal in HUD

Settings modal (update StatusPanel gear icon):
Fetch GET /status for current values
Toggle switches for: voice_output, wake_word_enabled, session_memory
Each toggle: POST to new PUT /settings endpoint
Add to api/server.py:
PUT /settings body: {"key":str,"value":Any}
ALLOWED_SETTINGS = ['voice_output','wake_word_enabled','session_memory','model','voice_speed']
Reject anything not in ALLOWED_SETTINGS (never allow pin_hash or api_token via API)
Write via settings.save()

Weekly KPI digest:
On HUD open: fetch GET /digest (add this endpoint)
GET /digest returns: {"week":str,"total":int,"success_rate":float,"avg_ms":int}
(pull from history.py — add weekly_digest() that queries last 7 days)
If last_shown is > 7 days ago (store in localStorage): show digest card in HUD
Also spoken if voice_output enabled: broadcast {"type":"digest","data":summary_text}

MODULE 3 — Startup sequence update (update main.py)
New order (replaces v1 startup):

1.  settings.load() → malformed: clean message → exit
2.  memory.review_and_expire() → silent
3.  rollback.auto_purge() → silent
4.  Check Ollama (localhost:11434) → not running: clean message → exit
5.  if not pin_hash: security.setup_pin()
6.  Start FastAPI background thread
7.  context_pruner.start_pruner()
8.  killswitch.register_hotkey()
9.  if wake_word_enabled and porcupine_key: wake_word.start_wake_word_listener()
    else: print push-to-talk mode message
10. Poll FastAPI ready (max 10s)
11. Print v2 startup banner:
    "ATLAS v2 | Model: [model] | Memory: ChromaDB | Wake Word: [on/off] | HUD: port 8000"

Updated atlas --status output:
Model: mistral
Memory: ChromaDB ([N] facts, [M] summaries)
Wake Word: active / push-to-talk mode
Voice Output: on / off
Uptime: Xh Ym Zs
Chains: N defined, M pinned
API: localhost:8000
HUD clients: N connected

PHASE 4 END TESTS (v2 daily-driver acceptance):

1. atlas --chain run dev → preview of 3 steps shown → confirm → all execute
2. Simulate step 2 failure (rename a file that doesn't exist) →
   rollback reverses step 1 → operations.log shows "chain_rollback" entries
3. atlas --chain test morning → table shows: PASS, PASS (no high-risk steps)
4. Pinned chain chip appears in HUD shortcuts bar → click → executes
5. HUD settings gear → toggle voice_output → setting saved → persists on restart
6. Monday (or set clock forward): KPI digest card appears in HUD, spoken aloud
7. Full voice-to-chain: "Hey ATLAS run dev chain" → wake word → chain preview
   → confirm → all 3 steps execute, each shown in ChatPanel as it completes
8. atlas --status → shows ChromaDB counts, chain counts, HUD clients
9. Ctrl+Shift+K mid-chain → chain stops cleanly, no partial state left

REGRESSION CHECKS — all v1 features must still work:
☑ atlas 'open chrome' → works
☑ atlas --history → works  
☑ atlas --dry 'delete notes' → works
☑ atlas --macro run vol50 → still loads from macros.json (backwards compat)
☑ PIN gate on high-risk → works
☑ E-05 unsaved document block → works
☑ atlas --status → works (now shows v2 extras too)
