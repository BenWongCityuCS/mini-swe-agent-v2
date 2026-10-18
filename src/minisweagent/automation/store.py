"""Automation definitions and run state."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from minisweagent.automation.schedule import parse_interval

logger = logging.getLogger("automation")


@dataclass
class Automation:
    name: str
    path: Path
    task: str
    schedule: str | None = None
    interval_seconds: float | None = None
    enabled: bool = True
    config: dict = field(default_factory=dict)
    output_dir: str | None = None


def _load_one(path: Path) -> Automation | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("could not read automation %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    task = str(data.get("task") or "").strip()
    if not task:
        logger.warning("automation %s has no task", path)
        return None
    schedule = data.get("schedule")
    interval = data.get("interval")
    interval_seconds = parse_interval(interval) if interval is not None else None
    schedule = str(schedule).strip() if schedule else None
    if not schedule and interval_seconds is None:
        logger.warning("automation %s has neither schedule nor interval", path)
        return None
    return Automation(
        name=str(data.get("name") or path.stem),
        path=path.resolve(),
        task=task,
        schedule=schedule,
        interval_seconds=interval_seconds,
        enabled=bool(data.get("enabled", True)),
        config=data.get("config") or {},
        output_dir=str(data["output_dir"]) if data.get("output_dir") else None,
    )


def _resolve_dirs(dirs: list[str], workspace: Path) -> list[Path]:
    resolved = []
    for value in dirs:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = workspace / path
        if path.is_dir():
            resolved.append(path.resolve())
    return resolved


def discover_automations(dirs: list[str], workspace: Path) -> dict[str, Automation]:
    automations: dict[str, Automation] = {}
    for directory in _resolve_dirs(dirs, workspace):
        for pattern in ("*.yaml", "*.yml", "*.json"):
            for path in sorted(directory.glob(pattern)):
                automation = _load_one(path)
                if automation is not None:
                    automations[automation.name] = automation
    return automations


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, dict):
            self.data = raw

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.warning("could not write automation state %s: %s", self.path, exc)

    def last_run(self, name: str) -> datetime | None:
        raw = self.data.get(name, {}).get("last_run")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def mark_run(self, name: str, status: str, when: datetime | None = None) -> None:
        when = when or datetime.now()
        entry = self.data.setdefault(name, {})
        entry["last_run"] = when.isoformat(timespec="seconds")
        entry["last_status"] = status
        entry["runs"] = int(entry.get("runs", 0)) + 1
        self.save()

    def set_value(self, name: str, key: str, value) -> None:
        self.data.setdefault(name, {})[key] = value
        self.save()
