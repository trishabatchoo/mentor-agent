import json

from agent import run_agent

# Mock, sanitized transcript
transcript_path = "fixtures/post_session_transcript.txt"
with open(transcript_path, "r", encoding="utf-8") as file:
    transcript = file.read()


if __name__ == "__main__":
    response = run_agent(
        student_name="Amya",
        mode="post_session",
        transcript=transcript,
        session_datetime="September 8, 2026 11:00 AM",
    )
    print("Stop reason:", response.stop_reason)
    print("Usage:", response.usage)
    print(
    json.dumps(
        [block.model_dump() for block in response.content],
        indent=2,
    )
)
