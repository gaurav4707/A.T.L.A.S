"""Persistent semantic and sliding-window memory for ATLAS v2."""

import os
os.environ["TRANSFORMERS_NO_ADVISORY_WARNINGS"] = "1"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"

import logging as _logging
_logging.getLogger("sentence_transformers").setLevel(_logging.ERROR)

from collections import deque
from datetime import datetime
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

import settings

try:
    from chromadb import PersistentClient

    _HAS_CHROMA = True
except Exception:
    PersistentClient = None  # type: ignore[assignment]
    _HAS_CHROMA = False

_CHROMA_PATH = Path(str(settings.get("chroma_path") or ".atlas_chroma"))
_client: Any | None = None
_facts: Any | None = None
_summaries: Any | None = None
_facts_fallback: list[dict[str, Any]] = []
_summaries_fallback: list[dict[str, Any]] = []

if _HAS_CHROMA and PersistentClient is not None:
    try:
        _client = PersistentClient(path=str(_CHROMA_PATH))
        _facts = _client.get_or_create_collection("facts")
        _summaries = _client.get_or_create_collection("summaries")
    except Exception:
        _client = None
        _facts = None
        _summaries = None
        _HAS_CHROMA = False


class _FallbackEncoder:
    """Deterministic fallback encoder when the sentence-transformer model is unavailable."""

    def encode(self, text: str) -> list[float]:
        vector = [0.0] * 384
        if not text:
            return vector

        for index, character in enumerate(text):
            vector[index % len(vector)] += (ord(character) % 37) / 37.0

        magnitude = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / magnitude for value in vector]


def _load_encoder() -> Any:
    """Load and cache the embedding model once for the current process."""
    try:
        import logging as _lg

        _lg.getLogger("sentence_transformers").setLevel(_lg.ERROR)
        _lg.getLogger("transformers").setLevel(_lg.ERROR)
        _lg.getLogger("transformers.modeling_utils").setLevel(_lg.ERROR)
        from transformers.utils import logging as _tlog
        _tlog.set_verbosity_error()
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer("all-MiniLM-L6-v2")
    except Exception:
        return _FallbackEncoder()


_encoder: Any = None  # Lazy-loaded on first use
_encoder_lock = threading.Lock()
_window_turns = int(settings.get("session_memory_turns") or 8)
sliding_window: deque[dict[str, str]] = deque(maxlen=max(1, _window_turns) * 2)


def _get_encoder() -> Any:
    """Get or initialize the encoder (lazy-loaded)."""
    global _encoder
    if _encoder is not None:
        return _encoder
    with _encoder_lock:
        if _encoder is None:
            _encoder = _load_encoder()
    return _encoder

def _embedding_for_text(text: str) -> list[float]:
    """Convert text to a Chroma-compatible embedding vector."""
    encoder = _get_encoder()
    embedding = encoder.encode(text)
    if hasattr(embedding, "tolist"):
        return list(embedding.tolist())
    return list(embedding)


def _collection_store(
    collection: Any,
    text: str,
    confidence: float,
    source: str,
    fallback_bucket: list[dict[str, Any]],
) -> None:
    """Write text to a Chroma collection when the confidence gate allows it."""
    threshold = float(settings.get("memory_confidence_threshold") or 0.75)
    if confidence < threshold:
        return

    payload = {
        "id": f"fact_{uuid4()}",
        "document": text,
        "metadata": {"source": source, "timestamp": datetime.now().isoformat()},
    }

    if not _HAS_CHROMA or collection is None:
        fallback_bucket.append(payload)
        return

    embedding = _embedding_for_text(text)
    collection.add(
        documents=[text],
        embeddings=[embedding],
        ids=[payload["id"]],
        metadatas=[payload["metadata"]],
    )


def add_to_sliding(role: str, content: str) -> None:
    """Append one message to the in-memory sliding conversation window."""
    sliding_window.append({"role": str(role), "content": str(content)})


def get_sliding_context() -> list[dict[str, str]]:
    """Return a snapshot of the current sliding conversation window."""
    return list(sliding_window)


def store_fact(fact: str, confidence: float, source: str) -> None:
    """Persist a durable fact when the confidence threshold is met."""
    _collection_store(_facts, fact, confidence, source, _facts_fallback)


def store_summary(summary: str, confidence: float, source: str) -> None:
    """Persist a compressed session summary for later retrieval."""
    _collection_store(_summaries, summary, confidence, source, _summaries_fallback)


def retrieve(query: str, n_results: int = 5) -> list[str]:
    """Retrieve the most relevant stored facts for a query."""
    if not _HAS_CHROMA or _facts is None:
        return [item["document"] for item in _facts_fallback[-max(1, n_results) :]][::-1]

    embedding = _embedding_for_text(query)
    results = _facts.query(query_embeddings=[embedding], n_results=n_results)
    if results.get("documents"):
        return [str(item) for item in results["documents"][0] if item]
    return []


def retrieve_summaries(n_results: int = 3) -> list[str]:
    """Return the most recent stored summaries ordered by timestamp."""
    if not _HAS_CHROMA or _summaries is None:
        return [item["document"] for item in _summaries_fallback[-max(1, n_results) :]][::-1]

    results = _summaries.get(include=["documents", "metadatas"])
    documents = results.get("documents") or []
    metadatas = results.get("metadatas") or []

    ordered: list[tuple[datetime, str]] = []
    for document, metadata in zip(documents, metadatas):
        if not document:
            continue
        timestamp_text = str((metadata or {}).get("timestamp", ""))
        try:
            timestamp = datetime.fromisoformat(timestamp_text)
        except ValueError:
            timestamp = datetime.min
        ordered.append((timestamp, str(document)))

    ordered.sort(key=lambda item: item[0], reverse=True)
    return [document for _, document in ordered[: max(1, n_results)]]


def review_and_expire() -> None:
    """Expire stale fact records from ChromaDB."""
    if not _HAS_CHROMA or _facts is None or _summaries is None:
        return

    expiry_days = int(settings.get("memory_expiry_days") or 30)
    cutoff = datetime.now()

    for collection in (_facts, _summaries):
        all_items = collection.get(include=["metadatas"])
        ids = all_items.get("ids") or []
        metadatas = all_items.get("metadatas") or []

        for item_id, metadata in zip(ids, metadatas):
            timestamp_text = str((metadata or {}).get("timestamp", ""))
            try:
                age = cutoff - datetime.fromisoformat(timestamp_text)
            except ValueError:
                continue

            if age.days > expiry_days:
                collection.delete(ids=[item_id])
                print(f"[dim]I've forgotten a fact — it was over {expiry_days} days old.[/dim]")


def build_system_prompt() -> str:
    """Build the baseline system prompt for memory-aware LLM calls."""
    return (
        "You are ATLAS, a local command assistant with persistent memory. "
        "Use retrieved facts and the recent conversation window when they are relevant. "
        "Prefer short, direct answers unless the user asks for detail. "
        "If memory is uncertain or missing, say so instead of inventing details. "
        "Keep responses grounded in the provided context and the current request."
    )


def _format_fact_block(facts: list[str]) -> str:
    """Render retrieved facts as a prompt block."""
    if not facts:
        return ""
    return "Relevant facts:\n" + "\n".join(f"- {fact}" for fact in facts)


def _format_window_block(window: list[dict[str, str]]) -> str:
    """Render the sliding window as a prompt block."""
    if not window:
        return ""
    lines = [f"- {entry.get('role', '')}: {entry.get('content', '')}" for entry in window if entry.get("content")]
    if not lines:
        return ""
    return "Recent conversation:\n" + "\n".join(lines)


def _count_tokens(text: str) -> int:
    """Estimate token usage with a simple word-based heuristic."""
    return len(text.split())


def get_context_for_llm(query: str) -> str:
    """Assemble a token-budgeted context string for LLM requests."""
    budget = 2200
    system = build_system_prompt()
    facts = retrieve(query)
    window = get_sliding_context()

    assembled = ""
    while True:
        parts = [system]
        fact_block = _format_fact_block(facts)
        window_block = _format_window_block(window)

        if fact_block:
            parts.append(fact_block)
        if window_block:
            parts.append(window_block)

        assembled = "\n\n".join(parts).strip()
        if _count_tokens(assembled) <= budget:
            return assembled

        if facts:
            facts.pop()
            continue
        if window:
            window.pop(0)
            continue
        return assembled