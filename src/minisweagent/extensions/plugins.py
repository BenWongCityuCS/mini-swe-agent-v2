"""Plugin loader: merge tools, skills, prompts, subagents, MCP servers, automations.

A plugin is a directory with ``plugin.yaml``/``plugin.json``::

    name: my-plugin
    version: 1.0.0
    enabled: true
    skills: [skills]
    prompts: [prompts]
    agents: [agents]
    mcp: mcp.json
    tools: [tools/my_tools.py]
    automations: [automations]

Tool modules are plain Python files with::

    def register(registry, context):
        registry.add(Tool(name="hello", ...))

The registry passed to plugins is a registrar that namespaces a tool as
``<plugin>__<name>`` on a collision, so plugins cannot silently replace core or
each other's tools.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from minisweagent.extensions.config import PluginsConfig
from minisweagent.extensions.registry import Tool, ToolRegistry

logger = logging.getLogger("extensions.plugins")

MANIFEST_NAMES = ("plugin.yaml", "plugin.yml", "plugin.json", ".mini-plugin.yaml")


@dataclass
class Plugin:
    name: str
    path: Path
    manifest: dict = field(default_factory=dict)
    skill_dirs: list[Path] = field(default_factory=list)
    prompt_dirs: list[Path] = field(default_factory=list)
    prompt_files: list[Path] = field(default_factory=list)
    agent_dirs: list[Path] = field(default_factory=list)
    automation_dirs: list[Path] = field(default_factory=list)
    mcp_files: list[Path] = field(default_factory=list)
    mcp_servers: dict = field(default_factory=dict)
    tool_files: list[Path] = field(default_factory=list)


class PluginToolRegistrar:
    """Registry proxy that namespaces plugin tools on collision."""

    def __init__(self, registry: ToolRegistry, plugin_name: str) -> None:
        self.registry = registry
        self.plugin_name = plugin_name
        self.registered: list[str] = []

    def _namespace(self, name: str) -> str:
        existing = set(self.registry.names())
        if name not in existing:
            return name
        prefix = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in self.plugin_name)
        candidate = f"{prefix}__{name}"
        counter = 2
        while candidate in existing:
            candidate = f"{prefix}__{name}_{counter}"
            counter += 1
        return candidate

    def add(self, tool: Tool) -> None:
        name = self._namespace(tool.name)
        if name != tool.name:
            tool = Tool(
                name=name,
                description=tool.description,
                parameters=tool.parameters,
                handler=tool.handler,
                read_only=tool.read_only,
                metadata={**tool.metadata, "original_name": tool.name, "plugin": self.plugin_name},
            )
        self.registry.add(tool)
        self.registered.append(name)

    def extend(self, tools) -> None:
        for tool in tools:
            self.add(tool)

    def names(self) -> list[str]:
        return self.registry.names()

    def specs(self) -> list[dict]:
        return self.registry.specs()


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _resolve(base: Path, value: str) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _read_manifest(directory: Path) -> tuple[Path, dict] | None:
    for name in MANIFEST_NAMES:
        candidate = directory / name
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
            data = json.loads(text) if candidate.suffix == ".json" else (yaml.safe_load(text) or {})
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.warning("could not read plugin manifest %s: %s", candidate, exc)
            return None
        if isinstance(data, dict):
            return candidate, data
    return None


def _load_plugin(directory: Path) -> Plugin | None:
    found = _read_manifest(directory)
    if found is None:
        return None
    manifest_path, manifest = found
    if manifest.get("enabled", True) is False:
        return None
    name = str(manifest.get("name") or directory.name).strip() or directory.name
    plugin = Plugin(name=name, path=directory.resolve(), manifest=manifest)

    for value in _as_list(manifest.get("skills")):
        path = _resolve(directory, value)
        if path.is_dir():
            plugin.skill_dirs.append(path)
        else:
            logger.warning("plugin %s: skill dir not found: %s", name, path)
    for value in _as_list(manifest.get("prompts")):
        path = _resolve(directory, value)
        if path.is_dir():
            plugin.prompt_dirs.append(path)
        elif path.is_file():
            plugin.prompt_files.append(path)
        else:
            logger.warning("plugin %s: prompt path not found: %s", name, path)
    for value in _as_list(manifest.get("agents")):
        path = _resolve(directory, value)
        if path.is_dir():
            plugin.agent_dirs.append(path)
        else:
            logger.warning("plugin %s: agent dir not found: %s", name, path)
    for value in _as_list(manifest.get("automations")):
        path = _resolve(directory, value)
        if path.is_dir():
            plugin.automation_dirs.append(path)
        else:
            logger.warning("plugin %s: automation dir not found: %s", name, path)

    mcp = manifest.get("mcp")
    if isinstance(mcp, dict):
        plugin.mcp_servers.update(mcp)
    else:
        for value in _as_list(mcp):
            path = _resolve(directory, value)
            if path.is_file():
                plugin.mcp_files.append(path)
            else:
                logger.warning("plugin %s: MCP file not found: %s", name, path)

    for value in _as_list(manifest.get("tools")):
        path = _resolve(directory, value)
        if path.is_file():
            plugin.tool_files.append(path)
        else:
            logger.warning("plugin %s: tool module not found: %s", name, path)

    return plugin


def discover_plugins(config: PluginsConfig, workspace: Path) -> list[Plugin]:
    directories: list[Path] = []
    for value in _as_list(config.paths):
        directories.append(_resolve(workspace, value))
    for value in _as_list(config.dirs):
        base = _resolve(workspace, value)
        if not base.is_dir():
            continue
        if _read_manifest(base) is not None:
            directories.append(base)
        else:
            directories.extend(sorted(path for path in base.iterdir() if path.is_dir()))

    plugins: list[Plugin] = []
    seen: set[Path] = set()
    for directory in directories:
        resolved = directory.resolve()
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        plugin = _load_plugin(resolved)
        if plugin is not None:
            plugins.append(plugin)
    return plugins


def plugin_mcp_servers(plugins: list[Plugin]) -> dict:
    servers: dict = {}
    for plugin in plugins:
        for path in plugin.mcp_files:
            try:
                text = path.read_text(encoding="utf-8")
                data = json.loads(text) if path.suffix.lower() == ".json" else (yaml.safe_load(text) or {})
            except (OSError, ValueError, yaml.YAMLError) as exc:
                logger.warning("plugin %s: could not read MCP file %s: %s", plugin.name, path, exc)
                continue
            raw = data.get("mcpServers") or data.get("servers") or {}
            if isinstance(raw, dict):
                servers.update(raw)
        servers.update(plugin.mcp_servers)
    return servers


def load_plugin_tools(plugins: list[Plugin], registry: ToolRegistry, workspace: Path) -> list[str]:
    registered: list[str] = []
    for plugin in plugins:
        for path in plugin.tool_files:
            module_name = f"mini_plugin_{plugin.name}_{path.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError(f"cannot load {path}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                register = getattr(module, "register", None)
                if not callable(register):
                    logger.warning("plugin %s: %s has no register(registry, context)", plugin.name, path)
                    continue
                registrar = PluginToolRegistrar(registry, plugin.name)
                register(
                    registrar,
                    {
                        "workspace": workspace,
                        "plugin": plugin,
                        "plugin_name": plugin.name,
                        "manifest": plugin.manifest,
                    },
                )
                registered.extend(registrar.registered)
            except Exception as exc:  # noqa: BLE001 - a bad plugin must not kill the agent
                logger.exception("plugin %s: failed to load %s", plugin.name, path)
                logger.warning("plugin %s: %s", plugin.name, exc)
    return registered
