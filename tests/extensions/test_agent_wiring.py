"""End-to-end wiring test: plugins + skills + prompts + TodoWrite + read-before-edit."""

import json
from pathlib import Path
from types import SimpleNamespace

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.extensions.config import PluginsConfig, PromptsConfig, SkillsConfig, ToolsConfig


class ScriptedModel:
    def __init__(self, outputs):
        self.config = SimpleNamespace(model_name="scripted", model_kwargs={})
        self.outputs = outputs
        self.index = 0
        self.tools = []

    def set_extra_tools(self, tools):
        self.tools = tools

    def query(self, messages, **kwargs):
        output = self.outputs[self.index]
        self.index += 1
        return output

    def format_message(self, **kwargs):
        return kwargs

    def format_observation_messages(self, message, outputs, template_vars=None):
        results = []
        for action, output in zip(message["extra"]["actions"], outputs):
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": action.get("tool_call_id"),
                    "content": f"<returncode>{output.get('returncode')}</returncode>\n"
                    f"<output>{output.get('output', '')}</output>",
                }
            )
        return results

    def generate_text(self, messages, **kwargs):
        return {"text": "summary", "cost": 0.0, "response": {}}

    def get_template_vars(self, **kwargs):
        return {}

    def serialize(self):
        return {"info": {"config": {"model": {}}}}


def assistant(actions):
    return {
        "role": "assistant",
        "content": "",
        "extra": {"actions": actions, "cost": 0.0},
    }


def write_plugin(root: Path) -> None:
    plugin = root / "plugins" / "demo"
    (plugin / "skills" / "hello").mkdir(parents=True)
    (plugin / "skills" / "hello" / "SKILL.md").write_text(
        "---\nname: hello\ndescription: Say hello\n---\nSay hello.\n", encoding="utf-8"
    )
    (plugin / "prompts").mkdir()
    (plugin / "prompts" / "rules.md").write_text("PLUGIN_PROMPT_MARKER", encoding="utf-8")
    (plugin / "tools").mkdir()
    (plugin / "tools" / "hello.py").write_text(
        "from minisweagent.extensions.registry import Tool, ok\n\n"
        "def register(registry, context):\n"
        "    registry.add(Tool(name='plugin_hello', description='hi', "
        "parameters={'type': 'object', 'properties': {}}, "
        "handler=lambda args: ok('PLUGIN_TOOL_OK')))\n",
        encoding="utf-8",
    )
    (plugin / "plugin.yaml").write_text(
        "name: demo\nenabled: true\nskills: [skills]\nprompts: [prompts]\ntools: [tools/hello.py]\n",
        encoding="utf-8",
    )


def test_full_agent_wiring(tmp_path):
    write_plugin(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("hello world", encoding="utf-8")

    model = ScriptedModel(
        [
            assistant(
                [
                    {
                        "tool": "TodoWrite",
                        "tool_call_id": "1",
                        "args": {"todos": [{"content": "read", "status": "in_progress"}]},
                    },
                    {"tool": "plugin_hello", "tool_call_id": "2", "args": {}},
                ]
            ),
            assistant(
                [
                    {"tool": "Read", "tool_call_id": "3", "args": {"file_path": "a.txt"}},
                ]
            ),
            assistant(
                [
                    {
                        "tool": "Edit",
                        "tool_call_id": "4",
                        "args": {
                            "file_path": "a.txt",
                            "old_string": "hello",
                            "new_string": "goodbye",
                        },
                    }
                ]
            ),
            assistant(
                [
                    {
                        "command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                        "tool_call_id": "5",
                    }
                ]
            ),
        ]
    )
    config = AgentConfig(
        system_template="sys",
        instance_template="{{task}}",
        workspace=str(tmp_path),
        tools=ToolsConfig(todo=True, edit=True),
        plugins=PluginsConfig(enabled=True, dirs=["plugins"]),
        skills=SkillsConfig(enabled=True, dirs=[]),
        prompts=PromptsConfig(files=[]),
    )
    agent = DefaultAgent(model, LocalEnvironment(cwd=str(tmp_path)), **config.model_dump())
    result = agent.run("do the thing")

    assert result["exit_status"] == "Submitted"
    assert target.read_text(encoding="utf-8") == "goodbye world"

    tool_names = set(agent._tools.names())
    assert {"TodoWrite", "Read", "Edit", "Write", "Skill", "plugin_hello"} <= tool_names

    system = agent.messages[0]["content"]
    assert "PLUGIN_PROMPT_MARKER" in system
    assert "The following skills are available" in system
    assert "hello" in system
    assert "TodoWrite" in system

    todos = json.loads((tmp_path / ".mini-swe-agent" / "todos.json").read_text(encoding="utf-8"))
    assert todos["todos"][0]["content"] == "read"

    observations = [
        message["content"] for message in agent.messages if message.get("role") == "tool"
    ]
    assert any("PLUGIN_TOOL_OK" in observation for observation in observations)
