"""Local entry point: runs a sample post-session agent call."""

import logging
import os
from pathlib import Path

from agent import run_agent

# Set LOG_LEVEL=DEBUG for verbose diagnostic output (tool inputs, response
# metadata, truncated tool-result previews). Defaults to INFO.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(levelname)s %(name)s: %(message)s",
)


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

    response = run_agent(
        student_name=student_name,
        mode="post_session",
        transcript=transcript,
        session_datetime=session_datetime,
    )

    final_text = "\n".join(
        block.text for block in response.content if block.type == "text"
    )
    print("\n--- Final response ---")
    print(final_text)


if __name__ == "__main__":
    main()
