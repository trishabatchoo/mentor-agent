"""Tool schemas and implementations for the mentor agent.

Two tools are exposed to the model: `get_student_context` (student record
lookup) and `get_path_context` (learning-path reference retrieval). They
are kept separate because the second call depends on data returned by the
first -- the model reads a student's path from get_student_context, then
requests that path's guide from get_path_context.
"""

import json
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).parent

STUDENTS_PATH = Path(
    os.environ.get("STUDENTS_DATA_PATH", PROJECT_DIR / "data" / "students.example.json")
)

PATH_REFERENCES_DIR= Path(
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


def get_student_context(name: str) -> dict | None:
    """Look up a student's stored context by name, or None if not found."""
    students = load_students()
    return students.get(name.strip())


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


def execute_tool(tool_name: str, tool_input: dict) -> dict | None:
    """Dispatch a tool_use request by name to its implementation."""
    if tool_name == "get_student_context":
        return get_student_context(tool_input["name"])

    if tool_name == "get_path_context":
        return get_path_context(tool_input["path"])

    raise ValueError(f"Unknown tool: {tool_name}")
