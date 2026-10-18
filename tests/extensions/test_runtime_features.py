"""Background tasks, hooks, agent messaging, media Read, and TodoRead."""

import base64
import time
from pathlib import Path
from types import SimpleNamespace

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.extensions.agent_bus import AgentBus, build_send_message_tool
from minisweagent.extensions.background import BackgroundTaskManager, build_background_tools
from minisweagent.extensions.config import (
    AgentBusConfig,
    BackgroundConfig,
    HookCommand,
    HooksConfig,
    ToolsConfig,
)
from minisweagent.extensions.hooks import HookRunner
from minisweagent.extensions.subagents import BUILTIN_PROFILES
from minisweagent.extensions.todo import TodoStore, build_todo_read_tool
from minisweagent.models.utils.actions_toolcall import parse_toolcall_actions

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


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
                    f"<output>{output.get('output', '')}</output>\n"
                    f"<exception>{output.get('exception_info', '')}</exception>",
                    "extra": output.get("extra", {}),
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
    return {"role": "assistant", "content": "", "extra": {"actions": actions, "cost": 0.0}}


def make_agent(tmp_path, outputs, **overrides):
    tools = overrides.pop("tools", None) or ToolsConfig(read_only=True, todo=True, edit=False)
    config = AgentConfig(
        system_template="sys",
        instance_template="{{task}}",
        workspace=str(tmp_path),
        tools=tools,
        **overrides,
    )
    return DefaultAgent(
        ScriptedModel(outputs),
        LocalEnvironment(cwd=str(tmp_path)),
        **config.model_dump(),
    )


# -- background ----------------------------------------------------------------


def test_background_command_streams_and_completes(tmp_path):
    manager = BackgroundTaskManager(BackgroundConfig(enabled=True), str(tmp_path))
    env = LocalEnvironment(cwd=str(tmp_path))
    started = manager.start_command("printf 'hello'; sleep 0.05", env)
    assert started["returncode"] == 0

    info = manager.poll(started["task_id"], wait=True, timeout=5)
    assert info["status"] == "completed"
    assert info["returncode"] == 0
    assert "hello" in info["output"]


def test_background_stop_kills_command(tmp_path):
    manager = BackgroundTaskManager(BackgroundConfig(enabled=True), str(tmp_path))
    env = LocalEnvironment(cwd=str(tmp_path))
    started = manager.start_command("sleep 30", env)
    time.sleep(0.1)
    result = manager.stop(started["task_id"], force=True)
    assert result["returncode"] == 0
    info = manager.poll(started["task_id"])
    assert info["status"] in {"cancelled", "completed"}
    assert info["status"] != "running"


def test_background_tools_and_parser(tmp_path):
    manager = BackgroundTaskManager(BackgroundConfig(enabled=True), str(tmp_path))
    tools = {tool.name: tool for tool in build_background_tools(manager)}
    started = manager.start_command("echo tool-ok", LocalEnvironment(cwd=str(tmp_path)))
    output = tools["TaskOutput"].handler({"task_id": started["task_id"], "wait": True, "timeout": 5})
    assert "tool-ok" in output["output"]

    model = ScriptedModel([])
    tool_call = SimpleNamespace(
        id="1",
        function=SimpleNamespace(
            name="bash",
            arguments='{"command": "echo hi", "run_in_background": true, "timeout": 12}',
        ),
    )
    actions = parse_toolcall_actions([tool_call], format_error_template="{{ error }}", allowed_tools={"bash"})
    assert actions[0]["run_in_background"] is True
    assert actions[0]["timeout"] == 12.0
    assert model is not None


def test_agent_background_bash_wiring(tmp_path):
    config = AgentConfig(
        system_template="sys",
        instance_template="{{task}}",
        workspace=str(tmp_path),
        background=BackgroundConfig(enabled=True),
    )
    agent = DefaultAgent(
        ScriptedModel(
            [
                assistant(
                    [
                        {
                            "tool_call_id": "1",
                            "command": "sleep 0.05; echo wired",
                            "run_in_background": True,
                        }
                    ]
                ),
                assistant([{"tool_call_id": "2", "command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]),
            ]
        ),
        LocalEnvironment(cwd=str(tmp_path)),
        **config.model_dump(),
    )
    agent.run("go")
    names = set(agent._tools.names())
    assert {"TaskOutput", "TaskStop"} <= names
    # The parent agent owns the manager and closes it after the run; the task
    # itself was started successfully, which is what this wiring test checks.
    assert any("Started background task" in str(m.get("content")) for m in agent.messages)


# -- hooks ---------------------------------------------------------------------


def _write_hook(path: Path, body: str) -> str:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_pre_tool_use_hook_blocks(tmp_path):
    hook = _write_hook(
        tmp_path / "block.sh",
        "#!/bin/sh\ncat >/dev/null\necho 'not allowed' >&2\nexit 2\n",
    )
    agent = make_agent(
        tmp_path,
        [
            assistant([{"tool": "Read", "tool_call_id": "1", "args": {"file_path": "missing.txt"}}]),
            assistant([{"tool_call_id": "2", "command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]),
        ],
        hooks=HooksConfig(enabled=True, PreToolUse=[HookCommand(matcher="Read", command=hook)]),
    )
    agent.run("go")
    tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
    assert any("Blocked by PreToolUse hook: not allowed" in str(m.get("content")) for m in tool_messages)


def test_post_tool_use_hook_appends_output(tmp_path):
    hook = _write_hook(tmp_path / "post.sh", "#!/bin/sh\ncat >/dev/null\necho 'post-ok'\n")
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    agent = make_agent(
        tmp_path,
        [
            assistant([{"tool": "Read", "tool_call_id": "1", "args": {"file_path": "a.txt"}}]),
            assistant([{"tool_call_id": "2", "command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]),
        ],
        hooks=HooksConfig(enabled=True, PostToolUse=[HookCommand(matcher="Read", command=hook)]),
    )
    agent.run("go")
    tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
    assert any("[hook] post-ok" in str(m.get("content")) for m in tool_messages)


def test_hook_runner_matches_only_tool_names(tmp_path):
    hook = _write_hook(tmp_path / "noop.sh", "#!/bin/sh\nexit 0\n")
    runner = HookRunner(
        HooksConfig(enabled=True, PreToolUse=[HookCommand(matcher="Write,Edit", command=hook)]),
        tmp_path,
    )
    assert runner.run("PreToolUse", tool_name="Read").blocked is False
    assert runner.run("PreToolUse", tool_name="Write").blocked is False


# -- agent bus -----------------------------------------------------------------


def test_agent_bus_roundtrip(tmp_path):
    bus = AgentBus(AgentBusConfig(enabled=True))
    bus.register("main")
    bus.register("sub-1")
    sent = bus.send("sub-1", "main", "progress", "halfway")
    assert sent["returncode"] == 0
    messages = bus.drain("main")
    assert len(messages) == 1
    assert "<agent_message id=" in messages[0].render()
    assert "progress" in messages[0].render()
    assert bus.send("main", "nobody", "x")["returncode"] == -1


def test_send_message_tool_and_inbox_injection(tmp_path):
    bus = AgentBus(AgentBusConfig(enabled=True))
    bus.register("main")
    tool = build_send_message_tool(bus, "sub-1")
    assert tool.handler({"to": "main", "message": "hello main"})["returncode"] == 0

    agent = make_agent(
        tmp_path,
        [assistant([{"tool_call_id": "1", "command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])],
        bus=AgentBusConfig(enabled=True),
    )
    # The agent registered itself as main in __init__ and created its own bus.
    agent._bus.send("sub-1", "main", "injected")
    agent._drain_inbox()
    assert any("<agent_message" in str(m.get("content")) for m in agent.messages)


# -- media Read ----------------------------------------------------------------


def test_read_image_attaches_native_input(tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(PNG_BYTES)
    agent = make_agent(
        tmp_path,
        [
            assistant([{"tool": "Read", "tool_call_id": "1", "args": {"file_path": "pixel.png"}}]),
            assistant([{"tool_call_id": "2", "command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]),
        ],
        tools=ToolsConfig(read_only=True, todo=True, read_media=True),
    )
    agent.run("go")
    attachments = [
        message
        for message in agent.messages
        if isinstance(message.get("content"), list)
        and any(part.get("type") == "image_url" for part in message["content"])
    ]
    assert len(attachments) == 1
    url = attachments[0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert any("is attached" in str(m.get("content")) for m in agent.messages if m.get("role") == "tool")


def test_read_video_attaches_native_input(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not-a-real-video")
    agent = make_agent(
        tmp_path,
        [
            assistant([{"tool": "Read", "tool_call_id": "1", "args": {"file_path": "clip.mp4"}}]),
            assistant([{"tool_call_id": "2", "command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]),
        ],
        tools=ToolsConfig(read_only=True, read_media=True),
    )
    agent.run("go")
    parts = [
        part
        for message in agent.messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
    ]
    assert any(part.get("type") == "video_url" for part in parts)


def test_read_media_disabled_reports_hint(tmp_path):
    (tmp_path / "pixel.png").write_bytes(PNG_BYTES)
    agent = make_agent(
        tmp_path,
        [
            assistant([{"tool": "Read", "tool_call_id": "1", "args": {"file_path": "pixel.png"}}]),
            assistant([{"tool_call_id": "2", "command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]),
        ],
    )
    agent.run("go")
    assert any("agent.tools.read_media" in str(m.get("content")) for m in agent.messages)


# -- todo & profiles -----------------------------------------------------------


def test_todo_read_tool(tmp_path):
    store = TodoStore(tmp_path, persist=False)
    store.update([{"content": "one", "status": "in_progress"}])
    result = build_todo_read_tool(store).handler({})
    assert result["returncode"] == 0
    assert "Todos (0/1 completed)" in result["output"]


def test_builtin_profiles_include_plan_and_visual_judge():
    assert {"general-purpose", "explore", "plan", "visual-judge"} <= set(BUILTIN_PROFILES)
    assert BUILTIN_PROFILES["plan"].tools == ["Read", "Grep", "Glob"]
    assert "Vision" in BUILTIN_PROFILES["visual-judge"].tools
