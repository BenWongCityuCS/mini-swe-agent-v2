#!/usr/bin/env python3
"""Run and inspect durable workflows (`mini-workflow`)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from minisweagent.agents.default import DefaultAgent
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.extensions.config import WorkflowConfig
from minisweagent.models import get_model
from minisweagent.utils.serialize import UNSET, recursive_merge
from minisweagent.workflow import (
    WorkflowEngine,
    WorkflowStore,
    build_workflow_runner,
    format_run,
)

console = Console(highlight=False)
app = typer.Typer(rich_markup_mode="rich")

DEFAULT_CONFIG_FILE = Path(os.getenv("MSWEA_MINI_CONFIG_PATH", builtin_config_dir / "mini.yaml"))


def _store() -> WorkflowStore:
    return WorkflowStore(WorkflowConfig(enabled=True), os.getcwd())


@app.command("list")
def list_workflows() -> Any:
    """List workflow definitions and recent runs."""
    store = _store()
    console.print("[bold]Definitions[/bold]")
    for entry in store.list_definitions():
        marker = " (built-in)" if entry["builtin"] else f" ({entry['path']})"
        console.print(f"- [green]{entry['id']}[/green]: {entry['title'] or entry['id']}{marker}")
    runs = store.list_runs()
    if runs:
        console.print("\n[bold]Recent runs[/bold]")
        for run in runs:
            console.print(
                f"- {run['id']}: {run['definition']} {run['status']} "
                f"cost=${run['cost']:.4f} {run['task']}"
            )
    return None


@app.command("status")
def status(run_id: str = typer.Argument("", help="Run id (omit to list)")) -> Any:
    """Show a workflow run's phases, nodes, and artifacts."""
    store = _store()
    if not run_id:
        runs = store.list_runs()
        if not runs:
            console.print("No workflow runs.")
            return None
        for run in runs:
            console.print(
                f"- {run['id']}: {run['definition']} {run['status']} "
                f"cost=${run['cost']:.4f} {run['task']}"
            )
        return None
    console.print(format_run(store.load_run(run_id)))
    return None


@app.command("run")
def run(
    workflow: str = typer.Argument("expert", help="Workflow id or path"),
    task: str | None = typer.Option(None, "-t", "--task", help="Task for a new run"),
    resume: str | None = typer.Option(None, "--resume", help="Run id or path to resume"),
    config_spec: list[str] = typer.Option([str(DEFAULT_CONFIG_FILE)], "-c", "--config"),
    model_name: str | None = typer.Option(None, "-m", "--model"),
    max_concurrent: int | None = typer.Option(None, "--max-concurrent"),
) -> Any:
    """Run a workflow in this environment."""
    configs = [get_config_from_spec(spec) for spec in config_spec]
    configs.append(
        {
            "agent": {"workflow": {"enabled": True}},
            "model": {"model_name": model_name or UNSET},
        }
    )
    config = recursive_merge(*configs)
    agent_config = dict(config.get("agent") or {})
    agent_config.pop("agent_class", None)
    agent_config.pop("mode", None)
    agent_config.setdefault("workflow", {})["enabled"] = True

    model = get_model(config=config.get("model", {}))
    env = get_environment(config.get("environment", {}), default_type="local")
    agent = DefaultAgent(model, env, **agent_config)
    try:
        store = agent._workflow_store  # noqa: SLF001
        if store is None:
            raise RuntimeError("workflow support was not initialized")
        if resume:
            existing = store.load_run(resume)
            definition = store.load_definition(existing.definition_id)
            engine = WorkflowEngine(
                definition,
                existing.task,
                store,
                build_workflow_runner(agent),
                max_concurrent=max_concurrent,
            )
            result = engine.run(resume_run_id=existing.id)
        else:
            if not task:
                raise typer.BadParameter("--task is required for a new run")
            definition = store.load_definition(workflow)
            engine = WorkflowEngine(
                definition,
                task,
                store,
                build_workflow_runner(agent),
                max_concurrent=max_concurrent,
            )
            result = engine.run()
        console.print(format_run(result))
        return result
    finally:
        agent.close()


if __name__ == "__main__":
    app()
