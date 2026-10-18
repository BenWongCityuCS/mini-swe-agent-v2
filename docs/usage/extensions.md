# Extensions: skills, subagents, tools, MCP, plugins, browser, vision, automation, sessions, hooks

This fork adds an optional extension layer inspired by ZCode. Everything is
**opt-in** and defaults to disabled, so a stock config behaves like upstream
mini-swe-agent v2.

Layer `extensions.yaml` on top of `mini.yaml` for the second-generation preset:

```bash
mini -c mini.yaml -c extensions.yaml --task "..."
```

Extensions require a tool-call model (`--model-class litellm`). Text-action
models do not expose tool schemas and raise a clear error if extensions are enabled.

## Built-in modern tools

| Tool | Purpose |
| --- | --- |
| `TodoWrite` / `TodoRead` | Persistent task list at `.mini-swe-agent/todos.json` |
| `Read` | Read a file with line numbers; attach images/videos with `read_media` |
| `Grep` | Regex search returning `path:line:text` |
| `Glob` | List files by glob |
| `Write` | Create/replace a file |
| `Edit` | Exact string replacement |
| `Skill` | Load a skill body on demand |
| `Task` | Delegate to a subagent (sync or background) |
| `TaskOutput` / `TaskStop` | Read or stop background commands and subagents |
| `SendMessage` | Message another agent (`main` or a background subagent) |
| `SessionRead` | Read a previous session transcript |
| `Vision` | Ask a separate vision model about an image |
| `Browser` | Drive Chrome/Edge via Kimi WebBridge |

`Write` and `Edit` enforce **read-before-edit**: the file must have been read in
the current session and must not have changed on disk since. This prevents stale
edits and accidental clobbering. Subagents get their own tracker and todo list;
they never contaminate the coordinator's state.

```yaml
agent:
  tools:
    read_only: true   # also expose Read/Grep/Glob to the main agent
    todo: true
    edit: true
    todo_persist: true
    read_media: true  # Read attaches images/videos as native model input
    max_image_bytes: 5000000
    max_video_bytes: 20000000
```

## Background tasks

`background.enabled` adds `TaskOutput` and `TaskStop` and lets the built-in
`bash` tool take `run_in_background: true`:

```yaml
agent:
  background:
    enabled: true
    max_tasks: 16
    max_output_chars: 200000
    default_timeout: 0     # 0 = no timeout
    kill_on_close: true
```

The model starts a command and keeps working:

```json
{"command": "pytest -q", "run_in_background": true}
```

It then polls with `TaskOutput(task_id="bg-...", wait=true, timeout=30)` and
stops it with `TaskStop(task_id="bg-...")`. `TaskOutput` without `task_id` lists
all tasks; pass the returned `next_offset` to read only new output.

`Task(..., run_in_background: true)` does the same for a subagent. The result
includes both a task id and an agent id, so the coordinator can read the final
report with `TaskOutput` and steer the subagent with `SendMessage`.

With `LocalEnvironment`, output streams while the command runs and `TaskStop`
kills the whole process group. Other environments execute the blocking
`env.execute` call in a worker thread: output appears when the command finishes
and stopping is cooperative.

## Session persistence and resume

```yaml
agent:
  session:
    enabled: true
    dir: "~/.mini-swe-agent/sessions"
    autosave: true
    keep_media: true
    max_sessions: 0
```

Every step writes a JSON session containing the messages, counters, compaction
state, and todo list (the trajectory file is still written separately). CLI:

```bash
mini -c mini.yaml -c extensions.yaml --task "..."          # new session
mini ... --resume 20261019-101500-ab12cd                   # by id
mini ... --resume /path/to/session.json                    # by path
mini ... --continue                                        # latest in this workspace
mini ... --session-id my-fix                               # choose the id
mini --list-sessions                                       # list and exit
```

Resuming appends a short "continue" user message and keeps the original system
prompt, so the model sees the same context it had before. `SessionRead` lets a
running agent inspect a previous transcript (text only; images are omitted).

## Hooks

Hooks are shell commands. They receive a JSON payload on stdin and run with the
workspace as cwd:

```yaml
agent:
  hooks:
    enabled: true
    auto_discover: true   # .mini-swe-agent/hooks.yaml + ~/.mini-swe-agent/hooks.yaml
    PreToolUse:
      - matcher: "bash|Write|Edit"   # glob(s), comma separated; * = all
        command: "python .mini-swe-agent/guard.py"
        timeout: 30
    PostToolUse:
      - matcher: "*"
        command: "python .mini-swe-agent/audit.py"
```

Supported events: `PreToolUse`, `PostToolUse`, `UserPromptSubmit`,
`SessionStart`, `SessionEnd`, `Stop`, `PreCompact`, `PostCompact`.

Payload fields: `event`, `session_id`, `cwd`, and for tool events `tool_name`,
`tool_input`, `tool_result`. Exit code **2 blocks** the action and returns stderr
to the model. Other non-zero exits are logged but do not block; a timeout blocks
unless the entry sets `fail_open: true`. Stdout is appended to the tool result
(or injected as `<hook_context>` for session/prompt events). `${WORKSPACE}`,
`${SESSION_ID}`, and `${TOOL_NAME}` are expanded in the command.

## Agent messaging

```yaml
agent:
  bus:
    enabled: true
    max_inbox: 50
```

`SendMessage(to="main", message="...")` reports to the coordinator;
`SendMessage(to="sub-general-purpose-ab12cd", ...)` steers a background
subagent. Messages are queued and injected as `<agent_message>` user messages
before the recipient's next model call; the tool does not wait for a reply.

## Reading images and videos

With `tools.read_media: true`, `Read` detects common image and video extensions
and sends the file as native model input in the next user message (base64 data
URL). The tool result itself stays short, and token estimates ignore the binary
payload. `max_image_bytes` / `max_video_bytes` guard against oversized files.

Images use an `image_url` content part; videos use `video_url` by default. Set
`tools.video_content_type: image_url` for providers that expect video in an
image part. If the main model has no vision support, leave `read_media` off and
use the `Vision` tool instead.

## Context sections and compaction

The context layer follows ZCode's design instead of concatenating everything
into one system prompt:

- **Sections with budgets.** Each piece (base prompt, environment, date, mounted
  prompts, skills, subagent profiles, todo list, compaction checkpoint) is a
  section with a token budget, a priority, a cache hint, and a target.
  Over-budget sections use a cheaper compact form (for skills/profiles: a
  names-only list) before being truncated.
- **Total budget.** `max_total_tokens` caps the assembled context. When it is
  exceeded, low-priority sections are compacted, truncated, or dropped; the base
  prompt is always kept.
- **meta-user injection.** Skills and subagent profiles are sent as a
  `<meta_user>` prefix on the first user message instead of the system prompt.
  This keeps the system prompt stable for provider prompt caching and matches
  ZCode's `injectionTarget: "meta_user"`.
- **System reminders.** The current todo list, the active compaction checkpoint,
  and a microcompact notice are injected as an ephemeral
  `<system-reminder>...</system-reminder>` user message on every model call. They
  are **not** stored in the conversation history, so they do not accumulate.
- **Compaction subsystem.**
  1. **Microcompact** (cheap): when usage crosses ~90% of the threshold, old
     tool results are replaced with `[Old tool result content cleared to save
     context]`, keeping the most recent `microcompact_keep_recent` results.
  2. **Full compaction**: if still over the threshold, older conversation
     groups are summarized into a checkpoint with a ZCode-style prompt
     (analysis + summary sections), keeping the recent `keep_tokens`.
  3. **Circuit breaker**: after `max_consecutive_failures` summary failures the
     agent stops retrying compaction for the run instead of looping.
  4. If the summary prompt itself does not fit a small model's input budget,
     the engine falls back to a minimal template.

```yaml
agent:
  context:
    enabled: true
    max_total_tokens: 40000
    max_section_tokens: 20000
    meta_user: true
    system_reminders: true
    reminder_tokens: 2000
    env_info: true
    current_date: true
    section_budgets:
      skills: 20000
      subagents: 20000
      prompts: 20000
      todo: 2000
      compaction: 8000

  compaction:
    enabled: true
    auto: true
    context_limit: 299200   # required for self-hosted models
    keep_tokens: 15000
    buffer: 20000
    summary_max_tokens: 4096
    microcompact: true
    microcompact_keep_recent: 5
    microcompact_min_savings: 256
    max_consecutive_failures: 3
```

`context.enabled` is off by default so a stock config keeps the upstream
behaviour; `extensions.yaml` turns it on. Trajectories record the assembled
sections under `context` (tokens, budgets, dropped sections) plus the
compaction/microcompact counters.

## Skills

A skill is `<dir>/<name>/SKILL.md` or `<dir>/<name>.md` with optional YAML
frontmatter:

```markdown
---
name: pdf-tools
description: Work with PDF files
when_to_use: when a task involves generating or reading a PDF
aliases: [pdf]
---
Use the pdf CLI. Relative paths resolve against ${SKILL_DIR}.
```

```yaml
agent:
  skills:
    enabled: true
    dirs: ["./skills", "~/.mini-swe-agent/skills"]
    max_description_chars: 250
    max_index_chars: 20000
    max_bytes: 100000
```

The index (name, description, path) goes into the system prompt; the body is
loaded only when the model calls `Skill`. `${SKILL_DIR}`, `${ZCODE_SKILL_DIR}`,
`${SKILL_BASE_DIR}`, and `${WORKSPACE}` are expanded at load time.

## Subagents

A `Task` tool runs a nested agent with its own context, a filtered tool set, and
the same workspace. Built-ins: `general-purpose`, `explore`, `plan` (read-only
implementation plan), and `visual-judge` (pass/fail verdicts for rendered images;
needs `read_media` or `Vision`). A profile file with the same name overrides a
built-in.

Pass `run_in_background: true` to return a task id and agent id immediately. Use
`TaskOutput` for the final report and `SendMessage` to steer the running subagent.
Synchronous calls return a `<subagent_result ... agent_id="...">` block.

```markdown
---
name: reviewer
description: Reviews a diff and reports risks
tools: Bash, Read, Grep, Glob
inject_prompts: true
max_steps: 40
model: openai/another-model     # or "inherit"
model_class: litellm
model_kwargs:
  max_tokens: 16384
reasoning_effort: high
---
You are a code review subagent...
```

```yaml
agent:
  subagents:
    enabled: true
    profile_dirs: ["./agents", "~/.mini-swe-agent/agents"]
    max_depth: 1
    default_type: general-purpose
    inherit_skills: true
    step_limit: 0
    max_result_chars: 20000
    max_concurrent: 3
    parallel_task_calls: true
    # reasoning_effort: high   # default; a Task call can override it
```

**Model resolution:** `model` + `model_class` + `model_kwargs` create a new model
for the profile; `inherit` (or omitting `model`) clones the parent model and
applies the overrides.

**Reasoning effort** is resolved at call time: `Task.reasoning_effort` →
profile `reasoning_effort` → `agent.subagents.reasoning_effort` → the model's
existing `model_kwargs`. It is always a payload field, never hardcoded.

**Concurrency:** `max_concurrent` is process-wide and shared by every agent. If a
single model response contains several `Task` calls, they run in parallel up to
that limit (`parallel_task_calls`). On timeout the tool returns an error
observation instead of blocking forever.

**Tool filtering:** `tools` accepts `*`, exact names (`Read`, `Skill`,
`mcp__fs__read_file`), or globs (`mcp__fs__*`). An empty list gives the subagent
only `bash`. The `Task` tool is never inherited unless `max_depth` allows it.

Subagents submit a text report with a heredoc:

```bash
cat <<'MSWEA_SUBMIT'
COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
Answer: ...
Files: /path/to/file.py:123
MSWEA_SUBMIT
```

## MCP

```json
{
  "mcpServers": {
    "fs": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
    },
    "docs": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer ..."}
    },
    "events": {"type": "sse", "url": "https://example.com/sse"}
  }
}
```

```yaml
agent:
  mcp:
    enabled: true
    config_file: "./mcp.json"
    timeout_seconds: 60
    max_output_chars: 50000
    tool_prefix: mcp
    required: false
```

Tools are exposed as `mcp__<server>__<tool>`. Servers run once per agent in a
background event loop. A failing server is skipped with a warning recorded under
`extensions.mcp_diagnostics`; `required: true` fails fast instead. Legacy fields
are normalized: `environment` → `env`, `http_headers` → `headers`,
`enable` → `enabled`, `type: "remote"` → `"http"`.

## Plugins

A plugin is a directory with `plugin.yaml` (or `plugin.json`):

```yaml
name: my-plugin
version: 1.0.0
enabled: true
skills: [skills]
prompts: [prompts]
agents: [agents]
mcp: mcp.json
tools: [tools/my_tools.py]
automations: [automations]
```

```python
# tools/my_tools.py
from minisweagent.extensions.registry import Tool, ok

def register(registry, context):
    registry.add(Tool(
        name="hello",
        description="Say hello",
        parameters={"type": "object", "properties": {}},
        handler=lambda args: ok(f"hello from {context['plugin_name']}"),
    ))
```

```yaml
agent:
  plugins:
    enabled: true
    dirs: ["./plugins", "~/.mini-swe-agent/plugins"]
    paths: ["/explicit/plugin"]
```

The loader merges each plugin's skills/prompts/agents/MCP/automations into the
agent and imports its tool modules. Tools are registered through a proxy that
renames collisions to `<plugin>__<tool>`, so plugins cannot silently replace core
tools or each other's. A broken plugin is logged and skipped.

## Automations

Automations are YAML files with a cron expression or interval:

```yaml
name: nightly-tests
schedule: "0 3 * * *"      # 5-field cron; or interval: "6h"
enabled: true
task: "Run the test suite and summarize failures"
config:
  model:
    model_name: openai/your-model
    model_class: litellm
  agent:
    step_limit: 60
  environment:
    cwd: /workspace
```

```yaml
agent:
  automations:
    enabled: true
    dirs: ["./automations", "~/.mini-swe-agent/automations"]
```

```bash
mini-automation list
mini-automation run nightly-tests
mini-automation tick                 # run everything due now
mini-automation serve --interval 60  # long-running loop
```

State (last run, status, count) lives in
`.mini-swe-agent/automations-state.json`; each run writes a trajectory and a log
under `~/.mini-swe-agent/automations/runs/<name>/`.

## Browser (WebBridge or browser-use)

The browser tool has two interchangeable backends; the user picks one with
`agent.browser.backend`:

- **`webbridge`** — [Kimi WebBridge](https://github.com/BenWongCityuCS/kimi-webbridge-enhanced)
  drives the user's real Chrome/Edge through a local daemon (`:10086`) and an
  extension, using their cookies and login sessions. Snapshots are an
  accessibility tree with `@e` refs.
- **`browser_use`** — [browser-use](https://github.com/browser-use/browser-use)
  runs a Playwright browser (its own profile, or attach over CDP). It does not
  touch the user's real profile.
- **`auto`** — prefer WebBridge when its extension is connected; otherwise use
  browser-use.

With `fallback: true` (default), a failed call is retried on the other backend.
The tool result records the backend and the fallback reason. Actions that only
WebBridge can express (`find_tab`, `network`, `upload`) automatically fall back
when browser-use is the selected backend.

```yaml
agent:
  browser:
    enabled: true
    backend: auto              # webbridge | browser_use | auto
    fallback: true
    daemon_url: "http://127.0.0.1:10086/command"
    status_url: "http://127.0.0.1:10086/status"
    proxy_url: "http://127.0.0.1:11086/command"
    prefer_proxy: true
    session: null              # generated when omitted
    group_title: null
    max_tabs_per_session: 3
    auto_close_on_finish: true
    timeout_seconds: 120
    network_enabled: true       # false = no external browsing
    allowed_domains: []         # allowlist (subdomains included)
    blocked_domains: []         # denylist (subdomains included)
    allow_localhost: true
    browser_use:
      headless: false
      executable_path: null    # e.g. /usr/bin/google-chrome
      user_data_dir: "~/.mini-swe-agent/playwright-profile"
      cdp_url: null            # e.g. http://127.0.0.1:9222
      keep_alive: true
```

Actions: `navigate`, `find_tab`, `snapshot`, `click`, `fill`, `evaluate`,
`screenshot`, `list_tabs`, `close_tab`, `close_session`, `save_as_pdf`,
`network`, `upload`, `cdp`, plus tab-group actions. browser-use supports all of
them except `find_tab`, `network`, and `upload`.

Guard-rails against the usual WebBridge failure modes:

- **Tab budget:** a `navigate` with `new_tab` is refused when the session already
  has `max_tabs_per_session` tabs (default 3). The model gets an actionable error
  telling it to close a tab.
- **Tab-limit proxy:** when `proxy_url` is reachable, all commands go through the
  proxy, which also enforces a global tab budget across sessions.
- **Auto-close:** agent-created sessions are closed when the run finishes.
- **Session isolation:** every subagent gets a derived session name, so parallel
  subagents do not fight over one tab group.
- **Network policy:** `network_enabled: false` blocks external navigation while
  keeping `localhost`, private IPs, `file:`, `data:`, and `about:` usable.
  `allowed_domains` is an allowlist (subdomains included) and `blocked_domains`
  is a denylist. This is a navigation guard, not a network sandbox: it prevents
  the agent from browsing the open internet to look up answers, but a page that
  is already loaded or a shell command can still reach the network.

Install the browser-use extra with:

```bash
pip install "mini-swe-agent[browser-use] @ git+https://github.com/BenWongCityuCS/mini-swe-agent-v2.git@main"
```

browser-use needs Playwright and a Chrome/Chromium installation. If it cannot
find a browser, set `browser_use.executable_path` or `browser_use.cdp_url`.

## Vision

Some models have no vision or weak vision. The `Vision` tool sends an image to a
separate model and returns text; the image never enters the main context.

```yaml
agent:
  vision:
    enabled: true
    model: openai/your-vision-model
    model_kwargs:
      max_tokens: 1024
    question: "Describe this image in detail, including any visible text."
    max_output_chars: 4000
    auto_describe_screenshots: true
```

With `auto_describe_screenshots`, a Browser `screenshot` result is automatically
described and appended to the observation. The model can also call `Vision`
directly with a local path, URL, or `data:` URL.

## Mounted prompts

```yaml
agent:
  prompts:
    files: ["AGENTS.md", "docs/agent-rules.md"]
    dirs: ["prompts"]
    required: false
    tag: mounted_instructions
    header: Mounted instructions
```

Relative paths resolve against `agent.workspace` (default: environment cwd).
`files` supports globs; `dirs` loads `*.md` and `*.txt` recursively. `${WORKSPACE}`
is expanded. Content is appended verbatim (no Jinja rendering) inside
`<mounted_instructions>...</mounted_instructions>`.

## Trajectory metadata

Every run records the active extension set under `extensions`:

```json
{
  "tools": ["TodoWrite", "Read", "Edit", "Skill", "Task"],
  "skills": ["pdf-tools"],
  "subagent_profiles": ["explore", "general-purpose"],
  "mcp_servers": ["fs"],
  "plugins": ["my-plugin"],
  "plugin_tools": ["hello"],
  "browser_session": "mini-1a2b3c4d",
  "browser_backend": "webbridge",
  "vision_model": "openai/your-vision-model",
  "todos": [{"content": "read code", "status": "completed", "priority": "high"}],
  "automation_dirs": ["./automations"]
}
```

## Security notes

- MCP servers execute arbitrary local commands (`stdio`) or receive your
  credentials (`http`/`sse` headers). Treat `mcp.json` as code.
- Plugins are code: `tools/*.py` runs in-process. Only load trusted plugins.
- Subagents share the environment. Tool filtering is a safety prompt, not a
  sandbox; `bash` can do anything the container user can do.
- Mounted prompts are trusted input.
- The browser tool acts with the user's real login sessions. Tab budgets and
  auto-close reduce noise but do not make an untrusted task safe.
