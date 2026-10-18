"""Background tasks: run shell commands or callables while the agent continues.

Commands started from the built-in ``bash`` tool with ``run_in_background`` are
executed by :class:`BackgroundTaskManager`. ``TaskOutput`` polls a task (with an
optional blocking wait) and ``TaskStop`` kills it. The same manager also runs
background subagents, so ``TaskOutput`` returns their final report.

Only :class:`~minisweagent.environments.local.LocalEnvironment` can stream
partial output and kill a running command. Other environments run the blocking
``env.execute`` call in a worker thread; their output appears when the command
finishes and ``TaskStop`` is cooperative.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from minisweagent.exceptions import InterruptAgentFlow
from minisweagent.extensions.config import BackgroundConfig
from minisweagent.extensions.registry import Tool, err, ok

logger = logging.getLogger("extensions")


class _OutputBuffer:
    """Thread-safe output accumulator with a head/tail cap."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1_000, limit)
        self._parts: list[str] = []
        self._chars = 0
        self.truncated = False
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._parts.append(text)
            self._chars += len(text)
            if self._chars > self.limit * 2:
                joined = "".join(self._parts)
                marker = "\n...[background output truncated]...\n"
                head_len = self.limit // 4
                tail_len = max(0, self.limit - head_len - len(marker))
                self._parts = [joined[:head_len], marker, joined[-tail_len:]]
                self._chars = sum(len(part) for part in self._parts)
                self.truncated = True

    def text(self) -> str:
        with self._lock:
            return "".join(self._parts)

    def __len__(self) -> int:
        with self._lock:
            return self._chars


@dataclass
class BackgroundTask:
    id: str
    kind: str
    label: str
    owner: str = ""
    started_at: float = field(default_factory=time.time)
    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    output: _OutputBuffer | None = None
    returncode: int | None = None
    exception: str = ""
    result: dict | None = None
    cancelled: bool = False

    @property
    def status(self) -> str:
        if self.done.is_set():
            return "cancelled" if self.cancelled else "completed"
        return "running"

    @property
    def elapsed(self) -> float:
        return max(0.0, time.time() - self.started_at)


class BackgroundTaskManager:
    def __init__(self, config: BackgroundConfig, workspace: str | None = None) -> None:
        self.config = config
        self.workspace = workspace
        self._tasks: dict[str, BackgroundTask] = {}
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def _new_task(self, kind: str, label: str, owner: str) -> BackgroundTask:
        task_id = f"bg-{uuid.uuid4().hex[:8]}"
        return BackgroundTask(
            id=task_id,
            kind=kind,
            label=label,
            owner=owner,
            output=_OutputBuffer(self.config.max_output_chars),
        )

    def _live_count(self) -> int:
        return sum(1 for task in self._tasks.values() if not task.done.is_set())

    def _register(self, task: BackgroundTask) -> dict:
        with self._lock:
            if self._live_count() >= self.config.max_tasks:
                return err(
                    f"Too many background tasks ({self.config.max_tasks}). "
                    "Wait for one with TaskOutput or stop one with TaskStop."
                )
            self._tasks[task.id] = task
        return ok(
            f'Started background task {task.id} ({task.kind}).\n'
            f"Use TaskOutput(task_id={task.id!r}) to read it and TaskStop(task_id={task.id!r}) to stop it.",
            task_id=task.id,
        )

    def start_command(
        self,
        command: str,
        env: Any,
        *,
        cwd: str = "",
        timeout: float | None = None,
        owner: str = "",
    ) -> dict:
        task = self._new_task("command", command, owner)
        registered = self._register(task)
        if registered["returncode"] != 0:
            return registered
        timeout = self.config.default_timeout if timeout is None else timeout

        from minisweagent.environments.local import LocalEnvironment

        if isinstance(env, LocalEnvironment):
            self._start_local(task, command, env, cwd, timeout)
        else:
            self._start_threaded_env(task, command, env, timeout)
        return registered

    def start_callable(
        self,
        fn: Callable[[threading.Event], Any],
        *,
        label: str,
        owner: str = "",
        kind: str = "agent",
    ) -> dict:
        task = self._new_task(kind, label, owner)
        registered = self._register(task)
        if registered["returncode"] != 0:
            return registered

        def runner() -> None:
            try:
                result = fn(task.cancel_event)
                task.result = result if isinstance(result, dict) else {"result": result}
                task.returncode = 0
                task.output.append(_format_result(task.result))
            except InterruptAgentFlow as exc:
                message = "\n".join(str(msg.get("content", "")) for msg in exc.messages)
                task.output.append(message)
                task.returncode = 0
                task.result = {"exit_status": type(exc).__name__, "submission": message}
            except Exception as exc:  # a background task must never crash the process
                logger.exception("background task %s failed", task.id)
                task.exception = f"{type(exc).__name__}: {exc}"
                task.returncode = -1
                task.output.append(task.exception)
            finally:
                task.done.set()

        task.thread = threading.Thread(target=runner, name=f"{task.id}-runner", daemon=True)
        task.thread.start()
        return registered

    # -- command backends --------------------------------------------------

    def _start_local(
        self,
        task: BackgroundTask,
        command: str,
        env: Any,
        cwd: str,
        timeout: float,
    ) -> None:
        config = env.config
        workdir = cwd or getattr(config, "cwd", "") or os.getcwd()
        process_env = os.environ | dict(getattr(config, "env", {}) or {})
        try:
            task.process = subprocess.Popen(
                command,
                shell=True,
                text=True,
                cwd=workdir,
                env=process_env,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=os.name == "posix",
            )
        except Exception as exc:
            task.exception = f"{type(exc).__name__}: {exc}"
            task.returncode = -1
            task.output.append(task.exception)
            task.done.set()
            return

        def reader() -> None:
            try:
                assert task.process is not None
                if task.process.stdout is not None:
                    for line in task.process.stdout:
                        task.output.append(line)
                task.returncode = task.process.wait()
            except Exception as exc:
                logger.debug("background reader for %s stopped: %s", task.id, exc)
            finally:
                task.done.set()

        def watchdog() -> None:
            if timeout <= 0:
                return
            if task.done.wait(timeout):
                return
            task.output.append(f"\n[background task timed out after {timeout:g}s]\n")
            self._kill_process(task)
            task.cancelled = True

        task.thread = threading.Thread(target=reader, name=f"{task.id}-reader", daemon=True)
        task.thread.start()
        if timeout > 0:
            threading.Thread(target=watchdog, name=f"{task.id}-watchdog", daemon=True).start()

    def _start_threaded_env(
        self,
        task: BackgroundTask,
        command: str,
        env: Any,
        timeout: float,
    ) -> None:
        def runner() -> None:
            try:
                output = env.execute({"command": command})
                task.output.append(str(output.get("output", "")))
                task.returncode = output.get("returncode")
                task.result = output
            except InterruptAgentFlow as exc:
                # A background command must never submit the parent agent.
                message = "\n".join(str(msg.get("content", "")) for msg in exc.messages)
                task.output.append(message)
                task.returncode = 0
            except Exception as exc:
                task.exception = f"{type(exc).__name__}: {exc}"
                task.returncode = -1
                task.output.append(task.exception)
            finally:
                task.done.set()

        def watchdog() -> None:
            if timeout <= 0:
                return
            if task.done.wait(timeout):
                return
            task.cancel_event.set()
            task.output.append(f"\n[background task timed out after {timeout:g}s]\n")
            task.cancelled = True

        task.thread = threading.Thread(target=runner, name=f"{task.id}-runner", daemon=True)
        task.thread.start()
        if timeout > 0:
            threading.Thread(target=watchdog, name=f"{task.id}-watchdog", daemon=True).start()

    @staticmethod
    def _kill_process(task: BackgroundTask) -> None:
        process = task.process
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError):
            pass

    # -- inspection --------------------------------------------------------

    def get(self, task_id: str) -> BackgroundTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def poll(
        self,
        task_id: str,
        *,
        wait: bool = False,
        timeout: float = 30.0,
        offset: int = 0,
    ) -> dict:
        task = self.get(task_id)
        if task is None:
            return err(f"Unknown background task {task_id!r}. Use TaskOutput without an id to list tasks.")
        if wait and not task.done.is_set():
            task.done.wait(max(0.0, timeout))
        text = task.output.text() if task.output is not None else ""
        offset = max(0, int(offset))
        chunk = text[offset:]
        return {
            "id": task.id,
            "kind": task.kind,
            "label": task.label,
            "status": task.status,
            "returncode": task.returncode,
            "elapsed": task.elapsed,
            "output": chunk,
            "next_offset": offset + len(chunk),
            "truncated": bool(task.output and task.output.truncated),
            "result": task.result,
            "exception": task.exception,
            "cancelled": task.cancelled,
        }

    def stop(self, task_id: str, *, force: bool = True) -> dict:
        task = self.get(task_id)
        if task is None:
            return err(f"Unknown background task {task_id!r}")
        if task.done.is_set():
            return ok(f"Task {task_id} is already {task.status}.")
        task.cancelled = True
        task.cancel_event.set()
        if task.process is not None:
            if force:
                self._kill_process(task)
            else:
                try:
                    task.process.terminate()
                except ProcessLookupError:
                    pass
        task.done.wait(2.0)
        if task.kind == "command" and not task.done.is_set():
            return err(f"Could not stop {task_id} yet; it is still running.")
        return ok(f"Stopped {task_id}.")

    def list(self) -> list[dict]:
        with self._lock:
            tasks = list(self._tasks.values())
        return [
            {
                "id": task.id,
                "kind": task.kind,
                "label": task.label,
                "status": task.status,
                "elapsed": task.elapsed,
                "returncode": task.returncode,
            }
            for task in tasks
        ]

    def close(self) -> None:
        if not self.config.kill_on_close:
            return
        for entry in self.list():
            if entry["status"] == "running":
                self.stop(entry["id"], force=True)

    def serialize(self) -> list[dict]:
        return self.list()


def _format_result(result: dict | None) -> str:
    if not result:
        return ""
    status = result.get("exit_status", "")
    submission = result.get("submission") or result.get("content") or result.get("result") or ""
    if status or submission:
        return f'<result status="{status}">\n{submission}\n</result>'
    return ""


def _status_line(info: dict) -> str:
    pieces = [
        f'id="{info["id"]}"',
        f'kind="{info["kind"]}"',
        f'status="{info["status"]}"',
        f'elapsed="{info["elapsed"]:.1f}s"',
    ]
    if info["returncode"] is not None:
        pieces.append(f'returncode="{info["returncode"]}"')
    if info.get("truncated"):
        pieces.append('truncated="true"')
    return " ".join(pieces)


def build_background_tools(manager: BackgroundTaskManager) -> list[Tool]:
    def output_handler(args: dict) -> dict:
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            tasks = manager.list()
            if not tasks:
                return ok("No background tasks.")
            lines = ["Background tasks:"]
            lines.extend(
                f'- {task["id"]}: {task["kind"]} {task["status"]} ({task["elapsed"]:.1f}s) {task["label"][:120]}'
                for task in tasks
            )
            return ok("\n".join(lines))
        try:
            wait = bool(args.get("wait") or False)
            timeout = float(args.get("timeout") or 30.0)
            offset = int(args.get("offset") or 0)
        except (TypeError, ValueError):
            return err("wait/timeout/offset must be numeric")
        info = manager.poll(task_id, wait=wait, timeout=timeout, offset=offset)
        if info.get("returncode") == -1 and info.get("status") != "completed":
            return err(str(info.get("exception") or info.get("output") or "task failed"))
        body = info.pop("output")
        lines = [f"<background_task {_status_line(info)}>"]
        if info.get("exception"):
            lines.append(f"<exception>{info['exception']}</exception>")
        if body:
            lines.append(f"<output>\n{body}\n</output>")
        if info.get("status") == "running" and not body:
            lines.append("(still running; call TaskOutput again with wait=true to block)")
        lines.append(f'<next_offset>{info["next_offset"]}</next_offset>')
        lines.append("</background_task>")
        return ok("\n".join(lines))

    def stop_handler(args: dict) -> dict:
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return err("task_id is required")
        return manager.stop(task_id, force=bool(args.get("force", True)))

    return [
        Tool(
            name="TaskOutput",
            description=(
                "Read the output of a background bash command or subagent started with run_in_background. "
                "Call without task_id to list tasks. Set wait=true to block until the task finishes or the "
                "timeout expires. Pass the returned next_offset to read only new output."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Background task id (omit to list)"},
                    "wait": {"type": "boolean", "default": False},
                    "timeout": {"type": "number", "default": 30, "description": "Seconds to wait when wait=true"},
                    "offset": {"type": "integer", "default": 0, "description": "Character offset for incremental reads"},
                },
                "required": [],
            },
            handler=output_handler,
            read_only=True,
        ),
        Tool(
            name="TaskStop",
            description="Stop a running background command or subagent.",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "force": {"type": "boolean", "default": True},
                },
                "required": ["task_id"],
            },
            handler=stop_handler,
            read_only=False,
        ),
    ]
