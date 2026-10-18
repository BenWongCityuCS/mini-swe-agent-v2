from minisweagent.context.compact import CLEARED_TOOL_RESULT, maybe_microcompact


def tool_message(index: int) -> dict:
    return {
        "role": "tool",
        "content": "x" * 1000,
        "tool_call_id": str(index),
        "extra": {"raw_output": "x" * 1000},
    }


def test_clears_old_tool_results_and_keeps_recent():
    messages = [tool_message(index) for index in range(8)]
    updated, saved, cleared = maybe_microcompact(messages, keep_recent=5, min_token_savings=1)

    assert cleared == 3
    assert saved > 0
    for index in range(3):
        assert updated[index]["content"] == CLEARED_TOOL_RESULT
        assert updated[index]["extra"]["microcompacted"] is True
    for index in range(3, 8):
        assert updated[index]["content"] == "x" * 1000
    assert messages[0]["content"] == "x" * 1000  # input is not mutated


def test_not_enough_savings_leaves_messages_untouched():
    messages = [tool_message(index) for index in range(8)]
    updated, saved, cleared = maybe_microcompact(messages, keep_recent=5, min_token_savings=100_000)
    assert updated is messages
    assert saved == 0
    assert cleared == 0


def test_keeps_task_message_and_small_outputs():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "the task"},
        {"role": "tool", "content": "small", "tool_call_id": "1"},
        {"role": "tool", "content": "x" * 1000, "tool_call_id": "2"},
        {"role": "tool", "content": "y" * 1000, "tool_call_id": "3"},
    ]
    updated, _, cleared = maybe_microcompact(messages, keep_recent=1, min_token_savings=1)
    assert updated[0]["content"] == "sys"
    assert updated[1]["content"] == "the task"
    assert updated[2]["content"] == "small"
    assert updated[3]["content"] == CLEARED_TOOL_RESULT
    assert updated[4]["content"] == "y" * 1000
    assert cleared == 1
