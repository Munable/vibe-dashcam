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
import shutil
import subprocess
import tempfile
import threading
import queue
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional
import time
import sys
import os
import re
import uuid
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

APP_NAME = "vibe-dashcam"
APP_VERSION = "0.1.0"
SOURCE_KINDS = {"codex_session", "hook", "demo"}
DEFAULT_REVIEW_MODEL = "default"
PREFERRED_REVIEW_MODEL = "gpt-5.3-codex-spark"
DEFAULT_STATS_SCOPE = "today"
STATS_SCOPES = {"today", "all"}
REVIEW_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
CODEX_MODEL_CATALOG_TTL_SECONDS = 300
RECENT_CASE_LIMIT = 50
MAX_SAVED_CASES = 200
MAX_REQUEST_BYTES = 64 * 1024
HOOK_TOKEN_HEADER = "X-Vibe-Dashcam-Token"
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
        "you misunderstood", "that's not what i asked", "this is not what i wanted",
        "the implementation is wrong", "the result is wrong",
        "不对", "不是这样", "理解错", "全部重来", "重新来",
        "重做", "撤销", "撤回", "回滚", "别乱改", "做错了",
        "方向不对", "做偏了", "偏了", "效果不对", "开发不对",
        "实现不对", "这段不对", "不是我要的", "不符合",
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
    explicit_failure_phrases = (
        "tool call failed",
        "traceback",
        "mcp error",
        "permission denied",
        "connection refused",
        "exception",
        "timed out",
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
        if event_type in self.hard_event_types or self._has_failure_signal(signal_text, event_type):
            return FeedbackDecision(
                True,
                "skill_mcp_hard_failure",
                "Skill/MCP 附近出现明确错误、超时或失败信号",
                0.82,
            )
        return FeedbackDecision(False, "none", "未检测到明确硬失败信号", 0.2)

    def _has_failure_signal(self, text: str, event_type: str) -> bool:
        if re.search(r"\bexit code:\s*0\b", text):
            return any(phrase in text for phrase in self.explicit_failure_phrases)
        if re.search(r"\bexit code:\s*[1-9]\d*\b", text):
            return True
        if event_type in {"tooloutput", "functioncalloutput"}:
            return any(phrase in text for phrase in self.explicit_failure_phrases)
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
    "skill_source",
    "tool_name",
    "tool",
    "call_id",
    "observed_at",
    "timestamp",
    "status",
    "exit_code",
    "skill_evidence",
    "model",
    "token_count",
    "tokens",
)
MAX_EVENT_FIELD_CHARS = 1200


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{8,}"),
    re.compile(r"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/\-]+=*"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*['\"]?[^'\"\s,}]+"),
    re.compile(r"(?i)://[^:/\s]+:[^@\s]+@"),
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
    "saved_case_count": 0,
    "stats_scope": DEFAULT_STATS_SCOPE,
    "display_ok_event_count": 0,
    "display_failure_count": 0,
    "review_model": DEFAULT_REVIEW_MODEL,
    "review_default_model": DEFAULT_REVIEW_MODEL,
    "review_model_options": [DEFAULT_REVIEW_MODEL],
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


def normalize_review_model(value: object) -> str:
    model = str(value or "").strip()
    if not model or model.lower() == DEFAULT_REVIEW_MODEL:
        return DEFAULT_REVIEW_MODEL
    return model if REVIEW_MODEL_RE.match(model) else DEFAULT_REVIEW_MODEL


def normalize_stats_scope(value: object) -> str:
    scope = str(value or "").strip().lower()
    return scope if scope in STATS_SCOPES else DEFAULT_STATS_SCOPE


def _case_date(value: object) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().date()
    except Exception:
        return None


def _case_in_scope(case: Dict[str, object], scope: str) -> bool:
    if normalize_stats_scope(scope) == "all":
        return True
    return _case_date(case.get("created_at")) == datetime.now().astimezone().date()


def _subprocess_no_window_kwargs() -> Dict[str, object]:
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


MODEL_CATALOG_CACHE_LOCK = threading.Lock()
MODEL_CATALOG_CACHE: Dict[str, object] = {"loaded_at": 0.0, "models": []}


def _append_review_model(models: List[str], value: object) -> None:
    model = normalize_review_model(value)
    if model != DEFAULT_REVIEW_MODEL and model not in models:
        models.append(model)


def _models_from_codex_catalog(data: object) -> List[str]:
    if not isinstance(data, dict):
        return []
    raw_models = data.get("models")
    if not isinstance(raw_models, list):
        return []
    models: List[str] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        visibility = str(item.get("visibility") or "list").lower()
        if visibility in {"hide", "hidden"}:
            continue
        _append_review_model(models, item.get("slug") or item.get("id") or item.get("model"))
    return models


def _codex_catalog_review_models(runner: Optional[Any] = None) -> List[str]:
    if runner is None:
        with MODEL_CATALOG_CACHE_LOCK:
            loaded_at = float(MODEL_CATALOG_CACHE.get("loaded_at") or 0.0)
            cached = list(MODEL_CATALOG_CACHE.get("models") or [])
            if cached and (time.time() - loaded_at) < CODEX_MODEL_CATALOG_TTL_SECONDS:
                return cached

    executable = "codex" if runner is not None else shutil.which("codex")
    if not executable:
        return []
    try:
        result = (runner or subprocess.run)(
            [executable, "debug", "models"],
            capture_output=True,
            text=True,
            timeout=8,
            **({} if runner is not None else _subprocess_no_window_kwargs()),
        )
    except Exception:
        return []
    if getattr(result, "returncode", 1) != 0:
        return []
    try:
        models = _models_from_codex_catalog(json.loads(str(getattr(result, "stdout", "") or "")))
    except Exception:
        models = []
    if runner is None and models:
        with MODEL_CATALOG_CACHE_LOCK:
            MODEL_CATALOG_CACHE["loaded_at"] = time.time()
            MODEL_CATALOG_CACHE["models"] = list(models)
    return models


def discover_codex_review_models(
    codex_home: Optional[Path] = None,
    selected: object = None,
    runner: Optional[Any] = None,
) -> List[str]:
    root = codex_home or (Path.home() / ".codex")
    models = [DEFAULT_REVIEW_MODEL]
    for model in _codex_catalog_review_models(runner=runner):
        _append_review_model(models, model)
    _append_review_model(models, PREFERRED_REVIEW_MODEL)
    candidates = [root / "config.toml"]
    if root.exists():
        candidates.extend(sorted(root.glob("*.config.toml")))
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for match in re.finditer(r'(?m)^\s*model\s*=\s*["\']([^"\']+)["\']', text):
            _append_review_model(models, match.group(1))
        in_availability = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_availability = stripped == "[tui.model_availability_nux]"
                continue
            if not in_availability:
                continue
            match = re.match(r'["\']?([A-Za-z0-9._:/-]{1,80})["\']?\s*=', stripped)
            if not match:
                continue
            _append_review_model(models, match.group(1))
    _append_review_model(models, selected)
    return models


def default_codex_review_model(codex_home: Optional[Path] = None, runner: Optional[Any] = None) -> str:
    for model in discover_codex_review_models(codex_home, runner=runner):
        if model != DEFAULT_REVIEW_MODEL:
            return model
    return DEFAULT_REVIEW_MODEL


def build_diagnostics(codex_home: Optional[Path] = None) -> Dict[str, object]:
    root = codex_home or (Path.home() / ".codex")
    sessions_root = root / "sessions"
    config_path = _default_config_path()
    cases_path = _default_cases_path()
    state = get_app_state()
    source_status = dict(state.get("source_status") or {})
    codex_status = dict(source_status.get("codex_session") or {})
    selected_model = normalize_review_model(state.get("review_model"))
    codex_cli = shutil.which("codex")
    return {
        "ok": True,
        "codex_cli": {"found": bool(codex_cli), "path": codex_cli},
        "codex_home": {"exists": root.exists(), "path": str(root)},
        "codex_sessions": {
            "exists": sessions_root.exists(),
            "path": str(sessions_root),
            "active": bool(codex_status.get("active")),
            "last_seen_at": codex_status.get("last_seen_at"),
        },
        "local_config": {"exists": config_path.exists(), "path": str(config_path)},
        "local_history": {"exists": cases_path.exists(), "path": str(cases_path)},
        "review_model": selected_model,
        "review_model_options": discover_codex_review_models(root, selected=selected_model),
    }


def mark_codex_session_seen(root: Path, event: Dict[str, object]) -> None:
    update_source_status(
        "codex_session",
        active=True,
        path=str(root),
        last_seen_at=_utc_now(),
        last_event_name=str(_event_target(event) or event.get("event_type") or "event"),
    )


CASE_HISTORY_LOCK = threading.Lock()
CASE_HISTORY: Deque[Dict[str, object]] = deque(maxlen=RECENT_CASE_LIMIT)
SKILL_STATS_LOCK = threading.Lock()
SKILL_STATS: Dict[str, Dict[str, object]] = {}
CASE_REVIEW_CACHE_LOCK = threading.Lock()
CASE_REVIEW_CACHE: Dict[str, Dict[str, object]] = {}


def _path_parts(value: object) -> List[str]:
    text = str(value or "").replace("\\", "/")
    return [part for part in text.split("/") if part]


def _looks_like_version(value: str) -> bool:
    return bool(re.match(r"^\d+(?:[._-]\d+)*", value))


def _skill_identity(skill_name: str, skill_source: object = None) -> Dict[str, str]:
    skill = skill_name or "unknown"
    parts = _path_parts(skill_source)
    lowered = [part.lower() for part in parts]
    plugin = ""
    if "skills" in lowered:
        skills_index = lowered.index("skills")
        if skills_index + 1 < len(lowered) and lowered[skills_index + 1] == ".system":
            plugin = "system"
        elif skills_index >= 2 and _looks_like_version(parts[skills_index - 1]):
            plugin = parts[skills_index - 2]
    display = f"{plugin}:{skill}" if plugin else skill
    return {"plugin": plugin, "skill": skill, "display": display}


def _skill_display_name(skill_name: str, skill_source: object = None) -> str:
    return _skill_identity(skill_name, skill_source)["display"]


def _event_target(event: Dict[str, object]) -> Optional[str]:
    skill = event.get("skill_name")
    if skill:
        return _skill_display_name(str(skill), event.get("skill_source"))
    tool = str(event.get("tool_name") or "")
    if "mcp" in tool.lower():
        return tool
    return None


def _matching_context_for_event(
    event: Optional[Dict[str, object]],
    recent_events: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    if not event:
        return recent_events
    call_id = event.get("call_id")
    if not call_id:
        return recent_events
    event_type = str(event.get("event_type") or "").lower()
    if event_type not in {"tooloutput", "functioncalloutput"}:
        return recent_events
    matched = [
        candidate
        for candidate in recent_events
        if candidate.get("call_id") == call_id
    ]
    return matched


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


def get_skill_board(limit: int = 8, scope: str = DEFAULT_STATS_SCOPE) -> List[Dict[str, object]]:
    scope = normalize_stats_scope(scope)
    persist_success = globals().get("DashcamServer") is not None and getattr(DashcamServer, "persist_success_stats", True)
    rows_by_target = load_success_stats(scope=scope) if persist_success else {}
    with SKILL_STATS_LOCK:
        for item in SKILL_STATS.values():
            target = str(item.get("target") or "")
            if not target or target in rows_by_target:
                continue
            rows_by_target[target] = dict(item)
    with CASE_HISTORY_LOCK:
        cases = [dict(case) for case in CASE_HISTORY if _case_in_scope(case, scope)]
    for case in cases:
        target = str(case.get("source_target") or case.get("suspected_skill") or "unknown")
        row = rows_by_target.setdefault(
            target,
            {"target": target, "success": 0, "failure": 0, "tokens": 0, "last_seen": None},
        )
        row["failure"] = int(row.get("failure") or 0) + 1
        row["tokens"] = int(row.get("tokens") or 0) + int(case.get("wasted_tokens") or 0)
        created_at = str(case.get("created_at") or "")
        if created_at > str(row.get("last_seen") or ""):
            row["last_seen"] = created_at
    rows = list(rows_by_target.values())
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
SKILL_SOURCE_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:[\\/]|[\\/])[^\"'\n\r]*?[\\/]skills[\\/](?:\.system[\\/])?(?P<name>[^\\/]+)[\\/]SKILL\.md)",
    re.IGNORECASE,
)


def _extract_skill_source_from_text(text: object) -> Optional[Dict[str, str]]:
    candidate = _as_text(text)
    if not candidate:
        return None
    normalized = candidate.replace("\\\\", "\\")
    source_match = SKILL_SOURCE_RE.search(normalized)
    if source_match:
        return {
            "skill_name": source_match.group("name"),
            "skill_source": source_match.group("path"),
        }
    name_match = SKILL_PATH_RE.search(normalized)
    if name_match:
        return {
            "skill_name": name_match.group(1),
            "skill_source": normalized[:MAX_EVENT_FIELD_CHARS],
        }
    return None


def _extract_skill_name_from_text(text: object) -> Optional[str]:
    source = _extract_skill_source_from_text(text)
    return source.get("skill_name") if source else None


def _skill_read_command(arguments: object) -> Optional[str]:
    text = _as_text(arguments)
    if not text:
        return None
    command = text
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        command = _as_text(parsed.get("command") or parsed.get("cmd") or parsed.get("script")) or text
    normalized = command.strip().lower()
    if "skill.md" not in normalized:
        return None
    if re.search(r"(?:^|[;&|]\s*|\s)(rg|select-string|findstr|git|pytest|python|npm|tauri)\b", normalized):
        return None
    if not re.search(r"(?:^|[;&|]\s*|\s)(get-content|gc|type)\b", normalized):
        return None
    return command


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
            call_id = _as_text(payload.get("call_id") or payload.get("id") or invocation.get("call_id"))
            if call_id:
                event["call_id"] = call_id
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
                call_id = _as_text(payload.get("call_id") or payload.get("id"))
                if call_id:
                    event["call_id"] = call_id
                if namespace and namespace.startswith("mcp__"):
                    event["event_type"] = "McpToolUse"
                skill_command = _skill_read_command(payload.get("arguments"))
                if skill_command:
                    skill_source = _extract_skill_source_from_text(skill_command)
                    if skill_source:
                        event["skill_name"] = skill_source["skill_name"]
                        event["skill_source"] = skill_source["skill_source"]
                        event["skill_evidence"] = "skill_file_read"
                return event
        if payload_type == "function_call_output":
            output = _as_text(payload.get("output") or payload.get("content"))
            if output:
                event = {
                    "client": "codex",
                    "source_kind": "codex_session",
                    "event_type": "ToolOutput",
                    "ai_output": output,
                }
                call_id = _as_text(payload.get("call_id") or payload.get("id"))
                if call_id:
                    event["call_id"] = call_id
                return event
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



CASE_REVIEW_TIMEOUT_SECONDS = 45
SOFT_FEEDBACK_TIMEOUT_SECONDS = 30
CASE_REVIEW_SCHEMA: Dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["tool_fault", "model_fault", "unclear"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "attributed_target": {"type": "string"},
        "attributed_call_id": {"type": "string"},
        "evidence_spans": {
            "type": "array",
            "items": {"type": "integer"},
        },
        "one_sentence_summary": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "confidence", "attributed_target", "attributed_call_id", "evidence_spans", "one_sentence_summary", "reason"],
}
SOFT_FEEDBACK_SCHEMA: Dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "negative": {"type": "boolean"},
        "category": {"type": "string", "enum": ["ai_failed_previous_result", "user_changed_mind", "unclear"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["negative", "category", "confidence", "reason"],
}


def _review_text(value: object, limit: int = 360) -> str:
    text = _as_text(value) or ""
    return text[:limit]


def build_case_review_payload(case: Dict[str, object]) -> Dict[str, object]:
    traces: List[Dict[str, object]] = []
    recent = case.get("recent_events")
    if isinstance(recent, list):
        window = recent[-6:]
        base_index = len(recent) - len(window)
        for offset, item in enumerate(window):
            if not isinstance(item, dict):
                continue
            safe: Dict[str, object] = {"event_index": base_index + offset}
            for field in (
                "observed_at",
                "timestamp",
                "source_kind",
                "event_type",
                "tool_name",
                "skill_name",
                "skill_source",
                "skill_evidence",
                "call_id",
                "model",
                "token_count",
                "status",
                "exit_code",
            ):
                if item.get(field) is not None:
                    safe[field] = item[field]
            snippet = item.get("ai_output") or item.get("user_input")
            if snippet:
                safe["snippet"] = _review_text(snippet, 280)
                exit_match = re.search(r"\bexit code:\s*(-?\d+)\b", str(snippet), re.IGNORECASE)
                if exit_match and "exit_code" not in safe:
                    safe["exit_code"] = int(exit_match.group(1))
            if safe:
                traces.append(safe)

    return {
        "case_id": _review_text(case.get("id"), 120),
        "source_kind": _review_text(case.get("source_kind"), 60),
        "evidence_type": _review_text(case.get("evidence_type") or case.get("category"), 120),
        "suspected_target": _review_text(case.get("source_target") or case.get("suspected_skill"), 180),
        "source_type": _review_text(case.get("source_type"), 80),
        "source_detail": _review_text(case.get("source_detail"), 260),
        "source_call_id": _review_text(case.get("source_call_id"), 120),
        "trigger_text": _review_text(case.get("trigger_text"), 500),
        "estimated_tokens": int(case.get("wasted_tokens") or 0),
        "trace": traces,
    }


def build_case_review_prompt(case: Dict[str, object]) -> str:
    payload = build_case_review_payload(case)
    return (
        "You are reviewing one local Vibe-Dashcam evidence candidate.\n"
        "Do not use tools. Do not ask questions. Do not reveal or infer secrets.\n"
        "Decide whether the evidence points to the Skill/MCP/tool, the base model, or is unclear.\n"
        "Attribute only to a target/call_id present in the trace. If the trace does not support a specific target or call_id, use empty strings and verdict=unclear.\n"
        "evidence_spans must list the event_index values that support your verdict.\n"
        "The one_sentence_summary is user-facing: name the failed step in one short sentence.\n"
        "Avoid project names, file paths, variable names, raw user work, and any business details.\n"
        "Use generic wording such as install step, MCP call, Skill setup, tool execution, or model reasoning.\n"
        "Return only JSON matching the provided schema.\n\n"
        f"Case summary:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
    )


def build_soft_feedback_prompt(user_input: str, recent_events: List[Dict[str, object]]) -> str:
    traces: List[Dict[str, object]] = []
    window = recent_events[-6:]
    base_index = len(recent_events) - len(window)
    for offset, item in enumerate(window):
        safe: Dict[str, object] = {"event_index": base_index + offset}
        for field in ("observed_at", "timestamp", "source_kind", "event_type", "tool_name", "skill_name", "skill_source", "call_id", "model", "status", "exit_code"):
            if item.get(field) is not None:
                safe[field] = item[field]
        snippet = item.get("ai_output") or item.get("user_input")
        if snippet:
            safe["snippet"] = _review_text(snippet, 220)
            exit_match = re.search(r"\bexit code:\s*(-?\d+)\b", str(snippet), re.IGNORECASE)
            if exit_match and "exit_code" not in safe:
                safe["exit_code"] = int(exit_match.group(1))
        if safe:
            traces.append(safe)
    payload = {
        "user_input": _review_text(user_input, 500),
        "nearby_skill_mcp_trace": traces,
    }
    return (
        "You are Vibe-Dashcam's lightweight soft-failure classifier.\n"
        "Decide whether the latest user input is correcting, rejecting, undoing, or complaining about the immediately previous work result.\n"
        "The user does not need to name a Skill, MCP, tool, plugin, file, or call id.\n"
        "Broad critiques like 'this development is wrong', 'the effect is wrong', 'the direction is off', '不对', '效果不对', or '这段开发不对' count as negative when the trace shows Skill/MCP involvement nearby.\n"
        "Return negative=true when the user clearly rejects the previous result and the trace contains nearby Skill/MCP participation.\n"
        "Return negative=false when the user is merely giving a new task, adding a normal follow-up, or changing requirements without blaming the previous result.\n"
        "Return negative=false when the user changed their mind or the trace cannot connect the input to nearby Skill/MCP-assisted work.\n"
        "Do not decide final blame here; later review can still say tool_fault, model_fault, or unclear.\n"
        "Use only the provided redacted summary. Do not use tools. Return only JSON matching the schema.\n\n"
        f"Input:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
    )


def _parse_soft_feedback_json(text: str) -> FeedbackDecision:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("soft feedback output is not an object")
    category = str(data.get("category") or "unclear")
    if category not in {"ai_failed_previous_result", "user_changed_mind", "unclear"}:
        category = "unclear"
    try:
        confidence = float(data.get("confidence") or 0)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))
    negative = bool(data.get("negative")) and category == "ai_failed_previous_result" and confidence >= 0.55
    return FeedbackDecision(
        negative=negative,
        category="ai_failed_previous_result" if negative else category,
        reason=_review_text(data.get("reason"), 220),
        confidence=confidence,
    )


class CodexFeedbackClassifier:
    """Uses local Codex once to judge semantic user rebuttals near Skill/MCP context."""

    def __init__(
        self,
        runner: Optional[Any] = None,
        timeout_seconds: int = SOFT_FEEDBACK_TIMEOUT_SECONDS,
        review_model: str = DEFAULT_REVIEW_MODEL,
    ) -> None:
        self.runner = runner or subprocess.run
        self.timeout_seconds = timeout_seconds
        self.review_model = normalize_review_model(review_model)
        self._custom_runner = runner is not None

    def set_model(self, value: object) -> str:
        self.review_model = normalize_review_model(value)
        return self.review_model

    def classify(self, user_input: str, recent_events: List[Dict[str, object]]) -> FeedbackDecision:
        executable = "codex" if self._custom_runner else shutil.which("codex")
        if not executable or not user_input.strip():
            return FeedbackDecision(False, "unknown", "Codex 语义判断不可用", 0.0)

        with tempfile.TemporaryDirectory(prefix="vibe-dashcam-soft-") as temp_dir:
            temp_root = Path(temp_dir)
            schema_path = temp_root / "schema.json"
            output_path = temp_root / "soft-feedback.json"
            schema_path.write_text(json.dumps(SOFT_FEEDBACK_SCHEMA), encoding="utf-8")
            args = [
                executable,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            if self.review_model != DEFAULT_REVIEW_MODEL:
                args[-1:-1] = ["--model", self.review_model]
            try:
                result = self.runner(
                    args,
                    input=build_soft_feedback_prompt(user_input, recent_events),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    cwd=str(temp_root),
                    **({} if self._custom_runner else _subprocess_no_window_kwargs()),
                )
            except Exception:
                return FeedbackDecision(False, "unknown", "Codex 语义判断失败", 0.0)
            if getattr(result, "returncode", 1) != 0:
                return FeedbackDecision(False, "unknown", "Codex 语义判断失败", 0.0)
            try:
                output_text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
                if not output_text:
                    output_text = str(getattr(result, "stdout", "") or "").strip()
                return _parse_soft_feedback_json(output_text)
            except Exception:
                return FeedbackDecision(False, "unknown", "Codex 语义判断解析失败", 0.0)


def _parse_review_json(text: str) -> Dict[str, object]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("review output is not an object")
    verdict = str(data.get("verdict") or "unclear")
    if verdict not in {"tool_fault", "model_fault", "unclear"}:
        verdict = "unclear"
    try:
        confidence = float(data.get("confidence") or 0)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))
    return {
        "status": "reviewed",
        "verdict": verdict,
        "confidence": confidence,
        "attributed_target": _review_text(data.get("attributed_target"), 180),
        "attributed_call_id": _review_text(data.get("attributed_call_id"), 120),
        "evidence_spans": [
            int(value) for value in data.get("evidence_spans", [])
            if isinstance(value, (int, float))
        ][:8] if isinstance(data.get("evidence_spans"), list) else [],
        "one_sentence_summary": _review_text(data.get("one_sentence_summary"), 240),
        "reason": _review_text(data.get("reason"), 500),
        "reviewed_at": _utc_now(),
        "reviewer": "codex_exec",
    }


class CaseReviewer:
    """Runs one short local Codex review for a flagged real case."""

    def __init__(
        self,
        runner: Optional[Any] = None,
        timeout_seconds: int = CASE_REVIEW_TIMEOUT_SECONDS,
        review_model: str = DEFAULT_REVIEW_MODEL,
    ) -> None:
        self.runner = runner or subprocess.run
        self.timeout_seconds = timeout_seconds
        self.review_model = normalize_review_model(review_model)
        self._custom_runner = runner is not None

    def set_model(self, value: object) -> str:
        self.review_model = normalize_review_model(value)
        return self.review_model

    def review(self, case: Dict[str, object]) -> Dict[str, object]:
        executable = "codex" if self._custom_runner else shutil.which("codex")
        if not executable:
            return self._unavailable("codex_cli_missing")

        prompt = build_case_review_prompt(case)
        with tempfile.TemporaryDirectory(prefix="vibe-dashcam-review-") as temp_dir:
            temp_root = Path(temp_dir)
            schema_path = temp_root / "schema.json"
            output_path = temp_root / "review.json"
            schema_path.write_text(json.dumps(CASE_REVIEW_SCHEMA), encoding="utf-8")
            args = [
                executable,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            if self.review_model != DEFAULT_REVIEW_MODEL:
                args[-1:-1] = ["--model", self.review_model]
            try:
                result = self.runner(
                    args,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    cwd=str(temp_root),
                    **({} if self._custom_runner else _subprocess_no_window_kwargs()),
                )
            except subprocess.TimeoutExpired:
                return self._unavailable("codex_review_timeout")
            except Exception:
                return self._unavailable("codex_review_failed")

            if getattr(result, "returncode", 1) != 0:
                return self._unavailable("codex_review_failed")
            try:
                output_text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
                if not output_text:
                    output_text = str(getattr(result, "stdout", "") or "").strip()
                review = _parse_review_json(output_text)
                review["model"] = self.review_model
                return review
            except Exception:
                return self._unavailable("codex_review_parse_failed")

    def _unavailable(self, reason: str) -> Dict[str, object]:
        return {
            "status": "unavailable",
            "reason": reason,
            "reviewed_at": _utc_now(),
            "reviewer": "codex_exec",
            "model": self.review_model,
        }


class DashcamServer(http.server.BaseHTTPRequestHandler):
    """HTTP 服务器处理类。接受来自代理的 Hook 并触发取证逻辑。"""

    # 共享队列，用于将生成的账单传递给 UI 线程
    summary_queue: 'queue.Queue[Dict[str, object]]' = queue.Queue()
    # 存储最近一小段脱敏后的行为摘要
    recent_events = RecentBehaviorBuffer()
    classifier = FeedbackClassifier()
    failure_detector = FailureSignalDetector()
    summarizer = SummaryGenerator()
    case_reviewer = CaseReviewer()
    semantic_classifier = CodexFeedbackClassifier()
    review_cases = True
    paused = False
    persist_cases = True
    persist_success_stats = True
    hook_token: Optional[str] = None

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        path = urlparse(self.path).path
        if not origin:
            return path in {"/hook", "/health"}
        return path != "/hook" and origin in ALLOWED_ORIGINS

    def _guard_origin(self) -> bool:
        if self._origin_allowed():
            return True
        self._send_json(403, {"ok": False, "error": "origin_forbidden"})
        return False

    def _guard_hook_token(self) -> bool:
        expected = DashcamServer.hook_token or load_or_create_hook_token()
        DashcamServer.hook_token = expected
        supplied = self.headers.get(HOOK_TOKEN_HEADER, "")
        if secrets.compare_digest(supplied, expected):
            return True
        self._send_json(401, {"ok": False, "error": "hook_token_required"})
        return False

    def end_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", f"Content-Type, {HOOK_TOKEN_HEADER}")
        super().end_headers()

    def _send_json(self, status: int, payload: Dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_payload(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            raise ValueError("bad_content_length")
        if length < 0:
            raise ValueError("bad_content_length")
        if length > MAX_REQUEST_BYTES:
            self.rfile.read(min(length, MAX_REQUEST_BYTES + 4096))
            raise OverflowError("payload_too_large")
        raw_data = self.rfile.read(length)
        try:
            payload = json.loads(raw_data)
            return payload if isinstance(payload, dict) else {"value": payload}
        except Exception:
            return {'user_input': raw_data.decode('utf-8', errors='ignore')}

    def _state_payload(self) -> Dict[str, object]:
        state = get_app_state()
        scope = normalize_stats_scope(state.get("stats_scope"))
        scoped_cases = _cases_for_scope(scope)
        success_rows = load_success_stats(scope=scope) if DashcamServer.persist_success_stats else {}
        state["paused"] = DashcamServer.paused
        state["stats_scope"] = scope
        state["display_ok_event_count"] = sum(int(row.get("success") or 0) for row in success_rows.values())
        state["display_failure_count"] = len(scoped_cases)
        state["skill_board"] = get_skill_board(scope=scope)
        state["review_default_model"] = default_codex_review_model()
        state["review_model_options"] = discover_codex_review_models(selected=state.get("review_model"))
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
        if event and not event.get("observed_at"):
            event["observed_at"] = _utc_now()
        if event.get("source_kind") == "codex_session":
            mark_codex_session_seen(Path.home() / ".codex" / "sessions", event)
        recent_events = cls.recent_events.snapshot()
        decision_context = _matching_context_for_event(event, recent_events)
        hard_decision = cls.failure_detector.classify(event, decision_context) if event else FeedbackDecision(False, "none", "", 0.0)
        if hard_decision.negative:
            evidence_events = decision_context + ([event] if event else [])
            summary_data = cls.summarizer.generate(
                evidence_events,
                trigger_text=str(event.get("ai_output") or event.get("event_type") or ""),
                decision=hard_decision,
            )
            summary_data = record_case(summary_data)
            cls.summary_queue.put(summary_data)
            cls.schedule_case_review(summary_data)
            cls.recent_events.clear()
            return

        raw_user_input = event.get("user_input") if event else _as_text(payload.get("user_input") or payload.get("prompt") or "")
        user_input = str(raw_user_input or "")
        local_decision = cls.classifier.classify(str(user_input))
        decision = local_decision
        has_tool_context = bool(recent_events and cls.failure_detector.has_skill_or_mcp_context(event, recent_events))
        if user_input and has_tool_context:
            decision = cls.semantic_classifier.classify(user_input, recent_events)
            if not decision.negative and decision.category == "unknown" and local_decision.negative:
                decision = local_decision
        if decision.negative:
            if has_tool_context:
                summary_data = cls.summarizer.generate(
                    recent_events,
                    trigger_text=str(user_input),
                    decision=decision,
                )
                summary_data = record_case(summary_data)
                cls.summary_queue.put(summary_data)
                cls.schedule_case_review(summary_data)
            cls.recent_events.clear()
        else:
            cls.recent_events.add(event)
            target = _event_target(event) if event else None
            if target:
                tokens = event.get("token_count")
                token_count = int(tokens) if isinstance(tokens, (int, float)) else 0
                if cls.persist_success_stats:
                    record_success_stat(target, token_count)
                else:
                    _update_skill_stats(target, failed=False, tokens=token_count)
                state = get_app_state()
                update_app_state(ok_event_count=int(state.get("ok_event_count") or 0) + 1)

    @classmethod
    def schedule_case_review(cls, case: Dict[str, object]) -> None:
        if not cls.review_cases or _is_demo_case(case):
            return
        case_id = case.get("id")
        if not case_id:
            return
        thread = threading.Thread(
            target=update_case_review,
            args=(case_id, cls.case_reviewer),
            daemon=True,
        )
        thread.start()

    @classmethod
    def set_review_model(cls, value: object, persist: bool = True) -> str:
        model = cls.case_reviewer.set_model(value)
        cls.semantic_classifier.set_model(model)
        update_app_state(
            review_model=model,
            review_default_model=default_codex_review_model(),
            review_model_options=discover_codex_review_models(selected=model),
        )
        if persist:
            try:
                save_local_config({"review_model": model})
            except Exception:
                pass
        return model

    @classmethod
    def set_stats_scope(cls, value: object, persist: bool = True) -> str:
        scope = normalize_stats_scope(value)
        update_app_state(stats_scope=scope)
        if persist:
            try:
                save_local_config({"stats_scope": scope})
            except Exception:
                pass
        return scope

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
        if path == "/diagnostics":
            self._send_json(200, build_diagnostics())
            return
        if path == "/cases":
            self._send_json(200, {"cases": get_cases(scope=normalize_stats_scope(get_app_state().get("stats_scope")))})
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if not self._guard_origin():
            return
        path = urlparse(self.path).path
        if path == '/hook':
            if not self._guard_hook_token():
                return
        try:
            payload = self._read_payload()
        except OverflowError:
            self._send_json(413, {"ok": False, "error": "payload_too_large"})
            return
        except ValueError:
            self._send_json(400, {"ok": False, "error": "bad_content_length"})
            return
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
        if path == "/control/review-model":
            model = DashcamServer.set_review_model(payload.get("model"))
            self._send_json(200, {"ok": True, "review_model": model, "state": self._state_payload()})
            return
        if path == "/control/stats-scope":
            scope = DashcamServer.set_stats_scope(payload.get("scope"))
            self._send_json(200, {"ok": True, "stats_scope": scope, "state": self._state_payload()})
            return
        if path == "/cases/clear-old":
            removed = clear_old_cases()
            removed += clear_old_success_stats()
            restore_local_cases()
            latest = get_app_state().get("latest_case")
            if isinstance(latest, dict) and not _case_in_scope(latest, "today"):
                update_app_state(latest_case=None)
            self._send_json(200, {"ok": True, "removed": removed, "state": self._state_payload()})
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
    target = str(data.get("source_target") or data.get("suspected_skill") or "unknown")
    tokens = data.get("wasted_tokens", "?")
    return f"{_evidence_type_label(data)} · {target} · {tokens} tokens"


def format_evidence_receipt(data: Dict[str, object]) -> str:
    lines = [
        "Evidence Receipt",
        "",
        f"Type: {_evidence_type_label(data)}",
        f"Target: {data.get('source_target') or data.get('suspected_skill', 'unknown')}",
        f"Source detail: {data.get('source_detail', 'unknown')}",
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


def _default_config_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".vibe-dashcam"
    return root / "VibeDashcam" / "config.json"


def _default_stats_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".vibe-dashcam"
    return root / "VibeDashcam" / "stats.json"


def save_local_config(config: Dict[str, object], path: Optional[Path] = None) -> Path:
    target = path or _default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    current: Dict[str, object] = {}
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            current = loaded if isinstance(loaded, dict) else {}
        except Exception:
            current = {}
    current.update(config)
    target.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def load_or_create_hook_token(path: Optional[Path] = None) -> str:
    target = path or _default_config_path()
    token = ""
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                token = str(loaded.get("hook_token") or "")
        except Exception:
            token = ""
    if len(token) >= 24:
        return token
    token = secrets.token_urlsafe(32)
    save_local_config({"hook_token": token}, target)
    return token


def load_review_model(path: Optional[Path] = None) -> str:
    target = path or _default_config_path()
    if not target.exists():
        return PREFERRED_REVIEW_MODEL
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_REVIEW_MODEL
    if not isinstance(loaded, dict):
        return DEFAULT_REVIEW_MODEL
    return normalize_review_model(loaded.get("review_model"))


def load_stats_scope(path: Optional[Path] = None) -> str:
    target = path or _default_config_path()
    if not target.exists():
        return DEFAULT_STATS_SCOPE
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_STATS_SCOPE
    if not isinstance(loaded, dict):
        return DEFAULT_STATS_SCOPE
    return normalize_stats_scope(loaded.get("stats_scope"))


def load_success_stats(path: Optional[Path] = None, scope: str = DEFAULT_STATS_SCOPE) -> Dict[str, Dict[str, object]]:
    target = path or _default_stats_path()
    if not target.exists():
        return {}
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    targets = loaded.get("targets") if isinstance(loaded, dict) else None
    if not isinstance(targets, dict):
        return {}
    today_key = datetime.now().astimezone().date().isoformat()
    result: Dict[str, Dict[str, object]] = {}
    for name, value in targets.items():
        if not isinstance(value, dict):
            continue
        if normalize_stats_scope(scope) == "today":
            days = value.get("days")
            item = days.get(today_key) if isinstance(days, dict) else None
            if not isinstance(item, dict):
                continue
            success = int(item.get("success") or 0)
            tokens = int(item.get("tokens") or 0)
            last_seen = item.get("last_seen")
        else:
            success = int(value.get("success") or 0)
            tokens = int(value.get("tokens") or 0)
            last_seen = value.get("last_seen")
        if success:
            result[str(name)] = {"target": str(name), "success": success, "failure": 0, "tokens": tokens, "last_seen": last_seen}
    return result


def record_success_stat(target_name: str, tokens: int = 0, path: Optional[Path] = None) -> None:
    if not target_name:
        return
    target = path or _default_stats_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        loaded = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except Exception:
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    targets = loaded.setdefault("targets", {})
    if not isinstance(targets, dict):
        targets = {}
        loaded["targets"] = targets
    now = _utc_now()
    day_key = datetime.now().astimezone().date().isoformat()
    item = targets.setdefault(target_name, {"success": 0, "tokens": 0, "last_seen": None, "days": {}})
    days = item.setdefault("days", {})
    day = days.setdefault(day_key, {"success": 0, "tokens": 0, "last_seen": None})
    for bucket in (item, day):
        bucket["success"] = int(bucket.get("success") or 0) + 1
        bucket["tokens"] = int(bucket.get("tokens") or 0) + max(int(tokens or 0), 0)
        bucket["last_seen"] = now
    target.write_text(json.dumps(loaded, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_old_success_stats(path: Optional[Path] = None) -> int:
    target = path or _default_stats_path()
    if not target.exists():
        return 0
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return 0
    targets = loaded.get("targets") if isinstance(loaded, dict) else None
    if not isinstance(targets, dict):
        return 0
    today_key = datetime.now().astimezone().date().isoformat()
    removed = 0
    for name in list(targets.keys()):
        item = targets.get(name)
        days = item.get("days") if isinstance(item, dict) else None
        if not isinstance(days, dict):
            removed += 1
            targets.pop(name, None)
            continue
        old_keys = [key for key in days if key != today_key]
        removed += len(old_keys)
        for key in old_keys:
            days.pop(key, None)
        today = days.get(today_key)
        if isinstance(today, dict):
            item["success"] = int(today.get("success") or 0)
            item["tokens"] = int(today.get("tokens") or 0)
            item["last_seen"] = today.get("last_seen")
        else:
            targets.pop(name, None)
    target.write_text(json.dumps(loaded, ensure_ascii=False, indent=2), encoding="utf-8")
    return removed


def _trim_saved_cases(path: Path, limit: int = MAX_SAVED_CASES) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return
    if len(lines) <= limit:
        return
    kept = lines[-limit:]
    with path.open("w", encoding="utf-8") as handle:
        for line in kept:
            handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def save_local_case(data: Dict[str, object], path: Optional[Path] = None) -> Path:
    target = path or _default_cases_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    case_id = data.get("id")
    record = {"saved_at": _utc_now(), "case": data}
    if case_id and target.exists():
        records: List[Dict[str, object]] = []
        replaced = False
        try:
            with target.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    try:
                        existing = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(existing, dict):
                        continue
                    existing_case = existing.get("case")
                    if isinstance(existing_case, dict) and existing_case.get("id") == case_id:
                        records.append(record)
                        replaced = True
                    else:
                        records.append(existing)
        except Exception:
            records = []
            replaced = False
        if replaced:
            with target.open("w", encoding="utf-8") as handle:
                for item in records:
                    handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            _trim_saved_cases(target)
            return target
    if case_id and _saved_case_id_exists(target, case_id):
        _trim_saved_cases(target)
        return target
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _trim_saved_cases(target)
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


def _source_type_for_event(event: Dict[str, object]) -> str:
    if event.get("skill_evidence") == "skill_file_read" or event.get("skill_name"):
        return "skill_file_read"
    if str(event.get("event_type") or "").lower() == "mcptooluse":
        return "mcp_tool_call"
    if "mcp" in str(event.get("tool_name") or "").lower():
        return "mcp_tool_call"
    return "tool_context"


def _case_source_from_events(case: Dict[str, object]) -> Dict[str, object]:
    events = case.get("recent_events")
    if not isinstance(events, list):
        events = []
    suspected = str(case.get("suspected_skill") or "")
    source_event: Optional[Dict[str, object]] = None
    for item in reversed(events):
        if not isinstance(item, dict):
            continue
        target = _event_target(item)
        if not target:
            continue
        if not suspected or suspected == "unknown" or target == suspected:
            source_event = item
            break
    if source_event is None:
        for item in reversed(events):
            if isinstance(item, dict) and _event_target(item):
                source_event = item
                break

    result: Dict[str, object] = {}
    if source_event is not None:
        target = _event_target(source_event) or suspected or "unknown"
        result["source_type"] = _source_type_for_event(source_event)
        result["source_target"] = target
        if source_event.get("skill_name"):
            identity = _skill_identity(str(source_event.get("skill_name") or ""), source_event.get("skill_source"))
            result["source_plugin"] = identity["plugin"]
            result["source_skill"] = identity["skill"]
            result["source_display_name"] = identity["display"]
        result["source_detail"] = (
            source_event.get("skill_source")
            or source_event.get("tool_name")
            or source_event.get("skill_name")
            or target
        )
        if source_event.get("call_id"):
            result["source_call_id"] = source_event["call_id"]
    else:
        result["source_type"] = "unknown"
        result["source_target"] = suspected or "unknown"
        result["source_detail"] = suspected or "unknown"

    for item in reversed(events):
        if isinstance(item, dict) and item.get("model"):
            result["source_model"] = item["model"]
            break
    return result


def _normalize_case(data: Dict[str, object]) -> Dict[str, object]:
    case = dict(data)
    case.setdefault("id", uuid.uuid4().hex)
    case.setdefault("created_at", _utc_now())
    case.setdefault("evidence_type", _evidence_type_label(case))
    for key, value in _case_source_from_events(case).items():
        if not case.get(key):
            case[key] = value
    return case


def _is_demo_case(case: Dict[str, object]) -> bool:
    target = str(case.get("source_target") or case.get("suspected_skill") or "")
    return case.get("source_kind") == "demo" or target.startswith("mcp__demo.")


def load_local_cases(path: Optional[Path] = None, limit: int = RECENT_CASE_LIMIT) -> List[Dict[str, object]]:
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


def clear_old_cases(path: Optional[Path] = None) -> int:
    target = path or _default_cases_path()
    if not target.exists():
        return 0
    kept_records: List[Dict[str, object]] = []
    removed = 0
    try:
        with target.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except Exception:
                    removed += 1
                    continue
                case = record.get("case") if isinstance(record, dict) else None
                if isinstance(case, dict) and _case_in_scope(case, "today"):
                    kept_records.append(record)
                else:
                    removed += 1
    except Exception:
        return 0
    with target.open("w", encoding="utf-8") as handle:
        for record in kept_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return removed


def restore_local_cases(path: Optional[Path] = None) -> int:
    cases = load_local_cases(path)
    with CASE_HISTORY_LOCK:
        CASE_HISTORY.clear()
        CASE_HISTORY.extend(cases)
    update_app_state(saved_case_count=len(cases))
    return len(cases)


def record_case(data: Dict[str, object]) -> Dict[str, object]:
    case = _normalize_case(data)
    case.setdefault("case_review", {"status": "pending"})
    with CASE_HISTORY_LOCK:
        CASE_HISTORY.append(case)
    state = get_app_state()
    failure_count = int(state.get("failure_count") or 0) + 1
    case_count = int(state.get("case_count") or 0) + 1
    saved_case_count = int(state.get("saved_case_count") or 0)
    if DashcamServer.persist_cases and not _is_demo_case(case):
        saved_case_count += 1
    update_app_state(
        failure_count=failure_count,
        case_count=case_count,
        saved_case_count=saved_case_count,
        latest_case=case,
    )
    if DashcamServer.persist_cases:
        try:
            save_local_case(case)
        except Exception:
            pass
    return case


def _cases_for_scope(scope: str) -> List[Dict[str, object]]:
    with CASE_HISTORY_LOCK:
        cases = list(CASE_HISTORY)
    return [dict(case) for case in cases if _case_in_scope(case, normalize_stats_scope(scope))]


def get_cases(limit: int = RECENT_CASE_LIMIT, scope: str = "all") -> List[Dict[str, object]]:
    cases = _cases_for_scope(scope)
    return list(reversed(cases[-limit:]))


def find_case(case_id: object) -> Optional[Dict[str, object]]:
    if not case_id:
        return None
    with CASE_HISTORY_LOCK:
        for case in CASE_HISTORY:
            if case.get("id") == case_id:
                return dict(case)
    return None


def _replace_case(updated_case: Dict[str, object]) -> Optional[Dict[str, object]]:
    case_id = updated_case.get("id")
    if not case_id:
        return None
    replaced = False
    with CASE_HISTORY_LOCK:
        for index, case in enumerate(CASE_HISTORY):
            if case.get("id") == case_id:
                CASE_HISTORY[index] = updated_case
                replaced = True
                break
    if not replaced:
        return None

    state = get_app_state()
    latest = state.get("latest_case")
    if isinstance(latest, dict) and latest.get("id") == case_id:
        update_app_state(latest_case=updated_case)
    return dict(updated_case)


def update_case_review(case_id: object, reviewer: Any) -> Dict[str, object]:
    if not case_id:
        return {"status": "unavailable", "reason": "missing_case_id"}
    case_key = str(case_id)
    with CASE_REVIEW_CACHE_LOCK:
        cached = CASE_REVIEW_CACHE.get(case_key)
    if cached:
        review = dict(cached)
    else:
        case = find_case(case_id)
        if case is None:
            return {"status": "unavailable", "reason": "case_not_found"}
        existing = case.get("case_review")
        if isinstance(existing, dict) and existing.get("status") in {"reviewed", "unavailable"}:
            review = dict(existing)
        else:
            review = reviewer.review(case)
        with CASE_REVIEW_CACHE_LOCK:
            CASE_REVIEW_CACHE[case_key] = dict(review)

    current = find_case(case_id)
    if current is not None:
        current["case_review"] = dict(review)
        updated = _replace_case(current)
        if updated is not None and DashcamServer.persist_cases:
            try:
                save_local_case(updated)
            except Exception:
                pass
    return dict(review)


def create_http_server() -> Optional[http.server.ThreadingHTTPServer]:
    DashcamServer.hook_token = DashcamServer.hook_token or load_or_create_hook_token()
    server_address = ('localhost', 8080)
    try:
        return http.server.ThreadingHTTPServer(server_address, DashcamServer)
    except OSError as exc:
        update_app_state(listening=False, server_error=str(exc))
        return None


def serve_http_server(httpd: http.server.ThreadingHTTPServer) -> None:
    """在子线程中启动 HTTP 服务器。"""
    update_app_state(listening=True, server_error=None)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        update_app_state(listening=False)
        httpd.server_close()


def run_server() -> None:
    httpd = create_http_server()
    if httpd is None:
        return
    serve_http_server(httpd)


def main() -> None:
    # 启动本地取证 API。正式桌面前端由 Tauri/React 连接这些接口。
    DashcamServer.hook_token = load_or_create_hook_token()
    DashcamServer.set_review_model(load_review_model(), persist=False)
    DashcamServer.set_stats_scope(load_stats_scope(), persist=False)
    restore_local_cases()
    httpd = create_http_server()
    if httpd is None:
        print("[Vibe-Dashcam] Core API failed to bind http://localhost:8080", file=sys.stderr)
        sys.exit(2)
    server_thread = threading.Thread(target=serve_http_server, args=(httpd,), daemon=True)
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
