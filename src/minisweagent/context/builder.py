"""Assemble context sections with per-section and total token budgets."""

from __future__ import annotations

from pathlib import Path

from minisweagent.context.sections import (
    base_section,
    compaction_section,
    date_section,
    env_section,
    prompts_section,
    skills_section,
    subagents_section,
    todo_section,
)
from minisweagent.context.types import ContextBuildResult, ContextSection
from minisweagent.context.utils import estimate_tokens, join_sections, truncate_to_tokens
from minisweagent.extensions.config import ContextConfig


class ContextBuilder:
    def __init__(
        self,
        config: ContextConfig,
        *,
        base_prompt: str,
        workspace: Path,
        model_name: str,
        mounted_prompts: list | None = None,
        skills: dict | None = None,
        skills_max_description_chars: int = 250,
        profiles: dict | None = None,
    ) -> None:
        self.config = config
        self.base_prompt = base_prompt
        self.workspace = Path(workspace)
        self.model_name = model_name
        self.mounted_prompts = mounted_prompts or []
        self.skills = skills or {}
        self.skills_max_description_chars = skills_max_description_chars
        self.profiles = profiles or {}
        self._built: ContextBuildResult | None = None

    def _budget(self, source: str, default: int | None = None) -> int:
        return int(self.config.section_budgets.get(source, default or self.config.max_section_tokens))

    def _apply_budget(self, section: ContextSection) -> ContextSection:
        budget = section.budget_tokens or self._budget(section.source)
        section.budget_tokens = budget
        if estimate_tokens(section.content) <= budget:
            return section
        if section.compact_form is not None:
            if estimate_tokens(section.compact_form) <= budget:
                section.content = section.compact_form
            else:
                section.content = truncate_to_tokens(section.compact_form, budget)
            return section
        section.content = truncate_to_tokens(section.content, budget)
        return section

    def _static_sections(self) -> list[ContextSection]:
        sections: list[ContextSection] = [base_section(self.base_prompt)]
        if self.config.env_info:
            sections.append(env_section(self.workspace, self.model_name, self._budget("env_info", 1_000)))
        if self.config.current_date:
            sections.append(date_section(budget=self._budget("current_date", 100)))
        prompts = prompts_section(self.mounted_prompts, self._budget("prompts", 20_000))
        if prompts is not None:
            sections.append(prompts)
        skills = skills_section(
            self.skills,
            self.skills_max_description_chars,
            self._budget("skills", 20_000),
        )
        if skills is not None:
            sections.append(skills)
        profiles = subagents_section(self.profiles, self._budget("subagents", 20_000))
        if profiles is not None:
            sections.append(profiles)
        return sections

    def _enforce_total_budget(self, sections: list[ContextSection]) -> tuple[list[ContextSection], list[str]]:
        budget = self.config.max_total_tokens
        if budget <= 0 or sum(estimate_tokens(section.content) for section in sections) <= budget:
            return sections, []

        kept: list[ContextSection] = []
        dropped: list[str] = []
        used = 0
        for section in sorted(sections, key=lambda item: -item.priority):
            cost = estimate_tokens(section.content)
            if section.source == "base":
                kept.append(section)
                used += cost
                continue
            if used + cost <= budget:
                kept.append(section)
                used += cost
                continue
            if section.compact_form is not None:
                compact_cost = estimate_tokens(section.compact_form)
                if used + compact_cost <= budget:
                    section.content = section.compact_form
                    kept.append(section)
                    used += compact_cost
                    continue
                section.content = section.compact_form
            remaining = budget - used
            if remaining > 200:
                section.content = truncate_to_tokens(section.content, remaining)
                kept.append(section)
                used += estimate_tokens(section.content)
            else:
                dropped.append(section.name)
        return kept, dropped

    def build(self) -> ContextBuildResult:
        sections = [self._apply_budget(section) for section in self._static_sections()]
        sections, dropped = self._enforce_total_budget(sections)

        ordered = [
            *[section for section in sections if section.injection_target == "system" and section.cache_hint == "stable"],
            *[section for section in sections if section.injection_target == "system" and section.cache_hint == "dynamic"],
            *[section for section in sections if section.injection_target == "meta_user" and section.cache_hint == "stable"],
            *[section for section in sections if section.injection_target == "meta_user" and section.cache_hint == "dynamic"],
        ]
        system_sections = [section for section in ordered if section.injection_target == "system"]
        meta_sections = [section for section in ordered if section.injection_target == "meta_user"]

        meta_content = ""
        if self.config.meta_user and meta_sections:
            meta_content = (
                "As you answer the user's questions, you can use the following context:\n\n"
                + join_sections(meta_sections)
                + "\n\nIMPORTANT: this context may or may not be relevant to your tasks. "
                "You should not respond to this context unless it is highly relevant to your task."
            )

        result = ContextBuildResult(
            sections=ordered,
            system_content=join_sections(system_sections),
            meta_user_content=meta_content,
            total_chars=sum(section.chars for section in ordered),
            total_tokens=sum(estimate_tokens(section.content) for section in ordered),
            dropped=dropped,
        )
        self._built = result
        return result

    def build_reminders(self, todo, compaction_state, *, microcompacted: bool = False) -> list[ContextSection]:
        if not self.config.system_reminders:
            return []
        sections: list[ContextSection] = []
        reminder = todo_section(todo, self._budget("todo", 2_000))
        if reminder is not None:
            sections.append(reminder)
        compaction = compaction_section(compaction_state, self._budget("compaction", 8_000))
        if compaction is not None:
            sections.append(compaction)
        if microcompacted:
            sections.append(
                ContextSection(
                    name="microcompact notice",
                    source="microcompact",
                    content="Some old tool results were cleared to save context. Re-run a command if you need its output.",
                    injection_target="system_reminder",
                    cache_hint="dynamic",
                    priority=45,
                    budget_tokens=500,
                )
            )

        total_budget = max(200, self.config.reminder_tokens)
        kept: list[ContextSection] = []
        used = 0
        for section in sorted(sections, key=lambda item: -item.priority):
            cost = estimate_tokens(section.content)
            if used + cost <= total_budget:
                kept.append(section)
                used += cost
            elif total_budget - used > 100:
                section.content = truncate_to_tokens(section.content, total_budget - used)
                kept.append(section)
                used += estimate_tokens(section.content)
        return kept

    @staticmethod
    def reminder_content(sections: list[ContextSection]) -> str:
        if not sections:
            return ""
        body = "\n\n".join(section.content for section in sections)
        return f"<system-reminder>\n{body}\n</system-reminder>"
