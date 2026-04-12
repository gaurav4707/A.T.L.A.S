"""Background context pruner that compresses sliding memory into ChromaDB."""

from __future__ import annotations

import threading
import time

import ollama

import memory
import settings

stop_event = threading.Event()


def compress_and_store() -> None:
    """Summarise the current sliding conversation window and persist the result."""
    context = memory.get_sliding_context()
    if len(context) < 4:
        return

    messages_text = "\n".join(f"{message['role']}: {message['content']}" for message in context)
    prompt = f"Summarise this conversation in 3-5 sentences for future reference:\n{messages_text}"
    try:
        response = ollama.generate(model=settings.get("model"), prompt=prompt)
        summary = str(response.get("response", "")).strip()
        if not summary:
            return
        memory.store_fact(summary, confidence=0.9, source="session_pruner")
        memory.store_summary(summary, confidence=0.9, source="session_pruner")
    except Exception:
        pass


def pruner_loop() -> None:
    """Run the background compression loop until stopped."""
    while not stop_event.is_set():
        stop_event.wait(timeout=1800)
        if not stop_event.is_set():
            compress_and_store()


def start_pruner() -> None:
    """Start the background pruner thread."""
    if stop_event.is_set():
        stop_event.clear()
    thread = threading.Thread(target=pruner_loop, daemon=True)
    thread.start()


def stop_pruner() -> None:
    """Signal the background pruner thread to stop."""
    stop_event.set()