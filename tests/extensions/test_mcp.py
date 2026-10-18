import json
from pathlib import Path
from types import SimpleNamespace

from minisweagent.extensions.config import McpConfig
from minisweagent.extensions.mcp import (
    McpManager,
    load_server_configs,
    mcp_tool_name,
    sanitize_name,
)


def test_tool_name_sanitization():
    assert sanitize_name("fs server") == "fs_server"
    assert sanitize_name("read-file") == "read_file"
    assert sanitize_name("***") == "unnamed"
    assert mcp_tool_name("mcp", "fs server", "read-file") == "mcp__fs_server__read_file"


def test_load_json_config_with_legacy_fields(tmp_path):
    (tmp_path / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fs": {
                        "command": "npx",
                        "args": ["-y", "server"],
                        "environment": {"TOKEN": "x"},
                        "enable": True,
                    },
                    "remote": {"type": "remote", "url": "http://localhost:1", "enabled": False},
                }
            }
        ),
        encoding="utf-8",
    )
    config = McpConfig(enabled=True, config_file="mcp.json")
    servers = load_server_configs(config, tmp_path)

    assert set(servers) == {"fs"}
    assert servers["fs"].env == {"TOKEN": "x"}
    assert servers["fs"].enabled is True


def test_typed_servers_and_disabled(tmp_path):
    config = McpConfig(
        enabled=True,
        servers={
            "sse": {"type": "sse", "url": "http://example.invalid/sse"},
            "off": {"command": "nope", "enabled": False},
        },
    )
    servers = load_server_configs(config, tmp_path)
    assert set(servers) == {"sse"}
    assert servers["sse"].type == "sse"


def test_render_text_error_and_resource():
    manager = McpManager(McpConfig(), Path("."))
    result = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hello")],
        isError=False,
    )
    rendered = manager._render_result(result)
    assert rendered["output"] == "hello"
    assert rendered["returncode"] == 0

    failed = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="bad thing")],
        isError=True,
    )
    assert manager._render_result(failed)["returncode"] == -1

    resource = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="resource",
                resource=SimpleNamespace(uri="file:///tmp/x", text="data"),
            )
        ],
        isError=False,
    )
    assert "file:///tmp/x" in manager._render_result(resource)["output"]
    assert "data" in manager._render_result(resource)["output"]


def test_output_truncation():
    manager = McpManager(McpConfig(max_output_chars=10), Path("."))
    result = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="x" * 100)],
        isError=False,
    )
    output = manager._render_result(result)["output"]
    assert output.startswith("x" * 10)
    assert "truncated" in output


def test_make_tool_binds_name_and_arguments():
    manager = McpManager(McpConfig(tool_prefix="mcp"), Path("."))
    connection = SimpleNamespace(
        name="fs",
        call=lambda name, args: SimpleNamespace(
            content=[SimpleNamespace(type="text", text=f"{name}:{args}")],
            isError=False,
        ),
    )
    remote_tool = SimpleNamespace(
        name="read-file",
        description="Read a file",
        inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
        annotations=None,
    )

    tool = manager._make_tool(connection, remote_tool)
    assert tool.name == "mcp__fs__read_file"
    assert tool.description == "[MCP:fs] Read a file"
    assert tool.parameters["properties"]["path"]["type"] == "string"
    assert tool.handler({"path": "a"})["output"] == "read-file:{'path': 'a'}"
