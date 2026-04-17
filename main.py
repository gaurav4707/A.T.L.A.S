"""ATLAS Phase 4 CLI with startup polish, voice support, and session memory."""

from __future__ import annotations
import os
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
import argparse
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
import uvicorn
from rich.console import Console
from rich import box
from rich.panel import Panel
from rich.table import Table

from api.server import app
import context_pruner
import classifier
import executor
import memory
import history
import killswitch
import llm_engine
import macros
import rollback
import security
import settings
import voice
import wake_word

console = Console()
logging.basicConfig(filename="error.log", level=logging.ERROR, format="%(asctime)s | %(message)s")


def _load_config_with_guard() -> dict[str, Any]:
    """Validate raw config JSON then return normalized loaded settings."""
    config_path = Path(__file__).resolve().parent / "config.json"
    try:
        if config_path.exists():
            json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("config.json is broken. Delete it and run atlas --setup.")
        sys.exit(1)
    return settings.load()


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


def _check_ollama_ready() -> None:
    """Ensure Ollama service is available before running CLI commands."""
    try:
        requests.get("http://localhost:11434", timeout=2)
    except requests.RequestException:
        print("Ollama is not running. Start it with: ollama serve")
        sys.exit(1)


def _print_model_hint(config: dict[str, Any]) -> None:
    """Print llama3 suggestion when available but mistral is configured."""
    try:
        result = subprocess.run(
            ["ollama", "list"],
            check=False,
            capture_output=True,
            text=True,
        )
        output = (result.stdout or "").lower()
        if "llama3" in output and str(config.get("model", "")).lower().startswith("mistral"):
            console.print("[dim]Tip: LLaMA 3 8B is available. Set model: llama3 in config.json[/dim]")
    except OSError:
        return


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
    """Start FastAPI on localhost:8000 unless an existing API is already reachable."""
    try:
        response = requests.get("http://localhost:8000/status", timeout=0.5)
        if response.status_code in {200, 401}:
            return
    except requests.RequestException:
        pass

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


def _classify_or_query(text: str, context_str: str) -> dict[str, Any]:
    """Resolve command intent via fast classifier then LLM fallback."""
    classified = classifier.classify(text)
    if classified is not None:
        return classified
    return llm_engine.query(text, context_str)


def _execute_text_command(
    text: str,
    context_str: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute one free-text command and log its outcome in history."""
    started = time.perf_counter()
    parsed = _classify_or_query(text, context_str)

    action = str(parsed.get("action", ""))
    params = parsed.get("params", {})
    if not isinstance(params, dict):
        params = {}

    execution_result = executor.execute(action, params)
    
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

    voice.speak(assistant_response)

    return parsed, execution_result


def _dry_run_panel(text: str, context_str: str) -> None:
    """Print exact dry-run block without executing any action."""
    parsed = _classify_or_query(text, context_str)
    action = str(parsed.get("action", "unknown"))
    params = parsed.get("params", {})
    if not isinstance(params, dict):
        params = {}
    if "path" in params and str(params.get("path", "")).strip():
        target = str(params.get("path", ""))
    else:
        target = "(no file specified)"
    risk = str(parsed.get("risk", "low")).lower()
    gate = "none"
    if risk == "medium":
        gate = "yes confirmation"
    elif risk == "high":
        gate = "PIN required"
    elif risk == "critical":
        gate = "blocked"

    body = (
        f"ACTION: {action}\n"
        f"TARGET: {target}\n"
        f"RISK: {risk.capitalize()}\n"
        f"GATE: {gate}\n"
        "-> Not executed. Remove --dry to run."
    )
    console.print(Panel(body, title="Dry Run", border_style="yellow", box=box.ASCII))


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

    if wake_word.is_listening():
        parts: list[str] = []
        if bool(settings.get("wake_word_enabled")):
            parts.append(f"wake word (say '{wake_word._wake_phrase()}')")
        if bool(settings.get("voice_input")):
            parts.append(f"PTT (hold '{settings.get('voice_key') or 'f8'}')")
        voice_mode = f"Audio loop active ({' + '.join(parts)})" if parts else "Audio loop active"
    elif settings.get("voice_input"):
        hotkey = str(settings.get("voice_key") or "f8")
        voice_mode = f"Push-to-talk ({hotkey})"
    else:
        voice_mode = "Disabled"

    body = (
        f"Model: {payload.get('model')}\n"
        f"Voice Input: {voice_mode}\n"
        f"Voice Output: {payload.get('voice_output')}\n"
        f"PIN Set: {payload.get('pin_set')}\n"
        f"Session Memory: {payload.get('session_memory')}\n"
        f"Uptime (s): {payload.get('uptime_s')}"
    )
    console.print(Panel.fit(body, title="ATLAS Status"))
    return 0


def _show_help_panel() -> None:
    """Render command help using a compact Rich panel."""
    help_table = Table.grid(expand=True)
    help_table.add_column(style="cyan", ratio=2)
    help_table.add_column(ratio=4)
    help_table.add_row("atlas 'cmd'", "Run single command")
    help_table.add_row("atlas", "Start REPL")
    help_table.add_row("atlas --dry 'cmd'", "Preview parse only")
    help_table.add_row("atlas --history", "Show last 20 commands")
    help_table.add_row("atlas --history search kw", "Search history")
    help_table.add_row("atlas --rerun N", "Re-run history row")
    help_table.add_row("atlas --macro list", "List macros")
    help_table.add_row("atlas --macro run NAME", "Run macro")
    help_table.add_row("atlas --macro run NAME X", "Run macro with input")
    help_table.add_row("atlas --chain list", "Alias of macro list")
    help_table.add_row("atlas --chain run NAME", "Alias of macro run")
    help_table.add_row("atlas --chain run NAME X", "Alias with input")
    help_table.add_row("atlas --macro add", "Open macros.json")
    help_table.add_row("atlas --status", "Show status panel")
    help_table.add_row("atlas --setup", "PIN wizard + Ollama check")
    help_table.add_row("atlas --install-cli", "Add console entrypoint")
    console.print(Panel(help_table, title="ATLAS Help", border_style="blue"))


def _parse_args() -> argparse.Namespace:
    """Parse CLI args for all Phase 3 command modes."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", nargs="*")
    parser.add_argument("--dry", dest="dry_command", nargs="+")
    parser.add_argument("--history", dest="history_args", nargs="*")
    parser.add_argument("--rerun", dest="rerun_id", type=int)
    parser.add_argument("--macro", dest="macro_args", nargs="+")
    parser.add_argument("--chain", dest="chain_args", nargs="+")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--install-cli", action="store_true")
    parser.add_argument("--help", action="store_true")
    return parser.parse_args()


def main() -> None:
    """Run the ATLAS Phase 4 CLI modes and interactive REPL."""
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

    # Startup order: load config -> purge -> check Ollama -> PIN -> API -> poll -> voice -> banner.
    config = _load_config_with_guard()
    rollback.auto_purge()
    _check_ollama_ready()
    memory.review_and_expire()
    if not str(config.get("pin_hash", "")).strip():
        security.setup_pin()
        config = settings.load()
    _start_api_server()
    killswitch.register_hotkey()
    context_pruner.start_pruner()

    def _voice_dispatch(voice_text: str) -> None:
        """Dispatch transcribed voice text through the same execution pipeline."""
        try:
            _parsed, voice_result = _execute_text_command(
                voice_text,
                memory.get_context_for_llm(voice_text),
            )
            border = "green" if bool(voice_result.get("success", False)) else "red"
            console.print(Panel.fit(str(voice_result), title="ATLAS Voice", border_style=border))
        except Exception as exc:
            logging.error("Voice dispatch failed: %s", exc)

    # Unified audio loop: handles both wake word and PTT in one stream.
    # Start it if either wake word or PTT voice input is enabled.
    _wake_available = wake_word.is_available()
    _wake_wanted = bool(settings.get("wake_word_enabled"))
    _ptt_wanted = bool(settings.get("voice_input"))

    if _wake_wanted or _ptt_wanted:
        if _wake_wanted and not _wake_available:
            print(
                "[yellow]Wake word model unavailable. PTT only (if voice_input: true in config).[/yellow]",
                flush=True,
            )
        started = wake_word.start_wake_word_listener()
        if started:
            mode_parts = []
            if _wake_wanted and _wake_available:
                mode_parts.append(f"wake word ('{wake_word._wake_phrase()}')")
            if _ptt_wanted:
                mode_parts.append(f"PTT (hold '{settings.get('voice_key') or 'f8'}')")
            print(
                f"[green][voice] Active: {' + '.join(mode_parts)}[/green]",
                flush=True,
            )
        else:
            print(
                "[red][voice] Audio loop failed to start. Check microphone and run as administrator.[/red]",
                flush=True,
            )
    else:
        print("[dim]Voice input disabled — text mode only.[/dim]", flush=True)

    banner = (
        f"ATLAS v1 | Model: {config.get('model')} | "
        f"Voice: {'on' if bool(config.get('voice_input') or config.get('voice_output')) else 'off'} | "
        f"Memory: on | --help for commands"
    )
    console.print(Panel.fit(banner, border_style="blue"))
    _print_model_hint(config)

    if args.status:
        sys.exit(_show_status(config))

    if args.dry_command:
        dry_text = " ".join(args.dry_command)
        _dry_run_panel(dry_text, memory.get_context_for_llm(dry_text))
        return

    if args.history_args is not None:
        if len(args.history_args) >= 2 and args.history_args[0].lower() == "search":
            _render_history(history.search(" ".join(args.history_args[1:])))
        else:
            _render_history(history.list_recent(20))
        return

    if args.rerun_id is not None:
        result = history.rerun(args.rerun_id)
        border = "green" if bool(result.get("success", False)) else "red"
        console.print(Panel.fit(str(result), title="Rerun", border_style=border))
        return

    chain_or_macro_args = args.chain_args if args.chain_args else args.macro_args
    chain_mode = bool(args.chain_args)
    if chain_or_macro_args:
        subcommand = chain_or_macro_args[0].lower()
        if subcommand == "list":
            _render_macro_list(macros.list())
            return
        if subcommand == "add":
            result = macros.add()
            border = "green" if bool(result.get("success", False)) else "red"
            title = "Chain Add" if chain_mode else "Macro Add"
            console.print(Panel.fit(str(result), title=title, border_style=border))
            return
        if subcommand == "run" and len(chain_or_macro_args) >= 2:
            name = chain_or_macro_args[1]
            input_value = " ".join(chain_or_macro_args[2:]) if len(chain_or_macro_args) > 2 else ""
            result = macros.run(name, input_value)
            border = "green" if bool(result.get("success", False)) else "red"
            title = "Chain Run" if chain_mode else "Macro Run"
            console.print(Panel.fit(str(result), title=title, border_style=border))
            return
        usage_label = "--chain" if chain_mode else "--macro"
        panel_title = "Chain" if chain_mode else "Macro"
        console.print(Panel.fit(f"Invalid {usage_label} usage.", title=panel_title, border_style="yellow"))
        return

    if args.command:
        command_text = " ".join(args.command)
        _parsed, result = _execute_text_command(
            command_text,
            memory.get_context_for_llm(command_text),
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
            context_text = memory.get_context_for_llm(text)
            _parsed, result = _execute_text_command(
                text,
                context_text,
            )
            color = "green" if bool(result.get("success", False)) else "red"
            console.print(Panel.fit(str(result), title="ATLAS Result", border_style=color))
        except Exception as exc:
            logging.error("REPL error: %s", exc)
            console.print(f"[red]Something went wrong: {exc}[/red]")


if __name__ == "__main__":
    main()
