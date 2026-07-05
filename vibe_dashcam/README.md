# Vibe-Dashcam

## AI Skills Are Burning Your Tokens. Keep the Receipt.

![Vibe-Dashcam catches Skill/MCP token waste and prints a local evidence receipt](assets/readme-illustrations/01-vibe-dashcam-token-receipt.png)

Vibe-Dashcam is a local desktop dashcam for AI coding agents. It watches tiny local summaries from your agent session, then turns suspicious Skill/MCP failures into a reviewable local case.

The first target is not "all AI mistakes". The first target is narrower and meaner:

> When a Skill or MCP server wastes expensive model time, catch the evidence before the context disappears.

## The Degen Rule

Do not build a giant lab before you can catch the crash.

Vibe-Dashcam keeps the rule simple:

1. A Skill/MCP signal opens a short observation window.
2. Tool-crash evidence becomes a case when the Skill/MCP itself leaves an error trail.
3. User-rebuttal evidence becomes a case when the user pushes back after that Skill/MCP was active.
4. No Skill/MCP signal, no public blame.

That keeps normal model mistakes separate from tool, Skill, and MCP failures.

## Two Evidence Triggers

Tool-crash evidence:

- The Skill/MCP reports `error`, `timeout`, `failed`, nonzero exit, exception, MCP error, or similar.
- Dashcam marks a crash-evidence candidate immediately.

User-rebuttal evidence:

- A Skill/MCP was active.
- The next user input says things like "wrong", "undo", "not like this", "不对", "重来", "撤销".
- Dashcam marks a rebuttal-evidence candidate because the tool path likely drifted.

The current build uses conservative local rules. Later model review can help summarize the case, but the trigger stays boring on purpose.

## Current Local Build

- Runs locally and listens on `http://localhost:8080/hook`.
- Ships a Tauri + React desktop HUD designed as a small bottom-right red/black board.
- Starts the local evidence core automatically from the desktop app when port 8080 is not already active.
- Watches local Codex session JSONL files as the default no-trust path.
- Accepts short hook summaries from Claude Code, Hermes, OpenCode, or other clients.
- Detects Codex Skill reads from `SKILL.md` paths.
- Detects MCP calls from `mcp__...` tool namespaces and MCP event summaries.
- Keeps only the latest 12 field-whitelisted, truncated, secret-redacted behavior summaries.
- Shows Skill/MCP clean/flagged counts, latest estimated receipt, local save, copy receipt, and pause.
- Does not ask for API keys.
- Does not read `.env`.
- Local only. Nothing leaves this machine.

## Run On Windows

PowerShell:

```powershell
cd .\desktop
npm install
npm run tauri dev
```

Development requires Node.js, Rust/Cargo, Python, and PyInstaller. The packaged Windows app bundles the local core. For a frontend-only build check:

```powershell
cd .\desktop
npm install
npm run build
```

## Hook Payload

Codex can be watched from local session logs. Other clients can POST a small JSON payload to `/hook`.

Send less, not more. Never send secrets or full source files.

```json
{
  "client": "codex",
  "event_type": "McpToolUse",
  "user_input": "optional user prompt summary",
  "ai_output": "short error or assistant summary",
  "skill_name": "optional-skill-name",
  "tool_name": "mcp__server.tool",
  "token_count": 120
}
```

Dashcam keeps only these fields: `client`, `event_type`, `user_input`, `ai_output`, `skill_name`, `tool_name`, `model`, `token_count`. Text fields are truncated and common secret patterns are replaced before display or local save.

## Package

Windows:

```powershell
cd .\desktop
npm run tauri build
```

The desktop app starts the bundled local evidence core automatically. Development mode still uses the system Python runtime for faster debugging.

## Boundary

This is a small local tool. It only creates local evidence cards for Skill/MCP activity.

---

# 中文版

## 震惊：你的 AI Skill 可能在偷偷烧 Token，先把账单拍下来

Vibe-Dashcam 是一个本地 AI 编程行车记录仪。它不做重型评测平台，不搞全仓库回放，也不假装能判断所有代码对错。它只先盯住最值钱的一层：Skill 和 MCP 有没有在真实使用里翻车。

土狗规则很简单：

1. 先看到 Skill/MCP 参与，才进入观察窗口。
2. Skill/MCP 自己报错、超时、失败，就是工具崩溃证据。
3. Skill/MCP 跑完后用户说“不对、重来、撤销”，就是用户驳斥证据。
4. 没摸到 Skill/MCP，就不乱扣锅。

当前版本就是一个本地小工具：只在本机生成候选账单，不要 API Key，不读 `.env`。
