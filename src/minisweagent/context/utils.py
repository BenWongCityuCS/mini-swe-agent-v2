"""Token estimation and budget helpers for context sections."""

from __future__ import annotations

import math
import re

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ESTIMATED_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Rough token estimate: CJK characters count double, others count once."""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return max(1, math.ceil((cjk * 2 + other) / _ESTIMATED_CHARS_PER_TOKEN))


def truncate_to_tokens(text: str, budget_tokens: int) -> str:
    if budget_tokens <= 0:
        return ""
    if estimate_tokens(text) <= budget_tokens:
        return text
    # Reserve room for the marker and keep both the opening instructions and
    # the most recent tail, which is usually what matters.
    char_budget = max(40, int(budget_tokens * _ESTIMATED_CHARS_PER_TOKEN) - 80)
    head = int(char_budget * 0.7)
    tail = char_budget - head
    marker = "\n...[section truncated to fit context budget]...\n"
    result = text[:head] + marker + (text[-tail:] if tail > 0 else "")
    return result[: max(0, int(budget_tokens * _ESTIMATED_CHARS_PER_TOKEN))]


def join_sections(sections) -> str:
    return "\n\n".join(section.content for section in sections if section.content)
