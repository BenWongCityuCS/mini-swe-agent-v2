"""Section builders for the context assembler."""

from __future__ import annotations

import platform
from datetime import datetime
from pathlib import Path

from minisweagent.context.types import ContextSection


def base_section(content: str) -> ContextSection:
    return ContextSection(
        name="base system prompt",
        source="base",
        content=content,
        injection_target="system",
        cache_hint="stable",
        priority=100,
    )


def env_section(workspace: Path, model_name: str, budget: int = 1_000) -> ContextSection:
    content = (
        "<environment>\n"
        f"workspace: {workspace}\n"
        f"platform: {platform.system()} {platform.release()}\n"
        f"python: {platform.python_version()}\n"
        f"model: {model_name}\n"
        f"date: {datetime.now().date().isoformat()}\n"
        "</environment>"
    )
    return ContextSection(
        name="environment",
        source="env_info",
        content=content,
        injection_target="system",
        cache_hint="stable",
        priority=90,
        budget_tokens=budget,
    )


def date_section(now: datetime | None = None, budget: int = 100) -> ContextSection:
    now = now or datetime.now()
    return ContextSection(
        name="current date",
        source="current_date",
        content=f"Current date: {now.date().isoformat()}",
        injection_target="system",
        cache_hint="dynamic",
        priority=85,
        budget_tokens=budget,
    )


def prompts_section(mounted: list, budget: int = 20_000) -> ContextSection | None:
    if not mounted:
        return None
    body = "\n\n".join(f"<!-- {prompt.path} -->\n{prompt.content}" for prompt in mounted)
    return ContextSection(
        name="mounted instructions",
        source="prompts",
        content=f"<mounted_instructions>\nMounted instructions\n\n{body}\n</mounted_instructions>",
        injection_target="system",
        cache_hint="stable",
        priority=80,
        budget_tokens=budget,
    )


def skills_section(skills: dict, max_description_chars: int, budget: int = 20_000) -> ContextSection | None:
    if not skills:
        return None
    unique = {skill.name: skill for skill in skills.values()}
    ordered = [unique[name] for name in sorted(unique, key=str.lower)]
    full_lines = [
        "The following skills are available for use with the Skill tool:",
        "",
        *[skill.line(max_description_chars) for skill in ordered],
    ]
    compact_lines = [
        "The following skills are available for use with the Skill tool:",
        "",
        *[f"- {skill.name} (file: {skill.path})" for skill in ordered],
    ]
    return ContextSection(
        name="skills",
        source="skills",
        content="\n".join(full_lines),
        injection_target="meta_user",
        cache_hint="dynamic",
        priority=60,
        budget_tokens=budget,
        compact_form="\n".join(compact_lines),
    )


def subagents_section(profiles: dict, budget: int = 20_000) -> ContextSection | None:
    if not profiles:
        return None
    ordered = [profiles[name] for name in sorted(profiles)]
    full_lines = ["Available agent types for the Task tool:", *[profile.line() for profile in ordered]]
    compact_lines = [
        "Available agent types for the Task tool:",
        *[f"- {profile.name}: {profile.description.split('.')[0]}." for profile in ordered],
    ]
    return ContextSection(
        name="subagent profiles",
        source="subagents",
        content="\n".join(full_lines),
        injection_target="meta_user",
        cache_hint="dynamic",
        priority=55,
        budget_tokens=budget,
        compact_form="\n".join(compact_lines),
    )


def todo_section(todo, budget: int = 2_000) -> ContextSection | None:
    if todo is None or not todo.items:
        return None
    return ContextSection(
        name="todo list",
        source="todo",
        content="Current todo list:\n" + todo.format(),
        injection_target="system_reminder",
        cache_hint="dynamic",
        priority=50,
        budget_tokens=budget,
    )


def compaction_section(state, budget: int = 8_000) -> ContextSection | None:
    if state is None or not state.summary:
        return None
    content = (
        "Context compaction is active. Earlier messages were replaced by this checkpoint:\n"
        f"{state.summary}"
    )
    return ContextSection(
        name="compaction checkpoint",
        source="compaction",
        content=content,
        injection_target="system_reminder",
        cache_hint="dynamic",
        priority=70,
        budget_tokens=budget,
    )
