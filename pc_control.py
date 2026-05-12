"""PC control action implementations used by the ATLAS executor dispatch map."""

from __future__ import annotations

import os
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path

import psutil
import pyperclip

import rollback


def _ok(message: str, **extra: object) -> dict:
    """Build a success response payload."""
    payload: dict = {"success": True, "message": message}
    payload.update(extra)
    return payload


def _fail(message: str) -> dict:
    """Build a failure response payload."""
    return {"success": False, "message": message}


def open_app(app: str) -> dict:
    """Open a supported desktop app by name."""
    try:
        normalized = app.strip().lower()

        if normalized in {"chrome", "google chrome"}:
            subprocess.Popen(["cmd", "/c", "start", "", "chrome", "about:blank"])
            return _ok("Opened Chrome.")
        if normalized in {"edge", "microsoft edge", "msedge"}:
            subprocess.Popen(["cmd", "/c", "start", "", "msedge", "about:blank"])
            return _ok("Opened Edge.")
        if normalized in {"explorer", "file explorer"}:
            subprocess.Popen(["explorer"])
            return _ok("Opened Explorer.")
        if normalized in {"terminal", "windows terminal"}:
            subprocess.Popen(["wt"])
            return _ok("Opened Terminal.")
        if normalized in {"cmd", "command prompt"}:
            subprocess.Popen(["cmd"])
            return _ok("Opened Command Prompt.")
        if normalized in {"vs code", "vscode", "code", "visual studio code"}:
            subprocess.Popen(["code"])
            return _ok("Opened VS Code.")
        if normalized.startswith("vscode ") or normalized.startswith("vs code "):
            path_arg = app.strip()[app.strip().lower().find(" ") + 1:].strip()
            if not path_arg:
                path_arg = "."
            subprocess.Popen(["code", path_arg])
            return _ok(f"Opened VS Code in {path_arg}.")
        if normalized in {"notepad++", "notepad plus plus"}:
            subprocess.Popen(["notepad++"])
            return _ok("Opened Notepad++.")
        if normalized in {"vlc", "vlc media player"}:
            subprocess.Popen(["vlc"])
            return _ok("Opened VLC.")
        if normalized in {"notepad"}:
            subprocess.Popen(["notepad"])
            return _ok("Opened Notepad.")

        return _fail(f"I don't support {app} yet. I can open it, or you can add it.")
    except Exception as exc:
        return _fail(f"Failed to open app: {exc}")


def close_app(app: str) -> dict:
    """Terminate running processes by app name prefix."""
    try:
        target = app.strip().lower()
        killed = 0
        for proc in psutil.process_iter(["name"]):
            try:
                name = str(proc.info.get("name") or "").lower()
                if name.startswith(target) or name == f"{target}.exe":
                    proc.terminate()
                    killed += 1
            except (psutil.Error, OSError):
                continue
        if killed == 0:
            return _fail(f"No running process found for {app}.")
        return _ok(f"Closed {killed} process(es) for {app}.")
    except Exception as exc:
        return _fail(f"Failed to close app: {exc}")


def web_search(query: str) -> dict:
    """Open a web search query in the browser."""
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://www.google.com/search?q={encoded}"
        subprocess.Popen(["cmd", "/c", "start", "", "chrome", url])
        return _ok(f"Searching web for: {query}")
    except Exception as exc:
        return _fail(f"Web search failed: {exc}")


def open_url(url: str) -> dict:
    """Open a URL in the browser."""
    try:
        target = url.strip()
        if not target.lower().startswith(("http://", "https://")):
            target = f"https://{target}"
        subprocess.Popen(["cmd", "/c", "start", "", "chrome", target])
        return _ok(f"Opened URL: {target}")
    except Exception as exc:
        return _fail(f"Failed to open URL: {exc}")


def set_volume(level: int) -> dict:
    """Set system volume level between 0 and 100 using PowerShell fallback."""
    try:
        bounded = max(0, min(100, int(level)))
        script = (
            "$s=(New-Object -ComObject WScript.Shell);"
            f"for($i=0;$i -lt 50;$i++){{$s.SendKeys([char]174)}};"
            f"for($i=0;$i -lt {bounded // 2};$i++){{$s.SendKeys([char]175)}}"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", script], check=False)
        return _ok(f"Volume set to {bounded}.")
    except Exception as exc:
        return _fail(f"Failed to set volume: {exc}")


def mute_volume() -> dict:
    """Toggle system mute using PowerShell fallback."""
    try:
        script = "$s=(New-Object -ComObject WScript.Shell);$s.SendKeys([char]173)"
        subprocess.run(["powershell", "-NoProfile", "-Command", script], check=False)
        return _ok("Mute toggled.")
    except Exception as exc:
        return _fail(f"Failed to mute volume: {exc}")


def sleep_pc() -> dict:
    """Put the PC to sleep."""
    try:
        subprocess.run(
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0", "1", "0"],
            check=False,
        )
        return _ok("Sleep command issued.")
    except Exception as exc:
        return _fail(f"Failed to sleep PC: {exc}")


def shutdown_pc() -> dict:
    """Schedule system shutdown in 5 seconds."""
    try:
        subprocess.run(["shutdown", "/s", "/t", "5"], check=False)
        return _ok("Shutdown command issued.")
    except Exception as exc:
        return _fail(f"Failed to shutdown PC: {exc}")


def restart_pc() -> dict:
    """Schedule system restart in 5 seconds."""
    try:
        subprocess.run(["shutdown", "/r", "/t", "5"], check=False)
        return _ok("Restart command issued.")
    except Exception as exc:
        return _fail(f"Failed to restart PC: {exc}")


def create_folder(path: str) -> dict:
    """Create a folder if it does not exist."""
    try:
        os.makedirs(path, exist_ok=True)
        return _ok(f"Created folder: {path}")
    except Exception as exc:
        return _fail(f"Failed to create folder: {exc}")


def create_file(path: str, content: str = "") -> dict:
    """Create a file and optionally write content."""
    try:
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return _ok(f"Created file: {path}")
    except Exception as exc:
        return _fail(f"Failed to create file: {exc}")


def rename_file(old: str, new: str) -> dict:
    """Rename or move a file path."""
    try:
        os.rename(old, new)
        return _ok(f"Renamed {old} to {new}")
    except Exception as exc:
        return _fail(f"Failed to rename file: {exc}")


def move_file(src: str, dst: str) -> dict:
    """Move a file to a new destination."""
    try:
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        return _ok(f"Moved {src} to {dst}")
    except Exception as exc:
        return _fail(f"Failed to move file: {exc}")


def delete_file(path: str) -> dict:
    """Soft-delete a file by moving it into .atlas_trash."""
    try:
        trash_path = rollback.soft_delete(path)
        return _ok("File moved to trash.", trash_path=trash_path)
    except Exception as exc:
        return _fail(f"Failed to delete file: {exc}")


def clipboard_read() -> dict:
    """Read text from the clipboard."""
    try:
        value = pyperclip.paste()
        return _ok(str(value), text=str(value))
    except Exception as exc:
        return _fail(f"Clipboard read failed: {exc}")


def clipboard_write(text: str) -> dict:
    """Write text to the clipboard."""
    try:
        pyperclip.copy(text)
        return _ok("Clipboard updated.")
    except Exception as exc:
        return _fail(f"Clipboard write failed: {exc}")


def get_time() -> dict:
    """Return the current local time."""
    try:
        return _ok(datetime.now().strftime("%H:%M:%S"))
    except Exception as exc:
        return _fail(f"Failed to get time: {exc}")


def run_macro_action(name: str) -> dict:
    """Placeholder macro runner action for ACTION_MAP completeness."""
    try:
        return _fail(f"Macro '{name}' is not configured yet.")
    except Exception as exc:
        return _fail(f"Failed to run macro: {exc}")
