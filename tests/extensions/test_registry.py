import pytest

from minisweagent.extensions.registry import Tool, ToolRegistry, err, ok


def make_tool(name: str, calls: list | None = None) -> Tool:
    def handler(args: dict) -> dict:
        if calls is not None:
            calls.append((name, args))
        return ok(name)

    return Tool(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )


def test_add_call_and_order():
    calls = []
    registry = ToolRegistry()
    registry.add(make_tool("Read", calls))
    registry.add(make_tool("mcp__fs__read_file", calls))

    assert registry.names() == ["Read", "mcp__fs__read_file"]
    assert registry.call("Read", {"path": "a"}) == ok("Read")
    assert calls == [("Read", {"path": "a"})]
    assert len(registry.specs()) == 2


def test_filter_patterns():
    registry = ToolRegistry()
    registry.extend([make_tool("Read"), make_tool("Grep"), make_tool("mcp__fs__read_file")])

    assert sorted(registry.filter(None).names()) == ["Grep", "Read", "mcp__fs__read_file"]
    assert sorted(registry.filter(["*"]).names()) == ["Grep", "Read", "mcp__fs__read_file"]
    assert registry.filter(["mcp__*"]).names() == ["mcp__fs__read_file"]
    assert registry.filter([]).names() == []


def test_override_and_bash_reserved():
    registry = ToolRegistry()
    registry.add(make_tool("Read"))
    replacement = make_tool("Read")
    registry.add(replacement)
    assert registry._tools["Read"] is replacement

    with pytest.raises(ValueError):
        registry.add(make_tool("bash"))


def test_unknown_tool_and_handler_exception():
    registry = ToolRegistry()
    unknown = registry.call("nope", {})
    assert unknown["returncode"] == -1
    assert "Unknown tool" in unknown["exception_info"]

    def boom(_args):
        raise RuntimeError("boom")

    registry.add(Tool(name="boom", description="", parameters={}, handler=boom))
    failed = registry.call("boom", {})
    assert failed["returncode"] == -1
    assert "RuntimeError: boom" in failed["exception_info"]


def test_ok_and_err_helpers():
    assert ok("x")["returncode"] == 0
    assert err("bad")["exception_info"] == "bad"
