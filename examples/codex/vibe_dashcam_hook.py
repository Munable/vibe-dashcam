import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


DASHCAM_URL = "http://localhost:8080/hook"
MAX_FIELD_CHARS = 1200
TOKEN_HEADER = "X-Vibe-Dashcam-Token"


def _config_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".vibe-dashcam"
    return root / "VibeDashcam" / "config.json"


def _hook_token() -> Optional[str]:
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    token = data.get("hook_token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token else None


def _as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    text = text.strip()
    return text[:MAX_FIELD_CHARS] if text else None


def _first_text(data: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        text = _as_text(data.get(key))
        if text:
            return text
    return None


def build_dashcam_payload(data: Dict[str, Any], event_type: str) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "client": "codex",
        "event_type": event_type,
    }

    user_input = _first_text(data, "prompt", "user_input", "input")
    if user_input:
        payload["user_input"] = user_input

    ai_output = _first_text(data, "assistant_output", "ai_output", "summary")
    if ai_output:
        payload["ai_output"] = ai_output

    tool_name = _first_text(data, "tool_name", "toolName", "tool", "name")
    if tool_name:
        payload["tool_name"] = tool_name

    model = _first_text(data, "model")
    if model:
        payload["model"] = model

    tokens = data.get("token_count") or data.get("tokens")
    if isinstance(tokens, (int, float)):
        payload["token_count"] = int(tokens)

    return payload


def post_payload(payload: Dict[str, object]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = _hook_token()
    if token:
        headers[TOKEN_HEADER] = token
    req = urllib.request.Request(
        DASHCAM_URL,
        data=body,
        headers=headers,
    )
    try:
        urllib.request.urlopen(req, timeout=0.7).read()
    except Exception:
        pass


def main() -> int:
    event_type = sys.argv[1] if len(sys.argv) > 1 else "CodexHook"
    raw = sys.stdin.read()
    try:
        data = json.loads(raw.replace("\ufeff", "")) if raw.strip() else {}
    except Exception:
        data = {"prompt": raw}
    post_payload(build_dashcam_payload(data, event_type))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
