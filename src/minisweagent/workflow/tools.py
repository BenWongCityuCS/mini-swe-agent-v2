"""Workflow tools exposed to the main agent and to workflow subagents."""

from __future__ import annotations

import json
import threading

import yaml

from minisweagent.exceptions import Submitted
from minisweagent.extensions.config import WorkflowConfig
from minisweagent.extensions.registry import Tool, err, ok
from minisweagent.workflow.engine import Runner, WorkflowEngine, format_run
from minisweagent.workflow.models import WorkflowDefinition, WorkflowError
from minisweagent.workflow.store import WorkflowStore
from minisweagent.workflow.validation import validate_value


def build_workflow_tools(
    store: WorkflowStore,
    runner: Runner,
    config: WorkflowConfig,
    *,
    background_manager=None,
    owner: str = "",
) -> list[Tool]:
    def create_handler(args: dict) -> dict:
        raw = args.get("definition")
        path = args.get("path")
        try:
            if path:
                from pathlib import Path

                text = Path(str(path)).expanduser().read_text(encoding="utf-8")
                raw = json.loads(text) if str(path).endswith(".json") else yaml.safe_load(text)
            if isinstance(raw, str):
                raw = yaml.safe_load(raw)
            if not isinstance(raw, dict):
                return err("definition must be a mapping, a YAML/JSON string, or a path")
            if "workflow" in raw and isinstance(raw["workflow"], dict):
                raw = raw["workflow"]
            definition = WorkflowDefinition.model_validate(raw)
            path = store.save_definition(definition, scope=str(args.get("scope") or "workspace"))
        except (OSError, ValueError, yaml.YAMLError, WorkflowError) as exc:
            return err(f"Could not save workflow: {exc}")
        return ok(f"Saved workflow {definition.id} ({len(definition.phases)} phases) to {path}.")

    def list_handler(args: dict) -> dict:
        lines = ["Workflow definitions:"]
        for entry in store.list_definitions():
            marker = " (built-in)" if entry["builtin"] else f" ({entry['path']})"
            lines.append(f"- {entry['id']}: {entry['title'] or entry['id']}{marker}")
        runs = store.list_runs(limit=10)
        if runs:
            lines.extend(["", "Recent runs:"])
            for run in runs:
                lines.append(
                    f"- {run['id']}: {run['definition']} {run['status']} "
                    f"cost=${run['cost']:.4f} {run['task']}"
                )
        return ok("\n".join(lines))

    def run_handler(args: dict) -> dict:
        resume = str(args.get("resume") or "").strip()
        background = bool(args.get("background"))
        max_concurrent = args.get("max_concurrent")
        try:
            max_concurrent = int(max_concurrent) if max_concurrent else None
        except (TypeError, ValueError):
            return err("max_concurrent must be an integer")

        if resume:
            try:
                existing = store.load_run(resume)
                definition = store.load_definition(existing.definition_id)
            except WorkflowError as exc:
                return err(str(exc))
            engine = WorkflowEngine(
                definition, existing.task, store, runner, max_concurrent=max_concurrent
            )
            run = existing
        else:
            workflow = str(args.get("workflow") or config.default_definition)
            task = str(args.get("task") or "").strip()
            if not task:
                return err("task is required for a new workflow run")
            try:
                definition = store.load_definition(workflow)
            except WorkflowError as exc:
                return err(str(exc))
            engine = WorkflowEngine(definition, task, store, runner, max_concurrent=max_concurrent)
            run = store.new_run(definition, task, max_concurrent=max_concurrent)
            store.save_run(run)

        if background:
            if background_manager is None:
                return err("Background tasks are disabled (agent.background.enabled).")
            def job(cancel_event: threading.Event) -> dict:
                engine.cancel_event = cancel_event
                result = engine.run(resume_run_id=run.id)
                return {
                    "exit_status": result.status,
                    "submission": format_run(result),
                    "run_id": result.id,
                }

            started = background_manager.start_callable(
                job, label=f"workflow {definition.id} {run.id}", owner=owner, kind="workflow"
            )
            if started.get("returncode") != 0:
                return started
            return ok(
                f"Started workflow {run.id} ({definition.id}) as {started.get('task_id')}. "
                f"Use TaskOutput(task_id={started.get('task_id')!r}) or WorkflowStatus(run_id={run.id!r}).",
                task_id=started.get("task_id"),
                run_id=run.id,
            )

        result = engine.run(resume_run_id=run.id)
        return ok(format_run(result))

    def status_handler(args: dict) -> dict:
        run_id = str(args.get("run_id") or "").strip()
        if not run_id:
            runs = store.list_runs(limit=int(args.get("limit") or 20))
            if not runs:
                return ok("No workflow runs.")
            lines = ["Workflow runs:"]
            lines.extend(
                f"- {run['id']}: {run['definition']} {run['status']} cost=${run['cost']:.4f} {run['task']}"
                for run in runs
            )
            return ok("\n".join(lines))
        try:
            run = store.load_run(run_id)
        except WorkflowError as exc:
            return err(str(exc))
        body = format_run(run)
        if args.get("journal"):
            recent = run.journal[-int(args.get("journal") or 20) :]
            body += "\n\nJournal:\n" + "\n".join(
                f"- {item.get('event')}: {json.dumps({k: v for k, v in item.items() if k not in {'event', 'ts'}}, ensure_ascii=False)}"
                for item in recent
            )
        return ok(body)

    return [
        Tool(
            name="WorkflowCreate",
            description=(
                "Create or update a workflow definition. Accepts a YAML/JSON mapping (or a path). "
                "A definition has an id, phases, and optional nodes per scheduled phase. "
                "Use WorkflowList to inspect built-ins and saved definitions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "definition": {"type": "object", "description": "Workflow definition mapping"},
                    "path": {"type": "string", "description": "Path to a YAML/JSON definition"},
                    "scope": {"type": "string", "enum": ["workspace", "user"], "default": "workspace"},
                },
                "required": [],
            },
            handler=create_handler,
            read_only=False,
        ),
        Tool(
            name="WorkflowList",
            description="List built-in and saved workflow definitions plus recent runs.",
            parameters={"type": "object", "properties": {}},
            handler=list_handler,
            read_only=True,
        ),
        Tool(
            name="WorkflowRun",
            description=(
                "Run a workflow. `expert` is built in: clarify, task analysis, architecture "
                "decomposition, environment setup, execution DAG, final critic, report. Use it for "
                "long multi-phase tasks. Set background=true for long runs and poll with TaskOutput. "
                "Set resume to a run id to continue an interrupted run."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workflow": {"type": "string", "default": config.default_definition},
                    "task": {"type": "string", "description": "Task for a new run"},
                    "resume": {"type": "string", "description": "Run id or path to resume"},
                    "background": {"type": "boolean", "default": False},
                    "max_concurrent": {"type": "integer", "description": "Override DAG concurrency"},
                },
                "required": [],
            },
            handler=run_handler,
            read_only=False,
        ),
        Tool(
            name="WorkflowStatus",
            description=(
                "Show workflow run status: phases, DAG nodes, artifacts, cost, and errors. Omit "
                "run_id to list recent runs."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                    "journal": {"type": "integer", "description": "Include the last N journal events"},
                },
                "required": [],
            },
            handler=status_handler,
            read_only=True,
        ),
    ]


def build_submit_result_tool(schema: dict | None) -> Tool:
    """The typed-result tool ZCode gives every workflow subagent."""

    def handler(args: dict) -> dict:
        value = args.get("result")
        violations = validate_value(value, schema)
        if violations:
            return err("submit_result rejected:\n" + "\n".join(violations))
        payload = json.dumps(value, ensure_ascii=False)
        raise Submitted(
            {
                "role": "exit",
                "content": payload,
                "extra": {
                    "exit_status": "ResultSubmitted",
                    "submission": payload,
                    "result": value,
                },
            }
        )

    if schema:
        description = (
            "Submit the typed result for this ask. The value must conform to the result schema. "
            f"Schema: {json.dumps(schema, ensure_ascii=False)}. Pass the conforming JSON as `result`."
        )
    else:
        description = (
            "Submit the final result for this ask. There is no result schema; pass your final text "
            "as `result`."
        )
    return Tool(
        name="submit_result",
        description=description,
        parameters={
            "type": "object",
            "properties": {"result": schema or {}},
            "required": ["result"],
        },
        handler=handler,
        read_only=False,
    )


def build_escalate_tool() -> Tool:
    def handler(args: dict) -> dict:
        question = str(args.get("question") or "").strip()
        if not question:
            return err("question is required")
        context = str(args.get("context") or "").strip()
        raise Submitted(
            {
                "role": "exit",
                "content": question,
                "extra": {
                    "exit_status": "Escalated",
                    "submission": question,
                    "escalation": {"question": question, "context": context},
                },
            }
        )

    return Tool(
        name="escalate",
        description=(
            "Stop and ask the run owner for a decision when a gate cannot pass, instructions "
            "contradict each other, or you need a fact only the owner knows. Questions written in "
            "plain prose reach nobody; this tool is the only way to ask."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["question"],
        },
        handler=handler,
        read_only=False,
    )
