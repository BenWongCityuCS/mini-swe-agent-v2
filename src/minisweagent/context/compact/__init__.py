"""Compaction policy, microcompact, and prompts.

The summary engine itself lives in :mod:`minisweagent.agents.compaction` to keep
the historical import path working. This package is the policy layer around it.
"""

from minisweagent.context.compact.microcompact import CLEARED_TOOL_RESULT, maybe_microcompact
from minisweagent.context.compact.policy import (
    DEFAULT_BUFFER_TOKENS,
    DEFAULT_OUTPUT_RESERVE_TOKENS,
    MAX_CONSECUTIVE_FAILURES,
    CompactDecision,
    compact_threshold,
    output_reserve,
    should_compact,
)
from minisweagent.context.compact.prompt import build_compact_prompt, build_summary_template

__all__ = [
    "CLEARED_TOOL_RESULT",
    "DEFAULT_BUFFER_TOKENS",
    "DEFAULT_OUTPUT_RESERVE_TOKENS",
    "MAX_CONSECUTIVE_FAILURES",
    "CompactDecision",
    "build_compact_prompt",
    "build_summary_template",
    "compact_threshold",
    "maybe_microcompact",
    "output_reserve",
    "should_compact",
]
