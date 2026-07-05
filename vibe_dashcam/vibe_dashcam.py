"""
Vibe‑Dashcam 客户端
======================

该脚本实现了本地 Vibe‑Dashcam core service 的核心功能：

* 在本地监听 8080 端口接收来自编程代理插件发送的用户输入。
* 保留最近一小段 AI 行为摘要。
* 在 Skill/MCP 明确报错，或用户明确否定最近结果时，生成脱敏的本地证据账单。
* 通过 JSON API 交给 Tauri/React 桌面小窗展示。

运行该脚本需要 Python 3.7 及以上，仅依赖标准库。
"""

import http.server
import json
import threading
import queue
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional
import time
import sys
import os
import platform
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

APP_NAME = "vibe-dashcam"
APP_VERSION = "0.1.0"
SOURCE_KINDS = {"codex_session", "hook", "demo"}
ALLOWED_ORIGINS = {
    "http://127.0.0.1:1420",
    "http://localhost:1420",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
}


@dataclass(frozen=True)
class FeedbackDecision:
    negative: bool
    category: str
    reason: str
    confidence: float


class FeedbackClassifier:
    """本地兜底判断器：只识别明确的用户否定信号。"""

    denial_phrases = (
        "wrong", "not right", "undo", "rollback", "start over",
        "you misunderstood", "that's not what i asked",
        "不对", "不是这样", "理解错", "全部重来", "重新来",
        "重做", "撤销", "撤回", "回滚", "别乱改", "做错了",
    )
    changed_mind_phrases = (
        "改成", "换成", "顺便", "再加", "instead", "also add",
    )

    def classify(self, text: str) -> FeedbackDecision:
        text = (text or "").strip()
        if not text:
            return FeedbackDecision(False, "none", "没有用户反馈", 0.0)

        lowered = text.lower()
        if any(phrase in lowered for phrase in self.denial_phrases):
            return FeedbackDecision(
                True,
                "ai_failed_previous_result",
                "用户明确否定最近结果或要求撤回重做",
                0.72,
            )
        if any(phrase in lowered for phrase in self.changed_mind_phrases):
            return FeedbackDecision(
                False,
                "user_changed_mind",
                "用户更像是在改变或补充需求",
                0.45,
            )
        return FeedbackDecision(False, "unknown", "未检测到明确否定信号", 0.2)

    def is_complaint(self, text: str) -> bool:
        return self.classify(text).negative


SentinelClassifier = FeedbackClassifier


class FailureSignalDetector:
    """本地硬失败检测：只认 Skill/MCP 附近的明确错误信号。"""

    hard_failure_phrases = (
        "exception", "traceback", "timeout", "timed out",
        "nonzero", "non-zero",
        "mcp error", "tool call failed", "connection refused",
        "permission denied", "错误", "失败", "超时", "异常", "报错",
    )
    hard_event_types = (
        "toolerror", "mcperror", "posttooluseerror", "error",
    )
    signal_event_types = (
        "mcptooluse", "posttooluse", "tooloutput", "functioncalloutput",
    )

    def classify(
        self,
        event: Dict[str, object],
        context: Optional[List[Dict[str, object]]] = None,
    ) -> FeedbackDecision:
        if not self.has_skill_or_mcp_context(event, context or []):
            return FeedbackDecision(False, "none", "未检测到 Skill/MCP 上下文", 0.0)

        event_type = str(event.get("event_type") or "").strip().lower()
        if event_type not in self.hard_event_types and event_type not in self.signal_event_types:
            return FeedbackDecision(False, "none", "当前事件不是工具执行信号", 0.1)

        signal_text = " ".join(
            str(event.get(field) or "")
            for field in ("event_type", "ai_output")
        ).lower()
        if event_type in self.hard_event_types or self._has_failure_signal(signal_text):
            return FeedbackDecision(
                True,
                "skill_mcp_hard_failure",
                "Skill/MCP 附近出现明确错误、超时或失败信号",
                0.82,
            )
        return FeedbackDecision(False, "none", "未检测到明确硬失败信号", 0.2)

    def _has_failure_signal(self, text: str) -> bool:
        if re.search(r"\bexit code:\s*0\b", text):
            return False
        if re.search(r"\bexit code:\s*[1-9]\d*\b", text):
            return True
        if any(phrase in text for phrase in self.hard_failure_phrases):
            return True
        return bool(re.search(
            r"\b(error|failed|failure)\b(?!\s*[\"']?\s*:\s*(null|0|false)\b)",
            text,
        ))

    def has_skill_or_mcp_context(
        self,
        event: Dict[str, object],
        context: List[Dict[str, object]],
    ) -> bool:
        candidates = [event] + list(context)
        for candidate in candidates:
            if candidate.get("skill_name"):
                return True
            signal = " ".join(
                str(candidate.get(field) or "")
                for field in ("client", "event_type", "tool_name")
            ).lower()
            if "mcp" in signal:
                return True
        return False


SAFE_EVENT_FIELDS = (
    "client",
    "source",
    "source_kind",
    "event_type",
    "user_input",
    "prompt",
    "ai_output",
    "assistant_output",
    "skill_name",
    "skill",
    "tool_name",
    "tool",
    "model",
    "token_count",
    "tokens",
)
MAX_EVENT_FIELD_CHARS = 1200


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*['\"]?[^'\"\s,}]+"),
)


def _redact_secrets(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


APP_STATE_LOCK = threading.Lock()
APP_STATE: Dict[str, object] = {
    "listening": False,
    "hook_url": "http://localhost:8080/hook",
    "server_error": None,
    "tailer_active": False,
    "tailer_error": None,
    "last_event_at": None,
    "event_count": 0,
    "ok_event_count": 0,
    "failure_count": 0,
    "case_count": 0,
    "latest_case": None,
    "source_status": {
        "codex_session": {
            "active": False,
            "path": None,
            "last_seen_at": None,
            "last_event_name": None,
        }
    },
}


def update_app_state(**changes: object) -> None:
    with APP_STATE_LOCK:
        APP_STATE.update(changes)


def get_app_state() -> Dict[str, object]:
    with APP_STATE_LOCK:
        return dict(APP_STATE)


def update_source_status(name: str, **changes: object) -> None:
    state = get_app_state()
    source_status = dict(state.get("source_status") or {})
    current = dict(source_status.get(name) or {})
    current.update(changes)
    source_status[name] = current
    update_app_state(source_status=source_status)


def mark_codex_session_seen(root: Path, event: Dict[str, object]) -> None:
    update_source_status(
        "codex_session",
        active=True,
        path=str(root),
        last_seen_at=_utc_now(),
        last_event_name=str(_event_target(event) or event.get("event_type") or "event"),
    )


CASE_HISTORY_LOCK = threading.Lock()
CASE_HISTORY: Deque[Dict[str, object]] = deque(maxlen=20)
SKILL_STATS_LOCK = threading.Lock()
SKILL_STATS: Dict[str, Dict[str, object]] = {}


def _event_target(event: Dict[str, object]) -> Optional[str]:
    skill = event.get("skill_name")
    if skill:
        return str(skill)
    tool = str(event.get("tool_name") or "")
    if "mcp" in tool.lower():
        return tool
    return None


def _update_skill_stats(target: str, failed: bool, tokens: int = 0) -> None:
    with SKILL_STATS_LOCK:
        item = SKILL_STATS.setdefault(
            target,
            {
                "target": target,
                "success": 0,
                "failure": 0,
                "tokens": 0,
                "last_seen": None,
            },
        )
        item["last_seen"] = _utc_now()
        item["tokens"] = int(item.get("tokens") or 0) + max(int(tokens or 0), 0)
        key = "failure" if failed else "success"
        item[key] = int(item.get(key) or 0) + 1


def get_skill_board(limit: int = 8) -> List[Dict[str, object]]:
    with SKILL_STATS_LOCK:
        rows = [dict(item) for item in SKILL_STATS.values()]
    return sorted(
        rows,
        key=lambda item: (
            int(item.get("failure") or 0),
            str(item.get("last_seen") or ""),
        ),
        reverse=True,
    )[:limit]


def _as_text(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    text = text.strip()
    text = _redact_secrets(text)
    return text[:MAX_EVENT_FIELD_CHARS] if text else None


def _sanitize_event(payload: Dict[str, Any]) -> Dict[str, object]:
    event: Dict[str, object] = {}

    for field in SAFE_EVENT_FIELDS:
        value = payload.get(field)
        if value is None or value == "":
            continue
        target = {
            "prompt": "user_input",
            "assistant_output": "ai_output",
            "skill": "skill_name",
            "tool": "tool_name",
            "tokens": "token_count",
            "source": "client",
        }.get(field, field)
        if target in event:
            continue
        if isinstance(value, str):
            text = _as_text(value)
            if text:
                event[target] = text
        elif isinstance(value, (int, float, bool)):
            event[target] = value

    if event and "event_type" not in event:
        event["event_type"] = "message"
    source_kind = str(event.get("source_kind") or "")
    if source_kind and source_kind not in SOURCE_KINDS:
        event.pop("source_kind", None)
    if event and "client" not in event:
        event["client"] = str(event.get("skill_name") or "unknown")
    return event


class RecentBehaviorBuffer:
    """保留最近一小段行为摘要，不保存 payload 原文。"""

    def __init__(self, limit: int = 12) -> None:
        self._events: Deque[Dict[str, object]] = deque(maxlen=limit)

    def add(self, payload: Dict[str, Any]) -> None:
        event = _sanitize_event(payload)
        if event:
            self._events.append(event)

    def snapshot(self) -> List[Dict[str, object]]:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()


class SummaryGenerator:
    """根据最近行为片段生成脱敏后的证据账单候选。"""

    def __init__(self, price_per_token: float = 0.00003) -> None:
        # 单个 token 的成本，默认按照 $0.00003 估算（约等于 0.03 美分）。
        self.price_per_token = price_per_token

    def generate(
        self,
        recent_events: List[Dict[str, object]],
        trigger_text: str = "",
        decision: Optional[FeedbackDecision] = None,
    ) -> Dict[str, object]:
        """
        根据最近行为摘要生成结构化证据账单候选。
        """
        events = recent_events or []
        clients = [str(e.get("client")) for e in events if e.get("client")]
        source_kind = "hook"
        for event in reversed(events):
            value = event.get("source_kind")
            if value:
                source_kind = str(value)
                break
        tools = []
        for event in events:
            target = _event_target(event)
            if target:
                tools.append(target)
        suspected_client = Counter(clients).most_common(1)[0][0] if clients else "unknown"
        suspected_skill = Counter(tools).most_common(1)[0][0] if tools else "unknown"
        if decision and decision.category == "skill_mcp_hard_failure":
            for event in reversed(events):
                target = _event_target(event)
                if target:
                    suspected_skill = target
                    break

        token_estimate = 0
        for event in events:
            if event.get("event_type") == "TokenCount":
                continue
            explicit_tokens = event.get("token_count")
            if isinstance(explicit_tokens, (int, float)):
                token_estimate += int(explicit_tokens)
                continue
            token_text = "".join(
                str(event.get(field) or "")
                for field in ("user_input", "ai_output", "skill_name", "tool_name")
            )
            token_estimate += max(len(token_text), 1) // 4
        token_estimate = max(token_estimate, 1)
        cost_estimate = round(token_estimate * self.price_per_token, 4)

        if decision and decision.category == "skill_mcp_hard_failure":
            summary_msg = (
                f"Skill/MCP 附近出现明确失败信号，已标记为崩溃证据候选；"
                f"疑似客户端 `{suspected_client}`，skill/tool `{suspected_skill}`，"
                f"估算浪费约 {token_estimate} 个 Token (≈${cost_estimate})."
            )
        else:
            summary_msg = (
                f"用户否定了最近 {len(events)} 条 AI 行为片段，已标记为驳斥证据候选；"
                f"疑似客户端 `{suspected_client}`，skill/tool `{suspected_skill}`，"
                f"估算浪费约 {token_estimate} 个 Token (≈${cost_estimate})."
            )
        return {
            "negative": bool(decision.negative if decision else True),
            "category": decision.category if decision else "ai_failed_previous_result",
            "reason": decision.reason if decision else "用户否定了最近结果",
            "confidence": decision.confidence if decision else 0.5,
            "suspected_client": suspected_client,
            "suspected_skill": suspected_skill,
            "source_kind": source_kind,
            "events_count": len(events),
            "wasted_tokens": token_estimate,
            "wasted_cost": cost_estimate,
            "trigger_text": trigger_text,
            "recent_events": events,
            "summary": summary_msg,
        }


# ---------------------------------------------------------------------------
# Configuration scanning utilities
# ---------------------------------------------------------------------------
# 本节函数用于扫描并解析 Codex、Claude Code、Hermes 和 OpenCode
# 的本地配置文件，从中提取当前配置的模型和提供者信息。

def _expand_path(path: str) -> Path:
    """扩展用户目录符号并返回 Path 对象。"""
    return Path(os.path.expanduser(path)).resolve()


def _parse_toml_model(path: Path) -> Optional[str]:
    """粗略解析 TOML 文件中的 model 配置。

    由于不依赖额外第三方库，这里采用简单的行匹配方式：
    查找包含 'model' 键且格式如 `model = "..."` 或 `model="..."`。
    如果找到多个，则返回第一个匹配值。

    参数:
        path (Path): toml 文件路径。

    返回:
        Optional[str]: 解析出的模型名或 None。
    """
    if not path.is_file():
        return None
    try:
        with path.open('r', encoding='utf-8') as f:
            for line in f:
                # 去除注释后的内容
                line_stripped = line.split('#')[0].strip()
                if not line_stripped.lower().startswith('model'):
                    continue
                # 支持类似 model = "foo" 或 model="foo"
                parts = line_stripped.split('=', 1)
                if len(parts) != 2:
                    continue
                value = parts[1].strip().strip('"\'')
                if value:
                    return value
    except Exception:
        return None
    return None


def _parse_json_model(path: Path, key: str = 'model') -> Optional[str]:
    """解析 JSON 配置文件中的模型字段。

    如果 JSON 顶层包含给定键 (默认为 'model')，则返回其值。
    当值为嵌套对象 (例如 {"default": "..."}) 时，优先返回其中的
    'default' 字段，否则返回整个对象的字符串表示。

    参数:
        path (Path): JSON 文件路径。
        key (str): 要查找的键，默认为 'model'。

    返回:
        Optional[str]: 模型名或 None。
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    if key not in data:
        return None
    model_val = data[key]
    if isinstance(model_val, str):
        return model_val
    if isinstance(model_val, dict):
        # 优先寻找 default
        default = model_val.get('default')
        if isinstance(default, str):
            return default
        # 其次寻找 name
        name = model_val.get('name') or model_val.get('model')
        if isinstance(name, str):
            return name
        # 返回 dict 的字符串表示，确保可打印
        return json.dumps(model_val)
    return None


def _parse_yaml_model(path: Path) -> Optional[str]:
    """解析 Hermes 的 YAML 配置文件中的模型字段。

    Hermes 的 ~/.hermes/config.yaml 在 model 字段为空字符串时，
    通过 `hermes model` 命令填充为一个对象，例如:
        model:
          provider: openai
          default: gpt-5.3-codex-spark
          base_url: https://api.openai.com/v1
          api_mode: chat

    这里采用简单的文本解析：
    查找行开头为 'model:'，然后在后续几行内寻找 'default:'。

    返回:
        Optional[str]: 默认模型名或 None。
    """
    if not path.is_file():
        return None
    try:
        with path.open('r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception:
        return None
    model_start = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('model:'):
            model_start = idx
            break
    if model_start is None:
        return None
    # 查找后续几行 (最多10行) 中的 default
    for j in range(model_start + 1, min(len(lines), model_start + 11)):
        stripped = lines[j].strip()
        if stripped.startswith('default'):
            # 格式可能为 'default: foo'
            parts = stripped.split(':', 1)
            if len(parts) == 2:
                value = parts[1].strip().strip('"\'')
                if value:
                    return value
    return None


def scan_configurations() -> Dict[str, Optional[str]]:
    """扫描 Codex、Claude Code、Hermes 和 OpenCode 配置文件以获取当前模型。

    返回:
        dict: 键为工具名称 ('codex', 'claude', 'hermes', 'opencode')，
              值为解析到的模型名称或 None。
    """
    models: Dict[str, Optional[str]] = {
        'codex': None,
        'claude': None,
        'hermes': None,
        'opencode': None,
    }
    system = platform.system()
    # Codex config path
    if system == 'Windows':
        codex_path = Path(os.environ.get('USERPROFILE', '')) / '.codex' / 'config.toml'
    else:
        codex_path = _expand_path('~/.codex/config.toml')
    models['codex'] = _parse_toml_model(codex_path)
    # Claude Code settings.json (user-level)
    if system == 'Windows':
        claude_path = Path(os.environ.get('USERPROFILE', '')) / '.claude' / 'settings.json'
    else:
        claude_path = _expand_path('~/.claude/settings.json')
    models['claude'] = _parse_json_model(claude_path, key='model')
    # Hermes config.yaml
    if system == 'Windows':
        hermes_path = Path(os.environ.get('USERPROFILE', '')) / '.hermes' / 'config.yaml'
    else:
        hermes_path = _expand_path('~/.hermes/config.yaml')
    models['hermes'] = _parse_yaml_model(hermes_path)
    # OpenCode global config
    if system == 'Windows':
        opencode_paths = [
            Path(os.environ.get('APPDATA', os.environ.get('USERPROFILE', ''))) / 'opencode' / 'opencode.json',
            Path(os.environ.get('USERPROFILE', '')) / '.config' / 'opencode' / 'opencode.json',
        ]
    elif system == 'Darwin':
        opencode_paths = [_expand_path('~/Library/Application Support/opencode/opencode.json')]
    else:
        opencode_paths = [_expand_path('~/.config/opencode/opencode.json')]
    for opencode_path in opencode_paths:
        models['opencode'] = _parse_json_model(opencode_path, key='model')
        if models['opencode']:
            break
    return models


def _codex_text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


SKILL_PATH_RE = re.compile(
    r"(?:^|[\\/])skills[\\/](?:\.system[\\/])?([^\\/]+)[\\/]SKILL\.md",
    re.IGNORECASE,
)


def _extract_skill_name_from_text(text: object) -> Optional[str]:
    candidate = _as_text(text)
    if not candidate:
        return None
    normalized = candidate.replace("\\\\", "\\")
    match = SKILL_PATH_RE.search(normalized)
    return match.group(1) if match else None


def extract_codex_session_event(record: Dict[str, Any]) -> Optional[Dict[str, object]]:
    """Convert one Codex session JSONL record into a small Dashcam event."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None

    if record.get("type") == "event_msg":
        payload_type = payload.get("type")
        if payload_type == "user_message":
            message = _as_text(payload.get("message"))
            if message:
                return {
                    "client": "codex",
                    "source_kind": "codex_session",
                    "event_type": "UserPromptSubmit",
                    "user_input": message,
                }
        if payload_type == "agent_message":
            message = _as_text(payload.get("message"))
            if message:
                return {
                    "client": "codex",
                    "source_kind": "codex_session",
                    "event_type": "AssistantMessage",
                    "ai_output": message,
                }
        if payload_type == "token_count":
            usage = payload.get("info", {}).get("last_token_usage") if isinstance(payload.get("info"), dict) else None
            total = usage.get("total_tokens") if isinstance(usage, dict) else None
            if isinstance(total, (int, float)):
                return {
                    "client": "codex",
                    "source_kind": "codex_session",
                    "event_type": "TokenCount",
                    "token_count": int(total),
                }
        if payload_type == "mcp_tool_call_end":
            invocation = payload.get("invocation")
            invocation = invocation if isinstance(invocation, dict) else {}
            server = _as_text(invocation.get("server")) or "unknown"
            tool = _as_text(invocation.get("tool")) or "unknown"
            event: Dict[str, object] = {
                "client": "codex",
                "source_kind": "codex_session",
                "event_type": "McpToolUse",
                "tool_name": f"mcp__{server}.{tool}",
            }
            error_text = _as_text(payload.get("error") or payload.get("status") or payload.get("message"))
            if error_text:
                event["ai_output"] = error_text
            return event

    if record.get("type") == "response_item":
        payload_type = payload.get("type")
        if payload_type == "function_call":
            tool_name = _as_text(payload.get("name"))
            namespace = _as_text(payload.get("namespace"))
            if tool_name:
                event = {
                    "client": "codex",
                    "source_kind": "codex_session",
                    "event_type": "PostToolUse",
                    "tool_name": f"{namespace}.{tool_name}" if namespace else tool_name,
                }
                if namespace and namespace.startswith("mcp__"):
                    event["event_type"] = "McpToolUse"
                skill_name = _extract_skill_name_from_text(payload.get("arguments"))
                if skill_name:
                    event["skill_name"] = skill_name
                return event
        if payload_type == "function_call_output":
            output = _as_text(payload.get("output") or payload.get("content"))
            if output:
                return {
                    "client": "codex",
                    "source_kind": "codex_session",
                    "event_type": "ToolOutput",
                    "ai_output": output,
                }
        if payload_type == "message" and payload.get("role") == "assistant":
            text = _as_text(_codex_text_from_content(payload.get("content")))
            if text:
                return {
                    "client": "codex",
                    "source_kind": "codex_session",
                    "event_type": "AssistantMessage",
                    "ai_output": text,
                }
    return None


class CodexSessionTailer:
    """Tail Codex local session logs as a no-trust fallback to lifecycle hooks."""

    def __init__(
        self,
        on_event: Any,
        sessions_root: Optional[Path] = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.on_event = on_event
        self.sessions_root = sessions_root or Path.home() / ".codex" / "sessions"
        self.poll_interval = poll_interval
        self._offsets: Dict[Path, int] = {}
        self._running = False

    def start(self) -> bool:
        if not self.sessions_root.is_dir():
            return False
        self._running = True
        self._prime_offsets()
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _recent_files(self) -> List[Path]:
        try:
            files = [p for p in self.sessions_root.rglob("*.jsonl") if p.is_file()]
        except Exception:
            return []
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:20]

    def _prime_offsets(self) -> None:
        for path in self._recent_files():
            try:
                self._offsets[path] = path.stat().st_size
            except Exception:
                pass

    def _run(self) -> None:
        while self._running:
            self.poll_once()
            time.sleep(self.poll_interval)

    def poll_once(self) -> None:
        for path in self._recent_files():
            try:
                size = path.stat().st_size
            except Exception:
                continue
            offset = self._offsets.get(path)
            if offset is None:
                self._offsets[path] = size
                continue
            if size < offset:
                offset = 0
            if size == offset:
                continue
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    handle.seek(offset)
                    for line in handle:
                        try:
                            record = json.loads(line)
                        except Exception:
                            continue
                        event = extract_codex_session_event(record)
                        if event:
                            mark_codex_session_seen(self.sessions_root, event)
                            self.on_event(event)
                    self._offsets[path] = handle.tell()
            except Exception:
                self._offsets[path] = size



class DashcamServer(http.server.BaseHTTPRequestHandler):
    """HTTP 服务器处理类。接受来自代理的 Hook 并触发取证逻辑。"""

    # 共享队列，用于将生成的账单传递给 UI 线程
    summary_queue: 'queue.Queue[Dict[str, object]]' = queue.Queue()
    # 存储最近一小段脱敏后的行为摘要
    recent_events = RecentBehaviorBuffer()
    classifier = FeedbackClassifier()
    failure_detector = FailureSignalDetector()
    summarizer = SummaryGenerator()
    paused = False
    persist_cases = True

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        path = urlparse(self.path).path
        return path in {"/hook", "/health"} or origin in ALLOWED_ORIGINS

    def _guard_origin(self) -> bool:
        if self._origin_allowed():
            return True
        self._send_json(403, {"ok": False, "error": "origin_forbidden"})
        return False

    def end_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        elif urlparse(self.path).path == "/hook":
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def _send_json(self, status: int, payload: Dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_payload(self) -> Dict[str, Any]:
        length = int(self.headers.get('Content-Length', 0))
        raw_data = self.rfile.read(length)
        try:
            payload = json.loads(raw_data)
            return payload if isinstance(payload, dict) else {"value": payload}
        except Exception:
            return {'user_input': raw_data.decode('utf-8', errors='ignore')}

    def _state_payload(self) -> Dict[str, object]:
        state = get_app_state()
        state["paused"] = DashcamServer.paused
        state["skill_board"] = get_skill_board()
        return state

    @classmethod
    def ingest_payload(cls, payload: Dict[str, Any]) -> None:
        update_app_state(
            last_event_at=_utc_now(),
            event_count=int(get_app_state().get("event_count") or 0) + 1,
        )
        if cls.paused:
            return
        event = _sanitize_event(payload)
        if event and not event.get("source_kind"):
            event["source_kind"] = "codex_session" if event.get("client") == "codex" else "hook"
        if event.get("source_kind") == "codex_session":
            mark_codex_session_seen(Path.home() / ".codex" / "sessions", event)
        recent_events = cls.recent_events.snapshot()
        hard_decision = cls.failure_detector.classify(event, recent_events) if event else FeedbackDecision(False, "none", "", 0.0)
        if hard_decision.negative:
            evidence_events = recent_events + ([event] if event else [])
            summary_data = cls.summarizer.generate(
                evidence_events,
                trigger_text=str(event.get("ai_output") or event.get("event_type") or ""),
                decision=hard_decision,
            )
            summary_data = record_case(summary_data)
            cls.summary_queue.put(summary_data)
            cls.recent_events.clear()
            return

        user_input = payload.get('user_input') or payload.get('prompt') or ''
        decision = cls.classifier.classify(str(user_input))
        if decision.negative:
            if recent_events and cls.failure_detector.has_skill_or_mcp_context(event, recent_events):
                summary_data = cls.summarizer.generate(
                    recent_events,
                    trigger_text=str(user_input),
                    decision=decision,
                )
                summary_data = record_case(summary_data)
                cls.summary_queue.put(summary_data)
            cls.recent_events.clear()
        else:
            cls.recent_events.add(payload)
            target = _event_target(event) if event else None
            if target:
                tokens = event.get("token_count")
                _update_skill_stats(target, failed=False, tokens=int(tokens) if isinstance(tokens, (int, float)) else 0)
                state = get_app_state()
                update_app_state(ok_event_count=int(state.get("ok_event_count") or 0) + 1)

    def do_OPTIONS(self) -> None:
        if not self._guard_origin():
            return
        self._send_json(200, {"ok": True})

    def do_GET(self) -> None:
        if not self._guard_origin():
            return
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"ok": True, "app": APP_NAME, "version": APP_VERSION})
            return
        if path == "/state":
            self._send_json(200, self._state_payload())
            return
        if path == "/cases":
            self._send_json(200, {"cases": get_cases()})
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if not self._guard_origin():
            return
        path = urlparse(self.path).path
        payload = self._read_payload()
        if path == '/hook':
            payload["source_kind"] = "hook"
            DashcamServer.ingest_payload(payload)
            self._send_json(200, {"ok": True})
            return
        if path == "/cases/save":
            case = payload.get("case") if isinstance(payload.get("case"), dict) else None
            if case is None:
                case = find_case(payload.get("case_id"))
            if case is None:
                latest = get_app_state().get("latest_case")
                case = latest if isinstance(latest, dict) else None
            if case is None:
                self._send_json(404, {"ok": False, "error": "no_case"})
                return
            saved_path = save_local_case(case)
            exists = saved_path.exists()
            size = saved_path.stat().st_size if exists else 0
            status = 200 if exists else 500
            self._send_json(status, {
                "ok": exists,
                "path": str(saved_path),
                "exists": exists,
                "bytes": size,
                "error": None if exists else "save_failed",
            })
            return
        if path == "/control/pause":
            paused = payload.get("paused")
            DashcamServer.paused = bool(paused) if isinstance(paused, bool) else not DashcamServer.paused
            self._send_json(200, {"ok": True, "state": self._state_payload()})
            return
        if path == "/test-capture":
            demo_event = {
                "client": "vibe-dashcam",
                "source_kind": "demo",
                "event_type": "McpToolUse",
                "tool_name": "mcp__demo.timeout",
                "ai_output": "timeout",
                "token_count": 321,
            }
            decision = DashcamServer.failure_detector.classify(demo_event, [])
            demo_case = DashcamServer.summarizer.generate([demo_event], "timeout", decision)
            demo_case.setdefault("source_kind", "demo")
            demo_case.setdefault("evidence_type", _evidence_type_label(demo_case))
            self._send_json(200, {"ok": True, "created": False, "demo_case": demo_case, "state": self._state_payload()})
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    def log_message(self, format: str, *args: object) -> None:
        # 覆盖默认的日志输出，防止在控制台打印请求日志
        return


def _evidence_type_label(data: Dict[str, object]) -> str:
    if data.get("category") == "skill_mcp_hard_failure":
        return "Tool-crash evidence"
    return "User-rebuttal evidence"


def _case_title(data: Dict[str, object]) -> str:
    target = str(data.get("suspected_skill") or "unknown")
    tokens = data.get("wasted_tokens", "?")
    return f"{_evidence_type_label(data)} · {target} · {tokens} tokens"


def format_evidence_receipt(data: Dict[str, object]) -> str:
    lines = [
        "Evidence Receipt",
        "",
        f"Type: {_evidence_type_label(data)}",
        f"Target: {data.get('suspected_skill', 'unknown')}",
        f"Estimated tokens: {data.get('wasted_tokens', '?')}",
        f"Estimated cost: ${data.get('wasted_cost', '?')}",
        "",
        "Summary:",
        str(data.get("summary") or "Evidence candidate captured."),
    ]

    events = data.get("recent_events")
    if isinstance(events, list) and events:
        lines.extend(["", "Recent local traces:"])
        for event in events[-6:]:
            if not isinstance(event, dict):
                continue
            target = event.get("skill_name") or event.get("tool_name") or event.get("client") or "unknown"
            snippet = event.get("ai_output") or event.get("user_input") or event.get("event_type") or ""
            lines.append(f"- {event.get('event_type', 'event')} | {target}: {str(snippet)[:180]}")
    return "\n".join(lines)


def _default_cases_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".vibe-dashcam"
    return root / "VibeDashcam" / "cases.jsonl"


def save_local_case(data: Dict[str, object], path: Optional[Path] = None) -> Path:
    target = path or _default_cases_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    case_id = data.get("id")
    if case_id and _saved_case_id_exists(target, case_id):
        return target
    record = {"saved_at": _utc_now(), "case": data}
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return target


def _saved_case_id_exists(path: Path, case_id: object) -> bool:
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                case = record.get("case") if isinstance(record, dict) else None
                if isinstance(case, dict) and case.get("id") == case_id:
                    return True
    except Exception:
        return False
    return False


def _normalize_case(data: Dict[str, object]) -> Dict[str, object]:
    case = dict(data)
    case.setdefault("id", uuid.uuid4().hex)
    case.setdefault("created_at", _utc_now())
    case.setdefault("evidence_type", _evidence_type_label(case))
    return case


def _is_demo_case(case: Dict[str, object]) -> bool:
    target = str(case.get("suspected_skill") or "")
    return case.get("source_kind") == "demo" or target.startswith("mcp__demo.")


def load_local_cases(path: Optional[Path] = None, limit: int = 20) -> List[Dict[str, object]]:
    target = path or _default_cases_path()
    if not target.exists():
        return []
    cases: List[Dict[str, object]] = []
    try:
        with target.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                case = record.get("case") if isinstance(record, dict) else None
                if isinstance(case, dict) and not _is_demo_case(case):
                    cases.append(_normalize_case(case))
    except Exception:
        return []
    return cases[-limit:]


def restore_local_cases(path: Optional[Path] = None) -> int:
    cases = load_local_cases(path)
    with CASE_HISTORY_LOCK:
        CASE_HISTORY.clear()
        CASE_HISTORY.extend(cases)
    with SKILL_STATS_LOCK:
        SKILL_STATS.clear()
    for case in cases:
        target = str(case.get("suspected_skill") or "unknown")
        tokens = int(case.get("wasted_tokens") or 0)
        _update_skill_stats(target, failed=True, tokens=tokens)
    update_app_state(
        failure_count=len(cases),
        case_count=len(cases),
        latest_case=cases[-1] if cases else None,
    )
    return len(cases)


def record_case(data: Dict[str, object]) -> Dict[str, object]:
    case = _normalize_case(data)
    target = str(case.get("suspected_skill") or "unknown")
    tokens = int(case.get("wasted_tokens") or 0)
    _update_skill_stats(target, failed=True, tokens=tokens)
    with CASE_HISTORY_LOCK:
        CASE_HISTORY.append(case)
    state = get_app_state()
    failure_count = int(state.get("failure_count") or 0) + 1
    case_count = int(state.get("case_count") or 0) + 1
    update_app_state(
        failure_count=failure_count,
        case_count=case_count,
        latest_case=case,
    )
    if DashcamServer.persist_cases:
        try:
            save_local_case(case)
        except Exception:
            pass
    return case


def get_cases(limit: int = 20) -> List[Dict[str, object]]:
    with CASE_HISTORY_LOCK:
        cases = list(CASE_HISTORY)
    return list(reversed(cases[-limit:]))


def find_case(case_id: object) -> Optional[Dict[str, object]]:
    if not case_id:
        return None
    with CASE_HISTORY_LOCK:
        for case in CASE_HISTORY:
            if case.get("id") == case_id:
                return dict(case)
    return None


def run_server() -> None:
    """在子线程中启动 HTTP 服务器。"""
    server_address = ('localhost', 8080)
    try:
        httpd = http.server.ThreadingHTTPServer(server_address, DashcamServer)
    except OSError as exc:
        update_app_state(listening=False, server_error=str(exc))
        return
    update_app_state(listening=True, server_error=None)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        update_app_state(listening=False)
        httpd.server_close()


def main() -> None:
    # 启动本地取证 API。正式桌面前端由 Tauri/React 连接这些接口。
    restore_local_cases()
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    # 监听 Codex 本地 session 日志，作为不依赖 hook 信任的接入方式。
    try:
        tailer = CodexSessionTailer(DashcamServer.ingest_payload)
        tailer_active = tailer.start()
        update_source_status("codex_session", active=tailer_active, path=str(tailer.sessions_root))
        update_app_state(tailer_active=tailer_active, tailer_error=None if tailer_active else "codex_sessions_not_found")
    except Exception as exc:
        update_app_state(tailer_active=False, tailer_error=str(exc))
    print("[Vibe-Dashcam] Core API listening on http://localhost:8080", file=sys.stderr)
    while True:
        time.sleep(3600)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f"[Vibe‑Dashcam] 程序运行异常: {exc}", file=sys.stderr)
        sys.exit(1)
