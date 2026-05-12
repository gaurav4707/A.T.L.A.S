"""Validation layer for ATLAS actions, risk tiers, and path safety checks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import psutil

import settings


@dataclass
class ValidationResult:
    """Result object returned by validation checks."""

    ok: bool
    risk: str
    reason: str


RISK_TABLE: dict[str, str] = {
    "open_app": "low",
    "close_app": "low",
    "web_search": "low",
    "open_url": "low",
    "get_time": "low",
    "clipboard_read": "low",
    "clipboard_write": "low",
    "set_volume": "low",
    "mute_volume": "low",
    "sleep_pc": "low",
    "screenshot": "low",
    "rename_file": "medium",
    "create_folder": "medium",
    "create_file": "medium",
    "move_file": "medium",
    "delete_file": "high",
    "run_script": "high",
    "shutdown_pc": "high",
    "restart_pc": "high",
}

_FILE_OPS: set[str] = {"rename_file", "create_folder", "create_file", "move_file", "delete_file"}
_PATH_PARAM_NAMES: tuple[str, ...] = ("path", "old", "new", "src", "dst", "folder_path", "file_path")


def _abspath(value: str) -> str:
    """Normalize a path for safe prefix comparisons."""
    return os.path.abspath(os.path.expanduser(value))


def _path_is_within(path_value: str, roots: list[str]) -> bool:
    """Return True if path_value is inside one of roots."""
    candidate = _abspath(path_value)
    for root in roots:
        normalized_root = _abspath(root)
        try:
            if os.path.commonpath([candidate, normalized_root]) == normalized_root:
                return True
        except ValueError:
            continue
    return False


def _extract_candidate_paths(params: dict[str, Any]) -> list[str]:
    """Collect candidate file paths from known parameter keys."""
    found: list[str] = []
    for key in _PATH_PARAM_NAMES:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value)
    return found


def _is_process_running(process_names: set[str]) -> bool:
    """Return True if any process in process_names is running."""
    for proc in psutil.process_iter(["name"]):
        try:
            name = str(proc.info.get("name") or "").lower()
            if name in process_names:
                return True
        except (psutil.Error, OSError):
            continue
    return False


def _vscode_unsaved(target_path: str) -> bool:
    """Best-effort check for VS Code unsaved state for a given target path."""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return False

    target_abs = _abspath(target_path).replace("\\", "/").lower()
    code_user = Path(appdata) / "Code" / "User"

    possible_files = [code_user / "workspaceStorage" / "storage.json"]
    try:
        for storage_dir in (code_user / "workspaceStorage").glob("*/"):
            possible_files.append(storage_dir / "state.vscdb")
            possible_files.append(storage_dir / "workspace.json")
    except OSError:
        pass

    for file_path in possible_files:
        if not file_path.exists() or not file_path.is_file():
            continue
        try:
            blob = file_path.read_text(encoding="utf-8", errors="ignore").lower()
            if target_abs in blob:
                return True
        except OSError:
            continue

    return False


def _notepadpp_unsaved(target_path: str) -> bool:
    """Read Notepad++ session.xml and detect unsaved backup entries."""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return False

    session_xml = Path(appdata) / "Notepad++" / "session.xml"
    if not session_xml.exists():
        return False

    target_abs = _abspath(target_path).replace("\\", "/").lower()
    try:
        root = ET.parse(session_xml).getroot()
    except (ET.ParseError, OSError):
        return False

    for file_node in root.findall(".//File"):
        filename = str(file_node.attrib.get("filename") or "").replace("\\", "/").lower()
        if filename != target_abs:
            continue
        backup_path = str(file_node.attrib.get("backupFilePath") or "").strip()
        is_dirty = str(file_node.attrib.get("dirty") or "0").strip() == "1"
        if backup_path or is_dirty:
            return True
    return False


def _e05_unsaved_document_guard(action: str, params: dict[str, Any]) -> ValidationResult | None:
    """E-05: block file operations when unsaved editor content is detected."""
    if action not in _FILE_OPS:
        return None

    candidates = _extract_candidate_paths(params)
    if not candidates:
        return None

    target = candidates[0]
    vscode_running = _is_process_running({"code.exe"})
    if vscode_running and _vscode_unsaved(target):
        return ValidationResult(
            ok=False,
            risk=RISK_TABLE.get(action, ""),
            reason="That file is open with unsaved changes in VS Code. Save or close it first.",
        )

    notepadpp_running = _is_process_running({"notepad++.exe"})
    if notepadpp_running and _notepadpp_unsaved(target):
        return ValidationResult(
            ok=False,
            risk=RISK_TABLE.get(action, ""),
            reason="That file is open with unsaved changes in Notepad++. Save or close it first.",
        )

    return None


def _e02_cross_volume_guard(action: str, params: dict[str, Any]) -> ValidationResult | None:
    """E-02: block move/delete operations crossing trash volume."""
    if action not in {"move_file", "delete_file"}:
        return None

    source = str(params.get("src") if action == "move_file" else params.get("path") or "")
    if not source:
        return None

    src_drive = os.path.splitdrive(_abspath(source))[0].lower()
    trash_drive = os.path.splitdrive(_abspath(".atlas_trash"))[0].lower()

    if src_drive and trash_drive and src_drive != trash_drive:
        return ValidationResult(
            ok=False,
            risk=RISK_TABLE.get(action, ""),
            reason="Cross-volume operation. ATLAS cannot guarantee rollback. Confirm manually.",
        )
    return None


def _path_policy_check(params: dict[str, Any], risk: str) -> ValidationResult | None:
    """Enforce blocked_paths precedence over allowed_paths using abspath checks."""
    config = settings.load()
    blocked_paths = [str(item) for item in config.get("blocked_paths", []) if isinstance(item, str)]
    allowed_paths = [str(item) for item in config.get("allowed_paths", []) if isinstance(item, str)]

    candidates = _extract_candidate_paths(params)
    for path_value in candidates:
        if _path_is_within(path_value, blocked_paths):
            return ValidationResult(ok=False, risk=risk, reason="Blocked path")

    if allowed_paths:
        for path_value in candidates:
            if not _path_is_within(path_value, allowed_paths):
                return ValidationResult(ok=False, risk=risk, reason="Path is outside allowed paths")

    return None


def validate(action: str, params: dict[str, Any]) -> ValidationResult:
    """Validate action name, risk, path policy, and E-02/E-05 guards."""
    risk = RISK_TABLE.get(action)
    if risk is None:
        return ValidationResult(ok=False, risk="", reason="Unknown action")

    path_result = _path_policy_check(params, risk)
    if path_result is not None:
        return path_result

    e05_result = _e05_unsaved_document_guard(action, params)
    if e05_result is not None:
        return e05_result

    e02_result = _e02_cross_volume_guard(action, params)
    if e02_result is not None:
        return e02_result

    return ValidationResult(ok=True, risk=risk, reason="OK")
