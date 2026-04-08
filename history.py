"""SQLite-backed command history for ATLAS CLI and API flows."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import classifier
import executor
import llm_engine
import settings


def _db_path() -> Path:
    """Resolve configured history database path from settings."""
    log_file = str(settings.get("log_file") or "history.db")
    return Path(__file__).resolve().parent / log_file


def _connect() -> sqlite3.Connection:
    """Open database connection and ensure the commands table exists."""
    conn = sqlite3.connect(str(_db_path()))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS commands(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            raw_command TEXT,
            parsed_action TEXT,
            params TEXT,
            success INTEGER,
            latency_ms INTEGER,
            risk_tier TEXT
        )
        """
    )
    return conn


def log(raw: str, action: str, params: dict[str, Any], success: bool, latency_ms: int, risk: str) -> None:
    """Insert one command execution row into history."""
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO commands(timestamp, raw_command, parsed_action, params, success, latency_ms, risk_tier)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                raw,
                action,
                json.dumps(params, ensure_ascii=True),
                1 if success else 0,
                int(latency_ms),
                risk,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Map a sqlite row to a plain dictionary payload."""
    return {
        "id": row[0],
        "timestamp": row[1],
        "raw_command": row[2],
        "parsed_action": row[3],
        "params": row[4],
        "success": row[5],
        "latency_ms": row[6],
        "risk_tier": row[7],
    }


def list_recent(n: int = 20) -> list[dict[str, Any]]:
    """Return the most recent n command rows ordered by newest first."""
    conn = _connect()
    try:
        cursor = conn.execute(
            """
            SELECT id, timestamp, raw_command, parsed_action, params, success, latency_ms, risk_tier
            FROM commands
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, int(n)),),
        )
        rows = cursor.fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def search(keyword: str) -> list[dict[str, Any]]:
    """Return command rows where raw command contains keyword."""
    conn = _connect()
    try:
        cursor = conn.execute(
            """
            SELECT id, timestamp, raw_command, parsed_action, params, success, latency_ms, risk_tier
            FROM commands
            WHERE raw_command LIKE ?
            ORDER BY id DESC
            """,
            (f"%{keyword}%",),
        )
        rows = cursor.fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def get_by_id(n: int) -> dict[str, Any] | None:
    """Return one command row by numeric id or None when missing."""
    conn = _connect()
    try:
        cursor = conn.execute(
            """
            SELECT id, timestamp, raw_command, parsed_action, params, success, latency_ms, risk_tier
            FROM commands
            WHERE id = ?
            """,
            (int(n),),
        )
        row = cursor.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def rerun(n: int) -> dict[str, Any]:
    """Re-execute the original raw command for a stored history row."""
    row = get_by_id(n)
    if row is None:
        return {"success": False, "message": f"History item {n} not found."}

    text = str(row["raw_command"])
    result = classifier.classify(text) or llm_engine.query(text, [])

    action = str(result.get("action", ""))
    params = result.get("params", {})
    if not isinstance(params, dict):
        params = {}

    return executor.execute(action, params)
