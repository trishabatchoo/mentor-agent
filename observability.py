"""Minimal local run tracing for the mentor agent.

Each agent run gets one RunTracer, which appends structured events as
newline-delimited JSON to logs/agent-runs.jsonl (gitignored). This is in
addition to the human-readable terminal logs, not a replacement.

Privacy: only operational metadata is recorded. Every event has a fixed
allowlist of field names, and every value must be a plain scalar, so
prompt text, transcripts, student data, tool inputs/results, model output,
exception messages, and secrets have no field to land in.

Tracing never interferes with a run: a failure to write the trace file is
reported once as a terminal warning and otherwise ignored.
"""

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent.resolve()
DEFAULT_TRACE_PATH = PROJECT_DIR / "logs" / "agent-runs.jsonl"

# Fixed values for run_failed's failure_reason field.
FAILURE_REASONS = frozenset(
    {"api_error", "tool_error", "max_tokens", "max_turns", "unexpected_error"}
)

# Fields present on every event.
_COMMON_FIELDS = ("timestamp", "run_id", "event", "mode", "model")

_RUN_TOTAL_FIELDS = (
    "status",
    "total_duration_ms",
    "turns",
    "total_input_tokens",
    "total_output_tokens",
    "tool_calls",
)

# Allowlist of event-specific fields. Anything not listed is dropped.
EVENT_FIELDS = {
    "run_started": (
        "student_id",
        "transcript_char_count",
        "transcript_sha256",
    ),
    "model_response": (
        "turn",
        "duration_ms",
        "stop_reason",
        "input_tokens",
        "output_tokens",
    ),
    "tool_execution": (
        "turn",
        "tool_name",
        "tool_use_id",
        "duration_ms",
        "status",
        "error_type",
    ),
    "run_completed": _RUN_TOTAL_FIELDS,
    "run_failed": _RUN_TOTAL_FIELDS + ("error_type", "failure_reason"),
}

_SCALAR_TYPES = (str, int, float, bool, type(None))


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


def text_sha256(text: str) -> str:
    """Hex SHA-256 of `text` encoded as UTF-8.

    A fingerprint for telling whether two runs used the same input, not a
    security mechanism. It hashes the string as given (for transcripts,
    the newline-normalized text from read_text() that the model receives),
    so it can differ from `sha256sum` of a file with CRLF line endings.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RunTracer:
    """Records the structured events for a single agent run."""

    def __init__(self, mode: str, model: str, trace_path: Path | None = None):
        self.run_id = uuid.uuid4().hex
        self.mode = mode
        self.model = model
        self.trace_path = Path(trace_path or DEFAULT_TRACE_PATH)
        self.turns = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.tool_calls = 0
        self.failure_reason: str | None = None
        self._start = time.perf_counter()
        self._write_failed = False

    # -- events -------------------------------------------------------

    def run_started(
        self,
        student_id: str | None = None,
        transcript_char_count: int | None = None,
        transcript_sha256: str | None = None,
    ) -> None:
        self._start = time.perf_counter()
        self._emit(
            "run_started",
            student_id=student_id,
            transcript_char_count=transcript_char_count,
            transcript_sha256=transcript_sha256,
        )

    def model_response(
        self,
        turn: int,
        duration_ms: float,
        stop_reason: str | None,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        self.turns = max(self.turns, turn)
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self._emit(
            "model_response",
            turn=turn,
            duration_ms=duration_ms,
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def tool_execution(
        self,
        turn: int,
        tool_name: str,
        tool_use_id: str,
        duration_ms: float,
        error_type: str | None = None,
    ) -> None:
        self.tool_calls += 1
        self._emit(
            "tool_execution",
            turn=turn,
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            duration_ms=duration_ms,
            status="error" if error_type else "success",
            error_type=error_type,
        )

    def mark_failure(self, reason: str) -> None:
        """Record why the run is about to fail, before the exception is
        raised. The first reason marked wins."""
        if reason not in FAILURE_REASONS:
            raise ValueError(f"Unknown failure_reason: {reason!r}")
        if self.failure_reason is None:
            self.failure_reason = reason

    def run_completed(self) -> None:
        self._emit("run_completed", **self._totals("completed"))

    def run_failed(self, error_type: str) -> None:
        self._emit(
            "run_failed",
            **self._totals("failed"),
            error_type=error_type,
            failure_reason=self.failure_reason or "unexpected_error",
        )

    # -- internals ----------------------------------------------------

    def _totals(self, status: str) -> dict:
        return {
            "status": status,
            "total_duration_ms": _elapsed_ms(self._start),
            "turns": self.turns,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "tool_calls": self.tool_calls,
        }

    def build_event(self, event: str, **fields) -> dict:
        """Build an event dict, keeping only allowlisted scalar fields."""
        allowed = EVENT_FIELDS[event]
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
            "mode": self.mode,
            "model": self.model,
        }
        for key in allowed:
            value = fields.get(key)
            if isinstance(value, _SCALAR_TYPES):
                record[key] = value
            else:
                record[key] = None
        return record

    def _emit(self, event: str, **fields) -> None:
        try:
            line = json.dumps(self.build_event(event, **fields))
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            with self.trace_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except (OSError, TypeError, ValueError) as exc:
            if not self._write_failed:
                self._write_failed = True
                logger.warning(
                    "Run trace could not be written (%s); continuing.",
                    type(exc).__name__,
                )
