"""A tiny tool registry shared by skills, subagents, MCP, and built-in tools."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch

logger = logging.getLogger("extensions")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], dict]
    read_only: bool = True
    metadata: dict = field(default_factory=dict)

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


class ToolRegistry:
    """Ordered collection of tools. Later registrations with the same name win."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        if tool.name == "bash":
            raise ValueError("'bash' is built in and cannot be registered as an extension tool")
        if tool.name in self._tools:
            logger.debug("overriding extension tool %s", tool.name)
        self._tools[tool.name] = tool

    def extend(self, tools: Iterable[Tool]) -> None:
        for tool in tools:
            self.add(tool)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[dict]:
        return [tool.spec() for tool in self._tools.values()]

    def call(self, name: str, args: dict) -> dict:
        try:
            return self._tools[name].handler(args or {})
        except KeyError:
            return err(f"Unknown tool {name!r}. Available tools: {', '.join(self.names()) or '(none)'}")
        except Exception as exc:  # a tool must never crash the agent loop
            logger.exception("extension tool %s failed", name)
            return err(f"{type(exc).__name__}: {exc}")

    def filter(self, patterns: list[str] | None) -> ToolRegistry:
        """Return a registry containing tools matching any fnmatch pattern.

        ``["*"]`` keeps everything; ``None`` also keeps everything; ``[]`` keeps nothing.
        """
        if patterns is None or "*" in patterns:
            selected = list(self._tools.values())
        else:
            selected = [
                tool
                for tool in self._tools.values()
                if any(fnmatch(tool.name, pattern) for pattern in patterns)
            ]
        filtered = ToolRegistry()
        for tool in selected:
            filtered._tools[tool.name] = tool
        return filtered


def ok(output: str = "", **extra) -> dict:
    return {"output": output, "returncode": 0, "exception_info": "", **extra}


def err(message: str, **extra) -> dict:
    return {"output": "", "returncode": -1, "exception_info": message, **extra}
