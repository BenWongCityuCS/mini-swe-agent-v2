# mini-swe-agent v2

An opinionated, unofficial rebuild of
[SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent).

## Why this exists

When mini-swe-agent v2 landed, "minimal" meant one loop, one bash tool, and any
model. That was a useful correction to the heavyweight agent frameworks of the
time. Since then, OpenCode, ZCode, Claude Code, and Codex have all grown a real
surface: plugins, skills, subagents, MCP, todo lists, scheduled automation,
browser control, image understanding, and safe editing. mini-swe-agent did not
keep up — v2.4.6 still ships a single tool, no history compaction, and no
extension point.

This fork keeps the part that made mini-swe-agent good — a small, readable agent
loop — and adds the modern surface around it. Everything is opt-in, so the bare
`mini.yaml` still behaves like upstream v2.

## What you get

**Core**

- One readable agent loop, bash, any LiteLLM-compatible model.
- **Conversation compaction** — old history is summarized into a checkpoint
  instead of dying at the context limit (upstream PR #948).
- **Sessions and resume** — every step is autosaved; `--resume <id>`,
  `--continue`, and `--list-sessions` continue a run after a crash or a restart.
- **Hooks** — shell commands around tool calls, prompts, sessions, and
  compaction. Exit code 2 blocks a tool call, so guards are enforced by the
  harness, not by the prompt.
- **Background work** — `bash` and `Task` accept `run_in_background`, with
  `TaskOutput` / `TaskStop` for polling and cancellation.
- **Agent messaging** — `SendMessage` steers a running subagent or reports back
  to the coordinator.

**Delegation**

- **Subagents** via a `Task` tool: Markdown profiles, isolated message history,
  filtered tools, configurable depth. Built-ins: `general-purpose`, `explore`,
  `plan`, and `visual-judge`; sync or background execution.
- **Model switching** per profile: `model: openai/...`, `model_class`, and
  `model_kwargs`. Omit it / use `inherit` to reuse the parent model.
- **Reasoning effort from the payload** — `reasoning_effort` is resolved from the
  Task call, then the profile, then the model config. Nothing is hardcoded.
- **Global concurrency control** — `max_concurrent: 3` across every agent in the
  process, plus parallel execution when one response contains several Task calls.

**Modern tools**

- **TodoWrite / TodoRead** — a persistent task list (`.mini-swe-agent/todos.json`).
- **Read / Grep / Glob** — structured file inspection. `Read` can attach images
  and videos as native model input (`read_media`), not just text.
- **SessionRead** — inspect a previous session transcript from inside a run.
- **Write / Edit with read-before-edit** — a file must be read in the current
  session and must not have changed on disk before it can be modified.
- **Skill** — `SKILL.md` discovery, a compact index in the prompt, and on-demand
  loading of the full instructions.
- **Vision** — send an image or screenshot to a separate vision model and receive
  text, so the main model never needs to be multimodal.
- **Browser** — drive the user's real Chrome/Edge through
  [Kimi WebBridge](https://github.com/BenWongCityuCS/kimi-webbridge-enhanced),
  with a per-session tab budget, automatic session close, and screenshot
  auto-description through the Vision model.

**Integration**

- **MCP** — stdio, streamable HTTP, and SSE servers exposed as
  `mcp__<server>__<tool>`.
- **Plugins** — one manifest can contribute tools, skills, prompts, agent
  profiles, MCP servers, and automations. Tools are namespaced on collision, so
  plugins cannot silently replace core tools.
- **Automations** — cron/interval YAML tasks with a `mini-automation` CLI
  (`list`, `run`, `tick`, `serve`).
- **Mountable prompts** — `AGENTS.md`-style files and directories appended to the
  system prompt, with globs and `${WORKSPACE}` expansion.
- **Workflows** — a durable multi-phase `expert` workflow with a scheduled DAG,
  typed `submit_result` / `escalate`, artifacts, journal, and resume. Definitions
  are YAML/JSON and can be authored with `WorkflowCreate`; runs are started with
  `WorkflowRun` or the `mini-workflow` CLI.

## Context management (ZCode-style)

Context is assembled from **sections**, not one growing system string:

- Every section has a token budget, priority, cache hint, and target. Skills and
  subagent profiles are injected as a **meta-user** prefix on the first user
  message (stable system prompt, cache-friendly); mounted prompts and the base
  prompt stay in the system message.
- Over-budget sections use a compact form (skills/profiles fall back to a
  names-only list) before truncation. `max_total_tokens` is a hard cap; the base
  prompt always survives.
- **Ephemeral system reminders** carry the todo list, the active compaction
  checkpoint, and a microcompact notice on every model call. They are not added
  to the stored conversation, so they do not accumulate.
- **Compaction subsystem:**
  1. microcompact clears old tool results (keeps the most recent N);
  2. full compaction summarizes older groups with a ZCode-style
     analysis + summary prompt;
  3. a circuit breaker stops retrying after repeated summary failures;
  4. a minimal summary template is used when the detailed prompt does not fit a
     small model's input budget.

```yaml
agent:
  context:
    enabled: true
    max_total_tokens: 40000
    meta_user: true
    system_reminders: true
    section_budgets: {skills: 20000, subagents: 20000, prompts: 20000, todo: 2000, compaction: 8000}
  compaction:
    enabled: true
    auto: true
    context_limit: 299200
    microcompact: true
    microcompact_keep_recent: 5
    max_consecutive_failures: 3
```

Trajectories record every section's tokens, budget, and drop/compact decision
under `context`, plus the microcompact and summary counters.

## Quick start

```bash
pip install "mini-swe-agent[extensions] @ git+https://github.com/BenWongCityuCS/mini-swe-agent-v2.git@main"
```

Use the extensions preset on top of the stock config:

```bash
mini -c mini.yaml -c extensions.yaml --task "Fix the failing test"
```

`extensions.yaml` enables TodoWrite, Edit/Write, skills, subagents, workflows,
plugins, and automations. Browser and vision are configured but disabled by
default.

For the ZCode-style prompt, layer `zcode.yaml` as well:

```bash
mini -c mini.yaml -c extensions.yaml -c zcode.yaml --task "Fix the failing test"
```

Minimal config for a self-hosted OpenAI-compatible endpoint:

```yaml
model:
  model_name: openai/your-model
  model_class: litellm
  model_kwargs:
    temperature: 1.0
    max_tokens: 32768
    reasoning_effort: high   # payload field, not an agent default
```

## Subagents

```markdown
---
name: explore
description: Read-only search agent for broad fan-out searches
tools: Read, Grep, Glob
inject_prompts: false
model: inherit
reasoning_effort: high
---
You are a read-only exploration agent...
```

```yaml
agent:
  subagents:
    enabled: true
    max_concurrent: 3
    max_depth: 1
```

A Task call can override the thinking budget for that invocation:

```json
{"subagent_type": "explore", "prompt": "...", "reasoning_effort": "max"}
```

## Plugins

```
plugins/my-plugin/
  plugin.yaml
  skills/
  prompts/
  agents/
  tools/my_tools.py
  mcp.json
  automations/
```

```yaml
name: my-plugin
version: 1.0.0
skills: [skills]
prompts: [prompts]
agents: [agents]
tools: [tools/my_tools.py]
mcp: mcp.json
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

## Browser (WebBridge or browser-use)

The browser tool has two backends and the user picks one:

- **`webbridge`** (default) — [Kimi WebBridge](https://github.com/BenWongCityuCS/kimi-webbridge-enhanced) drives the user's real Chrome/Edge
  with their cookies and logins. It prefers the tab-limit proxy on `:11086`,
  enforces a per-session tab budget, and closes agent-created sessions.
- **`browser_use`** — Playwright-based automation through the
  [browser-use](https://github.com/browser-use/browser-use) package. It launches
  its own browser (or attaches over CDP) and does not touch the user's profile.
- **`auto`** — prefer WebBridge when its extension is connected, otherwise
  browser-use.

`fallback: true` (default) retries the other backend for a call when the chosen
one fails. Errors are reported in the tool result, so the model knows a fallback
happened. Actions WebBridge supports but browser-use does not (`find_tab`,
`network`, `upload`) automatically fall back.

`network_enabled: false` is the anti-cheating switch: it blocks external
navigation while keeping `localhost`, private IPs, `file:`, `data:`, and `about:`
usable. Use `allowed_domains` for a tight allowlist or `blocked_domains` for a
denylist. This is a navigation guard, not a network sandbox — a page already
loaded or a shell command can still reach the network.

```yaml
agent:
  browser:
    enabled: true
    backend: auto          # webbridge | browser_use | auto
    fallback: true
    proxy_url: "http://127.0.0.1:11086/command"
    max_tabs_per_session: 3
    auto_close_on_finish: true
    # Anti-cheating / offline switch: block external browsing, keep local pages.
    network_enabled: true
    allowed_domains: []
    blocked_domains: []
    allow_localhost: true
    browser_use:
      headless: false
      user_data_dir: "~/.mini-swe-agent/playwright-profile"
      # cdp_url: "http://127.0.0.1:9222"   # attach to an existing Chrome instead
  vision:
    enabled: true
    model: openai/your-vision-model
    auto_describe_screenshots: true
```

Install the browser-use extra with:

```bash
pip install "mini-swe-agent[browser-use] @ git+https://github.com/BenWongCityuCS/mini-swe-agent-v2.git@main"
```

browser-use requires Playwright and a Chrome/Chromium installation. Use
`executable_path` or `cdp_url` if it cannot find one.

## Automations

```yaml
# automations/nightly-tests.yaml
name: nightly-tests
schedule: "0 3 * * *"      # or interval: "6h"
task: "Run the test suite and summarize failures"
config:
  model:
    model_name: openai/your-model
  agent:
    step_limit: 60
```

```bash
mini-automation list
mini-automation run nightly-tests
mini-automation serve --interval 60
```

## Design principles

1. **Opt-in.** The stock config keeps the upstream tool surface and behaviour.
2. **Small core, extension layer.** `agents/default.py` stays readable; features
   live in `extensions/`.
3. **Payload-configured models.** Thinking effort, token budgets, and model
   choice are data, not code. No provider-specific default is baked in.
4. **Composable.** Plugins, skills, prompts, MCP servers, and agent profiles are
   discovered from directories and can be layered.
5. **Defensive tools.** A failed tool returns an error observation; it never
   crashes the agent loop. A bad plugin is skipped, not fatal.

## Differences from upstream v2.4.6

| Area | Upstream v2.4.6 | This fork |
| --- | --- | --- |
| Context limit | Agent exits | Auto-compaction + overflow recovery |
| Tools | bash only | bash + Todo/Read/Grep/Glob/Write/Edit/Skill/Task/Vision/Browser/plugin tools |
| Subagents | None | Profiles, model switching, reasoning override, global concurrency |
| Extensibility | YAML only | Plugins + mountable prompts + skills |
| MCP | None | stdio / HTTP / SSE |
| Automation | None | cron/interval runner |
| Browser | None | Kimi WebBridge with tab guard |
| Images | Whatever the model can see | Optional separate vision model |
| Workflows | None | Expert phases + scheduled DAG + typed results + resume |

## Attribution and license

- Based on [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) v2.4.6 (MIT).
- Conversation compaction is ported from upstream PR
  [#948](https://github.com/SWE-agent/mini-swe-agent/pull/948) by
  [@wasiahmad](https://github.com/wasiahmad).
- The extension design (skills, subagents, MCP naming, mountable prompts) is
  inspired by [ZCode](https://github.com/zai-org/ZCode).
- The workflow runtime and its actor/phase/node/planner prompts are ported from
  ZCode's open-source dynamic-workflow implementation (Apache-2.0). See
  [NOTICE.md](NOTICE.md).
- The browser protocol follows
  [Kimi WebBridge](https://github.com/BenWongCityuCS/kimi-webbridge-enhanced).
- This project is not affiliated with or endorsed by the upstream maintainers.

MIT licensed. See [LICENSE.md](LICENSE.md) and [NOTICE.md](NOTICE.md).
