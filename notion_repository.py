"""Read-only student context and session notes from Notion.

Resolves a student's Notion page deterministically, renders its blocks as
readable text, selects the most recent session sub-page by its timestamp
title, and extracts that session's notes -- never its transcript.

Page and block ids stay inside this module: results returned to callers
(and, through tools.py, to the model) contain only titles, text, and
parsed fields. Logs carry only counts, flags, durations, and exception
class names; exception messages are fixed text.

Observability: the public operations accept an optional per-call
`observer` (see notion_client.NotionObserver). It receives the client's
notion_request events, then exactly one notion_retrieval summary per call,
whatever the outcome. Summaries hold counts, flags, fixed status values,
and exception class names -- never names, titles, text, datetimes, ids,
or queries. The observer is never stored on the repository, so a cached
repository cannot carry one caller's observer into another's call.

Caching: resolved student pages are cached in `_resolved` for the
lifetime of the repository, which tools.py keeps for the lifetime of the
process. That suits the CLI, where each invocation is a fresh process; a
long-lived service would need per-run scoping or invalidation, since a
renamed or moved student page would otherwise stay resolved to its old id.
"""

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from notion_client import (
    DEFAULT_NOTION_VERSION,
    MalformedNotionResponseError,
    NotionClient,
    NotionConfigError,
    NotionError,
    NotionObserver,
    notify_observer,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_TRAVERSAL_DEPTH = 6

NOTES_LABEL = "notes"
TRANSCRIPT_LABEL = "transcript"

# Blocks whose children are separate pages/databases: they are rendered
# (child_page by title) but never traversed.
_NON_TRAVERSED_TYPES = frozenset({"child_page", "child_database"})

_PATH_RE = re.compile(r"^\s*path\s*:\s*(\S.*?)\s*$", re.IGNORECASE)
_CURRENT_PROJECT_RE = re.compile(
    r"^\s*current\s+project\s*:\s*(\S.*?)\s*$", re.IGNORECASE
)


class StudentNotFoundError(NotionError):
    """No Notion page exactly matched the student under the parent page."""


class AmbiguousStudentMatchError(NotionError):
    """More than one Notion page exactly matched the student."""


class TraversalDepthExceededError(NotionError):
    """Block nesting went deeper than the configured traversal limit."""


@dataclass(frozen=True)
class NotionConfig:
    """Notion settings read from the environment. Secret and id fields are
    excluded from repr so the config can't leak them if printed."""

    api_key: str = field(repr=False)
    parent_page_id: str = field(repr=False)
    notion_version: str = DEFAULT_NOTION_VERSION
    max_traversal_depth: int = DEFAULT_MAX_TRAVERSAL_DEPTH

    @classmethod
    def from_env(cls, env) -> "NotionConfig":
        missing = [
            name
            for name in ("NOTION_API_KEY", "NOTION_PARENT_PAGE_ID")
            if not env.get(name)
        ]
        if missing:
            raise NotionConfigError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )

        depth = DEFAULT_MAX_TRAVERSAL_DEPTH
        raw_depth = env.get("NOTION_MAX_TRAVERSAL_DEPTH")
        if raw_depth:
            try:
                depth = int(raw_depth)
            except ValueError:
                depth = 0
            if depth < 1:
                raise NotionConfigError(
                    "NOTION_MAX_TRAVERSAL_DEPTH must be a positive integer."
                )

        return cls(
            api_key=env["NOTION_API_KEY"],
            parent_page_id=env["NOTION_PARENT_PAGE_ID"],
            notion_version=env.get("NOTION_VERSION") or DEFAULT_NOTION_VERSION,
            max_traversal_depth=depth,
        )


@dataclass
class _Node:
    block: dict
    children: list["_Node"]

    @property
    def type(self) -> str:
        return self.block["type"]


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


class _RetrievalStats:
    """Counters for one repository call, created per call and never stored.

    Also serves as the observer handed to the client: it tallies the
    client's notion_request events and forwards them to the caller's
    observer, if there is one.
    """

    def __init__(self, observer: NotionObserver | None):
        self._observer = observer
        self.requests_made = 0
        self.pages_fetched = 0
        self.blocks_examined = 0
        self.maximum_depth_reached: int | None = None
        self.cache_hit: bool | None = None
        self.session_found: bool | None = None

    def __call__(self, event: str, fields: dict) -> None:
        if event == "notion_request":
            self.requests_made += fields["request_count"]
            self.pages_fetched += fields["pages_fetched"]
        notify_observer(self._observer, event, fields)

    def listing_at(self, depth: int) -> None:
        if self.maximum_depth_reached is None or depth > self.maximum_depth_reached:
            self.maximum_depth_reached = depth

    def finish(
        self,
        retrieval_type: str,
        start: float,
        status: str,
        exc: Exception | None = None,
        **outcome,
    ) -> None:
        """Send the notion_retrieval summary. `outcome` sets the
        retrieval-specific fields; the rest stay None."""
        if self._observer is None:
            return
        fields = {
            "retrieval_type": retrieval_type,
            "duration_ms": _elapsed_ms(start),
            "status": status,
            "requests_made": self.requests_made,
            "pages_fetched": self.pages_fetched,
            "blocks_examined": self.blocks_examined,
            "maximum_depth_reached": self.maximum_depth_reached,
            "cache_hit": self.cache_hit,
            "path_found": None,
            "project_found": None,
            "session_found": self.session_found,
            "notes_source": None,
            "notes_char_count": None,
            "error_type": type(exc).__name__ if exc is not None else None,
        }
        fields.update(outcome)
        notify_observer(self._observer, "notion_retrieval", fields)


def _failure_status(exc: Exception) -> str:
    if isinstance(exc, StudentNotFoundError):
        return "not_found"
    if isinstance(exc, AmbiguousStudentMatchError):
        return "ambiguous"
    return "error"


def normalize_title(text: str) -> str:
    """Case-insensitive, whitespace-collapsed form used for exact matching."""
    return " ".join(text.split()).casefold()


def _normalize_id(value) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.replace("-", "").lower()


def _join_plain_text(rich_text) -> str:
    if not isinstance(rich_text, list):
        return ""
    return "".join(
        item.get("plain_text") or ""
        for item in rich_text
        if isinstance(item, dict)
    )


def _block_type(block: dict) -> str:
    block_type = block.get("type")
    if not isinstance(block_type, str) or not block_type:
        raise MalformedNotionResponseError("A Notion block was missing its type.")
    return block_type


def _block_id(block: dict) -> str:
    block_id = block.get("id")
    if not isinstance(block_id, str) or not block_id:
        raise MalformedNotionResponseError("A Notion block was missing its id.")
    return block_id


def _block_text(block: dict) -> str | None:
    """The full plain text of a block's rich_text, or None if it has none."""
    payload = block.get(block.get("type"))
    if not isinstance(payload, dict) or "rich_text" not in payload:
        return None
    return _join_plain_text(payload["rich_text"])


def _block_label(block: dict) -> str:
    text = _block_text(block)
    return normalize_title(text) if text else ""


def _child_page_title(block: dict) -> str:
    payload = block.get("child_page")
    title = payload.get("title") if isinstance(payload, dict) else None
    return title if isinstance(title, str) else ""


def _page_title(page: dict) -> str | None:
    """Title of a search-result page, from its title-typed property."""
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return None
    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return _join_plain_text(prop.get("title"))
    return None


def parse_session_datetime(title) -> datetime | None:
    """Parse a session page title as a timezone-aware ISO 8601 datetime.

    Returns None for anything else -- malformed, date-only, or timezone-less
    titles are not sessions. No timezone is ever inferred.
    """
    if not isinstance(title, str):
        return None
    try:
        parsed = datetime.fromisoformat(title.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _format_line(node: _Node, number: int) -> str | None:
    """One readable line for a block, or None if it has nothing to show."""
    block_type = node.type
    if block_type == "child_page":
        title = _child_page_title(node.block)
        return f"[Page] {title}" if title else None

    text = _block_text(node.block)
    if not text:
        return None
    if block_type == "heading_1":
        return f"# {text}"
    if block_type == "heading_2":
        return f"## {text}"
    if block_type == "heading_3":
        return f"### {text}"
    if block_type == "bulleted_list_item":
        return f"- {text}"
    if block_type == "numbered_list_item":
        return f"{number}. {text}"
    if block_type == "to_do":
        checked = node.block["to_do"].get("checked") is True
        return f"[{'x' if checked else ' '}] {text}"
    if block_type == "quote":
        return f"> {text}"
    # paragraph, toggle, callout, and any other rich_text block.
    return text


def _render(nodes: list[_Node], lines: list[str], indent: int = 0) -> list[str]:
    """Render nodes depth-first in document order, indenting nested blocks.
    Container blocks without text (e.g. tabs, columns) add no indentation."""
    number = 0
    for node in nodes:
        number = number + 1 if node.type == "numbered_list_item" else 0
        line = _format_line(node, number)
        if line is not None:
            lines.append("  " * indent + line)
        _render(node.children, lines, indent + 1 if line is not None else indent)
    return lines


def _raw_text_lines(nodes: list[_Node]):
    """Every line of block text, unformatted, in document order."""
    for node in nodes:
        text = _block_text(node.block)
        if text:
            yield from text.splitlines()
        yield from _raw_text_lines(node.children)


def _first_match(pattern: re.Pattern, lines: list[str]) -> str | None:
    for line in lines:
        match = pattern.match(line)
        if match:
            return match.group(1)
    return None


def _find_notes_branch(nodes: list[_Node]) -> _Node | None:
    """First block (pre-order) labelled exactly "Notes" that has children."""
    for node in nodes:
        if node.block.get("has_children") is True and _block_label(node.block) == NOTES_LABEL:
            return node
        found = _find_notes_branch(node.children)
        if found is not None:
            return found
    return None


class NotionStudentRepository:
    """Read-only student lookups against pages under one Notion parent page."""

    def __init__(
        self,
        client: NotionClient,
        parent_page_id: str,
        *,
        max_traversal_depth: int = DEFAULT_MAX_TRAVERSAL_DEPTH,
    ):
        parent = _normalize_id(parent_page_id)
        if parent is None:
            raise NotionConfigError("A Notion parent page id is required.")
        if max_traversal_depth < 1:
            raise NotionConfigError("The traversal depth limit must be at least 1.")
        self._client = client
        self._parent_id = parent
        self.max_traversal_depth = max_traversal_depth
        # normalized student name -> (page id, page title). Lives as long as
        # the repository; see "Caching" in the module docstring.
        self._resolved: dict[str, tuple[str, str]] = {}

    def __repr__(self) -> str:
        return "NotionStudentRepository()"

    @classmethod
    def from_env(cls, env=None) -> "NotionStudentRepository":
        config = NotionConfig.from_env(os.environ if env is None else env)
        client = NotionClient(config.api_key, config.notion_version)
        return cls(
            client,
            config.parent_page_id,
            max_traversal_depth=config.max_traversal_depth,
        )

    # -- public operations ------------------------------------------------

    def get_student_context(
        self, name: str, *, observer: NotionObserver | None = None
    ) -> dict:
        """Return the student's page title, readable profile text, and the
        parsed Path and Current project values (None when absent)."""
        start = time.perf_counter()
        stats = _RetrievalStats(observer)
        try:
            page_id, title = self._resolve_student_page(name, stats)
            nodes = self._fetch_children(page_id, 0, stats)
            raw_lines = list(_raw_text_lines(nodes))
            context = {
                "title": title,
                "profile_text": "\n".join(_render(nodes, [])),
                "path": _first_match(_PATH_RE, raw_lines),
                "current_project": _first_match(_CURRENT_PROJECT_RE, raw_lines),
            }
        except Exception as exc:
            self._log_failure("get_student_context", start, exc)
            stats.finish("student_context", start, _failure_status(exc), exc)
            raise
        logger.info(
            "Notion op=get_student_context status=success blocks=%d "
            "path_found=%s current_project_found=%s duration_ms=%s",
            stats.blocks_examined,
            context["path"] is not None,
            context["current_project"] is not None,
            _elapsed_ms(start),
        )
        stats.finish(
            "student_context",
            start,
            "success",
            path_found=context["path"] is not None,
            project_found=context["current_project"] is not None,
        )
        return context

    def get_latest_session_notes(
        self, name: str, *, observer: NotionObserver | None = None
    ) -> dict:
        """Return the most recent session's title, datetime, and notes, or
        {"latest_session": None} when the student has no session pages."""
        start = time.perf_counter()
        stats = _RetrievalStats(observer)
        try:
            result = self._latest_session_notes(name, stats)
        except Exception as exc:
            self._log_failure("get_latest_session_notes", start, exc)
            stats.finish("latest_session_notes", start, _failure_status(exc), exc)
            raise
        session = result["latest_session"]
        logger.info(
            "Notion op=get_latest_session_notes status=success session_found=%s "
            "notes_found=%s notes_source=%s blocks=%d duration_ms=%s",
            session is not None,
            bool(session and session["notes"]),
            session["notes_source"] if session else None,
            stats.blocks_examined,
            _elapsed_ms(start),
        )
        if session is None:
            stats.finish("latest_session_notes", start, "no_session", notes_source="none")
        else:
            stats.finish(
                "latest_session_notes",
                start,
                "success",
                notes_source=session["notes_source"],
                notes_char_count=len(session["notes"]),
            )
        return result

    # -- internals --------------------------------------------------------

    def _latest_session_notes(self, name: str, stats: _RetrievalStats) -> dict:
        page_id, _ = self._resolve_student_page(name, stats)

        sessions = []
        stats.listing_at(0)
        top_level = self._client.list_block_children(page_id, observer=stats)
        stats.blocks_examined += len(top_level)
        for block in top_level:
            if _block_type(block) != "child_page":
                continue
            title = _child_page_title(block)
            session_datetime = parse_session_datetime(title)
            if session_datetime is None:
                continue
            sessions.append((session_datetime, title.strip(), _block_id(block)))

        logger.info(
            "Notion session pages: child_pages_valid=%d top_level_blocks=%d",
            len(sessions),
            len(top_level),
        )
        stats.session_found = bool(sessions)
        if not sessions:
            return {"latest_session": None}

        # max() keeps the first of equal datetimes, so ties resolve to
        # document order rather than to anything arbitrary.
        session_datetime, title, session_id = max(sessions, key=lambda s: s[0])

        nodes = self._fetch_children(session_id, 0, stats)
        notes_branch = _find_notes_branch(nodes)
        if notes_branch is not None:
            content, source = notes_branch.children, "notes_branch"
        else:
            content, source = nodes, "page"

        return {
            "latest_session": {
                "title": title,
                "session_datetime": session_datetime.isoformat(),
                "notes_source": source,
                "notes": "\n".join(_render(content, [])),
            }
        }

    def _resolve_student_page(self, name: str, stats: _RetrievalStats) -> tuple[str, str]:
        target = normalize_title(name) if isinstance(name, str) else ""
        if not target:
            raise StudentNotFoundError("A student name is required.")
        stats.cache_hit = target in self._resolved
        if stats.cache_hit:
            return self._resolved[target]

        results = self._client.search_pages(name.strip(), observer=stats)
        matches: dict[str, tuple[str, str]] = {}
        for page in results:
            if page.get("object") != "page":
                continue
            if page.get("in_trash") is True or page.get("archived") is True:
                continue
            parent = page.get("parent")
            if not isinstance(parent, dict) or parent.get("type") != "page_id":
                continue
            if _normalize_id(parent.get("page_id")) != self._parent_id:
                continue
            title = _page_title(page)
            if title is None or normalize_title(title) != target:
                continue
            page_id = _block_id(page)
            # Keyed by normalized id: the same page appearing on two search
            # result pages is one match, not an ambiguity.
            matches.setdefault(_normalize_id(page_id), (page_id, title))

        logger.info(
            "Notion student lookup: search_results=%d exact_match_found=%s "
            "exact_matches=%d",
            len(results),
            len(matches) == 1,
            len(matches),
        )
        if not matches:
            raise StudentNotFoundError(
                "No Notion page under the configured parent page exactly "
                "matched the requested student name."
            )
        if len(matches) > 1:
            raise AmbiguousStudentMatchError(
                "Multiple Notion pages under the configured parent page exactly "
                "matched the requested student name."
            )
        resolved = next(iter(matches.values()))
        self._resolved[target] = resolved
        return resolved

    def _fetch_children(self, parent_id: str, depth: int, stats: _RetrievalStats) -> list[_Node]:
        """Fetch a block's descendants in document order.

        `depth` is the nesting level of the blocks being fetched (a page's
        direct children are depth 0). Branches labelled "Transcript" are
        dropped without fetching their children; child pages and databases
        are never entered.
        """
        if depth > self.max_traversal_depth:
            raise TraversalDepthExceededError(
                f"Notion block nesting exceeded the traversal depth limit "
                f"({self.max_traversal_depth})."
            )
        stats.listing_at(depth)
        nodes = []
        for block in self._client.list_block_children(parent_id, observer=stats):
            block_type = _block_type(block)
            stats.blocks_examined += 1
            if _block_label(block) == TRANSCRIPT_LABEL:
                continue
            children = []
            if block_type not in _NON_TRAVERSED_TYPES and block.get("has_children") is True:
                children = self._fetch_children(_block_id(block), depth + 1, stats)
            nodes.append(_Node(block, children))
        return nodes

    @staticmethod
    def _log_failure(operation: str, start: float, exc: Exception) -> None:
        logger.warning(
            "Notion op=%s status=failure error=%s duration_ms=%s",
            operation,
            type(exc).__name__,
            _elapsed_ms(start),
        )
