import os
import http.server
import importlib.util
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest import mock

from vibe_dashcam.vibe_dashcam import (
    APP_STATE,
    APP_STATE_LOCK,
    CASE_HISTORY,
    CASE_HISTORY_LOCK,
    CASE_REVIEW_CACHE,
    CASE_REVIEW_CACHE_LOCK,
    CodexSessionTailer,
    CodexFeedbackClassifier,
    DashcamServer,
    CaseReviewer,
    clear_old_cases,
    clear_old_success_stats,
    default_codex_review_model,
    discover_codex_review_models,
    extract_codex_session_event,
    FeedbackDecision,
    FeedbackClassifier,
    FailureSignalDetector,
    RecentBehaviorBuffer,
    SKILL_STATS,
    SKILL_STATS_LOCK,
    SummaryGenerator,
    build_case_review_payload,
    build_case_review_prompt,
    build_diagnostics,
    build_soft_feedback_prompt,
    create_http_server,
    get_app_state,
    get_cases,
    get_skill_board,
    load_local_cases,
    load_review_model,
    load_stats_scope,
    load_success_stats,
    normalize_review_model,
    normalize_stats_scope,
    _subprocess_no_window_kwargs,
    _sanitize_event,
    update_case_review,
    _update_skill_stats,
    format_evidence_receipt,
    record_case,
    record_success_stat,
    restore_local_cases,
    save_local_case,
    save_local_config,
    MAX_SAVED_CASES,
    MAX_REQUEST_BYTES,
    HOOK_TOKEN_HEADER,
)

HOOK_PATH = Path(__file__).resolve().parents[1] / "examples" / "codex" / "vibe_dashcam_hook.py"


def reset_runtime_state() -> None:
    DashcamServer.recent_events.clear()
    drain_queue(DashcamServer.summary_queue)
    DashcamServer.paused = False
    DashcamServer.persist_cases = False
    DashcamServer.persist_success_stats = False
    DashcamServer.review_cases = False
    DashcamServer.hook_token = "test-hook-token"
    DashcamServer.case_reviewer = CaseReviewer()
    DashcamServer.semantic_classifier = CodexFeedbackClassifier()
    with CASE_HISTORY_LOCK:
        CASE_HISTORY.clear()
    with SKILL_STATS_LOCK:
        SKILL_STATS.clear()
    with CASE_REVIEW_CACHE_LOCK:
        CASE_REVIEW_CACHE.clear()
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
            "saved_case_count": 0,
            "stats_scope": "today",
            "display_ok_event_count": 0,
            "display_failure_count": 0,
            "review_model": "default",
            "review_default_model": "default",
            "review_model_options": ["default"],
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
    headers = {"Content-Type": "application/json"}
    if path != "/hook":
        headers["Origin"] = "http://127.0.0.1:1420"
    if path == "/hook":
        headers[HOOK_TOKEN_HEADER] = DashcamServer.hook_token or ""
    request = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers=headers,
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

    def test_feedback_classifier_labels_product_result_rebuttal_as_negative(self) -> None:
        decision = FeedbackClassifier().classify("这个效果不对，中间这一段开发方向偏了")

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

    def test_hard_failure_detector_ignores_ui_text_without_explicit_tool_failure(self) -> None:
        detector = FailureSignalDetector()

        decision = detector.classify(
            {
                "event_type": "ToolOutput",
                "ai_output": "Vibe-Dashcam\nFailure history\n0 failed\nNo failure yet\nflagged",
            },
            [{"event_type": "McpToolUse", "tool_name": "mcp__node_repl.js"}],
        )

        self.assertFalse(decision.negative)

    def test_hard_failure_detector_flags_explicit_failure_even_with_clean_exit(self) -> None:
        detector = FailureSignalDetector()

        decision = detector.classify(
            {
                "event_type": "ToolOutput",
                "ai_output": "Exit code: 0\nOutput:\ntool call failed: timeout while calling MCP",
            },
            [{"event_type": "McpToolUse", "tool_name": "mcp__node_repl.js_reset"}],
        )

        self.assertTrue(decision.negative)

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

    def test_codex_hook_does_not_launch_dashcam(self) -> None:
        hook_source = HOOK_PATH.read_text(encoding="utf-8")

        self.assertNotIn("subprocess", hook_source)
        self.assertNotIn("Start-Process", hook_source)
        self.assertNotIn("Popen", hook_source)

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

    def test_sanitize_event_redacts_common_secret_formats(self) -> None:
        event = _sanitize_event({
            "client": "codex",
            "user_input": "Authorization: Bearer abcdefghijklmnop password=correct-horse",
            "ai_output": "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
        })

        text = json.dumps(event, ensure_ascii=False)
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("abcdefghijklmnop", text)
        self.assertNotIn("correct-horse", text)
        self.assertNotIn("PRIVATE KEY", text)

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
        self.assertEqual(event["skill_evidence"], "skill_file_read")
        self.assertIn("imagegen\\SKILL.md", event["skill_source"])

    def test_extract_codex_session_search_does_not_mark_skill_read(self) -> None:
        event = extract_codex_session_event({
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": (
                    '{"command":"rg -n \\"SKILL.md\\" '
                    'C:\\\\Users\\\\MUBIN\\\\.codex\\\\skills"}'
                ),
            },
        })

        self.assertEqual(event["event_type"], "PostToolUse")
        self.assertEqual(event["tool_name"], "shell_command")
        self.assertNotIn("skill_name", event)
        self.assertNotIn("skill_evidence", event)

    def test_extract_codex_session_mcp_namespace_marks_mcp_tool(self) -> None:
        event = extract_codex_session_event({
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "namespace": "mcp__node_repl",
                "name": "js",
                "call_id": "call_abc",
            },
        })

        self.assertEqual(event["event_type"], "McpToolUse")
        self.assertEqual(event["tool_name"], "mcp__node_repl.js")
        self.assertEqual(event["call_id"], "call_abc")

    def test_extract_codex_session_function_call_output(self) -> None:
        event = extract_codex_session_event({
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_abc",
                "output": "tool call failed: timeout",
            },
        })

        self.assertEqual(event["event_type"], "ToolOutput")
        self.assertEqual(event["call_id"], "call_abc")
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

    def test_dashcam_server_uses_semantic_soft_failure_after_skill_context(self) -> None:
        class FakeSemanticClassifier:
            def __init__(self) -> None:
                self.calls = 0

            def set_model(self, value):
                return str(value)

            def classify(self, user_input, recent_events):
                self.calls += 1
                if "上一版" not in user_input or not recent_events:
                    raise AssertionError("semantic classifier received wrong context")
                return FeedbackDecision(True, "ai_failed_previous_result", "用户语义上在纠偏上一轮", 0.82)

        fake = FakeSemanticClassifier()
        DashcamServer.semantic_classifier = fake
        DashcamServer.recent_events.clear()
        drain_queue(DashcamServer.summary_queue)

        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "McpToolUse",
            "tool_name": "mcp__node_repl.js",
        })
        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "UserPromptSubmit",
            "user_input": "上一版那个处理方向偏离了我原始目标，请按原意处理",
        })

        summary = DashcamServer.summary_queue.get_nowait()
        self.assertEqual(fake.calls, 1)
        self.assertEqual(summary["category"], "ai_failed_previous_result")
        self.assertIn("上一版", summary["trigger_text"])

    def test_dashcam_server_catches_generic_result_rebuttal_after_skill_context(self) -> None:
        class FakeSemanticClassifier:
            def __init__(self) -> None:
                self.calls = 0

            def classify(self, user_input, recent_events):
                self.calls += 1
                self.last_input = user_input
                return FeedbackDecision(True, "ai_failed_previous_result", "用户语义上在纠偏上一轮", 0.86)

            def set_model(self, value):
                return str(value)

        fake = FakeSemanticClassifier()
        DashcamServer.semantic_classifier = fake
        DashcamServer.recent_events.clear()
        drain_queue(DashcamServer.summary_queue)

        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "McpToolUse",
            "tool_name": "mcp__node_repl.js",
        })
        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "UserPromptSubmit",
            "user_input": "这个效果不对，中间这一段开发不对，整体方向偏了",
        })

        summary = DashcamServer.summary_queue.get_nowait()
        self.assertEqual(fake.calls, 1)
        self.assertIn("开发不对", fake.last_input)
        self.assertEqual(summary["category"], "ai_failed_previous_result")
        self.assertIn("开发不对", summary["trigger_text"])

    def test_dashcam_server_semantic_check_overrides_regex_soft_failure(self) -> None:
        class FakeSemanticClassifier:
            def __init__(self) -> None:
                self.calls = 0

            def classify(self, user_input, recent_events):
                self.calls += 1
                return FeedbackDecision(False, "user_changed_mind", "用户是在改需求，不是纠偏上一轮", 0.8)

            def set_model(self, value):
                return str(value)

        fake = FakeSemanticClassifier()
        DashcamServer.semantic_classifier = fake
        DashcamServer.recent_events.clear()
        drain_queue(DashcamServer.summary_queue)

        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "McpToolUse",
            "tool_name": "mcp__node_repl.js",
        })
        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "UserPromptSubmit",
            "user_input": "不对，改成另一个全新的方案",
        })

        self.assertEqual(fake.calls, 1)
        self.assertTrue(DashcamServer.summary_queue.empty())

    def test_dashcam_server_semantic_soft_failure_ignores_normal_followup(self) -> None:
        class FakeSemanticClassifier:
            def classify(self, user_input, recent_events):
                return FeedbackDecision(False, "user_changed_mind", "用户只是追加新任务", 0.74)

            def set_model(self, value):
                return str(value)

        DashcamServer.semantic_classifier = FakeSemanticClassifier()
        DashcamServer.recent_events.clear()
        drain_queue(DashcamServer.summary_queue)

        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "McpToolUse",
            "tool_name": "mcp__node_repl.js",
        })
        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "UserPromptSubmit",
            "user_input": "再加一个导出按钮",
        })

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

    def test_dashcam_server_tool_output_binds_to_matching_call_id(self) -> None:
        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "McpToolUse",
            "tool_name": "mcp__good.tool",
            "call_id": "call_good",
        })
        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "McpToolUse",
            "tool_name": "mcp__wrong.tool",
            "call_id": "call_wrong",
        })
        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "ToolOutput",
            "call_id": "call_good",
            "ai_output": "tool call failed: timeout",
        })

        summary = DashcamServer.summary_queue.get_nowait()
        self.assertEqual(summary["suspected_skill"], "mcp__good.tool")

    def test_dashcam_server_tool_output_with_unknown_call_id_does_not_guess_target(self) -> None:
        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "McpToolUse",
            "tool_name": "mcp__good.tool",
            "call_id": "call_good",
        })
        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "ToolOutput",
            "call_id": "call_unknown",
            "ai_output": "tool call failed: timeout",
        })

        self.assertTrue(DashcamServer.summary_queue.empty())

    def test_dashcam_server_redacts_user_trigger_text_before_saving_case(self) -> None:
        class FakeSemanticClassifier:
            def classify(self, user_input, recent_events):
                return FeedbackDecision(True, "ai_failed_previous_result", "用户纠偏上一轮", 0.8)

            def set_model(self, value):
                return str(value)

        DashcamServer.semantic_classifier = FakeSemanticClassifier()
        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "McpToolUse",
            "tool_name": "mcp__good.tool",
        })
        DashcamServer.ingest_payload({
            "client": "codex",
            "source_kind": "codex_session",
            "event_type": "UserPromptSubmit",
            "user_input": "不对 api_key=rawsecret123456789 重来",
        })

        summary = DashcamServer.summary_queue.get_nowait()
        self.assertIn("[REDACTED]", summary["trigger_text"])
        self.assertNotIn("rawsecret123456789", summary["trigger_text"])

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
        self.assertEqual(state["latest_case"]["source_type"], "mcp_tool_call")
        self.assertEqual(state["latest_case"]["source_target"], "mcp__demo.timeout")
        self.assertEqual(state["latest_case"]["source_detail"], "mcp__demo.timeout")
        self.assertEqual(cases["cases"][0]["suspected_skill"], "mcp__demo.timeout")
        self.assertEqual(state["skill_board"][0]["target"], "mcp__demo.timeout")
        self.assertEqual(state["skill_board"][0]["failure"], 1)

    def test_record_case_keeps_skill_file_provenance(self) -> None:
        case = record_case({
            "suspected_skill": "imagegen",
            "summary": "captured",
            "wasted_tokens": 12,
            "recent_events": [
                {
                    "client": "codex",
                    "source_kind": "codex_session",
                    "event_type": "PostToolUse",
                    "tool_name": "shell_command",
                    "skill_name": "imagegen",
                    "skill_evidence": "skill_file_read",
                    "skill_source": "C:\\Users\\MUBIN\\.codex\\skills\\.system\\imagegen\\SKILL.md",
                }
            ],
        })

        self.assertEqual(case["source_type"], "skill_file_read")
        self.assertEqual(case["source_target"], "system:imagegen")
        self.assertEqual(case["source_display_name"], "system:imagegen")
        self.assertIn("imagegen\\SKILL.md", case["source_detail"])

    def test_record_case_formats_plugin_skill_display_name(self) -> None:
        case = record_case({
            "suspected_skill": "computer-use",
            "summary": "captured",
            "wasted_tokens": 12,
            "recent_events": [
                {
                    "client": "codex",
                    "source_kind": "codex_session",
                    "event_type": "PostToolUse",
                    "tool_name": "shell_command",
                    "skill_name": "computer-use",
                    "skill_evidence": "skill_file_read",
                    "skill_source": (
                        "C:\\Users\\MUBIN\\.codex\\plugins\\cache\\openai-bundled\\computer-use\\"
                        "26.623.101652\\skills\\computer-use\\SKILL.md"
                    ),
                }
            ],
        })

        self.assertEqual(case["source_plugin"], "computer-use")
        self.assertEqual(case["source_skill"], "computer-use")
        self.assertEqual(case["source_target"], "computer-use:computer-use")

    def test_record_case_formats_curated_plugin_skill_display_name(self) -> None:
        case = record_case({
            "suspected_skill": "audit",
            "summary": "captured",
            "wasted_tokens": 12,
            "recent_events": [
                {
                    "client": "codex",
                    "source_kind": "codex_session",
                    "event_type": "PostToolUse",
                    "tool_name": "shell_command",
                    "skill_name": "audit",
                    "skill_evidence": "skill_file_read",
                    "skill_source": (
                        "C:\\Users\\MUBIN\\.codex\\plugins\\cache\\openai-curated-remote\\product-design\\"
                        "0.1.47\\skills\\audit\\SKILL.md"
                    ),
                }
            ],
        })

        self.assertEqual(case["source_plugin"], "product-design")
        self.assertEqual(case["source_skill"], "audit")
        self.assertEqual(case["source_target"], "product-design:audit")

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

    def test_save_local_case_rewrites_existing_case_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cases.jsonl"

            save_local_case({"id": "same", "summary": "pending"}, path)
            save_local_case({"id": "same", "summary": "reviewed"}, path)

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["case"]["summary"], "reviewed")

    def test_save_local_case_trims_old_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cases.jsonl"

            for index in range(MAX_SAVED_CASES + 3):
                save_local_case({"id": f"case-{index}", "summary": f"case {index}"}, path)

            lines = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), MAX_SAVED_CASES)
        self.assertEqual(json.loads(lines[0])["case"]["id"], "case-3")

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

    def test_restore_local_cases_rehydrates_saved_history_without_live_counters(self) -> None:
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
            self.assertEqual(get_app_state()["failure_count"], 0)
            self.assertEqual(get_app_state()["case_count"], 0)
            self.assertIsNone(get_app_state()["latest_case"])
            self.assertEqual(get_app_state()["saved_case_count"], 2)

    def test_stats_scope_api_filters_today_vs_all_cases(self) -> None:
        today = datetime.now().astimezone().isoformat()
        old = "2020-01-01T00:00:00+00:00"
        record_case({
            "id": "old",
            "created_at": old,
            "source_kind": "hook",
            "suspected_skill": "mcp__old.tool",
            "source_target": "mcp__old.tool",
            "summary": "old",
            "wasted_tokens": 3,
        })
        record_case({
            "id": "today",
            "created_at": today,
            "source_kind": "hook",
            "suspected_skill": "mcp__today.tool",
            "source_target": "mcp__today.tool",
            "summary": "today",
            "wasted_tokens": 5,
        })

        with run_test_server() as base_url:
            today_state = api_json(base_url, "/state")
            today_cases = api_json(base_url, "/cases")
            all_state = api_json(base_url, "/control/stats-scope", "POST", {"scope": "all"})["state"]
            all_cases = api_json(base_url, "/cases")

        self.assertEqual(today_state["stats_scope"], "today")
        self.assertEqual(today_state["display_failure_count"], 1)
        self.assertEqual([case["id"] for case in today_cases["cases"]], ["today"])
        self.assertEqual(all_state["stats_scope"], "all")
        self.assertEqual(all_state["display_failure_count"], 2)
        self.assertEqual([case["id"] for case in all_cases["cases"]], ["today", "old"])
        self.assertEqual(normalize_stats_scope("bad"), "today")

    def test_stats_scope_persists_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temp_dir}, clear=False):
                with run_test_server() as base_url:
                    api_json(base_url, "/control/stats-scope", "POST", {"scope": "all"})

                self.assertEqual(load_stats_scope(), "all")
                save_local_config({"stats_scope": "bad"})
                self.assertEqual(load_stats_scope(), "today")

    def test_clear_old_cases_keeps_today_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cases.jsonl"
            save_local_case({"id": "old", "created_at": "2020-01-01T00:00:00+00:00", "summary": "old"}, path)
            save_local_case({"id": "today", "created_at": datetime.now().astimezone().isoformat(), "summary": "today"}, path)

            removed = clear_old_cases(path)
            cases = load_local_cases(path)

        self.assertEqual(removed, 1)
        self.assertEqual([case["id"] for case in cases], ["today"])

    def test_success_stats_persist_today_and_all_without_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "stats.json"
            record_success_stat("mcp__ok.tool", 7, path)
            today = load_success_stats(path, "today")
            all_time = load_success_stats(path, "all")

        self.assertEqual(today["mcp__ok.tool"]["success"], 1)
        self.assertEqual(all_time["mcp__ok.tool"]["tokens"], 7)
        self.assertNotIn("user_input", json.dumps(all_time))

    def test_clear_old_success_stats_keeps_today_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "stats.json"
            path.write_text(json.dumps({
                "targets": {
                    "mcp__ok.tool": {
                        "success": 2,
                        "tokens": 5,
                        "last_seen": "2020-01-01T00:00:00+00:00",
                        "days": {
                            "2020-01-01": {"success": 1, "tokens": 2, "last_seen": "2020-01-01T00:00:00+00:00"},
                            datetime.now().astimezone().date().isoformat(): {"success": 1, "tokens": 3, "last_seen": datetime.now().astimezone().isoformat()},
                        },
                    }
                }
            }), encoding="utf-8")

            removed = clear_old_success_stats(path)
            stats = load_success_stats(path, "all")

        self.assertEqual(removed, 1)
        self.assertEqual(stats["mcp__ok.tool"]["success"], 1)
        self.assertEqual(stats["mcp__ok.tool"]["tokens"], 3)

    def test_case_review_prompt_is_redacted_and_minimal(self) -> None:
        prompt = build_case_review_prompt({
            "id": "case-1",
            "source_kind": "codex_session",
            "category": "skill_mcp_hard_failure",
            "suspected_skill": "mcp__node_repl.js",
            "trigger_text": "api_key=abc123456789 failed",
            "wasted_tokens": 99,
            "recent_events": [
                {"event_type": "McpToolUse", "tool_name": "mcp__node_repl.js"},
                {"event_type": "ToolOutput", "ai_output": "token: localtoken123"},
            ],
        })

        self.assertIn("case-1", prompt)
        self.assertIn("mcp__node_repl.js", prompt)
        self.assertNotIn("abc123456789", prompt)
        self.assertNotIn("localtoken123", prompt)
        self.assertLess(len(prompt), 6000)

    def test_case_reviewer_parses_codex_output_schema(self) -> None:
        calls = []
        cwd_values = []

        def fake_runner(args, input, capture_output, text, timeout, cwd):
            calls.append(args)
            cwd_values.append(cwd)
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text(
                json.dumps({
                    "verdict": "tool_fault",
                    "confidence": 0.75,
                    "attributed_target": "mcp__node_repl.js",
                    "attributed_call_id": "call_1",
                    "evidence_spans": [0],
                    "one_sentence_summary": "MCP timed out.",
                    "reason": "The trace shows a tool timeout.",
                }),
                encoding="utf-8",
            )
            return mock.Mock(returncode=0, stdout="", stderr="")

        review = CaseReviewer(runner=fake_runner).review({
            "id": "case-1",
            "source_kind": "codex_session",
            "category": "skill_mcp_hard_failure",
            "suspected_skill": "mcp__node_repl.js",
            "trigger_text": "timeout",
            "recent_events": [],
        })

        self.assertEqual(review["status"], "reviewed")
        self.assertEqual(review["verdict"], "tool_fault")
        self.assertEqual(review["attributed_target"], "mcp__node_repl.js")
        self.assertEqual(review["attributed_call_id"], "call_1")
        self.assertEqual(review["evidence_spans"], [0])
        self.assertEqual(len(calls), 1)
        self.assertIn("codex", calls[0][0])
        self.assertIn("--ephemeral", calls[0])
        self.assertIn("--sandbox", calls[0])
        self.assertIn("read-only", calls[0])
        self.assertIn("vibe-dashcam-review-", Path(cwd_values[0]).name)
        self.assertEqual(review["model"], "default")

    def test_case_reviewer_passes_selected_model_to_codex_exec(self) -> None:
        calls = []

        def fake_runner(args, input, capture_output, text, timeout, cwd):
            calls.append(args)
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text(
                json.dumps({
                    "verdict": "unclear",
                    "confidence": 0.4,
                    "attributed_target": "",
                    "attributed_call_id": "",
                    "evidence_spans": [],
                    "one_sentence_summary": "Needs a closer read.",
                    "reason": "Short trace.",
                }),
                encoding="utf-8",
            )
            return mock.Mock(returncode=0, stdout="", stderr="")

        review = CaseReviewer(runner=fake_runner, review_model="gpt-5-codex").review({
            "id": "case-1",
            "source_kind": "codex_session",
            "category": "skill_mcp_hard_failure",
            "suspected_skill": "mcp__node_repl.js",
            "recent_events": [],
        })

        self.assertEqual(review["model"], "gpt-5-codex")
        self.assertIn("--model", calls[0])
        self.assertEqual(calls[0][calls[0].index("--model") + 1], "gpt-5-codex")

    def test_review_payload_keeps_structural_trace_fields(self) -> None:
        payload = build_case_review_payload({
            "id": "case-1",
            "source_kind": "codex_session",
            "source_target": "mcp__node_repl.js",
            "recent_events": [
                {"event_type": "McpToolUse", "tool_name": "mcp__node_repl.js", "call_id": "call_1", "observed_at": "2026-01-01T00:00:00+00:00"},
                {"event_type": "ToolOutput", "ai_output": "Exit code: 1\nboom", "call_id": "call_1"},
            ],
        })

        self.assertEqual(payload["trace"][0]["event_index"], 0)
        self.assertEqual(payload["trace"][0]["observed_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(payload["trace"][1]["exit_code"], 1)

    def test_prompts_state_attribution_boundaries(self) -> None:
        review_prompt = build_case_review_prompt({
            "id": "case-1",
            "source_kind": "codex_session",
            "recent_events": [{"event_type": "McpToolUse", "tool_name": "mcp__node_repl.js"}],
        })
        soft_prompt = build_soft_feedback_prompt(
            "上一版方向偏了",
            [{"event_type": "McpToolUse", "tool_name": "mcp__node_repl.js"}],
        )

        self.assertIn("Attribute only to a target/call_id present in the trace", review_prompt)
        self.assertIn("evidence_spans", review_prompt)
        self.assertIn("The user does not need to name a Skill", soft_prompt)
        self.assertIn("Broad critiques", soft_prompt)
        self.assertIn("trace cannot connect", soft_prompt)

    def test_semantic_feedback_classifier_passes_selected_model(self) -> None:
        calls = []

        def fake_runner(args, input, capture_output, text, timeout, cwd):
            calls.append(args)
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text(
                json.dumps({
                    "negative": True,
                    "category": "ai_failed_previous_result",
                    "confidence": 0.8,
                    "reason": "The user is correcting the previous Skill/MCP result.",
                }),
                encoding="utf-8",
            )
            return mock.Mock(returncode=0, stdout="", stderr="")

        decision = CodexFeedbackClassifier(
            runner=fake_runner,
            review_model="gpt-5.3-codex-spark",
        ).classify(
            "上一轮方向不对，按原始需求重来",
            [{"event_type": "McpToolUse", "tool_name": "mcp__node_repl.js"}],
        )

        self.assertTrue(decision.negative)
        self.assertIn("--model", calls[0])
        self.assertEqual(calls[0][calls[0].index("--model") + 1], "gpt-5.3-codex-spark")

    def test_windows_subprocesses_hide_console_windows(self) -> None:
        kwargs = _subprocess_no_window_kwargs()
        if os.name == "nt":
            self.assertEqual(kwargs.get("creationflags"), subprocess.CREATE_NO_WINDOW)
        else:
            self.assertEqual(kwargs, {})

    def test_review_model_api_updates_state_and_sanitizes_invalid_values(self) -> None:
        with run_test_server() as base_url:
            ok = api_json(base_url, "/control/review-model", "POST", {"model": "gpt-5-codex"})
            bad = api_json(base_url, "/control/review-model", "POST", {"model": "bad model name"})
            state = api_json(base_url, "/state")

        self.assertEqual(ok["review_model"], "gpt-5-codex")
        self.assertEqual(bad["review_model"], "default")
        self.assertEqual(state["review_model"], "default")
        self.assertEqual(normalize_review_model(""), "default")

    def test_review_model_api_persists_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temp_dir}, clear=False):
                self.assertEqual(load_review_model(), "gpt-5.3-codex-spark")

                with run_test_server() as base_url:
                    api_json(base_url, "/control/review-model", "POST", {"model": "gpt-5-codex"})

                self.assertEqual(load_review_model(), "gpt-5-codex")

                save_local_config({"review_model": "bad model name"})
                self.assertEqual(load_review_model(), "default")

    def test_discovers_codex_review_models_from_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
            (root / "review.config.toml").write_text('[model_providers]\nmodel = "o3"\n', encoding="utf-8")

            models = discover_codex_review_models(
                root,
                selected="gpt-5-codex",
                runner=mock.Mock(return_value=mock.Mock(returncode=1, stdout="")),
            )
            default_model = default_codex_review_model(
                root,
                runner=mock.Mock(return_value=mock.Mock(returncode=1, stdout="")),
            )

        self.assertEqual(models, ["default", "gpt-5.3-codex-spark", "gpt-5.5", "o3", "gpt-5-codex"])
        self.assertEqual(default_model, "gpt-5.3-codex-spark")

    def test_discovers_codex_review_models_from_cli_catalog(self) -> None:
        catalog = {
            "models": [
                {"slug": "gpt-5.5", "visibility": "list"},
                {"slug": "gpt-5.4", "visibility": "list"},
                {"slug": "codex-auto-review", "visibility": "hide"},
                {"slug": "bad model name", "visibility": "list"},
            ]
        }

        runner = mock.Mock(return_value=mock.Mock(
            returncode=0,
            stdout=json.dumps(catalog),
        ))
        with tempfile.TemporaryDirectory() as temp_dir:
            models = discover_codex_review_models(Path(temp_dir), runner=runner)

        self.assertEqual(models, ["default", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex-spark"])
        runner.assert_called_once()

    def test_discovers_codex_models_from_availability_nux(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config.toml").write_text(
                '[tui.model_availability_nux]\n"gpt-5.5" = 1\n"o3" = 1\n',
                encoding="utf-8",
            )

            models = discover_codex_review_models(
                root,
                runner=mock.Mock(return_value=mock.Mock(returncode=1, stdout="")),
            )

        self.assertEqual(models, ["default", "gpt-5.3-codex-spark", "gpt-5.5", "o3"])

    def test_case_reviewer_failure_is_unavailable(self) -> None:
        def failing_runner(args, input, capture_output, text, timeout, cwd):
            return mock.Mock(returncode=1, stdout="", stderr="boom")

        review = CaseReviewer(runner=failing_runner).review({
            "id": "case-1",
            "source_kind": "codex_session",
            "category": "skill_mcp_hard_failure",
            "suspected_skill": "mcp__node_repl.js",
            "recent_events": [],
        })

        self.assertEqual(review["status"], "unavailable")

    def test_update_case_review_uses_cache_and_persists_once(self) -> None:
        class FakeReviewer:
            def __init__(self) -> None:
                self.calls = 0

            def review(self, case):
                self.calls += 1
                return {
                    "status": "reviewed",
                    "verdict": "unclear",
                    "confidence": 0.4,
                    "attributed_target": "",
                    "attributed_call_id": "",
                    "evidence_spans": [],
                    "one_sentence_summary": "Needs human read.",
                    "reason": "Short trace.",
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temp_dir}, clear=False):
                DashcamServer.persist_cases = True
                case = record_case({
                    "suspected_skill": "mcp__node_repl.js",
                    "summary": "captured",
                    "wasted_tokens": 12,
                })
                reviewer = FakeReviewer()

                first = update_case_review(case["id"], reviewer)
                second = update_case_review(case["id"], reviewer)

                path = Path(temp_dir) / "VibeDashcam" / "cases.jsonl"
                saved = load_local_cases(path)

        self.assertEqual(first["status"], "reviewed")
        self.assertEqual(second["status"], "reviewed")
        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(saved[0]["case_review"]["verdict"], "unclear")

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

    def test_diagnostics_reports_local_codex_and_storage_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / ".codex"
            (root / "sessions").mkdir(parents=True)
            with mock.patch("vibe_dashcam.vibe_dashcam.shutil.which", return_value="C:\\Codex\\codex.exe"):
                diagnostics = build_diagnostics(root)

        self.assertTrue(diagnostics["ok"])
        self.assertTrue(diagnostics["codex_cli"]["found"])
        self.assertTrue(diagnostics["codex_home"]["exists"])
        self.assertTrue(diagnostics["codex_sessions"]["exists"])
        self.assertIn("review_model_options", diagnostics)

    def test_create_http_server_reports_port_bind_failure(self) -> None:
        with mock.patch(
            "vibe_dashcam.vibe_dashcam.http.server.ThreadingHTTPServer",
            side_effect=OSError("address already in use"),
        ):
            server = create_http_server()

        state = get_app_state()
        self.assertIsNone(server)
        self.assertFalse(state["listening"])
        self.assertIn("address already in use", state["server_error"])

    def test_cors_allows_dev_origin_and_rejects_unknown_origin_for_private_api(self) -> None:
        with run_test_server() as base_url:
            ok_status, ok_headers, _ = api_status(
                base_url,
                "/state",
                headers={"Origin": "http://127.0.0.1:1420"},
            )
            tauri_status, tauri_headers, _ = api_status(
                base_url,
                "/state",
                headers={"Origin": "tauri://localhost"},
            )
            bad_status, _, _ = api_status(
                base_url,
                "/state",
                headers={"Origin": "https://example.com"},
            )
            no_origin_status, _, _ = api_status(base_url, "/state")

        self.assertEqual(ok_status, 200)
        self.assertEqual(ok_headers["Access-Control-Allow-Origin"], "http://127.0.0.1:1420")
        self.assertEqual(tauri_status, 200)
        self.assertEqual(tauri_headers["Access-Control-Allow-Origin"], "tauri://localhost")
        self.assertEqual(bad_status, 403)
        self.assertEqual(no_origin_status, 403)

    def test_hook_requires_token_and_rejects_browser_origin(self) -> None:
        payload = {
            "event_type": "McpToolUse",
            "tool_name": "mcp__demo.timeout",
            "ai_output": "timeout",
        }
        with run_test_server() as base_url:
            missing_status, _, _ = api_status(base_url, "/hook", "POST", payload)
            browser_status, _, _ = api_status(
                base_url,
                "/hook",
                "POST",
                payload,
                headers={
                    "Origin": "https://example.com",
                    HOOK_TOKEN_HEADER: DashcamServer.hook_token or "",
                },
            )
            ok_status, _, _ = api_status(
                base_url,
                "/hook",
                "POST",
                payload,
                headers={HOOK_TOKEN_HEADER: DashcamServer.hook_token or ""},
            )
            state = api_json(base_url, "/state")

        self.assertEqual(missing_status, 401)
        self.assertEqual(browser_status, 403)
        self.assertEqual(ok_status, 200)
        self.assertEqual(state["failure_count"], 1)

    def test_hook_rejects_oversized_payload_before_ingest(self) -> None:
        with run_test_server() as base_url:
            status, _, _ = api_status(
                base_url,
                "/hook",
                "POST",
                {"user_input": "x" * MAX_REQUEST_BYTES},
                headers={HOOK_TOKEN_HEADER: DashcamServer.hook_token or ""},
            )
            state = api_json(base_url, "/state")

        self.assertEqual(status, 413)
        self.assertEqual(state["event_count"], 0)

    def test_packaging_contract_keeps_core_sidecar_names_aligned(self) -> None:
        root = Path(__file__).resolve().parents[1]
        package = json.loads((root / "desktop" / "package.json").read_text(encoding="utf-8"))
        tauri = json.loads((root / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        main_rs = (root / "desktop" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")

        build_core = package["scripts"]["build:core"]
        resources = tauri["bundle"]["resources"]

        self.assertIn("--name vibe-dashcam-core", build_core)
        self.assertIn("--onedir", build_core)
        self.assertIn("--noconsole", build_core)
        self.assertIn("--distpath src-tauri/bin", build_core)
        self.assertEqual(resources["bin/vibe-dashcam-core"], "vibe-dashcam-core")
        self.assertIn('dir.join("vibe-dashcam-core").join(name)', main_rs)
        self.assertIn('if core_health_ok() {\n        set_core_status(app, "ok");', main_rs)
        self.assertNotIn("get_launch_on_startup", main_rs)
        self.assertNotIn("set_launch_on_startup", main_rs)
        self.assertNotIn("Vibe-Dashcam.lnk", main_rs)
        self.assertFalse(tauri["app"]["windows"][0]["alwaysOnTop"])
        self.assertLessEqual(tauri["app"]["windows"][0]["height"], 400)
        self.assertEqual(tauri["bundle"]["targets"], ["nsis"])
        self.assertIn('windows_subsystem = "windows"', main_rs)


def drain_queue(target_queue) -> None:
    while not target_queue.empty():
        target_queue.get_nowait()


def json_line(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    unittest.main()
