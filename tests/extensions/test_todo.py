from minisweagent.extensions.todo import TodoStore, build_todo_tool


def test_todo_tool_roundtrip(tmp_path):
    store = TodoStore(tmp_path, persist=True)
    tool = build_todo_tool(store)

    result = tool.handler(
        {
            "todos": [
                {"content": "read code", "status": "completed", "priority": "high"},
                {"content": "write fix", "status": "in_progress", "priority": "medium"},
                {"content": "run tests", "status": "pending", "priority": "low"},
            ]
        }
    )
    assert result["returncode"] == 0
    assert "Todos (1/3 completed)" in result["output"]
    assert "[in_progress]" in result["output"]

    reloaded = TodoStore(tmp_path, persist=True)
    assert len(reloaded.items) == 3
    assert reloaded.items[1].content == "write fix"
    assert reloaded.items[1].status == "in_progress"


def test_todo_invalid_values_are_normalized(tmp_path):
    store = TodoStore(tmp_path, persist=False)
    tool = build_todo_tool(store)
    result = tool.handler(
        {"todos": [{"content": "x", "status": "nonsense", "priority": "urgent"}]}
    )
    assert result["returncode"] == 0
    assert store.items[0].status == "pending"
    assert store.items[0].priority == "medium"


def test_todo_requires_list(tmp_path):
    tool = build_todo_tool(TodoStore(tmp_path, persist=False))
    result = tool.handler({"todos": "not-a-list"})
    assert result["returncode"] == -1
    assert "must be a list" in result["exception_info"]
