"""Summary prompt for full compaction (adapted from ZCode's compact prompt)."""

from __future__ import annotations

NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.
"""

NO_TOOLS_TRAILER = (
    "\n\nREMINDER: Do NOT call any tools. Respond with plain text only — "
    "an <analysis> block followed by a <summary> block."
)

BASE_COMPACT_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
The summary must capture the technical details needed to continue the work without losing context.

Before the summary, use <analysis> tags to work through the conversation:
- the user's explicit requests and intent
- your approach and key decisions
- files inspected or changed, with paths and full snippets where important
- commands run and their relevant results
- errors and how they were fixed
- user feedback that changed the direction of the work
- security-relevant instructions or constraints (preserve them verbatim)
- pending tasks and the immediate next step

Then write a <summary> block with these sections:
1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections
4. Errors and Fixes
5. Problem Solving
6. All User Messages (non-tool-result messages)
7. Pending Tasks
8. Current Work
9. Optional Next Step
"""


def build_compact_prompt(custom_instructions: str | None = None) -> str:
    extra = ""
    if custom_instructions and custom_instructions.strip():
        extra = f"\n\nAdditional Instructions:\n{custom_instructions.strip()}"
    return f"{NO_TOOLS_PREAMBLE}{BASE_COMPACT_PROMPT}{extra}{NO_TOOLS_TRAILER}"


def build_summary_template(custom_instructions: str | None = None) -> str:
    """Jinja template used by the compaction engine."""
    return (
        build_compact_prompt(custom_instructions)
        + "\n\nPrevious checkpoint:\n{{ previous_summary or '(none)' }}"
        + "\n\nConversation to incorporate:\n{{ transcript }}\n"
    )


MINIMAL_SUMMARY_TEMPLATE = (
    "Create a concise checkpoint of the conversation. Preserve the objective, technical details, "
    "files changed or inspected, commands and results, errors, and the next action.\n\n"
    "Previous checkpoint:\n{{ previous_summary or '(none)' }}\n\n"
    "Conversation to incorporate:\n{{ transcript }}\n"
)
