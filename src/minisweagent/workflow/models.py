"""Data models for the workflow runtime.

This is a Python port of ZCode's dynamic-workflow core. ZCode authors workflows
as TypeScript scripts; this port uses declarative YAML/JSON definitions so the
same concepts (phases, scheduled graph, typed results, artifacts, journal) can
run inside mini-swe-agent without embedding a TypeScript compiler.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

PhaseBehavior = Literal["agent", "scheduled_graph", "critic", "complete"]
RunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
NodeStatus = Literal["pending", "running", "completed", "failed", "skipped"]


class WorkflowNode(BaseModel):
    """One scheduled unit of work inside a `scheduled_graph` phase."""

    id: str = Field(min_length=1)
    phase: str = ""
    title: str = ""
    description: str = ""
    prompt: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    result_schema: dict | None = None
    status: NodeStatus = "pending"
    attempts: int = 0
    result: Any = None
    artifact: str | None = None
    error: str = ""


class WorkflowPhase(BaseModel):
    name: str = Field(min_length=1)
    title: str = ""
    description: str = ""
    behavior: PhaseBehavior = "agent"
    artifact: str | None = None
    result_schema: dict | None = None
    max_steps: int = 0
    depends_on: list[str] = Field(default_factory=list)
    nodes: list[WorkflowNode] = Field(default_factory=list)
    seed_graph: bool = False
    """Ask this phase to return a JSON graph that seeds the next scheduled_graph phase."""


class WorkflowDefinition(BaseModel):
    id: str = Field(min_length=1)
    title: str = ""
    description: str = ""
    version: int = 1
    phases: list[WorkflowPhase] = Field(min_length=1)
    max_concurrent: int = Field(default=3, ge=1)
    max_retries: int = Field(default=1, ge=0)
    on_node_failure: Literal["continue", "fail"] = "continue"
    strategy: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_graph(self) -> WorkflowDefinition:
        names = [phase.name for phase in self.phases]
        if len(names) != len(set(names)):
            raise ValueError("phase names must be unique")
        known = set(names)
        for phase in self.phases:
            unknown = [dependency for dependency in phase.depends_on if dependency not in known]
            if unknown:
                raise ValueError(f"phase {phase.name!r} depends on unknown phase(s): {unknown}")
            for node in phase.nodes:
                node.phase = node.phase or phase.name
        if _has_cycle({phase.name: phase.depends_on for phase in self.phases}):
            raise ValueError("phase dependencies must not contain a cycle")
        return self


class PhaseState(BaseModel):
    name: str
    status: NodeStatus = "pending"
    artifact: str | None = None
    error: str = ""
    started_at: float | None = None
    finished_at: float | None = None


class WorkflowRun(BaseModel):
    id: str = Field(default_factory=lambda: f"wfrun-{uuid.uuid4().hex[:12]}")
    definition_id: str
    task: str = ""
    status: RunStatus = "pending"
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    phases: list[PhaseState] = Field(default_factory=list)
    nodes: list[WorkflowNode] = Field(default_factory=list)
    artifacts: list[dict] = Field(default_factory=list)
    journal: list[dict] = Field(default_factory=list)
    cost: float = 0.0
    n_calls: int = 0
    n_compaction_calls: int = 0
    max_concurrent: int = 3
    error: str = ""

    def phase_state(self, name: str) -> PhaseState:
        for state in self.phases:
            if state.name == name:
                return state
        state = PhaseState(name=name)
        self.phases.append(state)
        return state


@dataclass
class AgentRunResult:
    """One workflow subagent turn."""

    text: str
    value: Any = None
    exit_status: str = "Submitted"
    cost: float = 0.0
    n_calls: int = 0
    n_compaction_calls: int = 0
    escalation: dict = field(default_factory=dict)


class WorkflowError(Exception):
    """A workflow could not continue."""


class WorkflowCancelled(WorkflowError):
    """The run was cancelled by the user or a parent agent."""


def _has_cycle(edges: dict[str, list[str]]) -> bool:
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
