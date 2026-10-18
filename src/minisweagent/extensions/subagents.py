"""Profile-based subagents exposed through a ``Task`` tool.

Profiles are Markdown files with YAML frontmatter::

    ---
    name: explore
    description: Read-only search agent for broad fan-out searches
    tools: Read, Grep, Glob
    inject_prompts: false
    max_steps: 40
    ---
    You are a read-only exploration agent...

Built-in profiles (``general-purpose`` and ``explore``) can be overridden by a
profile with the same name.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from minisweagent.extensions.concurrency import GLOBAL_SUBAGENT_GATE
from minisweagent.extensions.config import SubagentsConfig
from minisweagent.extensions.edit_tools import FileTracker, build_edit_tools
from minisweagent.extensions.registry import Tool, ToolRegistry, err, ok
from minisweagent.extensions.skills import parse_frontmatter
from minisweagent.extensions.todo import TodoStore, build_todo_tool
from minisweagent.extensions.tools import build_read_only_tools

logger = logging.getLogger("extensions")

DEFAULT_SYSTEM_PROMPT = """You are a focused subagent working on one delegated task.

Work autonomously, using the tools available to you. Do not ask the user questions.
When you are done, submit your final answer with ONE bash command that prints the
marker on the first line and your report after it. Use a heredoc, for example:

cat <<'MSWEA_SUBMIT'
COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
Your concise report goes here. Include file paths and the concrete answer.
MSWEA_SUBMIT

The text after the first line is returned to the coordinator, so it must contain
everything the coordinator needs. Do not submit an empty report.
"""

EXPLORE_SYSTEM_PROMPT = """You are a read-only exploration subagent.

Your job is to locate code and answer the coordinator's question. Do not modify
files, do not run commands that write to disk or the network, and do not attempt
to fix anything. Prefer Read/Grep/Glob over shell commands; use Bash only for
read-only inspection (ls, git log, git show, wc, head, tail).

When you are done, submit your report with ONE bash command that prints the
marker on the first line and the report after it. Use a heredoc, for example:

cat <<'MSWEA_SUBMIT'
COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
Answer: ...
Files: /path/to/file.py:123
Uncertainty: ...
MSWEA_SUBMIT

Keep it concise: the answer, relevant paths and line numbers, and any uncertainty.
Do not dump whole files and do not submit an empty report.
"""


@dataclass
class AgentProfile:
    name: str
    description: str
    system_prompt: str
    tools: list[str] = field(default_factory=lambda: ["*"])
    inject_prompts: bool = True
    max_steps: int = 0
    path: Path | None = None
    builtin: bool = False
    model_name: str | None = None
    """Model name for this profile. ``None``/``inherit`` uses the parent model."""
    model_class: str | None = None
    model_kwargs: dict = field(default_factory=dict)
    reasoning_effort: str | None = None
    """Per-profile reasoning effort. The Task call can override it per invocation."""

    def line(self) -> str:
        tool_text = ", ".join(self.tools) if self.tools else "(manager only)"
        return f"- {self.name}: {self.description} (Tools: {tool_text})"


BUILTIN_PROFILES: dict[str, AgentProfile] = {
    "general-purpose": AgentProfile(
        name="general-purpose",
        description=(
            "General-purpose agent for researching complex questions, searching code, and executing "
            "multi-step tasks. Use it when a task would flood the main context with search results or "
            "file dumps; it returns only the conclusion."
        ),
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        tools=["*"],
        inject_prompts=True,
        builtin=True,
    ),
    "explore": AgentProfile(
        name="explore",
        description=(
            "Read-only search agent for broad fan-out searches. It locates code and returns paths and "
            "conclusions, not file contents. Specify search breadth in the prompt."
        ),
        system_prompt=EXPLORE_SYSTEM_PROMPT,
        tools=["Read", "Grep", "Glob"],
        inject_prompts=False,
        builtin=True,
    ),
}


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


def parse_agent_profile(path: Path) -> AgentProfile | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read agent profile %s: %s", path, exc)
        return None
    meta, body = parse_frontmatter(text)
    name = str(meta.get("name") or path.stem).strip()
    if not name:
        return None
    description = str(meta.get("description") or "").strip()
    if not description:
        for line in body.splitlines():
            if line.strip():
                description = line.strip()
                break
    raw_tools = meta.get("tools")
    tools = _as_list(raw_tools) if raw_tools is not None else ["*"]
    inject = meta.get("inject_prompts", meta.get("inject_agents_md", True))
    if isinstance(inject, str):
        inject = inject.lower() not in {"false", "0", "no"}
    try:
        max_steps = int(meta.get("max_steps") or 0)
    except (TypeError, ValueError):
        max_steps = 0
    return AgentProfile(
        name=name,
        description=description,
        system_prompt=body.strip() or DEFAULT_SYSTEM_PROMPT,
        tools=tools,
        inject_prompts=bool(inject),
        max_steps=max_steps,
        path=path.resolve(),
        model_name=str(meta.get("model") or meta.get("model_name") or "") or None,
        model_class=str(meta.get("model_class") or "") or None,
        model_kwargs=dict(meta.get("model_kwargs") or {}),
        reasoning_effort=str(meta.get("reasoning_effort") or meta.get("reasoning") or "") or None,
    )


def _resolve_dir(spec: str, workspace: Path) -> Path:
    path = Path(spec).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def load_agent_profiles(config: SubagentsConfig, workspace: Path) -> dict[str, AgentProfile]:
    profiles = {name: profile for name, profile in BUILTIN_PROFILES.items()}
    for spec in config.profile_dirs:
        base = _resolve_dir(spec, workspace)
        if not base.is_dir():
            logger.debug("agent profile directory not found: %s", base)
            continue
        for path in sorted(base.glob("*.md")):
            profile = parse_agent_profile(path)
            if profile is not None:
                profiles[profile.name] = profile
    return profiles


def format_profiles_for_prompt(profiles: dict[str, AgentProfile]) -> str:
    if not profiles:
        return ""
    ordered = [profiles[name] for name in sorted(profiles)]
    return "\n".join(["Available agent types for the Task tool:", *[profile.line() for profile in ordered]])


def _clone_model(model):
    config = getattr(model, "config", None)
    dump = getattr(config, "model_dump", None)
    if dump is None:
        return None
    try:
        return type(model)(**dump())
    except Exception:
        logger.exception("could not clone model %s for subagent", type(model).__name__)
        return None


def _build_sub_model(parent, profile: AgentProfile, reasoning_effort: str | None = None):
    """Create the subagent model: switch models or inherit, and apply reasoning/payload overrides."""
    effort = reasoning_effort or profile.reasoning_effort
    overrides = dict(profile.model_kwargs or {})
    if effort:
        overrides["reasoning_effort"] = effort

    if profile.model_name and profile.model_name.lower() != "inherit":
        from minisweagent.models import get_model

        try:
            return get_model(
                input_model_name=profile.model_name,
                config={
                    "model_class": profile.model_class or "litellm",
                    "model_kwargs": overrides,
                },
            )
        except Exception:
            logger.exception("could not create model %s for subagent", profile.model_name)
            return None

    model = _clone_model(parent.model)
    if model is None:
        return None
    if overrides:
        base = dict(getattr(model.config, "model_kwargs", {}) or {})
        base.update(overrides)
        model.config.model_kwargs = base
    return model


def _subagent_registry(parent, profile: AgentProfile) -> ToolRegistry:
    registry = ToolRegistry()
    # Reuse shared extension tools (Skill, MCP, Browser, Vision, plugin tools).
    # Workspace tools are rebuilt with fresh state so subagents do not contaminate
    # the parent's read-before-edit tracking or todo list.
    fresh = {"Read", "Grep", "Glob", "Write", "Edit", "TodoWrite"}
    for name in parent._tools.names():  # noqa: SLF001 - extensions are part of the agent
        if name in fresh or name == "Task":
            continue
        registry._tools[name] = parent._tools._tools[name]  # noqa: SLF001

    tracker = FileTracker()
    registry.extend(build_read_only_tools(parent._workspace, tracker))  # noqa: SLF001
    if parent.config.tools.edit:
        registry.extend(build_edit_tools(parent._workspace, tracker))  # noqa: SLF001
    if parent.config.tools.todo:
        registry.add(build_todo_tool(TodoStore(parent._workspace, persist=parent.config.tools.todo_persist)))  # noqa: SLF001

    return registry.filter(profile.tools)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)] + "\n...[truncated]"


def build_task_tool(parent, profiles: dict[str, AgentProfile], config: SubagentsConfig) -> Tool:
    available = ", ".join(sorted(profiles))

    def handler(args: dict) -> dict:
        name = str(args.get("subagent_type") or config.default_type).strip()
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return err("prompt is required")
        profile = profiles.get(name)
        if profile is None:
            return err(f"Unknown subagent_type {name!r}. Available: {available}")

        depth = getattr(parent, "_subagent_depth", 0)
        if depth >= config.max_depth:
            return err(
                f"Subagent depth limit reached ({config.max_depth}); finish the task yourself "
                "or delegate to an existing subagent."
            )

        reasoning = args.get("reasoning_effort") or config.reasoning_effort
        try:
            with GLOBAL_SUBAGENT_GATE.slot():
                sub_model = _build_sub_model(parent, profile, reasoning)
                if sub_model is None:
                    return err("The configured model cannot be created for a subagent")
                tools = _subagent_registry(parent, profile)
                setter = getattr(sub_model, "set_extra_tools", None)
                if setter is None and tools.names():
                    return err("The configured model does not support extension tools (use model_class=litellm)")
                if setter is not None:
                    setter(tools.specs())

                sections = []
                if profile.inject_prompts:
                    sections.extend(parent._prompt_sections)  # noqa: SLF001
                if config.inherit_skills and "Skill" in tools.names() and parent._skills_section:  # noqa: SLF001
                    sections.append(parent._skills_section)  # noqa: SLF001

                sub_config = parent.config.model_copy(deep=True)
                sub_config.system_template = profile.system_prompt
                sub_config.instance_template = "{{task}}"
                sub_config.output_path = None
                sub_config.subagents.enabled = False
                sub_config.skills.enabled = False
                sub_config.mcp.enabled = False
                sub_config.browser.enabled = False
                sub_config.vision.enabled = False
                sub_config.plugins.enabled = False
                sub_config.automations.enabled = False
                sub_config.tools.read_only = False
                sub_config.prompts.files = []
                sub_config.prompts.dirs = []
                if config.step_limit:
                    sub_config.step_limit = config.step_limit
                elif profile.max_steps:
                    sub_config.step_limit = profile.max_steps

                from minisweagent.extensions.browser import CURRENT_GROUP, CURRENT_SESSION

                base_session = CURRENT_SESSION.get() or parent.config.browser.session or "mini"
                session_token = CURRENT_SESSION.set(f"{base_session}-sub{depth + 1}-{uuid.uuid4().hex[:4]}")
                base_group = CURRENT_GROUP.get() or parent.config.browser.group_title or "mini"
                group_token = CURRENT_GROUP.set(f"{base_group} / {profile.name}")
                try:
                    from minisweagent.agents.default import DefaultAgent

                    subagent = DefaultAgent(
                        sub_model,
                        parent.env,
                        tool_registry=tools,
                        prompt_sections=sections,
                        **sub_config.model_dump(exclude={"agent_class"}),
                    )
                    subagent._subagent_depth = depth + 1  # noqa: SLF001
                    subagent._owns_mcp = False  # noqa: SLF001
                    result = subagent.run(prompt)
                finally:
                    CURRENT_GROUP.reset(group_token)
                    CURRENT_SESSION.reset(session_token)
        except TimeoutError as exc:
            return err(str(exc))
        except Exception as exc:
            logger.exception("subagent %s failed", name)
            return err(f"Subagent {name} failed: {type(exc).__name__}: {exc}")

        parent.cost += subagent.cost
        parent.n_calls += subagent.n_calls
        parent.n_compaction_calls += subagent.n_compaction_calls

        status = result.get("exit_status") or "unknown"
        submission = result.get("submission") or result.get("content") or ""
        body = _truncate(str(submission), config.max_result_chars)
        model_name = getattr(getattr(sub_model, "config", None), "model_name", "inherit")
        return ok(
            f'<subagent_result type="{name}" model="{model_name}" status="{status}">\n{body}\n</subagent_result>'
        )

    return Tool(
        name="Task",
        description=(
            "Delegate a self-contained task to a subagent with an isolated context. "
            "The subagent does not see this conversation; give it all needed context and a clear "
            "deliverable. Use it for broad searches or multi-step side work whose intermediate output "
            "would flood the main context.\n\n" + format_profiles_for_prompt(profiles)
        ),
        parameters={
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Short (3-5 word) task description"},
                "prompt": {"type": "string", "description": "Complete, self-contained instructions for the subagent"},
                "subagent_type": {
                    "type": "string",
                    "description": f"Agent profile to use. One of: {available}",
                    "default": config.default_type,
                },
                "reasoning_effort": {
                    "type": "string",
                    "description": (
                        "Optional thinking effort for this subagent (provider payload field). "
                        "Overrides the profile's reasoning_effort. Examples: minimal, low, medium, high, max."
                    ),
                },
            },
            "required": ["description", "prompt"],
        },
        handler=handler,
        read_only=False,
    )
