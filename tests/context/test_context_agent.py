from types import SimpleNamespace

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.extensions.config import ContextConfig, SkillsConfig, ToolsConfig


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
        return [
            {
                "role": "tool",
                "tool_call_id": action.get("tool_call_id"),
                "content": f"<returncode>{output.get('returncode')}</returncode>\n"
                f"<output>{output.get('output', '')}</output>",
            }
            for action, output in zip(message["extra"]["actions"], outputs)
        ]

    def generate_text(self, messages, **kwargs):
        return {"text": "summary", "cost": 0.0, "response": {}}

    def get_template_vars(self, **kwargs):
        return {}

    def serialize(self):
        return {"info": {"config": {"model": {}}}}


def assistant(actions):
    return {"role": "assistant", "content": "", "extra": {"actions": actions, "cost": 0.0}}


def test_context_agent_meta_user_and_ephemeral_reminder(tmp_path):
    skill = tmp_path / "skills" / "hello"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: hello\ndescription: Say hello\n---\nSay hello.\n", encoding="utf-8"
    )

    model = ScriptedModel(
        [
            assistant(
                [
                    {
                        "tool": "TodoWrite",
                        "tool_call_id": "1",
                        "args": {"todos": [{"content": "do it", "status": "in_progress"}]},
                    }
                ]
            ),
            assistant(
                [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "tool_call_id": "2"}]
            ),
        ]
    )
    config = AgentConfig(
        system_template="BASE_SYSTEM",
        instance_template="TASK:{{task}}",
        workspace=str(tmp_path),
        context=ContextConfig(enabled=True, env_info=False, current_date=False),
        tools=ToolsConfig(todo=True),
        skills=SkillsConfig(enabled=True, dirs=["skills"]),
    )
    agent = DefaultAgent(model, LocalEnvironment(cwd=str(tmp_path)), **config.model_dump())
    result = agent.run("hello")

    assert result["exit_status"] == "Submitted"
    system = agent.messages[0]["content"]
    first_user = agent.messages[1]["content"]

    assert "BASE_SYSTEM" in system
    assert "The following skills are available" not in system
    assert first_user.startswith("<meta_user>")
    assert "The following skills are available" in first_user
    assert "TASK:hello" in first_user

    api_messages = agent._messages_for_model()
    reminder = api_messages[-1]["content"]
    assert reminder.startswith("<system-reminder>")
    assert "do it" in reminder
    assert all("<system-reminder>" not in (message.get("content") or "") for message in agent.messages)
