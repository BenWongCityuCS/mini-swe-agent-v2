import json
from types import SimpleNamespace

from minisweagent.extensions import browser as browser_module
from minisweagent.extensions.browser import (
    BrowserRouter,
    WebBridgeClient,
    build_browser_tool,
    check_network_policy,
)
from minisweagent.extensions.config import BrowserConfig
from minisweagent.extensions.registry import ok


class FakeVision:
    def __init__(self):
        self.config = SimpleNamespace(auto_describe_screenshots=True)

    def describe_file(self, path, question):
        return ok(f"vision saw {path}")


def make_client(tmp_path, **overrides):
    config = BrowserConfig(
        enabled=True,
        daemon_url="http://daemon/command",
        status_url="http://daemon/status",
        proxy_url=None,
        prefer_proxy=False,
        **overrides,
    )
    return WebBridgeClient(config, tmp_path)


def fake_request_factory(calls, tab_count=0, screenshot=False):
    def fake(url, payload=None, *, method="POST", timeout=120):
        calls.append((url, payload, method))
        if method == "GET":
            return 200, json.dumps({"running": True, "extension_connected": True})
        action = payload["action"]
        if action == "list_tabs":
            return 200, json.dumps({"ok": True, "data": {"tabs": [{"tabId": i} for i in range(tab_count)]}})
        if action == "navigate":
            return 200, json.dumps(
                {"ok": True, "data": {"url": payload["args"].get("url"), "tabId": 1}}
            )
        if action == "screenshot" and screenshot:
            return 200, json.dumps({"ok": True, "data": {"path": "/tmp/shot.png", "format": "png"}})
        if action == "close_session":
            return 200, json.dumps({"ok": True, "data": {"closed": 1}})
        return 200, json.dumps({"ok": True, "data": {"action": action}})

    return fake


def test_navigate_maps_arguments(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(browser_module, "_request", fake_request_factory(calls))
    client = make_client(tmp_path, group_title="Research")
    tool = build_browser_tool(client)

    result = tool.handler(
        {"action": "navigate", "url": "https://example.com", "new_tab": True}
    )
    assert result["returncode"] == 0
    navigate = next(call for call in calls if call[1] and call[1]["action"] == "navigate")
    assert navigate[1]["args"] == {
        "url": "https://example.com",
        "newTab": True,
        "group_title": "Research",
    }
    assert client.current_session().startswith("mini-")


def test_tab_guard_refuses_fourth_tab(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(browser_module, "_request", fake_request_factory(calls, tab_count=3))
    client = make_client(tmp_path, max_tabs_per_session=3)
    tool = build_browser_tool(client)

    result = tool.handler({"action": "navigate", "url": "https://example.com", "new_tab": True})
    assert result["returncode"] == -1
    assert "already has 3 tabs" in result["exception_info"]


def test_error_envelope_is_reported(monkeypatch, tmp_path):
    calls = []

    def fake(url, payload=None, *, method="POST", timeout=120):
        calls.append((url, payload, method))
        if method == "GET":
            return 200, json.dumps({"extension_connected": True})
        return 200, json.dumps(
            {"ok": False, "error": {"code": "tab_limit_exceeded", "message": "close a tab"}}
        )

    monkeypatch.setattr(browser_module, "_request", fake)
    client = make_client(tmp_path)
    tool = build_browser_tool(client)
    result = tool.handler({"action": "snapshot"})
    assert result["returncode"] == -1
    assert "tab_limit_exceeded" in result["exception_info"]


def test_screenshot_is_described_by_vision(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(browser_module, "_request", fake_request_factory(calls, screenshot=True))
    client = make_client(tmp_path)
    client.vision = FakeVision()
    tool = build_browser_tool(client)

    result = tool.handler({"action": "screenshot", "path": "/tmp/shot.png"})
    assert result["returncode"] == 0
    assert "[vision description]" in result["output"]
    assert "vision saw /tmp/shot.png" in result["output"]


def test_close_session_on_finish(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(browser_module, "_request", fake_request_factory(calls))
    client = make_client(tmp_path, auto_close_on_finish=True)
    tool = build_browser_tool(client)
    tool.handler({"action": "navigate", "url": "https://example.com", "new_tab": True})

    client.close()
    closed = [call for call in calls if call[1] and call[1]["action"] == "close_session"]
    assert closed, calls


def test_browser_use_backend_is_explicit(tmp_path):
    client = WebBridgeClient(
        BrowserConfig(enabled=True, backend="browser_use"), tmp_path
    )
    tool = build_browser_tool(client)
    result = tool.handler({"action": "snapshot"})
    assert result["returncode"] == -1
    assert "browser_use backend" in result["exception_info"]


class FakeBackend:
    def __init__(self, name, result=None, connected=True):
        self.name = name
        self.result = result or ok("ok")
        self.connected = connected
        self.calls = []

    def command(self, action, args, session=None):
        self.calls.append((action, args, session))
        return dict(self.result)

    def current_session(self):
        return self.name

    def status(self):
        return {"extension_connected": self.connected}

    def close(self):
        self.calls.append(("close", {}, None))


def test_router_falls_back_from_browser_use_to_webbridge(tmp_path, monkeypatch):
    router = BrowserRouter(
        BrowserConfig(
            enabled=True,
            backend="browser_use",
            fallback=True,
            proxy_url=None,
            prefer_proxy=False,
        ),
        tmp_path,
    )
    web = FakeBackend("web")
    browser_use = FakeBackend("bu", result={"returncode": -1, "output": "", "exception_info": "browser-use is not installed"})
    monkeypatch.setattr(router, "_webbridge_client", lambda: web)
    monkeypatch.setattr(router, "_browser_use_client", lambda: browser_use)

    result = router.command("snapshot", {})
    assert result["returncode"] == 0
    assert result["extra"]["backend"] == "webbridge"
    assert "[fallback]" in result["output"]
    assert browser_use.calls and web.calls
    # A missing package disables that backend for the rest of the run.
    assert "browser_use" in router._disabled
    router.command("snapshot", {})
    assert len(browser_use.calls) == 1


def test_router_no_fallback_reports_failure(tmp_path, monkeypatch):
    router = BrowserRouter(
        BrowserConfig(enabled=True, backend="webbridge", fallback=False), tmp_path
    )
    web = FakeBackend("web", result={"returncode": -1, "output": "", "exception_info": "daemon down"})
    browser_use = FakeBackend("bu")
    monkeypatch.setattr(router, "_webbridge_client", lambda: web)
    monkeypatch.setattr(router, "_browser_use_client", lambda: browser_use)

    result = router.command("snapshot", {})
    assert result["returncode"] == -1
    assert "daemon down" in result["exception_info"]
    assert browser_use.calls == []


def test_router_auto_prefers_ready_webbridge(tmp_path, monkeypatch):
    router = BrowserRouter(BrowserConfig(enabled=True, backend="auto"), tmp_path)
    web = FakeBackend("web", connected=True)
    browser_use = FakeBackend("bu")
    monkeypatch.setattr(router, "_webbridge_client", lambda: web)
    monkeypatch.setattr(router, "_browser_use_client", lambda: browser_use)

    result = router.command("snapshot", {})
    assert result["returncode"] == 0
    assert result["extra"]["backend"] == "webbridge"
    assert browser_use.calls == []


def test_router_auto_uses_browser_use_when_webbridge_not_ready(tmp_path, monkeypatch):
    router = BrowserRouter(BrowserConfig(enabled=True, backend="auto"), tmp_path)
    web = FakeBackend("web", connected=False)
    browser_use = FakeBackend("bu")
    monkeypatch.setattr(router, "_webbridge_client", lambda: web)
    monkeypatch.setattr(router, "_browser_use_client", lambda: browser_use)

    result = router.command("snapshot", {})
    assert result["returncode"] == 0
    assert result["extra"]["backend"] == "browser_use"
    assert web.calls == []


def test_network_policy_blocks_external_when_disabled():
    config = BrowserConfig(enabled=True, network_enabled=False)
    assert check_network_policy(config, "https://example.com") is not None
    assert check_network_policy(config, "http://localhost:3000") is None
    assert check_network_policy(config, "http://127.0.0.1:8000") is None
    assert check_network_policy(config, "http://192.168.1.5") is None
    assert check_network_policy(config, "file:///tmp/x.html") is None
    assert check_network_policy(config, "data:text/html,hi") is None


def test_network_policy_allowlist_and_denylist():
    allow = BrowserConfig(enabled=True, allowed_domains=["example.com"])
    assert check_network_policy(allow, "https://api.example.com") is None
    assert check_network_policy(allow, "https://example.com/path") is None
    assert check_network_policy(allow, "https://other.com") is not None
    assert check_network_policy(allow, "http://localhost:3000") is None

    deny = BrowserConfig(enabled=True, blocked_domains=["bad.com"])
    assert check_network_policy(deny, "https://sub.bad.com") is not None
    assert check_network_policy(deny, "https://good.com") is None


def test_webbridge_blocks_navigate_before_http(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(browser_module, "_request", fake_request_factory(calls))
    client = make_client(tmp_path, network_enabled=False)
    tool = build_browser_tool(client)

    result = tool.handler({"action": "navigate", "url": "https://example.com"})
    assert result["returncode"] == -1
    assert "network access is disabled" in result["exception_info"]
    assert calls == []


def test_webbridge_allows_localhost_when_network_disabled(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(browser_module, "_request", fake_request_factory(calls))
    client = make_client(tmp_path, network_enabled=False)
    tool = build_browser_tool(client)

    result = tool.handler({"action": "navigate", "url": "http://localhost:3000"})
    assert result["returncode"] == 0
