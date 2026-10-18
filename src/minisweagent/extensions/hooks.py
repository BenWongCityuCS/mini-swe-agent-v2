"""Event hooks.

Hooks are shell commands that receive a JSON payload on stdin. The payload has
``event``, ``session_id``, ``cwd`` and, for tool events, ``tool_name``,
``tool_input`` and ``tool_result``.

Exit code 2 blocks the action and returns stderr to the model. Any other
non-zero exit is logged and, unless ``fail_open`` is false for a timeout,
does not block. Stdout is appended to the tool result (or shown to the model
for session/prompt events).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from minisweagent.extensions.config import HookCommand, HooksConfig

logger = logging.getLogger("extensions")

EVENT_NAMES = (
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "SessionStart",
    "SessionEnd",
    "Stop",
    "PreCompact",
    "PostCompact",
)

_TOOL_EVENTS = {"PreToolUse", "PostToolUse"}
_MAX_TOOL_RESULT_CHARS = 20_000


@dataclass
class HookResult:
    blocked: bool = False
    message: str = ""
    output: str = ""

    def merged_output(self) -> str:
        return self.output.strip()


def _matches(matcher: str, tool_name: str | None) -> bool:
    if not tool_name:
        return False
    patterns = [part.strip() for part in str(matcher or "*").split(",") if part.strip()]
    if not patterns or "*" in patterns:
        return True
    return any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in patterns)


def _coerce_command(entry: Any) -> HookCommand | None:
    if isinstance(entry, str):
        return HookCommand(command=entry)
    if isinstance(entry, dict) and entry.get("command"):
        return HookCommand(**entry)
    logger.warning("ignoring invalid hook entry: %r", entry)
    return None


class HookRunner:
    def __init__(self, config: HooksConfig, workspace: str | Path, *, session_id: str = "") -> None:
        self.config = config
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.session_id = session_id
        self.commands = self._load()

    def _load(self) -> dict[str, list[HookCommand]]:
        commands: dict[str, list[HookCommand]] = {
            event: list(getattr(self.config, event) or []) for event in EVENT_NAMES
        }
        for path in self._config_paths():
            data = self._read_file(path)
            if data is None:
                continue
            if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
                data = data["hooks"]
            if not isinstance(data, dict):
                logger.warning("hook file %s must contain a mapping", path)
                continue
            for event, entries in data.items():
                if event not in commands:
                    logger.warning("hook file %s: unknown event %r", path, event)
                    continue
                if isinstance(entries, (str, dict)):
                    entries = [entries]
                for entry in entries or []:
                    command = _coerce_command(entry)
                    if command is not None:
                        commands[event].append(command)
        return commands

    def _config_paths(self) -> list[Path]:
        paths: list[Path] = []
        if self.config.config_file:
            candidate = Path(self.config.config_file).expanduser()
            if not candidate.is_absolute():
                candidate = Path(self.workspace) / candidate
            paths.append(candidate)
        if self.config.auto_discover:
            paths.append(Path(self.workspace) / ".mini-swe-agent" / "hooks.yaml")
            paths.append(Path("~/.mini-swe-agent/hooks.yaml").expanduser())
        unique: list[Path] = []
        for path in paths:
            resolved = path.resolve()
            if resolved not in unique:
                unique.append(resolved)
        return unique

    @staticmethod
    def _read_file(path: Path) -> dict | None:
        if not path.is_file():
            return None
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("could not read hook file %s: %s", path, exc)
            return None

    def has(self, event: str) -> bool:
        return bool(self.commands.get(event))

    def _expand(self, text: str, hook: HookCommand, tool_name: str | None) -> str:
        values = {
            "WORKSPACE": self.workspace,
            "SESSION_ID": self.session_id,
            "TOOL_NAME": tool_name or "",
        }
        for key, value in values.items():
            text = text.replace(f"${{{key}}}", value)
        return text

    def run(
        self,
        event: str,
        *,
        tool_name: str | None = None,
        tool_input: Any = None,
        tool_result: Any = None,
        extra: dict | None = None,
    ) -> HookResult:
        entries = self.commands.get(event) or []
        result = HookResult()
        if not entries:
            return result
        payload_result = tool_result
        if isinstance(payload_result, dict) and isinstance(payload_result.get("output"), str):
            output = payload_result["output"]
            if len(output) > _MAX_TOOL_RESULT_CHARS:
                payload_result = {
                    **payload_result,
                    "output": output[:_MAX_TOOL_RESULT_CHARS] + "\n...[truncated for hook]",
                }
        payload = {
            "event": event,
            "session_id": self.session_id,
            "cwd": self.workspace,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_result": payload_result,
            **(extra or {}),
        }
        serialized = json.dumps(payload, ensure_ascii=False, default=str)

        for hook in entries:
            if event in _TOOL_EVENTS and not _matches(hook.matcher, tool_name):
                continue
            command = self._expand(hook.command, hook, tool_name)
            env = os.environ | {str(key): str(value) for key, value in (hook.env or {}).items()}
            try:
                completed = subprocess.run(
                    command,
                    shell=True,
                    cwd=self.workspace,
                    env=env,
                    input=serialized,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=hook.timeout,
                )
            except subprocess.TimeoutExpired:
                message = f"{event} hook timed out after {hook.timeout:g}s: {hook.command}"
                logger.warning(message)
                if not hook.fail_open:
                    result.blocked = True
                    result.message = message
                    break
                continue
            except OSError as exc:
                message = f"{event} hook failed to start: {exc}"
                logger.warning(message)
                if not hook.fail_open:
                    result.blocked = True
                    result.message = message
                    break
                continue

            stdout = (completed.stdout or "").strip()
            stderr = (completed.stderr or "").strip()
            if completed.returncode == 2:
                result.blocked = True
                result.message = stderr or stdout or f"Blocked by {event} hook"
                break
            if completed.returncode != 0:
                logger.warning(
                    "%s hook exited with %s: %s", event, completed.returncode, stderr or hook.command
                )
            if stdout:
                result.output = f"{result.output}\n{stdout}".strip() if result.output else stdout
        return result


def build_hook_runner(config: HooksConfig, workspace: str | Path, *, session_id: str = "") -> HookRunner:
    return HookRunner(config, workspace, session_id=session_id)
