"""Unified command dispatcher for ATLAS (API, CLI, and Voice)."""

from __future__ import annotations

import logging
import time
from typing import Any

import classifier
import executor
import history
import llm_engine
import memory
import settings
import verifier
import voice
from api.broadcaster import broadcast_sync


def execute_text_command(
    text: str,
    context_str: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Execute one free-text command and log its outcome in history.
    Broadcasts all lifecycle events (user_message, token, action, done, error) to the HUD.
    """
    broadcast_sync({"type": "user_message", "data": text})
    
    started = time.perf_counter()
    
    def on_token(token: str):
        broadcast_sync({"type": "token", "data": token})
        
    try:
        parsed = classifier.classify(text) or llm_engine.query(text, context_str, on_token=on_token)

        action = str(parsed.get("action", ""))
        params = parsed.get("params", {})
        if not isinstance(params, dict):
            params = {}

        broadcast_sync({"type": "action", "data": action})
        execution_result = executor.execute(action, params)
        verify_result = verifier.verify(action, params, execution_result)
        
        # For unknown actions, preserve conversational response but keep KPI/history unsuccessful.
        if action == "unknown" and parsed.get("response"):
            execution_result = {
                "success": False,
                "message": str(parsed.get("response", "")),
            }
        
        latency_ms = int((time.perf_counter() - started) * 1000)
        history.log(
            raw=text,
            action=action,
            params=params,
            success=bool(execution_result.get("success", False)),
            latency_ms=latency_ms,
            risk=str(parsed.get("risk", "")),
        )

        assistant_response = str(parsed.get("response", execution_result.get("message", "")))
        memory.add_to_sliding("user", text)
        memory.add_to_sliding("assistant", assistant_response)

        broadcast_sync({"type": "done", "data": assistant_response})
        voice.speak(assistant_response)

        return parsed, execution_result
    except Exception as exc:
        logging.error("Dispatch failed: %s", exc)
        broadcast_sync({"type": "error", "data": str(exc)})
        raise
