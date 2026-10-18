"""Session autosave, resume, and SessionRead."""

import json
from types import SimpleNamespace

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.agents.session import SessionStore, build_session_read_tool, sanitize_messages
from minisweagent.environments.local import LocalEnvironment
from minisweagent.extensions.config import SessionConfig, ToolsConfig


class ScriptedModel:
    def __init__(self, outputs):
        self.config = SimpleNamespace(model_name="scripted", model_kwargs={})
        self.outputs = list(outputs)
        self.index = 0

    def set_extra_tools(self, tools):
        pass

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
                "content": f"<returncode>{output.get('returncode')}</returncode>\n<output>{output.get('output', '')}</output>",
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


def submit(call_id="9"):
    return assistant([{"tool_call_id": call_id, "command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])


def make_agent(tmp_path, outputs, session_dir):
    config = AgentConfig(
        system_template="sys",
        instance_template="{{task}}",
        workspace=str(tmp_path),
        tools=ToolsConfig(todo=True, todo_persist=False),
        session=SessionConfig(enabled=True, dir=str(session_dir)),
    )
    return DefaultAgent(
        ScriptedModel(outputs), LocalEnvironment(cwd=str(tmp_path)), **config.model_dump()
    )


def test_session_is_saved_and_resumed(tmp_path):
    session_dir = tmp_path / "sessions"
    first = make_agent(
        tmp_path,
        [
            assistant(
                [
                    {
                        "tool": "TodoWrite",
                        "tool_call_id": "1",
                        "args": {"todos": [{"content": "finish", "status": "in_progress"}]},
                    }
                ]
            ),
            submit("2"),
        ],
        session_dir,
    )
    result = first.run("do the thing")
    assert result["exit_status"] == "Submitted"
    assert first._session_id

    path = session_dir / f"{first._session_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "Submitted"
    assert data["n_calls"] == 2
    assert data["todos"][0]["content"] == "finish"
    assert data["workspace"] == str(tmp_path.resolve())

    second = make_agent(tmp_path, [submit("3")], session_dir)
    result = second.run("", resume=first._session_id)
    assert result["exit_status"] == "Submitted"
    assert second._resumed is True
    assert second.n_calls == 3
    # The restored todo list comes from the session, not from disk persistence.
    assert [item.content for item in second._todo.items] == ["finish"]
    assert any("Session resumed" in str(message.get("content")) for message in second.messages)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["n_calls"] == 3
    assert saved["id"] == first._session_id


def test_session_read_tool(tmp_path):
    session_dir = tmp_path / "sessions"
    agent = make_agent(tmp_path, [submit("1")], session_dir)
    agent.run("investigate the frobnicator")
    store = SessionStore(SessionConfig(enabled=True, dir=str(session_dir)), tmp_path)
    tool = build_session_read_tool(store)
    result = tool.handler({"query": "frobnicator"})
    assert result["returncode"] == 0
    assert "investigate the frobnicator" in result["output"]
    assert "<session id=" in result["output"]


def test_sanitize_messages_drops_api_response_but_keeps_media():
    messages = [
        {
            "role": "assistant",
            "content": "hi",
            "extra": {"response": {"huge": "payload"}, "cost": 0.1},
        },
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}],
        },
    ]
    cleaned = sanitize_messages(messages, keep_media=True)
    assert "response" not in cleaned[0]["extra"]
    assert cleaned[0]["extra"]["cost"] == 0.1
    assert cleaned[1]["content"][0]["type"] == "image_url"
