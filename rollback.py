"""Rollback helpers for soft delete logging and trash maintenance."""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import settings


def _trash_dir_for_source(source_path: str) -> Path:
    """Resolve a source-local trash directory on the same drive."""
    source_abs = os.path.abspath(source_path)
    source_drive, _ = os.path.splitdrive(source_abs)
    if not source_drive:
        source_drive = os.path.splitdrive(str(Path.cwd()))[0]
    return Path(f"{source_drive}/.atlas_trash")


def soft_delete(source_path: str) -> str:
    """Move a file to same-drive .atlas_trash and return the new path."""
    source_abs = os.path.abspath(source_path)
    source_drive = os.path.splitdrive(source_abs)[0].lower()
    trash_dir = _trash_dir_for_source(source_abs)
    trash_drive = os.path.splitdrive(str(trash_dir))[0].lower()

    if source_drive and trash_drive and source_drive != trash_drive:
        raise ValueError("Cross-volume soft delete not supported")

    trash_dir.mkdir(parents=True, exist_ok=True)
    filename = os.path.basename(source_abs)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    new_name = f"{filename}_{timestamp}"
    destination = trash_dir / new_name

    shutil.move(source_abs, str(destination))
    return str(destination)


def log_step(action: str, target: str, result: dict[str, Any], verified: bool) -> None:
    """Append execution details to operations.log."""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} | {action} | {target} | {result} | {verified}\n"
        log_path = Path(__file__).resolve().parent / "operations.log"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        return


def auto_purge() -> None:
    """Delete trash entries older than configured retention days using mtime."""
    config = settings.load()
    retention_days = int(config.get("trash_retention_days", 7))
    now = datetime.now().timestamp()

    try:
        trash_dir = _trash_dir_for_source(str(Path.cwd()))
        if not trash_dir.exists():
            return

        for item in trash_dir.iterdir():
            if not item.is_file():
                continue
            age_days = (now - os.path.getmtime(item)) / 86400
            if age_days > retention_days:
                os.remove(item)
    except (OSError, ValueError):
        return
