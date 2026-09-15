import json
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
STUDENTS_PATH = PROJECT_DIR / "data" / "students.json"
PATH_REFERENCES_DIR = PROJECT_DIR / "references" / "paths"

PATH_FILES = {
    "Application Developer Apprenticeship":
        "application-development-apprenticeship.md",
    "Application Developer Skills Bootcamp":
        "application-developer-skills-bootcamp.md",
    "Data Analyst Apprenticeship":
        "data-analyst-apprenticeship.md",
    "Data Visualization with Power BI":
        "data-viz-power-bi.md",
}

def load_students() -> dict:
    with open(STUDENTS_PATH, "r", encoding="utf-8") as file:
        return json.load(file)

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
    students = load_students()
    return students.get(name.strip())

def get_path_context(path: str) -> dict:
    if path not in PATH_FILES:
        raise ValueError(
            f"Path name not recognized. "
            f"Available path names are: {list(PATH_FILES.keys())}"
        )

    file_path = PATH_REFERENCES_DIR / PATH_FILES[path]

    with open(file_path, "r", encoding="utf-8") as file:
        reference_content = file.read()

    return {
        "path": path,
        "reference": reference_content,
    }


def execute_tool(tool_name: str, tool_input: dict):
    if tool_name == "get_student_context":
        return get_student_context(tool_input["name"])

    if tool_name == "get_path_context":
        return get_path_context(tool_input["path"])

    raise ValueError(f"Unknown tool: {tool_name}")
