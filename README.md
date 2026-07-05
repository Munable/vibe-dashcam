# Vibe-Dashcam

## AI Skills Are Burning Your Tokens. Keep the Receipt.

![Vibe-Dashcam catches Skill/MCP token waste and prints a local evidence receipt](vibe_dashcam/assets/readme-illustrations/01-vibe-dashcam-token-receipt.png)

Vibe-Dashcam is a local desktop dashcam for AI coding agents. It watches tiny local summaries from your agent session, then turns suspicious Skill/MCP failures into a reviewable local case.

The first target is not "all AI mistakes". The first target is narrower and meaner:

> When a Skill or MCP server wastes expensive model time, catch the evidence before the context disappears.

This is the first local brick for Vibe-Hub: a future public board for real-world Skill/MCP failure reports.

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
- Watches local Codex session JSONL files as the default no-trust path.
- Accepts short hook summaries from Claude Code, Hermes, OpenCode, or other clients.
- Detects Codex Skill reads from `SKILL.md` paths.
- Detects MCP calls from `mcp__...` tool namespaces and MCP event summaries.
- Keeps only the latest 12 field-whitelisted, truncated, secret-redacted behavior summaries.
- Shows Skill/MCP clean/crash counts, latest token receipt, local save, copy receipt, pause, and test capture.
- Does not ask for API keys.
- Does not read `.env`.
- Does not upload anything to the public internet in this build.

## Run On Windows

PowerShell:

```powershell
# Terminal 1: start the local evidence core
python .\vibe_dashcam\vibe_dashcam.py

# Terminal 2: start the desktop HUD
cd .\desktop
npm install
npm run tauri dev
```

The native desktop HUD requires Node.js and Rust/Cargo because it is built with Tauri. For a frontend-only build check:

```powershell
cd .\desktop
npm install
npm run build
```

## Hook Payload

Codex can be watched from local session logs. Other clients can POST a small JSON payload to `/hook`.

Send less, not more. Never send secrets or full source files.

An optional Codex hook example lives at `examples/codex/vibe_dashcam_hook.py`.

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

The Python core still runs as a separate local service in this build. Bundling it as a Tauri sidecar is intentionally left for the next packaging pass.

## Not Yet

This local build does not run Vibe-Hub, publish a leaderboard, read the whole repository, prove code correctness, or auto-shame a project online.

It only creates local evidence cards. Public upload should stay explicit and user-approved.

---

# 中文版

## 震惊：你的 AI Skill 可能在偷偷烧 Token，先把账单拍下来

Vibe-Dashcam 是一个本地 AI 编程行车记录仪。它不做重型评测平台，不搞全仓库回放，也不假装能判断所有代码对错。它只先盯住最值钱的一层：Skill 和 MCP 有没有在真实使用里翻车。

土狗规则很简单：

1. 先看到 Skill/MCP 参与，才进入观察窗口。
2. Skill/MCP 自己报错、超时、失败，就是工具崩溃证据。
3. Skill/MCP 跑完后用户说“不对、重来、撤销”，就是用户驳斥证据。
4. 没摸到 Skill/MCP，就不乱扣锅。

当前版本只在本地生成候选账单，不上传公网，不要 API Key，不读 `.env`。后面 Vibe-Hub 要做红黑榜，也应该基于这种真实、本地、可脱敏的证据，而不是靠营销文案吹哪个 Skill 很神。
