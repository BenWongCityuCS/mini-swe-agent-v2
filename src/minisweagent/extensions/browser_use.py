"""Optional browser-use backend (Playwright-based).

The adapter exposes the same action surface as the WebBridge client. Actions
that browser-use cannot express cleanly (`find_tab`, `network`, `upload`) return
an error so the router can fall back to WebBridge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from pathlib import Path

from minisweagent.extensions.browser import check_network_policy
from minisweagent.extensions.config import BrowserConfig
from minisweagent.extensions.registry import err, ok

logger = logging.getLogger("extensions.browser_use")


class _AsyncRunner:
    """Dedicated event loop for browser-use, which is async and loop-bound."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="browser-use", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coroutine, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:
            pass
        if self.thread.is_alive():
            self.thread.join(timeout=5)


class BrowserUseClient:
    def __init__(self, config: BrowserConfig, workspace: Path, vision=None) -> None:
        self.config = config
        self.workspace = Path(workspace)
        self.vision = vision
        self._runner = None
        self._session = None
        self._started = False
        self._session_name = config.session or f"mini-bu-{uuid.uuid4().hex[:8]}"

    def current_session(self) -> str:
        return self._session_name

    def status(self) -> dict:
        try:
            import browser_use  # noqa: F401
        except ImportError:
            return {"backend": "browser_use", "available": False, "reason": "browser-use is not installed"}
        return {"backend": "browser_use", "available": True}

    def _ensure_started(self) -> None:
        if self._started:
            return
        try:
            from browser_use import BrowserSession
        except ImportError as exc:
            raise RuntimeError(
                "browser-use is not installed. Install it with "
                "`pip install 'mini-swe-agent[browser-use]'`, or use backend=webbridge."
            ) from exc

        settings = self.config.browser_use
        kwargs = {
            "headless": settings.headless,
            "keep_alive": settings.keep_alive,
        }
        if settings.executable_path:
            kwargs["executable_path"] = settings.executable_path
        if settings.user_data_dir:
            kwargs["user_data_dir"] = str(Path(settings.user_data_dir).expanduser())
        if settings.cdp_url:
            kwargs["cdp_url"] = settings.cdp_url

        allowed = [domain for domain in self.config.allowed_domains if domain]
        if not self.config.network_enabled:
            if self.config.allow_localhost:
                allowed.extend(["localhost", "127.0.0.1"])
            else:
                # browser-use treats a non-empty allowlist as "only these"; a
                # domain that cannot resolve blocks every external request.
                allowed.append("invalid.invalid")
        elif self.config.allow_localhost and allowed:
            allowed.extend(["localhost", "127.0.0.1"])
        if allowed:
            kwargs["allowed_domains"] = sorted(set(allowed))
        if self.config.blocked_domains:
            kwargs["prohibited_domains"] = list(self.config.blocked_domains)

        if self._runner is None:
            self._runner = _AsyncRunner()
        self._session = BrowserSession(**kwargs)
        self._runner.call(self._session.start(), self.config.timeout_seconds)
        self._started = True

    async def _tab_count(self) -> int:
        try:
            return len(await self._session.get_tabs())
        except Exception:
            return 0

    async def _page(self):
        page = await self._session.must_get_current_page()
        if page is None:
            raise RuntimeError("browser-use has no current page")
        return page

    async def _execute(self, action: str, args: dict) -> dict:
        session = self._session
        if action == "navigate":
            await session.navigate_to(args["url"], new_tab=bool(args.get("newTab")))
            return ok(json.dumps({"url": args["url"], "backend": "browser_use"}))

        if action == "snapshot":
            text = await session.get_state_as_text()
            return ok(text)

        if action in {"click", "fill"}:
            page = await self._page()
            if action == "click":
                await page.click(args["selector"])
            else:
                await page.fill(args["selector"], str(args.get("value", "")))
            return ok(f"{action} ok on {args['selector']}")

        if action == "evaluate":
            page = await self._page()
            value = await page.evaluate(args["code"])
            return ok(json.dumps(value, default=str, ensure_ascii=False))

        if action == "screenshot":
            page = await self._page()
            fmt = str(args.get("format") or "png")
            path = args.get("path")
            if not path:
                shots = self.workspace / ".mini-swe-agent" / "shots"
                shots.mkdir(parents=True, exist_ok=True)
                path = str(shots / f"shot-{int(time.time() * 1000)}.{fmt}")
            screenshot_kwargs = {"path": path, "type": fmt}
            if fmt == "jpeg" and args.get("quality") is not None:
                screenshot_kwargs["quality"] = int(args["quality"])
            await page.screenshot(**screenshot_kwargs)
            payload = {"path": path, "format": fmt}
            text = json.dumps(payload)
            if (
                self.vision is not None
                and self.config.enabled
                and self.vision.config.auto_describe_screenshots
            ):
                described = self.vision.describe_file(
                    path,
                    "Describe this browser screenshot. Include the page purpose, visible text, and UI state.",
                )
                if described.get("returncode") == 0:
                    text += "\n\n[vision description]\n" + described.get("output", "")
            return ok(text)

        if action == "list_tabs":
            tabs = await session.get_tabs()
            payload = [
                {
                    "tabId": getattr(tab, "target_id", None),
                    "url": getattr(tab, "url", ""),
                    "title": getattr(tab, "title", ""),
                }
                for tab in tabs
            ]
            return ok(json.dumps(payload, default=str, ensure_ascii=False))

        if action == "close_tab":
            page = await session.get_current_page()
            if page is not None:
                await session.close_page(page)
            return ok("closed current tab")

        if action == "close_session":
            await session.close()
            self._started = False
            self._session = None
            return ok("closed browser-use session")

        if action == "save_as_pdf":
            page = await self._page()
            path = args.get("path") or str(
                self.workspace / ".mini-swe-agent" / f"page-{int(time.time())}.pdf"
            )
            await page.pdf(
                path=path,
                format=args.get("paper_format") or "A4",
                landscape=bool(args.get("landscape") or False),
                scale=float(args.get("scale") or 1.0),
                print_background=bool(args.get("print_background") or False),
            )
            return ok(json.dumps({"path": path}))

        if action == "cdp":
            page = await self._page()
            cdp = await page.context.new_cdp_session(page)
            try:
                response = await cdp.send(args["method"], args.get("params") or {})
            finally:
                await cdp.detach()
            return ok(json.dumps(response, default=str, ensure_ascii=False))

        return err(
            f"browser-use backend does not support {action!r}. "
            "The router can fall back to WebBridge for this call."
        )

    def command(self, action: str, args: dict, session: str | None = None) -> dict:
        if action == "navigate" and args.get("url"):
            blocked = check_network_policy(self.config, str(args["url"]))
            if blocked:
                return err(blocked)
        try:
            self._ensure_started()
            timeout = self.config.timeout_seconds
            if action == "navigate" and args.get("newTab"):
                count = self._runner.call(self._tab_count(), timeout)
                if self.config.max_tabs_per_session > 0 and count >= self.config.max_tabs_per_session:
                    return err(
                        f"browser-use already has {count} tabs (limit "
                        f"{self.config.max_tabs_per_session}). Close a tab first."
                    )
            result = self._runner.call(self._execute(action, args), timeout)
            if isinstance(result, dict):
                result.setdefault("extra", {})["backend"] = "browser_use"
            return result
        except Exception as exc:  # noqa: BLE001 - the router handles fallback
            logger.exception("browser-use action %s failed", action)
            return err(f"browser-use backend failed: {type(exc).__name__}: {exc}")

    def close(self) -> None:
        try:
            if self._started and self._session is not None and self._runner is not None:
                self._runner.call(self._session.close(), self.config.timeout_seconds)
        except Exception:
            logger.debug("browser-use close failed", exc_info=True)
        finally:
            self._started = False
            self._session = None
            if self._runner is not None:
                self._runner.stop()
                self._runner = None
