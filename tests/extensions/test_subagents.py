from pathlib import Path

from minisweagent.agents.default import AgentConfig
from minisweagent.environments.local import LocalEnvironment
from minisweagent.extensions.config import SubagentsConfig
from minisweagent.extensions.registry import ToolRegistry
from minisweagent.extensions.subagents import (
    build_task_tool,
    format_profiles_for_prompt,
    load_agent_profiles,
    parse_agent_profile,
)
from minisweagent.models.test_models import DeterministicModel, make_output


def write_profile(root: Path, name: str, body: str = "", tools: str = "[]") -> Path:
    path = root / "agents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {name} worker\ntools: {tools}\ninject_prompts: false\n---\n{body}",
        encoding="utf-8",
    )
    return path


def make_parent(tmp_path: Path, outputs=None):
    class Parent:
        pass

    parent = Parent()
    parent.model = DeterministicModel(
        outputs=outputs
        or [make_output(None, [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])]
    )
    parent.env = LocalEnvironment(cwd=str(tmp_path))
    parent.config = AgentConfig(
        system_template="sys",
        instance_template="{{task}}",
        subagents=SubagentsConfig(enabled=True, profile_dirs=["agents"]),
    )
    parent._tools = ToolRegistry()
    parent._workspace = tmp_path
    parent._prompt_sections = []
    parent._skills_section = ""
    parent._subagent_depth = 0
    parent.cost = 0.0
    parent.n_calls = 0
    parent.n_compaction_calls = 0
    return parent


def test_parse_and_load_profiles(tmp_path):
    path = write_profile(tmp_path, "worker", "You are a worker.")
    profile = parse_agent_profile(path)
    assert profile is not None
    assert profile.name == "worker"
    assert profile.tools == []
    assert profile.inject_prompts is False
    assert profile.system_prompt == "You are a worker."

    config = SubagentsConfig(enabled=True, profile_dirs=["agents"])
    profiles = load_agent_profiles(config, tmp_path)
    assert "general-purpose" in profiles
    assert "explore" in profiles
    assert "worker" in profiles

    prompt = format_profiles_for_prompt(profiles)
    assert "Available agent types for the Task tool:" in prompt
    assert "- worker: worker worker (Tools: (manager only))" in prompt


def test_profile_model_switch_and_reasoning(tmp_path):
    directory = tmp_path / "agents"
    directory.mkdir()
    path = directory / "smart.md"
    path.write_text(
        "---\n"
        "name: smart\n"
        "description: uses another model\n"
        "tools: [Read]\n"
        "model: openai/another-model\n"
        "model_class: litellm\n"
        "model_kwargs:\n"
        "  max_tokens: 1234\n"
        "reasoning_effort: high\n"
        "---\n"
        "Think hard.\n",
        encoding="utf-8",
    )
    profile = parse_agent_profile(path)
    assert profile is not None
    assert profile.model_name == "openai/another-model"
    assert profile.model_class == "litellm"
    assert profile.model_kwargs == {"max_tokens": 1234}
    assert profile.reasoning_effort == "high"
    assert profile.tools == ["Read"]


def test_task_tool_runs_subagent(tmp_path):
    write_profile(tmp_path, "worker")
    parent = make_parent(tmp_path)
    profiles = load_agent_profiles(parent.config.subagents, tmp_path)
    tool = build_task_tool(parent, profiles, parent.config.subagents)

    result = tool.handler({"description": "do it", "prompt": "finish", "subagent_type": "worker"})
    assert result["returncode"] == 0
    assert '<subagent_result type="worker"' in result["output"]
    assert 'status="Submitted"' in result["output"]
    assert parent.cost > 0
    assert parent.n_calls > 0


def test_task_tool_errors(tmp_path):
    write_profile(tmp_path, "worker")
    parent = make_parent(tmp_path)
    profiles = load_agent_profiles(parent.config.subagents, tmp_path)
    tool = build_task_tool(parent, profiles, parent.config.subagents)

    missing_prompt = tool.handler({"description": "x", "subagent_type": "worker"})
    assert missing_prompt["returncode"] == -1
    assert "prompt is required" in missing_prompt["exception_info"]

    unknown = tool.handler({"description": "x", "prompt": "y", "subagent_type": "nope"})
    assert unknown["returncode"] == -1
    assert "Unknown subagent_type" in unknown["exception_info"]

    parent._subagent_depth = 1
    depth = tool.handler({"description": "x", "prompt": "y", "subagent_type": "worker"})
    assert depth["returncode"] == -1
    assert "depth limit" in depth["exception_info"]
