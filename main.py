"""ATLAS Phase 3 CLI with history, macros, dry-run, and FastAPI integration."""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from api.server import app
import classifier
import executor
import history
import llm_engine
import macros
import security
import settings

console = Console()


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


def _run_setup() -> int:
    """Run setup workflow: PIN wizard and Ollama availability check."""
    if not security.setup_pin():
        console.print("[red]PIN setup failed.[/red]")
        return 1
    try:
        requests.get("http://localhost:11434", timeout=2)
    except requests.RequestException:
        console.print("[red]Ollama connection failed. Start it with: ollama serve[/red]")
        return 1
    console.print("[green]Setup complete: PIN configured and Ollama reachable.[/green]")
    return 0


def _install_cli_entrypoint() -> int:
    """Create or update pyproject.toml with atlas console script entrypoint."""
    pyproject_path = Path(__file__).resolve().parent / "pyproject.toml"
    if not pyproject_path.exists():
        pyproject_path.write_text(
            """[project]
name = "atlas"
version = "0.1.0"

[project.scripts]
atlas = "main:main"
""",
            encoding="utf-8",
        )
        console.print("[green]Created pyproject.toml with atlas entrypoint.[/green]")
        return 0

    content = pyproject_path.read_text(encoding="utf-8")
    if "[project.scripts]" in content and "atlas = \"main:main\"" in content:
        console.print("[yellow]atlas entrypoint already exists in pyproject.toml.[/yellow]")
        return 0

    if "[project.scripts]" in content:
        updated = content.rstrip() + "\natlas = \"main:main\"\n"
    else:
        updated = content.rstrip() + "\n\n[project.scripts]\natlas = \"main:main\"\n"
    pyproject_path.write_text(updated, encoding="utf-8")
    console.print("[green]Updated pyproject.toml with atlas entrypoint.[/green]")
    return 0


def _start_api_server() -> None:
    """Start FastAPI on localhost:8000 in a daemon thread and wait until ready."""
    thread = threading.Thread(
        target=uvicorn.run,
        args=(app,),
        kwargs={"host": "127.0.0.1", "port": 8000, "log_level": "error"},
        daemon=True,
    )
    thread.start()

    for _ in range(20):
        try:
            response = requests.get("http://localhost:8000/status", timeout=0.5)
            if response.status_code in {200, 401}:
                break
        except requests.RequestException:
            pass
        time.sleep(0.5)


def _classify_or_query(text: str, llm_context: list[str]) -> dict[str, Any]:
    """Resolve command intent via fast classifier then LLM fallback."""
    classified = classifier.classify(text)
    if classified is not None:
        return classified
    return llm_engine.query(text, llm_context)


def _execute_text_command(
    text: str,
    session_context: list[str],
    session_memory_enabled: bool,
    session_memory_turns: int,
) -> dict[str, Any]:
    """Execute one free-text command and log its outcome in history."""
    llm_context = session_context if session_memory_enabled else []
    started = time.perf_counter()
    parsed = _classify_or_query(text, llm_context)

    action = str(parsed.get("action", ""))
    params = parsed.get("params", {})
    if not isinstance(params, dict):
        params = {}

    execution_result = executor.execute(action, params)
    latency_ms = int((time.perf_counter() - started) * 1000)
    history.log(
        raw=text,
        action=action,
        params=params,
        success=bool(execution_result.get("success", False)),
        latency_ms=latency_ms,
        risk=str(parsed.get("risk", "")),
    )

    if session_memory_enabled:
        _update_session_context(session_context, text, parsed, session_memory_turns)

    return execution_result


def _dry_run_panel(text: str, session_context: list[str], session_memory_enabled: bool) -> None:
    """Print exact dry-run block without executing any action."""
    llm_context = session_context if session_memory_enabled else []
    parsed = _classify_or_query(text, llm_context)
    action = str(parsed.get("action", "unknown"))
    params = parsed.get("params", {})
    if not isinstance(params, dict):
        params = {}
    target = str(
        params.get("path")
        or params.get("src")
        or params.get("dst")
        or params.get("app")
        or params.get("url")
        or "-"
    )
    risk = str(parsed.get("risk", "low")).lower()
    gate = "none"
    if risk == "medium":
        gate = "yes confirmation"
    elif risk == "high":
        gate = "PIN required"
    elif risk == "critical":
        gate = "blocked"

    print("┌─ Dry Run ──────────────────────────────┐")
    print(f"│ ACTION: {action} │")
    print(f"│ TARGET: {target} │")
    print(f"│ RISK: {risk.capitalize()} │")
    print(f"│ GATE: {gate} │")
    print("│ → Not executed. Remove --dry to run. │")
    print("└────────────────────────────────────────┘")


def _render_history(rows: list[dict[str, Any]]) -> None:
    """Render history rows as a Rich table."""
    table = Table(title="ATLAS History")
    table.add_column("ID", style="cyan")
    table.add_column("Timestamp")
    table.add_column("Command")
    table.add_column("Action")
    table.add_column("Success")
    for row in rows:
        table.add_row(
            str(row.get("id", "")),
            str(row.get("timestamp", "")),
            str(row.get("raw_command", "")),
            str(row.get("parsed_action", "")),
            str(bool(row.get("success", 0))),
        )
    console.print(table)


def _render_macro_list(macro_map: dict[str, Any]) -> None:
    """Render configured macros as a Rich table."""
    table = Table(title="ATLAS Macros")
    table.add_column("Name", style="cyan")
    table.add_column("Definition")
    for name, value in macro_map.items():
        table.add_row(str(name), str(value))
    console.print(table)


def _show_status(config: dict[str, Any]) -> int:
    """Call local API status endpoint with token and render as a panel."""
    token = str(config.get("api_token") or "")
    try:
        response = requests.get(
            "http://localhost:8000/status",
            headers={"X-ATLAS-Token": token},
            timeout=2,
        )
        if response.status_code != 200:
            console.print(Panel.fit(f"Status request failed: HTTP {response.status_code}", title="Status"))
            return 1
        payload = response.json()
    except requests.RequestException as exc:
        console.print(Panel.fit(f"Status request failed: {exc}", title="Status"))
        return 1

    body = (
        f"Model: {payload.get('model')}\n"
        f"Voice Input: {payload.get('voice_input')}\n"
        f"Voice Output: {payload.get('voice_output')}\n"
        f"PIN Set: {payload.get('pin_set')}\n"
        f"Session Memory: {payload.get('session_memory')}\n"
        f"Uptime (s): {payload.get('uptime_s')}"
    )
    console.print(Panel.fit(body, title="ATLAS Status"))
    return 0


def _show_help_panel() -> None:
    """Render command help using a compact Rich panel."""
    lines = [
        "atlas 'cmd'                Run single command",
        "atlas                      Start REPL",
        "atlas --dry 'cmd'          Preview parse only",
        "atlas --history            Show last 20 commands",
        "atlas --history search kw  Search history",
        "atlas --rerun N            Re-run history row",
        "atlas --macro list         List macros",
        "atlas --macro run NAME     Run macro",
        "atlas --macro run NAME X   Run macro with input",
        "atlas --macro add          Open macros.json",
        "atlas --status             Show status panel",
        "atlas --setup              PIN + Ollama setup",
        "atlas --install-cli        Add console entrypoint",
    ]
    console.print(Panel("\n".join(lines), title="ATLAS Help"))


def _parse_args() -> argparse.Namespace:
    """Parse CLI args for all Phase 3 command modes."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", nargs="*")
    parser.add_argument("--dry", dest="dry_command", nargs="+")
    parser.add_argument("--history", dest="history_args", nargs="*")
    parser.add_argument("--rerun", dest="rerun_id", type=int)
    parser.add_argument("--macro", dest="macro_args", nargs="+")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--install-cli", action="store_true")
    parser.add_argument("--help", action="store_true")
    return parser.parse_args()


def main() -> None:
    """Run the ATLAS Phase 3 CLI modes and interactive REPL."""
    try:
        import readline  # type: ignore # noqa: F401
    except Exception:
        try:
            import pyreadline3  # type: ignore # noqa: F401
        except Exception:
            pass

    args = _parse_args()

    if args.help:
        _show_help_panel()
        return

    if args.install_cli:
        sys.exit(_install_cli_entrypoint())

    if args.setup:
        sys.exit(_run_setup())

    config = settings.load()
    _start_api_server()

    if config.get("needs_pin_setup", False):
        console.print("First-run setup required.")
        if not security.setup_pin():
            console.print("PIN setup incomplete. Exiting ATLAS.")
            return

    session_context: list[str] = []
    session_memory_enabled = bool(config.get("session_memory", False))
    session_memory_turns = int(config.get("session_memory_turns", 8))

    if args.status:
        sys.exit(_show_status(config))

    if args.dry_command:
        _dry_run_panel(" ".join(args.dry_command), session_context, session_memory_enabled)
        return

    if args.history_args is not None:
        if len(args.history_args) >= 2 and args.history_args[0].lower() == "search":
            _render_history(history.search(" ".join(args.history_args[1:])))
        else:
            _render_history(history.list_recent(20))
        return

    if args.rerun_id is not None:
        result = history.rerun(args.rerun_id)
        console.print(Panel.fit(str(result), title="Rerun"))
        return

    if args.macro_args:
        subcommand = args.macro_args[0].lower()
        if subcommand == "list":
            _render_macro_list(macros.list())
            return
        if subcommand == "add":
            result = macros.add()
            console.print(Panel.fit(str(result), title="Macro Add"))
            return
        if subcommand == "run" and len(args.macro_args) >= 2:
            name = args.macro_args[1]
            input_value = " ".join(args.macro_args[2:]) if len(args.macro_args) > 2 else ""
            result = macros.run(name, input_value)
            console.print(Panel.fit(str(result), title="Macro Run"))
            return
        console.print(Panel.fit("Invalid --macro usage.", title="Macro"))
        return

    if args.command:
        result = _execute_text_command(
            " ".join(args.command),
            session_context,
            session_memory_enabled,
            session_memory_turns,
        )
        color = "green" if bool(result.get("success", False)) else "red"
        console.print(Panel.fit(str(result), title="ATLAS Result", border_style=color))
        return

    while True:
        try:
            text = input("ATLAS > ")
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            sys.exit(0)

        if text.strip().lower() in {"exit", "quit"}:
            print("Goodbye.")
            break

        try:
            result = _execute_text_command(
                text,
                session_context,
                session_memory_enabled,
                session_memory_turns,
            )
            color = "green" if bool(result.get("success", False)) else "red"
            console.print(Panel.fit(str(result), title="ATLAS Result", border_style=color))
        except Exception as exc:
            console.print(f"[red]Something went wrong: {exc}[/red]")


if __name__ == "__main__":
    main()
