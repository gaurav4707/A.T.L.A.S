"""Security confirmation gates and PIN handling for ATLAS actions."""

from __future__ import annotations

import getpass
import json
import time
from pathlib import Path

import bcrypt

import settings

_ATTEMPT_COUNTER = 0
_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def setup_pin() -> bool:
    """Set up a 4-digit PIN and persist its bcrypt hash to config.json."""
    try:
        first = getpass.getpass("Set a 4-digit PIN: ")
        second = getpass.getpass("Confirm your 4-digit PIN: ")

        if first != second:
            print("PINs do not match.")
            return False
        if len(first) != 4 or not first.isdigit():
            print("PIN must be exactly 4 digits.")
            return False

        pin_hash = bcrypt.hashpw(first.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

        data = settings.load()
        data["pin_hash"] = pin_hash
        data.pop("needs_pin_setup", None)
        _CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print("PIN setup complete.")
        return True
    except Exception as exc:
        print(f"PIN setup failed: {exc}")
        return False


def verify_pin(entered: str) -> bool:
    """Check an entered PIN against the stored bcrypt hash."""
    try:
        payload = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        pin_hash = str(payload.get("pin_hash") or "")
        if not pin_hash:
            return False
        return bcrypt.checkpw(entered.encode("utf-8"), pin_hash.encode("utf-8"))
    except Exception:
        return False


def _lockout_countdown(seconds: int) -> None:
    """Block for lockout duration while printing progress every 10 seconds."""
    remaining = seconds
    while remaining > 0:
        print(f"Too many failed PIN attempts. Lockout: {remaining}s remaining")
        sleep_for = 10 if remaining >= 10 else remaining
        time.sleep(sleep_for)
        remaining -= sleep_for


def request_confirmation(risk: str, description: str) -> bool:
    """Apply ring-2 confirmation logic based on action risk tier."""
    global _ATTEMPT_COUNTER

    normalized = (risk or "").strip().lower()

    if normalized == "low":
        return True

    if normalized == "medium":
        answer = input(f"Confirm: {description}? (yes/no) > ").strip().lower()
        return answer == "yes"

    if normalized == "high":
        print(description)
        for _ in range(3):
            entered = getpass.getpass("Enter PIN: ")
            if verify_pin(entered):
                _ATTEMPT_COUNTER = 0
                return True
            _ATTEMPT_COUNTER += 1
            print("Incorrect PIN.")
            if _ATTEMPT_COUNTER >= 3:
                _lockout_countdown(60)
                _ATTEMPT_COUNTER = 0
                return False
        return False

    if normalized == "critical":
        print("This action is blocked by default.")
        return False

    return False
