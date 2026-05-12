"""Post-action verification checks for ATLAS execution results."""

from __future__ import annotations

import os
from dataclasses import dataclass

import psutil
import pyperclip


@dataclass
class VerifyResult:
    """Result object for post-execution verification."""

    ok: bool
    message: str


def _process_names() -> list[str]:
    """Collect lowercase process names safely."""
    names: list[str] = []
    for proc in psutil.process_iter(["name"]):
        try:
            names.append(str(proc.info.get("name") or "").lower())
        except (psutil.Error, OSError):
            continue
    return names


def verify(action: str, params: dict, result: dict) -> VerifyResult:
    """Verify specific action outcomes and never raise exceptions."""
    try:
        if action == "open_app":
            app_name = str(params.get("app", "")).lower()
            ok = any(name.startswith(app_name) for name in _process_names())
            return VerifyResult(ok=ok, message="open_app verification completed")

        if action == "close_app":
            app_name = str(params.get("app", "")).lower()
            still_running = any(name.startswith(app_name) for name in _process_names())
            return VerifyResult(ok=not still_running, message="close_app verification completed")

        if action == "create_file":
            path = str(params.get("path", ""))
            return VerifyResult(ok=os.path.exists(path), message="create_file verification completed")

        if action == "delete_file":
            original = str(params.get("path", ""))
            trash_path = str(result.get("trash_path", ""))
            ok = (not os.path.exists(original)) and bool(trash_path) and os.path.exists(trash_path)
            return VerifyResult(ok=ok, message="delete_file verification completed")

        if action == "rename_file":
            old = str(params.get("old", ""))
            new = str(params.get("new", ""))
            ok = os.path.exists(new) and (not os.path.exists(old))
            return VerifyResult(ok=ok, message="rename_file verification completed")

        if action == "move_file":
            src = str(params.get("src", ""))
            dst = str(params.get("dst", ""))
            ok = os.path.exists(dst) and (not os.path.exists(src))
            return VerifyResult(ok=ok, message="move_file verification completed")

        if action in {"web_search", "open_url"}:
            names = _process_names()
            ok = any("chrome" in name or "msedge" in name for name in names)
            return VerifyResult(ok=ok, message="browser action verification completed")

        if action in {"shutdown_pc", "restart_pc"}:
            return VerifyResult(ok=True, message="Cannot verify power action")

        if action == "clipboard_write":
            expected = str(params.get("text", ""))
            ok = pyperclip.paste() == expected
            return VerifyResult(ok=ok, message="clipboard_write verification completed")

        return VerifyResult(ok=True, message="No verification available")
    except Exception as exc:
        return VerifyResult(ok=False, message=f"Verification error: {exc}")
