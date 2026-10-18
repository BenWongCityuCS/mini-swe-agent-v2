"""Run automation tasks with a fresh agent."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from minisweagent.agents import get_agent
from minisweagent.automation.store import Automation, StateStore
from minisweagent.config import get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.utils.serialize import recursive_merge

logger = logging.getLogger("automation")


def _merge_config(automation: Automation) -> dict:
    config = dict(automation.config or {})
    if config.get("config_file"):
        base = get_config_from_spec(config["config_file"])
        config = recursive_merge(base, {k: v for k, v in config.items() if k != "config_file"})
    return config


def run_automation(
    automation: Automation,
    *,
    workspace: Path,
    state: StateStore,
    runs_root: Path | None = None,
) -> str:
    started = time.time()
    now = datetime.now()
    runs_root = runs_root or (Path.home() / ".mini-swe-agent" / "automations" / "runs")
    run_dir = runs_root / automation.name
    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = run_dir / f"{now:%Y%m%d-%H%M%S}.json"
    log_path = run_dir / f"{now:%Y%m%d-%H%M%S}.log"

    config = _merge_config(automation)
    model_config = dict(config.get("model") or {})
    environment_config = dict(config.get("environment") or {})
    environment_config.setdefault("cwd", str(workspace))
    agent_config = dict(config.get("agent") or {})
    agent_config["output_path"] = output_path

    status = "unknown"
    detail = ""
    try:
        model = get_model(config=model_config)
        environment = get_environment(environment_config, default_type="local")
        agent = get_agent(model, environment, agent_config, default_type="default")
        result = agent.run(automation.task)
        status = str(result.get("exit_status") or "unknown")
        detail = str(result.get("submission") or "")[:2000]
    except Exception as exc:  # noqa: BLE001 - a failing automation must not stop the loop
        logger.exception("automation %s failed", automation.name)
        status = f"Error: {type(exc).__name__}"
        detail = str(exc)

    elapsed = time.time() - started
    state.mark_run(automation.name, status, when=now)
    try:
        log_path.write_text(
            f"[{now.isoformat(timespec='seconds')}] {automation.name}\n"
            f"status: {status}\n"
            f"elapsed_seconds: {elapsed:.1f}\n"
            f"task: {automation.task}\n\n{detail}\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    logger.info("automation %s finished: %s (%.1fs)", automation.name, status, elapsed)
    return status
