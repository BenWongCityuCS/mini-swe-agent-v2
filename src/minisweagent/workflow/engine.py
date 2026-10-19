"""Workflow engine: phases, scheduled DAGs, artifacts, journal, and resume."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from minisweagent.workflow.models import (
    AgentRunResult,
    WorkflowCancelled,
    WorkflowDefinition,
    WorkflowError,
    WorkflowNode,
    WorkflowPhase,
    WorkflowRun,
)
from minisweagent.workflow.prompts import (
    WORKFLOW_ACTOR_SYSTEM,
    build_node_prompt,
    build_phase_prompt,
    build_planner_prompt,
    build_report,
    build_scheduled_summary,
)
from minisweagent.workflow.store import WorkflowStore

logger = logging.getLogger("workflow")

Runner = Callable[..., AgentRunResult]


class WorkflowEngine:
    def __init__(
        self,
        definition: WorkflowDefinition,
        task: str,
        store: WorkflowStore,
        runner: Runner,
        *,
        max_concurrent: int | None = None,
        max_retries: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.definition = definition
        self.task = task
        self.store = store
        self.runner = runner
        self.max_concurrent = max(1, max_concurrent or definition.max_concurrent)
        self.max_retries = definition.max_retries if max_retries is None else max(0, max_retries)
        self.cancel_event = cancel_event
        self._lock = threading.Lock()

    # -- public ------------------------------------------------------------

    def run(self, *, resume_run_id: str | None = None) -> WorkflowRun:
        run = self.store.load_run(resume_run_id) if resume_run_id else self.store.new_run(
            self.definition, self.task, max_concurrent=self.max_concurrent
        )
        if resume_run_id:
            self._prepare_resume(run)
            if self.definition.id != run.definition_id:
                self.definition = self.store.load_definition(run.definition_id)
            self.task = run.task
        for phase in self.definition.phases:
            run.phase_state(phase.name)
        run.max_concurrent = self.max_concurrent
        if run.status in {"completed", "cancelled"}:
            return run

        run.status = "running"
        run.error = ""
        self._save(run)
        self._journal(run, "run-started", definition=run.definition_id)
        try:
            ordered = self._ordered_phases()
            for phase in ordered:
                state = run.phase_state(phase.name)
                if state.status == "completed":
                    continue
                if self._dependency_failed(run, phase):
                    state.status = "skipped"
                    state.error = "a dependency did not complete"
                    self._save(run)
                    continue
                self._check_cancel()
                state.status = "running"
                state.started_at = time.time()
                state.error = ""
                self._save(run)
                self._journal(run, "phase-started", phase=phase.name)
                try:
                    artifact = self._run_phase(run, phase)
                except WorkflowCancelled:
                    raise
                except Exception as exc:
                    logger.exception("workflow phase %s failed", phase.name)
                    state.status = "failed"
                    state.error = f"{type(exc).__name__}: {exc}"
                    state.finished_at = time.time()
                    run.status = "failed"
                    run.error = state.error
                    self._journal(run, "run-failed", phase=phase.name, error=state.error)
                    self._save(run)
                    return run
                state.status = "completed"
                state.artifact = artifact
                state.finished_at = time.time()
                self._save(run)
                self._journal(run, "phase-completed", phase=phase.name, artifact=artifact)
            run.status = "completed"
            self._journal(run, "run-completed")
        except WorkflowCancelled:
            run.status = "cancelled"
            run.error = "cancelled"
            self._journal(run, "run-cancelled")
        finally:
            self._save(run)
        return run

    # -- phases ------------------------------------------------------------

    def _run_phase(self, run: WorkflowRun, phase: WorkflowPhase) -> str | None:
        if phase.behavior == "complete":
            # The report describes the finished run, so mark the terminal phase
            # and run status before rendering it.
            run.phase_state(phase.name).status = "completed"
            run.status = "completed"
            text = build_report(run)
            return self._write_artifact(run, phase, text)
        if phase.behavior == "scheduled_graph":
            nodes = self._ensure_nodes(run, phase)
            self._run_graph(run, phase, nodes)
            text = build_scheduled_summary(run, phase.name)
            return self._write_artifact(run, phase, text)

        prompt = build_phase_prompt(run, phase)
        result = self._run_agent(
            run,
            prompt,
            phase,
            system=WORKFLOW_ACTOR_SYSTEM,
            result_schema=phase.result_schema,
            max_steps=phase.max_steps,
        )
        text = _result_text(result)
        artifact = self._write_artifact(run, phase, text)
        if phase.seed_graph:
            self._seed_nodes(run, self._scheduled_phase_after(phase), text)
        return artifact

    def _run_agent(
        self,
        run: WorkflowRun,
        prompt: str,
        phase: WorkflowPhase,
        *,
        system: str,
        result_schema: dict | None,
        max_steps: int,
    ) -> AgentRunResult:
        self._check_cancel()
        result = self.runner(
            prompt,
            system=system,
            result_schema=result_schema,
            max_steps=max_steps,
            cancel_event=self.cancel_event,
        )
        with self._lock:
            run.cost += result.cost
            run.n_calls += result.n_calls
            run.n_compaction_calls += result.n_compaction_calls
            self._journal(
                run,
                "agent-finished",
                phase=phase.name,
                status=result.exit_status,
                calls=result.n_calls,
                cost=result.cost,
            )
            self._save(run)
        if result.escalation or result.exit_status == "Escalated":
            question = (result.escalation or {}).get("question") or result.text
            self._journal(run, "escalation", phase=phase.name, question=question)
            raise WorkflowError(f"workflow subagent escalated: {question}")
        return result

    # -- scheduled graph ---------------------------------------------------

    def _ensure_nodes(self, run: WorkflowRun, phase: WorkflowPhase) -> list[WorkflowNode]:
        existing = [node for node in run.nodes if node.phase == phase.name]
        if existing:
            return existing
        if phase.nodes:
            nodes = [node.model_copy(deep=True) for node in phase.nodes]
            for node in nodes:
                node.phase = phase.name
            run.nodes.extend(nodes)
            self._save(run)
            return nodes

        prompt = build_planner_prompt(run, phase)
        result = self._run_agent(
            run,
            prompt,
            phase,
            system=WORKFLOW_ACTOR_SYSTEM,
            result_schema=None,
            max_steps=phase.max_steps,
        )
        nodes = self._parse_nodes(result.text, phase.name)
        if not nodes:
            nodes = [
                WorkflowNode(
                    id="execute",
                    phase=phase.name,
                    title=phase.title or phase.name,
                    description=phase.description,
                    prompt=result.text,
                )
            ]
            self._journal(run, "planner-fallback", phase=phase.name, reason="could not parse a graph")
        run.nodes.extend(nodes)
        self._save(run)
        return nodes

    def _seed_nodes(self, run: WorkflowRun, phase: WorkflowPhase | None, text: str) -> None:
        if phase is None:
            return
        if any(node.phase == phase.name for node in run.nodes):
            return
        nodes = self._parse_nodes(text, phase.name)
        if not nodes:
            self._journal(run, "graph-seed-skipped", phase=phase.name, reason="no parseable JSON graph")
            return
        run.nodes.extend(nodes)
        self._journal(run, "graph-seeded", phase=phase.name, nodes=len(nodes))
        self._save(run)

    def _scheduled_phase_after(self, phase: WorkflowPhase) -> WorkflowPhase | None:
        seen = False
        for candidate in self.definition.phases:
            if candidate is phase:
                seen = True
                continue
            if seen and candidate.behavior == "scheduled_graph":
                return candidate
        return next((candidate for candidate in self.definition.phases if candidate.behavior == "scheduled_graph"), None)

    def _parse_nodes(self, text: str, phase_name: str) -> list[WorkflowNode]:
        data = _extract_json(text)
        if not isinstance(data, dict):
            return []
        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, list):
            return []
        nodes: list[WorkflowNode] = []
        for raw in raw_nodes:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            depends = list(raw.get("dependsOn") or raw.get("depends_on") or [])
            nodes.append(
                WorkflowNode(
                    id=str(raw["id"]),
                    phase=phase_name,
                    title=str(raw.get("title") or raw["id"]),
                    description=str(raw.get("description") or ""),
                    prompt=str(raw["prompt"]) if raw.get("prompt") else None,
                    depends_on=[str(item) for item in depends],
                    result_schema=raw.get("result_schema") or raw.get("resultSchema"),
                )
            )
        for edge in data.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            source, target = str(edge.get("from") or ""), str(edge.get("to") or "")
            if not source or not target:
                continue
            node = next((item for item in nodes if item.id == target), None)
            if node is not None and source not in node.depends_on:
                node.depends_on.append(source)
        try:
            _validate_nodes(nodes)
        except WorkflowError as exc:
            logger.warning("invalid workflow graph: %s", exc)
            return []
        return nodes

    def _run_graph(self, run: WorkflowRun, phase: WorkflowPhase, nodes: list[WorkflowNode]) -> None:
        by_id = {node.id: node for node in nodes}
        _validate_nodes(nodes)
        remaining = set(by_id)
        completed: set[str] = {node.id for node in nodes if node.status == "completed"}
        levels = _topological_levels(nodes)
        for level in levels:
            ready = []
            for node_id in level:
                node = by_id[node_id]
                if node.status == "completed":
                    continue
                deps = set(node.depends_on)
                if not deps.issubset(completed):
                    missing = deps - completed
                    if any(by_id[item].status in {"failed", "skipped"} for item in missing if item in by_id):
                        node.status = "skipped"
                        node.error = "a dependency failed"
                        self._journal(run, "node-skipped", node=node.id, phase=phase.name)
                        continue
                ready.append(node)
            if not ready:
                continue
            workers = min(self.max_concurrent, max(1, len(ready)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="workflow") as pool:
                futures = [pool.submit(self._run_node, run, phase, node) for node in ready]
                for future in futures:
                    future.result()
            completed.update(node.id for node in ready if node.status == "completed")
            remaining.difference_update(node.id for node in ready)

        failed = [node for node in nodes if node.status == "failed"]
        if failed and self.definition.on_node_failure == "fail":
            raise WorkflowError(
                "scheduled graph failed: " + ", ".join(f"{node.id}: {node.error}" for node in failed)
            )

    def _run_node(self, run: WorkflowRun, phase: WorkflowPhase, node: WorkflowNode) -> None:
        self._check_cancel()
        prompt = build_node_prompt(run, phase, node)
        last_error = ""
        for attempt in range(self.max_retries + 1):
            with self._lock:
                node.status = "running"
                node.attempts += 1
                self._journal(
                    run, "node-started", phase=phase.name, node=node.id, attempt=node.attempts
                )
                self._save(run)
            try:
                result = self.runner(
                    prompt,
                    system=WORKFLOW_ACTOR_SYSTEM,
                    result_schema=node.result_schema,
                    max_steps=phase.max_steps,
                    cancel_event=self.cancel_event,
                )
                if result.escalation or result.exit_status == "Escalated":
                    question = (result.escalation or {}).get("question") or result.text
                    raise WorkflowError(f"workflow subagent escalated: {question}")
                with self._lock:
                    node.result = result.value
                    node.status = "completed"
                    node.error = ""
                    node.artifact = self._write_artifact(
                        run,
                        phase,
                        _result_text(result),
                        name=f"{phase.name}-{node.id}",
                    )
                    run.cost += result.cost
                    run.n_calls += result.n_calls
                    run.n_compaction_calls += result.n_compaction_calls
                    self._journal(
                        run,
                        "node-finished",
                        phase=phase.name,
                        node=node.id,
                        status=result.exit_status,
                    )
                    self._save(run)
                return
            except WorkflowCancelled:
                raise
            except WorkflowError as exc:
                # Escalation and missing typed results are terminal for this node.
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("workflow node %s stopped: %s", node.id, last_error)
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("workflow node %s failed (attempt %d): %s", node.id, attempt + 1, last_error)
        with self._lock:
            node.status = "failed"
            node.error = last_error
            self._journal(run, "node-failed", phase=phase.name, node=node.id, error=last_error)
            self._save(run)

    # -- helpers -----------------------------------------------------------

    def _write_artifact(self, run: WorkflowRun, phase: WorkflowPhase, text: str, *, name: str | None = None) -> str:
        artifact_name = name or phase.artifact or f"{phase.name}.md"
        if not artifact_name.endswith(".md"):
            artifact_name += ".md"
        return self.store.write_artifact(
            run,
            artifact_name,
            text or "(empty artifact)",
            label=phase.title or phase.name,
        )

    def _ordered_phases(self) -> list[WorkflowPhase]:
        by_name = {phase.name: phase for phase in self.definition.phases}
        ordered: list[WorkflowPhase] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise WorkflowError(f"phase dependency cycle at {name!r}")
            visiting.add(name)
            for dependency in by_name[name].depends_on:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
            ordered.append(by_name[name])

        for phase in self.definition.phases:
            visit(phase.name)
        return ordered

    def _dependency_failed(self, run: WorkflowRun, phase: WorkflowPhase) -> bool:
        for dependency in phase.depends_on:
            state = run.phase_state(dependency)
            if state.status in {"failed", "skipped"}:
                return True
        return False

    def _prepare_resume(self, run: WorkflowRun) -> None:
        for state in run.phases:
            if state.status == "running":
                state.status = "pending"
                state.error = ""
        for node in run.nodes:
            if node.status == "running":
                node.status = "pending"
                node.error = ""

    def _check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise WorkflowCancelled("workflow cancelled")

    def _journal(self, run: WorkflowRun, event: str, **data) -> None:
        run.journal.append({"ts": time.time(), "event": event, **data})

    def _save(self, run: WorkflowRun) -> None:
        self.store.save_run(run)


def _result_text(result: AgentRunResult) -> str:
    if isinstance(result.value, str):
        return result.value
    if result.value is not None:
        return json.dumps(result.value, ensure_ascii=False, indent=2)
    return result.text


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _validate_nodes(nodes: list[WorkflowNode]) -> None:
    ids = [node.id for node in nodes]
    if len(ids) != len(set(ids)):
        raise WorkflowError("workflow node ids must be unique")
    known = set(ids)
    for node in nodes:
        unknown = [item for item in node.depends_on if item not in known]
        if unknown:
            raise WorkflowError(f"node {node.id!r} depends on unknown node(s): {unknown}")
    edges = {node.id: node.depends_on for node in nodes}
    if _cycle(edges):
        raise WorkflowError("workflow node dependencies contain a cycle")


def _topological_levels(nodes: list[WorkflowNode]) -> list[list[str]]:
    by_id = {node.id: set(node.depends_on) for node in nodes}
    levels: list[list[str]] = []
    remaining = dict(by_id)
    while remaining:
        level = sorted(node_id for node_id, deps in remaining.items() if deps.isdisjoint(remaining))
        if not level:
            raise WorkflowError("workflow node dependencies contain a cycle")
        levels.append(level)
        for node_id in level:
            remaining.pop(node_id, None)
        for deps in remaining.values():
            deps.difference_update(level)
    return levels


def _cycle(edges: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for dependency in edges.get(node, []):
            if visit(dependency):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in edges)


def format_run(run: WorkflowRun) -> str:
    lines = [
        f'<workflow_run id="{run.id}" definition="{run.definition_id}" status="{run.status}" '
        f'calls="{run.n_calls}" cost="{run.cost:.4f}">',
        f"Task: {run.task}",
        "",
        "Phases:",
    ]
    for state in run.phases:
        line = f"- {state.name}: {state.status}"
        if state.artifact:
            line += f" ({state.artifact})"
        if state.error:
            line += f" error={state.error}"
        lines.append(line)
    nodes = [node for node in run.nodes if node.phase]
    if nodes:
        lines.extend(["", "Nodes:"])
        lines.extend(
            f"- [{node.phase}] {node.id}: {node.status}"
            + (f" error={node.error}" if node.error else "")
            for node in nodes
        )
    if run.artifacts:
        lines.extend(["", "Artifacts:"])
        lines.extend(f"- {item.get('label')}: {item.get('path')}" for item in run.artifacts)
    if run.error:
        lines.extend(["", f"Error: {run.error}"])
    lines.append("</workflow_run>")
    return "\n".join(lines)
