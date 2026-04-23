# ATLAS — Copilot Instructions

## Project

ATLAS is a local-first command and voice assistant. Current work is on the v2 line: Python 3.11+, Windows 10/11, Node 20+, and Rust for the Tauri HUD.

Before adding new detail here, prefer linking to existing docs:

- [Readme.md](../Readme.md)
- [ATLAS_Bug_Report.md](../ATLAS_Bug_Report.md)
- [OPENWAKEWORD_MIGRATION.md](../OPENWAKEWORD_MIGRATION.md)
- Treat `.github/prompts/*.prompt.md` as workflow helpers, not architecture source of truth.

## Always-On Rules

1. Keep the CLI working. The `atlas` entrypoint must remain valid, and `python main.py --status` should work when the launcher is unavailable.
2. Never send LLM output directly to `os.system()` or `subprocess`.
3. Keep execution on the fixed action pipeline: classifier or LLM fallback -> validator -> security -> executor.ACTION_MAP -> verifier -> rollback if needed.
4. Preserve the three memory layers only: sliding window, ChromaDB facts/summaries, and the background pruner.
5. Every Python function should stay typed, and new or changed files should keep module/class docstrings.
6. Use async/await for I/O where the code already follows that pattern.
7. Prefer small, testable changes. Run the relevant existing checks before and after edits.
8. Do not create a second backend for the HUD. The Tauri/React client must use the same FastAPI service as the CLI.
9. Chains and macros must stay on the same security path as normal commands.
10. Keep voice/PTT robustness fixes from [ATLAS_Bug_Report.md](../ATLAS_Bug_Report.md) active unless a replacement is tested.

## Coding Conventions

- Keep `from __future__ import annotations` in Python modules.
- Use modern typing (`dict[str, Any]`, `list[str]`, `X | None`) and fully typed function signatures.
- Keep module docstrings in new or changed Python files.
- FastAPI and WebSocket handlers are async; keep other paths sync unless an existing async pattern already exists.

## Environment Preflight

- Ensure Ollama is running before assistant flows that need model inference (`ollama serve`).
- Use the workspace venv for validation when launcher resolution is ambiguous.
- Voice flows may require FFmpeg/FFplay and valid Windows microphone permissions.
- Preserve graceful degradation in voice/wake flows when optional dependencies are unavailable.

## How To Run It

- Install in editable mode: `python -m pip install -e .`
- Setup and health checks: `atlas --setup`, `atlas --status`
- Single command: `atlas "<command>"`
- Preview only: `atlas --dry "<command>"`
- History: `atlas --history`, `atlas --history search <term>`, `atlas --rerun <id>`
- Macros/chains: `atlas --macro list`, `atlas --macro run <name>`, `atlas --chain list`, `atlas --chain run <name>`
- CLI installer: `atlas --install-cli`
- If the launcher is not installed, use `.venv\Scripts\python.exe main.py --status` or `python main.py --status`

## Tests To Prefer

- `python phase2_selftest.py`
- `python openwakeword_migration_selftest.py`
- `python test_voice_integration.py`
- `python test_wake_tuning.py`
- `python test_audio.py`

## Code Boundaries

- [Readme.md](../Readme.md) documents the feature set, setup, and usage.
- [ATLAS_Bug_Report.md](../ATLAS_Bug_Report.md) captures the known voice and settings fixes that must stay active.
- [OPENWAKEWORD_MIGRATION.md](../OPENWAKEWORD_MIGRATION.md) covers wake-word migration details.
- [main.py](../main.py) is the orchestrator and CLI entrypoint.
- [api/server.py](../api/server.py) and [api/ws_manager.py](../api/ws_manager.py) own the FastAPI REST and WebSocket layer.
- [memory.py](../memory.py) owns sliding context and semantic memory.
- [context_pruner.py](../context_pruner.py) owns background compression into memory.
- [voice.py](../voice.py) and [wake_word.py](../wake_word.py) own audio capture and wake-word behavior.
- [executor.py](../executor.py), [validator.py](../validator.py), [security.py](../security.py), [verifier.py](../verifier.py), and [rollback.py](../rollback.py) form the security and execution pipeline.

## Known Pitfalls

- `settings.get()` is key-only in this repo; use `settings.get(key) or default`.
- `atlas` may point to the wrong Python install on this machine; prefer the workspace venv when validating changes.
- ChromaDB collection reads return ids automatically; do not pass an `ids` include key.
- For keyboard hooks in PTT flows, unhook with `keyboard.unhook(handle)` rather than `keyboard.remove_hotkey()`.
- The voice stack is expected to degrade gracefully when wake-word or audio dependencies are unavailable.
- Keep critical fixes from [ATLAS_Bug_Report.md](../ATLAS_Bug_Report.md) intact unless a replacement fix is fully validated.

## Notes For Future Work

- Keep phase-specific or bug-specific detail in linked docs instead of expanding this file.
- If a new area needs different rules, add a targeted instruction file with `applyTo` instead of bloating the workspace-wide instructions.
