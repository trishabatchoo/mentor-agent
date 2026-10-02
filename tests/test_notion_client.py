"""Offline tests for notion_client.py: headers, pagination, error handling,
and log privacy. Every response comes from an httpx2.MockTransport; no
request ever reaches the real Notion API.
"""

import io
import json
import logging
import sys
import unittest
from pathlib import Path

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notion_client  # noqa: E402
from fake_notion import FAKE_API_KEY, FakeNotion, block  # noqa: E402
from notion_client import (  # noqa: E402
    MalformedNotionResponseError,
    NotionClient,
    NotionConfigError,
    NotionRequestError,
    PaginationLimitExceededError,
)

BLOCK_ID = "SENTINEL-BLOCK-ID"
QUERY = "SENTINEL-STUDENT-QUERY"


def capture_logs(fn):
    """Run fn with every logger captured at DEBUG; return (result_or_exc, output)."""
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
        except Exception as exc:  # returned for inspection
            outcome = exc
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
    return outcome, stream.getvalue()


def static_client(*responses, **kwargs):
    """A client whose transport returns `responses` in order."""
    queue = list(responses)
    seen = []

    def handler(request):
        seen.append(request)
        item = queue.pop(0)
        return item if isinstance(item, httpx2.Response) else httpx2.Response(200, json=item)

    client = NotionClient(FAKE_API_KEY, transport=httpx2.MockTransport(handler), **kwargs)
    return client, seen


class HeaderTests(unittest.TestCase):
    def test_auth_and_default_version_headers(self):
        fake = FakeNotion()
        fake.children[BLOCK_ID] = []
        fake.client().list_block_children(BLOCK_ID)
        headers = fake.requests[0].headers
        self.assertEqual(headers["Authorization"], f"Bearer {FAKE_API_KEY}")
        self.assertEqual(headers["Notion-Version"], "2026-03-11")

    def test_custom_version_header(self):
        fake = FakeNotion()
        fake.children[BLOCK_ID] = []
        client = NotionClient(FAKE_API_KEY, "2099-01-01", transport=fake.transport())
        client.list_block_children(BLOCK_ID)
        self.assertEqual(fake.requests[0].headers["Notion-Version"], "2099-01-01")

    def test_missing_api_key_is_a_config_error(self):
        with self.assertRaises(NotionConfigError):
            NotionClient("")

    def test_repr_hides_api_key(self):
        self.assertNotIn(FAKE_API_KEY, repr(FakeNotion().client()))


class PaginationTests(unittest.TestCase):
    def test_search_follows_cursors_and_filters_pages(self):
        fake = FakeNotion(page_size=2)
        fake.search_results = [{"object": "page", "id": f"p{i}"} for i in range(5)]
        results = fake.client().search_pages(QUERY)

        self.assertEqual([r["id"] for r in results], ["p0", "p1", "p2", "p3", "p4"])
        bodies = [json.loads(r.content) for r in fake.requests]
        self.assertEqual(len(bodies), 3)
        self.assertNotIn("start_cursor", bodies[0])
        self.assertEqual([b.get("start_cursor") for b in bodies[1:]], ["2", "4"])
        for body in bodies:
            self.assertEqual(body["query"], QUERY)
            self.assertEqual(body["filter"], {"property": "object", "value": "page"})

    def test_block_children_follow_cursors_in_order(self):
        fake = FakeNotion(page_size=2)
        fake.children[BLOCK_ID] = [block(f"b{i}", "paragraph", str(i)) for i in range(5)]
        results = fake.client().list_block_children(BLOCK_ID)

        self.assertEqual([b["id"] for b in results], ["b0", "b1", "b2", "b3", "b4"])
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual(
            [r.url.params.get("start_cursor") for r in fake.requests], [None, "2", "4"]
        )

    def test_pagination_limit(self):
        fake = FakeNotion(page_size=1)
        fake.children[BLOCK_ID] = [block(f"b{i}", "paragraph", "x") for i in range(5)]
        with self.assertRaises(PaginationLimitExceededError):
            fake.client(max_pages=3).list_block_children(BLOCK_ID)
        self.assertEqual(len(fake.requests), 3)

    def test_repeated_cursor_is_malformed(self):
        page = {"results": [], "has_more": True, "next_cursor": "same"}
        client, _ = static_client(page, page)
        with self.assertRaises(MalformedNotionResponseError):
            client.list_block_children(BLOCK_ID)


class ErrorTests(unittest.TestCase):
    def test_http_errors_raise_request_error_with_category(self):
        for status, category in ((400, "4xx"), (401, "4xx"), (404, "4xx"),
                                 (429, "4xx"), (500, "5xx"), (503, "5xx")):
            with self.subTest(status=status):
                fake = FakeNotion()
                fake.fail_status = status
                with self.assertRaises(NotionRequestError) as ctx:
                    fake.client().list_block_children(BLOCK_ID)
                err = ctx.exception
                self.assertEqual(err.status_code, status)
                self.assertEqual(err.status_category, category)
                self.assertEqual(err.operation, "list_block_children")
                self.assertEqual(len(fake.requests), 1)  # no retries
                self.assertNotIn("SENTINEL", str(err))

    def test_transport_failure_hides_underlying_exception(self):
        def handler(request):
            raise httpx2.ConnectError(f"failed to reach {request.url}")

        client = NotionClient(FAKE_API_KEY, transport=httpx2.MockTransport(handler))
        with self.assertLogs(notion_client.logger, "WARNING"):
            with self.assertRaises(NotionRequestError) as ctx:
                client.list_block_children(BLOCK_ID)
        err = ctx.exception
        self.assertEqual(err.status_category, "transport_error")
        self.assertIsNone(err.__cause__)
        self.assertTrue(err.__suppress_context__)
        self.assertNotIn(BLOCK_ID, str(err))
        self.assertNotIn("notion.com", str(err))

    def test_malformed_responses(self):
        cases = {
            "not json": httpx2.Response(200, content=b"<html>"),
            "not object": httpx2.Response(200, json=["x"]),
            "no results": {"has_more": False},
            "results not list": {"results": {}, "has_more": False},
            "no has_more": {"results": []},
            "non-object item": {"results": ["x"], "has_more": False},
            "missing cursor": {"results": [], "has_more": True, "next_cursor": None},
        }
        for label, response in cases.items():
            with self.subTest(label):
                client, _ = static_client(response)
                with self.assertRaises(MalformedNotionResponseError):
                    client.list_block_children(BLOCK_ID)

    def test_empty_block_id_is_rejected_without_a_request(self):
        client, seen = static_client()
        with self.assertRaises(MalformedNotionResponseError):
            client.list_block_children("")
        self.assertEqual(seen, [])


class ReadOnlyTests(unittest.TestCase):
    def test_only_search_post_and_children_get_are_issued(self):
        fake = FakeNotion(page_size=1)
        fake.search_results = [{"object": "page", "id": "p"}] * 2
        fake.children[BLOCK_ID] = [block("b", "paragraph", "x")] * 2
        client = fake.client()
        client.search_pages(QUERY)
        client.list_block_children(BLOCK_ID)
        for request in fake.requests:
            self.assertIn(
                (request.method, request.url.path),
                {("POST", "/v1/search"), ("GET", f"/v1/blocks/{BLOCK_ID}/children")},
            )


class LogPrivacyTests(unittest.TestCase):
    def assert_private_values_absent(self, text):
        for private in (FAKE_API_KEY, BLOCK_ID, QUERY, "api.notion.com",
                        "SENTINEL-ERROR-BODY", "SENTINEL-CONTENT"):
            self.assertNotIn(private, text)

    def test_success_logs_are_metadata_only(self):
        fake = FakeNotion(page_size=1)
        fake.search_results = [{"object": "page", "id": "p"}] * 2
        fake.children[BLOCK_ID] = [block("b", "paragraph", "SENTINEL-CONTENT")]
        client = fake.client()

        _, output = capture_logs(
            lambda: (client.search_pages(QUERY), client.list_block_children(BLOCK_ID))
        )
        self.assert_private_values_absent(output)
        self.assertIn("op=search status=2xx", output)
        self.assertIn("op=list_block_children", output)
        self.assertIn("pages=2 items=2", output)

    def test_failure_logs_are_metadata_only(self):
        fake = FakeNotion()
        fake.fail_status = 500
        client = fake.client()
        err, output = capture_logs(lambda: client.list_block_children(BLOCK_ID))
        self.assertIsInstance(err, NotionRequestError)
        self.assert_private_values_absent(output + str(err))
        self.assertIn("status=5xx", output)

    def test_httpx2_url_log_records_for_notion_are_dropped(self):
        record = logging.LogRecord(
            "httpx2", logging.INFO, __file__, 1,
            'HTTP Request: %s %s "%s"', ("GET", f"https://api.notion.com/v1/blocks/{BLOCK_ID}", "200"), None,
        )
        self.assertFalse(notion_client._url_filter.filter(record))
        other = logging.LogRecord(
            "httpx2", logging.INFO, __file__, 1,
            "HTTP Request: %s %s", ("POST", "https://api.anthropic.com/v1/messages"), None,
        )
        self.assertTrue(notion_client._url_filter.filter(other))


if __name__ == "__main__":
    unittest.main()
