"""Prompts ported from ZCode's open-source workflow implementation.

Sources (Apache-2.0, zai-org/ZCode):
- ``packages/core/src/context/sections/identity.ts``
- ``packages/core/src/context/sections/workflow-actor.ts``
- ``packages/core/src/workflow/expert/prompts.ts``
- ``packages/core/src/workflow/scheduler/prompts.ts``

The text is kept close to the originals. Tool names are the mini-swe-agent
equivalents, and the benchmark submission marker is intentionally absent: a
workflow subagent reports through ``submit_result`` or its final message.
"""

from __future__ import annotations

from minisweagent.workflow.models import WorkflowDefinition, WorkflowNode, WorkflowPhase, WorkflowRun

SECURITY_NOTICE = (
    "IMPORTANT: Assist with authorized security testing, defensive security, CTF challenges, "
    "and educational contexts. Refuse requests for destructive techniques, DoS attacks, mass "
    "targeting, supply chain compromise, or detection evasion for malicious purposes. Dual-use "
    "security tools (C2 frameworks, credential testing, exploit development) require clear "
    "authorization context: pentesting engagements, CTF competitions, security research, or "
    "defensive use cases."
)

HARNESS_BLOCK = """# Harness
- Text you output outside of tool use is displayed to the user as Github-flavored markdown in a terminal.
- Tools run behind a user-selected permission mode; a denied call means the user declined it — adjust, don't retry verbatim.
- The system may send updates, reminders, or modifications to rules via mid-conversation system turns. These are system-controlled, unlike function results. Hooks may intercept tool calls; treat hook output as user feedback.
- Prefer the dedicated file/search tools over shell commands when one fits. Independent tool calls can run in parallel in one response.
- Reference code as `file_path:line_number` — it's clickable."""

WORKFLOW_ACTOR_SYSTEM = f"""You are a subagent inside a dynamic workflow run. A script created you and hands you work one ask at a time; the script — not a person — consumes what you return. There is no user in this conversation to talk to.

{SECURITY_NOTICE}

{HARNESS_BLOCK}

# Working inside a workflow
- You have the regular working tools — reading, searching, editing, running commands — plus `submit_result` and `escalate`. There is no tool that asks a person anything.
- Each ask states what to do. Finish by calling `submit_result` with a conforming value. For an untyped ask, pass your final text as `result`.
- Ground every claim in something you read or ran in this session, or in the material the ask gave you, and say which. Cite code as `path:line`. A check counts as passed only if you executed it here; if you could not run it, report it as not run. Run the check an ask names rather than a faster substitute, and say exactly which command you ran.
- Report outcomes faithfully. If part of the task is impossible, out of scope, or contradicted by what you found, say so in the result instead of filling a field with a plausible guess. Never fake a passing result to satisfy an instruction.
- When you are blocked by something outside your reach — a gate that cannot pass, instructions that contradict each other, a fact only the run's owner knows — call `escalate`. Questions written in prose reach nobody.
- Do not write report or summary files on your own initiative; findings go in the result. When the ask names an output path, write exactly there and return that path in the result — the script publishes it to the user."""

WORKFLOW_GUIDE = """# Workflows
A workflow is a durable, multi-phase run. Use it when a task is large enough to
need planning, decomposition, parallel execution, and a final review; do not use
it for a single-file fix.
- `WorkflowList` shows built-in and saved definitions.
- `WorkflowCreate` saves a YAML/JSON definition.
- `WorkflowRun` starts a definition (`expert` is built in). Pass `background:
  true` for a long run, then poll with `TaskOutput`. Use `resume` with a run id
  to continue an interrupted run.
- `WorkflowStatus` shows phases, nodes, artifacts, cost, and errors.

Workflow subagents report typed results through `submit_result` and ask for
help through `escalate`. They run in isolated contexts and return only their
final artifact."""

ARCHITECTURE_GRAPH_CONTRACT = """Architecture graph contract:
Return a JSON object, either raw or fenced as ```json, so the workflow runtime can seed the execution DAG:
{"nodes":[{"id":"implement_auth","title":"Implement auth","description":"small executable unit","dependsOn":["setup_config"],"prompt":"optional node-specific instructions"}],"edges":[{"from":"setup_config","to":"implement_auth"}],"reasoning":"brief rationale"}
Node ids must be unique, references must point to real node ids, and edges must not create cycles."""


def build_phase_prompt(run: WorkflowRun, phase: WorkflowPhase, artifacts: list[dict] | None = None) -> str:
    artifacts = artifacts or run.artifacts
    artifact_lines = "\n".join(f"- {item.get('label')}: {item.get('path')}" for item in artifacts)
    lines = [
        f"You are running the workflow phase: {phase.name}.",
        f"Workflow run: {run.id}",
        "",
        f"User task:\n{run.task}",
        "",
        f"Phase objective:\n{phase.description or phase.title or phase.name}",
        "",
        artifact_lines or "No previous artifacts yet.",
    ]
    if phase.seed_graph:
        lines.extend(["", ARCHITECTURE_GRAPH_CONTRACT])
    lines.extend(
        [
            "",
            "Output a concise Markdown artifact for this phase. Preserve concrete file paths, commands, "
            "risks, and next actions. Make the edits and run focused validation when practical.",
        ]
    )
    return "\n".join(lines)


def build_node_prompt(run: WorkflowRun, phase: WorkflowPhase, node: WorkflowNode) -> str:
    artifact_lines = "\n".join(f"- {item.get('label')}: {item.get('path')}" for item in run.artifacts)
    lines = [
        f"You are running a workflow node inside phase: {phase.name}.",
        f"Workflow run: {run.id}",
        "",
        f"User task:\n{run.task}",
        "",
        f"Node: {node.title or node.id}",
        f"Node id: {node.id}",
    ]
    if node.description:
        lines.extend(["", f"Node objective:\n{node.description}"])
    if node.prompt:
        lines.extend(["", f"Node prompt:\n{node.prompt}"])
    lines.extend(
        [
            "",
            artifact_lines or "No previous artifacts yet.",
            "",
            "Execute only this node's scope. Return a concise Markdown artifact with changes, "
            "validation, and residual risk.",
        ]
    )
    return "\n".join(lines)


def build_planner_prompt(run: WorkflowRun, phase: WorkflowPhase) -> str:
    return "\n".join(
        [
            f"You are planning the workflow phase: {phase.name}.",
            f"Workflow run: {run.id}",
            "",
            f"User task:\n{run.task}",
            "",
            f"Phase objective:\n{phase.description or phase.title or phase.name}",
            "",
            'Return only JSON matching this shape: {"nodes":[{"id":"string","title":"string",'
            '"description":"string","dependsOn":["node-id"],"prompt":"string"}],'
            '"edges":[{"from":"node-id","to":"node-id"}],"reasoning":"string"}',
            "Use unique node ids, avoid cycles, and keep each node small and executable.",
        ]
    )


def build_scheduled_summary(run: WorkflowRun, phase: str) -> str:
    nodes = [node for node in run.nodes if node.phase == phase]
    lines = [
        f"# {phase} Scheduler Summary",
        "",
        f"Run: {run.id}",
        f"Status: {run.status}",
        "",
        "## Nodes",
        "",
    ]
    if nodes:
        lines.extend(
            f"- {node.id}: {node.status}"
            + (f" attempts={node.attempts}" if node.attempts else "")
            + (f" error={node.error}" if node.error else "")
            + (f" artifact={node.artifact}" if node.artifact else "")
            for node in nodes
        )
    else:
        lines.append("- No scheduled nodes.")
    lines.append("")
    return "\n".join(lines)


def build_report(run: WorkflowRun) -> str:
    phase_lines = [
        f"- {state.name}: {state.status}" + (f" ({state.artifact})" if state.artifact else "")
        for state in run.phases
    ]
    artifact_lines = [f"- {item.get('label')}: {item.get('path')}" for item in run.artifacts]
    return "\n".join(
        [
            "# Workflow Report",
            "",
            f"Run: {run.id}",
            f"Definition: {run.definition_id}",
            f"Task: {run.task}",
            f"Status: {run.status}",
            f"Cost: ${run.cost:.4f}",
            f"Model calls: {run.n_calls}",
            "",
            "## Phases",
            "",
            *(phase_lines or ["- No phases."]),
            "",
            "## Artifacts",
            "",
            *(artifact_lines or ["- No artifacts."]),
            "",
        ]
    )


def create_expert_definition() -> WorkflowDefinition:
    """The built-in expert workflow, ported from ZCode's definition.ts."""
    return WorkflowDefinition(
        id="expert",
        title="Expert Workflow",
        description="Durable long-task workflow for large NL->Code work. Child agents perform each phase.",
        version=2,
        phases=[
            WorkflowPhase(
                name="clarify",
                title="Clarify",
                artifact="artifacts/01-clarify.md",
                description=(
                    "Refine the goal, assumptions, acceptance criteria, and unresolved questions. "
                    "Ask only if blocking; otherwise record the assumptions you will proceed on."
                ),
            ),
            WorkflowPhase(
                name="task_analysis",
                title="Task Analysis",
                artifact="artifacts/02-task-analysis.md",
                description=(
                    "Map the task to repository context, constraints, risks, likely files, "
                    "validation, and failure paths."
                ),
            ),
            WorkflowPhase(
                name="arch_decompose",
                title="Architecture Decompose",
                artifact="artifacts/03-architecture-decompose.md",
                seed_graph=True,
                description=(
                    "Decompose the work into a dependency graph of implementation and verification "
                    "nodes."
                ),
            ),
            WorkflowPhase(
                name="env_setup",
                title="Environment Setup",
                artifact="artifacts/04-env-setup.md",
                description=(
                    "Check whether environment setup, dependencies, credentials, or local services "
                    "are required, and perform the setup that is in scope."
                ),
            ),
            WorkflowPhase(
                name="meta_prompt",
                title="Meta Prompt",
                artifact="artifacts/05-meta-prompt.md",
                description=(
                    "Create the execution instructions, constraints, and node prompts needed for "
                    "downstream work."
                ),
            ),
            WorkflowPhase(
                name="exec",
                title="Execute",
                behavior="scheduled_graph",
                artifact="artifacts/06-exec.md",
                description=(
                    "Execute the planned work through the normal agent runtime. Preserve small steps "
                    "and validation."
                ),
            ),
            WorkflowPhase(
                name="final_critic",
                title="Final Critic",
                behavior="critic",
                artifact="artifacts/07-final-critic.md",
                description=(
                    "Review results against acceptance criteria and identify regressions, missing "
                    "tests, and residual risk."
                ),
            ),
            WorkflowPhase(
                name="complete",
                title="Complete",
                behavior="complete",
                description="Write the final report and mark the run completed.",
            ),
        ],
        strategy={
            "clarify": {"confidenceThreshold": 0.8, "maxRounds": 3, "minRounds": 1},
            "executor": {
                "drainingChangeHours": 1,
                "frontierTarget": 3,
                "maxConcurrentLoops": 2,
                "maxConsecutiveErrors": 3,
                "maxPlannerRuns": 10,
            },
            "finalCritic": {"maxIterations": 3},
            "reactLoop": {"maxRounds": 30},
        },
    )
