"""Offline unit tests for observability.py: event serialization, field
allowlisting, token aggregation, failure reasons, and write-failure
tolerance. No agent code is imported and no API calls are made.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import observability  # noqa: E402
from observability import EVENT_FIELDS, RunTracer  # noqa: E402

COMMON_FIELDS = {"timestamp", "run_id", "event", "mode", "model"}


class TracerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.trace_path = Path(self._tmp.name) / "logs" / "agent-runs.jsonl"
        self.tracer = RunTracer(
            mode="post_session", model="test-model", trace_path=self.trace_path
        )

    def tearDown(self):
        self._tmp.cleanup()

    def read_events(self):
        return [
            json.loads(line)
            for line in self.trace_path.read_text(encoding="utf-8").splitlines()
        ]


class SerializationTests(TracerTestCase):
    def test_creates_parent_directory_and_writes_one_json_object_per_line(self):
        self.tracer.run_started()
        self.tracer.run_completed()

        lines = self.trace_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIsInstance(json.loads(line), dict)

    def test_every_event_has_exactly_common_plus_allowlisted_fields(self):
        self.tracer.run_started()
        self.tracer.model_response(1, 12.5, "tool_use", 100, 20)
        self.tracer.tool_execution(1, "get_student_context", "toolu_1", 0.4)
        self.tracer.run_completed()
        self.tracer.run_failed("RuntimeError")

        events = self.read_events()
        self.assertEqual(
            [e["event"] for e in events],
            [
                "run_started",
                "model_response",
                "tool_execution",
                "run_completed",
                "run_failed",
            ],
        )
        for event in events:
            expected = COMMON_FIELDS | set(EVENT_FIELDS[event["event"]])
            self.assertEqual(set(event), expected, event["event"])
            self.assertEqual(event["mode"], "post_session")
            self.assertEqual(event["model"], "test-model")

    def test_timestamp_is_utc_iso8601(self):
        self.tracer.run_started()
        timestamp = self.read_events()[0]["timestamp"]
        self.assertTrue(timestamp.endswith("+00:00"), timestamp)

    def test_model_response_fields(self):
        self.tracer.model_response(2, 123.456, "end_turn", 300, 45)
        event = self.read_events()[0]
        self.assertEqual(event["turn"], 2)
        self.assertEqual(event["duration_ms"], 123.456)
        self.assertEqual(event["stop_reason"], "end_turn")
        self.assertEqual(event["input_tokens"], 300)
        self.assertEqual(event["output_tokens"], 45)


class InputMetadataTests(TracerTestCase):
    def test_run_started_serializes_input_metadata(self):
        self.tracer.run_started(
            student_id="student_007",
            transcript_char_count=8421,
            transcript_sha256="ab" * 32,
        )
        event = self.read_events()[0]
        self.assertEqual(event["student_id"], "student_007")
        self.assertEqual(event["transcript_char_count"], 8421)
        self.assertEqual(event["transcript_sha256"], "ab" * 32)

    def test_run_started_defaults_input_metadata_to_null(self):
        self.tracer.run_started()
        event = self.read_events()[0]
        self.assertIsNone(event["student_id"])
        self.assertIsNone(event["transcript_char_count"])
        self.assertIsNone(event["transcript_sha256"])

    def test_input_metadata_is_not_on_other_events(self):
        for event, fields in EVENT_FIELDS.items():
            if event == "run_started":
                continue
            for field in (
                "student_id", "transcript_char_count", "transcript_sha256"
            ):
                self.assertNotIn(field, fields, event)


class FingerprintTests(unittest.TestCase):
    TRANSCRIPT = "Mentor: How did the join go?\nStudent: It worked after I fixed the keys.\n"

    def test_identical_transcripts_have_identical_fingerprints(self):
        copy = "".join(list(self.TRANSCRIPT))  # equal value, distinct object
        self.assertEqual(
            observability.text_sha256(self.TRANSCRIPT),
            observability.text_sha256(copy),
        )

    def test_changed_transcripts_have_different_fingerprints(self):
        original = observability.text_sha256(self.TRANSCRIPT)
        for changed in (
            self.TRANSCRIPT.replace("join", "Join"),
            self.TRANSCRIPT + " ",
            self.TRANSCRIPT[:-1],
        ):
            with self.subTest(changed=changed):
                self.assertNotEqual(original, observability.text_sha256(changed))

    def test_fingerprint_is_hex_sha256_of_utf8(self):
        digest = observability.text_sha256("café")
        self.assertEqual(len(digest), 64)
        self.assertEqual(
            digest,
            "850f7dc43910ff890f8879c0ed26fe697c93a067ad93a7d50f466a7028a9bf4e",
        )


class RunIdTests(TracerTestCase):
    def test_all_events_share_the_tracer_run_id(self):
        self.tracer.run_started()
        self.tracer.model_response(1, 1.0, "end_turn", 1, 1)
        self.tracer.run_completed()

        run_ids = {e["run_id"] for e in self.read_events()}
        self.assertEqual(run_ids, {self.tracer.run_id})

    def test_separate_tracers_get_distinct_run_ids(self):
        other = RunTracer("post_session", "test-model", self.trace_path)
        self.assertNotEqual(self.tracer.run_id, other.run_id)


class AggregationTests(TracerTestCase):
    def test_run_completed_totals(self):
        self.tracer.run_started()
        self.tracer.model_response(1, 1.0, "tool_use", 100, 10)
        self.tracer.tool_execution(1, "get_student_context", "toolu_1", 1.0)
        self.tracer.tool_execution(1, "get_path_context", "toolu_2", 1.0)
        self.tracer.model_response(2, 1.0, "end_turn", 250, 40)
        self.tracer.run_completed()

        final = self.read_events()[-1]
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["turns"], 2)
        self.assertEqual(final["total_input_tokens"], 350)
        self.assertEqual(final["total_output_tokens"], 50)
        self.assertEqual(final["tool_calls"], 2)
        self.assertIsInstance(final["total_duration_ms"], float)
        self.assertGreaterEqual(final["total_duration_ms"], 0)


class ToolEventTests(TracerTestCase):
    def test_successful_tool_event(self):
        self.tracer.tool_execution(1, "get_student_context", "toolu_1", 2.5)
        event = self.read_events()[0]
        self.assertEqual(event["status"], "success")
        self.assertIsNone(event["error_type"])
        self.assertEqual(event["tool_name"], "get_student_context")
        self.assertEqual(event["tool_use_id"], "toolu_1")
        self.assertEqual(event["turn"], 1)

    def test_failed_tool_event(self):
        self.tracer.tool_execution(
            1, "get_path_context", "toolu_2", 2.5, error_type="ValueError"
        )
        event = self.read_events()[0]
        self.assertEqual(event["status"], "error")
        self.assertEqual(event["error_type"], "ValueError")


class FailureReasonTests(TracerTestCase):
    def test_defaults_to_unexpected_error(self):
        self.tracer.run_failed("TypeError")
        event = self.read_events()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_type"], "TypeError")
        self.assertEqual(event["failure_reason"], "unexpected_error")

    def test_marked_reason_is_recorded_and_first_mark_wins(self):
        self.tracer.mark_failure("tool_error")
        self.tracer.mark_failure("api_error")
        self.tracer.run_failed("ValueError")
        self.assertEqual(self.read_events()[0]["failure_reason"], "tool_error")

    def test_unknown_reason_is_rejected(self):
        with self.assertRaises(ValueError):
            self.tracer.mark_failure("something free-form")


class PrivacyTests(TracerTestCase):
    def test_non_allowlisted_fields_are_dropped(self):
        record = self.tracer.build_event(
            "tool_execution",
            tool_name="get_student_context",
            tool_input={"name": "SECRET-STUDENT"},
            result="SECRET-RESULT",
            message="SECRET-MESSAGE",
        )
        serialized = json.dumps(record)
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("tool_input", record)
        self.assertNotIn("result", record)
        self.assertNotIn("message", record)

    def test_non_scalar_values_are_nulled(self):
        record = self.tracer.build_event(
            "tool_execution",
            tool_name={"nested": "SECRET-NESTED"},
            tool_use_id=["SECRET-LIST"],
        )
        self.assertIsNone(record["tool_name"])
        self.assertIsNone(record["tool_use_id"])
        self.assertNotIn("SECRET", json.dumps(record))


class WriteFailureTests(unittest.TestCase):
    def test_unwritable_path_warns_once_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A regular file where the logs directory should be makes
            # mkdir/open fail with an OSError.
            blocker = Path(tmp) / "logs"
            blocker.write_text("not a directory")
            tracer = RunTracer(
                "post_session", "test-model", blocker / "agent-runs.jsonl"
            )
            with self.assertLogs(observability.logger, "WARNING") as logs:
                tracer.run_started()
                tracer.run_completed()
            self.assertEqual(len(logs.records), 1)


if __name__ == "__main__":
    unittest.main()
