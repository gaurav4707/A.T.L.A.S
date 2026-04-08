"""ATLAS LLM engine wrapper for Ollama with strict JSON output handling."""

from __future__ import annotations

import json
from typing import Any

import ollama

import settings

SAFE_FALLBACK: dict[str, Any] = {
    "intent": "unknown",
    "action": "unknown",
    "params": {},
    "response": "I did not understand that. Could you rephrase?",
    "risk": "low",
}

_ALLOWED_RISK = {"low", "medium", "high", "critical"}

# Grammar guidance for llama.cpp-compatible JSON shape constraints.
_GBNF_GRAMMAR = """
root ::= object
object ::= "{" ws "\"intent\"" ws ":" ws string ws "," ws "\"action\"" ws ":" ws string ws "," ws "\"params\"" ws ":" ws params ws "," ws "\"response\"" ws ":" ws string ws "," ws "\"risk\"" ws ":" ws risk ws "}"
risk ::= "\"low\"" | "\"medium\"" | "\"high\"" | "\"critical\""
params ::= "{" ws [pair (ws "," ws pair)*] ws "}"
pair ::= string ws ":" ws value
value ::= string | number | "true" | "false" | "null" | params | array
array ::= "[" ws [value (ws "," ws value)*] ws "]"
string ::= "\"" chars "\""
chars ::= "" | char chars
char ::= [^"\\] | "\\" ["\\/bfnrt]
number ::= [0-9]+
ws ::= [ \t\n\r]*
""".strip()


def _build_prompt(user_prompt: str, session_context: list[Any]) -> str:
    """Build the full prompt with optional session context and JSON constraints."""
    context_block = ""
    if session_context:
        context_lines = [f"- {item}" for item in session_context]
        context_block = "Session context:\n" + "\n".join(context_lines) + "\n\n"

    return (
        "You are ATLAS command parser.\n"
        "Return ONLY valid JSON matching this exact object shape:\n"
        "{\"intent\": str, \"action\": str, \"params\": dict, \"response\": str, \"risk\": str}.\n"
        "Risk must be one of: low, medium, high, critical.\n"
        "Do not include markdown or explanations.\n"
        "Use this llama.cpp GBNF grammar guidance:\n"
        f"{_GBNF_GRAMMAR}\n\n"
        f"{context_block}"
        f"User input: {user_prompt}"
    )


def _validate_payload(data: Any) -> dict[str, Any] | None:
    """Validate parsed JSON payload against required fields and values."""
    if not isinstance(data, dict):
        return None

    required = {"intent", "action", "params", "response", "risk"}
    if set(data.keys()) != required:
        return None

    if not isinstance(data["intent"], str):
        return None
    if not isinstance(data["action"], str):
        return None
    if not isinstance(data["params"], dict):
        return None
    if not isinstance(data["response"], str):
        return None
    if not isinstance(data["risk"], str):
        return None
    if data["risk"] not in _ALLOWED_RISK:
        return None

    return data


def _safe_json(raw: str) -> dict[str, Any] | None:
    """Parse and validate a candidate JSON response string."""
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return _validate_payload(parsed)


def query(prompt: str, session_context: list[Any]) -> dict[str, Any]:
    """Query the configured Ollama model and return a safe structured payload."""
    model_name = str(settings.get("model") or "mistral:7b")
    full_prompt = _build_prompt(prompt, session_context)

    attempts = 0
    while attempts < 2:
        attempts += 1
        try:
            response = ollama.generate(model=model_name, prompt=full_prompt)
            text = str(response.get("response", "")).strip()
            payload = _safe_json(text)
            if payload is not None:
                return payload
        except Exception:
            continue

    return dict(SAFE_FALLBACK)
