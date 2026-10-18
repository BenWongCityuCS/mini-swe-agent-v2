# NOTICE

This repository is an unofficial fork of **mini-swe-agent**:

- Upstream: https://github.com/SWE-agent/mini-swe-agent
- Upstream version this fork is based on: **v2.4.6**
- Upstream license: MIT (see [LICENSE.md](LICENSE.md))

The conversation-compaction feature is ported from the still-unmerged pull
request **#948**:

- PR: https://github.com/SWE-agent/mini-swe-agent/pull/948
- Author: [@wasiahmad](https://github.com/wasiahmad)

The extension layer (skills, subagents, MCP, mountable prompts, read-only
tools, plugins, automations) is inspired by the design of **ZCode**:

- ZCode: https://github.com/zai-org/ZCode

No ZCode source code was copied; the Python implementation in
`src/minisweagent/extensions/` was written for this fork.

The browser tool implements the local HTTP protocol documented by
**kimi-webbridge-enhanced** (Kimi WebBridge):

- Kimi WebBridge: https://github.com/BenWongCityuCS/kimi-webbridge-enhanced

No Kimi WebBridge code is vendored; the client is a small urllib-based bridge.

This fork is maintained independently. It is not affiliated with, sponsored by,
or endorsed by the upstream maintainers, SWE-agent, ZCode, or Kimi.
