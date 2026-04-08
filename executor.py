"""Secure ATLAS action executor using validator, confirmation gates, verification, and logging."""

from __future__ import annotations

from typing import Any

import rollback
import security
import validator
import verifier
from pc_control import (
    clipboard_read,
    clipboard_write,
    close_app,
    create_file,
    delete_file,
    get_time,
    move_file,
    mute_volume,
    open_app,
    open_url,
    rename_file,
    restart_pc,
    run_macro_action,
    set_volume,
    shutdown_pc,
    sleep_pc,
    web_search,
)


ACTION_MAP = {
    "open_app": open_app,
    "close_app": close_app,
    "web_search": web_search,
    "open_url": open_url,
    "set_volume": set_volume,
    "mute_volume": mute_volume,
    "sleep_pc": sleep_pc,
    "shutdown_pc": shutdown_pc,
    "restart_pc": restart_pc,
    "create_file": create_file,
    "rename_file": rename_file,
    "move_file": move_file,
    "delete_file": delete_file,
    "clipboard_read": clipboard_read,
    "clipboard_write": clipboard_write,
    "get_time": get_time,
    "run_macro": run_macro_action,
}


def _describe(action: str, params: dict[str, Any]) -> str:
    """Build confirmation text for medium/high risk actions."""
    target_value = (
        params.get("path")
        or params.get("src")
        or params.get("dst")
        or params.get("app")
        or params.get("url")
        or "-"
    )
    return f"Action: {action} | Target: {target_value}"


def execute(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Execute one action through the fixed ACTION_MAP and security pipeline."""
    if action not in ACTION_MAP:
        return {"success": False, "message": "Unknown action"}

    validation = validator.validate(action, params)
    if not validation.ok:
        return {"success": False, "message": validation.reason}

    description = _describe(action, params)
    if not security.request_confirmation(validation.risk, description):
        return {"success": False, "message": "Action cancelled"}

    result = ACTION_MAP[action](**params)

    verify_result = verifier.verify(action, params, result)
    if not verify_result.ok:
        print(f"[WARN] Verification uncertain: {verify_result.message}")

    rollback.log_step(action, str(params), result, verify_result.ok)
    return result
