import json
import logging
import os
import time
from typing import Literal

from anthropic import Anthropic
from anthropic.types import Message

from observability import RunTracer, text_sha256
from prompts import build_instructions
from tools import TOOLS, execute_tool

logger = logging.getLogger(__name__)

model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
api_key = os.environ["ANTHROPIC_API_KEY"]
MAX_TURNS = 5

Mode = Literal["pre_session", "post_session", "new_student"]


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


def run_agent(
    student_name: str,
    mode: Mode,
    transcript: str | None = None,
    session_datetime: str | None = None,
    student_id: str | None = None,
) -> Message:
    """Run the mentor agent loop and return the final Claude response.

    `student_id` is an already-resolved, non-identifying id used only to
    tag the run trace; it is never sent to the model. It is optional so
    existing callers keep working, at the cost that omitting it silently
    records student_id=null.

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

    # Tracing starts only once arguments are valid: invalid arguments are
    # caller errors, not runs. Exceptions are recorded and re-raised
    # unchanged; process-level signals (KeyboardInterrupt, SystemExit)
    # are deliberately not caught.
    tracer = RunTracer(mode=mode, model=model)
    # Only post_session sends the transcript to the model, so only that
    # mode records its size and fingerprint (null otherwise).
    has_transcript = mode == "post_session"
    tracer.run_started(
        student_id=student_id,
        transcript_char_count=len(transcript) if has_transcript else None,
        transcript_sha256=text_sha256(transcript) if has_transcript else None,
    )
    try:
        response = _run_loop(
            student_name, mode, transcript, session_datetime, tracer
        )
    except Exception as exc:
        tracer.run_failed(type(exc).__name__)
        raise
    tracer.run_completed()
    return response


def _run_loop(
    student_name: str,
    mode: Mode,
    transcript: str | None,
    session_datetime: str | None,
    tracer: RunTracer,
) -> Message:
    """The agent loop proper. Called only by run_agent, after validation."""
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
        call_start = time.perf_counter()
        try:
            response = client.messages.create(
                model=model,
                max_tokens=5000,
                tools=TOOLS,
                system=instructions,
                messages=messages,
            )
        except Exception:
            tracer.mark_failure("api_error")
            raise

        tracer.model_response(
            turn=turn + 1,
            duration_ms=_elapsed_ms(call_start),
            stop_reason=response.stop_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
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
                    # Field names only: tool input values and tool results
                    # can contain student data and are never logged.
                    logger.debug(
                        "Tool input fields: %s",
                        sorted(block.input) if isinstance(block.input, dict) else [],
                    )

                    # Scoped to this run and tool call: Notion events are
                    # tagged with this turn and tool_use_id by the tracer,
                    # so the Notion layer never sees either.
                    observer = tracer.notion_observer(
                        turn=turn + 1, tool_use_id=block.id
                    )
                    tool_start = time.perf_counter()
                    try:
                        result = execute_tool(
                            block.name, block.input, observer=observer
                        )
                    except Exception as exc:
                        tracer.tool_execution(
                            turn=turn + 1,
                            tool_name=block.name,
                            tool_use_id=block.id,
                            duration_ms=_elapsed_ms(tool_start),
                            error_type=type(exc).__name__,
                        )
                        tracer.mark_failure("tool_error")
                        # Exception type only; its message may echo inputs.
                        logger.error(
                            "Tool %s failed (tool_use_id=%s): %s",
                            block.name,
                            block.id,
                            type(exc).__name__,
                        )
                        raise

                    tracer.tool_execution(
                        turn=turn + 1,
                        tool_name=block.name,
                        tool_use_id=block.id,
                        duration_ms=_elapsed_ms(tool_start),
                    )

                    result_json = json.dumps(result)

                    logger.info(
                        "Tool %s returned %d chars", block.name, len(result_json)
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
            tracer.mark_failure("max_tokens")
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
    tracer.mark_failure("max_turns")
    raise RuntimeError(
        f"Agent did not finish within {MAX_TURNS} turns."
    )
