"""Persistence for workflow definitions, runs, and artifacts."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import yaml

from minisweagent.extensions.config import WorkflowConfig
from minisweagent.workflow.models import WorkflowDefinition, WorkflowError, WorkflowRun
from minisweagent.workflow.prompts import create_expert_definition


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _safe_name(value: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return (name or "artifact")[:100]


class WorkflowStore:
    def __init__(self, config: WorkflowConfig, workspace: str | Path) -> None:
        self.config = config
        self.workspace = Path(workspace).expanduser().resolve()
        self.definition_dirs: list[Path] = []
        for spec in config.dirs:
            path = Path(spec).expanduser()
            if not path.is_absolute():
                path = self.workspace / path
            self.definition_dirs.append(path.resolve())
        run_dir = Path(config.run_dir).expanduser()
        if not run_dir.is_absolute():
            run_dir = self.workspace / run_dir
        self.run_dir = run_dir.resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)

    # -- definitions -------------------------------------------------------

    def builtin_definitions(self) -> dict[str, WorkflowDefinition]:
        return {"expert": create_expert_definition()}

    def load_definition(self, spec: str) -> WorkflowDefinition:
        spec = str(spec or self.config.default_definition).strip()
        builtins = self.builtin_definitions()
        if spec in builtins:
            return builtins[spec].model_copy(deep=True)

        candidate = Path(spec).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        if candidate.is_file():
            return self._read_definition(candidate)

        for directory in self.definition_dirs:
            for suffix in (".yaml", ".yml", ".json"):
                path = directory / f"{spec}{suffix}"
                if path.is_file():
                    return self._read_definition(path)
        raise WorkflowError(f"Unknown workflow {spec!r}")

    def _read_definition(self, path: Path) -> WorkflowDefinition:
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
            if not isinstance(data, dict):
                raise ValueError("definition must be a mapping")
            if "workflow" in data and isinstance(data["workflow"], dict):
                data = data["workflow"]
            return WorkflowDefinition.model_validate(data)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise WorkflowError(f"Could not load workflow {path}: {exc}") from exc

    def save_definition(self, definition: WorkflowDefinition, *, scope: str = "workspace") -> Path:
        directory = self.definition_dirs[0] if scope != "user" or len(self.definition_dirs) < 2 else self.definition_dirs[1]
        path = directory / f"{_safe_name(definition.id)}.yaml"
        payload = definition.model_dump(mode="json")
        _atomic_write(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
        return path

    def list_definitions(self) -> list[dict]:
        entries = [
            {"id": definition.id, "title": definition.title, "builtin": True, "path": None}
            for definition in self.builtin_definitions().values()
        ]
        seen = {entry["id"] for entry in entries}
        for directory in self.definition_dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*")):
                if path.suffix not in {".yaml", ".yml", ".json"}:
                    continue
                try:
                    definition = self._read_definition(path)
                except WorkflowError:
                    continue
                if definition.id in seen:
                    continue
                seen.add(definition.id)
                entries.append(
                    {"id": definition.id, "title": definition.title, "builtin": False, "path": str(path)}
                )
        return entries

    # -- runs --------------------------------------------------------------

    def new_run(self, definition: WorkflowDefinition, task: str, *, max_concurrent: int | None = None) -> WorkflowRun:
        return WorkflowRun(
            definition_id=definition.id,
            task=task,
            max_concurrent=max_concurrent or definition.max_concurrent,
        )

    def save_run(self, run: WorkflowRun) -> Path:
        run.updated_at = time.time()
        path = self.run_dir / f"{_safe_name(run.id)}.json"
        _atomic_write(path, json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return path

    def load_run(self, run_id: str) -> WorkflowRun:
        candidate = Path(run_id).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        if not candidate.is_file():
            candidate = self.run_dir / f"{_safe_name(run_id)}.json"
        if not candidate.is_file():
            raise WorkflowError(f"Unknown workflow run {run_id!r}")
        try:
            return WorkflowRun.model_validate(json.loads(candidate.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"Could not load workflow run {candidate}: {exc}") from exc

    def list_runs(self, limit: int = 20) -> list[dict]:
        entries: list[dict] = []
        for path in self.run_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            entries.append(
                {
                    "id": data.get("id"),
                    "definition": data.get("definition_id"),
                    "status": data.get("status"),
                    "task": str(data.get("task") or "")[:120],
                    "updated_at": float(data.get("updated_at") or 0.0),
                    "cost": float(data.get("cost") or 0.0),
                    "path": str(path),
                }
            )
        entries.sort(key=lambda entry: entry["updated_at"], reverse=True)
        return entries[:limit]

    def write_artifact(self, run: WorkflowRun, name: str, content: str, *, label: str | None = None) -> str:
        safe_name = _safe_name(name)
        if not safe_name.endswith(".md"):
            safe_name += ".md"
        relative = Path("artifacts") / safe_name
        path = self.run_dir / _safe_name(run.id) / relative
        _atomic_write(path, content)
        entry = {"label": label or name, "path": str(path), "kind": "markdown"}
        run.artifacts.append(entry)
        return str(path)

    def run_artifact_dir(self, run: WorkflowRun) -> Path:
        path = self.run_dir / _safe_name(run.id) / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path
