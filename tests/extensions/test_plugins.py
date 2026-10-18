import json
from pathlib import Path

from minisweagent.extensions.config import PluginsConfig
from minisweagent.extensions.plugins import (
    discover_plugins,
    load_plugin_tools,
    plugin_mcp_servers,
)
from minisweagent.extensions.registry import Tool, ToolRegistry, ok


def write_plugin(root: Path, name: str = "demo", enabled: bool = True) -> Path:
    plugin = root / "plugins" / name
    (plugin / "skills" / "hello").mkdir(parents=True)
    (plugin / "skills" / "hello" / "SKILL.md").write_text(
        "---\nname: hello\ndescription: Hello skill\n---\nSay hello.\n", encoding="utf-8"
    )
    (plugin / "prompts").mkdir()
    (plugin / "prompts" / "rules.md").write_text("Plugin rules", encoding="utf-8")
    (plugin / "agents").mkdir()
    (plugin / "agents" / "worker.md").write_text(
        "---\nname: worker\ndescription: work\ntools: []\n---\nWork.\n", encoding="utf-8"
    )
    (plugin / "tools").mkdir()
    (plugin / "tools" / "hello.py").write_text(
        "from minisweagent.extensions.registry import Tool, ok\n\n"
        "def register(registry, context):\n"
        "    def handler(args):\n"
        "        return ok(context['plugin_name'] + ':' + str(args.get('x')))\n"
        "    registry.add(Tool(name='hello', description='hi', "
        "parameters={'type': 'object', 'properties': {}}, handler=handler))\n",
        encoding="utf-8",
    )
    (plugin / "mcp.json").write_text(
        json.dumps({"mcpServers": {"fs": {"command": "npx", "args": ["server"]}}}),
        encoding="utf-8",
    )
    (plugin / "automations").mkdir()
    (plugin / "automations" / "nightly.yaml").write_text(
        "name: nightly\ntask: run tests\nschedule: '0 3 * * *'\n", encoding="utf-8"
    )
    (plugin / "plugin.yaml").write_text(
        f"name: {name}\nversion: 1.0.0\nenabled: {str(enabled).lower()}\n"
        "skills: [skills]\nprompts: [prompts]\nagents: [agents]\n"
        "mcp: mcp.json\ntools: [tools/hello.py]\nautomations: [automations]\n",
        encoding="utf-8",
    )
    return plugin


def test_discover_plugin_contributions(tmp_path):
    write_plugin(tmp_path)
    plugins = discover_plugins(PluginsConfig(enabled=True, dirs=["plugins"]), tmp_path)
    assert [plugin.name for plugin in plugins] == ["demo"]
    plugin = plugins[0]
    assert any(path.name == "skills" for path in plugin.skill_dirs)
    assert plugin.prompt_dirs and plugin.prompt_files == []
    assert plugin.agent_dirs
    assert plugin.tool_files
    assert plugin.automation_dirs

    servers = plugin_mcp_servers(plugins)
    assert servers["fs"]["command"] == "npx"


def test_plugin_tools_register_and_namespace_on_collision(tmp_path):
    write_plugin(tmp_path)
    plugins = discover_plugins(PluginsConfig(enabled=True, dirs=["plugins"]), tmp_path)

    registry = ToolRegistry()
    registered = load_plugin_tools(plugins, registry, tmp_path)
    assert registered == ["hello"]
    assert registry.call("hello", {"x": 1})["output"] == "demo:1"

    colliding = ToolRegistry()
    colliding.add(
        Tool(
            name="hello",
            description="core",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: ok("core"),
        )
    )
    registered = load_plugin_tools(plugins, colliding, tmp_path)
    assert registered == ["demo__hello"]
    assert colliding.call("demo__hello", {"x": 2})["output"] == "demo:2"
    assert colliding.call("hello", {})["output"] == "core"


def test_disabled_plugin_is_skipped(tmp_path):
    write_plugin(tmp_path, enabled=False)
    plugins = discover_plugins(PluginsConfig(enabled=True, dirs=["plugins"]), tmp_path)
    assert plugins == []


def test_explicit_plugin_path(tmp_path):
    plugin = write_plugin(tmp_path)
    plugins = discover_plugins(
        PluginsConfig(enabled=True, dirs=[], paths=[str(plugin)]), tmp_path
    )
    assert [item.name for item in plugins] == ["demo"]
