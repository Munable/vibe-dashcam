import os
import http.server
import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from vibe_dashcam.vibe_dashcam import (
    APP_STATE,
    APP_STATE_LOCK,
    CASE_HISTORY,
    CASE_HISTORY_LOCK,
    CodexSessionTailer,
    DashcamServer,
    extract_codex_session_event,
    FeedbackClassifier,
    FailureSignalDetector,
    RecentBehaviorBuffer,
    SKILL_STATS,
    SKILL_STATS_LOCK,
    SummaryGenerator,
    get_cases,
    get_skill_board,
    load_local_cases,
    _parse_toml_model,
    _sanitize_event,
    _update_skill_stats,
    format_evidence_receipt,
    record_case,
    restore_local_cases,
    save_local_case,
    scan_configurations,
)

HOOK_PATH = Path(__file__).resolve().parents[1] / "examples" / "codex" / "vibe_dashcam_hook.py"


def reset_runtime_state() -> None:
    DashcamServer.recent_events.clear()
    drain_queue(DashcamServer.summary_queue)
    DashcamServer.paused = False
    DashcamServer.persist_cases = False
    with CASE_HISTORY_LOCK:
        CASE_HISTORY.clear()
    with SKILL_STATS_LOCK:
        SKILL_STATS.clear()
    with APP_STATE_LOCK:
        APP_STATE.update({
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
        })


@contextmanager
def run_test_server():
    server = http.server.ThreadingHTTPServer(("localhost", 0), DashcamServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://localhost:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def api_json(base_url: str, path: str, method: str = "GET", payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def api_status(base_url: str, path: str, method: str = "GET", payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, response.headers, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.headers, exc.read().decode("utf-8")
        finally:
            exc.close()


def load_hook_module():
    spec = importlib.util.spec_from_file_location("vibe_dashcam_hook", HOOK_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load hook module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VibeDashcamTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_runtime_state()

    def test_recent_buffer_keeps_latest_whitelisted_events(self) -> None:
        buffer = RecentBehaviorBuffer(limit=2)

        buffer.add({"user_input": "first", "api_key": "secret", "skill_name": "codex"})
        buffer.add({"event_type": "PostToolUse", "tool_name": "edit"})
        buffer.add({"user_input": "third", "client": "claude"})

        events = buffer.snapshot()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event_type"], "PostToolUse")
        self.assertEqual(events[1]["user_input"], "third")
        self.assertNotIn("api_key", events[0])
        self.assertNotIn("api_key", events[1])

    def test_feedback_classifier_labels_clear_denial_as_negative(self) -> None:
        decision = FeedbackClassifier().classify("不对，全部重来")

        self.assertTrue(decision.negative)
        self.assertEqual(decision.category, "ai_failed_previous_result")

    def test_hard_failure_detector_requires_skill_or_mcp_context(self) -> None:
        detector = FailureSignalDetector()

        plain_error = detector.classify({
            "event_type": "PostToolUse",
            "tool_name": "shell_command",
            "ai_output": "timeout",
        })
        mcp_error = detector.classify({
            "event_type": "McpToolUse",
            "tool_name": "mcp__node_repl.js",
            "ai_output": "timeout",
        })

        self.assertFalse(plain_error.negative)
        self.assertTrue(mcp_error.negative)
        self.assertEqual(mcp_error.category, "skill_mcp_hard_failure")

    def test_hard_failure_detector_ignores_clean_status_output(self) -> None:
        detector = FailureSignalDetector()

        decision = detector.classify(
            {
                "event_type": "ToolOutput",
                "ai_output": 'Exit code: 0\n{"server_error":null,"failure_count":0}',
            },
            [{"event_type": "McpToolUse", "tool_name": "mcp__node_repl.js_reset"}],
        )

        self.assertFalse(decision.negative)

    def test_hard_failure_detector_ignores_clean_tool_output_with_failure_words(self) -> None:
        detector = FailureSignalDetector()

        decision = detector.classify(
            {
                "event_type": "ToolOutput",
                "ai_output": "Exit code: 0\nOutput:\nThis doc mentions failure, error, timeout, and 失败.",
            },
            [{"event_type": "McpToolUse", "tool_name": "mcp__node_repl.js_reset"}],
        )

        self.assertFalse(decision.negative)

    def test_hard_failure_detector_flags_nonzero_exit_code(self) -> None:
        detector = FailureSignalDetector()

        decision = detector.classify(
            {"event_type": "ToolOutput", "ai_output": "Exit code: 1\nboom"},
            [{"event_type": "McpToolUse", "tool_name": "mcp__node_repl.js_reset"}],
        )

        self.assertTrue(decision.negative)

    def test_hard_failure_detector_ignores_assistant_words_near_mcp(self) -> None:
        detector = FailureSignalDetector()

        decision = detector.classify(
            {"event_type": "AssistantMessage", "ai_output": "保存失败时显示失败提示"},
            [{"event_type": "McpToolUse", "tool_name": "mcp__node_repl.js_reset"}],
        )

        self.assertFalse(decision.negative)

    def test_summary_uses_recent_events_and_cautious_negative_language(self) -> None:
        decision = FeedbackClassifier().classify("wrong, undo that")
        summary = SummaryGenerator().generate(
            [
                {"client": "codex", "skill_name": "skill-a", "ai_output": "x" * 80},
                {"client": "codex", "tool_name": "edit", "token_count": 20},
            ],
            trigger_text="wrong, undo that",
            decision=decision,
        )

        self.assertTrue(summary["negative"])
        self.assertEqual(summary["events_count"], 2)
        self.assertEqual(summary["suspected_client"], "codex")
        self.assertIn("用户否定", summary["summary"])
        self.assertNotIn("一定", summary["summary"])

    def test_summary_distinguishes_hard_failure(self) -> None:
        decision = FailureSignalDetector().classify({
            "event_type": "McpToolUse",
            "tool_name": "mcp__node_repl.js",
            "ai_output": "tool call failed: timeout",
        })

        summary = SummaryGenerator().generate(
            [{"client": "codex", "tool_name": "mcp__node_repl.js", "ai_output": "timeout"}],
            trigger_text="timeout",
            decision=decision,
        )

        self.assertEqual(summary["category"], "skill_mcp_hard_failure")
        self.assertIn("崩溃证据候选", summary["summary"])

    def test_summary_prioritizes_hard_failure_trigger_target(self) -> None:
        decision = FailureSignalDetector().classify({
            "event_type": "McpToolUse",
            "tool_name": "mcp__demo.timeout",
            "ai_output": "timeout",
        })

        summary = SummaryGenerator().generate(
            [
                {"client": "codex", "event_type": "PostToolUse", "tool_name": "shell_command"},
                {
                    "client": "vibe-dashcam",
                    "event_type": "McpToolUse",
                    "tool_name": "mcp__demo.timeout",
                    "ai_output": "timeout",
                },
            ],
            trigger_text="timeout",
            decision=decision,
        )

        self.assertEqual(summary["suspected_skill"], "mcp__demo.timeout")

    def test_summary_ignores_plain_tools_when_choosing_failure_target(self) -> None:
        decision = FailureSignalDetector().classify(
            {"event_type": "ToolOutput", "ai_output": "timeout"},
            [{"event_type": "McpToolUse", "tool_name": "mcp__node_repl.js"}],
        )

        summary = SummaryGenerator().generate(
            [
                {"client": "codex", "event_type": "McpToolUse", "tool_name": "mcp__node_repl.js"},
                {"client": "codex", "event_type": "PostToolUse", "tool_name": "shell_command"},
                {"client": "codex", "event_type": "ToolOutput", "ai_output": "timeout"},
            ],
            trigger_text="timeout",
            decision=decision,
        )

        self.assertEqual(summary["suspected_skill"], "mcp__node_repl.js")

    def test_summary_ignores_session_total_token_count_events(self) -> None:
        decision = FailureSignalDetector().classify({
            "event_type": "McpToolUse",
            "tool_name": "mcp__demo.timeout",
            "ai_output": "timeout",
        })

        summary = SummaryGenerator().generate(
            [
                {"client": "codex", "event_type": "TokenCount", "token_count": 999999},
                {
                    "client": "vibe-dashcam",
                    "event_type": "McpToolUse",
                    "tool_name": "mcp__demo.timeout",
                    "ai_output": "timeout",
                    "token_count": 321,
                },
            ],
            trigger_text="timeout",
            decision=decision,
        )

        self.assertEqual(summary["wasted_tokens"], 321)

    def test_skill_board_prioritizes_failures_before_recent_successes(self) -> None:
        _update_skill_stats("mcp__ok.newer", failed=False)
        _update_skill_stats("mcp__bad.older", failed=True)

        board = get_skill_board()

        self.assertEqual(board[0]["target"], "mcp__bad.older")

    def test_toml_scan_reads_model_without_exposing_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config.toml"
            config.write_text(
                'api_key = "KEY_SHOULD_NOT_LEAK"\nmodel = "gpt-5.3-codex-spark"\n',
                encoding="utf-8",
            )

            self.assertEqual(_parse_toml_model(config), "gpt-5.3-codex-spark")

    def test_windows_opencode_scan_uses_config_fallback_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fallback = root / ".config" / "opencode" / "opencode.json"
            fallback.parent.mkdir(parents=True)
            fallback.write_text('{"model": "openai/test-model"}', encoding="utf-8")

            with mock.patch("vibe_dashcam.vibe_dashcam.platform.system", return_value="Windows"):
                with mock.patch.dict(
                    os.environ,
                    {"USERPROFILE": str(root), "APPDATA": str(root / "AppData" / "Roaming")},
                    clear=False,
                ):
                    models = scan_configurations()

            self.assertEqual(models["opencode"], "openai/test-model")

    def test_codex_hook_maps_prompt_without_forwarding_secret_fields(self) -> None:
        hook = load_hook_module()

        payload = hook.build_dashcam_payload(
            {
                "prompt": "不对，重来",
                "api_key": "KEY_SHOULD_NOT_FORWARD",
                "tool_name": "Bash",
            },
            "UserPromptSubmit",
        )

        self.assertEqual(payload["client"], "codex")
        self.assertEqual(payload["event_type"], "UserPromptSubmit")
        self.assertEqual(payload["user_input"], "不对，重来")
        self.assertEqual(payload["tool_name"], "Bash")
        self.assertNotIn("api_key", payload)

    def test_sanitize_event_redacts_embedded_secret_text(self) -> None:
        event = _sanitize_event({
            "client": "codex",
            "user_input": "api_key=abc123456789 please debug",
            "ai_output": "token: localtoken123",
        })

        self.assertIn("[REDACTED]", event["user_input"])
        self.assertIn("[REDACTED]", event["ai_output"])
        self.assertNotIn("abc123456789", event["user_input"])
        self.assertNotIn("localtoken123", event["ai_output"])

    def test_extract_codex_session_user_message(self) -> None:
        event = extract_codex_session_event({
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "不对，重来",
            },
        })

        self.assertEqual(event["client"], "codex")
        self.assertEqual(event["source_kind"], "codex_session")
        self.assertEqual(event["event_type"], "UserPromptSubmit")
        self.assertEqual(event["user_input"], "不对，重来")

    def test_extract_codex_session_tool_call_without_arguments(self) -> None:
        event = extract_codex_session_event({
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": '{"command":"Get-Content secret.txt"}',
            },
        })

        self.assertEqual(event["event_type"], "PostToolUse")
        self.assertEqual(event["tool_name"], "shell_command")
        self.assertNotIn("arguments", event)

    def test_extract_codex_session_skill_read_marks_skill_name(self) -> None:
        event = extract_codex_session_event({
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": (
                    '{"command":"Get-Content -LiteralPath '
                    'C:\\\\Users\\\\MUBIN\\\\.codex\\\\skills\\\\.system\\\\imagegen\\\\SKILL.md"}'
                ),
            },
        })

        self.assertEqual(event["tool_name"], "shell_command")
        self.assertEqual(event["skill_name"], "imagegen")

    def test_extract_codex_session_mcp_namespace_marks_mcp_tool(self) -> None:
        event = extract_codex_session_event({
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "namespace": "mcp__node_repl",
                "name": "js",
            },
        })

        self.assertEqual(event["event_type"], "McpToolUse")
        self.assertEqual(event["tool_name"], "mcp__node_repl.js")

    def test_extract_codex_session_function_call_output(self) -> None:
        event = extract_codex_session_event({
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": "tool call failed: timeout",
            },
        })

        self.assertEqual(event["event_type"], "ToolOutput")
        self.assertIn("timeout", event["ai_output"])

    def test_dashcam_server_soft_failure_requires_skill_or_mcp_context(self) -> None:
        DashcamServer.recent_events.clear()
        drain_queue(DashcamServer.summary_queue)

        DashcamServer.ingest_payload({
            "client": "codex",
            "event_type": "PostToolUse",
            "tool_name": "shell_command",
        })
        DashcamServer.ingest_payload({"client": "codex", "user_input": "不对，重来"})

        self.assertTrue(DashcamServer.summary_queue.empty())

    def test_dashcam_server_queues_hard_failure_for_mcp_error(self) -> None:
        DashcamServer.recent_events.clear()
        drain_queue(DashcamServer.summary_queue)
        DashcamServer.paused = False

        DashcamServer.ingest_payload({
            "client": "codex",
            "event_type": "McpToolUse",
            "tool_name": "mcp__node_repl.js",
            "ai_output": "timeout",
        })

        summary = DashcamServer.summary_queue.get_nowait()
        self.assertEqual(summary["category"], "skill_mcp_hard_failure")
        self.assertEqual(summary["source_kind"], "codex_session")

    def test_dashcam_server_queues_tool_output_failure_after_skill_context(self) -> None:
        DashcamServer.recent_events.clear()
        drain_queue(DashcamServer.summary_queue)
        DashcamServer.paused = False

        DashcamServer.ingest_payload({
            "client": "codex",
            "event_type": "PostToolUse",
            "tool_name": "shell_command",
            "skill_name": "imagegen",
        })
        DashcamServer.ingest_payload({
            "client": "codex",
            "event_type": "ToolOutput",
            "ai_output": "tool call failed: timeout",
        })

        summary = DashcamServer.summary_queue.get_nowait()
        self.assertEqual(summary["category"], "skill_mcp_hard_failure")

    def test_dashcam_server_pause_suppresses_cases(self) -> None:
        DashcamServer.paused = True
        try:
            DashcamServer.ingest_payload({
                "client": "codex",
                "event_type": "McpToolUse",
                "tool_name": "mcp__node_repl.js",
                "ai_output": "timeout",
            })

            self.assertTrue(DashcamServer.summary_queue.empty())
        finally:
            DashcamServer.paused = False

    def test_state_api_reports_skill_board_and_latest_case(self) -> None:
        with run_test_server() as base_url:
            response = api_json(base_url, "/hook", "POST", {
                "client": "codex",
                "source_kind": "hook",
                "event_type": "McpToolUse",
                "tool_name": "mcp__demo.timeout",
                "ai_output": "timeout",
                "token_count": 321,
            })
            state = api_json(base_url, "/state")
            cases = api_json(base_url, "/cases")

        self.assertTrue(response["ok"])
        self.assertEqual(state["failure_count"], 1)
        self.assertEqual(state["case_count"], 1)
        self.assertEqual(state["latest_case"]["suspected_skill"], "mcp__demo.timeout")
        self.assertEqual(state["latest_case"]["source_kind"], "hook")
        self.assertEqual(cases["cases"][0]["suspected_skill"], "mcp__demo.timeout")
        self.assertEqual(state["skill_board"][0]["target"], "mcp__demo.timeout")
        self.assertEqual(state["skill_board"][0]["failure"], 1)

    def test_test_capture_returns_demo_without_polluting_real_state(self) -> None:
        with run_test_server() as base_url:
            response = api_json(base_url, "/test-capture", "POST", {})
            state = api_json(base_url, "/state")
            cases = api_json(base_url, "/cases")

        self.assertTrue(response["ok"])
        self.assertFalse(response["created"])
        self.assertEqual(response["demo_case"]["source_kind"], "demo")
        self.assertEqual(state["failure_count"], 0)
        self.assertEqual(state["case_count"], 0)
        self.assertIsNone(state["latest_case"])
        self.assertEqual(cases["cases"], [])
        self.assertEqual(state["skill_board"], [])

    def test_pause_api_suppresses_hook_cases(self) -> None:
        with run_test_server() as base_url:
            pause_response = api_json(base_url, "/control/pause", "POST", {"paused": True})
            capture_response = api_json(base_url, "/hook", "POST", {
                "event_type": "McpToolUse",
                "tool_name": "mcp__demo.timeout",
                "ai_output": "timeout",
            })
            state = api_json(base_url, "/state")

        self.assertTrue(pause_response["state"]["paused"])
        self.assertTrue(capture_response["ok"])
        self.assertEqual(state["failure_count"], 0)
        self.assertEqual(state["case_count"], 0)

    def test_cases_save_api_writes_selected_case(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temp_dir}, clear=False):
                with run_test_server() as base_url:
                    api_json(base_url, "/hook", "POST", {
                        "event_type": "McpToolUse",
                        "tool_name": "mcp__demo.timeout",
                        "ai_output": "timeout",
                    })
                    case_id = api_json(base_url, "/cases")["cases"][0]["id"]
                    response = api_json(base_url, "/cases/save", "POST", {"case_id": case_id})

            saved_path = Path(response["path"])
            saved_lines = saved_path.read_text(encoding="utf-8").splitlines()

        self.assertTrue(response["ok"])
        self.assertTrue(response["exists"])
        self.assertGreater(response["bytes"], 0)
        self.assertEqual(len(saved_lines), 1)
        self.assertEqual(json.loads(saved_lines[0])["case"]["id"], case_id)

    def test_format_evidence_receipt_includes_recent_traces(self) -> None:
        receipt = format_evidence_receipt({
            "category": "skill_mcp_hard_failure",
            "suspected_skill": "mcp__demo.timeout",
            "wasted_tokens": 321,
            "wasted_cost": 0.01,
            "summary": "captured",
            "recent_events": [
                {"event_type": "McpToolUse", "tool_name": "mcp__demo.timeout", "ai_output": "timeout"}
            ],
        })

        self.assertIn("Evidence Receipt", receipt)
        self.assertIn("Tool-crash evidence", receipt)
        self.assertIn("Recent local traces", receipt)

    def test_save_local_case_appends_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cases.jsonl"

            saved_path = save_local_case({"summary": "captured"}, path)

            lines = saved_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(saved_path, path)
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["case"]["summary"], "captured")

    def test_record_case_auto_persists_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temp_dir}, clear=False):
                DashcamServer.persist_cases = True

                case = record_case({
                    "suspected_skill": "mcp__real.timeout",
                    "summary": "captured",
                    "wasted_tokens": 12,
                })

                path = Path(temp_dir) / "VibeDashcam" / "cases.jsonl"
                saved = load_local_cases(path)
                self.assertEqual(saved[0]["id"], case["id"])

    def test_restore_local_cases_rehydrates_recent_cases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cases.jsonl"
            save_local_case({
                "id": "demo",
                "suspected_skill": "mcp__demo.timeout",
                "summary": "demo",
                "wasted_tokens": 99,
            }, path)
            save_local_case({
                "id": "older",
                "suspected_skill": "mcp__old.tool",
                "summary": "old",
                "wasted_tokens": 5,
            }, path)
            save_local_case({
                "id": "newer",
                "suspected_skill": "mcp__new.tool",
                "summary": "new",
                "wasted_tokens": 8,
            }, path)

            restored = restore_local_cases(path)

            self.assertEqual(restored, 2)
            self.assertEqual(get_cases()[0]["id"], "newer")
            self.assertEqual(get_skill_board()[0]["target"], "mcp__new.tool")

    def test_codex_session_tailer_reads_only_new_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_dir = root / "2026" / "07" / "05"
            log_dir.mkdir(parents=True)
            log_file = log_dir / "session.jsonl"
            log_file.write_text(
                json_line({"type": "event_msg", "payload": {"type": "user_message", "message": "old"}}),
                encoding="utf-8",
            )
            events = []
            tailer = CodexSessionTailer(events.append, sessions_root=root)
            tailer._prime_offsets()

            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(json_line({"type": "event_msg", "payload": {"type": "user_message", "message": "new"}}))
            tailer.poll_once()

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["user_input"], "new")

    def test_codex_session_tailer_default_root_uses_home_on_all_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            with mock.patch("vibe_dashcam.vibe_dashcam.Path.home", return_value=home):
                with mock.patch.dict(os.environ, {"USERPROFILE": "C:\\wrong"}, clear=False):
                    tailer = CodexSessionTailer(lambda event: None)

        self.assertEqual(tailer.sessions_root, home / ".codex" / "sessions")

    def test_health_api_identifies_vibe_dashcam_core(self) -> None:
        with run_test_server() as base_url:
            response = api_json(base_url, "/health")

        self.assertTrue(response["ok"])
        self.assertEqual(response["app"], "vibe-dashcam")

    def test_cors_allows_dev_origin_and_rejects_unknown_origin_for_private_api(self) -> None:
        with run_test_server() as base_url:
            ok_status, ok_headers, _ = api_status(
                base_url,
                "/state",
                headers={"Origin": "http://127.0.0.1:1420"},
            )
            bad_status, _, _ = api_status(
                base_url,
                "/state",
                headers={"Origin": "https://example.com"},
            )

        self.assertEqual(ok_status, 200)
        self.assertEqual(ok_headers["Access-Control-Allow-Origin"], "http://127.0.0.1:1420")
        self.assertEqual(bad_status, 403)


def drain_queue(target_queue) -> None:
    while not target_queue.empty():
        target_queue.get_nowait()


def json_line(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    unittest.main()
