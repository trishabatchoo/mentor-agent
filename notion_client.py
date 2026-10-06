"""Minimal read-only Notion API client: authenticated requests and pagination.

Only two read operations are exposed -- page search and block-children
listing. Nothing here creates, updates, archives, or deletes Notion content.

Privacy: the API key, request URLs (which embed page and block ids), search
queries, and response bodies are never logged or placed in exception
messages. Log records carry only the operation name, HTTP status category,
duration, and page/item counts. Errors are raised with `from None` so the
underlying httpx2 exception -- whose message includes the request URL -- is
not shown in tracebacks.

Observability: each public read operation accepts an optional `observer`,
a callable that receives one `notion_request` event per completed logical
operation (however many result pages it took). The observer is passed per
call and never stored, so this module stays independent of any agent or
tracing code. Event fields are built here from fixed operation names,
counts, durations, status categories, and exception class names only --
never queries, ids, cursors, URLs, headers, response bodies, or exception
messages. A failing observer is ignored and never affects the operation.
"""

import logging
import time
from dataclasses import dataclass
from typing import Callable

import httpx2

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.notion.com/v1"
NOTION_HOST = "api.notion.com"
DEFAULT_NOTION_VERSION = "2026-03-11"
DEFAULT_TIMEOUT_SECONDS = 30.0
PAGE_SIZE = 100
# Upper bound on pages fetched for one paginated listing, so a misbehaving
# cursor can't loop forever.
DEFAULT_MAX_PAGES = 50

# Receives (event_name, fields). Fields are metadata only; see module docstring.
NotionObserver = Callable[[str, dict], None]

# status_category values an observer may receive; anything else is None.
REQUEST_STATUS_CATEGORIES = frozenset(
    {"2xx", "4xx", "5xx", "transport_error", "malformed_response"}
)


class NotionError(Exception):
    """Base class for Notion integration errors. Messages are fixed text
    and never contain names, ids, URLs, or page content."""


class NotionConfigError(NotionError):
    """Required Notion configuration is missing or invalid."""


class NotionRequestError(NotionError):
    """A Notion API request failed (non-2xx status or transport failure)."""

    def __init__(self, operation: str, status_category: str, status_code: int | None = None):
        self.operation = operation
        self.status_category = status_category
        self.status_code = status_code
        super().__init__(
            f"Notion {operation} request failed (status category: {status_category})."
        )


class MalformedNotionResponseError(NotionError):
    """A Notion API response did not have the expected structure."""


class PaginationLimitExceededError(NotionError):
    """A paginated listing exceeded the configured maximum page count."""


class _NotionURLLogFilter(logging.Filter):
    """Drops httpx2's per-request INFO records for Notion requests.

    httpx2 logs "HTTP Request: <method> <url> ..." for every request, and
    Notion URLs contain page and block ids. Records for other hosts (e.g.
    the Anthropic API) are left untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return NOTION_HOST not in record.getMessage()
        except Exception:
            # A record we can't render is dropped rather than risked.
            return False


_url_filter = _NotionURLLogFilter()
logging.getLogger("httpx2").addFilter(_url_filter)


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


def _status_category(status_code: int) -> str:
    return f"{status_code // 100}xx"


def notify_observer(observer: NotionObserver | None, event: str, fields: dict) -> None:
    """Send an event to `observer`, if any. Observer failures are logged by
    class name only and otherwise ignored: observability never changes the
    outcome of a Notion operation."""
    if observer is None:
        return
    try:
        observer(event, fields)
    except Exception as exc:
        logger.debug("Notion observer failed (%s); ignored.", type(exc).__name__)


@dataclass
class _OperationStats:
    """Counters for one logical (possibly paginated) read operation."""

    request_count: int = 0
    pages_fetched: int = 0
    # Category of the last HTTP response received, if any.
    status_category: str | None = None


class NotionClient:
    """Authenticated, read-only access to the Notion REST API."""

    def __init__(
        self,
        api_key: str,
        notion_version: str = DEFAULT_NOTION_VERSION,
        *,
        transport: httpx2.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_pages: int = DEFAULT_MAX_PAGES,
    ):
        if not api_key:
            raise NotionConfigError("A Notion API key is required.")
        self.max_pages = max_pages
        self._http = httpx2.Client(
            base_url=API_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": notion_version,
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    def __repr__(self) -> str:
        return "NotionClient()"

    def close(self) -> None:
        self._http.close()

    # -- public read operations -----------------------------------------

    def search_pages(
        self, query: str, *, observer: NotionObserver | None = None
    ) -> list[dict]:
        """Return every page result of a Notion title search, across all
        result pages. Notion's search is fuzzy; callers must filter."""
        body = {
            "query": query,
            "filter": {"property": "object", "value": "page"},
            "page_size": PAGE_SIZE,
        }

        def fetch(cursor, stats):
            page_body = dict(body)
            if cursor:
                page_body["start_cursor"] = cursor
            return self._request("POST", "/search", "search", stats, json=page_body)

        def run(stats):
            return self._paginate("search", fetch, stats)

        return self._observed("search_pages", run, observer)

    def list_block_children(
        self, block_id: str, *, observer: NotionObserver | None = None
    ) -> list[dict]:
        """Return every direct child block of a page or block, in order."""

        def fetch(cursor, stats):
            params = {"page_size": PAGE_SIZE}
            if cursor:
                params["start_cursor"] = cursor
            return self._request(
                "GET",
                f"/blocks/{block_id}/children",
                "list_block_children",
                stats,
                params=params,
            )

        def run(stats):
            if not isinstance(block_id, str) or not block_id:
                raise MalformedNotionResponseError("A Notion block id was missing.")
            return self._paginate("list_block_children", fetch, stats)

        return self._observed("list_block_children", run, observer)

    # -- internals ------------------------------------------------------

    def _observed(self, operation: str, run, observer: NotionObserver | None) -> list[dict]:
        """Run one logical operation and report it as a single
        notion_request event. Exceptions are re-raised unchanged."""
        start = time.perf_counter()
        stats = _OperationStats()
        try:
            results = run(stats)
        except Exception as exc:
            if stats.request_count == 0:
                category = None
            elif isinstance(exc, NotionRequestError):
                category = exc.status_category
            elif isinstance(exc, MalformedNotionResponseError):
                category = "malformed_response"
            else:
                # e.g. PaginationLimitExceededError: every response succeeded,
                # so the category stays that of the last response (2xx).
                category = stats.status_category
            self._notify_request(
                observer, operation, start, stats, "error", category, None, exc
            )
            raise
        self._notify_request(
            observer, operation, start, stats, "success",
            stats.status_category, len(results), None,
        )
        return results

    @staticmethod
    def _notify_request(
        observer, operation, start, stats, status, category, results_returned, exc
    ) -> None:
        if observer is None:
            return
        notify_observer(observer, "notion_request", {
            "operation": operation,
            "duration_ms": _elapsed_ms(start),
            "status": status,
            "status_category": (
                category if category in REQUEST_STATUS_CATEGORIES else None
            ),
            "request_count": stats.request_count,
            "pages_fetched": stats.pages_fetched,
            "results_returned": results_returned,
            "error_type": type(exc).__name__ if exc is not None else None,
        })

    def _request(
        self, method: str, path: str, operation: str, stats: _OperationStats, **kwargs
    ) -> dict:
        start = time.perf_counter()
        stats.request_count += 1
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx2.HTTPError as exc:
            logger.warning(
                "Notion request op=%s status=transport_error duration_ms=%s error=%s",
                operation,
                _elapsed_ms(start),
                type(exc).__name__,
            )
            raise NotionRequestError(operation, "transport_error") from None

        category = _status_category(response.status_code)
        stats.status_category = category
        logger.info(
            "Notion request op=%s status=%s duration_ms=%s",
            operation,
            category,
            _elapsed_ms(start),
        )
        if not 200 <= response.status_code < 300:
            raise NotionRequestError(operation, category, response.status_code)

        try:
            data = response.json()
        except ValueError:
            raise MalformedNotionResponseError(
                f"Notion {operation} response was not valid JSON."
            ) from None
        if not isinstance(data, dict):
            raise MalformedNotionResponseError(
                f"Notion {operation} response was not a JSON object."
            )
        return data

    def _paginate(self, operation: str, fetch, stats: _OperationStats) -> list[dict]:
        results: list[dict] = []
        cursor = None
        seen_cursors = set()
        pages = 0
        while True:
            if pages >= self.max_pages:
                raise PaginationLimitExceededError(
                    f"Notion {operation} exceeded {self.max_pages} result pages."
                )
            data = fetch(cursor, stats)
            pages += 1

            page_results = data.get("results")
            has_more = data.get("has_more")
            if not isinstance(page_results, list) or not isinstance(has_more, bool):
                raise MalformedNotionResponseError(
                    f"Notion {operation} response was missing results or has_more."
                )
            if not all(isinstance(item, dict) for item in page_results):
                raise MalformedNotionResponseError(
                    f"Notion {operation} response contained a non-object result."
                )
            results.extend(page_results)
            stats.pages_fetched += 1

            if not has_more:
                break
            cursor = data.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise MalformedNotionResponseError(
                    f"Notion {operation} response had an invalid next_cursor."
                )
            seen_cursors.add(cursor)

        logger.info(
            "Notion pagination op=%s pages=%d items=%d", operation, pages, len(results)
        )
        return results
