from types import SimpleNamespace

import pytest

from minisweagent.exceptions import FormatError
from minisweagent.models.utils.actions_toolcall import parse_toolcall_actions


def tool_call(name: str, arguments: str, call_id: str = "call_1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def test_bash_action_shape_is_unchanged():
    actions = parse_toolcall_actions(
        [tool_call("bash", '{"command": "ls"}')],
        format_error_template="{{ error }}",
        allowed_tools={"bash"},
    )
    assert actions == [{"command": "ls", "tool_call_id": "call_1"}]


def test_extension_tool_action_shape():
    actions = parse_toolcall_actions(
        [tool_call("Skill", '{"name": "pdf"}')],
        format_error_template="{{ error }}",
        allowed_tools={"bash", "Skill"},
    )
    assert actions == [{"tool": "Skill", "args": {"name": "pdf"}, "tool_call_id": "call_1"}]


def test_unknown_tool_rejected():
    with pytest.raises(FormatError):
        parse_toolcall_actions(
            [tool_call("Nope", "{}")],
            format_error_template="{{ error }}",
            allowed_tools={"bash"},
        )


def test_bash_requires_command():
    with pytest.raises(FormatError):
        parse_toolcall_actions(
            [tool_call("bash", '{"other": 1}')],
            format_error_template="{{ error }}",
            allowed_tools={"bash"},
        )
