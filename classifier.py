"""ATLAS fast command classifier using regex and keyword matching."""

from __future__ import annotations

import re
from typing import Any

_JSON_BASE: dict[str, Any] = {
    "intent": "command",
    "action": "unknown",
    "params": {},
    "response": "",
    "risk": "low",
}


def _result(action: str, params: dict[str, Any], response: str, risk: str) -> dict[str, Any]:
    """Build a normalized command result payload."""
    payload = dict(_JSON_BASE)
    payload["action"] = action
    payload["params"] = params
    payload["response"] = response
    payload["risk"] = risk
    return payload


def classify(text: str) -> dict[str, Any] | None:
    """Classify user text into a known command payload or return None."""
    try:
        normalized: str = re.sub(r"\s+", " ", text.strip())
        if not normalized:
            return None

        lower = normalized.lower()

        if lower == "mute":
            return _result("mute_volume", {}, "Muting system volume.", "low")
        if lower == "shutdown":
            return _result("shutdown_pc", {}, "Shutting down the PC.", "high")
        if lower == "restart":
            return _result("restart_pc", {}, "Restarting the PC.", "high")
        if lower == "sleep":
            return _result("sleep_pc", {}, "Putting the PC to sleep.", "low")
        if lower == "what time is it":
            return _result("get_time", {}, "Checking the current time.", "low")

        match = re.fullmatch(r"run\s+macro\s+(.+)", normalized, flags=re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            return _result("run_macro", {"name": name}, f"Running macro '{name}'.", "low")

        match = re.fullmatch(r"set\s+volume\s+(\d{1,3})", normalized, flags=re.IGNORECASE)
        if not match:
            match = re.fullmatch(r"volume\s+(\d{1,3})", normalized, flags=re.IGNORECASE)
        if match:
            level = max(0, min(100, int(match.group(1))))
            return _result("set_volume", {"level": level}, f"Setting volume to {level}.", "low")

        match = re.fullmatch(r"search\s+(.+)", normalized, flags=re.IGNORECASE)
        if match:
            query = match.group(1).strip()
            return _result("web_search", {"query": query}, f"Searching for '{query}'.", "low")

        match = re.fullmatch(r"copy\s+(.+)", normalized, flags=re.IGNORECASE)
        if match:
            copied_text = match.group(1)
            return _result("clipboard_write", {"text": copied_text}, "Copying text to clipboard.", "low")

        match = re.fullmatch(r"close\s+(.+)", normalized, flags=re.IGNORECASE)
        if match:
            app = match.group(1).strip()
            return _result("close_app", {"app": app}, f"Closing {app}.", "low")

        match = re.fullmatch(r"open\s+(.+)", normalized, flags=re.IGNORECASE)
        if match:
            target = match.group(1).strip()
            if re.match(r"^(https?://|www\.)", target, flags=re.IGNORECASE):
                return _result("open_url", {"url": target}, f"Opening URL {target}.", "low")
            return _result("open_app", {"app": target}, f"Opening {target}.", "low")

        return None
    except Exception:
        return None
