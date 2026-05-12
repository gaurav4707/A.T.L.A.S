# ATLAS Bug Report & Fix Summary

## Files Changed
- `voice.py` — 4 bugs fixed
- `wake_word.py` — 2 bugs fixed
- `settings.py` — 1 bug fixed
- `macros.py` — 1 bug fixed

---

## Bug 1 — `voice.py`: Missing `import time` and `import logging`

**Location:** Top of `voice.py`  
**Impact:** Critical. `_ptt_capture_loop`'s error-retry path calls `time.sleep()` and `logging.warning()` — without these imports Python raises `NameError` the first time PTT hits a stream error, crashing the PTT thread silently.  
**Fix:** Added `import time` and `import logging` to the module imports.

---

## Bug 2 — `voice.py`: Wrong keyboard API in `stop_ptt_listener()`

**Location:** `stop_ptt_listener()` in `voice.py`  
**Symptom:** PTT key hooks stay registered after `stop_ptt_listener()` is called. The `_on_ptt_press` / `_on_ptt_release` callbacks keep firing even after stop, causing phantom recordings.

**Root cause:**
```python
# WRONG — remove_hotkey() is for hotkeys added with keyboard.add_hotkey()
keyboard.remove_hotkey(_ptt_press_listener)

# CORRECT — on_press_key() returns a hook; unhook() removes it
keyboard.unhook(_ptt_press_listener)
```

`keyboard.on_press_key()` and `keyboard.on_release_key()` return hook handles. To remove them you must call `keyboard.unhook(handle)`, not `keyboard.remove_hotkey()`. The wrong API call silently does nothing, so the hooks remain active forever.

**Fix:** Replaced `keyboard.remove_hotkey(...)` with `keyboard.unhook(...)` in `stop_ptt_listener()`.

---

## Bug 3 — `voice.py`: PTT dies permanently on first stream error

**Location:** `_ptt_capture_loop()` exception handler  
**Symptom:** PTT works for a session, then stops working silently. Typically happens if another app briefly takes the mic, Windows audio stack resets, or PortAudio hiccups.

**Root cause:**
```python
except Exception as exc:
    # After device fallback exhausted:
    print(f"[red]PTT disabled: {message}[/red]")
    return  # ← Exits the while loop forever. PTT is dead for this session.
```

Any transient audio error kills PTT permanently for the rest of the session.

**Fix:** Replaced `return` with `time.sleep(2.0); continue` so the loop retries after a brief pause. PTT recovers automatically from transient errors.

---

## Bug 4 — `voice.py`: Whisper parameters cause empty transcription on short commands

**Location:** `transcribe_from_array()` in `voice.py`  
**Symptom:** Whisper returns empty string for short commands like "open notepad" or "what time is it". Wake word transcription and PTT both appear to hear nothing.

**Root cause:**  
```python
no_speech_threshold=0.5,  # Whisper's default is 0.6 — 0.5 is too aggressive
logprob_threshold=-1.0,   # Default, but short commands have lower log-prob
                          # especially with background noise → empty result
```

Whisper returns an empty string when either:
- The no-speech probability exceeds `no_speech_threshold`, OR  
- The average log probability of tokens falls below `logprob_threshold`

Short spoken commands (1–4 words) have naturally lower log-probability than long sentences. With the threshold at -1.0, many valid short commands were silently discarded.

Also added a minimum audio padding guard: Whisper needs at least ~0.5s of audio to work correctly. Very short captures (< 0.5s) were passed raw and caused unreliable results.

**Fix:**
```python
no_speech_threshold=0.6,   # Whisper's own default — less aggressive
logprob_threshold=-2.0,    # More permissive for short commands
# Plus: pad audio to minimum 0.5s before transcription
```

---

## Bug 5 — `wake_word.py`: `_wake_phrase()` shows the wrong phrase to the user

**Location:** `_wake_phrase()` in `wake_word.py`  
**Symptom:** Wake word never triggers even after saying the correct phrase many times.

**Root cause:**
```python
def _wake_phrase() -> str:
    configured = str(settings.get("wake_word_model") or "hey_atlas")
    return configured.replace("_", " ")  # Always returns "hey atlas"
```

When `hey_jarvis` (the backend model for "hey_atlas") isn't available, OpenWakeWord falls back to `alexa` or `computer`. The startup message still says `"Wake word active — say 'hey atlas'"` but the model is listening for `"alexa"`. The user says the wrong phrase and nothing happens.

**Fix:** `_wake_phrase()` now returns the **active backend model's** phrase, not the configured name:
```python
def _wake_phrase() -> str:
    if _active_backend_model and _active_backend_model not in ("", "auto"):
        return _active_backend_model.replace("_", " ")
    configured = str(settings.get("wake_word_model") or "hey_atlas")
    return configured.replace("_", " ")
```

The startup print also shows the threshold: `"Wake word active — say 'hey jarvis' (threshold: 0.35)"` which tells you exactly what to say and whether the threshold is sensible.

---

## Bug 6 — `wake_word.py`: No visibility into OWW detection scores

**Location:** `_listen_loop()` in `wake_word.py`  
**Symptom:** Impossible to know if the model is detecting anything. Users can't tell if the threshold is wrong, the model is wrong, or the mic isn't working.

**Fix:** Added peak score tracking and printing. When OWW scores a new session peak above 0.1, it prints:
```
[dim]Wake peak: 0.18 (need >0.35 to trigger)[/dim]
```

This tells you:
- The model IS detecting something (mic and model work)
- Whether to lower the threshold (e.g. set `wake_word_threshold: 0.15`)
- Or that nothing is being detected at all (mic/device problem)

Also added `logging.debug()` for all scores above 0.05 — visible with `--log-level DEBUG`.

---

## Bug 7 — `settings.py`: `_CACHE` never cleared after `save()`

**Location:** `save()` in `settings.py`  
**Symptom:** `pin_set: false` in `/status` immediately after first-run PIN setup. Any code calling `settings.get("pin_hash")` in the same session after setup sees an empty string even though the PIN was written to disk successfully. PIN verification still works (reads disk directly) but ATLAS behaves as if no PIN is set.

**Root cause:**
```python
def save(config: dict) -> None:
    _CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
    # _CACHE is NOT cleared — next load() returns stale pre-save values
```

`security.setup_pin()` calls `settings.save()` after writing the new pin_hash. But since `_CACHE` is still set to the old pre-PIN dict, every subsequent `settings.load()` call returns that stale dict until the process restarts.

**Fix:** `save()` now sets `_CACHE = None` after writing to disk, so the next `load()` re-reads from disk with the fresh values:
```python
def save(config: dict) -> None:
    global _CACHE
    _CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
    _CACHE = None  # Invalidate — next load() will re-read disk
```

---

## Bug 8 — `macros.py`: Macro steps run context-blind

**Location:** `run()` in `macros.py`  
**Symptom:** Multi-step macros where later steps depend on context set by earlier steps (or general session context) fail to resolve correctly because they're passed no memory context.

**Root cause:**
```python
parsed = classifier.classify(step_text) or llm_engine.query(step_text, [])
#                                                                      ^^^
#                                        Empty list — no memory context at all
```

Every other ATLAS command path uses `memory.get_context_for_llm(text)` for context assembly. Macros bypassed this entirely, making the LLM operate as if it had no conversation history.

**Fix:**
```python
context_str = memory.get_context_for_llm(step_text)
parsed = classifier.classify(step_text) or llm_engine.query(step_text, context_str)
```

---

## Files Not Changed (verified clean)

| File | Status |
|------|--------|
| `executor.py` | OK — unknown-action guard is correct |
| `validator.py` | OK — E-02, E-05 checks are correct |
| `security.py` | OK — verify_pin reads disk directly, PIN works |
| `verifier.py` | OK — trash_path key matches pc_control's `_ok(**extra)` |
| `rollback.py` | OK — soft_delete and auto_purge logic correct |
| `classifier.py` | OK — all patterns correct |
| `llm_engine.py` | OK — `_build_prompt` handles both str and list context |
| `history.py` | OK — uses memory.get_context_for_llm correctly |
| `memory.py` | OK — thread-safe lazy encoder load, ChromaDB API correct |
| `context_pruner.py` | OK — stop_event handling correct |
| `api/server.py` | OK — WebSocket disconnect, dry-run body parsing, auth all correct |
| `killswitch.py` | OK — asyncio fallback path is correct |
| `pc_control.py` | OK — all action functions return correct dict shape |
| `main.py` | OK — PIN setup path, voice fallback logic all correct |
