"""Kimi WebBridge browser control.

The tool talks to the local WebBridge daemon (``POST /command``), which drives
the user's real Chrome/Edge with their existing login sessions. It prefers the
optional tab-limit proxy on :11086, enforces a per-session tab budget itself, and
can close the session automatically when the agent finishes.
"""

from __future__ import annotations

import contextvars
import ipaddress
import json
import logging
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from minisweagent.extensions.config import BrowserConfig
from minisweagent.extensions.registry import Tool, err, ok

logger = logging.getLogger("extensions.browser")

CURRENT_SESSION: contextvars.ContextVar[str | None] = contextvars.ContextVar("mini_browser_session", default=None)
CURRENT_GROUP: contextvars.ContextVar[str | None] = contextvars.ContextVar("mini_browser_group", default=None)

ACTIONS = [
    "navigate",
    "find_tab",
    "snapshot",
    "click",
    "fill",
    "evaluate",
    "screenshot",
    "list_tabs",
    "close_tab",
    "close_session",
    "save_as_pdf",
    "network",
    "upload",
    "cdp",
    "close_tab_group",
    "list_tab_groups",
]


def _domain_matches(host: str, domain: str) -> bool:
    domain = domain.strip().lower().lstrip(".")
    if not domain:
        return False
    return host == domain or host.endswith("." + domain)


def _is_local_host(host: str) -> bool:
    if host in {"localhost", "127.0.0.1", "::1", "[::1]"}:
        return True
    if host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def check_network_policy(config: BrowserConfig, url: str) -> str | None:
    """Return an error message when ``url`` is not allowed, otherwise ``None``.

    This is a navigation-level guard. It stops the agent from browsing to the
    open internet when ``network_enabled`` is false (or outside ``allowed_domains``),
    while keeping local dashboards, file:// pages, and data: URLs usable. It is
    not a network sandbox: a page already loaded elsewhere or a shell command can
    still reach the network.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in {"about", "data", "file", "chrome", "view-source", "blob", "devtools"}:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None

    for domain in config.blocked_domains:
        if _domain_matches(host, domain):
            return f"Blocked by agent.browser.blocked_domains: {host}"

    local = _is_local_host(host)
    if config.allowed_domains:
        if not any(_domain_matches(host, domain) for domain in config.allowed_domains):
            if not (config.allow_localhost and local):
                return (
                    f"Domain {host!r} is not in agent.browser.allowed_domains. "
                    "Add it to the allowlist if this navigation is legitimate."
                )

    if not config.network_enabled and not (config.allow_localhost and local):
        return (
            f"External network access is disabled (agent.browser.network_enabled=false): {host}. "
            "Enable it in the config only if this task is allowed to browse the internet."
        )
    return None


def _tcp_reachable(url: str, timeout: float = 0.3) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _heartbeat(session: str) -> None:
    try:
        directory = Path(tempfile.gettempdir()) / "wb-sessions"
        directory.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in session)
        (directory / f"{safe}.stamp").write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _request(
    url: str,
    payload: dict | None = None,
    *,
    method: str = "POST",
    timeout: float = 120,
) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


class WebBridgeClient:
    def __init__(self, config: BrowserConfig, workspace: Path, vision=None) -> None:
        self.config = config
        self.workspace = Path(workspace)
        self.vision = vision
        self._default_session = config.session or f"mini-{uuid.uuid4().hex[:8]}"
        self._url = self._choose_url()
        self._status_checked = False
        self._created_sessions: set[str] = set()

    def _choose_url(self) -> str:
        if self.config.prefer_proxy and self.config.proxy_url and _tcp_reachable(self.config.proxy_url):
            return self.config.proxy_url
        return self.config.daemon_url

    def current_session(self) -> str:
        return CURRENT_SESSION.get() or self._default_session

    def status(self) -> dict:
        try:
            _, body = _request(self.config.status_url, method="GET", timeout=5)
            return json.loads(body)
        except Exception as exc:  # noqa: BLE001 - status is advisory
            return {"error": str(exc)}

    def _post(self, action: str, args: dict, session: str) -> tuple[int, str]:
        return _request(
            self._url,
            {"action": action, "args": args, "session": session},
            timeout=self.config.timeout_seconds,
        )

    def _tab_count(self, session: str) -> int:
        try:
            _, body = self._post("list_tabs", {}, session)
            data = json.loads(body)
            return len((data.get("data") or {}).get("tabs") or [])
        except Exception:
            return 0

    def command(self, action: str, args: dict, session: str | None = None) -> dict:
        if self.config.backend == "browser_use":
            return err(
                "The WebBridge client does not serve the browser_use backend. Use BrowserRouter "
                "or set agent.browser.backend=webbridge."
            )

        session = session or self.current_session()
        if action in {"navigate", "find_tab"} and args.get("url"):
            blocked = check_network_policy(self.config, str(args["url"]))
            if blocked:
                return err(blocked)
        if not self._status_checked:
            status = self.status()
            if status.get("extension_connected") is False:
                return err(
                    "Kimi WebBridge is running but the browser extension is not connected. Open Chrome/Edge "
                    "with the Kimi Browser Extension enabled, wait a few seconds, and retry."
                )
            self._status_checked = True

        if action == "navigate" and args.get("newTab") and self.config.max_tabs_per_session > 0:
            count = self._tab_count(session)
            if count >= self.config.max_tabs_per_session:
                return err(
                    f"Session {session!r} already has {count} tabs (limit "
                    f"{self.config.max_tabs_per_session}). Call close_tab or close_session first, then retry."
                )

        _heartbeat(session)
        if action == "navigate" and args.get("newTab"):
            self._created_sessions.add(session)
        try:
            http_status, body = self._post(action, args, session)
        except Exception as exc:  # noqa: BLE001
            return err(
                f"Could not reach Kimi WebBridge at {self._url}: {type(exc).__name__}: {exc}. "
                "Run `kimi-webbridge status` and `kimi-webbridge start`."
            )

        try:
            envelope = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return ok(body[: self.config.max_output_chars])

        if envelope.get("ok") is False:
            error = envelope.get("error") or {}
            return err(
                f"WebBridge error {error.get('code', http_status)}: {error.get('message', body[:1000])}"
            )

        data = envelope.get("data", envelope)
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
        if len(text) > self.config.max_output_chars:
            text = text[: self.config.max_output_chars] + "\n...[truncated]"

        if (
            action == "screenshot"
            and self.vision is not None
            and self.config.enabled
            and self.vision.config.auto_describe_screenshots
            and isinstance(data, dict)
            and data.get("path")
        ):
            described = self.vision.describe_file(
                str(data["path"]),
                "Describe this browser screenshot. Include the page purpose, visible text, and any UI state.",
            )
            if described.get("returncode") == 0:
                text += "\n\n[vision description]\n" + described.get("output", "")
            else:
                text += "\n\n[vision unavailable: " + described.get("exception_info", "") + "]"

        return ok(text)

    def close(self) -> None:
        if not self.config.auto_close_on_finish:
            return
        sessions = set(self._created_sessions) or {self._default_session}
        for session in sessions:
            try:
                self._post("close_session", {}, session)
            except Exception:
                pass
        group = CURRENT_GROUP.get() or self.config.group_title
        if group:
            try:
                self._post("close_tab_group", {"group_title": group}, self.current_session())
            except Exception:
                pass


class BrowserRouter:
    """Selects between WebBridge and browser-use, with per-call fallback."""

    def __init__(self, config: BrowserConfig, workspace: Path, vision=None) -> None:
        self.config = config
        self.workspace = Path(workspace)
        self.vision = vision
        self._webbridge = None
        self._browser_use = None
        self._disabled: set[str] = set()
        self.last_backend: str | None = None

    def _webbridge_client(self) -> WebBridgeClient:
        if self._webbridge is None:
            self._webbridge = WebBridgeClient(self.config, self.workspace, vision=self.vision)
        return self._webbridge

    def _browser_use_client(self):
        if self._browser_use is None:
            from minisweagent.extensions.browser_use import BrowserUseClient

            self._browser_use = BrowserUseClient(self.config, self.workspace, vision=self.vision)
        return self._browser_use

    def current_session(self) -> str:
        if self._webbridge is not None:
            return self._webbridge.current_session()
        if self._browser_use is not None:
            return self._browser_use.current_session()
        return self.config.session or "mini"

    def status(self) -> dict:
        webbridge = self._webbridge_client().status()
        try:
            browser_use = self._browser_use_client().status()
        except Exception as exc:  # noqa: BLE001
            browser_use = {"backend": "browser_use", "available": False, "reason": str(exc)}
        return {"webbridge": webbridge, "browser_use": browser_use, "disabled": sorted(self._disabled)}

    def _order(self) -> list[str]:
        backend = self.config.backend
        if backend == "webbridge":
            order = ["webbridge"]
        elif backend == "browser_use":
            order = ["browser_use"]
        else:
            ready = bool(self._webbridge_client().status().get("extension_connected"))
            order = ["webbridge", "browser_use"] if ready else ["browser_use", "webbridge"]
        if self.config.fallback:
            order.extend(item for item in ("webbridge", "browser_use") if item not in order)
        return [item for item in order if item not in self._disabled]

    def command(self, action: str, args: dict, session: str | None = None) -> dict:
        attempts: list[tuple[str, str]] = []
        for backend in self._order():
            try:
                client = self._webbridge_client() if backend == "webbridge" else self._browser_use_client()
                result = client.command(action, args, session=session)
            except Exception as exc:  # noqa: BLE001 - try the next backend
                result = err(f"{backend} failed: {type(exc).__name__}: {exc}")

            if result.get("returncode") == 0:
                result.setdefault("extra", {})["backend"] = backend
                result["extra"]["attempts"] = attempts
                self.last_backend = backend
                if attempts:
                    result["output"] = (
                        result.get("output", "")
                        + "\n\n[fallback] "
                        + " | ".join(f"{name}: {message}" for name, message in attempts)
                    )
                return result

            message = result.get("exception_info") or "failed"
            attempts.append((backend, message))
            lowered = message.lower()
            if "not installed" in lowered or "no module named" in lowered:
                self._disabled.add(backend)

        if not attempts:
            return err("No browser backend is available (all disabled).")
        return err(
            "All browser backends failed: "
            + " | ".join(f"{name}: {message}" for name, message in attempts),
            extra={"attempts": attempts},
        )

    def close(self) -> None:
        if self._webbridge is not None:
            self._webbridge.close()
        if self._browser_use is not None:
            self._browser_use.close()


def _map_args(action: str, args: dict) -> tuple[dict | None, str | None]:
    if action == "navigate":
        url = args.get("url")
        if not url:
            return None, "url is required for navigate"
        mapped = {"url": url, "newTab": bool(args.get("new_tab") or args.get("newTab"))}
        if args.get("group_title"):
            mapped["group_title"] = args["group_title"]
        return mapped, None
    if action == "find_tab":
        url = args.get("url")
        if not url:
            return None, "url is required for find_tab"
        return {"url": url, "active": bool(args.get("active"))}, None
    if action in {"click", "fill"}:
        selector = args.get("selector")
        if not selector:
            return None, f"selector is required for {action}"
        mapped = {"selector": selector}
        if action == "fill":
            if "value" not in args:
                return None, "value is required for fill"
            mapped["value"] = args.get("value")
        return mapped, None
    if action == "evaluate":
        if not args.get("code"):
            return None, "code is required for evaluate"
        return {"code": args["code"]}, None
    if action == "screenshot":
        mapped = {}
        for key in ("format", "quality", "selector", "path"):
            if args.get(key) is not None:
                mapped[key] = args[key]
        return mapped, None
    if action == "save_as_pdf":
        mapped = {}
        for key in ("paper_format", "landscape", "scale", "print_background", "path"):
            if args.get(key) is not None:
                mapped[key] = args[key]
        return mapped, None
    if action == "network":
        if not args.get("cmd"):
            return None, "cmd is required for network (start/stop/list/detail)"
        mapped = {"cmd": args["cmd"]}
        for key in ("filter", "requestId"):
            if args.get(key) is not None:
                mapped[key] = args[key]
        return mapped, None
    if action == "upload":
        if not args.get("selector") or not args.get("files"):
            return None, "selector and files are required for upload"
        return {"selector": args["selector"], "files": args["files"]}, None
    if action == "cdp":
        if not args.get("method"):
            return None, "method is required for cdp"
        return {"method": args["method"], "params": args.get("params") or {}}, None
    if action in {"close_tab_group", "list_tab_groups"}:
        return args.get("args") or {}, None
    if action in {"snapshot", "list_tabs", "close_tab", "close_session"}:
        return {}, None
    return None, f"Unknown browser action {action!r}. Available: {', '.join(ACTIONS)}"


def build_browser_tool(client) -> Tool:
    def handler(args: dict) -> dict:
        action = str(args.get("action") or "").strip()
        mapped, problem = _map_args(action, args)
        if problem:
            return err(problem)
        if action == "navigate" and mapped is not None and not mapped.get("group_title"):
            group = CURRENT_GROUP.get() or client.config.group_title
            if group:
                mapped["group_title"] = group
        return client.command(action, mapped or {}, session=args.get("session"))

    return Tool(
        name="Browser",
        description=(
            "Control a browser. Two backends are available and the user chooses one: "
            "Kimi WebBridge drives the user's real Chrome/Edge with their login sessions; "
            "browser-use runs a Playwright browser. Automatic fallback is enabled, so if the "
            "selected backend fails the other one is tried for that call. "
            "Typical flow: navigate -> snapshot -> click/fill -> snapshot. Tabs are capped per "
            "session and agent-created sessions are closed when the run finishes. Use Vision on "
            "screenshot paths when the page is visual."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ACTIONS},
                "url": {"type": "string", "description": "URL for navigate/find_tab"},
                "new_tab": {"type": "boolean", "description": "Open in a new tab (navigate)"},
                "group_title": {"type": "string", "description": "Visible tab-group label (first navigate)"},
                "selector": {"type": "string", "description": "@e ref or CSS selector (click/fill/screenshot/upload)"},
                "value": {"type": "string", "description": "Value to type (fill)"},
                "code": {"type": "string", "description": "JavaScript to run (evaluate); wrap await in an async IIFE"},
                "path": {"type": "string", "description": "Output path (screenshot/save_as_pdf)"},
                "format": {"type": "string", "enum": ["png", "jpeg"]},
                "quality": {"type": "integer", "description": "JPEG quality 0-100"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "Files to upload"},
                "paper_format": {"type": "string"},
                "method": {"type": "string", "description": "CDP method (cdp)"},
                "params": {"type": "object", "description": "CDP params (cdp)"},
                "args": {"type": "object", "description": "Escape hatch for tab-group actions"},
                "session": {"type": "string", "description": "Override the session name (advanced)"},
            },
            "required": ["action"],
        },
        handler=handler,
        read_only=False,
        metadata={"default_session": client.current_session()},
    )
