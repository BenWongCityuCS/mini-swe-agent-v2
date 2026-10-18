"""Configuration models for the optional agent extensions.

Everything defaults to disabled so that a stock mini-swe-agent config keeps its
original behaviour. The bundled ``extensions.yaml`` enables the modern tool set
(TodoWrite + Edit/Write) and the other opt-in features.
"""

from typing import Literal

from pydantic import BaseModel, Field


class ContextConfig(BaseModel):
    """ZCode-style context assembly: sections, budgets, meta-user, reminders."""

    enabled: bool = False
    max_total_tokens: int = 40_000
    max_section_tokens: int = 20_000
    section_budgets: dict[str, int] = Field(
        default_factory=lambda: {
            "env_info": 1_000,
            "current_date": 100,
            "prompts": 20_000,
            "skills": 20_000,
            "subagents": 20_000,
            "todo": 2_000,
            "compaction": 8_000,
        }
    )
    meta_user: bool = True
    """Inject skills/profiles as a meta-user prefix instead of the system prompt."""
    system_reminders: bool = True
    reminder_tokens: int = 2_000
    env_info: bool = True
    current_date: bool = True


class SkillsConfig(BaseModel):
    """Discover ``SKILL.md`` files and expose them through a ``Skill`` tool."""

    enabled: bool = False
    dirs: list[str] = Field(default_factory=lambda: ["./skills", "~/.mini-swe-agent/skills"])
    max_description_chars: int = 250
    max_index_chars: int = 20_000
    max_bytes: int = 100_000


class SubagentsConfig(BaseModel):
    """Expose a ``Task`` tool that runs nested agents with isolated contexts."""

    enabled: bool = False
    profile_dirs: list[str] = Field(default_factory=lambda: ["./agents", "~/.mini-swe-agent/agents"])
    max_depth: int = 1
    default_type: str = "general-purpose"
    max_result_chars: int = 20_000
    inherit_skills: bool = True
    step_limit: int = 0
    """Per-subagent step limit. 0 inherits the parent agent's limit."""
    max_concurrent: int = 3
    """Global cap on simultaneously running subagents (all agents share it)."""
    parallel_task_calls: bool = True
    """Run multiple Task tool calls from one model response in parallel."""
    reasoning_effort: str | None = None
    """Default reasoning effort for subagents. ``None`` inherits the model payload."""


class McpServerConfig(BaseModel):
    """One MCP server (stdio, streamable HTTP, or SSE)."""

    type: Literal["stdio", "http", "sse"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class McpConfig(BaseModel):
    enabled: bool = False
    config_file: str | None = None
    """JSON/YAML file using the Claude-style ``{"mcpServers": {...}}`` shape."""
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    timeout_seconds: float = 60.0
    max_output_chars: int = 50_000
    tool_prefix: str = "mcp"
    required: bool = False
    """Fail agent construction when a server cannot start (default: skip it)."""


class PromptsConfig(BaseModel):
    """Mount Markdown/text files into the system prompt."""

    files: list[str] = Field(default_factory=list)
    dirs: list[str] = Field(default_factory=list)
    required: bool = False
    tag: str = "mounted_instructions"
    header: str = "Mounted instructions"


class ToolsConfig(BaseModel):
    """Built-in tools. Read/Grep/Glob are always available to subagents."""

    read_only: bool = False
    """Register Read/Grep/Glob for the main agent as well."""
    todo: bool = False
    """Register TodoWrite."""
    edit: bool = False
    """Register Write/Edit with read-before-edit enforcement."""
    todo_persist: bool = True
    """Persist the todo list under .mini-swe-agent/todos.json."""


class PluginsConfig(BaseModel):
    """Discover plugin directories and merge everything they contribute."""

    enabled: bool = False
    dirs: list[str] = Field(default_factory=lambda: ["./plugins", "~/.mini-swe-agent/plugins"])
    paths: list[str] = Field(default_factory=list)
    """Explicit plugin directories (in addition to ``dirs``)."""


class AutomationsConfig(BaseModel):
    """Scheduled tasks stored as YAML files (see ``mini-automation``)."""

    enabled: bool = False
    dirs: list[str] = Field(default_factory=lambda: ["./automations", "~/.mini-swe-agent/automations"])
    state_file: str | None = None


class BrowserUseConfig(BaseModel):
    """Settings for the optional browser-use backend (Playwright-based)."""

    headless: bool = False
    executable_path: str | None = None
    user_data_dir: str | None = None
    """Persistent profile directory. Do not point this at a running Chrome profile."""
    cdp_url: str | None = None
    """Attach to an existing Chrome via CDP instead of launching one."""
    keep_alive: bool = True


class BrowserConfig(BaseModel):
    """Browser control. WebBridge (real Chrome/Edge) and browser-use (Playwright)."""

    enabled: bool = False
    backend: Literal["webbridge", "browser_use", "auto"] = "webbridge"
    """webbridge = user's real browser; browser_use = Playwright; auto = prefer WebBridge when ready."""
    fallback: bool = True
    """When a backend fails, retry the other one for that call."""
    daemon_url: str = "http://127.0.0.1:10086/command"
    status_url: str = "http://127.0.0.1:10086/status"
    proxy_url: str | None = "http://127.0.0.1:11086/command"
    """Optional tab-limit proxy (per-session + global tab budgets)."""
    prefer_proxy: bool = True
    session: str | None = None
    """Session name; generated when omitted. Subagents get a derived session."""
    group_title: str | None = None
    max_tabs_per_session: int = 3
    """Refuse a new tab when the session already has this many (0 = no limit)."""
    auto_close_on_finish: bool = True
    timeout_seconds: float = 120.0
    max_output_chars: int = 30_000
    network_enabled: bool = True
    """False blocks external navigation; localhost/private/file/data stay allowed."""
    allowed_domains: list[str] = Field(default_factory=list)
    """If non-empty, only these domains and their subdomains are reachable."""
    blocked_domains: list[str] = Field(default_factory=list)
    """Always blocked (and their subdomains)."""
    allow_localhost: bool = True
    """Keep localhost/private IPs reachable when external networking is restricted."""
    browser_use: BrowserUseConfig = BrowserUseConfig()
    """Settings used when the browser-use backend is selected or used as a fallback."""


class VisionConfig(BaseModel):
    """Route images to a separate vision-capable model."""

    enabled: bool = False
    model: str | None = None
    model_kwargs: dict = Field(default_factory=dict)
    question: str = "Describe this image in detail, including any visible text."
    max_output_chars: int = 4_000
    auto_describe_screenshots: bool = True
    """When Browser returns a screenshot path, describe it via this model."""
