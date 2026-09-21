"""Local entry point: runs a sample post-session agent call."""

import logging
import os
from pathlib import Path

from agent import run_agent

# Mock, sanitized transcript
TRANSCRIPT_PATH = Path("fixtures/post_session_transcript.txt")

# Set LOG_LEVEL=DEBUG for verbose diagnostic output (tool inputs, response
# metadata, truncated tool-result previews). Defaults to INFO.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(levelname)s %(name)s: %(message)s",
)


if __name__ == "__main__":
    transcript = TRANSCRIPT_PATH.read_text(encoding="utf-8")

    response = run_agent(
        student_name="Jordan",
        mode="post_session",
        transcript=transcript,
        session_datetime="September 8, 2026 11:00 AM",
    )

    final_text = "\n".join(
        block.text for block in response.content if block.type == "text"
    )
    print("\n--- Final response ---")
    print(final_text)
