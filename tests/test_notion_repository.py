"""Offline tests for notion_repository.py: student page resolution, profile
rendering and field parsing, latest-session selection, Notes/Transcript
handling, traversal limits, configuration, and log/exception privacy.

All Notion data is synthetic and served by tests/fake_notion.py.
"""

import io
import json
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fake_notion import (  # noqa: E402
    FAKE_API_KEY,
    OTHER_PARENT_ID,
    PARENT_ID,
    FakeNotion,
    block,
    child_page,
    page,
    tab,
)
from notion_client import (  # noqa: E402
    MalformedNotionResponseError,
    NotionConfigError,
    NotionRequestError,
)
from notion_repository import (  # noqa: E402
    AmbiguousStudentMatchError,
    NotionConfig,
    NotionStudentRepository,
    StudentNotFoundError,
    TraversalDepthExceededError,
    parse_session_datetime,
)

STUDENT = "Casey Rivera"
STUDENT_PAGE = "student-page-casey"

CONTEXT_KEYS = {"title", "profile_text", "path", "current_project"}
SESSION_KEYS = {"title", "session_datetime", "notes_source", "notes"}


def quiet_notion_logs(test):
    """Keep expected failure warnings off stderr; records still propagate
    to any handler a test installs on the root logger."""
    handler = logging.NullHandler()
    for name in ("notion_repository", "notion_client"):
        logging.getLogger(name).addHandler(handler)
        test.addCleanup(logging.getLogger(name).removeHandler, handler)


class RepositoryTestCase(unittest.TestCase):
    def setUp(self):
        quiet_notion_logs(self)
        self.fake = FakeNotion()
        self.fake.search_results = [page(STUDENT_PAGE, STUDENT)]
        self.fake.children[STUDENT_PAGE] = []

    def repo(self, **kwargs):
        return self.fake.repository(**kwargs)

    def set_sessions(self, *blocks):
        self.fake.children[STUDENT_PAGE] = list(blocks)
        for b in blocks:
            if b["type"] == "child_page":
                self.fake.children.setdefault(b["id"], [])


class StudentResolutionTests(RepositoryTestCase):
    def test_exact_match_is_case_and_whitespace_insensitive(self):
        context = self.repo().get_student_context("  casey   RIVERA ")
        self.assertEqual(context["title"], STUDENT)

    def test_fuzzy_results_are_rejected(self):
        self.fake.search_results = [
            page("p1", "Casey Rivera-Smith"),
            page("p2", "Casey"),
            page("p3", "Casey Rivera notes"),
        ]
        with self.assertRaises(StudentNotFoundError):
            self.repo().get_student_context(STUDENT)

    def test_exact_title_under_wrong_parent_is_rejected(self):
        self.fake.search_results = [page("p1", STUDENT, parent_id=OTHER_PARENT_ID)]
        with self.assertRaises(StudentNotFoundError):
            self.repo().get_student_context(STUDENT)

    def test_non_page_parent_is_rejected(self):
        result = page("p1", STUDENT)
        result["parent"] = {"type": "workspace", "workspace": True}
        self.fake.search_results = [result]
        with self.assertRaises(StudentNotFoundError):
            self.repo().get_student_context(STUDENT)

    def test_parent_id_compared_without_dashes_or_case(self):
        self.fake.search_results = [
            page(STUDENT_PAGE, STUDENT, parent_id=PARENT_ID.replace("-", "").upper())
        ]
        self.assertEqual(self.repo().get_student_context(STUDENT)["title"], STUDENT)

    def test_trashed_pages_are_ignored(self):
        self.fake.search_results = [
            page("p-old", STUDENT, in_trash=True),
            page(STUDENT_PAGE, STUDENT),
        ]
        self.assertEqual(self.repo().get_student_context(STUDENT)["title"], STUDENT)

    def test_no_match(self):
        self.fake.search_results = []
        with self.assertRaises(StudentNotFoundError):
            self.repo().get_student_context(STUDENT)

    def test_blank_name_is_not_found_without_searching(self):
        with self.assertRaises(StudentNotFoundError):
            self.repo().get_student_context("   ")
        self.assertEqual(self.fake.requests, [])

    def test_multiple_exact_matches_are_ambiguous(self):
        self.fake.search_results = [page("p1", STUDENT), page("p2", "casey rivera")]
        with self.assertRaises(AmbiguousStudentMatchError):
            self.repo().get_student_context(STUDENT)

    def test_same_page_repeated_in_results_is_not_ambiguous(self):
        self.fake.search_results = [page(STUDENT_PAGE, STUDENT)] * 2
        self.assertEqual(self.repo().get_student_context(STUDENT)["title"], STUDENT)

    def test_match_found_on_a_later_search_page(self):
        self.fake.page_size = 2
        self.fake.search_results = [
            page("p1", "Casey Rivera Jr"),
            page("p2", "Someone Else"),
            page("p3", "Casey R."),
            page(STUDENT_PAGE, STUDENT),
            page("p5", STUDENT, parent_id=OTHER_PARENT_ID),
        ]
        self.assertEqual(self.repo().get_student_context(STUDENT)["title"], STUDENT)
        self.assertEqual(self.fake.search_count(), 3)

    def test_ambiguity_detected_across_search_pages(self):
        self.fake.page_size = 1
        self.fake.search_results = [page("p1", STUDENT), page("p2", "x"), page("p3", STUDENT)]
        with self.assertRaises(AmbiguousStudentMatchError):
            self.repo().get_student_context(STUDENT)

    def test_title_built_from_all_fragments(self):
        self.fake.search_results = [
            page(STUDENT_PAGE, None, title_fragments=("Casey ", "Rivera"))
        ]
        self.assertEqual(self.repo().get_student_context(STUDENT)["title"], STUDENT)

    def test_resolution_is_cached_across_tools(self):
        repo = self.repo()
        repo.get_student_context(STUDENT)
        repo.get_latest_session_notes(STUDENT)
        self.assertEqual(self.fake.search_count(), 1)


class StudentContextTests(RepositoryTestCase):
    def test_profile_text_fields_and_order(self):
        self.fake.children[STUDENT_PAGE] = [
            block("b1", "heading_1", "Profile"),
            block("b2", "paragraph", "Path: ", "Data Analyst ", "Apprenticeship"),
            block("b3", "paragraph", "Current project: Project 2 - ", "Analyze Requirements"),
            block("b4", "heading_2", "Goals"),
            block("b5", "bulleted_list_item", "Build a portfolio"),
            block("b6", "numbered_list_item", "First"),
            block("b7", "numbered_list_item", "Second"),
            block("b8", "to_do", "Done item", checked=True),
            block("b9", "to_do", "Open item", checked=False),
            block("b10", "heading_3", "Sessions"),
            child_page("s1", "2026-09-29T11:00:00.000-04:00"),
        ]
        context = self.repo().get_student_context(STUDENT)

        self.assertEqual(set(context), CONTEXT_KEYS)
        self.assertEqual(context["path"], "Data Analyst Apprenticeship")
        self.assertEqual(context["current_project"], "Project 2 - Analyze Requirements")
        self.assertEqual(
            context["profile_text"],
            "\n".join([
                "# Profile",
                "Path: Data Analyst Apprenticeship",
                "Current project: Project 2 - Analyze Requirements",
                "## Goals",
                "- Build a portfolio",
                "1. First",
                "2. Second",
                "[x] Done item",
                "[ ] Open item",
                "### Sessions",
                "[Page] 2026-09-29T11:00:00.000-04:00",
            ]),
        )

    def test_session_child_pages_are_not_traversed(self):
        self.fake.children[STUDENT_PAGE] = [child_page("s1", "2026-09-29T11:00:00-04:00")]
        self.fake.children["s1"] = [block("x", "paragraph", "session body")]
        context = self.repo().get_student_context(STUDENT)
        self.assertNotIn("s1", self.fake.fetched_block_ids())
        self.assertNotIn("session body", context["profile_text"])

    def test_missing_fields_are_none(self):
        self.fake.children[STUDENT_PAGE] = [
            block("b1", "paragraph", "Pathway notes without a colon"),
            block("b2", "paragraph", "Current project:   "),
        ]
        context = self.repo().get_student_context(STUDENT)
        self.assertIsNone(context["path"])
        self.assertIsNone(context["current_project"])

    def test_field_parsing_is_case_insensitive_and_first_wins(self):
        self.fake.children[STUDENT_PAGE] = [
            block("b1", "bulleted_list_item", "PATH:   Path One  "),
            block("b2", "paragraph", "current PROJECT: P1\nPath: Path Two"),
        ]
        context = self.repo().get_student_context(STUDENT)
        self.assertEqual(context["path"], "Path One")
        self.assertEqual(context["current_project"], "P1")

    def test_paginated_children_keep_order(self):
        self.fake.page_size = 2
        self.fake.children[STUDENT_PAGE] = [
            block(f"b{i}", "paragraph", f"line {i}") for i in range(5)
        ]
        context = self.repo().get_student_context(STUDENT)
        self.assertEqual(
            context["profile_text"], "\n".join(f"line {i}" for i in range(5))
        )
        self.assertEqual(self.fake.fetched_block_ids(), [STUDENT_PAGE] * 3)

    def test_nested_blocks_are_indented_in_order(self):
        self.fake.children[STUDENT_PAGE] = [
            block("b1", "bulleted_list_item", "Parent", has_children=True),
            block("b2", "paragraph", "After"),
        ]
        self.fake.children["b1"] = [
            block("c1", "bulleted_list_item", "Child", has_children=True)
        ]
        self.fake.children["c1"] = [block("g1", "paragraph", "Grandchild")]
        context = self.repo().get_student_context(STUDENT)
        self.assertEqual(
            context["profile_text"], "- Parent\n  - Child\n    Grandchild\nAfter"
        )

    def test_result_contains_no_ids(self):
        self.fake.children[STUDENT_PAGE] = [block("SENTINEL-BLOCK", "paragraph", "x")]
        dumped = json.dumps(self.repo().get_student_context(STUDENT))
        self.assertNotIn(STUDENT_PAGE, dumped)
        self.assertNotIn("SENTINEL-BLOCK", dumped)

    def test_malformed_block_raises(self):
        self.fake.children[STUDENT_PAGE] = [{"object": "block", "id": "b1"}]
        with self.assertRaises(MalformedNotionResponseError):
            self.repo().get_student_context(STUDENT)

    def test_api_error_propagates(self):
        self.fake.fail_status = 502
        with self.assertRaises(NotionRequestError):
            self.repo().get_student_context(STUDENT)


class SessionDatetimeParsingTests(unittest.TestCase):
    def test_accepts_timezone_aware_iso(self):
        for title in (
            "2026-09-29T11:00:00.000-04:00",
            "2026-09-29T15:00:00Z",
            " 2026-09-29T15:00:00+00:00 ",
        ):
            with self.subTest(title=title):
                self.assertIsNotNone(parse_session_datetime(title))

    def test_rejects_everything_else(self):
        for title in (
            "2026-09-29",
            "2026-09-29T11:00:00",
            "2026-13-45T11:00:00-04:00",
            "Kickoff",
            "",
            None,
        ):
            with self.subTest(title=title):
                self.assertIsNone(parse_session_datetime(title))


class LatestSessionTests(RepositoryTestCase):
    def test_latest_selected_by_parsed_datetime_not_order(self):
        # 11:00-04:00 is 15:00Z, later than 14:30Z despite the earlier clock time.
        self.set_sessions(
            child_page("s-a", "2026-09-29T14:30:00+00:00"),
            child_page("s-b", "2026-09-29T11:00:00.000-04:00"),
            child_page("s-c", "2026-09-15T11:00:00.000-04:00"),
        )
        self.fake.children["s-b"] = [block("n", "paragraph", "latest notes")]
        session = self.repo().get_latest_session_notes(STUDENT)["latest_session"]

        self.assertEqual(set(session), SESSION_KEYS)
        self.assertEqual(session["title"], "2026-09-29T11:00:00.000-04:00")
        self.assertEqual(session["session_datetime"], "2026-09-29T11:00:00-04:00")
        self.assertEqual(session["notes"], "latest notes")
        self.assertEqual(session["notes_source"], "page")
        self.assertNotIn("s-a", self.fake.fetched_block_ids())
        self.assertNotIn("s-c", self.fake.fetched_block_ids())

    def test_invalid_session_titles_are_ignored(self):
        self.set_sessions(
            child_page("s-valid", "2026-09-01T10:00:00-04:00"),
            child_page("s-date", "2026-12-01"),
            child_page("s-naive", "2026-12-01T10:00:00"),
            child_page("s-bad", "2026-99-01T10:00:00-04:00"),
            child_page("s-text", "Onboarding resources"),
        )
        self.fake.children["s-valid"] = [block("n", "paragraph", "valid notes")]
        session = self.repo().get_latest_session_notes(STUDENT)["latest_session"]
        self.assertEqual(session["title"], "2026-09-01T10:00:00-04:00")
        self.assertEqual(session["notes"], "valid notes")

    def test_no_session_pages(self):
        self.set_sessions(
            block("b1", "paragraph", "Profile only"),
            child_page("s-text", "Resources"),
        )
        self.assertEqual(
            self.repo().get_latest_session_notes(STUDENT), {"latest_session": None}
        )

    def test_sessions_found_across_paginated_children(self):
        self.fake.page_size = 1
        self.set_sessions(
            child_page("s1", "2026-09-01T10:00:00-04:00"),
            block("b", "paragraph", "x"),
            child_page("s2", "2026-09-08T10:00:00-04:00"),
        )
        session = self.repo().get_latest_session_notes(STUDENT)["latest_session"]
        self.assertEqual(session["title"], "2026-09-08T10:00:00-04:00")


class SessionNotesTests(RepositoryTestCase):
    SESSION = "session-1"

    def setUp(self):
        super().setUp()
        self.set_sessions(child_page(self.SESSION, "2026-09-29T11:00:00.000-04:00"))

    def notes(self, **kwargs):
        return self.repo(**kwargs).get_latest_session_notes(STUDENT)["latest_session"]

    def build_tabbed(self):
        self.fake.children[self.SESSION] = [tab("tab-1")]
        self.fake.children["tab-1"] = [
            block("notes-label", "paragraph", "Notes", has_children=True),
            block("transcript-label", "paragraph", "Transcript", has_children=True),
        ]
        self.fake.children["notes-label"] = [
            block("n1", "heading_2", "Summary"),
            block("n2", "bulleted_list_item", "Discussed ", "joins", has_children=True),
        ]
        self.fake.children["n2"] = [block("n3", "paragraph", "Inner detail")]
        self.fake.children["transcript-label"] = [
            block("t1", "paragraph", "SENTINEL-TRANSCRIPT-LINE")
        ]

    def test_tabbed_notes_branch_only(self):
        self.build_tabbed()
        session = self.notes()
        self.assertEqual(session["notes_source"], "notes_branch")
        self.assertEqual(
            session["notes"], "## Summary\n- Discussed joins\n  Inner detail"
        )

    def test_transcript_branch_is_never_fetched_or_returned(self):
        self.build_tabbed()
        session = self.notes()
        self.assertNotIn("transcript-label", self.fake.fetched_block_ids())
        self.assertNotIn("SENTINEL-TRANSCRIPT-LINE", json.dumps(session))
        self.assertNotIn("Transcript", session["notes"])

    def test_transcript_branch_skipped_on_non_tabbed_page(self):
        self.fake.children[self.SESSION] = [
            block("p1", "paragraph", "Agenda"),
            block("tr", "heading_3", " transcript ", has_children=True),
            block("p2", "paragraph", "Wrap-up"),
        ]
        self.fake.children["tr"] = [block("t1", "paragraph", "SENTINEL-TRANSCRIPT-LINE")]
        session = self.notes()
        self.assertEqual(session["notes_source"], "page")
        self.assertEqual(session["notes"], "Agenda\nWrap-up")
        self.assertNotIn("tr", self.fake.fetched_block_ids())

    def test_transcript_skipped_in_student_profile_too(self):
        self.fake.children[STUDENT_PAGE].append(
            block("tr", "paragraph", "Transcript", has_children=True)
        )
        self.fake.children["tr"] = [block("t1", "paragraph", "SENTINEL-TRANSCRIPT-LINE")]
        context = self.repo().get_student_context(STUDENT)
        self.assertNotIn("SENTINEL-TRANSCRIPT-LINE", context["profile_text"])
        self.assertNotIn("tr", self.fake.fetched_block_ids())

    def test_non_tabbed_session_page(self):
        self.fake.children[self.SESSION] = [
            block("p1", "heading_2", "Topics"),
            block("p2", "numbered_list_item", "Reviewed data model"),
            block("p3", "to_do", "Send dataset", checked=False),
        ]
        session = self.notes()
        self.assertEqual(session["notes_source"], "page")
        self.assertEqual(
            session["notes"], "## Topics\n1. Reviewed data model\n[ ] Send dataset"
        )

    def test_notes_label_without_children_is_ordinary_content(self):
        self.fake.children[self.SESSION] = [
            block("h", "heading_2", "Notes"),
            block("p", "paragraph", "Body under heading"),
        ]
        session = self.notes()
        self.assertEqual(session["notes_source"], "page")
        self.assertEqual(session["notes"], "## Notes\nBody under heading")

    def test_paginated_notes_branch(self):
        self.build_tabbed()
        self.fake.page_size = 1
        self.fake.children["notes-label"] = [
            block(f"n{i}", "paragraph", f"note {i}") for i in range(4)
        ]
        session = self.notes()
        self.assertEqual(session["notes"], "note 0\nnote 1\nnote 2\nnote 3")

    def test_child_pages_inside_a_session_are_not_traversed(self):
        self.fake.children[self.SESSION] = [child_page("sub", "Attachments")]
        self.fake.children["sub"] = [block("x", "paragraph", "hidden")]
        session = self.notes()
        self.assertEqual(session["notes"], "[Page] Attachments")
        self.assertNotIn("sub", self.fake.fetched_block_ids())

    def test_session_result_contains_no_ids(self):
        self.build_tabbed()
        dumped = json.dumps(self.notes())
        for block_id in (self.SESSION, "tab-1", "notes-label", "n1", STUDENT_PAGE):
            self.assertNotIn(f'"{block_id}"', dumped)
            self.assertNotIn(block_id, dumped)

    def nest(self, levels):
        """Session content nested `levels` blocks deep below the top level."""
        parent = self.SESSION
        for level in range(levels + 1):
            block_id = f"lvl{level}"
            self.fake.children[parent] = [
                block(block_id, "paragraph", f"level {level}", has_children=level < levels)
            ]
            parent = block_id

    def test_depth_within_limit_succeeds(self):
        self.nest(2)
        session = self.notes(max_traversal_depth=2)
        self.assertEqual(session["notes"], "level 0\n  level 1\n    level 2")

    def test_depth_beyond_limit_raises(self):
        self.nest(3)
        with self.assertRaises(TraversalDepthExceededError):
            self.notes(max_traversal_depth=2)

    def test_default_depth_limit_is_six(self):
        self.nest(6)
        self.assertIn("level 6", self.notes()["notes"])
        self.nest(7)
        with self.assertRaises(TraversalDepthExceededError):
            self.notes()

    def test_api_error_while_reading_session(self):
        self.build_tabbed()
        del self.fake.children["notes-label"]  # fake returns 404
        with self.assertRaises(NotionRequestError):
            self.notes()


class ConfigTests(unittest.TestCase):
    def test_missing_variables_named_without_values(self):
        with self.assertRaises(NotionConfigError) as ctx:
            NotionConfig.from_env({"NOTION_API_KEY": "SENTINEL-KEY"})
        self.assertIn("NOTION_PARENT_PAGE_ID", str(ctx.exception))
        self.assertNotIn("NOTION_API_KEY", str(ctx.exception))
        self.assertNotIn("SENTINEL", str(ctx.exception))

    def test_defaults(self):
        config = NotionConfig.from_env(
            {"NOTION_API_KEY": "k", "NOTION_PARENT_PAGE_ID": PARENT_ID}
        )
        self.assertEqual(config.notion_version, "2026-03-11")
        self.assertEqual(config.max_traversal_depth, 6)

    def test_overrides(self):
        config = NotionConfig.from_env({
            "NOTION_API_KEY": "k",
            "NOTION_PARENT_PAGE_ID": PARENT_ID,
            "NOTION_VERSION": "2099-01-01",
            "NOTION_MAX_TRAVERSAL_DEPTH": "3",
        })
        self.assertEqual(config.notion_version, "2099-01-01")
        self.assertEqual(config.max_traversal_depth, 3)

    def test_invalid_depth(self):
        for value in ("0", "-1", "deep"):
            with self.subTest(value=value):
                with self.assertRaises(NotionConfigError):
                    NotionConfig.from_env({
                        "NOTION_API_KEY": "k",
                        "NOTION_PARENT_PAGE_ID": PARENT_ID,
                        "NOTION_MAX_TRAVERSAL_DEPTH": value,
                    })

    def test_repr_hides_secrets(self):
        config = NotionConfig.from_env(
            {"NOTION_API_KEY": "SENTINEL-KEY", "NOTION_PARENT_PAGE_ID": "SENTINEL-PARENT"}
        )
        self.assertNotIn("SENTINEL", repr(config))
        repo = NotionStudentRepository(FakeNotion().client(), "SENTINEL-PARENT")
        self.assertNotIn("SENTINEL", repr(repo))


class PrivacyTests(unittest.TestCase):
    """Private markers in every input and response must never reach logs or
    exception messages, whether a lookup succeeds or fails."""

    NAME = "SENTINEL-STUDENT-NAME"
    MARKERS = (
        "SENTINEL-STUDENT-NAME",
        "SENTINEL-PAGE-ID",
        "SENTINEL-SESSION-ID",
        "SENTINEL-BLOCK-ID",
        "SENTINEL-PROFILE-TEXT",
        "SENTINEL-SESSION-NOTE",
        "SENTINEL-TRANSCRIPT-LINE",
        "SENTINEL-ERROR-BODY",
        FAKE_API_KEY,
        PARENT_ID,
        PARENT_ID.replace("-", ""),
        "api.notion.com",
    )

    def build(self):
        fake = FakeNotion(page_size=1)
        fake.search_results = [page("SENTINEL-PAGE-ID", self.NAME)]
        fake.children["SENTINEL-PAGE-ID"] = [
            block("SENTINEL-BLOCK-ID", "paragraph", "SENTINEL-PROFILE-TEXT"),
            block("b2", "paragraph", "Path: SENTINEL-PROFILE-TEXT"),
            child_page("SENTINEL-SESSION-ID", "2026-09-29T11:00:00-04:00"),
        ]
        fake.children["SENTINEL-SESSION-ID"] = [tab("tab")]
        fake.children["tab"] = [
            block("notes", "paragraph", "Notes", has_children=True),
            block("tr", "paragraph", "Transcript", has_children=True),
        ]
        fake.children["notes"] = [block("n", "paragraph", "SENTINEL-SESSION-NOTE")]
        fake.children["tr"] = [block("t", "paragraph", "SENTINEL-TRANSCRIPT-LINE")]
        return fake

    def capture(self, fn):
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

    def assert_clean(self, text):
        for marker in self.MARKERS:
            self.assertNotIn(marker, text)

    def test_successful_lookups_log_metadata_only(self):
        fake = self.build()
        repo = fake.repository()

        def run():
            return repo.get_student_context(self.NAME), repo.get_latest_session_notes(self.NAME)

        (context, notes), output = self.capture(run)
        self.assert_clean(output)
        self.assertIn("exact_match_found=True", output)
        self.assertIn("notes_found=True", output)
        self.assertIn("notes_source=notes_branch", output)
        # The model-facing results do carry the content, but never ids.
        self.assertIn("SENTINEL-SESSION-NOTE", notes["latest_session"]["notes"])
        for marker in ("SENTINEL-PAGE-ID", "SENTINEL-SESSION-ID", "SENTINEL-BLOCK-ID",
                       "SENTINEL-TRANSCRIPT-LINE"):
            self.assertNotIn(marker, json.dumps([context, notes]))

    def test_failures_log_and_raise_without_private_data(self):
        scenarios = {}

        fake = self.build()
        fake.search_results = []
        scenarios["not found"] = (fake, StudentNotFoundError)

        fake = self.build()
        fake.search_results.append(page("SENTINEL-PAGE-ID-2", self.NAME))
        scenarios["ambiguous"] = (fake, AmbiguousStudentMatchError)

        fake = self.build()
        fake.fail_status = 429
        scenarios["api error"] = (fake, NotionRequestError)

        fake = self.build()
        fake.children["SENTINEL-PAGE-ID"].insert(
            0, {"object": "block", "id": "SENTINEL-BLOCK-ID"}
        )
        scenarios["malformed"] = (fake, MalformedNotionResponseError)

        for label, (fake, expected) in scenarios.items():
            for method in ("get_student_context", "get_latest_session_notes"):
                with self.subTest(label, method=method):
                    repo = fake.repository(max_traversal_depth=6)
                    err, output = self.capture(lambda: getattr(repo, method)(self.NAME))
                    self.assertIsInstance(err, expected)
                    self.assert_clean(output)
                    self.assert_clean(str(err))
                    self.assertIn("status=failure", output)
                    self.assertIn(type(err).__name__, output)

    def test_depth_error_is_private(self):
        fake = self.build()
        fake.children["n"] = []
        fake.children["notes"] = [
            block("SENTINEL-BLOCK-ID", "paragraph", "SENTINEL-SESSION-NOTE", has_children=True)
        ]
        fake.children["SENTINEL-BLOCK-ID"] = []
        repo = fake.repository(max_traversal_depth=2)
        err, output = self.capture(lambda: repo.get_latest_session_notes(self.NAME))
        self.assertIsInstance(err, TraversalDepthExceededError)
        self.assert_clean(output + str(err))


if __name__ == "__main__":
    unittest.main()
