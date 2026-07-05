"""
Vibe‑Dashcam 客户端
======================

该脚本实现了本地 Vibe‑Dashcam 客户端的核心功能：

* 在本地监听 8080 端口接收来自编程代理插件发送的用户输入。
* 保留最近一小段 AI 行为摘要。
* 在 Skill/MCP 明确报错，或用户明确否定最近结果时，生成脱敏的负反馈候选。
* 通过桌面弹窗显示候选片段，并让用户本地确认。

运行该脚本需要 Python 3.7 及以上，标准库中仅依赖 `tkinter`、`http.server` 和 `threading` 等模块。
"""

import http.server
import json
import threading
import queue
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional
import time
import sys
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path

# 尝试导入系统托盘库和绘图库
try:
    import pystray  # type: ignore
    from pystray import Menu, MenuItem  # type: ignore
    from PIL import Image, ImageDraw  # type: ignore
    _PYSTRAY_AVAILABLE = True
except Exception:
    # pystray 或 Pillow 未安装；托盘功能将被禁用
    _PYSTRAY_AVAILABLE = False


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
        "error", "exception", "traceback", "timeout", "timed out",
        "failed", "failure", "nonzero", "non-zero", "exit code",
        "mcp error", "tool call failed", "connection refused",
        "permission denied", "错误", "失败", "超时", "异常", "报错",
    )
    hard_event_types = (
        "toolerror", "mcperror", "posttooluseerror", "error",
    )

    def classify(
        self,
        event: Dict[str, object],
        context: Optional[List[Dict[str, object]]] = None,
    ) -> FeedbackDecision:
        if not self.has_skill_or_mcp_context(event, context or []):
            return FeedbackDecision(False, "none", "未检测到 Skill/MCP 上下文", 0.0)

        event_type = str(event.get("event_type") or "").strip().lower()
        signal_text = " ".join(
            str(event.get(field) or "")
            for field in ("event_type", "ai_output", "tool_name", "skill_name")
        ).lower()
        if event_type in self.hard_event_types or any(
            phrase in signal_text for phrase in self.hard_failure_phrases
        ):
            return FeedbackDecision(
                True,
                "skill_mcp_hard_failure",
                "Skill/MCP 附近出现明确错误、超时或失败信号",
                0.82,
            )
        return FeedbackDecision(False, "none", "未检测到明确硬失败信号", 0.2)

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
}


def update_app_state(**changes: object) -> None:
    with APP_STATE_LOCK:
        APP_STATE.update(changes)


def get_app_state() -> Dict[str, object]:
    with APP_STATE_LOCK:
        return dict(APP_STATE)


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
    """根据最近行为片段生成脱敏后的负反馈候选。"""

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
        根据最近行为摘要生成结构化负反馈候选。
        """
        events = recent_events or []
        clients = [str(e.get("client")) for e in events if e.get("client")]
        tools = [
            str(e.get("skill_name") or e.get("tool_name"))
            for e in events
            if e.get("skill_name") or e.get("tool_name")
        ]
        suspected_client = Counter(clients).most_common(1)[0][0] if clients else "unknown"
        suspected_skill = Counter(tools).most_common(1)[0][0] if tools else "unknown"

        token_estimate = 0
        for event in events:
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
                    "event_type": "UserPromptSubmit",
                    "user_input": message,
                }
        if payload_type == "agent_message":
            message = _as_text(payload.get("message"))
            if message:
                return {
                    "client": "codex",
                    "event_type": "AssistantMessage",
                    "ai_output": message,
                }
        if payload_type == "token_count":
            usage = payload.get("info", {}).get("last_token_usage") if isinstance(payload.get("info"), dict) else None
            total = usage.get("total_tokens") if isinstance(usage, dict) else None
            if isinstance(total, (int, float)):
                return {
                    "client": "codex",
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
                    "event_type": "ToolOutput",
                    "ai_output": output,
                }
        if payload_type == "message" and payload.get("role") == "assistant":
            text = _as_text(_codex_text_from_content(payload.get("content")))
            if text:
                return {
                    "client": "codex",
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
        self.sessions_root = sessions_root or Path(os.environ.get("USERPROFILE", "")) / ".codex" / "sessions"
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

    @classmethod
    def ingest_payload(cls, payload: Dict[str, Any]) -> None:
        update_app_state(
            last_event_at=_utc_now(),
            event_count=int(get_app_state().get("event_count") or 0) + 1,
        )
        if cls.paused:
            return
        event = _sanitize_event(payload)
        recent_events = cls.recent_events.snapshot()
        hard_decision = cls.failure_detector.classify(event, recent_events) if event else FeedbackDecision(False, "none", "", 0.0)
        if hard_decision.negative:
            evidence_events = recent_events + ([event] if event else [])
            summary_data = cls.summarizer.generate(
                evidence_events,
                trigger_text=str(event.get("ai_output") or event.get("event_type") or ""),
                decision=hard_decision,
            )
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
                cls.summary_queue.put(summary_data)
            cls.recent_events.clear()
        else:
            cls.recent_events.add(payload)

    def do_POST(self) -> None:
        # 仅处理 /hook 路径
        if self.path != '/hook':
            self.send_error(404, 'Not Found')
            return
        length = int(self.headers.get('Content-Length', 0))
        raw_data = self.rfile.read(length)
        try:
            payload = json.loads(raw_data)
        except Exception:
            # 如果不是 JSON，则当作纯文本处理
            payload = {'user_input': raw_data.decode('utf-8', errors='ignore')}

        DashcamServer.ingest_payload(payload)

        # 回复 200 OK
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')

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
    record = {"saved_at": _utc_now(), "case": data}
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return target


class DashcamUI:
    """图形界面类，负责展示负反馈候选并提供确认操作。"""

    def __init__(self, queue_ref: 'queue.Queue[Dict[str, object]]') -> None:
        self.queue = queue_ref
        self.root = tk.Tk()
        self.root.title("Vibe‑Dashcam")
        self.root.geometry("620x480")
        self.root.minsize(560, 420)
        # 使用原生 ttk 主题提升美观度
        style = ttk.Style(self.root)
        # 尝试选择较现代的主题，如果不存在则忽略
        for candidate in ("clam", "default", "alt"):
            try:
                style.theme_use(candidate)
                break
            except Exception:
                continue
        # 设置全局字体
        default_font = ("Helvetica", 11)
        self.root.option_add("*Font", default_font)
        self.latest_summary: Optional[Dict[str, object]] = None
        self.case_history: List[Dict[str, object]] = []
        self.case_count = 0
        self.status_var = tk.StringVar(value="Listening on localhost:8080")
        self.case_count_var = tk.StringVar(value="0 local cases")
        self._build_main_panel()
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)
        # 定时检查队列是否有新的账单
        self.root.after(500, self._poll_queue)
        # 如果支持托盘，初始化托盘图标
        if _PYSTRAY_AVAILABLE:
            self._init_tray_icon()

        # 扫描配置文件，准备可用模型列表
        try:
            self.available_models = scan_configurations()
        except Exception:
            self.available_models = {}
        # 当前选择的模型，初始为 None，稍后在弹窗中设置
        self.selected_model: Optional[str] = None

    def _build_main_panel(self) -> None:
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(frame)
        header.pack(fill=tk.X)
        ttk.Label(header, text="Vibe-Dashcam", font=("Helvetica", 18, "bold")).pack(side=tk.LEFT)
        ttk.Label(header, text="LOCAL ONLY", foreground="#0a7f35").pack(side=tk.RIGHT)
        ttk.Label(
            frame,
            text="Local evidence recorder for Skill/MCP token waste.",
            foreground="#555555",
        ).pack(anchor="w", pady=(2, 18))

        status = ttk.LabelFrame(frame, text="Status", padding=12)
        status.pack(fill=tk.X)
        ttk.Label(status, textvariable=self.status_var).pack(anchor="w")
        ttk.Label(status, text="Hook: http://localhost:8080/hook", foreground="#555555").pack(anchor="w", pady=(4, 0))
        self.source_var = tk.StringVar(value="Sources: starting")
        self.last_event_var = tk.StringVar(value="Last event: none")
        ttk.Label(status, textvariable=self.source_var, foreground="#555555").pack(anchor="w", pady=(4, 0))
        ttk.Label(status, textvariable=self.last_event_var, foreground="#555555").pack(anchor="w", pady=(4, 0))
        ttk.Label(status, textvariable=self.case_count_var, foreground="#555555").pack(anchor="w", pady=(4, 0))

        recent = ttk.LabelFrame(frame, text="Evidence Cases", padding=12)
        recent.pack(fill=tk.BOTH, expand=True, pady=(14, 0))
        self.case_list = tk.Listbox(recent, height=6, activestyle="none")
        self.case_list.pack(fill=tk.BOTH, expand=True)
        self.case_list.insert(tk.END, "No evidence case yet. Dashcam is watching locally.")
        self.case_list.bind("<Double-Button-1>", lambda _event: self._show_selected_summary())

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(14, 0))
        ttk.Button(buttons, text="Open Case", command=self._show_selected_summary).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Copy Hook URL", command=self._copy_hook_url).pack(side=tk.LEFT, padx=(8, 0))
        self.pause_button = ttk.Button(buttons, text="Pause", command=self._toggle_pause)
        self.pause_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Test Capture", command=self._test_capture).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Hide", command=self.root.withdraw).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Quit", command=self._quit_app).pack(side=tk.RIGHT)

    def _init_tray_icon(self) -> None:
        """初始化系统托盘图标及菜单。"""
        # 生成简单的红色圆形警示图标
        def _create_icon() -> Image.Image:
            size = (64, 64)
            image = Image.new("RGBA", size, (255, 255, 255, 0))
            draw = ImageDraw.Draw(image)
            # 绘制红色圆底
            draw.ellipse((0, 0, size[0], size[1]), fill=(220, 0, 0, 255))
            # 画白色感叹号
            draw.rectangle((28, 15, 36, 40), fill=(255, 255, 255, 255))
            draw.rectangle((28, 48, 36, 54), fill=(255, 255, 255, 255))
            return image

        icon_image = _create_icon()
        menu = Menu(
            MenuItem(
                "显示窗口",
                lambda: self._show_root(),
                default=True
            ),
            MenuItem(
                "最新账单",
                lambda: self.root.after(0, self._show_latest_summary)
            ),
            MenuItem(
                "暂停/继续",
                lambda: self.root.after(0, self._toggle_pause)
            ),
            MenuItem(
                "退出",
                lambda: self._quit_app()
            ),
        )
        # 创建 pystray 图标
        self.tray_icon = pystray.Icon(
            name="Vibe‑Dashcam",
            icon=icon_image,
            title="Vibe‑Dashcam",
            menu=menu
        )
        # 在后台线程启动托盘
        def _run_tray():
            try:
                self.tray_icon.run()
            except Exception:
                pass

        tray_thread = threading.Thread(target=_run_tray, daemon=True)
        tray_thread.start()

    def _show_root(self) -> None:
        """显示主窗口。"""
        try:
            self.root.after(0, self.root.deiconify)
            self.root.after(0, self.root.lift)
        except Exception:
            pass

    def _quit_app(self) -> None:
        """退出程序。"""
        # 停止托盘并退出应用
        try:
            if _PYSTRAY_AVAILABLE and hasattr(self, 'tray_icon'):
                self.tray_icon.stop()
        except Exception:
            pass
        # 退出 tkinter 主循环
        self.root.quit()
        # 终止进程
        os._exit(0)  # type: ignore
    def _poll_queue(self) -> None:
        try:
            summary_data = self.queue.get_nowait()
        except queue.Empty:
            # 没有新数据
            pass
        else:
            self._remember_summary(summary_data)
            self._show_summary_window(summary_data)
        finally:
            self._refresh_status()
            self.root.after(500, self._poll_queue)

    def _refresh_status(self) -> None:
        state = get_app_state()
        if DashcamServer.paused:
            self.status_var.set("Paused")
        elif state.get("server_error"):
            self.status_var.set(f"Port error: {state.get('server_error')}")
        elif state.get("listening"):
            self.status_var.set("Listening on localhost:8080")
        else:
            self.status_var.set("Starting listener")
        source = "Sources: local hook"
        if state.get("tailer_active"):
            source += " + Codex session logs"
        elif state.get("tailer_error"):
            source += f" (Codex: {state.get('tailer_error')})"
        self.source_var.set(source)
        self.last_event_var.set(
            f"Events: {state.get('event_count', 0)} · Last event: {state.get('last_event_at') or 'none'}"
        )

    def _remember_summary(self, data: Dict[str, object]) -> None:
        self.latest_summary = data
        self.case_history.append(data)
        self.case_count += 1
        self.case_count_var.set(f"{self.case_count} local case{'s' if self.case_count != 1 else ''}")
        self.case_list.delete(0, tk.END)
        for item in reversed(self.case_history[-12:]):
            self.case_list.insert(tk.END, _case_title(item))

    def _show_latest_summary(self) -> None:
        if self.latest_summary:
            self._show_summary_window(self.latest_summary)
        else:
            messagebox.showinfo("Vibe-Dashcam", "No evidence case yet.")

    def _show_selected_summary(self) -> None:
        selection = self.case_list.curselection()
        if not selection:
            self._show_latest_summary()
            return
        index = selection[0]
        cases = list(reversed(self.case_history[-12:]))
        if 0 <= index < len(cases):
            self._show_summary_window(cases[index])
        else:
            self._show_latest_summary()

    def _copy_hook_url(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append("http://localhost:8080/hook")
        messagebox.showinfo("Copied", "Hook URL copied.")

    def _copy_text(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        messagebox.showinfo("Copied", "Receipt copied.")

    def _toggle_pause(self) -> None:
        DashcamServer.paused = not DashcamServer.paused
        self.pause_button.configure(text="Resume" if DashcamServer.paused else "Pause")
        self._refresh_status()

    def _test_capture(self) -> None:
        if DashcamServer.paused:
            messagebox.showinfo("Paused", "Resume listening before testing capture.")
            return
        DashcamServer.ingest_payload({
            "client": "vibe-dashcam",
            "event_type": "McpToolUse",
            "tool_name": "mcp__demo.timeout",
            "ai_output": "timeout",
            "token_count": 321,
        })

    def _show_summary_window(self, data: Dict[str, object]) -> None:
        # 创建一个顶层窗口显示账单信息
        window = tk.Toplevel(self.root)
        window.title("Vibe‑Dashcam Evidence Receipt")
        window.geometry("560x360")
        window.minsize(520, 320)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Evidence Receipt", font=("Helvetica", 16, "bold")).pack(anchor="w")
        meta = (
            f"Target: {data.get('suspected_skill', 'unknown')}   "
            f"Tokens: {data.get('wasted_tokens', '?')}   "
            f"Cost: ${data.get('wasted_cost', '?')}"
        )
        ttk.Label(frame, text=meta, foreground="#555555").pack(anchor="w", pady=(2, 12))

        receipt = tk.Text(frame, height=9, wrap=tk.WORD)
        receipt.pack(fill=tk.BOTH, expand=True)
        receipt_text = format_evidence_receipt(data)
        receipt.insert("1.0", receipt_text)
        receipt.configure(state=tk.DISABLED)

        # 确认按钮
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, anchor='w', pady=(12, 0))
        upload_button = ttk.Button(
            btn_frame,
            text="Confirm Local Case",
            command=lambda: self._confirm_negative(window, data)
        )
        upload_button.pack(side=tk.LEFT, padx=(0, 8))
        copy_button = ttk.Button(
            btn_frame,
            text="Copy Receipt",
            command=lambda: self._copy_text(receipt_text)
        )
        copy_button.pack(side=tk.LEFT, padx=(0, 8))
        # 关闭按钮
        close_button = ttk.Button(
            btn_frame,
            text="Close",
            command=window.destroy
        )
        close_button.pack(side=tk.LEFT)
        # 显示窗口
        window.transient(self.root)
        window.lift()

    def _confirm_negative(self, window: tk.Toplevel, data: Dict[str, object]) -> None:
        """确认本地负反馈候选。"""
        path = save_local_case(data)
        messagebox.showinfo("Saved", f"Saved locally:\n{path}\n\nThis build does not upload.")
        window.destroy()

    def run(self) -> None:
        self.root.mainloop()


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
    # 启动服务器线程
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    # 监听 Codex 本地 session 日志，作为不依赖 hook 信任的接入方式。
    try:
        tailer_active = CodexSessionTailer(DashcamServer.ingest_payload).start()
        update_app_state(tailer_active=tailer_active, tailer_error=None if tailer_active else "Codex sessions not found")
    except Exception as exc:
        update_app_state(tailer_active=False, tailer_error=str(exc))
    # 创建并运行 UI
    ui = DashcamUI(DashcamServer.summary_queue)
    ui.run()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f"[Vibe‑Dashcam] 程序运行异常: {exc}", file=sys.stderr)
        sys.exit(1)
