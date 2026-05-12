---
agent: agent
description: Build ATLAS v1 Phase 2 — Safety + PC Control
---

Build Phase 2 of ATLAS v1. Read #file:../copilot-instructions.md first.
Phase 1 is already working. Do not modify classifier.py, llm_engine.py, settings.py.

MODULE 1 — validator.py
Risk tier table (hardcoded, these exact tiers):
low: open_app, close_app, web_search, open_url, get_time,
clipboard_read, clipboard_write, set_volume, mute_volume, sleep_pc, screenshot
medium: rename_file, create_folder, create_file, move_file
high: delete_file, run_script, shutdown_pc, restart_pc
critical: blocked by default — no v1 actions reach this tier

E-05 check (unsaved document guard) — run before ANY file operation:
Check if VS Code is running (psutil) AND target file is in open docs
Check if Notepad++ is running AND target file is open
If unsaved changes detected: return ok=False, reason=
"That file is open with unsaved changes in [editor]. Save or close it first."

E-02 check (cross-volume guard) — run before file move or delete:
Compare drive letter of source vs drive letter of .atlas_trash/
If different: return ok=False, reason=
"Cross-volume operation. ATLAS cannot guarantee rollback. Confirm manually."

Path checks:
blocked_paths wins over allowed_paths — check blocked first
Use os.path.abspath() to normalise before comparing — never raw string compare

Return type: ValidationResult dataclass with ok: bool, risk: str, reason: str
Unknown action (not in risk table): ok=False, risk="", reason="Unknown action"
Expose: validate(action: str, params: dict) → ValidationResult

MODULE 2 — security.py
Three-ring model:
Ring 1: validator (already done before reaching security)
Ring 2: confirmation gate:
low → return True immediately, no prompt
medium → print "Confirm: [action description]? (yes/no) > "
require exactly 'yes' (case-insensitive). Anything else = False
high → PIN gate (see below)
critical → print "This action is blocked by default." return False
Ring 3: executor dispatch map (next module)

PIN system:
setup_pin(): prompt user to enter 4-digit PIN twice, confirm match,
bcrypt.hashpw, save to config.json pin_hash
verify_pin(entered: str) → bool: bcrypt.checkpw against stored hash
request_confirmation(): for high risk:
print "Action: [action_name] | Target: [target_value]" first
then getpass.getpass("Enter PIN: ")
3 wrong attempts → time.sleep(60) with countdown printed every 10s
attempt counter is module-level (persists within session)
NEVER use input() for PIN — always getpass.getpass()
Expose: setup_pin(), request_confirmation(risk: str, description: str) → bool

MODULE 3 — executor.py (MOST CRITICAL FILE)
Import all functions from pc_control.py (built below).

ACTION_MAP = {
"open_app": open_app, "close_app": close_app, "web_search": web_search,
"open_url": open_url, "set_volume": set_volume, "mute_volume": mute_volume,
"sleep_pc": sleep_pc, "shutdown_pc": shutdown_pc, "restart_pc": restart_pc,
"create_file": create_file, "rename_file": rename_file,
"move_file": move_file, "delete_file": delete_file,
"clipboard_read": clipboard_read, "clipboard_write": clipboard_write,
"get_time": get_time, "run_macro": run_macro_action,
}

execute(action: str, params: dict) → dict:
Step 1: if action not in ACTION_MAP → return {"success":False,"message":"Unknown action"}
Step 2: validator.validate(action, params) → if not ok: return error
Step 3: security.request_confirmation(risk, description) → if False: return cancelled
Step 4: call ACTION_MAP[action](**params)
Step 5: verifier.verify(action, params, result) → if not ok: log uncertain
Step 6: rollback.log_step(action, str(params), result, verified)
Step 7: return result

NEVER EVER: os.system(llm_text), eval(), exec(), subprocess(llm_text)
ACTION_MAP is the only path to execution. No exceptions.

MODULE 4 — verifier.py
verify(action, params, result) → VerifyResult(ok: bool, message: str)
Per-action checks:
open_app → any(p.name().lower().startswith(app_name) for p in psutil.process_iter())
close_app → same check, expect False
create_file → os.path.exists(params['path'])
delete_file → not os.path.exists(original) AND atlas_trash file exists
rename_file → os.path.exists(params['new']) and not os.path.exists(params['old'])
move_file → os.path.exists(params['dst']) and not os.path.exists(params['src'])
web_search / open_url → 'chrome' or 'msedge' in [p.name() for p in psutil.process_iter()]
shutdown_pc / restart_pc → return VerifyResult(ok=True, message="Cannot verify power action")
clipboard_write → pyperclip.paste() == params['text']
All others → return VerifyResult(ok=True, message="No verification available")
Wrap ALL checks in try/except — never raise from verify()

MODULE 5 — rollback.py
soft*delete(source_path: str) → str:
trash dir = same drive as source + "/.atlas_trash"
if different drive: raise ValueError("Cross-volume soft delete not supported")
create trash dir if not exists
new_name = filename + "*" + datetime.now().strftime('%Y%m%d\_%H%M%S')
shutil.move(source, trash/new_name) → return new path

log_step(action, target, result, verified):
append to operations.log: "TIMESTAMP | ACTION | TARGET | RESULT | VERIFIED\n"
open in mode 'a' — never 'w'

auto_purge():
for each file in .atlas_trash/: if age > trash_retention_days: os.remove()
use os.path.getmtime() for age check (not creation time)

MODULE 6 — pc_control.py
Implement every function in ACTION_MAP. Each returns dict(success=bool, message=str).
All wrap in try/except — never raise.

App dispatch:
Chrome/Edge: subprocess.Popen(['start','chrome',url]) or Playwright for tab control
Explorer: subprocess.Popen(['explorer', folder_path])
Terminal: subprocess.Popen(['wt']) or subprocess.Popen(['cmd'])
VS Code: subprocess.Popen(['code', file_or_folder])
Notepad++: subprocess.Popen(['notepad++', file_path])
VLC: subprocess.Popen(['vlc', file_path])
Others: return dict(success=False, message="I don't support [app] yet. I can open it, or you can add it.")

System controls:
set_volume(level): use pycaw or ctypes to set 0-100
mute_volume(): toggle mute via ctypes
shutdown_pc(): subprocess.run(['shutdown','/s','/t','5'])
restart_pc(): subprocess.run(['shutdown','/r','/t','5'])
sleep_pc(): subprocess.run(['rundll32.exe','powrprof.dll,SetSuspendState','0','1','0'])

delete_file(path): calls rollback.soft_delete(path) — NEVER os.remove()
rename_file(old, new): os.rename(old, new)
clipboard read/write: pyperclip
get_time(): return dict(success=True, message=datetime.now().strftime('%H:%M:%S'))

SUBPROCESS SAFETY: always use list args, never shell=True with user input
WRONG: subprocess.run(f"code {file_path}", shell=True)
RIGHT: subprocess.run(['code', file_path])

Update main.py to use executor.execute() instead of printing raw JSON.

Install new deps:
pip install pywinauto playwright psutil pyperclip pycaw
playwright install chromium

PHASE 2 END TESTS:

1. atlas "open notepad" → Notepad opens → verify() confirms notepad.exe in process list
2. atlas "delete test_safe.txt" (create this file first) → PIN prompt → correct PIN →
   file moves to .atlas_trash/ → verify() passes → original gone
3. Create a file, open it in VS Code, leave unsaved → atlas "delete [that file]" →
   E-05 blocks it, prints clear message, no PIN prompt
4. atlas "shutdown" → PIN gate fires → type wrong PIN 3 times → 60s lockout visible
5. atlas "delete C:/Windows/system32/test.txt" → blocked_paths rejects before PIN
6. Check operations.log was created and has entries
7. atlas "open spotify" → clean decline message (unsupported app)
