# session_prompts.md
# ATLAS Phase: HUD Complete Rewrite — Minimal Modern UI + Avatar Bug Fix
# Drop this file in your atlas/ root, then run: vibe start

---

## Plan

### Prompt 1 — Generate plan.md
# [PLAN] [T1] [MODEL: gemini-2.5-pro] # PARALLEL: true

```prompt
You are my senior architect. I am fixing and rebuilding the ATLAS Tauri + React HUD.

CURRENT BUGS (fix these):
1. AvatarPanel wave animation goes insane on listening_start — multiple rAF loops compound, never cancelled
2. Avatar never returns to idle — no done/killswitch handler resets state
3. listening_start fires from Vosk partial results repeatedly, each re-triggers animation from scratch
4. Start button non-functional — onClick not wired to WebSocket or FastAPI POST /command
5. Text commands typed in input not sent — form submit not calling POST /command
6. HUD state stuck on LISTENING after command completes

DESIGN DIRECTION — minimal, modern, no sci-fi noise:
- Font: IBM Plex Mono (labels/values) + IBM Plex Sans (body)
- Colors: #08090d bg, #0f1117 surface, #38bdf8 blue accent, #f59e0b gold accent
- Avatar: single clean sine wave (NOT Lissajous), CSS keyframe only, NO canvas, NO rAF
  - idle: slow low-amplitude wave, blue at 30% opacity
  - listening: faster mid-amplitude wave, bright blue
  - processing: flat line with traveling dot
  - speaking: taller faster wave, gold
- Layout: left=ChatPanel (flex-grow), center=AvatarPanel, right=DiagnosticsPanel
- No all-caps monospace noise labels everywhere

Output plan.md with:
1. File tree (hud/src/ only — never touch any Python file)
2. Component interfaces (props, state shape for each component)
3. WebSocket event contract (listening_start, processing, token, done, killswitch, user_command, status)
4. CSS animation keyframes for all 4 avatar states (idle/listening/processing/speaking)
5. State machine: AtlasState transitions with guard — scheduleIdle() cancels previous timer before setting new one
6. End-to-end test cases

Rules: No canvas. No rAF. CSS animations only for avatar. IBM Plex fonts via Google Fonts. Tailwind NOT used — plain CSS variables in index.css.
```

Tip: Run as: gemini "[prompt]" > plan.md — commit before touching any code.

---

### Prompt 2 — Parallel review of current broken HUD
# [PLAN] [T1] [MODEL: gemini-2.5-pro] # PARALLEL: true

```prompt
Read all files in hud/src/. You are a senior reviewer. Do NOT write code.

Produce review.md with exactly these sections:
AVATAR_BUG:   Why the wave animation compounds — identify the exact rAF loop issue and which event re-triggers it
STATE_BUG:    Why atlas state never returns to idle — which event handler is missing
INPUT_BUG:    Why typed commands and Start button do nothing — trace the missing wiring
VRAM:         Any risk of loading 2 GPU models simultaneously from HUD actions
SECURITY:     Any raw shell calls or unvalidated inputs from HUD
MEMORY:       Any 4th memory store introduced by HUD code

One line per issue. Markdown only.
```

Tip: Run as: gemini "[prompt]" > review.md & — runs in background while you read plan.md.

---

## Build

### Prompt 3 — index.css design tokens
# [BUILD] [T2] [MODEL: qwen2.5-coder:14b]

```prompt
Read plan.md first.

Create hud/src/index.css. This is the complete design token file for the ATLAS HUD.

Requirements:
- Google Fonts import: IBM Plex Mono (weights 300,400) + IBM Plex Sans (weights 300,400,500)
- CSS :root variables:
    --bg: #08090d
    --surface: #0f1117
    --surface-2: #161820
    --border: rgba(255,255,255,0.07)
    --border-2: rgba(255,255,255,0.12)
    --blue: #38bdf8
    --blue-dim: rgba(56,189,248,0.12)
    --gold: #f59e0b
    --gold-dim: rgba(245,158,11,0.12)
    --red: #ef4444
    --green: #22c55e
    --text: #e2e8f0
    --text-dim: #64748b
    --text-muted: #334155
    --mono: 'IBM Plex Mono', monospace
    --sans: 'IBM Plex Sans', sans-serif
    --radius: 6px
    --radius-lg: 10px
- CSS keyframe animations for avatar wave — 4 states:
    @keyframes wave-idle: translateY sine 0px to 6px, 3s ease-in-out infinite alternate, opacity 0.3
    @keyframes wave-listening: translateY sine 0px to 18px, 0.6s ease-in-out infinite alternate, opacity 1.0
    @keyframes wave-processing: no translateY, traveling dot via background-position, 1.2s linear infinite
    @keyframes wave-speaking: translateY sine 0px to 28px, 0.4s ease-in-out infinite alternate, opacity 1.0, color var(--gold)
- html/body/root: height 100%, overflow hidden, bg var(--bg), color var(--text), font var(--sans)
- Thin custom scrollbar: 4px, var(--border-2) thumb

No Tailwind. No external UI library. Plain CSS only.
When done: print DONE: hud/src/index.css
```

Tip: This file must exist before any component file is written.

### Prompt 4 — ATLAS regression check after index.css
# [TEST] [T2] [MODEL: qwen2.5-coder:14b]

```prompt
Run: atlas --status
Run: atlas 'open notepad'
Both must return success before proceeding.
Report result. If either fails, stop and report the error.
```

Tip: index.css change cannot break backend — this confirms baseline is still clean.

---

### Prompt 5 — App.tsx root component with correct state machine
# [BUILD] [T2] [MODEL: qwen2.5-coder:14b]

```prompt
Read plan.md first.

Replace hud/src/App.tsx entirely. This is the root component.

Requirements:
- AtlasState type: 'idle' | 'listening' | 'processing' | 'speaking'
- Message type: { id: string, role: 'user'|'atlas', content: string, timestamp: number }
- StatusData type: { model: string, vram_tier: string, uptime_seconds: number, wake_word: string, context_tokens: number, command_count: number }
- WebSocket: ws://localhost:8000/ws/stream — connect on mount, reconnect after 3s on close
- WebSocket event handlers (THIS IS THE CRITICAL BUG FIX):
    'listening_start' → clearIdleTimer(), setAtlasState('listening')
    'processing'      → clearIdleTimer(), setAtlasState('processing')
    'token'           → clearIdleTimer(), setAtlasState('speaking'), append token to streaming message
    'done'            → streamingIdRef.current = null, scheduleIdle(800)
    'killswitch'      → clearIdleTimer(), streamingIdRef.current = null, setAtlasState('idle') immediately
    'user_command'    → append user message to messages[]
    'status'          → setStatus(data)
- scheduleIdle(delayMs): ALWAYS calls clearIdleTimer() first, then setTimeout → setAtlasState('idle')
- clearIdleTimer(): clears idleTimerRef.current if set
- idleTimerRef: useRef<ReturnType<typeof setTimeout> | null>(null)
- streamingIdRef: useRef<string | null>(null) — tracks which message is currently streaming
- Token streaming: if streamingIdRef.current is null, create new atlas message, set streamingIdRef; then append token to that message id only
- sendCommand(text): POST to http://localhost:8000/command with { command: text }, also appends user message locally
- fetchStatus(): GET http://localhost:8000/status every 10s
- Layout: 3-column grid — ChatPanel (flex-grow) | AvatarPanel (400px fixed) | DiagnosticsPanel (260px fixed)
- Pass atlasState, messages, sendCommand to ChatPanel
- Pass atlasState to AvatarPanel
- Pass status, wsConnected to DiagnosticsPanel

Do NOT use any canvas or requestAnimationFrame. Import child components from ./components/.
When done: print DONE: hud/src/App.tsx
```

Tip: The scheduleIdle/clearIdleTimer pattern is the entire fix for the avatar never returning to idle.

### Prompt 6 — ATLAS regression after App.tsx
# [TEST] [T2] [MODEL: qwen2.5-coder:14b]

```prompt
Run: atlas --status
Run: atlas 'open notepad'
Report result. Stop if either fails.
```

---

### Prompt 7 — AvatarPanel.tsx (CSS-only wave, no canvas, no rAF)
# [BUILD] [T2] [MODEL: qwen2.5-coder:14b]

```prompt
Read plan.md first.

Create hud/src/components/AvatarPanel.tsx.

Props: { state: 'idle' | 'listening' | 'processing' | 'speaking' }

THIS IS THE ROOT CAUSE FIX — the old component used requestAnimationFrame in a loop that was never cancelled. This component uses ZERO canvas, ZERO requestAnimationFrame, ZERO useEffect for animation. CSS keyframes only.

Implementation:
- Outer container: full width/height of its grid cell, display flex, align+justify center, background var(--surface), border-radius var(--radius-lg), border 1px solid var(--border)
- Wave: render as an SVG <path> using a pre-computed sine wave polyline (static SVG path — not animated via JS)
  - SVG viewBox="0 0 400 120", width 100%, height 120px, overflow visible
  - Path: M 0,60 C 50,60 50,30 100,30 C 150,30 150,90 200,90 C 250,90 250,30 300,30 C 350,30 350,60 400,60
    (this is a static smooth sine — do NOT compute it in JS)
  - Apply CSS class based on state prop: wave-idle | wave-listening | wave-processing | wave-speaking
  - Each class applies: stroke color, stroke-width, animation name from index.css keyframes
  - wave-idle:      stroke var(--blue), opacity 0.35, stroke-width 1.5, animation wave-idle 3s ease-in-out infinite alternate
  - wave-listening: stroke var(--blue), opacity 1.0,  stroke-width 2,   animation wave-listening 0.5s ease-in-out infinite alternate, filter drop-shadow(0 0 6px var(--blue))
  - wave-processing: stroke var(--text-dim), opacity 0.5, stroke-width 1, animation wave-processing 1.2s linear infinite, stroke-dasharray 4 8
  - wave-speaking:  stroke var(--gold), opacity 1.0,  stroke-width 2.5, animation wave-speaking 0.35s ease-in-out infinite alternate, filter drop-shadow(0 0 8px var(--gold))
- State label: bottom-right corner, font var(--mono), font-size 11px, color var(--text-dim), letter-spacing 0.08em
  - idle → 'STANDBY', listening → 'LISTENING', processing → 'PROCESSING', speaking → 'RESPONDING'
- Status dot: 8px circle bottom-left
  - idle: var(--text-muted), listening: var(--blue) with pulse animation, speaking: var(--gold)
- NO canvas element. NO requestAnimationFrame. NO useEffect with animation logic.

When done: print DONE: hud/src/components/AvatarPanel.tsx
```

Tip: The SVG path is static — CSS transform on the path element handles the amplitude change per state, not JS.

### Prompt 8 — ATLAS regression after AvatarPanel
# [TEST] [T2] [MODEL: qwen2.5-coder:14b]

```prompt
Run: atlas --status
Run: atlas 'open notepad'
Then run: npm run tauri dev in hud/ — confirm HUD opens without console errors.
Report result. Stop if any fails.
```

---

### Prompt 9 — ChatPanel.tsx with working command input
# [BUILD] [T2] [MODEL: qwen2.5-coder:14b]

```prompt
Read plan.md first.

Create hud/src/components/ChatPanel.tsx.

Props:
  messages: Message[]
  atlasState: AtlasState
  onSendCommand: (text: string) => void

Requirements:
- Outer: flex column, full height, background var(--surface), border-radius var(--radius-lg), border 1px solid var(--border)
- Messages area: flex-grow, overflow-y auto, padding 16px, gap 12px between messages
  - User message: right-aligned, background var(--surface-2), border-left 2px solid var(--blue), padding 10px 14px, border-radius var(--radius), font-size 13px, max-width 85%
  - Atlas message: left-aligned, transparent bg, border-left 2px solid var(--gold), padding 10px 14px, border-radius var(--radius), font-size 13px, max-width 90%, white-space pre-wrap
  - Empty state: centered text "AWAITING INPUT", color var(--text-muted), font var(--mono), font-size 11px, letter-spacing 0.1em
- Auto-scroll: useRef on messages container, useEffect scrolls to bottom when messages change
- Input row: bottom, padding 12px, border-top 1px solid var(--border), display flex, gap 8px
  - Text input: flex-grow, background var(--surface-2), border 1px solid var(--border-2), border-radius var(--radius), padding 10px 14px, color var(--text), font var(--sans), font-size 13px, outline none
  - Input placeholder: "COMMAND /" in color var(--text-muted), font var(--mono)
  - Focus state: border-color var(--blue)
  - Submit: on Enter keydown OR send button click → call onSendCommand(inputValue), clear input
  - Disabled when atlasState is 'processing' or 'speaking'
  - Send button: background var(--blue-dim), border 1px solid var(--blue), color var(--blue), border-radius var(--radius), padding 10px 16px, font var(--mono), font-size 11px, cursor pointer, text "SEND"
  - Button hover: background var(--blue), color var(--bg)
- State indicator dot left of input: 8px circle, color matches atlasState (idle=muted, listening=blue, processing=gold pulse, speaking=gold)
- Show "LISTENING..." placeholder text when atlasState is 'listening'
- Do NOT use <form> element. Use onKeyDown on input and onClick on button.

When done: print DONE: hud/src/components/ChatPanel.tsx
```

Tip: The critical fix here is onKeyDown (not onSubmit) and wiring onSendCommand directly — no form, no preventDefault needed.

### Prompt 10 — ATLAS regression after ChatPanel
# [TEST] [T2] [MODEL: qwen2.5-coder:14b]

```prompt
Run: atlas --status
Run: atlas 'open notepad'
Run: npm run tauri dev in hud/
Type a test command in the input and press Enter — confirm it appears as a user message.
Report result.
```

---

### Prompt 11 — DiagnosticsPanel.tsx
# [BUILD] [T2] [MODEL: qwen2.5-coder:14b]

```prompt
Read plan.md first.

Create hud/src/components/DiagnosticsPanel.tsx.

Props:
  status: StatusData | null
  wsConnected: boolean

Requirements:
- Outer: full height, display flex, flex-direction column, gap 12px, padding 0 (no outer border — it's a sidebar)
- Each stat group: background var(--surface), border 1px solid var(--border), border-radius var(--radius-lg), padding 14px 16px
- Label style: font var(--mono), font-size 10px, color var(--text-dim), letter-spacing 0.1em, margin-bottom 4px
- Value style: font var(--mono), font-size 13px, color var(--text), font-weight 400
- Stat rows (label above, value below):
    LINK:     wsConnected ? 'ESTABLISHED' (color var(--green)) : 'OFFLINE' (color var(--red))
    CORE:     status?.model ?? 'NULL'
    UPTIME:   format status?.uptime_seconds as 'Xh Ym' — if null show '—'
    CONTEXT:  status?.context_tokens ?? '—' + ' / 2200 tok'
    WAKE:     status?.wake_word ?? 'DISARMED'
    COMMANDS: status?.command_count ?? '0'
- Context token bar: thin 4px height bar below context value, width = (tokens/2200)*100%, background var(--blue), max-width 100%, border-radius 2px, background of track var(--surface-2)
- Two action buttons at bottom:
    ENGAGE MUTE: background transparent, border 1px solid var(--border-2), color var(--text-dim), font var(--mono), font-size 10px, letter-spacing 0.08em, padding 10px, border-radius var(--radius), onClick → POST http://localhost:8000/command with { command: 'mute toggle' }
    EMERGENCY HALT: background var(--red-dim), border 1px solid var(--red), color var(--red), same font/size, onClick → POST http://localhost:8000/command with { command: 'atlas stop' }
    EMERGENCY HALT hover: background var(--red), color var(--bg)
- If status is null: show skeleton placeholders (--text-muted dashes) — do not crash

When done: print DONE: hud/src/components/DiagnosticsPanel.tsx
```

Tip: uptime formatter — Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm'

### Prompt 12 — Full regression + visual check
# [TEST] [T2] [MODEL: qwen2.5-coder:14b]

```prompt
Run full ATLAS v2 regression suite:
1. atlas --status               (all fields populated)
2. atlas 'open notepad'        (Notepad opens)
3. atlas --dry 'delete x'     (preview shown, no execution)
4. npm run tauri dev in hud/   (HUD opens, no console errors)
5. Say wake word 'Hey ATLAS'   (avatar transitions to listening, NOT going insane)
6. Stay silent 2s after wake   (avatar returns to idle — this was broken before)
7. Type command in input + Enter (message appears in chat)
8. atlas stop                  (avatar returns to idle immediately)

Report each test: PASS or FAIL with reason.
Stop if any test fails — fix before continuing.
```

---

## Debug

### Prompt 13 — Avatar still going insane after build
# [DEBUG] [T2] [MODEL: deepseek-r1:14b]

```prompt
Here is a failing ATLAS test and full error trace:
[PASTE FULL ERROR TRACE]

The AvatarPanel wave animation is still misbehaving. Reason through root cause step by step.

Check in this order:
1. Is there ANY requestAnimationFrame call remaining in AvatarPanel.tsx or App.tsx? Search for 'requestAnimationFrame' and 'rAF' in hud/src/
2. Is the WebSocket onmessage handler calling handleWSMessage via a stale closure that doesn't have the latest clearIdleTimer ref?
3. Is listening_start being fired multiple times from Vosk partial results? Check wake_word.py — does _contains_wake() fire on every partial frame?
4. Is idleTimerRef being shared correctly across re-renders? Confirm it's useRef not useState
5. Is the CSS animation class being toggled correctly — confirm the wave element has exactly ONE class applied at a time based on state prop

After root cause: write the minimal fix only. No refactoring.
```

Tip: The most common remaining issue is stale closure on wsRef.current.onmessage — wrap handleWSMessage in useCallback with correct deps.

### Prompt 14 — Commands not reaching backend
# [DEBUG] [T2] [MODEL: deepseek-r1:14b]

```prompt
Here is a failing ATLAS test and full error trace:
[PASTE FULL ERROR TRACE]

Text commands typed in HUD input are not reaching the backend. Reason through step by step.

Check in this order:
1. Is onSendCommand prop actually passed from App.tsx to ChatPanel.tsx? Check the JSX prop name matches exactly
2. Is the POST to http://localhost:8000/command using correct body shape? Backend expects { command: string } — check Content-Type header is 'application/json'
3. Is CORS blocking the request? FastAPI must have localhost:1420 (Tauri default port) in allowed origins
4. Is the input's onKeyDown checking e.key === 'Enter' (capital E) not 'enter'?
5. Is the button onClick calling the prop or a local function that doesn't call the prop?

After root cause: write the minimal fix only.
```

Tip: Add console.log in onSendCommand to confirm it fires before suspecting the network.

---

## Ship

### Prompt 15 — Final review before commit
# [SHIP] [T1] [MODEL: gemini-2.5-pro] # PARALLEL: true

```prompt
Read hud/src/ entirely. Review the rebuilt ATLAS HUD against plan.md.

Output final_review.md with:
AVATAR:    Is wave animation CSS-only? Any rAF or canvas remaining?
STATE:     Does scheduleIdle always call clearIdleTimer first? Is killswitch handler correct?
INPUT:     Is onSendCommand wired end-to-end? Does Enter key work?
VRAM:      Any HUD action that could load 2 GPU models simultaneously?
SECURITY:  Any unvalidated input sent directly to backend without sanitisation?
CORS:      Is FastAPI CORS config updated to allow Tauri's localhost:1420?
PERF:      Any unnecessary re-renders? Heavy useEffect missing deps?

One line per issue. Markdown only.
```

Tip: Run as: gemini "[prompt]" > final_review.md — check before running vibe commit.

### Prompt 16 — Ship
# [SHIP] [T3] [MODEL: copilot-cli]

```prompt
vibe commit

Conventional commit message format:
fix(hud): rebuild AvatarPanel with CSS-only wave animation, fix idle state never resetting

Regression checks before commit:
- atlas --status ✓
- atlas 'open notepad' ✓
- HUD opens without console errors ✓
- Wake word triggers listening state without animation going insane ✓
- Silence after wake returns avatar to idle ✓
- Typed commands reach backend ✓
- EMERGENCY HALT button fires killswitch ✓

If all green: git tag hud-rewrite-complete && git push origin main --tags
Update copilot-instructions.md: set '## Current Build Phase' to 'Phase 3 complete — HUD rebuilt'
```

Tip: Only run vibe commit after final_review.md has zero critical issues.

---

## ATLAS Rules Enforced In This Session

- ONE GPU model at a time — HUD never loads any model, zero VRAM impact
- All commands via POST /command → executor.ACTION_MAP — no raw shell from HUD
- 3 memory systems only — HUD reads status, never writes to ChromaDB directly
- Test atlas --status AND atlas 'open notepad' after every file change (prompts 4, 6, 8, 10, 12)
- Vosk model at: models/vosk-model-small-en-us-0.15/ — unchanged

## Files Changed This Session

| File | Action |
|------|--------|
| hud/src/index.css | REPLACE — design tokens + avatar CSS keyframes |
| hud/src/App.tsx | REPLACE — fixed state machine, scheduleIdle/clearIdleTimer |
| hud/src/components/AvatarPanel.tsx | REPLACE — CSS-only SVG wave, no canvas, no rAF |
| hud/src/components/ChatPanel.tsx | REPLACE — working input, onKeyDown, onSendCommand wired |
| hud/src/components/DiagnosticsPanel.tsx | REPLACE — clean diagnostics, HALT button |

## Files Never Touched

executor.py, security.py, memory.py, wake_word.py, main.py, api/server.py, api/ws_manager.py
— If Copilot tries to edit any of these, reject the change immediately.
