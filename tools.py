"""Tool schemas and implementations for the mentor agent.

Three tools are exposed to the model: `get_student_context` (student record
lookup), `get_latest_session_notes` (the most recent previous session's
notes), and `get_path_context` (learning-path reference retrieval). The
path tool is separate because it depends on data returned by the first --
the model reads a student's path from get_student_context, then requests
that path's guide from get_path_context.

Student context and session notes come from a backend selected explicitly
by STUDENT_CONTEXT_BACKEND: "json" (a local student file; the default and
the public example) or "notion" (read-only Notion pages). The backend is
never inferred from which credentials happen to be set, and a failing
Notion backend never falls back to the JSON file.

The student tools accept an optional `observer` that is forwarded, per
call, to the Notion backend for metadata-only retrieval events (see
notion_client.NotionObserver). The JSON backend ignores it.
"""

import json
import os
from pathlib import Path

from notion_client import NotionObserver
from notion_repository import NotionStudentRepository

PROJECT_DIR = Path(__file__).parent

BACKENDS = ("json", "notion")

STUDENT_CONTEXT_BACKEND = os.environ.get("STUDENT_CONTEXT_BACKEND", "json")

STUDENTS_PATH = Path(
    os.environ.get("STUDENTS_DATA_PATH", PROJECT_DIR / "data" / "students.example.json")
)

PATH_REFERENCES_DIR = Path(
    os.environ.get(
        "REFERENCE_ROOT",
        PROJECT_DIR / "references" / "example",
    )
)


PATH_INDEX_PATH = PATH_REFERENCES_DIR / "path_index.json"

# Maps a learning path's display name (as used in student records and
# tool calls) to its reference guide's filename.
PATH_FILES = json.loads(
    PATH_INDEX_PATH.read_text(encoding="utf-8")
)


def load_students() -> dict:
    """Load the local student record store."""
    return json.loads(STUDENTS_PATH.read_text(encoding="utf-8"))


TOOLS = [
    {
        "name": "get_student_context",
        "description": (
            "Get the current student's context, including what path & project they're on."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name of the student."
                },
            },
            "required": ["name"],
        }
    },
    {
        "name": "get_latest_session_notes",
        "description": (
            "Get the notes from the student's most recent previous mentoring "
            "session, with the session's date/time when available. Returns "
            "latest_session as null when no previous session is recorded. "
            "Never includes session transcripts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name of the student."
                },
            },
            "required": ["name"],
        }
    },
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
    }
]


_notion_repository: NotionStudentRepository | None = None


def student_context_backend() -> str:
    """Return the configured backend, rejecting anything unrecognized."""
    if STUDENT_CONTEXT_BACKEND not in BACKENDS:
        raise ValueError(
            "STUDENT_CONTEXT_BACKEND must be one of: " + ", ".join(BACKENDS)
        )
    return STUDENT_CONTEXT_BACKEND


def get_notion_repository() -> NotionStudentRepository:
    """Create the Notion repository on first use, from the environment.

    Lazy so that importing this module never requires Notion settings.
    The repository is shared by every later call in the process, so
    per-run state (such as a tracing observer) is passed per call and
    never attached to it.
    """
    global _notion_repository
    if _notion_repository is None:
        _notion_repository = NotionStudentRepository.from_env()
    return _notion_repository


def get_student_context(
    name: str, observer: NotionObserver | None = None
) -> dict | None:
    """Look up a student's stored context by name.

    JSON backend: the stored record, or None if not found. The record's
    student_id is a tracing identifier, not context for the model, so it
    is removed from a copy of the record before returning.

    Notion backend: title, profile_text, path, and current_project. A
    missing or ambiguous student raises rather than returning None.
    """
    if student_context_backend() == "notion":
        return get_notion_repository().get_student_context(name, observer=observer)

    students = load_students()
    record = students.get(name.strip())
    if record is None:
        return None
    context = record.copy()
    context.pop("student_id", None)
    return context


def get_student_id(name: str) -> str | None:
    """Return the explicit, non-identifying student_id stored in a
    student's record, or None if the student or the field is missing.

    Used only to tag run traces; never derived from the name. The Notion
    backend has no student_id yet, so it always returns None.
    """
    if student_context_backend() == "notion":
        return None

    record = load_students().get(name.strip())
    if not isinstance(record, dict):
        return None
    student_id = record.get("student_id")
    return student_id if isinstance(student_id, str) and student_id else None


def get_latest_session_notes(
    name: str, observer: NotionObserver | None = None
) -> dict | None:
    """Return the student's most recent previous session notes.

    JSON backend: {"latest_session": <the record's previous_session or
    None>}, or None if the student is not found.

    Notion backend: {"latest_session": {title, session_datetime,
    notes_source, notes}} or {"latest_session": None} when the student has
    no session pages. Transcripts are never retrieved.
    """
    if student_context_backend() == "notion":
        return get_notion_repository().get_latest_session_notes(name, observer=observer)

    record = load_students().get(name.strip())
    if record is None:
        return None
    return {"latest_session": record.get("previous_session")}


def get_path_context(path: str) -> dict[str, str]:
    """Return the complete Markdown reference guide for a learning path.

    This intentionally returns the whole guide rather than a targeted
    excerpt -- the model is expected to locate the section relevant to
    the student's current project itself. See README for rationale.
    """
    if path not in PATH_FILES:
        raise ValueError(
            f"Path name not recognized. "
            f"Available path names are: {list(PATH_FILES.keys())}"
        )

    file_path = PATH_REFERENCES_DIR / PATH_FILES[path]
    reference_content = file_path.read_text(encoding="utf-8")

    return {
        "path": path,
        "reference": reference_content,
    }


def execute_tool(
    tool_name: str, tool_input: dict, observer: NotionObserver | None = None
) -> dict | None:
    """Dispatch a tool_use request by name to its implementation.

    `observer`, if given, receives Notion retrieval events for this call
    only (Notion backend only).
    """
    if tool_name == "get_student_context":
        return get_student_context(tool_input["name"], observer)

    if tool_name == "get_latest_session_notes":
        return get_latest_session_notes(tool_input["name"], observer)

    if tool_name == "get_path_context":
        return get_path_context(tool_input["path"])

    raise ValueError(f"Unknown tool: {tool_name}")
