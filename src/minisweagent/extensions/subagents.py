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
from minisweagent.extensions.todo import TodoStore, build_todo_read_tool, build_todo_tool
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

PLAN_SYSTEM_PROMPT = """You are a read-only planning subagent.

Inspect the codebase and produce an implementation plan. Do not modify files
and do not attempt the work yourself. Identify the affected files and symbols,
then give ordered, concrete steps, risks, and the tests that should pass.

When you are done, submit the plan with ONE bash command that prints the marker
on the first line and the plan after it. Use a heredoc, for example:

cat <<'MSWEA_SUBMIT'
COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
Goal: ...
Files: /path/to/file.py:123
Steps:
1. ...
Risks: ...
Tests: ...
MSWEA_SUBMIT

Do not submit an empty report.
"""

VISUAL_JUDGE_SYSTEM_PROMPT = """You are a visual acceptance judge.

Inspect the images the coordinator points at. Read attaches images as native
model input when enabled; use the Vision tool when it is available. Judge what
a user would actually see: layout, composition, legibility, clipping, overlap,
spacing, alignment, and visual asset quality. Do not speculate about code.

Return one JSON object per line, and nothing else in the verdict list:
{"image": "path-or-label", "verdict": "pass", "issues": []}

A fail must name something visible in the image. If everything looks acceptable,
return pass with an empty issues list.

Submit with ONE bash command that prints the marker on the first line and the
verdict lines after it:

cat <<'MSWEA_SUBMIT'
COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
{"image": "page-1.png", "verdict": "pass", "issues": []}
MSWEA_SUBMIT
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
    "plan": AgentProfile(
        name="plan",
        description=(
            "Read-only planning agent. Investigates the codebase and returns an ordered, "
            "implementation-ready plan with files, risks, and tests. It does not edit."
        ),
        system_prompt=PLAN_SYSTEM_PROMPT,
        tools=["Read", "Grep", "Glob"],
        inject_prompts=False,
        max_steps=40,
        builtin=True,
    ),
    "visual-judge": AgentProfile(
        name="visual-judge",
        description=(
            "Visual acceptance agent. Inspects rendered screenshots or images and returns one "
            "pass/fail verdict per image with evidence-backed issues. Needs read_media or Vision."
        ),
        system_prompt=VISUAL_JUDGE_SYSTEM_PROMPT,
        tools=["Read", "Glob", "Vision"],
        inject_prompts=False,
        max_steps=30,
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


def _subagent_registry(parent, profile: AgentProfile, agent_id: str) -> ToolRegistry:
    registry = ToolRegistry()
    # Reuse shared extension tools (Skill, MCP, Browser, Vision, plugin tools).
    # Workspace tools are rebuilt with fresh state so subagents do not contaminate
    # the parent's read-before-edit tracking or todo list.
    fresh = {"Read", "Grep", "Glob", "Write", "Edit", "TodoWrite", "TodoRead", "SendMessage"}
    for name in parent._tools.names():  # noqa: SLF001 - extensions are part of the agent
        if name in fresh or name == "Task":
            continue
        registry._tools[name] = parent._tools._tools[name]  # noqa: SLF001

    tracker = FileTracker()
    registry.extend(
        build_read_only_tools(
            parent._workspace,  # noqa: SLF001
            tracker,
            read_media=parent.config.tools.read_media,  # noqa: SLF001
            max_image_bytes=parent.config.tools.max_image_bytes,  # noqa: SLF001
            max_video_bytes=parent.config.tools.max_video_bytes,  # noqa: SLF001
            media_detail=parent.config.tools.media_detail,  # noqa: SLF001
        )
    )
    if parent.config.tools.edit:  # noqa: SLF001
        registry.extend(build_edit_tools(parent._workspace, tracker))  # noqa: SLF001
    if parent.config.tools.todo:  # noqa: SLF001
        store = TodoStore(parent._workspace, persist=parent.config.tools.todo_persist)  # noqa: SLF001
        registry.add(build_todo_tool(store))
        registry.add(build_todo_read_tool(store))
    bus = getattr(parent, "_bus", None)
    if bus is not None:
        from minisweagent.extensions.agent_bus import build_send_message_tool

        registry.add(build_send_message_tool(bus, agent_id))

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
        slug = "".join(ch if ch.isalnum() else "-" for ch in name).strip("-")[:24] or "agent"
        sub_agent_id = f"sub-{slug}-{uuid.uuid4().hex[:6]}"

        def run_subagent(cancel_event=None) -> dict:
            with GLOBAL_SUBAGENT_GATE.slot():
                sub_model = _build_sub_model(parent, profile, reasoning)
                if sub_model is None:
                    raise RuntimeError("The configured model cannot be created for a subagent")
                tools = _subagent_registry(parent, profile, sub_agent_id)
                setter = getattr(sub_model, "set_extra_tools", None)
                if setter is None and tools.names():
                    raise RuntimeError(
                        "The configured model does not support extension tools (use model_class=litellm)"
                    )
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
                sub_config.session.enabled = False
                sub_config.hooks.enabled = False
                sub_config.background.enabled = False
                sub_config.bus.enabled = False
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
                        background_manager=getattr(parent, "_background", None),
                        hook_runner=getattr(parent, "_hooks", None),
                        agent_bus=getattr(parent, "_bus", None),
                        agent_id=sub_agent_id,
                        cancel_event=cancel_event,
                        **sub_config.model_dump(exclude={"agent_class"}),
                    )
                    subagent._subagent_depth = depth + 1  # noqa: SLF001
                    subagent._owns_mcp = False  # noqa: SLF001
                    result = subagent.run(prompt)
                finally:
                    CURRENT_GROUP.reset(group_token)
                    CURRENT_SESSION.reset(session_token)

            parent.cost += subagent.cost
            parent.n_calls += subagent.n_calls
            parent.n_compaction_calls += subagent.n_compaction_calls

            status = result.get("exit_status") or "unknown"
            submission = result.get("submission") or result.get("content") or ""
            return {
                "exit_status": status,
                "submission": _truncate(str(submission), config.max_result_chars),
                "model_name": getattr(getattr(sub_model, "config", None), "model_name", "inherit"),
            }

        if args.get("run_in_background"):
            background = getattr(parent, "_background", None)
            if background is None:
                return err("Background tasks are disabled (agent.background.enabled).")
            # Register now so SendMessage can target the subagent before its
            # worker thread has finished constructing the agent.
            bus = getattr(parent, "_bus", None)
            if bus is not None:
                bus.register(sub_agent_id)
            started = background.start_callable(
                lambda event: run_subagent(event),
                label=f"{name}: {prompt[:80]}",
                owner=parent.agent_id,
                kind="agent",
            )
            if started.get("returncode") != 0:
                return started
            task_id = started.get("task_id")
            return ok(
                f"Started background subagent {sub_agent_id} as {task_id}. "
                f"Use TaskOutput(task_id={task_id!r}) for the report, or "
                f"SendMessage(to={sub_agent_id!r}, ...) to steer it.",
                task_id=task_id,
                agent_id=sub_agent_id,
            )

        try:
            report = run_subagent()
        except TimeoutError as exc:
            return err(str(exc))
        except Exception as exc:
            logger.exception("subagent %s failed", name)
            return err(f"Subagent {name} failed: {type(exc).__name__}: {exc}")

        return ok(
            f'<subagent_result type="{name}" model="{report["model_name"]}" '
            f'status="{report["exit_status"]}" agent_id="{sub_agent_id}">\n'
            f'{report["submission"]}\n</subagent_result>',
            agent_id=sub_agent_id,
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
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Return immediately with a task id and agent id. Read the report with TaskOutput "
                        "and steer the subagent with SendMessage."
                    ),
                },
            },
            "required": ["description", "prompt"],
        },
        handler=handler,
        read_only=False,
    )
