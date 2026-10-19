import json

import pytest

from minisweagent.exceptions import Submitted
from minisweagent.extensions.config import WorkflowConfig
from minisweagent.workflow import (
    AgentRunResult,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowNode,
    WorkflowPhase,
    WorkflowStore,
    build_submit_result_tool,
    build_workflow_tools,
)
from minisweagent.workflow.validation import validate_value


class FakeRunner:
    def __init__(self, answers, values=None):
        self.answers = list(answers)
        self.values = list(values or [])
        self.prompts = []

    def __call__(self, prompt, *, system, result_schema=None, max_steps=0, cancel_event=None):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError("unexpected workflow agent call")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        value = self.values.pop(0) if self.values else None
        return AgentRunResult(text=answer, value=value, cost=0.01, n_calls=1)


def make_store(tmp_path):
    return WorkflowStore(
        WorkflowConfig(
            enabled=True,
            dirs=[str(tmp_path / "workflows")],
            run_dir=str(tmp_path / "runs"),
        ),
        tmp_path,
    )


def simple_definition():
    return WorkflowDefinition(
        id="simple",
        phases=[
            WorkflowPhase(name="analyze", title="Analyze", artifact="artifacts/analyze.md"),
            WorkflowPhase(
                name="exec",
                title="Execute",
                behavior="scheduled_graph",
                artifact="artifacts/exec.md",
                nodes=[WorkflowNode(id="a"), WorkflowNode(id="b", depends_on=["a"])],
            ),
            WorkflowPhase(name="complete", behavior="complete"),
        ],
    )


def test_engine_runs_phases_and_dag(tmp_path):
    store = make_store(tmp_path)
    runner = FakeRunner(["analysis", "node a", "node b"])
    run = WorkflowEngine(simple_definition(), "do it", store, runner).run()

    assert run.status == "completed"
    assert [node.id for node in run.nodes] == ["a", "b"]
    assert all(node.status == "completed" for node in run.nodes)
    assert len(runner.prompts) == 3
    assert "Node: a" in runner.prompts[1]
    assert "Node: b" in runner.prompts[2]
    assert any("analyze" in item["path"] for item in run.artifacts)
    assert all(not item["path"].endswith(".md.md") for item in run.artifacts)
    assert run.n_calls == 3
    assert run.cost == pytest.approx(0.03)
    assert (tmp_path / "runs" / f"{run.id}.json").is_file()


def test_arch_phase_seeds_exec_graph(tmp_path):
    definition = WorkflowDefinition(
        id="seed",
        phases=[
            WorkflowPhase(name="arch", seed_graph=True, artifact="artifacts/arch.md"),
            WorkflowPhase(name="exec", behavior="scheduled_graph", artifact="artifacts/exec.md"),
            WorkflowPhase(name="complete", behavior="complete"),
        ],
    )
    graph = json.dumps(
        {
            "nodes": [
                {"id": "a", "title": "A"},
                {"id": "b", "title": "B", "dependsOn": ["a"]},
            ],
            "edges": [{"from": "a", "to": "b"}],
        }
    )
    runner = FakeRunner([graph, "node a", "node b"])
    run = WorkflowEngine(definition, "do it", make_store(tmp_path), runner).run()

    assert run.status == "completed"
    assert {node.id for node in run.nodes if node.phase == "exec"} == {"a", "b"}
    assert len(runner.prompts) == 3


def test_failed_phase_can_resume(tmp_path):
    definition = WorkflowDefinition(
        id="resume",
        phases=[
            WorkflowPhase(name="first"),
            WorkflowPhase(name="second"),
        ],
    )
    store = make_store(tmp_path)
    failing = FakeRunner(["first ok", RuntimeError("boom")])
    failed = WorkflowEngine(definition, "do it", store, failing).run()
    assert failed.status == "failed"
    assert failed.phases[1].status == "failed"

    recovering = FakeRunner(["second ok"])
    resumed = WorkflowEngine(definition, "do it", store, recovering).run(resume_run_id=failed.id)
    assert resumed.id == failed.id
    assert resumed.status == "completed"
    assert resumed.phases[0].status == "completed"
    assert len(recovering.prompts) == 1


def test_submit_result_validates_and_returns_value():
    tool = build_submit_result_tool(
        {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
    )
    rejected = tool.handler({"result": {"ok": "yes"}})
    assert rejected["returncode"] == -1
    assert "expected boolean" in rejected["exception_info"]

    with pytest.raises(Submitted) as excinfo:
        tool.handler({"result": {"ok": True}})
    assert excinfo.value.messages[0]["extra"]["result"] == {"ok": True}


def test_untyped_submit_result_accepts_text():
    tool = build_submit_result_tool(None)
    with pytest.raises(Submitted) as excinfo:
        tool.handler({"result": "WORKFLOW_OK"})
    assert excinfo.value.messages[0]["extra"]["result"] == "WORKFLOW_OK"


def test_schema_validator_covers_common_constraints():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 2},
            "count": {"type": "integer", "minimum": 1},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    assert validate_value({"name": "ok", "count": 2, "tags": ["x"]}, schema) == []
    errors = validate_value({"name": "x", "count": 0, "tags": [1], "extra": True}, schema)
    assert len(errors) == 4


def test_workflow_tools_create_run_and_status(tmp_path):
    store = make_store(tmp_path)
    runner = FakeRunner(["analysis", "node a", "node b"])
    tools = {
        tool.name: tool
        for tool in build_workflow_tools(store, runner, WorkflowConfig(enabled=True))
    }
    created = tools["WorkflowCreate"].handler({"definition": simple_definition().model_dump()})
    assert created["returncode"] == 0
    assert (tmp_path / "workflows" / "simple.yaml").is_file()

    listed = tools["WorkflowList"].handler({})
    assert "simple" in listed["output"]

    started = tools["WorkflowRun"].handler({"workflow": "simple", "task": "do it"})
    assert started["returncode"] == 0
    assert 'status="completed"' in started["output"]

    run_id = started["output"].split('id="', 1)[1].split('"', 1)[0]
    status = tools["WorkflowStatus"].handler({"run_id": run_id})
    assert "Phases:" in status["output"]
    assert "a: completed" in status["output"]


class FakeBackgroundManager:
    def __init__(self):
        self.jobs = []

    def start_callable(self, fn, *, label, owner="", kind="agent"):
        self.jobs.append(label)
        fn(None)
        return {"output": "started", "returncode": 0, "exception_info": "", "task_id": "bg-test"}


def test_workflow_run_background(tmp_path):
    store = make_store(tmp_path)
    store.save_definition(simple_definition())
    runner = FakeRunner(["analysis", "node a", "node b"])
    manager = FakeBackgroundManager()
    tools = {
        tool.name: tool
        for tool in build_workflow_tools(
            store,
            runner,
            WorkflowConfig(enabled=True),
            background_manager=manager,
            owner="main",
        )
    }
    result = tools["WorkflowRun"].handler({"workflow": "simple", "task": "do it", "background": True})
    assert result["returncode"] == 0
    assert result["task_id"] == "bg-test"
    assert manager.jobs
    runs = store.list_runs()
    assert runs and runs[0]["status"] == "completed"


def test_agent_registers_workflow_tools(tmp_path):
    from types import SimpleNamespace

    from minisweagent.agents.default import AgentConfig, DefaultAgent
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.extensions.config import SubagentsConfig, ToolsConfig

    class ScriptedModel:
        def __init__(self):
            self.config = SimpleNamespace(model_name="scripted", model_kwargs={})

        def set_extra_tools(self, tools):
            self.tools = tools

        def query(self, messages, **kwargs):
            raise AssertionError("not queried")

        def format_message(self, **kwargs):
            return kwargs

        def format_observation_messages(self, message, outputs, template_vars=None):
            return []

        def generate_text(self, messages, **kwargs):
            return {"text": "summary", "cost": 0.0, "response": {}}

        def get_template_vars(self, **kwargs):
            return {}

        def serialize(self):
            return {"info": {"config": {"model": {}}}}

    config = AgentConfig(
        system_template="sys",
        instance_template="{{task}}",
        workspace=str(tmp_path),
        tools=ToolsConfig(read_only=True, todo=True, edit=True),
        subagents=SubagentsConfig(enabled=True),
        workflow=WorkflowConfig(
            enabled=True,
            dirs=[str(tmp_path / "workflows")],
            run_dir=str(tmp_path / "runs"),
        ),
    )
    agent = DefaultAgent(ScriptedModel(), LocalEnvironment(cwd=str(tmp_path)), **config.model_dump())
    assert {"WorkflowCreate", "WorkflowList", "WorkflowRun", "WorkflowStatus"} <= set(
        agent._tools.names()
    )
    assert agent._workflow_store is not None
    assert "Workflows" in agent._system_content()


def test_definition_rejects_cycles():
    with pytest.raises(ValueError, match="cycle"):
        WorkflowDefinition(
            id="bad",
            phases=[
                WorkflowPhase(name="a", depends_on=["b"]),
                WorkflowPhase(name="b", depends_on=["a"]),
            ],
        )
