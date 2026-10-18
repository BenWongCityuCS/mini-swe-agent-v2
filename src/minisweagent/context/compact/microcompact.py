"""Microcompact: clear old tool results before running a full summary.

This is the cheap first line of defence. It keeps the conversation structure and
the most recent observations intact, replacing only old (usually large) tool
outputs with a short marker.
"""

from __future__ import annotations

CLEARED_TOOL_RESULT = "[Old tool result content cleared to save context]"
DEFAULT_KEEP_RECENT = 5
DEFAULT_MIN_TOKEN_SAVINGS = 256


def _is_compactable(message: dict) -> bool:
    role = message.get("role")
    if role == "tool":
        return True
    if role == "user" and "raw_output" in (message.get("extra") or {}):
        return True
    return False


def maybe_microcompact(
    messages: list[dict],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    min_token_savings: int = DEFAULT_MIN_TOKEN_SAVINGS,
) -> tuple[list[dict], int, int]:
    """Return ``(messages, saved_tokens, cleared_count)``.

    ``messages`` is returned unchanged when there is nothing worth clearing.
    """
    from minisweagent.context.utils import estimate_tokens

    candidates = [index for index, message in enumerate(messages) if _is_compactable(message)]
    if len(candidates) <= keep_recent:
        return messages, 0, 0

    to_clear = candidates[: len(candidates) - keep_recent]
    cleared = 0
    saved = 0
    updated = list(messages)
    for index in to_clear:
        message = dict(messages[index])
        content = message.get("content")
        if not isinstance(content, str) or len(content) < 200:
            continue
        before = estimate_tokens(content)
        after = estimate_tokens(CLEARED_TOOL_RESULT)
        if before - after <= 0:
            continue
        message["content"] = CLEARED_TOOL_RESULT
        extra = dict(message.get("extra") or {})
        extra["microcompacted"] = True
        message["extra"] = extra
        updated[index] = message
        saved += before - after
        cleared += 1

    if saved < min_token_savings or cleared == 0:
        return messages, 0, 0
    return updated, saved, cleared
