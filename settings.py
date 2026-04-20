"""ATLAS settings management for loading, validating, and persisting config."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

_CONFIG_PATH: Path = Path(__file__).resolve().parent / "config.json"
_CACHE: dict[str, Any] | None = None


DEFAULT_CONFIG: dict[str, Any] = {
    "model": "mistral:7b",
    "chroma_path": ".atlas_chroma",
    "memory_confidence_threshold": 0.75,
    "memory_expiry_days": 30,
    "voice_input": False,
    "voice_output": False,
    "voice_key": "right_ctrl",
    "voice_speed": 1.0,
    "wake_word_enabled": False,
    "wake_word_backend": "vosk",
    "wake_word_phrase": "hey atlas",
    "vosk_model_path": "vosk-model-small-en-us-0.15",
    "vad_silence_ms": 1500,
    "killswitch_hotkey": "ctrl+shift+k",
    "killswitch_word": "stop",
    "session_memory": False,
    "session_memory_turns": 8,
    "allowed_paths": [str(Path.home()).replace("\\", "/")],
    "blocked_paths": ["C:/Windows", "C:/Program Files"],
    "pin_hash": "",
    "trash_dir": ".atlas_trash",
    "trash_retention_days": 7,
    "log_file": "history.db",
    "api_token": "",
}


def _normalize(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize configuration values and fill missing fields with defaults."""
    normalized: dict[str, Any] = dict(DEFAULT_CONFIG)
    normalized.update(config)

    if not isinstance(normalized.get("allowed_paths"), list):
        normalized["allowed_paths"] = [DEFAULT_CONFIG["allowed_paths"][0]]
    if not isinstance(normalized.get("blocked_paths"), list):
        normalized["blocked_paths"] = list(DEFAULT_CONFIG["blocked_paths"])

    if not normalized.get("api_token"):
        normalized["api_token"] = str(uuid.uuid4())

    if not isinstance(normalized.get("session_memory_turns"), int):
        normalized["session_memory_turns"] = int(DEFAULT_CONFIG["session_memory_turns"])

    try:
        normalized["memory_confidence_threshold"] = float(normalized.get("memory_confidence_threshold", 0.75))
    except (TypeError, ValueError):
        normalized["memory_confidence_threshold"] = 0.75

    try:
        normalized["memory_expiry_days"] = int(normalized.get("memory_expiry_days", 30))
    except (TypeError, ValueError):
        normalized["memory_expiry_days"] = 30

    normalized["chroma_path"] = str(normalized.get("chroma_path", ".atlas_chroma"))

    if normalized["session_memory_turns"] < 1:
        normalized["session_memory_turns"] = 1

    try:
        normalized["voice_speed"] = float(normalized.get("voice_speed", 1.0))
    except (TypeError, ValueError):
        normalized["voice_speed"] = 1.0

    normalized["voice_input"] = bool(normalized.get("voice_input", False))
    normalized["voice_output"] = bool(normalized.get("voice_output", False))
    normalized["session_memory"] = bool(normalized.get("session_memory", False))
    normalized["pin_hash"] = str(normalized.get("pin_hash", ""))
    normalized["model"] = str(normalized.get("model", "mistral:7b"))

    # Derived runtime flag for first-run PIN setup.
    normalized["needs_pin_setup"] = normalized["pin_hash"].strip() == ""
    return normalized


def save(config: dict[str, Any]) -> None:
    """Persist configuration to disk and invalidate the in-memory cache.

    FIX BUG 7: The original save() wrote to disk but never cleared _CACHE.
    Any subsequent settings.load() call in the same process returned the
    stale pre-save values. This caused pin_hash to appear empty right after
    security.setup_pin() set it, making /status report pin_set=False and
    making every in-process settings.get('pin_hash') return '' until restart.

    Fix: set _CACHE = None after every save so the next load() re-reads disk.
    """
    global _CACHE

    clean_config: dict[str, Any] = dict(config)
    clean_config.pop("needs_pin_setup", None)
    _CONFIG_PATH.write_text(json.dumps(clean_config, indent=2), encoding="utf-8")

    # Invalidate cache so next load() reflects the just-written values.
    _CACHE = None


def load() -> dict[str, Any]:
    """Load configuration from disk, creating defaults on first run."""
    global _CACHE

    if _CACHE is not None:
        return dict(_CACHE)

    if not _CONFIG_PATH.exists():
        initial_config: dict[str, Any] = _normalize({})
        _CACHE = initial_config
        # Use write-through path that also clears cache (safe: _CACHE just set)
        clean = dict(initial_config)
        clean.pop("needs_pin_setup", None)
        _CONFIG_PATH.write_text(json.dumps(clean, indent=2), encoding="utf-8")
        return dict(_CACHE)

    try:
        raw_text: str = _CONFIG_PATH.read_text(encoding="utf-8")
        parsed: dict[str, Any] = json.loads(raw_text)
    except (OSError, json.JSONDecodeError):
        parsed = {}

    normalized = _normalize(parsed)

    # Rewrite if defaults/normalization changed persisted values.
    clean = dict(normalized)
    clean.pop("needs_pin_setup", None)
    _CONFIG_PATH.write_text(json.dumps(clean, indent=2), encoding="utf-8")

    _CACHE = normalized
    return dict(_CACHE)


def get(key: str) -> Any:
    """Get a single setting value by key from loaded config."""
    config = load()
    return config.get(key)
