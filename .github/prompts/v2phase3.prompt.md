---
agent: agent
description: ATLAS v2 Phase 3 — Tauri + React HUD
---

Build Phase 3 of ATLAS v2. Read #file:../copilot-instructions.md.
The HUD talks to the existing FastAPI backend only. No new Python backend.
Do NOT modify any Python files except api/server.py (to add WebSocket broadcasts).

SETUP (run in terminal first):
cd atlas/
npm create tauri-app@latest hud -- --template react-ts
cd hud
npm install react-markdown prism-react-renderer lucide-react

Tauri window config (hud/src-tauri/tauri.conf.json):
"windows": [{"width":420,"height":720,"resizable":true,
"title":"ATLAS","alwaysOnTop":false}]
"systemTray": {"iconPath":"icons/atlas.png","iconAsTemplate":true}

HUD calls Python via: FastAPI REST (fetch) + WebSocket (ws://localhost:8000/ws)
No Tauri native commands needed in v2.

DESIGN TOKENS — Iron Man aesthetic:
--bg: #0A0E1A (deep navy)
--surface: #111827 (card backgrounds)
--accent-blue:#00D4FF (electric blue — primary accent)
--accent-gold:#FFB000 (amber/gold — speaking state, warnings)
--text-primary:#FFFFFF
--text-dim: #8899AA
--border: #1E293B
--success: #00FF88
--error: #FF4444
--radius: 4px (sharp, military/tech — not rounded)

COMPONENT 1 — App.tsx
WebSocket: connect to ws://localhost:8000/ws on mount
Auto-reconnect with exponential backoff (max 5s delay) on disconnect
State: messages[], currentStreamText, systemStatus, avatarState, isOnline
Layout: 3 columns — 60% ChatPanel, 20% AvatarPanel, 20% StatusPanel
If WebSocket fails to connect: show "ATLAS offline" banner with Start button

COMPONENT 2 — ChatPanel.tsx
Scrollable message list (user messages right-aligned, ATLAS left-aligned)
Each ATLAS message: react-markdown render (bold, code, lists)
Code blocks: Prism syntax highlighting, copy button on hover
Streaming bubble: appears immediately on {"type":"token"}, updates live
do NOT wait for {"type":"done"} to show the bubble
Auto-scroll to bottom on new content (useEffect + scrollIntoView)
Input box at bottom: placeholder "Type a command..." or "Listening..." when wake word active
On Enter: POST to http://localhost:8000/command with X-ATLAS-Token header
Show token from settings (read from a local config or env var for HUD)

COMPONENT 3 — AvatarPanel.tsx
Centred circle with CSS animations — 3 states:
idle: slow 2s pulse, dim blue ring (#00D4FF at 30% opacity)
listening: fast 0.5s pulse, bright blue, rotating outer ring
speaking: wave animation on 3 concentric rings, gold colour (#FFB000)
State machine driven by WebSocket messages:
{"type":"listening_start"} → switch to listening
{"type":"token"} → switch to speaking
{"type":"done"} → after 1s delay, return to idle
{"type":"killswitch"} → return to idle immediately
{"type":"error"} → brief red flash, return to idle
"ATLAS" text below avatar in monospace, dim colour

COMPONENT 4 — StatusPanel.tsx
Fetch GET /status every 30 seconds (with auth header)
Display: model name, uptime formatted as Xh Ym, memory ON/OFF, wake word ON/OFF
Mute toggle button: POST /command {"text":"mute","source":"api"}
Big STOP button (red): fires killswitch signal via POST /command {"text":"stop"}
Small gear icon → opens SettingsModal

COMPONENT 5 — DryRunModal.tsx
Appears on {"type":"dry_run","data":{action,target,risk,gate}} from WebSocket
Shows styled card:
ACTION [action value]
TARGET [target value]
RISK [coloured badge: green=low, yellow=medium, red=high]
GATE [gate description]
Proceed button: POST /command with execute:true flag
Cancel button: dismiss modal
Backdrop blur behind modal

COMPONENT 6 — HistoryDrawer.tsx
Slide-out from right side, triggered by clock icon in header
On open: fetch GET /history?n=20 with auth header
Render as list: timestamp + command text + success/fail dot
Rerun button on each: POST /command with the raw command text
Search input at top: onChange fetches GET /history?q={query}

Global hotkeys (Tauri globalShortcut):
Ctrl+Space → toggle window show/hide
Ctrl+Shift+K → send killswitch POST to /command

System tray:
Left click: show/hide window
Right-click menu: Show, Mute, Quit

Update api/server.py to broadcast these events (add to existing endpoints):
On /command receive: broadcast({"type":"user_message","data":text})
On action determined: broadcast({"type":"action","data":action_name})
On dry-run: broadcast({"type":"dry_run","data":{action,target,risk,gate}})
On success: broadcast({"type":"done","data":response_text})
On error: broadcast({"type":"error","data":str(e)})

Also add these to wake_word.py's \_on_wake_word():
broadcast({"type":"listening_start"}) at start of listening
broadcast({"type":"user_message","data":transcribed}) before executing

PHASE 3 END TESTS:

1. npm run tauri dev → HUD window opens, shows "ATLAS online" or "ATLAS offline"
2. Type 'atlas open notepad' in terminal → ChatPanel shows the command + result
3. Say "Hey ATLAS open chrome" → AvatarPanel: idle → listening (fast pulse) →
   speaking (gold wave) → idle
4. Type a long-answer question → tokens stream into ChatPanel bubble in real-time
5. Press Ctrl+Space → HUD hides → press again → reappears
6. Click STOP button → TTS stops, avatar returns to idle
7. Type 'atlas --dry delete notes.txt' → DryRunModal appears in HUD
8. Click History drawer → last 20 commands load → click Rerun on one → executes
9. Close HUD → atlas --status in terminal still works (HUD is additive, not required)
10. HUD memory: check Task Manager → ATLAS HUD process < 50 MB RAM
