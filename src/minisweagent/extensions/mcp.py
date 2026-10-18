"""MCP (Model Context Protocol) client bridge.

Servers are started once per agent, in background event loops, and their tools
are exposed as ``mcp__<server>__<tool>``. A failing server is skipped unless
``agent.mcp.required`` is true.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

import yaml

from minisweagent.extensions.config import McpConfig, McpServerConfig
from minisweagent.extensions.registry import Tool, err, ok

logger = logging.getLogger("extensions.mcp")


def sanitize_name(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", value or "")
    return sanitized.strip("_") or "unnamed"


def mcp_tool_name(prefix: str, server: str, tool: str) -> str:
    return f"{prefix}__{sanitize_name(server)}__{sanitize_name(tool)}"


def _normalize_server(data: dict) -> dict:
    data = dict(data)
    aliases = {
        "environment": "env",
        "http_headers": "headers",
        "enable": "enabled",
    }
    for old, new in aliases.items():
        if old in data and new not in data:
            data[new] = data.pop(old)
    if data.get("type") == "remote":
        data["type"] = "http"
    return data


def load_server_configs(config: McpConfig, workspace: Path) -> dict[str, McpServerConfig]:
    raw: dict[str, Any] = {}
    if config.config_file:
        path = Path(config.config_file).expanduser()
        if not path.is_absolute():
            path = workspace / path
        if not path.is_file():
            raise FileNotFoundError(f"MCP config file not found: {path}")
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix.lower() == ".json" else (yaml.safe_load(text) or {})
        servers = data.get("mcpServers") or data.get("servers") or {}
        if not isinstance(servers, dict):
            raise ValueError(f"MCP config {path} must contain a mapping under 'mcpServers'")
        raw.update(servers)

    for name, value in config.servers.items():
        raw[name] = value

    resolved: dict[str, McpServerConfig] = {}
    for name, value in raw.items():
        if isinstance(value, McpServerConfig):
            server = value
        else:
            server = McpServerConfig(**_normalize_server(value))
        if not server.enabled:
            continue
        if server.type == "stdio" and not server.command:
            raise ValueError(f"MCP server {name!r}: 'command' is required for stdio transport")
        if server.type in {"http", "sse"} and not server.url:
            raise ValueError(f"MCP server {name!r}: 'url' is required for {server.type} transport")
        resolved[name] = server
    return resolved


class _ServerConnection:
    def __init__(self, name: str, config: McpServerConfig, timeout: float) -> None:
        self.name = name
        self.config = config
        self.timeout = timeout
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.error: BaseException | None = None
        self.session: Any = None
        self.tools: list[Any] = []
        self.thread = threading.Thread(target=self._thread_main, name=f"mcp-{name}", daemon=True)

    def start(self) -> _ServerConnection:
        self.thread.start()
        if not self.ready.wait(self.timeout + 5):
            raise TimeoutError(f"MCP server {self.name!r} did not become ready in {self.timeout + 5:.0f}s")
        if self.error is not None:
            raise self.error
        return self

    def _thread_main(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except BaseException as exc:  # noqa: BLE001 - surfaced through start()
            self.error = exc
        finally:
            self.ready.set()
            try:
                self.loop.close()
            except Exception:
                pass

    async def _main(self) -> None:
        config = self.config
        http_client = None
        try:
            if config.type == "stdio":
                from mcp import StdioServerParameters
                from mcp.client.stdio import stdio_client

                params = StdioServerParameters(
                    command=config.command or "",
                    args=list(config.args),
                    env=config.env or None,
                    cwd=config.cwd,
                )
                context_manager = stdio_client(params)
            elif config.type == "sse":
                from mcp.client.sse import sse_client

                context_manager = sse_client(config.url or "", headers=config.headers or None)
            else:
                try:
                    from mcp.client.streamable_http import streamable_http_client
                except ImportError:  # MCP SDK 1.x
                    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

                if config.headers:
                    import httpx2

                    http_client = httpx2.AsyncClient(headers=config.headers)
                context_manager = streamable_http_client(config.url or "", http_client=http_client)

            async with context_manager as streams:
                read_stream, write_stream = streams[0], streams[1]
                from mcp import ClientSession

                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    self.session = session
                    result = await session.list_tools()
                    self.tools = list(getattr(result, "tools", []) or [])
                    self.ready.set()
                    while not self.stop_event.is_set():
                        await asyncio.sleep(0.1)
        finally:
            if http_client is not None:
                await http_client.aclose()

    def call(self, tool_name: str, arguments: dict) -> Any:
        if self.session is None:
            raise RuntimeError(f"MCP server {self.name!r} is not connected")
        future = asyncio.run_coroutine_threadsafe(
            self.session.call_tool(tool_name, arguments or {}, read_timeout_seconds=self.timeout),
            self.loop,
        )
        return future.result(timeout=self.timeout + 10)

    def close(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)


class McpManager:
    def __init__(self, config: McpConfig, workspace: Path) -> None:
        self.config = config
        self.workspace = workspace
        self.connections: dict[str, _ServerConnection] = {}
        self.tools: list[Tool] = []
        self.diagnostics: list[str] = []

    def start(self) -> McpManager:
        try:
            import mcp  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "MCP support requires the 'mcp' package. Install it with "
                "`pip install 'mini-swe-agent[extensions]'` or `pip install mcp`."
            ) from exc
        servers = load_server_configs(self.config, self.workspace)
        for name, server in servers.items():
            try:
                connection = _ServerConnection(name, server, self.config.timeout_seconds).start()
            except Exception as exc:
                message = f"{name}: {type(exc).__name__}: {exc}"
                self.diagnostics.append(message)
                if self.config.required:
                    raise RuntimeError(f"Required MCP server failed to start: {message}") from exc
                logger.warning("MCP server %s failed to start: %s", name, exc)
                continue
            self.connections[name] = connection
            for remote_tool in connection.tools:
                self.tools.append(self._make_tool(connection, remote_tool))
        return self

    def _make_tool(self, connection: _ServerConnection, remote_tool: Any) -> Tool:
        original_name = getattr(remote_tool, "name", "tool")
        parameters = getattr(remote_tool, "inputSchema", None) or {
            "type": "object",
            "properties": {},
        }
        annotations = getattr(remote_tool, "annotations", None)
        read_only = bool(getattr(annotations, "readOnlyHint", True)) if annotations is not None else True

        def handler(args: dict) -> dict:
            result = connection.call(original_name, args)
            return self._render_result(result)

        return Tool(
            name=mcp_tool_name(self.config.tool_prefix, connection.name, original_name),
            description=f"[MCP:{connection.name}] {getattr(remote_tool, 'description', '') or ''}".strip(),
            parameters=parameters,
            handler=handler,
            read_only=read_only,
            metadata={"mcp_server": connection.name, "mcp_tool": original_name},
        )

    def _render_result(self, result: Any) -> dict:
        parts: list[str] = []
        for item in getattr(result, "content", None) or []:
            item_type = getattr(item, "type", "")
            if item_type == "text":
                parts.append(getattr(item, "text", ""))
            elif item_type == "image":
                parts.append(f"[image {getattr(item, 'mimeType', '')}]")
            elif item_type == "resource":
                resource = getattr(item, "resource", None)
                uri = getattr(resource, "uri", "")
                text = getattr(resource, "text", None)
                parts.append(f"[resource {uri}]" + (f"\n{text}" if text else ""))
            elif item_type == "resource_link":
                parts.append(f"[resource_link {getattr(item, 'uri', '')}]")
            else:
                parts.append(f"[{item_type or 'content'}]")
        text = "\n".join(part for part in parts if part)
        if len(text) > self.config.max_output_chars:
            text = text[: self.config.max_output_chars] + "\n...[truncated]"
        is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
        return err(text or "(no output)") if is_error else ok(text)

    @property
    def server_names(self) -> list[str]:
        return sorted(self.connections)

    def close(self) -> None:
        for connection in self.connections.values():
            connection.close()
        self.connections.clear()
