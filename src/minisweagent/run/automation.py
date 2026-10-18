#!/usr/bin/env python3
"""CLI for scheduled automation tasks.

    mini-automation list
    mini-automation run nightly-tests
    mini-automation tick
    mini-automation serve --interval 60
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from minisweagent.automation.runner import run_automation
from minisweagent.automation.schedule import is_due
from minisweagent.automation.store import StateStore, discover_automations

console = Console()
app = typer.Typer(help="Run scheduled mini-swe-agent tasks stored as YAML files.")


def _context(workspace: str, dirs: list[str] | None, state_file: str | None):
    workspace_path = Path(workspace).expanduser().resolve()
    automation_dirs = dirs or ["./automations", "~/.mini-swe-agent/automations"]
    state_path = (
        Path(state_file).expanduser()
        if state_file
        else workspace_path / ".mini-swe-agent" / "automations-state.json"
    )
    return workspace_path, automation_dirs, StateStore(state_path)


def _due_automations(workspace: Path, dirs: list[str], state: StateStore):
    now = datetime.now()
    for automation in discover_automations(dirs, workspace).values():
        if not automation.enabled:
            continue
        if is_due(
            schedule=automation.schedule,
            interval_seconds=automation.interval_seconds,
            last_run=state.last_run(automation.name),
            now=now,
        ):
            yield automation


@app.command("list")
def list_automations(
    workspace: str = typer.Option(".", "--workspace", "-w"),
    dirs: list[str] = typer.Option(None, "--dir", "-d", help="Automation directory (repeatable)"),
    state_file: str = typer.Option(None, "--state-file"),
) -> None:
    workspace_path, automation_dirs, state = _context(workspace, dirs, state_file)
    automations = discover_automations(automation_dirs, workspace_path)
    if not automations:
        console.print("[yellow]No automations found.[/yellow]")
        return
    now = datetime.now()
    for automation in automations.values():
        last = state.last_run(automation.name)
        due = automation.enabled and is_due(
            schedule=automation.schedule,
            interval_seconds=automation.interval_seconds,
            last_run=last,
            now=now,
        )
        schedule = automation.schedule or f"every {automation.interval_seconds:g}s"
        console.print(
            f"- [bold]{automation.name}[/bold]  schedule={schedule}  "
            f"enabled={automation.enabled}  due={due}  last_run={last.isoformat() if last else 'never'}"
        )


@app.command()
def run(
    name: str,
    workspace: str = typer.Option(".", "--workspace", "-w"),
    dirs: list[str] = typer.Option(None, "--dir", "-d"),
    state_file: str = typer.Option(None, "--state-file"),
) -> None:
    workspace_path, automation_dirs, state = _context(workspace, dirs, state_file)
    automations = discover_automations(automation_dirs, workspace_path)
    automation = automations.get(name)
    if automation is None:
        console.print(f"[red]Unknown automation: {name}[/red]")
        raise typer.Exit(1)
    status = run_automation(automation, workspace=workspace_path, state=state)
    console.print(f"[bold]{name}[/bold] finished: {status}")


@app.command()
def tick(
    workspace: str = typer.Option(".", "--workspace", "-w"),
    dirs: list[str] = typer.Option(None, "--dir", "-d"),
    state_file: str = typer.Option(None, "--state-file"),
) -> None:
    workspace_path, automation_dirs, state = _context(workspace, dirs, state_file)
    ran = 0
    for automation in list(_due_automations(workspace_path, automation_dirs, state)):
        console.print(f"Running [bold]{automation.name}[/bold]...")
        status = run_automation(automation, workspace=workspace_path, state=state)
        console.print(f"  -> {status}")
        ran += 1
    if ran == 0:
        console.print("No automations are due.")


@app.command()
def serve(
    interval: int = typer.Option(60, "--interval", help="Seconds between ticks"),
    workspace: str = typer.Option(".", "--workspace", "-w"),
    dirs: list[str] = typer.Option(None, "--dir", "-d"),
    state_file: str = typer.Option(None, "--state-file"),
) -> None:
    console.print(f"Automation loop started (tick every {interval}s). Ctrl-C to stop.")
    try:
        while True:
            tick(workspace=workspace, dirs=dirs, state_file=state_file)
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("Stopped.")


if __name__ == "__main__":
    app()
