import json
import logging
import os
from typing import Literal

from anthropic import Anthropic
from anthropic.types import Message

from prompts import build_instructions
from tools import TOOLS, execute_tool

logger = logging.getLogger(__name__)

model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
api_key = os.environ["ANTHROPIC_API_KEY"]
MAX_TURNS = 5

# Characters of a tool result to show in terminal logs before truncating.
# Display only -- the full result is still sent to the model untouched.
LOG_TRUNCATE_CHARS = 300

Mode = Literal["pre_session", "post_session", "new_student"]


def _truncate_for_log(text: str, limit: int = LOG_TRUNCATE_CHARS) -> str:
    """Shorten `text` for terminal display. Never used for API payloads."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... ({len(text)} chars total)"


def run_agent(
    student_name: str,
    mode: Mode,
    transcript: str | None = None,
    session_datetime: str | None = None,
) -> Message:
    """Run the mentor agent loop and return the final Claude response.

    `mode` is typed as a Literal for editor/type-checker support, but that
    hint is erased at runtime, so we still validate it explicitly below.
    """
    if mode not in {"pre_session", "post_session", "new_student"}:
        logger.error("Invalid mode: %r", mode)
        raise ValueError(
            "Mode must be 'pre_session', 'post_session', or 'new_student'."
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

    messages = [
        {
            "role": "user",
            "content": content,
        }
    ]

    for turn in range(MAX_TURNS):
        # System instructions and tool definitions are resent every call:
        # the Messages API is stateless, so each request must carry the
        # full context the model needs, not just the newest message.
        response = client.messages.create(
            model=model,
            max_tokens=5000,
            tools=TOOLS,
            system=instructions,
            messages=messages,
        )

        logger.info(
            "Turn %d: stop_reason=%s tokens_in=%d tokens_out=%d",
            turn + 1,
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        logger.debug("Response id=%s model=%s", response.id, response.model)

        if response.stop_reason == "end_turn":
            logger.info("Agent completed in %d turn(s)", turn + 1)
            return response

        if response.stop_reason == "tool_use":
            # The assistant's tool_use blocks must be appended before the
            # matching tool_result blocks: the API pairs each tool_result
            # to a tool_use by id, and expects the tool_use turn to already
            # be in the conversation history when the result is sent back.
            messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                }
            )
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    logger.info("Executing tool: %s", block.name)
                    logger.debug("Tool use id: %s", block.id)
                    logger.debug("Tool input: %s", block.input)

                    try:
                        result = execute_tool(block.name, block.input)
                    except Exception:
                        logger.exception(
                            "Tool %s failed for input %s",
                            block.name,
                            block.input,
                        )
                        raise

                    result_json = json.dumps(result)

                    # Only the logged preview is truncated for readability.
                    # The full, untruncated result_json is what gets sent
                    # to the model below
                    logger.info(
                        "Tool result: %s", _truncate_for_log(result_json)
                    )

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_json,
                        }
                    )

            messages.append(
                {
                    "role": "user",
                    "content": tool_results,
                }
            )

            continue

        if response.stop_reason == "max_tokens":
            logger.error("Claude reached max_tokens before finishing.")
            raise RuntimeError(
                "Claude reached max_tokens before finishing."
            )

        # Unexpected stop_reason (e.g. "stop_sequence"): the loop falls
        # through to the next iteration and resends the same messages.
        logger.warning(
            "Unexpected stop_reason %r on turn %d; retrying.",
            response.stop_reason,
            turn + 1,
        )

    logger.error("Agent did not finish within %d turns.", MAX_TURNS)
    raise RuntimeError(
        f"Agent did not finish within {MAX_TURNS} turns."
    )
