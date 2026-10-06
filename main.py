"""Local entry point: runs a sample post-session agent call."""

import logging
import os
from pathlib import Path

from agent import run_agent
from tools import get_student_id

logger = logging.getLogger(__name__)

# Keep SDK and HTTP-client logs at INFO or higher, even when application
# logging is set to DEBUG. Anthropic SDK debug logs may include complete
# request payloads containing prompts, transcripts, and tool results.

THIRD_PARTY_LOGGERS = ("anthropic", "httpx2", "httpcore2")


def configure_logging(level: str) -> None:
    """Configure terminal logging at `level` for this project's loggers.

    Third-party loggers are never allowed below INFO, so LOG_LEVEL=DEBUG
    (or the SDK's own ANTHROPIC_LOG=debug, applied at import) can't print
    request bodies. Called after the SDK is imported so it takes precedence.
    """
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    floor = max(logging.getLogger().getEffectiveLevel(), logging.INFO)
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(floor)


# Set LOG_LEVEL=DEBUG for verbose diagnostic output (tool-use ids, tool input
# field names, response metadata). Defaults to INFO.
configure_logging(os.environ.get("LOG_LEVEL", "INFO").upper())


def main() -> None:
    # Defaults reproduce the original hardcoded public example when no
    # profile launcher (run.py) has set these environment variables.
    student_name = os.environ.get("MENTOR_STUDENT_NAME", "Jordan")
    transcript_path = Path(
        os.environ.get(
            "TRANSCRIPT_PATH", "fixtures/example/post_session_transcript.txt"
        )
    )
    session_datetime = os.environ.get("SESSION_DATETIME", "September 8, 2026 11:00 AM ET")
    transcript = transcript_path.read_text(encoding="utf-8")

    student_id = get_student_id(student_name)
    if student_id is None:
        # Deliberately does not include the student's name.
        logger.warning(
            "No student_id found for the selected student; trace will record null."
        )

    response = run_agent(
        student_name=student_name,
        mode="post_session",
        transcript=transcript,
        session_datetime=session_datetime,
        student_id=student_id,
    )

    final_text = "\n".join(
        block.text for block in response.content if block.type == "text"
    )
    print("\n--- Final response ---")
    print(final_text)


if __name__ == "__main__":
    main()
