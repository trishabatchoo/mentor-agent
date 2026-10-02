"""Offline tests for tools.py's student-context backend selection: the JSON
backend (public example) and the Notion backend (local profile), the new
get_latest_session_notes tool, and end-to-end log/trace privacy through the
agent loop with the Notion backend.

Notion responses come from tests/fake_notion.py; the Anthropic client is a
scripted fake. agent.py reads ANTHROPIC_API_KEY at import time, so a
placeholder is set first.
"""

import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ANTHROPIC_API_KEY", "placeholder-key-for-offline-tests")

import agent  # noqa: E402
import observability  # noqa: E402
import tools  # noqa: E402
from fake_notion import FakeNotion, block, child_page, page, tab  # noqa: E402
from notion_client import NotionConfigError, NotionRequestError  # noqa: E402
from notion_repository import StudentNotFoundError  # noqa: E402

EXAMPLE_STUDENTS = tools.PROJECT_DIR / "data" / "students.example.json"


def patch_backend(test, backend, repository=None):
    patches = [
        unittest.mock.patch.object(tools, "STUDENT_CONTEXT_BACKEND", backend),
        unittest.mock.patch.object(tools, "_notion_repository", repository),
    ]
    for p in patches:
        p.start()
        test.addCleanup(p.stop)


class ToolSchemaTests(unittest.TestCase):
    def test_three_tools_with_name_inputs(self):
        names = [tool["name"] for tool in tools.TOOLS]
        self.assertEqual(
            names,
            ["get_student_context", "get_latest_session_notes", "get_path_context"],
        )
        notes_tool = tools.TOOLS[1]
        self.assertEqual(notes_tool["input_schema"]["required"], ["name"])

    def test_path_tool_schema_unchanged(self):
        self.assertEqual(
            tools.TOOLS[2],
            {
                "name": "get_path_context",
                "description": (
                    "Retrieve the complete reference guide for a student's learning path. "
                    "Use the student's current project to identify the relevant section "
                    "within the returned guide."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The student's full learning path.",
                        },
                    },
                    "required": ["path"],
                },
            },
        )


class JsonBackendTests(unittest.TestCase):
    """The example profile keeps using data/students.example.json."""

    def setUp(self):
        patch_backend(self, "json")
        p = unittest.mock.patch.object(tools, "STUDENTS_PATH", EXAMPLE_STUDENTS)
        p.start()
        self.addCleanup(p.stop)
        self.from_env = unittest.mock.patch.object(
            tools.NotionStudentRepository, "from_env",
            side_effect=AssertionError("Notion must not be used"),
        )
        self.from_env.start()
        self.addCleanup(self.from_env.stop)

    def test_default_backend_is_json(self):
        # A fresh interpreter, so the import-time default is what's tested.
        env = {k: v for k, v in os.environ.items()
               if k not in ("STUDENT_CONTEXT_BACKEND", "NOTION_API_KEY",
                            "NOTION_PARENT_PAGE_ID", "STUDENTS_DATA_PATH",
                            "REFERENCE_ROOT")}
        output = subprocess.run(
            [sys.executable, "-c", "import tools; print(tools.student_context_backend())"],
            cwd=tools.PROJECT_DIR, env=env, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(output, "json")

    def test_student_context_response_is_unchanged(self):
        record = json.loads(EXAMPLE_STUDENTS.read_text(encoding="utf-8"))["Jordan"]
        expected = dict(record)
        del expected["student_id"]
        self.assertEqual(
            tools.execute_tool("get_student_context", {"name": "Jordan"}), expected
        )

    def test_latest_session_notes_return_previous_session(self):
        record = json.loads(EXAMPLE_STUDENTS.read_text(encoding="utf-8"))["Jordan"]
        self.assertEqual(
            tools.execute_tool("get_latest_session_notes", {"name": " Jordan "}),
            {"latest_session": record["previous_session"]},
        )

    def test_latest_session_notes_without_previous_session(self):
        with unittest.mock.patch.object(
            tools, "load_students", return_value={"Casey": {"path": "P"}}
        ):
            self.assertEqual(
                tools.get_latest_session_notes("Casey"), {"latest_session": None}
            )

    def test_unknown_student_returns_none(self):
        self.assertIsNone(tools.get_latest_session_notes("Nobody"))
        self.assertIsNone(tools.get_student_context("Nobody"))

    def test_student_id_still_comes_from_json(self):
        self.assertEqual(tools.get_student_id("Jordan"), "example_student_001")

    def test_runs_without_notion_environment(self):
        with unittest.mock.patch.dict(os.environ):
            for name in ("NOTION_API_KEY", "NOTION_PARENT_PAGE_ID"):
                os.environ.pop(name, None)
            self.assertIsNotNone(tools.get_student_context("Jordan"))
            self.assertIsNotNone(tools.get_latest_session_notes("Jordan"))


class NotionBackendTests(unittest.TestCase):
    """The local profile reads Notion and never touches the JSON file."""

    def setUp(self):
        self.repository = unittest.mock.Mock()
        patch_backend(self, "notion", self.repository)
        p = unittest.mock.patch.object(
            tools, "load_students",
            side_effect=AssertionError("JSON must not be read"),
        )
        p.start()
        self.addCleanup(p.stop)

    def test_tools_dispatch_to_notion(self):
        self.repository.get_student_context.return_value = {"title": "T"}
        self.repository.get_latest_session_notes.return_value = {"latest_session": None}
        self.assertEqual(
            tools.execute_tool("get_student_context", {"name": "Casey"}), {"title": "T"}
        )
        self.assertEqual(
            tools.execute_tool("get_latest_session_notes", {"name": "Casey"}),
            {"latest_session": None},
        )
        self.repository.get_student_context.assert_called_once_with("Casey")
        self.repository.get_latest_session_notes.assert_called_once_with("Casey")

    def test_student_id_is_none(self):
        self.assertIsNone(tools.get_student_id("Casey"))
        self.repository.assert_not_called()

    def test_notion_errors_propagate_without_json_fallback(self):
        for error in (StudentNotFoundError("x"), NotionRequestError("search", "5xx")):
            with self.subTest(error=type(error).__name__):
                self.repository.get_student_context.side_effect = error
                with self.assertRaises(type(error)):
                    tools.get_student_context("Casey")

    def test_path_tool_unaffected(self):
        result = tools.execute_tool(
            "get_path_context", {"path": next(iter(tools.PATH_FILES))}
        )
        self.assertIn("reference", result)
        self.repository.assert_not_called()


class RepositoryCreationTests(unittest.TestCase):
    def setUp(self):
        patch_backend(self, "notion", None)

    def test_repository_created_lazily_once(self):
        sentinel = object()
        with unittest.mock.patch.object(
            tools.NotionStudentRepository, "from_env", return_value=sentinel
        ) as from_env:
            self.assertIs(tools.get_notion_repository(), sentinel)
            self.assertIs(tools.get_notion_repository(), sentinel)
        from_env.assert_called_once_with()

    def test_missing_configuration_fails_clearly(self):
        with unittest.mock.patch.dict(os.environ):
            for name in ("NOTION_API_KEY", "NOTION_PARENT_PAGE_ID"):
                os.environ.pop(name, None)
            with self.assertRaises(NotionConfigError):
                tools.get_student_context("Casey")

    def test_unknown_backend_is_rejected(self):
        patch_backend(self, "sqlite", None)
        with self.assertRaises(ValueError):
            tools.get_student_context("Casey")
        with self.assertRaises(ValueError):
            tools.get_student_id("Casey")


class NotionAgentPrivacyTests(unittest.TestCase):
    """Runs the real agent loop and tool dispatch against fake Notion data
    planted with markers; none may reach logs or the JSONL trace."""

    NAME = "SENTINEL-STUDENT-NAME"
    MARKERS = (
        NAME,
        "SENTINEL-PAGE-ID",
        "SENTINEL-SESSION-ID",
        "SENTINEL-PROFILE-TEXT",
        "SENTINEL-SESSION-NOTE",
        "SENTINEL-TRANSCRIPT-LINE",
        "SENTINEL-NOTION-API-KEY",
        "api.notion.com",
    )

    def setUp(self):
        fake = FakeNotion()
        fake.search_results = [page("SENTINEL-PAGE-ID", self.NAME)]
        fake.children["SENTINEL-PAGE-ID"] = [
            block("b1", "paragraph", "Path: SENTINEL-PROFILE-TEXT"),
            child_page("SENTINEL-SESSION-ID", "2026-09-29T11:00:00-04:00"),
        ]
        fake.children["SENTINEL-SESSION-ID"] = [tab("tab")]
        fake.children["tab"] = [
            block("notes", "paragraph", "Notes", has_children=True),
            block("tr", "paragraph", "Transcript", has_children=True),
        ]
        fake.children["notes"] = [block("n", "paragraph", "SENTINEL-SESSION-NOTE")]
        fake.children["tr"] = [block("t", "paragraph", "SENTINEL-TRANSCRIPT-LINE")]
        self.fake = fake
        patch_backend(self, "notion", fake.repository())

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.trace_path = Path(self._tmp.name) / "logs" / "agent-runs.jsonl"

        tool_turn = SimpleNamespace(
            id="msg_1", model="test-model", stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="toolu_ctx",
                                name="get_student_context", input={"name": self.NAME}),
                SimpleNamespace(type="tool_use", id="toolu_notes",
                                name="get_latest_session_notes", input={"name": self.NAME}),
            ],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        final = SimpleNamespace(
            id="msg_2", model="test-model", stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="done")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        self.create = unittest.mock.Mock(side_effect=[tool_turn, final])
        fake_client = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        for p in (
            unittest.mock.patch.object(agent, "Anthropic", return_value=fake_client),
            unittest.mock.patch.object(agent, "build_instructions", return_value="system"),
            unittest.mock.patch.object(observability, "DEFAULT_TRACE_PATH", self.trace_path),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_logs_and_trace_hold_no_private_notion_data(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            agent.run_agent(student_name=self.NAME, mode="pre_session")
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)

        logs = stream.getvalue()
        trace = self.trace_path.read_text(encoding="utf-8")
        for marker in self.MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, logs)
                self.assertNotIn(marker, trace)

        events = [json.loads(line) for line in trace.splitlines()]
        tool_events = [e for e in events if e["event"] == "tool_execution"]
        self.assertEqual(
            [e["tool_name"] for e in tool_events],
            ["get_student_context", "get_latest_session_notes"],
        )
        self.assertTrue(all(e["status"] == "success" for e in tool_events))

        # The model receives the notes but never ids or transcript text.
        tool_results = self.create.call_args.kwargs["messages"][2]["content"]
        sent = json.dumps([r["content"] for r in tool_results])
        self.assertIn("SENTINEL-SESSION-NOTE", sent)
        for private in ("SENTINEL-PAGE-ID", "SENTINEL-SESSION-ID", "SENTINEL-TRANSCRIPT-LINE"):
            self.assertNotIn(private, sent)
        self.assertNotIn("tr", self.fake.fetched_block_ids())


if __name__ == "__main__":
    unittest.main()
