import json
import os

from anthropic import Anthropic

from prompts import build_instructions
from tools import TOOLS, execute_tool

model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
api_key = os.environ["ANTHROPIC_API_KEY"]
MAX_TURNS = 5


def run_agent(
    student_name: str,
    mode: str,
    transcript: str | None = None,
    session_datetime: str | None = None,
):
    # Raises a value error for an unexpected mode
    if mode not in {"pre_session", "post_session", "new_student"}:
        raise ValueError(
            "Mode must be 'pre_session' or 'post_session'."
        )
    if mode == "post_session" and not transcript:
        raise ValueError(
            "A transcript is required for post_session mode."
        )
    if mode == "post_session" and not session_datetime:
        raise ValueError(
            "Session date and time are required for post_session mode."
        )

    client = Anthropic()
    instructions = build_instructions(mode)

    content = [
        {
            "type": "text",
            "text": f"Student name: {student_name}",
        }
    ]

    if mode == "post_session":
        content.append(
            {
                "type": "text",
                "text": (
                    f"Session date and time: {session_datetime}\n\n"
                    f"Transcript:\n{transcript}"
                ),
            }
        )

    messages=[
        {
            "role": "user",
            "content": content,
        }
    ]

    for turn in range(MAX_TURNS):
        response = client.messages.create(
            model=model,
            max_tokens=5000,
            tools=TOOLS,
            system=instructions,
            messages=messages,
        )
        print(f"\n--- Turn {turn + 1} ---")
        print(f"Stop reason: {response.stop_reason}")
        print(
            f"Tokens: {response.usage.input_tokens} input, "
            f"{response.usage.output_tokens} output"
        )

        if response.stop_reason == "end_turn":
            return response

        if response.stop_reason == "tool_use":
            messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                }
            )
            tool_results = []

            for block in response.content:
                if block.type == "thinking" and block.thinking:
                    print(f"Thinking: {block.thinking}")

                if block.type == "tool_use":
                    print(f"Executing tool: {block.name}")
                    result = execute_tool(block.name, block.input)
                    print (
                        "Tool result: ",
                        json.dumps(result, indent=2),
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        }
                    )
                    print(f"Tool requested: {block.name}")
                    print(f"Tool use ID: {block.id}")
                    print(
                        "Tool input:",
                        json.dumps(block.input, indent=2),
                    )

            messages.append(
                {
                    "role": "user",
                    "content": tool_results
                }
            )

            continue

        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                "Claude reached max_tokens before finishing."
            )

    raise RuntimeError(
        f"Agent did not finish within {MAX_TURNS} turns."
    )
