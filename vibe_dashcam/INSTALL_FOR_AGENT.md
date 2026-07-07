# Vibe-Dashcam Integration Contract

This file is for Codex agents that want to wire a local hook into Vibe-Dashcam.

One-line goal:

> Send tiny local Codex summaries so Dashcam can catch Skill/MCP crash evidence and user-rebuttal evidence.

Do not turn this into a framework. Do not rewrite the model request pipeline if Codex session logs, lifecycle hooks, plugins, or existing tool events can already provide the summary.

## Must Obey

1. POST only to `http://localhost:8080/hook`.
2. Read `hook_token` from the local Dashcam config and send it as `X-Vibe-Dashcam-Token`.
3. Use async send or a very short timeout.
4. Fail silently if Dashcam is not running or the token is not present.
5. Do not start Dashcam, Codex, a background service, or any Windows startup item from the hook.
6. Send summaries only. Do not send API keys, `.env`, full source files, private repo dumps, or raw tool output.
7. Dashcam does not need Codex credentials. Case review runs locally through the user's existing Codex CLI when available.

## Detection Model

Dashcam is focused on Skills and MCP servers.

The basic case model is:

1. Skill/MCP activation opens a short observation window.
2. Tool-crash evidence: the Skill/MCP itself reports an error, timeout, failed call, exception, nonzero exit, or MCP error. Dashcam may show this immediately, then let the local Codex review read the short surrounding context and revise the one-line verdict.
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
  "source_kind": "hook",
  "event_type": "McpToolUse",
  "user_input": "optional user prompt summary",
  "ai_output": "short error or assistant summary",
  "skill_name": "optional-skill-name",
  "tool_name": "mcp__server.tool",
  "call_id": "optional-tool-call-id",
  "model": "optional-current-model",
  "token_count": 0
}
```

Dashcam only keeps a field whitelist. Extra fields are ignored.
Text fields are truncated and common secret patterns are replaced before display or local save.

Whitelisted fields:

- `client`
- `source_kind`
- `event_type`
- `user_input`
- `ai_output`
- `skill_name`
- `skill_evidence`
- `tool_name`
- `call_id`
- `model`
- `token_count`

## Local Codex Review

Dashcam first captures a real flagged case with local rules. Only after that it may run one local review:

```powershell
codex exec --ephemeral --sandbox read-only --output-schema <schema> -
```

If the user selects a review model in the HUD, Dashcam passes it to Codex:

```powershell
codex exec --model <model> --ephemeral --sandbox read-only --output-schema <schema> -
```

The model picker should prefer local Codex config/profile values, with the user's default Codex model as the default. Do not make users configure a separate provider or API key.

The review prompt contains only a redacted case summary, the suspected Skill/MCP target, estimated tokens, and a few truncated trace lines. It does not read `.env`, does not ask for API keys, and does not upload to any Vibe cloud.

## Python Standard-Library Sender

```python
import json
import os
import threading
import urllib.request
from pathlib import Path


def vibe_token() -> str | None:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".vibe-dashcam"
    try:
        data = json.loads((root / "VibeDashcam" / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    token = data.get("hook_token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token else None


def vibe_post(payload: dict) -> None:
    def send() -> None:
        token = vibe_token()
        if not token:
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            "http://localhost:8080/hook",
            data=body,
            headers={"Content-Type": "application/json", "X-Vibe-Dashcam-Token": token},
        )
        try:
            urllib.request.urlopen(req, timeout=1).read()
        except Exception:
            pass

    threading.Thread(target=send, daemon=True).start()
```

## Codex Local Path

Codex can work without a custom lifecycle hook. Dashcam watches the current user's home directory at `~\.codex\sessions` and extracts small summaries from new JSONL records.
If you still want a hook script, use `examples/codex/vibe_dashcam_hook.py` as an example; do not commit your own `.codex` runtime state.

Product boundary: users open Dashcam when they want recording. The hook is an optional signal path only; it must not auto-launch the desktop app. Opening before use means Dashcam records from that point forward. If Dashcam was not open, no recording is promised.

Current local extraction:

- user input from `event_msg.user_message`
- assistant summaries from `event_msg.agent_message` and assistant messages
- token totals from `event_msg.token_count`
- Skill evidence from `...skills...\<name>\SKILL.md` reads
- MCP activation from `mcp__...` namespaces or MCP tool events

Do not send Codex credentials to Dashcam.

## Current Boundary

Dashcam does not prove the code is wrong. It creates a local candidate case when a Skill/MCP leaves crash evidence or when the user rebuts output after Skill/MCP involvement.

Dashcam stays local. The hook should only send tiny summaries to localhost and never act as an auto-start mechanism.

---

# 中文简版

目标不是把 Codex 改造成大平台，只是让它把本地小摘要发给已经打开的 Dashcam。

核心规则：

1. 先确认 Skill/MCP 参与。
2. Skill/MCP 自己报错、超时、失败，就是工具崩溃证据；先显示，再让本机 Codex 读短上下文复核一句话结论。
3. 用户在 Skill/MCP 之后说“不对、重来、撤销”，就是用户驳斥证据。
4. 没有 Skill/MCP 上下文，就不乱归因。

Hook 只发摘要，不负责启动 Dashcam，不写开机自启，不改系统配置。用户打开 Dashcam 后开始记录；没打开就不承诺记录。只发摘要，不发密钥，不发完整仓库，不发 `.env`。复核模型默认用用户自己的 Codex 配置，也可以从本机 Codex 配置/profile 里选，本质上只是调用 `codex exec --model <model>`。Dashcam 当前只在本机生成候选账单。
