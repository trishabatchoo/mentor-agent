#!/usr/bin/env python3
"""Profile-based launcher for the mentor agent.

Usage:
    python3 run.py example
    python3 run.py local --student Amya \
        --transcript fixtures/private/amya-september-8.txt \
        --session-datetime "September 8, 2026 11:00 AM ET"

Selects a configuration profile (public synthetic example, or private
local setup) and runs the agent with that profile's prompts, student
data, reference material, transcript, and session date/time.

This launcher never sets secrets. ANTHROPIC_API_KEY, ANTHROPIC_MODEL,
and the NOTION_* variables continue to come from the parent shell
environment untouched -- only the profile's own configuration variables
(PROMPTS_DIR, STUDENTS_DATA_PATH, REFERENCE_ROOT, STUDENT_CONTEXT_BACKEND,
TRANSCRIPT_PATH, MENTOR_STUDENT_NAME, SESSION_DATETIME) are set here. For
the Notion backend it checks only that the required NOTION_* variables are
present; their values are never printed.
"""

import argparse
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()

# Profile configuration. Paths are relative to this file's directory.
# "default_student" is only used as a fallback for the public example --
# the local profile has none, so a student must always be specified
# explicitly for it (see resolve_student_name). The transcript and
# session date/time similarly default only for 'example' (see
# resolve_transcript_path / resolve_session_datetime).
#
# "STUDENT_CONTEXT_BACKEND" selects where student context and session notes
# come from. Only the "json" backend uses STUDENTS_DATA_PATH, so profiles
# using "notion" don't define it.
PROFILES = {
    "example": {
        "PROMPTS_DIR": "prompts/example",
        "STUDENTS_DATA_PATH": "data/students.example.json",
        "REFERENCE_ROOT": "references/example",
        "STUDENT_CONTEXT_BACKEND": "json",
        "default_student": "Jordan",
    },
    "local": {
        "PROMPTS_DIR": "prompts/local",
        "REFERENCE_ROOT": "references/local",
        "STUDENT_CONTEXT_BACKEND": "notion",
        "default_student": None,
    },
}

# Environment variables each backend needs. Checked for presence only.
BACKEND_REQUIRED_ENV = {
    "json": (),
    "notion": ("NOTION_API_KEY", "NOTION_PARENT_PAGE_ID"),
}

REQUIRED_PROMPT_FILES = (
    "shared.md",
    "pre_session.md",
    "post_session.md",
    "new_student.md",
)

# Fallbacks used only for the public 'example' profile.
DEFAULT_TRANSCRIPT_PATH = "fixtures/example/post_session_transcript.txt"
DEFAULT_SESSION_DATETIME = "September 8, 2026 11:00 AM ET"


def parse_args(argv=None):
    """Parse CLI arguments: a required profile and optional overrides."""
    parser = argparse.ArgumentParser(
        prog="run.py",
        description=(
            "Run the mentor agent using a named configuration profile."
        ),
        epilog=(
            "Profiles:\n"
            "  example   public synthetic data (default student: Jordan)\n"
            "  local     private local configuration, student context from\n"
            "            Notion (student, transcript, and session date/time\n"
            "            all required; NOTION_API_KEY and NOTION_PARENT_PAGE_ID\n"
            "            must be set)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "profile",
        choices=sorted(PROFILES),
        help="Configuration profile to run: 'example' or 'local'.",
    )
    parser.add_argument(
        "--student",
        default=None,
        metavar="NAME",
        help=(
            "Student name to use. Overrides MENTOR_STUDENT_NAME. Required "
            "for the 'local' profile unless MENTOR_STUDENT_NAME is set."
        ),
    )
    parser.add_argument(
        "--transcript",
        default=None,
        metavar="PATH",
        help=(
            "Path to the session transcript. Overrides TRANSCRIPT_PATH. "
            "Relative paths are resolved against the project directory; "
            "absolute paths are used as-is. Required for the 'local' "
            "profile unless TRANSCRIPT_PATH is set."
        ),
    )
    parser.add_argument(
        "--session-datetime",
        default=None,
        metavar="DATETIME",
        help=(
            "Session date and time, e.g. 'September 8, 2026 11:00 AM ET'. "
            "Overrides SESSION_DATETIME. Required for the 'local' profile "
            "unless SESSION_DATETIME is set."
        ),
    )
    return parser.parse_args(argv)


def resolve_student_name(profile_name, cli_student, env):
    """Resolve the student name for a run.

    Precedence:
      1. --student command-line argument
      2. an existing MENTOR_STUDENT_NAME environment variable
      3. "Jordan", only when the selected profile is 'example'
      4. otherwise (local, with neither of the above): a clear error
    """
    if cli_student:
        return cli_student

    if env.get("MENTOR_STUDENT_NAME"):
        return env["MENTOR_STUDENT_NAME"]

    default_student = PROFILES[profile_name]["default_student"]
    if default_student:
        return default_student

    raise SystemExit(
        f"error: no student specified for profile '{profile_name}'.\n"
        "Pass --student <name> or set the MENTOR_STUDENT_NAME "
        "environment variable."
    )


def resolve_transcript_path(profile_name, cli_transcript, env):
    """Resolve the transcript path for a run, as an absolute Path.

    Precedence:
      1. --transcript command-line argument
      2. an existing TRANSCRIPT_PATH environment variable
      3. fixtures/example/post_session_transcript.txt, only when the
         selected profile is 'example'
      4. otherwise (local, with neither of the above): a clear error

    Relative paths (from any of the three sources above) are resolved
    against PROJECT_DIR; absolute paths are returned unmodified.
    """
    if cli_transcript:
        candidate = cli_transcript
    elif env.get("TRANSCRIPT_PATH"):
        candidate = env["TRANSCRIPT_PATH"]
    elif profile_name == "example":
        candidate = DEFAULT_TRANSCRIPT_PATH
    else:
        raise SystemExit(
            f"error: no transcript specified for profile '{profile_name}'.\n"
            "Pass --transcript <path> or set the TRANSCRIPT_PATH "
            "environment variable."
        )

    candidate_path = Path(candidate)
    if candidate_path.is_absolute():
        return candidate_path
    return PROJECT_DIR / candidate_path


def resolve_session_datetime(profile_name, cli_datetime, env):
    """Resolve the session date/time string for a run.

    Precedence:
      1. --session-datetime command-line argument
      2. an existing SESSION_DATETIME environment variable
      3. "September 8, 2026 11:00 AM ET", only when the selected profile
         is 'example'
      4. otherwise (local, with neither of the above): a clear error
    """
    if cli_datetime:
        return cli_datetime

    if env.get("SESSION_DATETIME"):
        return env["SESSION_DATETIME"]

    if profile_name == "example":
        return DEFAULT_SESSION_DATETIME

    raise SystemExit(
        f"error: no session date/time specified for profile '{profile_name}'.\n"
        "Pass --session-datetime <value> or set the SESSION_DATETIME "
        "environment variable."
    )


def validate_profile_files(profile_name, transcript_path):
    """Return a list of required paths for a run that are missing.

    Checks the profile's prompt files, student data, and reference index,
    plus the already-resolved `transcript_path`. Checked, but never read
    or printed: file contents are never inspected here, only whether each
    required path exists.
    """
    profile = PROFILES[profile_name]
    missing = []

    prompts_dir = PROJECT_DIR / profile["PROMPTS_DIR"]
    for filename in REQUIRED_PROMPT_FILES:
        path = prompts_dir / filename
        if not path.is_file():
            missing.append(str(path))

    if "STUDENTS_DATA_PATH" in profile:
        students_path = PROJECT_DIR / profile["STUDENTS_DATA_PATH"]
        if not students_path.is_file():
            missing.append(str(students_path))

    reference_index = PROJECT_DIR / profile["REFERENCE_ROOT"] / "path_index.json"
    if not reference_index.is_file():
        missing.append(str(reference_index))

    if not transcript_path.is_file():
        missing.append(str(transcript_path))

    return missing


def missing_backend_env(profile_name, env):
    """Return the names of environment variables the profile's backend
    requires but that are unset or empty. Values are never returned."""
    backend = PROFILES[profile_name]["STUDENT_CONTEXT_BACKEND"]
    return [name for name in BACKEND_REQUIRED_ENV[backend] if not env.get(name)]


def apply_profile_env(profile_name, student_name, transcript_path, session_datetime, env=None):
    """Write a run's resolved configuration into `env` (defaults to the
    real process environment, os.environ).

    Only the configuration keys below are set. STUDENTS_DATA_PATH is set
    for profiles that define it and removed for those that don't, so a
    stale value inherited from the shell can't apply. Everything else
    already present in `env` -- notably ANTHROPIC_API_KEY, ANTHROPIC_MODEL,
    and NOTION_* inherited from the parent shell -- is left untouched.
    """
    if env is None:
        env = os.environ

    profile = PROFILES[profile_name]
    env["PROMPTS_DIR"] = str(PROJECT_DIR / profile["PROMPTS_DIR"])
    if "STUDENTS_DATA_PATH" in profile:
        env["STUDENTS_DATA_PATH"] = str(PROJECT_DIR / profile["STUDENTS_DATA_PATH"])
    else:
        env.pop("STUDENTS_DATA_PATH", None)
    env["REFERENCE_ROOT"] = str(PROJECT_DIR / profile["REFERENCE_ROOT"])
    env["STUDENT_CONTEXT_BACKEND"] = profile["STUDENT_CONTEXT_BACKEND"]
    env["TRANSCRIPT_PATH"] = str(transcript_path)
    env["MENTOR_STUDENT_NAME"] = student_name
    env["SESSION_DATETIME"] = session_datetime

    return env


def main(argv=None):
    args = parse_args(argv)

    student_name = resolve_student_name(args.profile, args.student, os.environ)
    transcript_path = resolve_transcript_path(args.profile, args.transcript, os.environ)
    session_datetime = resolve_session_datetime(
        args.profile, args.session_datetime, os.environ
    )

    missing = validate_profile_files(args.profile, transcript_path)
    if missing:
        print(
            f"error: profile '{args.profile}' is missing required files:",
            file=sys.stderr,
        )
        for path in missing:
            print(f"  - {path}", file=sys.stderr)
        raise SystemExit(1)

    missing_env = missing_backend_env(args.profile, os.environ)
    if missing_env:
        print(
            f"error: profile '{args.profile}' requires these environment "
            "variables to be set:",
            file=sys.stderr,
        )
        for name in missing_env:
            print(f"  - {name}", file=sys.stderr)
        raise SystemExit(1)

    # Configuration must land in the real process environment before
    # main.py (and, transitively, prompts.py / tools.py) is imported:
    # those modules read PROMPTS_DIR / STUDENTS_DATA_PATH / REFERENCE_ROOT /
    # STUDENT_CONTEXT_BACKEND at import time, not at call time.
    apply_profile_env(args.profile, student_name, transcript_path, session_datetime)

    import main as app_main

    app_main.main()


if __name__ == "__main__":
    main()
