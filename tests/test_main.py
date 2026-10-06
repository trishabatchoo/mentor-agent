"""Offline tests for main.py: student_id resolution before run_agent, and
end-to-end trace privacy for the student name and transcript file.

The Anthropic client is replaced with a fake, the student file with a
temporary one, and the trace path with a temporary directory. agent.py
reads ANTHROPIC_API_KEY at import time, so a placeholder is set first.
"""

import io
import json
import logging
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ANTHROPIC_API_KEY", "placeholder-key-for-offline-tests")

import agent  # noqa: E402
import main  # noqa: E402
import observability  # noqa: E402
import tools  # noqa: E402

STUDENT = "SENTINEL-STUDENT-NAME"
STUDENT_ID = "student_test_042"
TRANSCRIPT = "SENTINEL-TRANSCRIPT-TEXT\r\nsecond line\r\n"
TRANSCRIPT_DIR = "SENTINEL-TRANSCRIPT-DIR"
TRANSCRIPT_FILE = "SENTINEL-TRANSCRIPT-FILE.txt"


class MainTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)

        self.students_path = tmp / "students.json"
        self.write_students({STUDENT: {"student_id": STUDENT_ID, "path": "P"}})

        transcript_dir = tmp / TRANSCRIPT_DIR
        transcript_dir.mkdir()
        self.transcript_path = transcript_dir / TRANSCRIPT_FILE
        # Written in binary so the CRLF line endings survive on disk.
        self.transcript_path.write_bytes(TRANSCRIPT.encode("utf-8"))

        self.trace_path = tmp / "logs" / "agent-runs.jsonl"

        patches = [
            unittest.mock.patch.object(tools, "STUDENTS_PATH", self.students_path),
            unittest.mock.patch.object(
                observability, "DEFAULT_TRACE_PATH", self.trace_path
            ),
            unittest.mock.patch.dict(
                os.environ,
                {
                    "MENTOR_STUDENT_NAME": STUDENT,
                    "TRANSCRIPT_PATH": str(self.transcript_path),
                    "SESSION_DATETIME": "September 8, 2026 11:00 AM ET",
                },
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def write_students(self, students):
        self.students_path.write_text(json.dumps(students), encoding="utf-8")

    def run_main(self):
        with redirect_stdout(io.StringIO()):
            main.main()


class StudentIdResolutionTests(MainTestCase):
    def setUp(self):
        super().setUp()
        final = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])
        self.run_agent = unittest.mock.Mock(return_value=final)
        patch = unittest.mock.patch.object(main, "run_agent", self.run_agent)
        patch.start()
        self.addCleanup(patch.stop)

    def test_resolved_student_id_is_passed_to_run_agent(self):
        self.run_main()
        kwargs = self.run_agent.call_args.kwargs
        self.assertEqual(kwargs["student_id"], STUDENT_ID)
        self.assertEqual(kwargs["student_name"], STUDENT)

    def test_missing_student_id_passes_none_and_warns_without_name(self):
        self.write_students({STUDENT: {"path": "P"}})
        with self.assertLogs(main.logger, "WARNING") as logs:
            self.run_main()
        self.assertIsNone(self.run_agent.call_args.kwargs["student_id"])
        self.assertNotIn(STUDENT, "\n".join(logs.output))


class EndToEndTraceTests(MainTestCase):
    def setUp(self):
        super().setUp()
        final = SimpleNamespace(
            id="msg_test",
            model="test-model",
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        self.create = unittest.mock.Mock(return_value=final)
        fake_client = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        patch = unittest.mock.patch.object(agent, "Anthropic", return_value=fake_client)
        patch.start()
        self.addCleanup(patch.stop)

    def run_main_capturing_logs(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            self.run_main()
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
        return stream.getvalue()

    def test_trace_records_safe_metadata_only(self):
        log_output = self.run_main_capturing_logs()
        raw_trace = self.trace_path.read_text(encoding="utf-8")
        started = json.loads(raw_trace.splitlines()[0])

        # The model receives the newline-normalized text read_text()
        # returns; the fingerprint is of exactly that string.
        normalized = TRANSCRIPT.replace("\r\n", "\n")
        sent = self.create.call_args.kwargs["messages"][0]["content"][1]["text"]
        self.assertTrue(sent.endswith(f"Transcript:\n{normalized}"))
        self.assertEqual(started["student_id"], STUDENT_ID)
        self.assertEqual(started["transcript_char_count"], len(normalized))
        self.assertEqual(
            started["transcript_sha256"], observability.text_sha256(normalized)
        )

        for private in (
            STUDENT,
            "SENTINEL-TRANSCRIPT-TEXT",
            TRANSCRIPT_DIR,
            TRANSCRIPT_FILE,
            str(self.transcript_path),
        ):
            with self.subTest(private=private):
                self.assertNotIn(private, raw_trace)
                self.assertNotIn(private, log_output)


class LoggingConfigurationTests(unittest.TestCase):
    """LOG_LEVEL=DEBUG must not let the real Anthropic SDK log request
    bodies. The SDK runs for real against an offline mock transport."""

    def setUp(self):
        root = logging.getLogger()
        saved_root = (root.level, list(root.handlers))
        saved_levels = {
            name: logging.getLogger(name).level for name in main.THIRD_PARTY_LOGGERS
        }

        def restore():
            root.setLevel(saved_root[0])
            root.handlers[:] = saved_root[1]
            for name, level in saved_levels.items():
                logging.getLogger(name).setLevel(level)

        self.addCleanup(restore)
        # basicConfig only applies its level to a root logger with no handlers.
        root.handlers[:] = []

    def test_debug_level_does_not_log_sdk_request_bodies(self):
        import anthropic
        import httpx2

        def respond(request):
            return httpx2.Response(
                200,
                json={
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-model",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        stream = io.StringIO()
        main.configure_logging("DEBUG")
        logging.getLogger().addHandler(logging.StreamHandler(stream))
        logging.getLogger("main").debug("project debug record")

        client = anthropic.Anthropic(
            api_key="placeholder-key-for-offline-tests",
            http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
        )
        client.messages.create(
            model="test-model",
            max_tokens=10,
            system="SENTINEL-SYSTEM-PROMPT",
            messages=[{"role": "user", "content": "SENTINEL-TRANSCRIPT-TEXT"}],
        )

        output = stream.getvalue()
        self.assertIn("project debug record", output)
        self.assertNotIn("SENTINEL-SYSTEM-PROMPT", output)
        self.assertNotIn("SENTINEL-TRANSCRIPT-TEXT", output)

    def test_quieter_levels_are_kept_for_third_party_loggers(self):
        main.configure_logging("WARNING")
        for name in main.THIRD_PARTY_LOGGERS:
            with self.subTest(logger=name):
                self.assertEqual(logging.getLogger(name).level, logging.WARNING)


if __name__ == "__main__":
    unittest.main()
