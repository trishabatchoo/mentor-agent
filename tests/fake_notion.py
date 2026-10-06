"""A synthetic, in-memory stand-in for the Notion REST API, for offline tests.

Serves POST /v1/search and GET /v1/blocks/{id}/children through an
httpx2.MockTransport, paginating with a configurable page size, and records
every request so tests can assert what was (and was not) fetched. All
names, ids, and contents used with it are synthetic.
"""

import json
import sys
from pathlib import Path

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notion_client import NotionClient  # noqa: E402
from notion_repository import NotionStudentRepository  # noqa: E402

PARENT_ID = "aaaaaaaa-0000-4000-8000-000000000001"
OTHER_PARENT_ID = "bbbbbbbb-0000-4000-8000-000000000002"
FAKE_API_KEY = "SENTINEL-NOTION-API-KEY"


def rich_text(*fragments):
    return [
        {"type": "text", "text": {"content": f}, "plain_text": f}
        for f in fragments
    ]


def page(page_id, title, parent_id=PARENT_ID, *, title_fragments=None, **extra):
    """A search-result page object."""
    fragments = title_fragments if title_fragments is not None else (title,)
    result = {
        "object": "page",
        "id": page_id,
        "parent": {"type": "page_id", "page_id": parent_id},
        "in_trash": False,
        "properties": {
            "title": {"id": "title", "type": "title", "title": rich_text(*fragments)}
        },
    }
    result.update(extra)
    return result


def block(block_id, block_type, *fragments, has_children=False, **payload):
    """A rich-text block; `fragments` become its rich_text items."""
    return {
        "object": "block",
        "id": block_id,
        "type": block_type,
        "has_children": has_children,
        block_type: {"rich_text": rich_text(*fragments), **payload},
    }


def child_page(block_id, title, has_children=True):
    return {
        "object": "block",
        "id": block_id,
        "type": "child_page",
        "has_children": has_children,
        "child_page": {"title": title},
    }


def tab(block_id):
    return {
        "object": "block",
        "id": block_id,
        "type": "tab",
        "has_children": True,
        "tab": {},
    }


class FakeNotion:
    def __init__(self, page_size=100, cursor_prefix=""):
        self.page_size = page_size
        # Prepended to every next_cursor, so tests can plant a marker in it.
        self.cursor_prefix = cursor_prefix
        self.search_results: list[dict] = []
        self.children: dict[str, list[dict]] = {}
        self.requests: list[httpx2.Request] = []
        self.fail_status: int | None = None
        self.fail_body = {"object": "error", "message": "SENTINEL-ERROR-BODY"}

    def transport(self):
        return httpx2.MockTransport(self.handle)

    def client(self, **kwargs):
        return NotionClient(FAKE_API_KEY, transport=self.transport(), **kwargs)

    def repository(self, **kwargs):
        return NotionStudentRepository(self.client(), PARENT_ID, **kwargs)

    def fetched_block_ids(self):
        prefix, suffix = "/v1/blocks/", "/children"
        return [
            r.url.path[len(prefix):-len(suffix)]
            for r in self.requests
            if r.method == "GET" and r.url.path.startswith(prefix)
        ]

    def search_count(self):
        return sum(1 for r in self.requests if r.url.path == "/v1/search")

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.fail_status is not None:
            return httpx2.Response(self.fail_status, json=self.fail_body)

        path = request.url.path
        if request.method == "POST" and path == "/v1/search":
            cursor = json.loads(request.content).get("start_cursor")
            items = self.search_results
        elif (
            request.method == "GET"
            and path.startswith("/v1/blocks/")
            and path.endswith("/children")
        ):
            block_id = path[len("/v1/blocks/"):-len("/children")]
            if block_id not in self.children:
                return httpx2.Response(404, json={"object": "error"})
            cursor = request.url.params.get("start_cursor")
            items = self.children[block_id]
        else:
            return httpx2.Response(405, json={"object": "error"})

        start = int(cursor[len(self.cursor_prefix):]) if cursor else 0
        end = start + self.page_size
        has_more = end < len(items)
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "results": items[start:end],
                "has_more": has_more,
                "next_cursor": f"{self.cursor_prefix}{end}" if has_more else None,
            },
        )
