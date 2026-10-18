"""Optional extensions for mini-swe-agent: skills, subagents, MCP, mounted prompts.

All features are opt-in through the ``agent`` config section. The core agent
loop keeps working unchanged when none of them are enabled.
"""

from minisweagent.extensions.config import (
    McpConfig,
    McpServerConfig,
    PromptsConfig,
    SkillsConfig,
    SubagentsConfig,
    ToolsConfig,
)
from minisweagent.extensions.registry import Tool, ToolRegistry

__all__ = [
    "McpConfig",
    "McpServerConfig",
    "PromptsConfig",
    "SkillsConfig",
    "SubagentsConfig",
    "Tool",
    "ToolRegistry",
    "ToolsConfig",
]
