#!/usr/bin/env python3

"""Run mini-SWE-agent in your local environment. This is the default executable `mini`."""
# Read this first: https://mini-swe-agent.com/latest/usage/mini/  (usage)

import os
import time
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from minisweagent import global_config_dir
from minisweagent.agents import get_agent
from minisweagent.agents.session import SessionStore
from minisweagent.agents.utils.prompt_user import _multiline_prompt
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.extensions.config import SessionConfig
from minisweagent.models import get_model
from minisweagent.run.utilities.config import configure_if_first_time
from minisweagent.utils.serialize import UNSET, recursive_merge

DEFAULT_CONFIG_FILE = Path(os.getenv("MSWEA_MINI_CONFIG_PATH", builtin_config_dir / "mini.yaml"))
DEFAULT_OUTPUT_FILE = global_config_dir / "last_mini_run.traj.json"


_HELP_TEXT = """Run mini-SWE-agent in your local environment.

[not dim]
More information about the usage: [bold green]https://mini-swe-agent.com/latest/usage/mini/[/bold green]
[/not dim]
"""

_CONFIG_SPEC_HELP_TEXT = """Path to config files, filenames, or key-value pairs.

[bold red]IMPORTANT:[/bold red] [red]If you set this option, the default config file will not be used.[/red]
So you need to explicitly set it e.g., with [bold green]-c mini.yaml <other options>[/bold green]

Multiple configs will be recursively merged.

Examples:

[bold red]-c model.model_kwargs.temperature=0[/bold red] [red]You forgot to add the default config file! See above.[/red]

[bold green]-c mini.yaml -c model.model_kwargs.temperature=0.5[/bold green]

[bold green]-c swebench.yaml agent.mode=yolo[/bold green]
"""

console = Console(highlight=False)
app = typer.Typer(rich_markup_mode="rich")


# fmt: off
@app.command(help=_HELP_TEXT)
def main(
    model_name: str | None = typer.Option(None, "-m", "--model", help="Model to use",),
    model_class: str | None = typer.Option(None, "--model-class", help="Model class to use (e.g., 'litellm' or 'minisweagent.models.litellm_model.LitellmModel')", rich_help_panel="Advanced"),
    agent_class: str | None = typer.Option(None, "--agent-class", help="Agent class to use (e.g., 'interactive' or 'minisweagent.agents.interactive.InteractiveAgent')", rich_help_panel="Advanced"),
    environment_class: str | None = typer.Option(None, "--environment-class", help="Environment class to use (e.g., 'local' or 'minisweagent.environments.local.LocalEnvironment')", rich_help_panel="Advanced"),
    task: str | None = typer.Option(None, "-t", "--task", help="Task/problem statement", show_default=False),
    yolo: bool = typer.Option(False, "-y", "--yolo", help="Run without confirmation"),
    cost_limit: float | None = typer.Option(None, "-l", "--cost-limit", help="Cost limit. Set to 0 to disable."),
    config_spec: list[str] = typer.Option([str(DEFAULT_CONFIG_FILE)], "-c", "--config", help=_CONFIG_SPEC_HELP_TEXT),
    output: Path | None = typer.Option(DEFAULT_OUTPUT_FILE, "-o", "--output", help="Output trajectory file"),
    exit_immediately: bool = typer.Option(False, "--exit-immediately", help="Exit immediately when the agent wants to finish instead of prompting.", rich_help_panel="Advanced"),
    resume: str | None = typer.Option(None, "--resume", help="Resume a saved session id or path"),
    continue_session: bool = typer.Option(False, "--continue", help="Resume the most recent session in this workspace"),
    session_id: str | None = typer.Option(None, "--session-id", help="Use a specific id for a new session"),
    list_sessions: bool = typer.Option(False, "--list-sessions", help="List saved sessions and exit"),
) -> Any:
    # fmt: on
    # Direct Python callers (tests, scripts) can omit options; Typer's
    # OptionInfo defaults are truthy, so normalize them before using booleans.
    resume = resume if isinstance(resume, str) else None
    continue_session = continue_session if isinstance(continue_session, bool) else False
    session_id = session_id if isinstance(session_id, str) else None
    list_sessions = list_sessions if isinstance(list_sessions, bool) else False

    if resume and continue_session:
        raise typer.BadParameter("--resume and --continue are mutually exclusive")

    if list_sessions:
        store = SessionStore(SessionConfig(enabled=True), os.getcwd())
        entries = store.list()
        if not entries:
            console.print("[yellow]No saved sessions.[/yellow]")
        for entry in entries:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["updated_at"]))
            console.print(
                f"[bold green]{entry['id']}[/bold green]  {when}  {entry['status'] or 'running'}  "
                f"{entry['n_calls']} calls  {entry['workspace']}\n  {entry['task']}"
            )
        return None

    configure_if_first_time()

    # Build the config from the command line arguments
    console.print(f"Building agent config from specs: [bold green]{config_spec}[/bold green]")
    configs = [get_config_from_spec(spec) for spec in config_spec]
    configs.append({
        "run": {
            "task": task or UNSET,
        },
        "agent": {
            "agent_class": agent_class or UNSET,
            "mode": "yolo" if yolo else UNSET,
            "cost_limit": cost_limit if cost_limit is not None else UNSET,
            "confirm_exit": False if exit_immediately else UNSET,
            "output_path": output or UNSET,
            "session": {
                "enabled": True if (resume or continue_session or session_id) else UNSET,
                "id": session_id or UNSET,
            },
        },
        "model": {
            "model_class": model_class or UNSET,
            "model_name": model_name or UNSET,
        },
        "environment": {
            "environment_class": environment_class or UNSET,
        },
    })
    config = recursive_merge(*configs)

    resume_spec = resume or ("latest" if continue_session else None)
    if (run_task := config.get("run", {}).get("task", UNSET)) is UNSET:
        if resume_spec:
            run_task = ""
        else:
            console.print("[bold yellow]What do you want to do?")
            run_task = _multiline_prompt()
            console.print("[bold green]Got that, thanks![/bold green]")

    model = get_model(config=config.get("model", {}))
    env = get_environment(config.get("environment", {}), default_type="local")
    agent = get_agent(model, env, config.get("agent", {}), default_type="interactive")
    if resume_spec:
        agent.run(run_task, resume=resume_spec)
    else:
        agent.run(run_task)
    if (output_path := config.get("agent", {}).get("output_path")):
        console.print(f"Saved trajectory to [bold green]'{output_path}'[/bold green]")
    if getattr(agent, "_session_store", None) is not None:
        console.print(f"Session: [bold green]{agent._session_id}[/bold green]")
    return agent


if __name__ == "__main__":
    app()
