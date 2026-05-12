"""Macro storage and execution through the ATLAS security pipeline."""

from __future__ import annotations

import json
import os
import time
import builtins
from pathlib import Path
from typing import Any

import classifier
import executor
import llm_engine
import memory  # FIX BUG 8: was missing — macros need memory context like every other caller

_MACROS_PATH = Path(__file__).resolve().parent / "macros.json"
_DEFAULT_MACROS: dict[str, Any] = {
    "dev": ["open vscode .", "open chrome localhost:3000", "open terminal"],
    "notes": "open vscode C:/Users/Gaurav/notes.md",
    "vol50": "set volume 50",
    "search": "open chrome google.com/search?q={input}",
}


def _ensure_macros_file() -> None:
    """Create macros.json with defaults when it does not exist."""
    if not _MACROS_PATH.exists():
        _MACROS_PATH.write_text(json.dumps(_DEFAULT_MACROS, indent=2), encoding="utf-8")


def _load_macros() -> dict[str, Any]:
    """Load and parse macros from disk with default fallback."""
    _ensure_macros_file()
    try:
        return json.loads(_MACROS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_MACROS)


def list() -> dict[str, Any]:
    """Return all configured macros."""
    return _load_macros()


def add() -> dict[str, Any]:
    """Open macros.json in the default editor for manual editing."""
    _ensure_macros_file()
    try:
        os.startfile(str(_MACROS_PATH))
        return {"success": True, "message": "Opened macros.json for editing."}
    except OSError as exc:
        return {"success": False, "message": f"Unable to open macros.json: {exc}"}


def run(name: str, input_val: str = "") -> dict[str, Any]:
    """Run each command step in a named macro via executor.execute."""
    macro_map = _load_macros()
    if name not in macro_map:
        return {"success": False, "message": f"Macro '{name}' not found."}

    raw_steps = macro_map[name]
    steps = raw_steps if isinstance(raw_steps, builtins.list) else [raw_steps]

    step_results: builtins.list[dict[str, Any]] = []
    for index, item in enumerate(steps, start=1):
        step_text = str(item).replace("{input}", input_val)

        # FIX BUG 8: The original called llm_engine.query(step_text, [])
        # with an empty list, completely bypassing memory context.  Every
        # other caller in ATLAS (main.py, history.py, api/server.py) uses
        # memory.get_context_for_llm() so macro steps were context-blind.
        # Use memory context here too for consistent behaviour.
        context_str = memory.get_context_for_llm(step_text)
        parsed = classifier.classify(step_text) or llm_engine.query(step_text, context_str)

        action = str(parsed.get("action", ""))
        params = parsed.get("params", {})
        if not isinstance(params, dict):
            params = {}

        result = executor.execute(action, params)
        step_results.append({"step": index, "command": step_text, "result": result})

        if not bool(result.get("success", False)):
            return {
                "success": False,
                "message": "Macro stopped due to failed step.",
                "failed_step": index,
                "results": step_results,
            }

        time.sleep(0.5)

    return {
        "success": True,
        "message": f"Macro '{name}' completed.",
        "results": step_results,
    }
