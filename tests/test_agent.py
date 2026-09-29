"""Offline tests for agent.py's run tracing and log privacy.

The Anthropic client is replaced with a scripted fake and execute_tool with
a stub, so these tests never call the API or read student data. The trace
file is redirected to a temporary directory.

agent.py reads ANTHROPIC_API_KEY at import time, so a placeholder is set
before importing it if the variable is not already present. The fake
client never uses it.
"""

import io
import hashlib
import json
import logging
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ANTHROPIC_API_KEY", "placeholder-key-for-offline-tests")

import agent  # noqa: E402
import observability  # noqa: E402

# Sentinel strings planted in every private input and output. None of
# them may appear in the trace file or in any terminal log record.
STUDENT = "SENTINEL-STUDENT-NAME"
TRANSCRIPT = "SENTINEL-TRANSCRIPT-TEXT"
SESSION_DT = "SENTINEL-SESSION-DATETIME"
TOOL_INPUT = "SENTINEL-TOOL-INPUT"
TOOL_RESULT = "SENTINEL-TOOL-RESULT"
RESPONSE_TEXT = "SENTINEL-MODEL-RESPONSE"
THINKING = "SENTINEL-THINKING"
EXC_MESSAGE = "SENTINEL-EXCEPTION-MESSAGE"
API_KEY = "SENTINEL-API-KEY"
PROMPT = "SENTINEL-SYSTEM-PROMPT"
STUDENT_ID = "student_test_042"
SENTINELS = (
    STUDENT, TRANSCRIPT, SESSION_DT, TOOL_INPUT, TOOL_RESULT,
    RESPONSE_TEXT, THINKING, EXC_MESSAGE, API_KEY, PROMPT,
)


class FakeAPIError(Exception):
    pass


def text_block(text=RESPONSE_TEXT):
    return SimpleNamespace(type="text", text=text)


def thinking_block():
    return SimpleNamespace(type="thinking", thinking=THINKING)


def tool_block(tool_use_id, name="get_student_context", value=TOOL_INPUT):
    return SimpleNamespace(
        type="tool_use", id=tool_use_id, name=name, input={"name": value}
    )


def response(stop_reason, content, input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        id="msg_test",
        model="test-model",
        stop_reason=stop_reason,
        content=content,
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens
        ),
    )


class AgentTracingTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.trace_path = Path(self._tmp.name) / "logs" / "agent-runs.jsonl"

        self.create = unittest.mock.Mock()
        fake_client = SimpleNamespace(
            messages=SimpleNamespace(create=self.create)
        )
        self.execute_tool = unittest.mock.Mock(
            return_value={"record": TOOL_RESULT}
        )

        patches = [
            unittest.mock.patch.object(
                observability, "DEFAULT_TRACE_PATH", self.trace_path
            ),
            unittest.mock.patch.object(
                agent, "Anthropic", return_value=fake_client
            ),
            unittest.mock.patch.object(agent, "execute_tool", self.execute_tool),
            unittest.mock.patch.object(
                agent, "build_instructions", return_value=PROMPT
            ),
            unittest.mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": API_KEY}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def run_agent(self, **overrides):
        kwargs = dict(
            student_name=STUDENT,
            mode="post_session",
            transcript=TRANSCRIPT,
            session_datetime=SESSION_DT,
            student_id=STUDENT_ID,
        )
        kwargs.update(overrides)
        return agent.run_agent(**kwargs)

    def run_capturing_logs(self, expect=None):
        """Run the agent with all log records captured at DEBUG level.

        Returns (result_or_exception, captured_log_output).
        """
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            if expect is None:
                outcome = self.run_agent()
            else:
                with self.assertRaises(expect) as ctx:
                    self.run_agent()
                outcome = ctx.exception
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
        return outcome, stream.getvalue()

    def events(self):
        if not self.trace_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.trace_path.read_text(encoding="utf-8").splitlines()
        ]

    def assert_no_sentinels(self, text):
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)


class SuccessfulRunTests(AgentTracingTestCase):
    def setUp(self):
        super().setUp()
        self.first = response(
            "tool_use",
            [thinking_block(), tool_block("toolu_a"), tool_block("toolu_b")],
            input_tokens=100,
            output_tokens=20,
        )
        self.final = response(
            "end_turn", [text_block()], input_tokens=300, output_tokens=60
        )
        self.create.side_effect = [self.first, self.final]

    def test_returns_final_response_unchanged(self):
        self.assertIs(self.run_agent(), self.final)

    def test_event_sequence_and_shared_run_id(self):
        self.run_agent()
        events = self.events()
        self.assertEqual(
            [e["event"] for e in events],
            [
                "run_started",
                "model_response",
                "tool_execution",
                "tool_execution",
                "model_response",
                "run_completed",
            ],
        )
        self.assertEqual(len({e["run_id"] for e in events}), 1)
        self.assertEqual({e["mode"] for e in events}, {"post_session"})
        self.assertEqual({e["model"] for e in events}, {agent.model})

    def test_model_and_tool_event_fields(self):
        self.run_agent()
        events = self.events()
        first_model, tool_a, tool_b, second_model = events[1:5]

        self.assertEqual(first_model["turn"], 1)
        self.assertEqual(first_model["stop_reason"], "tool_use")
        self.assertEqual(first_model["input_tokens"], 100)
        self.assertEqual(first_model["output_tokens"], 20)
        self.assertIsInstance(first_model["duration_ms"], float)
        self.assertEqual(second_model["turn"], 2)
        self.assertEqual(second_model["stop_reason"], "end_turn")

        self.assertEqual(tool_a["tool_use_id"], "toolu_a")
        self.assertEqual(tool_b["tool_use_id"], "toolu_b")
        for tool_event in (tool_a, tool_b):
            self.assertEqual(tool_event["tool_name"], "get_student_context")
            self.assertEqual(tool_event["status"], "success")
            self.assertIsNone(tool_event["error_type"])
            self.assertEqual(tool_event["turn"], 1)

    def test_run_completed_token_totals(self):
        self.run_agent()
        final = self.events()[-1]
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["turns"], 2)
        self.assertEqual(final["total_input_tokens"], 400)
        self.assertEqual(final["total_output_tokens"], 80)
        self.assertEqual(final["tool_calls"], 2)

    def test_api_message_structure_is_unchanged(self):
        self.run_agent()
        self.assertEqual(self.create.call_count, 2)
        kwargs = self.create.call_args.kwargs
        self.assertEqual(kwargs["model"], agent.model)
        self.assertEqual(kwargs["max_tokens"], 5000)
        self.assertIs(kwargs["tools"], agent.TOOLS)
        self.assertEqual(kwargs["system"], PROMPT)

        messages = kwargs["messages"]
        self.assertEqual(
            messages[0],
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Student name: {STUDENT}"},
                    {
                        "type": "text",
                        "text": (
                            f"Session date and time: {SESSION_DT}\n\n"
                            f"Transcript:\n{TRANSCRIPT}"
                        ),
                    },
                ],
            },
        )
        self.assertEqual(
            messages[1], {"role": "assistant", "content": self.first.content}
        )
        result_json = json.dumps({"record": TOOL_RESULT})
        self.assertEqual(
            messages[2],
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_a",
                        "content": result_json,
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_b",
                        "content": result_json,
                    },
                ],
            },
        )
        self.assertEqual(len(messages), 3)

    def test_trace_and_logs_contain_no_private_data(self):
        _, log_output = self.run_capturing_logs()
        self.assert_no_sentinels(self.trace_path.read_text(encoding="utf-8"))
        self.assert_no_sentinels(log_output)
        # Terminal logs keep the operational detail that is safe to show.
        self.assertIn("toolu_a", log_output)
        self.assertIn("Tool input fields: ['name']", log_output)

    def test_unexpected_stop_reason_retries_and_completes(self):
        self.create.side_effect = [
            response("stop_sequence", [text_block()]),
            self.final,
        ]
        self.assertIs(self.run_agent(), self.final)
        final = self.events()[-1]
        self.assertEqual(final["event"], "run_completed")
        self.assertEqual(final["turns"], 2)


class FailedRunTests(AgentTracingTestCase):
    def assert_run_failed(self, error_type, failure_reason):
        final = self.events()[-1]
        self.assertEqual(final["event"], "run_failed")
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["error_type"], error_type)
        self.assertEqual(final["failure_reason"], failure_reason)
        self.assertEqual(len({e["run_id"] for e in self.events()}), 1)
        return final

    def test_tool_error(self):
        error = ValueError(EXC_MESSAGE)
        self.execute_tool.side_effect = error
        self.create.side_effect = [
            response("tool_use", [tool_block("toolu_x")], 70, 7)
        ]

        raised, log_output = self.run_capturing_logs(expect=ValueError)

        self.assertIs(raised, error)
        tool_event = self.events()[2]
        self.assertEqual(tool_event["event"], "tool_execution")
        self.assertEqual(tool_event["status"], "error")
        self.assertEqual(tool_event["error_type"], "ValueError")
        self.assertEqual(tool_event["tool_use_id"], "toolu_x")
        final = self.assert_run_failed("ValueError", "tool_error")
        self.assertEqual(final["total_input_tokens"], 70)
        self.assertEqual(final["total_output_tokens"], 7)
        self.assertEqual(final["tool_calls"], 1)
        self.assert_no_sentinels(self.trace_path.read_text(encoding="utf-8"))
        self.assert_no_sentinels(log_output)

    def test_api_error(self):
        error = FakeAPIError(EXC_MESSAGE)
        self.create.side_effect = error

        raised, log_output = self.run_capturing_logs(expect=FakeAPIError)

        self.assertIs(raised, error)
        self.assertEqual(
            [e["event"] for e in self.events()], ["run_started", "run_failed"]
        )
        final = self.assert_run_failed("FakeAPIError", "api_error")
        self.assertEqual(final["turns"], 0)
        self.assert_no_sentinels(self.trace_path.read_text(encoding="utf-8"))
        self.assert_no_sentinels(log_output)

    def test_max_tokens(self):
        self.create.side_effect = [response("max_tokens", [text_block()])]
        with self.assertRaisesRegex(
            RuntimeError, "^Claude reached max_tokens before finishing.$"
        ):
            self.run_agent()
        self.assert_run_failed("RuntimeError", "max_tokens")

    def test_max_turns(self):
        self.create.side_effect = [
            response("stop_sequence", [text_block()])
            for _ in range(agent.MAX_TURNS)
        ]
        with self.assertRaisesRegex(
            RuntimeError, f"^Agent did not finish within {agent.MAX_TURNS} turns.$"
        ):
            self.run_agent()
        final = self.assert_run_failed("RuntimeError", "max_turns")
        self.assertEqual(final["turns"], agent.MAX_TURNS)

    def test_unexpected_error(self):
        # A tool result that is not JSON-serializable fails after the tool
        # itself succeeded, outside any classified failure site.
        self.execute_tool.return_value = object()
        self.create.side_effect = [response("tool_use", [tool_block("toolu_y")])]
        with self.assertRaises(TypeError):
            self.run_agent()
        self.assertEqual(self.events()[2]["status"], "success")
        self.assert_run_failed("TypeError", "unexpected_error")

    def test_keyboard_interrupt_is_not_caught(self):
        self.execute_tool.side_effect = KeyboardInterrupt
        self.create.side_effect = [response("tool_use", [tool_block("toolu_z")])]
        with self.assertRaises(KeyboardInterrupt):
            self.run_agent()
        self.assertNotIn(
            "run_failed", [e["event"] for e in self.events()]
        )

    def test_trace_write_failure_does_not_change_run_outcome(self):
        # Make the logs directory path a regular file so every write fails.
        self.trace_path.parent.parent.mkdir(parents=True, exist_ok=True)
        self.trace_path.parent.write_text("not a directory")
        final = response("end_turn", [text_block()])
        self.create.side_effect = [final]
        self.assertIs(self.run_agent(), final)


class InputMetadataTests(AgentTracingTestCase):
    INPUT_FIELDS = ("student_id", "transcript_char_count", "transcript_sha256")

    def setUp(self):
        super().setUp()
        self.create.side_effect = [response("end_turn", [text_block()])]

    def test_post_session_records_student_id_and_transcript_fingerprint(self):
        self.run_agent()
        started = self.events()[0]
        self.assertEqual(started["event"], "run_started")
        self.assertEqual(started["student_id"], STUDENT_ID)
        self.assertEqual(started["transcript_char_count"], len(TRANSCRIPT))
        self.assertEqual(
            started["transcript_sha256"],
            hashlib.sha256(TRANSCRIPT.encode("utf-8")).hexdigest(),
        )

    def test_input_metadata_appears_only_on_run_started(self):
        self.run_agent()
        for event in self.events()[1:]:
            for field in self.INPUT_FIELDS:
                self.assertNotIn(field, event, event["event"])

    def test_modes_without_transcript_record_null(self):
        for mode in ("pre_session", "new_student"):
            with self.subTest(mode=mode):
                self.create.side_effect = [response("end_turn", [text_block()])]
                # A transcript passed to these modes is never sent to the
                # model, so it is not fingerprinted either.
                self.run_agent(mode=mode)
                started = self.events()[-3]
                self.assertEqual(started["event"], "run_started")
                self.assertEqual(started["mode"], mode)
                self.assertEqual(started["student_id"], STUDENT_ID)
                self.assertIsNone(started["transcript_char_count"])
                self.assertIsNone(started["transcript_sha256"])

    def test_omitted_student_id_records_null(self):
        kwargs = dict(
            student_name=STUDENT,
            mode="post_session",
            transcript=TRANSCRIPT,
            session_datetime=SESSION_DT,
        )
        agent.run_agent(**kwargs)
        self.assertIsNone(self.events()[0]["student_id"])

    def test_student_id_is_not_sent_to_the_model(self):
        self.run_agent()
        self.assertNotIn(STUDENT_ID, repr(self.create.call_args))

    def test_name_and_transcript_are_not_recorded(self):
        _, log_output = self.run_capturing_logs()
        trace = self.trace_path.read_text(encoding="utf-8")
        self.assert_no_sentinels(trace)
        self.assert_no_sentinels(log_output)


class ValidationTests(AgentTracingTestCase):
    def test_invalid_arguments_create_no_trace_events(self):
        cases = [
            dict(mode="not_a_mode"),
            dict(transcript=None),
            dict(session_datetime=None),
        ]
        for overrides in cases:
            with self.subTest(**overrides):
                with self.assertRaises(ValueError):
                    self.run_agent(**overrides)
        self.assertFalse(self.trace_path.exists())
        self.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
