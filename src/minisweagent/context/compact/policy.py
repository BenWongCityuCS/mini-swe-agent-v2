"""Compaction policy: thresholds, output reserve, and a circuit breaker."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_BUFFER_TOKENS = 13_000
DEFAULT_OUTPUT_RESERVE_TOKENS = 21_000
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class CompactDecision:
    should_compact: bool
    reason: str
    token_count: int
    threshold: int
    context_limit: int
    output_reserve: int
    buffer: int


def output_reserve(model_output_tokens: int) -> int:
    return min(model_output_tokens or DEFAULT_OUTPUT_RESERVE_TOKENS, DEFAULT_OUTPUT_RESERVE_TOKENS)


def compact_threshold(context_limit: int, buffer: int, output_tokens: int) -> int:
    # ZCode keeps the output reserve and the buffer as separate deductions:
    # effective_window = context - output_reserve; threshold = effective - buffer.
    return max(0, context_limit - output_reserve(output_tokens) - max(0, buffer))


def should_compact(
    *,
    token_count: int,
    context_limit: int,
    buffer: int,
    output_tokens: int,
    consecutive_failures: int = 0,
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    enough_messages: bool = True,
) -> CompactDecision:
    threshold = compact_threshold(context_limit, buffer, output_tokens)
    reserve = output_reserve(output_tokens)
    common = {
        "token_count": token_count,
        "threshold": threshold,
        "context_limit": context_limit,
        "output_reserve": reserve,
        "buffer": buffer,
    }
    if not enough_messages:
        return CompactDecision(False, "not_enough_messages", **common)
    if consecutive_failures >= max_consecutive_failures:
        return CompactDecision(False, "circuit_breaker", **common)
    if token_count < threshold:
        return CompactDecision(False, "below_threshold", **common)
    return CompactDecision(True, "above_threshold", **common)
