"""Offline tests for Notion retrieval observability: notion_request and
notion_retrieval events from the client and repository, their correlation
with the surrounding agent run, error reporting, per-run isolation of the
cached repository, and privacy of traces, logs, and exceptions.

Notion responses come from tests/fake_notion.py and the Anthropic client is
a scripted fake, so no request leaves the process. agent.py reads
ANTHROPIC_API_KEY at import time, so a placeholder is set first.
"""

import io
import json
import logging
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ANTHROPIC_API_KEY", "placeholder-key-for-offline-tests")

import agent  # noqa: E402
import observability  # noqa: E402
import tools  # noqa: E402
from fake_notion import (  # noqa: E402
    FAKE_API_KEY,
    PARENT_ID,
    FakeNotion,
    block,
    child_page,
    page,
    tab,
)
from notion_client import (  # noqa: E402
    MalformedNotionResponseError,
    NotionClient,
    NotionRequestError,
    PaginationLimitExceededError,
)
from notion_repository import (  # noqa: E402
    AmbiguousStudentMatchError,
    NotionStudentRepository,
    StudentNotFoundError,
    TraversalDepthExceededError,
)
from observability import EVENT_FIELDS, RunTracer  # noqa: E402

COMMON_FIELDS = {"timestamp", "run_id", "event", "mode", "model"}

NAME = "SENTINEL-STUDENT-NAME"
QUERY = "SENTINEL-SEARCH-QUERY"
STUDENT_PAGE = "SENTINEL-PAGE-ID"
SESSION_PAGE = "SENTINEL-SESSION-ID"
PROFILE_BLOCK = "SENTINEL-BLOCK-ID"
PROFILE_TEXT = "SENTINEL-PROFILE-TEXT"
SESSION_NOTE = "SENTINEL-SESSION-NOTE"
NOTION_TRANSCRIPT = "SENTINEL-NOTION-TRANSCRIPT"
CURSOR = "SENTINEL-CURSOR-"
EXC_MESSAGE = "SENTINEL-EXCEPTION-MESSAGE"
ERROR_BODY = "SENTINEL-ERROR-BODY"
TRANSCRIPT = "SENTINEL-RUN-TRANSCRIPT"
SESSION_DT = "SENTINEL-SESSION-DATETIME"
PRIVATE_FILE = "SENTINEL-PRIVATE-FILENAME"
SESSION_TITLE = "2026-09-29T11:00:00-04:00"

MARKERS = (
    NAME, QUERY, STUDENT_PAGE, SESSION_PAGE, PROFILE_BLOCK, PROFILE_TEXT,
    SESSION_NOTE, NOTION_TRANSCRIPT, CURSOR, EXC_MESSAGE, ERROR_BODY,
    TRANSCRIPT, SESSION_DT, PRIVATE_FILE, FAKE_API_KEY, PARENT_ID,
    PARENT_ID.replace("-", ""), "api.notion.com", "/v1/", SESSION_TITLE,
)


def quiet_logs(test):
    handler = logging.NullHandler()
    for name in ("notion_repository", "notion_client", "agent", "observability"):
        logging.getLogger(name).addHandler(handler)
        test.addCleanup(logging.getLogger(name).removeHandler, handler)


def capture_logs(fn):
    """Run fn with every logger captured at DEBUG; return (outcome, output)."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        try:
            outcome = fn()
        except Exception as exc:
            outcome = exc
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
    return outcome, stream.getvalue()


def build_fake(page_size=1):
    """A student page with profile text and one tabbed session, paginated
    one item per page so every listing takes several requests."""
    fake = FakeNotion(page_size=page_size, cursor_prefix=CURSOR)
    fake.search_results = [
        page("other-1", "Someone Else"),
        page(STUDENT_PAGE, NAME),
    ]
    fake.children[STUDENT_PAGE] = [
        block(PROFILE_BLOCK, "paragraph", f"Path: {PROFILE_TEXT}"),
        block("b2", "paragraph", f"Current project: {PROFILE_TEXT}"),
        child_page(SESSION_PAGE, SESSION_TITLE),
    ]
    fake.children[SESSION_PAGE] = [tab("tab")]
    fake.children["tab"] = [
        block("notes", "paragraph", "Notes", has_children=True),
        block("tr", "paragraph", "Transcript", has_children=True),
    ]
    fake.children["notes"] = [block("n", "paragraph", SESSION_NOTE)]
    fake.children["tr"] = [block("t", "paragraph", NOTION_TRANSCRIPT)]
    return fake


class Recorder:
    """An observer that keeps every event it receives."""

    def __init__(self):
        self.events = []

    def __call__(self, event, fields):
        self.events.append((event, dict(fields)))

    def named(self, name):
        return [fields for event, fields in self.events if event == name]


# -- client ---------------------------------------------------------------


class ClientRequestEventTests(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_paginated_search_is_one_event_with_accurate_counts(self):
        fake = FakeNotion(page_size=2)
        fake.search_results = [{"object": "page", "id": f"p{i}"} for i in range(5)]
        rec = Recorder()
        fake.client().search_pages(QUERY, observer=rec)

        self.assertEqual(len(rec.events), 1)
        event, fields = rec.events[0]
        self.assertEqual(event, "notion_request")
        self.assertEqual(fields["operation"], "search_pages")
        self.assertEqual(fields["status"], "success")
        self.assertEqual(fields["status_category"], "2xx")
        self.assertEqual(fields["request_count"], 3)
        self.assertEqual(fields["pages_fetched"], 3)
        self.assertEqual(fields["results_returned"], 5)
        self.assertIsNone(fields["error_type"])
        self.assertIsInstance(fields["duration_ms"], float)
        self.assertEqual(len(fake.requests), 3)

    def test_paginated_block_listing_is_one_event(self):
        fake = FakeNotion(page_size=2)
        fake.children["blk"] = [block(f"b{i}", "paragraph", "x") for i in range(4)]
        rec = Recorder()
        fake.client().list_block_children("blk", observer=rec)
        (fields,) = rec.named("notion_request")
        self.assertEqual(fields["operation"], "list_block_children")
        self.assertEqual(
            (fields["request_count"], fields["pages_fetched"], fields["results_returned"]),
            (2, 2, 4),
        )

    def assert_error_event(self, client_call, exc_type, category, requests, pages):
        rec = Recorder()
        with self.assertRaises(exc_type):
            client_call(rec)
        (fields,) = rec.named("notion_request")
        self.assertEqual(fields["status"], "error")
        self.assertEqual(fields["status_category"], category)
        self.assertEqual(fields["error_type"], exc_type.__name__)
        self.assertEqual(fields["request_count"], requests)
        self.assertEqual(fields["pages_fetched"], pages)
        self.assertIsNone(fields["results_returned"])

    def test_http_error_categories(self):
        for status, category in ((404, "4xx"), (429, "4xx"), (500, "5xx"), (503, "5xx")):
            with self.subTest(status=status):
                fake = FakeNotion()
                fake.fail_status = status
                self.assert_error_event(
                    lambda rec: fake.client().list_block_children("b", observer=rec),
                    NotionRequestError, category, 1, 0,
                )

    def test_error_on_a_later_page_counts_earlier_pages(self):
        fake = FakeNotion(page_size=1)
        fake.search_results = [{"object": "page", "id": "p"}] * 3
        responses = iter([None, None, 500])
        original = fake.handle

        def handle(request):
            status = next(responses)
            if status:
                fake.requests.append(request)
                return httpx2.Response(status, json={})
            return original(request)

        client = NotionClient(FAKE_API_KEY, transport=httpx2.MockTransport(handle))
        self.assert_error_event(
            lambda rec: client.search_pages(QUERY, observer=rec),
            NotionRequestError, "5xx", 3, 2,
        )

    def test_transport_error(self):
        def handler(request):
            raise httpx2.ConnectError(EXC_MESSAGE)

        client = NotionClient(FAKE_API_KEY, transport=httpx2.MockTransport(handler))
        self.assert_error_event(
            lambda rec: client.search_pages(QUERY, observer=rec),
            NotionRequestError, "transport_error", 1, 0,
        )

    def test_malformed_responses(self):
        for label, response in {
            "not json": httpx2.Response(200, content=b"<html>"),
            "no has_more": httpx2.Response(200, json={"results": []}),
        }.items():
            with self.subTest(label):
                client = NotionClient(
                    FAKE_API_KEY, transport=httpx2.MockTransport(lambda r: response)
                )
                self.assert_error_event(
                    lambda rec: client.list_block_children("b", observer=rec),
                    MalformedNotionResponseError, "malformed_response", 1, 0,
                )

    def test_pagination_limit_keeps_last_success_category(self):
        fake = FakeNotion(page_size=1)
        fake.children["b"] = [block(f"b{i}", "paragraph", "x") for i in range(5)]
        self.assert_error_event(
            lambda rec: fake.client(max_pages=3).list_block_children("b", observer=rec),
            PaginationLimitExceededError, "2xx", 3, 3,
        )

    def test_missing_block_id_reports_no_request(self):
        self.assert_error_event(
            lambda rec: FakeNotion().client().list_block_children("", observer=rec),
            MalformedNotionResponseError, None, 0, 0,
        )

    def test_failing_observer_does_not_affect_the_operation(self):
        fake = FakeNotion()
        fake.children["b"] = [block("b1", "paragraph", "x")]

        def broken(event, fields):
            raise RuntimeError(EXC_MESSAGE)

        result, output = capture_logs(
            lambda: fake.client().list_block_children("b", observer=broken)
        )
        self.assertEqual([b["id"] for b in result], ["b1"])
        self.assertIn("Notion observer failed (RuntimeError)", output)
        self.assertNotIn(EXC_MESSAGE, output)


# -- repository -----------------------------------------------------------


class RepositoryRetrievalEventTests(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.fake = build_fake()

    def call(self, method, name=NAME, repo=None, **repo_kwargs):
        repo = repo or self.fake.repository(**repo_kwargs)
        rec = Recorder()
        try:
            result = getattr(repo, method)(name, observer=rec)
        except Exception as exc:
            result = exc
        return result, rec

    def test_student_context_events(self):
        context, rec = self.call("get_student_context")
        self.assertEqual(context["path"], PROFILE_TEXT)

        names = [event for event, _ in rec.events]
        self.assertEqual(
            names, ["notion_request", "notion_request", "notion_retrieval"]
        )
        search, listing = rec.named("notion_request")
        self.assertEqual(search["operation"], "search_pages")
        self.assertEqual((search["request_count"], search["results_returned"]), (2, 2))
        self.assertEqual(listing["operation"], "list_block_children")
        self.assertEqual(listing["request_count"], 3)

        (summary,) = rec.named("notion_retrieval")
        self.assertEqual(summary["retrieval_type"], "student_context")
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["requests_made"], 5)
        self.assertEqual(summary["pages_fetched"], 5)
        self.assertEqual(summary["blocks_examined"], 3)
        self.assertEqual(summary["maximum_depth_reached"], 0)
        self.assertIs(summary["cache_hit"], False)
        self.assertIs(summary["path_found"], True)
        self.assertIs(summary["project_found"], True)
        for field in ("session_found", "notes_source", "notes_char_count", "error_type"):
            self.assertIsNone(summary[field], field)
        self.assertEqual(set(summary), set(EVENT_FIELDS["notion_retrieval"]) - {"turn", "tool_use_id"})

    def test_latest_session_events(self):
        result, rec = self.call("get_latest_session_notes")
        notes = result["latest_session"]["notes"]
        (summary,) = rec.named("notion_retrieval")
        self.assertEqual(summary["retrieval_type"], "latest_session_notes")
        self.assertEqual(summary["status"], "success")
        self.assertIs(summary["session_found"], True)
        self.assertEqual(summary["notes_source"], "notes_branch")
        self.assertEqual(summary["notes_char_count"], len(notes))
        # student page, session page, tab, notes branch -- not the transcript.
        self.assertEqual(len(rec.named("notion_request")), 5)
        self.assertEqual(summary["maximum_depth_reached"], 2)
        self.assertEqual(summary["blocks_examined"], 3 + 1 + 2 + 1)
        self.assertIsNone(summary["path_found"])
        self.assertNotIn(SESSION_NOTE, json.dumps(rec.events))

    def test_page_notes_source(self):
        self.fake.children[SESSION_PAGE] = [block("p", "paragraph", SESSION_NOTE)]
        _, rec = self.call("get_latest_session_notes")
        (summary,) = rec.named("notion_retrieval")
        self.assertEqual(summary["notes_source"], "page")
        self.assertEqual(summary["notes_char_count"], len(SESSION_NOTE))

    def test_no_session(self):
        self.fake.children[STUDENT_PAGE] = [block("b", "paragraph", "x")]
        result, rec = self.call("get_latest_session_notes")
        self.assertEqual(result, {"latest_session": None})
        (summary,) = rec.named("notion_retrieval")
        self.assertEqual(summary["status"], "no_session")
        self.assertIs(summary["session_found"], False)
        self.assertEqual(summary["notes_source"], "none")
        self.assertIsNone(summary["notes_char_count"])
        self.assertIsNone(summary["error_type"])

    def test_not_found_and_ambiguous(self):
        cases = {
            "not_found": ([page("x", "Someone Else")], StudentNotFoundError),
            "ambiguous": (
                [page(STUDENT_PAGE, NAME), page("dup", NAME)], AmbiguousStudentMatchError
            ),
        }
        for status, (results, exc_type) in cases.items():
            for method in ("get_student_context", "get_latest_session_notes"):
                with self.subTest(status, method=method):
                    self.fake.search_results = results
                    err, rec = self.call(method)
                    self.assertIsInstance(err, exc_type)
                    (request,) = rec.named("notion_request")
                    self.assertEqual(request["status"], "success")
                    (summary,) = rec.named("notion_retrieval")
                    self.assertEqual(summary["status"], status)
                    self.assertEqual(summary["error_type"], exc_type.__name__)
                    self.assertIs(summary["cache_hit"], False)
                    self.assertIsNone(summary["maximum_depth_reached"])

    def test_blank_name_is_not_found_without_requests(self):
        err, rec = self.call("get_student_context", name="  ")
        self.assertIsInstance(err, StudentNotFoundError)
        (summary,) = rec.events
        self.assertEqual(summary[1]["status"], "not_found")
        self.assertEqual(summary[1]["requests_made"], 0)
        self.assertIsNone(summary[1]["cache_hit"])

    def test_traversal_depth_error(self):
        self.fake.children["notes"] = [block("deep", "paragraph", "x", has_children=True)]
        self.fake.children["deep"] = []
        err, rec = self.call("get_latest_session_notes", max_traversal_depth=2)
        self.assertIsInstance(err, TraversalDepthExceededError)
        self.assertTrue(all(f["status"] == "success" for f in rec.named("notion_request")))
        (summary,) = rec.named("notion_retrieval")
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["error_type"], "TraversalDepthExceededError")
        self.assertEqual(summary["maximum_depth_reached"], 2)
        self.assertIs(summary["session_found"], True)
        self.assertIsNone(summary["notes_source"])

    def test_api_error_reports_each_layer_once(self):
        del self.fake.children["notes"]  # the fake answers 404
        err, rec = self.call("get_latest_session_notes")
        self.assertIsInstance(err, NotionRequestError)
        errors = [(e, f["error_type"]) for e, f in rec.events if f["status"] != "success"]
        self.assertEqual(
            errors,
            [("notion_request", "NotionRequestError"), ("notion_retrieval", "NotionRequestError")],
        )
        self.assertEqual(rec.named("notion_request")[-1]["status_category"], "4xx")

    def test_original_exception_object_is_preserved(self):
        error = NotionRequestError("search", "5xx", 503)
        client = unittest.mock.Mock()
        client.search_pages.side_effect = error
        repo = NotionStudentRepository(client, PARENT_ID)
        for method in ("get_student_context", "get_latest_session_notes"):
            with self.subTest(method=method):
                raised, rec = self.call(method, repo=repo)
                self.assertIs(raised, error)
                self.assertEqual(rec.named("notion_retrieval")[0]["status"], "error")

    def test_cache_hit_skips_search(self):
        repo = self.fake.repository()
        self.call("get_student_context", repo=repo)
        _, rec = self.call("get_latest_session_notes", repo=repo)
        self.assertNotIn("search_pages", [f["operation"] for f in rec.named("notion_request")])
        self.assertIs(rec.named("notion_retrieval")[0]["cache_hit"], True)

    def test_no_events_without_an_observer(self):
        repo = self.fake.repository()
        with unittest.mock.patch("notion_repository.notify_observer") as notify:
            repo.get_student_context(NAME)
            repo.get_latest_session_notes(NAME)
        # The per-call counters still see the client's events, but have
        # no caller observer to forward them to.
        self.assertTrue(notify.call_args_list)
        for call in notify.call_args_list:
            self.assertIsNone(call.args[0])
        self.assert_holds_no_observer(repo)

    def test_failing_observer_does_not_affect_retrieval(self):
        repo = self.fake.repository()

        def broken(event, fields):
            raise RuntimeError(EXC_MESSAGE)

        result = repo.get_latest_session_notes(NAME, observer=broken)
        self.assertIn(SESSION_NOTE, result["latest_session"]["notes"])

    def assert_holds_no_observer(self, repo):
        for obj in (repo, repo._client):
            for value in vars(obj).values():
                self.assertFalse(callable(value) and not isinstance(value, type), value)


# -- tracer ---------------------------------------------------------------


class TracerNotionEventTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.trace_path = Path(self._tmp.name) / "agent-runs.jsonl"
        self.tracer = RunTracer("pre_session", "test-model", self.trace_path)

    def events(self):
        return [json.loads(line) for line in self.trace_path.read_text().splitlines()]

    def test_scoped_observer_adds_correlation_and_common_fields(self):
        observe = self.tracer.notion_observer(turn=2, tool_use_id="toolu_7")
        observe("notion_request", {"operation": "search_pages", "status": "success"})
        (event,) = self.events()
        self.assertEqual(set(event), COMMON_FIELDS | set(EVENT_FIELDS["notion_request"]))
        self.assertEqual((event["turn"], event["tool_use_id"]), (2, "toolu_7"))
        self.assertEqual(event["run_id"], self.tracer.run_id)
        self.assertEqual(event["mode"], "pre_session")

    def test_notion_layer_cannot_override_correlation(self):
        observe = self.tracer.notion_observer(turn=1, tool_use_id="toolu_real")
        observe("notion_retrieval", {"turn": 99, "tool_use_id": "SENTINEL-SPOOF"})
        event = self.events()[0]
        self.assertEqual((event["turn"], event["tool_use_id"]), (1, "toolu_real"))

    def test_unlisted_fields_and_events_are_dropped(self):
        observe = self.tracer.notion_observer(turn=1, tool_use_id="t")
        observe("notion_request", {"query": "SENTINEL-Q", "url": "SENTINEL-U"})
        observe("run_completed", {"status": "SENTINEL-EVENT"})
        observe("notion_secret", {})
        observe("notion_request", ["not", "a", "dict"])
        text = self.trace_path.read_text()
        self.assertEqual(len(self.events()), 1)
        self.assertNotIn("SENTINEL", text)
        self.assertNotIn("query", self.events()[0])

    def test_string_values_are_restricted(self):
        self.tracer.notion_event("notion_request", {
            "operation": "SENTINEL-OP",
            "status": "success",
            "status_category": "3xx",
            "request_count": "SENTINEL-COUNT",
            "error_type": "SENTINEL message with spaces",
        })
        self.tracer.notion_event("notion_retrieval", {
            "retrieval_type": "student_context",
            "notes_source": "SENTINEL-SOURCE",
            "cache_hit": "SENTINEL-BOOL",
            "notes_char_count": {"nested": "SENTINEL"},
            "error_type": "StudentNotFoundError",
        })
        request, retrieval = self.events()
        self.assertNotIn("SENTINEL", self.trace_path.read_text())
        self.assertEqual(request["status"], "success")
        for key in ("operation", "status_category", "request_count", "error_type"):
            self.assertIsNone(request[key], key)
        self.assertEqual(retrieval["retrieval_type"], "student_context")
        self.assertEqual(retrieval["error_type"], "StudentNotFoundError")
        for key in ("notes_source", "cache_hit", "notes_char_count"):
            self.assertIsNone(retrieval[key], key)

    def test_existing_event_schemas_unchanged(self):
        self.assertEqual(
            EVENT_FIELDS["tool_execution"],
            ("turn", "tool_name", "tool_use_id", "duration_ms", "status", "error_type"),
        )
        self.assertNotIn("match_count", EVENT_FIELDS["notion_retrieval"])


# -- agent runs -----------------------------------------------------------


def model_turns(*tool_calls):
    """A tool_use turn calling each (tool_use_id, tool_name), then end_turn."""
    tool_turn = SimpleNamespace(
        id="msg_1", model="test-model", stop_reason="tool_use",
        content=[
            SimpleNamespace(type="tool_use", id=tool_id, name=name, input={"name": NAME})
            for tool_id, name in tool_calls
        ],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    final = SimpleNamespace(
        id="msg_2", model="test-model", stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="done")],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    return [tool_turn, final]


BOTH_TOOLS = (("toolu_ctx", "get_student_context"), ("toolu_notes", "get_latest_session_notes"))


class AgentRunTestCase(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.trace_path = Path(self._tmp.name) / "logs" / "agent-runs.jsonl"
        self.fake = build_fake()
        self.repository = self.fake.repository()
        self.create = unittest.mock.Mock()
        fake_client = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        for p in (
            unittest.mock.patch.object(agent, "Anthropic", return_value=fake_client),
            unittest.mock.patch.object(agent, "build_instructions", return_value="system"),
            unittest.mock.patch.object(observability, "DEFAULT_TRACE_PATH", self.trace_path),
            unittest.mock.patch.object(tools, "STUDENT_CONTEXT_BACKEND", "notion"),
            unittest.mock.patch.object(tools, "_notion_repository", self.repository),
            unittest.mock.patch.object(
                tools, "STUDENTS_PATH", Path(self._tmp.name) / f"{PRIVATE_FILE}.json"
            ),
        ):
            p.start()
            self.addCleanup(p.stop)

    def run_agent(self, tool_calls=BOTH_TOOLS, mode="pre_session"):
        self.create.side_effect = model_turns(*tool_calls)
        kwargs = {"student_name": NAME, "mode": mode}
        if mode == "post_session":
            kwargs.update(transcript=TRANSCRIPT, session_datetime=SESSION_DT)
        return agent.run_agent(**kwargs)

    def events(self, run_id=None):
        events = [json.loads(line) for line in self.trace_path.read_text().splitlines()]
        return [e for e in events if run_id is None or e["run_id"] == run_id]


class AgentNotionTracingTests(AgentRunTestCase):
    def test_successful_run_correlates_notion_events(self):
        self.run_agent()
        events = self.events()
        self.assertEqual(
            [e["event"] for e in events],
            [
                "run_started", "model_response",
                "notion_request", "notion_request", "notion_retrieval", "tool_execution",
                "notion_request", "notion_request", "notion_request", "notion_request",
                "notion_retrieval", "tool_execution",
                "model_response", "run_completed",
            ],
        )
        self.assertEqual(len({e["run_id"] for e in events}), 1)
        for e in events:
            self.assertEqual(set(e), COMMON_FIELDS | set(EVENT_FIELDS[e["event"]]), e["event"])

        # Every Notion event carries the turn and tool_use_id of the
        # tool_execution event that follows it.
        pending = []
        for e in events:
            if e["event"].startswith("notion_"):
                pending.append(e)
            elif e["event"] == "tool_execution":
                self.assertTrue(pending)
                for n in pending:
                    self.assertEqual(
                        (n["turn"], n["tool_use_id"]), (e["turn"], e["tool_use_id"])
                    )
                pending = []
        self.assertEqual(pending, [])

        retrievals = [e for e in events if e["event"] == "notion_retrieval"]
        self.assertEqual(
            [(r["retrieval_type"], r["cache_hit"]) for r in retrievals],
            [("student_context", False), ("latest_session_notes", True)],
        )

    def test_tool_execution_behaviour_unchanged(self):
        self.run_agent()
        tool_events = [e for e in self.events() if e["event"] == "tool_execution"]
        self.assertEqual([e["status"] for e in tool_events], ["success", "success"])
        for e in tool_events:
            self.assertIsInstance(e["duration_ms"], float)
            self.assertIsNone(e["error_type"])
        self.assertEqual(self.events()[-1]["tool_calls"], 2)

    def test_json_backend_emits_no_notion_events(self):
        students = Path(self._tmp.name) / "students.json"
        students.write_text(json.dumps({NAME: {"path": "P", "previous_session": None}}))
        with unittest.mock.patch.object(tools, "STUDENT_CONTEXT_BACKEND", "json"), \
                unittest.mock.patch.object(tools, "STUDENTS_PATH", students):
            self.run_agent()
        names = [e["event"] for e in self.events()]
        self.assertFalse([n for n in names if n.startswith("notion_")])
        self.assertEqual(names.count("tool_execution"), 2)
        self.assertEqual(self.fake.requests, [])

    def test_notion_failure_reports_each_layer(self):
        self.fake.fail_status = 503
        with self.assertRaises(NotionRequestError):
            self.run_agent(tool_calls=(("toolu_ctx", "get_student_context"),))
        failures = [
            (e["event"], e.get("error_type"))
            for e in self.events()
            if e.get("status") in ("error", "failed")
        ]
        self.assertEqual(
            failures,
            [
                ("notion_request", "NotionRequestError"),
                ("notion_retrieval", "NotionRequestError"),
                ("tool_execution", "NotionRequestError"),
                ("run_failed", "NotionRequestError"),
            ],
        )
        self.assertEqual(self.events()[-1]["failure_reason"], "tool_error")

    def test_trace_write_failure_does_not_break_retrieval(self):
        self.trace_path.parent.parent.mkdir(parents=True, exist_ok=True)
        self.trace_path.parent.write_text("not a directory")
        result = self.run_agent()
        self.assertEqual(result.stop_reason, "end_turn")
        sent = json.dumps(self.create.call_args.kwargs["messages"][2]["content"])
        self.assertIn(SESSION_NOTE, sent)

    def test_failing_tracer_does_not_break_the_agent(self):
        with unittest.mock.patch.object(
            RunTracer, "notion_event", side_effect=RuntimeError(EXC_MESSAGE)
        ):
            result = self.run_agent()
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(self.events()[-1]["event"], "run_completed")

    def test_sequential_runs_with_cached_repository_are_isolated(self):
        self.run_agent(tool_calls=(("toolu_first", "get_student_context"),))
        first_run = self.events()[0]["run_id"]
        self.assert_holds_no_run_state(self.repository)

        self.run_agent(tool_calls=(("toolu_second", "get_student_context"),))
        second_run = self.events()[-1]["run_id"]
        self.assertNotEqual(first_run, second_run)

        second = self.events(second_run)
        notion = [e for e in second if e["event"].startswith("notion_")]
        self.assertTrue(notion)
        self.assertTrue(all(e["tool_use_id"] == "toolu_second" for e in notion))
        (retrieval,) = [e for e in notion if e["event"] == "notion_retrieval"]
        self.assertIs(retrieval["cache_hit"], True)
        self.assertNotIn("search_pages", [e.get("operation") for e in notion])

        first = self.events(first_run)
        self.assertTrue(all(e.get("tool_use_id") != "toolu_second" for e in first))
        self.assertEqual(len(first) + len(second), len(self.events()))
        self.assert_holds_no_run_state(self.repository)

    def assert_holds_no_run_state(self, repo):
        for obj in (repo, repo._client):
            for key, value in vars(obj).items():
                self.assertFalse(
                    callable(value) and not isinstance(value, type), key
                )
                self.assertNotIsInstance(value, RunTracer, key)
        flattened = repr(vars(self.repository))
        for e in self.events():
            self.assertNotIn(e["run_id"], flattened)


class AgentNotionPrivacyTests(AgentRunTestCase):
    """Markers planted in every private input must not reach the JSONL
    trace, DEBUG terminal logs, or Notion-layer exception messages."""

    def assert_clean(self, text, where):
        for marker in MARKERS:
            with self.subTest(where=where, marker=marker):
                self.assertNotIn(marker, text)

    def run_flow(self, label):
        outcome, logs = capture_logs(lambda: self.run_agent(mode="post_session"))
        self.assert_clean(logs, f"{label} logs")
        if isinstance(outcome, Exception):
            self.assert_clean(str(outcome), f"{label} exception")
        return outcome

    def test_successful_flow(self):
        result = self.run_flow("success")
        self.assertEqual(result.stop_reason, "end_turn")
        # The direct search uses a separate query marker.
        _, logs = capture_logs(
            lambda: self.repository._client.search_pages(QUERY, observer=lambda e, f: None)
        )
        self.assert_clean(logs, "search logs")
        self.assert_clean(self.trace_path.read_text(), "trace")
        self.assertTrue(any(
            r.url.params.get("start_cursor", "").startswith(CURSOR)
            or CURSOR in r.content.decode()
            for r in self.fake.requests
        ), "cursor marker was not exercised")

    def test_failed_flows(self):
        def transport_failure(request):
            raise httpx2.ConnectError(f"{EXC_MESSAGE} {request.url}")

        def failing(status):
            def arrange(fake):
                fake.fail_status = status
            return arrange

        scenarios = {
            "4xx": (failing(404), NotionRequestError),
            "5xx": (failing(500), NotionRequestError),
            "not found": (
                lambda fake: setattr(fake, "search_results", []), StudentNotFoundError
            ),
            "ambiguous": (
                lambda fake: fake.search_results.append(page("dup", NAME)),
                AmbiguousStudentMatchError,
            ),
            "malformed": (
                lambda fake: fake.children[STUDENT_PAGE].insert(
                    0, {"object": "block", "id": PROFILE_BLOCK}
                ),
                MalformedNotionResponseError,
            ),
            "transport": (None, NotionRequestError),
        }
        for label, (arrange, expected) in scenarios.items():
            fake = build_fake()
            fake.fail_body = {"object": "error", "message": ERROR_BODY}
            if arrange is None:
                client = NotionClient(
                    FAKE_API_KEY, transport=httpx2.MockTransport(transport_failure)
                )
            else:
                arrange(fake)
                client = fake.client()
            repository = NotionStudentRepository(client, PARENT_ID)
            with unittest.mock.patch.object(tools, "_notion_repository", repository):
                outcome = self.run_flow(label)
            self.assertIsInstance(outcome, expected, label)
        trace = self.trace_path.read_text()
        self.assert_clean(trace, "trace")
        self.assertEqual(trace.count('"event": "run_failed"'), len(scenarios))

    def test_depth_error_flow(self):
        self.fake.children["notes"] = [
            block(PROFILE_BLOCK, "paragraph", SESSION_NOTE, has_children=True)
        ]
        self.fake.children[PROFILE_BLOCK] = []
        self.repository.max_traversal_depth = 2
        outcome = self.run_flow("depth")
        self.assertIsInstance(outcome, TraversalDepthExceededError)
        self.assert_clean(self.trace_path.read_text(), "trace")


if __name__ == "__main__":
    unittest.main()
