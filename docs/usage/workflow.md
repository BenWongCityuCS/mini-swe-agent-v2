# Workflows

A workflow is a durable, multi-phase run. It is a Python port of
[ZCode](https://github.com/zai-org/ZCode)'s dynamic-workflow core: phases,
a scheduled DAG, typed subagent results, artifacts, a journal, and resume.

The port uses declarative YAML/JSON definitions instead of ZCode's TypeScript
authoring facade and sandbox. The runtime concepts and the actor/phase/node/
planner prompts are kept; the TypeScript compiler, `vm` cell, holes, streams,
and GUI are not included.

## Enable it

```yaml
agent:
  workflow:
    enabled: true
    dirs: ["./workflows", "~/.mini-swe-agent/workflows"]
    run_dir: ".mini-swe-agent/workflow-runs"
    default_definition: expert
    max_concurrent: 3
    max_retries: 1
    max_steps_per_agent: 0
```

`extensions.yaml` enables it by default. Running a workflow needs a tool-call
model and the `Task` machinery, so keep `subagents.enabled: true`.

## Built-in `expert` workflow

| Phase | Behavior | Artifact |
| --- | --- | --- |
| `clarify` | agent | `01-clarify.md` |
| `task_analysis` | agent | `02-task-analysis.md` |
| `arch_decompose` | agent, emits a JSON DAG | `03-architecture-decompose.md` |
| `env_setup` | agent | `04-env-setup.md` |
| `meta_prompt` | agent | `05-meta-prompt.md` |
| `exec` | scheduled graph | `06-exec.md` |
| `final_critic` | agent | `07-final-critic.md` |
| `complete` | report | final report |

If `arch_decompose` returns a parseable graph, it seeds `exec`. Otherwise the
runtime asks a planner subagent for a graph; if that also fails, it falls back
to one `execute` node. Each node runs as an isolated subagent and writes its own
artifact.

## Definitions

```yaml
id: my-workflow
title: My Workflow
description: A small two-phase workflow
max_concurrent: 2
max_retries: 1
on_node_failure: continue   # or fail
phases:
  - name: analyze
    title: Analyze
    artifact: artifacts/analyze.md
    description: Inspect the task and list the files to change.
  - name: exec
    title: Execute
    behavior: scheduled_graph
    description: Implement and verify the change.
    nodes:
      - id: implement
        title: Implement
        prompt: Make the change described in the analyze artifact.
      - id: test
        title: Test
        depends_on: [implement]
        prompt: Run the focused test suite and report failures.
        result_schema:
          type: object
          properties:
            passed:
              type: boolean
            summary:
              type: string
          required: [passed, summary]
          additionalProperties: false
  - name: complete
    behavior: complete
```

A phase `behavior` is one of `agent`, `scheduled_graph`, `critic`, or
`complete`. `depends_on` orders phases; node `depends_on` orders the DAG.
`seed_graph: true` asks an agent phase to return a JSON graph for the next
scheduled phase.

Save a definition with `WorkflowCreate`, or put it in `./workflows/<id>.yaml`
(`~/.mini-swe-agent/workflows/` also works).

## Typed results and escalation

A node or phase with `result_schema` gets a `submit_result` tool. The value is
validated against the JSON schema; violations are returned to the model so it
can correct itself. If the subagent ends without submitting, the node fails.

```json
{"result": {"passed": true, "summary": "go test ./... passed"}}
```

When a subagent cannot proceed, it calls `escalate` with a question. The run
records the escalation and fails that phase; the main agent sees the question in
the workflow status.

## Agent tools

| Tool | Purpose |
| --- | --- |
| `WorkflowCreate` | Validate and save a definition (mapping, YAML string, or path) |
| `WorkflowList` | List built-in and saved definitions plus recent runs |
| `WorkflowRun` | Start a run; `background: true` returns a task id |
| `WorkflowStatus` | Phases, nodes, artifacts, cost, errors, optional journal |

```json
{"workflow": "expert", "task": "Fix the flaky pagination test", "background": true}
```

Long runs should use `background: true`; poll with `TaskOutput` or
`WorkflowStatus`.

## CLI

```bash
mini-workflow list
mini-workflow status <run-id>
mini-workflow run expert --task "..." -c mini.yaml -c extensions.yaml
mini-workflow run expert --resume <run-id> -c mini.yaml -c extensions.yaml
```

## Storage

- Definitions: `./workflows` and `~/.mini-swe-agent/workflows`.
- Runs: `.mini-swe-agent/workflow-runs/<run-id>.json`.
- Artifacts: `.mini-swe-agent/workflow-runs/<run-id>/artifacts/`.

Every phase and node transition is journaled. Resuming resets interrupted
phases/nodes to pending and skips completed work.

## Limits

- Workflow subagents use the parent's model and tool registry. Per-node model
  selection is not part of this port.
- The DAG scheduler runs independent nodes in parallel up to `max_concurrent`;
  phases run in dependency order.
- The TypeScript authoring surface (`agent()`, `.ask()`, `hole<T>()`, streams,
  the sandbox, and the confirmation UI) is intentionally not ported.
