# Vibe-Dashcam Integration Contract

This file is for Claude Code, Codex, Hermes, OpenCode, Aider-like clients, and any Agent that wants to wire a local hook into Vibe-Dashcam.

One-line goal:

> Send tiny local summaries so Dashcam can catch Skill/MCP crash evidence and user-rebuttal evidence.

Do not turn this into a framework. Do not rewrite the model request pipeline if session logs, lifecycle hooks, plugins, or existing tool events can already provide the summary.

## Must Obey

1. POST only to `http://localhost:8080/hook`.
2. Use async send or a very short timeout.
3. Fail silently if Dashcam is not running.
4. Send summaries only. Do not send API keys, `.env`, full source files, private repo dumps, or raw tool output.
5. Dashcam does not need provider credentials. Model review, if added later, should use the user's already configured client side.

## Detection Model

Dashcam is focused on Skills and MCP servers.

The basic case model is:

1. Skill/MCP activation opens a short observation window.
2. Tool-crash evidence: the Skill/MCP itself reports an error, timeout, failed call, exception, nonzero exit, or MCP error.
3. User-rebuttal evidence: the Skill/MCP was active, then the user pushes back with "wrong", "undo", "not like this", "不对", "重来", "撤销", etc.
4. No Skill/MCP signal means no public blame candidate.

This is intentional. Generic model mistakes are real, but Vibe-Dashcam is not trying to blame Skills/MCPs when no Skill/MCP was involved.

## Recommended Events

Send the smallest useful subset:

- `UserPromptSubmit`: user input summary.
- `PostToolUse`: normal tool or Skill event summary.
- `McpToolUse`: MCP tool event summary.
- `ToolError` / `McpError`: hard failure summary.
- `Stop`: final assistant summary if it is already available.

If the client can identify a Skill, send `skill_name`.

If the client can identify MCP, send `tool_name` with an `mcp__...` style name when possible, for example `mcp__node_repl.js`.

If an error happened, put only the short error class or message in `ai_output`, such as `timeout`, `tool call failed`, `permission denied`, or `MCP error: connection refused`.

## Payload

Recommended JSON:

```json
{
  "client": "codex",
  "event_type": "McpToolUse",
  "user_input": "optional user prompt summary",
  "ai_output": "short error or assistant summary",
  "skill_name": "optional-skill-name",
  "tool_name": "mcp__server.tool",
  "model": "optional-current-model",
  "token_count": 0
}
```

Dashcam only keeps a field whitelist. Extra fields are ignored.

Whitelisted fields:

- `client`
- `event_type`
- `user_input`
- `ai_output`
- `skill_name`
- `tool_name`
- `model`
- `token_count`

## Python Standard-Library Sender

```python
import json
import threading
import urllib.request


def vibe_post(payload: dict) -> None:
    def send() -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            "http://localhost:8080/hook",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=1).read()
        except Exception:
            pass

    threading.Thread(target=send, daemon=True).start()
```

## Codex Local Path

Codex can work without a custom lifecycle hook. Dashcam watches `%USERPROFILE%\.codex\sessions` and extracts small summaries from new JSONL records.

Current local extraction:

- user input from `event_msg.user_message`
- assistant summaries from `event_msg.agent_message` and assistant messages
- token totals from `event_msg.token_count`
- Skill activation from `...skills...\<name>\SKILL.md` reads
- MCP activation from `mcp__...` namespaces or MCP tool events

Do not send Codex credentials to Dashcam.

## Other Clients

Claude Code:

- Use project-level or user-level hooks if available.
- Send `UserPromptSubmit`, tool summaries, Skill names, and MCP summaries.
- Keep errors short.

Hermes:

- Use local hook/plugin events such as `agent:start`, `agent:step`, and `agent:end`.
- Mark MCP or Skill involvement when known.

OpenCode:

- Use the local plugin system to POST user messages and tool events.
- Windows config may live in `%APPDATA%\opencode\opencode.json` or `%USERPROFILE%\.config\opencode\opencode.json`.

## Current Boundary

Dashcam does not prove the code is wrong. It creates a local candidate case when a Skill/MCP leaves crash evidence or when the user rebuts output after Skill/MCP involvement.

Public upload, leaderboards, and model-assisted verdicts are later steps and should stay user-approved.

---

# 中文简版

目标不是把 Agent 改造成大平台，只是让它把本地小摘要发给 Dashcam。

核心规则：

1. 先确认 Skill/MCP 参与。
2. Skill/MCP 自己报错、超时、失败，就是工具崩溃证据。
3. 用户在 Skill/MCP 之后说“不对、重来、撤销”，就是用户驳斥证据。
4. 没有 Skill/MCP 上下文，就不乱归因。

只发摘要，不发密钥，不发完整仓库，不发 `.env`。Dashcam 当前只在本地生成候选账单，不上传公网。
