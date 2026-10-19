"""Basic agent class. See https://mini-swe-agent.com/latest/advanced/control_flow/ for visual explanation
or https://minimal-agent.com for a tutorial on the basic building principles.
"""

import json
import logging
import os
import threading
import time
import traceback
from pathlib import Path

from jinja2 import StrictUndefined, Template
from pydantic import BaseModel

from minisweagent import Environment, Model, __version__
from minisweagent.agents.compaction import (
    CompactionConfig,
    CompactionRecord,
    CompactionState,
    compactable_boundary,
    compaction_threshold,
    estimate_tokens,
    fitting_summary_boundary,
    render_summary_prompt,
    requested_output_tokens,
    resolve_context_limit,
    summary_message,
)
from minisweagent.agents.session import sanitize_messages
from minisweagent.exceptions import (
    Cancelled,
    ContextWindowExceeded,
    FormatError,
    InterruptAgentFlow,
    LimitsExceeded,
    TimeExceeded,
)
from minisweagent.extensions.config import (
    AgentBusConfig,
    AutomationsConfig,
    BackgroundConfig,
    BrowserConfig,
    ContextConfig,
    HooksConfig,
    McpConfig,
    PluginsConfig,
    PromptsConfig,
    SessionConfig,
    SkillsConfig,
    SubagentsConfig,
    ToolsConfig,
    VisionConfig,
    WorkflowConfig,
)
from minisweagent.extensions.registry import ToolRegistry
from minisweagent.utils.serialize import recursive_merge


class AgentConfig(BaseModel):
    """Check the config files in minisweagent/config for example settings."""

    system_template: str
    """Template for the system message (the first message)."""
    instance_template: str
    """Template for the first user message specifying the task (the second message overall)."""
    step_limit: int = 0
    """Maximum number of steps the agent can take."""
    cost_limit: float = 3.0
    """Stop agent after exceeding (!) this cost."""
    wall_time_limit_seconds: int = 0
    """Stop agent after this many seconds of wall-clock time. 0 means no limit."""
    max_consecutive_format_errors: int = 3
    """Exit after this many format errors in a row (0 = no limit)."""
    output_path: Path | None = None
    """Save the trajectory to this path."""
    compaction: CompactionConfig = CompactionConfig()
    """Conversation compaction settings."""
    context: ContextConfig = ContextConfig()
    """ZCode-style section budgets, meta-user injection, and system reminders."""
    workspace: str | None = None
    """Root used to resolve extension paths. Defaults to the environment cwd."""
    skills: SkillsConfig = SkillsConfig()
    """SKILL.md discovery and the Skill tool."""
    subagents: SubagentsConfig = SubagentsConfig()
    """Profile-based subagents exposed through the Task tool."""
    mcp: McpConfig = McpConfig()
    """Model Context Protocol servers."""
    prompts: PromptsConfig = PromptsConfig()
    """External prompt files mounted into the system message."""
    tools: ToolsConfig = ToolsConfig()
    """Built-in tools (Read/Grep/Glob, TodoWrite, Edit/Write)."""
    plugins: PluginsConfig = PluginsConfig()
    """Plugin directories contributing tools, skills, prompts, agents, MCP, automations."""
    automations: AutomationsConfig = AutomationsConfig()
    """Scheduled task directories (used by the mini-automation CLI)."""
    browser: BrowserConfig = BrowserConfig()
    """Kimi WebBridge browser control."""
    vision: VisionConfig = VisionConfig()
    """Separate vision model for images and screenshots."""
    background: BackgroundConfig = BackgroundConfig()
    """Run bash commands and subagents in the background."""
    hooks: HooksConfig = HooksConfig()
    """Shell hooks around tool calls, prompts, sessions, and compaction."""
    session: SessionConfig = SessionConfig()
    """Conversation persistence and resume."""
    bus: AgentBusConfig = AgentBusConfig()
    """Agent-to-agent messaging (SendMessage)."""
    workflow: WorkflowConfig = WorkflowConfig()
    """Durable multi-phase workflows with a scheduled DAG and typed subagent results."""


class DefaultAgent:
    def __init__(
        self,
        model: Model,
        env: Environment,
        *,
        config_class: type = AgentConfig,
        tool_registry: ToolRegistry | None = None,
        prompt_sections: list[str] | None = None,
        background_manager=None,
        hook_runner=None,
        agent_bus=None,
        session_store=None,
        agent_id: str | None = None,
        cancel_event: threading.Event | None = None,
        **kwargs,
    ):
        """See the `AgentConfig` class for permitted keyword arguments.

        ``tool_registry``/``prompt_sections`` are used internally to build subagents
        with a pre-created extension set instead of starting a second MCP client.
        The other keyword arguments let subagents share their parent's background
        manager, hooks, message bus, and cancellation event.
        """
        self.config = config_class(**kwargs)
        self.messages: list[dict] = []
        self.model = model
        self.env = env
        self.extra_template_vars = {}
        self.logger = logging.getLogger("agent")
        self.cost = 0.0
        self.n_calls = 0
        self.n_compaction_calls = 0
        self.n_consecutive_format_errors = 0
        self.compaction_state = CompactionState()
        env_cwd = getattr(getattr(env, "config", None), "cwd", None)
        if not isinstance(env_cwd, (str, os.PathLike)):
            env_cwd = None
        self._workspace = Path(self.config.workspace or env_cwd or os.getcwd()).expanduser().resolve()
        self.agent_id = agent_id or os.getenv("MSWEA_AGENT_ID") or "main"
        self._cancel_event = cancel_event or threading.Event()
        self._background = background_manager
        self._owns_background = background_manager is None
        self._hooks = hook_runner
        self._bus = agent_bus
        self._session_store = session_store
        self._workflow_store = None
        self._session_id = self.config.session.id or ""
        self._session_created_at = time.time()
        self._resumed = False
        self._task = ""
        self._tools = tool_registry if tool_registry is not None else ToolRegistry()
        self._prompt_sections = list(prompt_sections or [])
        self._skills: dict = {}
        self._skills_section = ""
        self._profiles: dict = {}
        self._mounted_prompt_files: list[Path] = []
        self._mcp = None
        self._owns_mcp = False
        self._subagent_depth = 0
        self._plugins: list = []
        self._plugin_tool_names: list[str] = []
        self._automation_dirs: list[str] = []
        self._browser = None
        self._vision = None
        self._todo = None
        self._file_tracker = None
        self._context_builder = None
        self._context_result = None
        self._mounted_prompts = []
        self._microcompacted = False
        self._compaction_failures = 0
        self.context_limit = (
            resolve_context_limit(self.model.config.model_name, self.config.compaction.context_limit)
            if self.config.compaction.enabled
            else 0
        )
        if self.config.compaction.enabled and self.config.compaction.summary_max_tokens >= self.context_limit:
            raise ValueError("agent.compaction.summary_max_tokens must be smaller than the model context limit.")
        if self.config.compaction.enabled and not compaction_threshold(
            self.context_limit, self.config.compaction.buffer, requested_output_tokens(self.model)
        ):
            raise ValueError("agent.compaction.buffer and the requested output must be smaller than the context limit.")
        if tool_registry is None:
            self._setup_extensions()
        if self._bus is not None:
            self._bus.register(self.agent_id, kind="agent")
        self._start_time = time.time()

    def _setup_extensions(self) -> None:
        """Build the opt-in extension layer. Disabled features are no-ops."""
        from minisweagent.extensions.concurrency import configure_subagent_concurrency
        from minisweagent.extensions.prompts import load_mounted_prompts

        plugin_skill_dirs: list[str] = []
        plugin_prompt_dirs: list[str] = []
        plugin_prompt_files: list[str] = []
        plugin_agent_dirs: list[str] = []
        plugin_automation_dirs: list[str] = []

        if self.config.plugins.enabled:
            from minisweagent.extensions.plugins import (
                discover_plugins,
                load_plugin_tools,
                plugin_mcp_servers,
            )

            self._plugins = discover_plugins(self.config.plugins, self._workspace)
            for plugin in self._plugins:
                plugin_skill_dirs.extend(str(path) for path in plugin.skill_dirs)
                plugin_prompt_dirs.extend(str(path) for path in plugin.prompt_dirs)
                plugin_prompt_files.extend(str(path) for path in plugin.prompt_files)
                plugin_agent_dirs.extend(str(path) for path in plugin.agent_dirs)
                plugin_automation_dirs.extend(str(path) for path in plugin.automation_dirs)
            self._plugin_tool_names = load_plugin_tools(self._plugins, self._tools, self._workspace)

        self._automation_dirs = [*self.config.automations.dirs, *plugin_automation_dirs]

        prompt_config = self.config.prompts.model_copy(deep=True)
        prompt_config.files = [*prompt_config.files, *plugin_prompt_files]
        prompt_config.dirs = [*prompt_config.dirs, *plugin_prompt_dirs]
        if prompt_config.files or prompt_config.dirs:
            mounted, section = load_mounted_prompts(prompt_config, self._workspace)
            self._mounted_prompts = mounted
            self._mounted_prompt_files = [prompt.path for prompt in mounted]
            if section:
                self._prompt_sections.append(section)

        skills_config = self.config.skills.model_copy(deep=True)
        skills_config.dirs = [*skills_config.dirs, *plugin_skill_dirs]
        if skills_config.enabled:
            from minisweagent.extensions.skills import build_skill_tool, discover_skills, format_skills_index

            self._skills = discover_skills(skills_config, self._workspace)
            if self._skills:
                self._skills_section = format_skills_index(self._skills, skills_config)
                if self._skills_section:
                    self._prompt_sections.append(self._skills_section)
                self._tools.add(build_skill_tool(self._skills, skills_config, self._workspace))

        sub_config = self.config.subagents.model_copy(deep=True)
        sub_config.profile_dirs = [*sub_config.profile_dirs, *plugin_agent_dirs]
        if sub_config.enabled:
            from minisweagent.extensions.subagents import (
                build_task_tool,
                format_profiles_for_prompt,
                load_agent_profiles,
            )

            configure_subagent_concurrency(sub_config.max_concurrent)
            self._profiles = load_agent_profiles(sub_config, self._workspace)
            profiles_section = format_profiles_for_prompt(self._profiles)
            if profiles_section:
                self._prompt_sections.append(profiles_section)
            self._tools.add(build_task_tool(self, self._profiles, sub_config))

        if self.config.tools.read_only or self.config.tools.edit:
            from minisweagent.extensions.edit_tools import FileTracker
            from minisweagent.extensions.tools import build_read_only_tools

            self._file_tracker = FileTracker()
            self._tools.extend(
                build_read_only_tools(
                    self._workspace,
                    self._file_tracker,
                    read_media=self.config.tools.read_media,
                    max_image_bytes=self.config.tools.max_image_bytes,
                    max_video_bytes=self.config.tools.max_video_bytes,
                    media_detail=self.config.tools.media_detail,
                )
            )
        if self.config.tools.edit:
            from minisweagent.extensions.edit_tools import build_edit_tools

            self._tools.extend(build_edit_tools(self._workspace, self._file_tracker))
        if self.config.tools.todo:
            from minisweagent.extensions.todo import TodoStore, build_todo_read_tool, build_todo_tool

            self._todo = TodoStore(self._workspace, persist=self.config.tools.todo_persist)
            self._tools.add(build_todo_tool(self._todo))
            self._tools.add(build_todo_read_tool(self._todo))
            self._prompt_sections.append(
                "Track multi-step work with TodoWrite: pass the complete list, keep exactly one item "
                "in_progress, and mark items completed as soon as they are done. Use TodoRead to check it."
            )

        if self.config.session.enabled and self._session_store is None:
            from minisweagent.agents.session import SessionStore, build_session_read_tool

            self._session_store = SessionStore(self.config.session, self._workspace)
            if not self._session_id:
                self._session_id = self._session_store.new_id()
            self._tools.add(build_session_read_tool(self._session_store))

        if self.config.background.enabled and self._background is None:
            from minisweagent.extensions.background import BackgroundTaskManager, build_background_tools

            self._background = BackgroundTaskManager(self.config.background, str(self._workspace))
            self._tools.extend(build_background_tools(self._background))

        if self.config.bus.enabled and self._bus is None:
            from minisweagent.extensions.agent_bus import AgentBus, build_send_message_tool

            self._bus = AgentBus(self.config.bus)
            self._tools.add(build_send_message_tool(self._bus, self.agent_id))

        if self.config.hooks.enabled and self._hooks is None:
            from minisweagent.extensions.hooks import build_hook_runner

            self._hooks = build_hook_runner(
                self.config.hooks,
                self._workspace,
                session_id=self._session_id or self.agent_id,
            )

        if self.config.workflow.enabled:
            from minisweagent.workflow import (
                WORKFLOW_GUIDE,
                WorkflowStore,
                build_workflow_runner,
                build_workflow_tools,
            )

            self._workflow_store = WorkflowStore(self.config.workflow, self._workspace)
            workflow_runner = build_workflow_runner(self)
            self._tools.extend(
                build_workflow_tools(
                    self._workflow_store,
                    workflow_runner,
                    self.config.workflow,
                    background_manager=self._background,
                    owner=self.agent_id,
                )
            )
            self._prompt_sections.append(WORKFLOW_GUIDE)

        if self.config.vision.enabled:
            from minisweagent.extensions.vision import VisionClient, build_vision_tool

            self._vision = VisionClient(self.config.vision, self._workspace)
            self._tools.add(build_vision_tool(self._vision))

        if self.config.browser.enabled:
            from minisweagent.extensions.browser import BrowserRouter, build_browser_tool

            self._browser = BrowserRouter(self.config.browser, self._workspace, vision=self._vision)
            self._tools.add(build_browser_tool(self._browser))

        mcp_config = self.config.mcp.model_copy(deep=True)
        if self.config.plugins.enabled and self._plugins:
            from minisweagent.extensions.plugins import plugin_mcp_servers

            merged_servers = plugin_mcp_servers(self._plugins)
            merged_servers.update(mcp_config.servers)
            mcp_config.servers = merged_servers
        if mcp_config.enabled:
            from minisweagent.extensions.mcp import McpManager

            self._mcp = McpManager(mcp_config, self._workspace).start()
            self._owns_mcp = True
            self._tools.extend(self._mcp.tools)

        if self._tools.names():
            setter = getattr(self.model, "set_extra_tools", None)
            if setter is None:
                raise ValueError(
                    "Agent extensions require a tool-call model. Use model_class=litellm "
                    "(or another model that implements set_extra_tools)."
                )
            setter(self._tools.specs())
            if self._mcp is not None and self._mcp.diagnostics:
                self.logger.warning("MCP diagnostics: %s", "; ".join(self._mcp.diagnostics))

        if self.config.context.enabled:
            from minisweagent.context import ContextBuilder

            self._context_builder = ContextBuilder(
                self.config.context,
                base_prompt="",
                workspace=self._workspace,
                model_name=getattr(getattr(self.model, "config", None), "model_name", "unknown"),
                mounted_prompts=self._mounted_prompts,
                skills=self._skills,
                skills_max_description_chars=self.config.skills.max_description_chars,
                profiles=self._profiles,
            )

    def _system_content(self) -> str:
        if self._context_result is not None:
            return self._context_result.system_content
        parts = [self._render_template(self.config.system_template)]
        parts.extend(section for section in self._prompt_sections if section)
        return "\n\n".join(part for part in parts if part)

    def close(self) -> None:
        """Stop extension resources owned by this agent (browser, MCP, background tasks)."""
        if self._browser is not None:
            self._browser.close()
        if self._mcp is not None and self._owns_mcp:
            self._mcp.close()
            self._owns_mcp = False
        if self._background is not None and self._owns_background:
            self._background.close()
        if self._bus is not None:
            self._bus.unregister(self.agent_id)

    def get_template_vars(self, **kwargs) -> dict:
        return recursive_merge(
            self.config.model_dump(),
            self.env.get_template_vars(),
            self.model.get_template_vars(),
            {
                "n_model_calls": self.n_calls,
                "n_compaction_calls": self.n_compaction_calls,
                "model_cost": self.cost,
                "elapsed_seconds": int(time.time() - self._start_time),
            },
            self.extra_template_vars,
            kwargs,
        )

    def _render_template(self, template: str) -> str:
        return Template(template, undefined=StrictUndefined).render(**self.get_template_vars())

    def add_messages(self, *messages: dict) -> list[dict]:
        self.logger.debug(messages)  # set log level to debug to see
        self.messages.extend(messages)
        return list(messages)

    def handle_uncaught_exception(self, e: Exception) -> list[dict]:
        return self.add_messages(
            self.model.format_message(
                role="exit",
                content=str(e),
                extra={
                    "exit_status": type(e).__name__,
                    "submission": "",
                    "exception_str": str(e),
                    "traceback": traceback.format_exc(),
                },
            )
        )

    def run(self, task: str = "", *, resume: str | None = None, **kwargs) -> dict:
        """Run step() until agent is finished. Returns dictionary with exit_status, submission keys.

        Pass ``resume`` (a session id, a path, or ``"latest"``) to continue a saved
        conversation instead of starting from the task template.
        """
        self.extra_template_vars |= {"task": task, **kwargs}
        self._task = task
        if resume:
            self._load_session(resume, task=task)
        else:
            self.messages = []
            self.compaction_state = CompactionState()
            self._microcompacted = False
            self._compaction_failures = 0
            self._resumed = False
            if self.config.session.enabled and not self._session_id and self._session_store is not None:
                self._session_id = self._session_store.new_id()
            task_content = self._initial_task_content()
            submit_hook = self._run_hook("UserPromptSubmit", extra={"prompt": task_content})
            if submit_hook is not None:
                if submit_hook.output:
                    task_content += f"\n\n<hook_context>\n{submit_hook.output}\n</hook_context>"
                if submit_hook.blocked:
                    task_content += f"\n\n<hook_blocked>\n{submit_hook.message}\n</hook_blocked>"
            self.add_messages(
                self.model.format_message(role="system", content=self._system_content()),
                self.model.format_message(role="user", content=task_content),
            )
        start_hook = self._run_hook("SessionStart", extra={"task": self._task, "resumed": self._resumed})
        if start_hook is not None and start_hook.output:
            self.add_messages(
                self.model.format_message(
                    role="user",
                    content=f'<hook_context event="SessionStart">\n{start_hook.output}\n</hook_context>',
                )
            )
        try:
            while True:
                try:
                    self.step()
                    self.n_consecutive_format_errors = 0  # reset on any clean step
                except FormatError as e:
                    # The call was billed before parsing failed, so query() never got to charge it.
                    self.cost += e.messages[0].get("extra", {}).get("cost", 0.0)
                    self.n_consecutive_format_errors += 1
                    if 0 < self.config.max_consecutive_format_errors <= self.n_consecutive_format_errors:
                        self.add_messages(
                            *e.messages,
                            {
                                "role": "exit",
                                "content": "RepeatedFormatError",
                                "extra": {"exit_status": "RepeatedFormatError", "submission": ""},
                            },
                        )
                    else:
                        self.add_messages(*e.messages)
                except InterruptAgentFlow as e:
                    self.add_messages(*e.messages)
                except Exception as e:
                    self.handle_uncaught_exception(e)
                    raise
                finally:
                    self.save(self.config.output_path)
                    self._save_session()
                if self.messages[-1].get("role") == "exit":
                    break
        finally:
            stop_hook = self._run_hook("Stop", extra={"status": self._status()})
            if stop_hook is not None and stop_hook.output:
                self.logger.info("Stop hook: %s", stop_hook.output)
            self._save_session(force=True)
            self._run_hook("SessionEnd", extra={"status": self._status()})
            self.close()
        return self.messages[-1].get("extra", {})

    def _initial_task_content(self) -> str:
        if self._context_builder is not None:
            self._context_builder.base_prompt = self._render_template(self.config.system_template)
            self._context_result = self._context_builder.build()
        task_content = self._render_template(self.config.instance_template)
        if self._context_result is not None and self._context_result.meta_user_content:
            task_content = (
                f"<meta_user>\n{self._context_result.meta_user_content}\n</meta_user>\n\n{task_content}"
            )
        return task_content

    def _status(self) -> str:
        if not self.messages:
            return "running"
        last = self.messages[-1]
        if last.get("role") == "exit":
            return str(last.get("extra", {}).get("exit_status") or "exit")
        return "running"

    def _load_session(self, spec: str, *, task: str = "") -> None:
        from minisweagent.agents.session import SessionStore, compaction_state_from_dict

        store = self._session_store
        if store is None:
            store = SessionStore(self.config.session, self._workspace)
            self._session_store = store
        data = store.load(spec)
        messages = data.get("messages") or []
        if not messages:
            raise ValueError(f"Session {data.get('_path')} contains no messages")
        self.messages = list(messages)
        self.cost = float(data.get("cost") or 0.0)
        self.n_calls = int(data.get("n_calls") or 0)
        self.n_compaction_calls = int(data.get("compaction_calls") or 0)
        self.compaction_state = compaction_state_from_dict(data.get("compaction"))
        self._microcompacted = bool(data.get("microcompacted") or False)
        self._compaction_failures = int(data.get("compaction_failures") or 0)
        self._session_id = str(data.get("id") or Path(data["_path"]).stem)
        self._session_created_at = float(data.get("created_at") or time.time())
        self._task = str(data.get("task") or "")
        self._resumed = True
        if self._todo is not None and data.get("todos"):
            self._todo.update(list(data["todos"]))
        continuation = task or "[Session resumed. Continue from the last state; do not repeat completed work.]"
        self._task = task or self._task
        self.add_messages(self.model.format_message(role="user", content=continuation))

    def _save_session(self, *, force: bool = False) -> None:
        if self._session_store is None:
            return
        if not self.config.session.autosave and not force:
            return
        if not self._session_id:
            self._session_id = self._session_store.new_id()
        payload = {
            "id": self._session_id,
            "created_at": self._session_created_at,
            "workspace": str(self._workspace),
            "agent_id": self.agent_id,
            "model": getattr(getattr(self.model, "config", None), "model_name", ""),
            "task": self._task,
            "status": self._status(),
            "n_calls": self.n_calls,
            "cost": self.cost,
            "compaction_calls": self.n_compaction_calls,
            "messages": sanitize_messages(
                self.messages, keep_media=self.config.session.keep_media
            ),
            "compaction": self.compaction_state.serialize(),
            "microcompacted": self._microcompacted,
            "compaction_failures": self._compaction_failures,
            "todos": (
                [
                    {"content": item.content, "status": item.status, "priority": item.priority}
                    for item in self._todo.items
                ]
                if self._todo is not None
                else []
            ),
        }
        try:
            self._session_store.save(payload)
        except OSError:
            self.logger.exception("could not save session %s", self._session_id)

    def step(self) -> list[dict]:
        """Query the LM, execute actions."""
        self._check_cancelled()
        return self.execute_actions(self.query())

    def query(self) -> dict:
        """Query the model and return model messages. Override to add hooks."""
        self._check_cancelled()
        self._drain_inbox()
        self._check_limits()
        if self.config.compaction.enabled and self.config.compaction.auto:
            self._compact_until_fit("auto")
            self._check_limits()
        self.n_calls += 1
        try:
            message = self.model.query(self._messages_for_model())
        except ContextWindowExceeded:
            if not self.config.compaction.enabled or not self._compact_until_fit("overflow"):
                raise
            self._check_limits()
            message = self.model.query(self._messages_for_model())
        self.cost += message.get("extra", {}).get("cost", 0.0)
        self.add_messages(message)
        return message

    def _check_limits(self) -> None:
        if 0 < self.config.step_limit <= self.n_calls or 0 < self.config.cost_limit <= self.cost:
            raise LimitsExceeded(
                {
                    "role": "exit",
                    "content": "LimitsExceeded",
                    "extra": {"exit_status": "LimitsExceeded", "submission": ""},
                }
            )
        if 0 < self.config.wall_time_limit_seconds <= int(time.time() - self._start_time):
            raise TimeExceeded(
                {
                    "role": "exit",
                    "content": "TimeExceeded",
                    "extra": {"exit_status": "TimeExceeded", "submission": ""},
                }
            )

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise Cancelled(
                {
                    "role": "exit",
                    "content": "Cancelled",
                    "extra": {"exit_status": "Cancelled", "submission": ""},
                }
            )

    def _drain_inbox(self) -> None:
        if self._bus is None:
            return
        for entry in self._bus.drain(self.agent_id):
            self.add_messages(self.model.format_message(role="user", content=entry.render()))

    def _run_hook(self, event: str, **kwargs):
        if self._hooks is None:
            return None
        return self._hooks.run(event, **kwargs)

    def _attachment_messages(self, outputs: list[dict]) -> list[dict]:
        messages: list[dict] = []
        for output in outputs:
            for attachment in (output.get("extra") or {}).get("attachments") or []:
                kind = str(attachment.get("kind") or "")
                data_url = str(attachment.get("data_url") or "")
                if kind not in {"image", "video"} or not data_url:
                    continue
                if kind == "image":
                    part = {"type": "image_url", "image_url": {"url": data_url}}
                    if attachment.get("detail"):
                        part["image_url"]["detail"] = attachment["detail"]
                else:
                    content_type = self.config.tools.video_content_type
                    part = {"type": content_type, content_type: {"url": data_url}}
                text = (
                    f"Attached {kind}: {attachment.get('path', '')} "
                    f"({attachment.get('mime', '')}, {int(attachment.get('bytes') or 0):,} bytes)"
                )
                messages.append(
                    self.model.format_message(role="user", content=[{"type": "text", "text": text}, part])
                )
        return messages

    def _messages_for_model(self) -> list[dict]:
        if not self.compaction_state.summary:
            messages = list(self.messages)
        else:
            messages = [
                self.messages[0],
                summary_message(self.model, self.compaction_state.summary),
                *self.messages[self.compaction_state.compacted_until :],
            ]
        if self._context_builder is not None and self.config.context.system_reminders:
            reminder_sections = self._context_builder.build_reminders(
                self._todo,
                self.compaction_state,
                microcompacted=self._microcompacted,
            )
            reminder = self._context_builder.reminder_content(reminder_sections)
            if reminder:
                messages.append(self.model.format_message(role="user", content=reminder))
        return messages

    def _compact_once(self, reason: str) -> bool:
        compact_boundary = compactable_boundary(
            self.messages, self.compaction_state.compacted_until, self.config.compaction.keep_tokens
        )
        if compact_boundary is None:
            return False
        from minisweagent.context.compact.prompt import MINIMAL_SUMMARY_TEMPLATE

        template = self.config.compaction.summary_template
        input_limit = max(0, self.context_limit - self.config.compaction.summary_max_tokens)
        boundary = fitting_summary_boundary(
            self.messages,
            self.compaction_state.compacted_until,
            compact_boundary,
            previous_summary=self.compaction_state.summary,
            template=template,
            input_limit=input_limit,
        )
        if boundary is None and template != MINIMAL_SUMMARY_TEMPLATE:
            # A very long summary prompt can exceed a small model's input budget.
            # Fall back to the compact template before giving up.
            template = MINIMAL_SUMMARY_TEMPLATE
            boundary = fitting_summary_boundary(
                self.messages,
                self.compaction_state.compacted_until,
                compact_boundary,
                previous_summary=self.compaction_state.summary,
                template=template,
                input_limit=input_limit,
            )
        if boundary is None:
            return False
        before = estimate_tokens(self._messages_for_model())
        pre = self._run_hook(
            "PreCompact", extra={"reason": reason, "boundary": boundary, "tokens_before": before}
        )
        if pre is not None and pre.blocked:
            self.logger.warning("PreCompact hook blocked compaction: %s", pre.message)
            return False
        prompt = render_summary_prompt(
            template,
            self.compaction_state.summary,
            self.messages[self.compaction_state.compacted_until : boundary],
        )
        if self.config.compaction.summary_instructions:
            prompt += "\n\nAdditional Instructions:\n" + self.config.compaction.summary_instructions
        result = self.model.generate_text(
            [
                self.model.format_message(
                    role="system", content="Return only the requested conversation checkpoint, without commentary."
                ),
                self.model.format_message(role="user", content=prompt),
            ],
            max_tokens=self.config.compaction.summary_max_tokens,
        )
        self.n_compaction_calls += 1
        cost = result["cost"]
        self.cost += cost
        if not (summary := result["text"].strip()):
            # A reasoning-only or truncated response can leave content empty.
            # Do not kill a long run for a summarizer hiccup: keep a bounded,
            # deterministic checkpoint and continue.
            summary = self._fallback_summary(boundary)
            self.logger.warning("compaction model returned an empty checkpoint; using a local fallback summary")
        self.compaction_state.summary = summary
        self.compaction_state.compacted_until = boundary
        after = estimate_tokens(self._messages_for_model())
        self.compaction_state.records.append(
            CompactionRecord(
                summary=self.compaction_state.summary,
                compacted_until=boundary,
                reason=reason,
                estimated_tokens_before=before,
                estimated_tokens_after=after,
                cost=cost,
            )
        )
        self._run_hook(
            "PostCompact", extra={"reason": reason, "tokens_after": after, "cost": cost}
        )
        return True

    def _fallback_summary(self, boundary: int) -> str:
        """Build a bounded local checkpoint when the summarizer returns no text."""
        from minisweagent.context.utils import truncate_to_tokens
        from minisweagent.models.utils.content_string import get_content_string

        parts: list[str] = []
        for message in self.messages[self.compaction_state.compacted_until : boundary]:
            text = get_content_string(message).strip()
            if text:
                parts.append(f"[{message.get('role', 'unknown')}] {text}")
        joined = "\n\n".join(parts)
        if not joined:
            return "Previous conversation compacted (the summarizer returned no text)."
        budget = max(256, self.config.compaction.summary_max_tokens - 64)
        return "Automatic fallback summary (the summarizer returned no text):\n" + truncate_to_tokens(
            joined, budget
        )

    def _compact_until_fit(self, reason: str) -> bool:
        from minisweagent.context.compact import maybe_microcompact, should_compact

        compacted = False
        output_tokens = requested_output_tokens(self.model)
        threshold = compaction_threshold(self.context_limit, self.config.compaction.buffer, output_tokens)
        token_count = estimate_tokens(self._messages_for_model())

        # Cheap first pass: clear old tool results before paying for a summary.
        if self.config.compaction.microcompact and token_count > int(threshold * 0.9):
            updated, saved, cleared = maybe_microcompact(
                self.messages,
                keep_recent=self.config.compaction.microcompact_keep_recent,
                min_token_savings=self.config.compaction.microcompact_min_savings,
            )
            if saved:
                self.messages = updated
                self._microcompacted = True
                self.logger.info(
                    "microcompact cleared %d old tool result(s), saving ~%d tokens", cleared, saved
                )
                token_count = estimate_tokens(self._messages_for_model())

        if reason == "auto":
            decision = should_compact(
                token_count=token_count,
                context_limit=self.context_limit,
                buffer=self.config.compaction.buffer,
                output_tokens=output_tokens,
                consecutive_failures=self._compaction_failures,
                max_consecutive_failures=self.config.compaction.max_consecutive_failures,
            )
            if not decision.should_compact:
                return compacted

        if reason == "overflow" and token_count <= threshold:
            self._check_limits()
            compacted = self._compact_once(reason)
        while estimate_tokens(self._messages_for_model()) > threshold:
            self._check_limits()
            try:
                did_compact = self._compact_once(reason)
            except Exception:
                self._compaction_failures += 1
                self.logger.exception(
                    "compaction failed (%d/%d)",
                    self._compaction_failures,
                    self.config.compaction.max_consecutive_failures,
                )
                if (
                    self.config.compaction.max_consecutive_failures > 0
                    and self._compaction_failures >= self.config.compaction.max_consecutive_failures
                ):
                    self.logger.error("compaction circuit breaker open; continuing without compaction")
                    return compacted
                raise
            if did_compact:
                self._compaction_failures = 0
            if not did_compact:
                break
            compacted = True
        return compacted

    def execute_actions(self, message: dict) -> list[dict]:
        """Execute actions in message, add observation messages, return them."""
        actions = message.get("extra", {}).get("actions", [])
        if (
            len(actions) > 1
            and self.config.subagents.enabled
            and self.config.subagents.parallel_task_calls
            and all(action.get("tool") == "Task" for action in actions)
        ):
            from concurrent.futures import ThreadPoolExecutor

            workers = min(len(actions), max(1, self.config.subagents.max_concurrent))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="subagent") as pool:
                outputs = list(pool.map(self._execute_action, actions))
        else:
            outputs = [self._execute_action(action) for action in actions]
        attachments = self._attachment_messages(outputs)
        # Attachments are delivered as a user message; keep the base64 payload
        # out of the tool observation (and therefore out of the trajectory).
        clean_outputs = []
        for output in outputs:
            extra = output.get("extra") or {}
            if "attachments" in extra:
                output = {**output, "extra": {key: value for key, value in extra.items() if key != "attachments"}}
            clean_outputs.append(output)
        observations = self.model.format_observation_messages(
            message, clean_outputs, self.get_template_vars()
        )
        self.add_messages(*observations)
        if attachments:
            self.add_messages(*attachments)
        return [*observations, *attachments]

    def _execute_action(self, action: dict) -> dict:
        tool_name = str(action.get("tool") or "bash")
        tool_input = action.get("args") if action.get("tool") else {"command": action.get("command", "")}
        if isinstance(tool_input, dict) and action.get("run_in_background"):
            tool_input = {**tool_input, "run_in_background": True}

        pre = self._run_hook(
            "PreToolUse", tool_name=tool_name, tool_input=tool_input
        )
        if pre is not None and pre.blocked:
            return {
                "output": "",
                "returncode": -1,
                "exception_info": f"Blocked by PreToolUse hook: {pre.message}",
            }

        if tool_name == "bash":
            if action.get("run_in_background"):
                if self._background is None:
                    result = {
                        "output": "",
                        "returncode": -1,
                        "exception_info": "Background tasks are disabled (agent.background.enabled).",
                    }
                else:
                    result = self._background.start_command(
                        str(action.get("command") or ""),
                        self.env,
                        timeout=action.get("timeout"),
                        owner=self.agent_id,
                    )
            else:
                result = self.env.execute(action)
        else:
            result = self._tools.call(tool_name, action.get("args") or {})

        post = self._run_hook(
            "PostToolUse", tool_name=tool_name, tool_input=tool_input, tool_result=result
        )
        if post is not None and post.output:
            result = dict(result)
            result["output"] = f"{result.get('output') or ''}\n[hook] {post.output}".strip()
        return result

    def serialize(self, *extra_dicts) -> dict:
        """Serialize agent state to a json-compatible nested dictionary for saving."""
        last_message = self.messages[-1] if self.messages else {}
        last_extra = last_message.get("extra", {})
        agent_data = {
            "info": {
                "model_stats": {
                    "instance_cost": self.cost,
                    "api_calls": self.n_calls + self.n_compaction_calls,
                    "task_calls": self.n_calls,
                    "compaction_calls": self.n_compaction_calls,
                },
                "config": {
                    "agent": self.config.model_dump(mode="json"),
                    "agent_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                },
                "mini_version": __version__,
                "exit_status": last_extra.get("exit_status", ""),
                "submission": last_extra.get("submission", ""),
            },
            "messages": self.messages,
            "compaction": {
                **self.compaction_state.serialize(),
                "active_estimated_tokens": estimate_tokens(self._messages_for_model()),
                "context_limit": self.context_limit,
            },
            "extensions": {
                "tools": sorted(self._tools.names()),
                "skills": sorted({skill.name for skill in self._skills.values()}),
                "subagent_profiles": sorted(self._profiles),
                "mcp_servers": self._mcp.server_names if self._mcp is not None else [],
                "mcp_diagnostics": list(self._mcp.diagnostics) if self._mcp is not None else [],
                "mounted_prompts": [str(path) for path in self._mounted_prompt_files],
                "plugins": [plugin.name for plugin in self._plugins],
                "plugin_tools": list(self._plugin_tool_names),
                "browser_session": (
                    self._browser.current_session() if self._browser is not None else None
                ),
                "browser_backend": self.config.browser.backend if self.config.browser.enabled else None,
                "vision_model": self.config.vision.model if self.config.vision.enabled else None,
                "todos": (
                    [
                        {"content": item.content, "status": item.status, "priority": item.priority}
                        for item in self._todo.items
                    ]
                    if self._todo is not None
                    else []
                ),
                "automation_dirs": list(self._automation_dirs),
                "background_tasks": self._background.serialize() if self._background is not None else [],
                "agents": self._bus.serialize() if self._bus is not None else {},
            },
            "session": {
                "id": self._session_id,
                "resumed": self._resumed,
                "path": (
                    str(self._session_store.path_for(self._session_id))
                    if self._session_store is not None and self._session_id
                    else None
                ),
            },
            "context": {
                "enabled": self.config.context.enabled,
                "microcompacted": self._microcompacted,
                "compaction_failures": self._compaction_failures,
                **(self._context_result.stats() if self._context_result is not None else {}),
            },
            "trajectory_format": "mini-swe-agent-1.1",
        }
        return recursive_merge(agent_data, self.model.serialize(), self.env.serialize(), *extra_dicts)

    def save(self, path: Path | None, *extra_dicts) -> dict:
        """Save the trajectory of the agent to a file if path is given. Returns full serialized data.
        You can pass additional dictionaries with extra data to be (recursively) merged into the output data.
        """
        data = self.serialize(*extra_dicts)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2))
        return data
