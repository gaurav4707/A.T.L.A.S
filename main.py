"""ATLAS Phase 1 CLI loop using fast classification with LLM fallback."""

from __future__ import annotations

from typing import Any

import classifier
import llm_engine
import settings


def _update_session_context(
    session_context: list[str],
    text: str,
    result: dict[str, Any],
    session_memory_turns: int,
) -> None:
    """Append exchange and trim to configured session memory window."""
    session_context.append(f"User: {text}")
    session_context.append(f"ATLAS: {result}")

    max_items = max(1, session_memory_turns) * 2
    if len(session_context) > max_items:
        del session_context[:-max_items]


def main() -> None:
    """Run the ATLAS Phase 1 command loop."""
    config = settings.load()

    if config.get("needs_pin_setup", False):
        print("First-run setup required: please configure a PIN hash in config.json before using ATLAS.")
        print("Tip: set 'pin_hash' to a bcrypt hash, then restart main.py.")
        return

    session_context: list[str] = []
    session_memory_enabled = bool(config.get("session_memory", False))
    session_memory_turns = int(config.get("session_memory_turns", 8))

    while True:
        try:
            text = input("ATLAS > ")
        except (KeyboardInterrupt, EOFError):
            print("\nExiting ATLAS.")
            break

        if text.strip().lower() in {"exit", "quit"}:
            print("Exiting ATLAS.")
            break

        classified = classifier.classify(text)
        if classified is not None:
            print("[DEBUG] classifier matched; skipping Ollama call")
            result = classified
        else:
            print("[DEBUG] classifier returned None; calling Ollama")
            llm_context = session_context if session_memory_enabled else []
            result = llm_engine.query(text, llm_context)

        print(result)

        if session_memory_enabled:
            _update_session_context(session_context, text, result, session_memory_turns)


if __name__ == "__main__":
    main()
