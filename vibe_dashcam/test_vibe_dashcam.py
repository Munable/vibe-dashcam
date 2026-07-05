import os
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_dashcam.vibe_dashcam import (
    CodexSessionTailer,
    DashcamServer,
    extract_codex_session_event,
    FeedbackClassifier,
    FailureSignalDetector,
    RecentBehaviorBuffer,
    SummaryGenerator,
    _parse_toml_model,
    scan_configurations,
)

HOOK_PATH = Path(__file__).resolve().parents[1] / ".codex" / "hooks" / "vibe_dashcam_hook.py"


def load_hook_module():
    spec = importlib.util.spec_from_file_location("vibe_dashcam_hook", HOOK_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load hook module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VibeDashcamTests(unittest.TestCase):
    def test_recent_buffer_keeps_latest_sanitized_events(self) -> None:
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

    def test_toml_scan_reads_model_without_exposing_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config.toml"
            config.write_text(
                'api_key = "sk-should-not-leak"\nmodel = "gpt-5.3-codex-spark"\n',
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
                "api_key": "sk-secret",
                "tool_name": "Bash",
            },
            "UserPromptSubmit",
        )

        self.assertEqual(payload["client"], "codex")
        self.assertEqual(payload["event_type"], "UserPromptSubmit")
        self.assertEqual(payload["user_input"], "不对，重来")
        self.assertEqual(payload["tool_name"], "Bash")
        self.assertNotIn("api_key", payload)

    def test_extract_codex_session_user_message(self) -> None:
        event = extract_codex_session_event({
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "不对，重来",
            },
        })

        self.assertEqual(event["client"], "codex")
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

        DashcamServer.ingest_payload({
            "client": "codex",
            "event_type": "McpToolUse",
            "tool_name": "mcp__node_repl.js",
            "ai_output": "timeout",
        })

        summary = DashcamServer.summary_queue.get_nowait()
        self.assertEqual(summary["category"], "skill_mcp_hard_failure")

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


def drain_queue(target_queue) -> None:
    while not target_queue.empty():
        target_queue.get_nowait()


def json_line(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    unittest.main()
