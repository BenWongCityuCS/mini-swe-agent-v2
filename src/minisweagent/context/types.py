"""Structured context sections, inspired by ZCode's context builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

InjectionTarget = Literal["system", "meta_user", "system_reminder"]
CacheHint = Literal["stable", "dynamic"]


@dataclass
class ContextSection:
    name: str
    source: str
    content: str
    injection_target: InjectionTarget = "system"
    cache_hint: CacheHint = "dynamic"
    priority: int = 50
    """Higher priority sections are kept first when the total budget is exceeded."""
    budget_tokens: int | None = None
    compact_form: str | None = None
    """Cheaper representation used when the section exceeds its budget."""

    @property
    def chars(self) -> int:
        return len(self.content)

    @property
    def tokens(self) -> int:
        # Imported lazily to keep this module dependency-free.
        from minisweagent.context.utils import estimate_tokens

        return estimate_tokens(self.content)

    @property
    def preview(self) -> str:
        return self.content[:100]


@dataclass
class ContextBuildResult:
    sections: list[ContextSection] = field(default_factory=list)
    system_content: str = ""
    meta_user_content: str = ""
    reminders: list[ContextSection] = field(default_factory=list)
    total_chars: int = 0
    total_tokens: int = 0
    dropped: list[str] = field(default_factory=list)

    def stats(self) -> dict:
        return {
            "total_chars": self.total_chars,
            "total_tokens": self.total_tokens,
            "dropped": list(self.dropped),
            "sections": [
                {
                    "name": section.name,
                    "source": section.source,
                    "target": section.injection_target,
                    "cache": section.cache_hint,
                    "chars": section.chars,
                    "tokens": section.tokens,
                    "budget_tokens": section.budget_tokens,
                    "preview": section.preview,
                }
                for section in self.sections + self.reminders
            ],
        }
