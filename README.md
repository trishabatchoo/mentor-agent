# Mentor Agent

An exploratory agentic workflow for preparing for mentoring sessions and completing post-session admin.

This project is an independent implementation of a mentoring workflow that previously ran inside Claude Desktop. The goal is to explore agent orchestration, tool use, grounding, observability, reliability, and evaluation using the Anthropic Messages API directly.

## Current Status

This is an early working baseline.

The agent currently:

* Supports pre-session, post-session, and new-student modes in `run_agent()`; the command-line entry points (`run.py`, `main.py`) run post-session only
* Loads shared and mode-specific instructions
* Accepts runtime information such as a student name, transcript, and session date
* Uses an agent loop that can handle multiple tool calls
* Retrieves student context and latest-session notes from local mock data (example profile) or, read-only, from Notion (local profile)
* Retrieves a learning-path reference document
* Uses retrieved context to produce a grounded response
* Stops after a configurable maximum number of turns

The current implementation intentionally retrieves an entire learning-path document. More targeted retrieval may be explored in a later version.

## How It Works

A typical post-session run follows this sequence:

1. The user supplies a student name, transcript, and session date.
2. The model calls `get_student_context`.
3. The tool returns the student’s learning path and current project.
4. The model calls `get_path_context`.
5. The tool returns the complete reference guide for that learning path.
6. The model uses the transcript, student record, and path guide to generate structured post-session notes.

The second tool call depends on information returned by the first, allowing the model to determine which reference document it needs.

## Project Structure

```text
mentor-agent/
├── run.py            # Profile-based launcher (example / local)
├── agent.py          # Agent loop and Anthropic API calls
├── observability.py  # Local structured run tracing (JSONL)
├── main.py           # Entry point (reads its config from the environment)
├── prompts.py        # Prompt loading and request construction
├── tools.py          # Tool definitions and backend dispatch
├── notion_client.py  # Read-only Notion HTTP client (auth, pagination)
├── notion_repository.py # Notion page resolution, block traversal, sessions
├── data/             # Local student data
├── fixtures/         # Sanitized development inputs
├── prompts/          # Shared and mode-specific instructions
├── references/       # Learning-path reference documents
└── tests/            # Automated tests
```

Some data, prompt, and reference files are intentionally excluded from version control because they may contain private or proprietary information.

## Tools

### `get_student_context`

Retrieves a student’s current context, including:

* Learning path
* Current project
* Session length

### `get_latest_session_notes`

Retrieves the notes from the student's most recent previous session, with the
session's date/time when available, or `latest_session: null` when there is
none. Session transcripts are never retrieved.

### `get_path_context`

Retrieves the complete reference document for a specified learning path.

In this baseline version, the tool layer handles deterministic document selection while the model identifies and interprets the section relevant to the student’s current project.

## Student Context Backends

The agent uses the backend selected by `STUDENT_CONTEXT_BACKEND`. `run.py`
sets this explicitly for each profile; it is never inferred from available
credentials, and Notion failures do not fall back to local JSON.

| Profile | Backend | Source |
| --- | --- | --- |
| `example` | JSON | Synthetic records in `data/students.example.json` |
| `local` | Notion | Student pages under `NOTION_PARENT_PAGE_ID` |

### Notion backend (read-only)

Configuration comes from the environment, never from code:

Required environment variables:

- `NOTION_API_KEY`
- `NOTION_PARENT_PAGE_ID`

Optional configuration:

- `NOTION_VERSION` — defaults to `2026-03-11`
- `NOTION_MAX_TRAVERSAL_DEPTH` — defaults to `6`

Student pages are resolved by an exact normalized title match beneath the
configured parent page. Missing or ambiguous matches fail explicitly rather
than using fuzzy results.

Behavior:

- extracts the student’s path and current project;
- identifies the latest session from timestamped child pages;
- reads the `Notes` branch when present, or the session page otherwise;
- skips transcript branches; and
- returns privacy-safe errors without names, Notion identifiers, URLs, or
  page content.

Notion access is read-only. Page and block IDs are excluded from tool results
and logs. Notion-backed runs currently record `student_id: null`.

## Agent Loop

The project uses the Anthropic Messages API directly rather than an agent framework.

On each turn, the loop:

1. Sends the conversation history, instructions, and tool definitions to the model.
2. Inspects the response’s stop reason.
3. Returns the final response when the model finishes.
4. Executes requested tools when the model returns `tool_use`.
5. Appends the assistant tool request and corresponding tool result to the message history.
6. Continues until the model completes the task or reaches the maximum-turn limit.

## Observability

Every valid run writes structured events to
`logs/agent-runs.jsonl`, alongside human-readable terminal logs. The file is
gitignored and uses JSONL: one JSON event per line.

Events from the same run share a `run_id`, allowing the full execution path
to be reconstructed across model calls, tool use, and Notion retrieval.

| Event | Records |
| --- | --- |
| `run_started` | Pseudonymous student ID and transcript fingerprint metadata |
| `model_response` | Turn, latency, stop reason, and token usage |
| `tool_execution` | Tool name, tool-use ID, latency, status, and error type |
| `notion_request` | Notion operation, request counts, latency, and status |
| `notion_retrieval` | Retrieval type, cache use, blocks examined, and found/not-found flags |
| `run_completed` | Total duration, turns, tokens, and tool calls |
| `run_failed` | Run totals, error type, and failure category |

### Privacy

Traces contain operational metadata, not the content being processed. They
exclude student names, transcript text and paths, prompts, tool inputs and
results, model responses, Notion IDs and URLs, exception messages, and API
keys.

Each event has a fixed allowlist of permitted fields. A pseudonymous
`student_id` supports correlation, while the transcript character count and
SHA-256 fingerprint help distinguish inputs without storing transcript text.
The fingerprint is an identifier, not a security mechanism.

Tracing is fail-open: if an event cannot be written, the application emits a
warning and the agent run continues.

### Reading trace values

For optional fields:

- `true` or `false` means the value was evaluated;
- `null` means it was unavailable, not applicable, or not reached; and
- values such as `notes_source: "none"` mean the lookup completed and found
  no matching data.

For example, `path_found` is `null` during a latest-session retrieval because
that operation does not inspect the student’s path.

### Observability is not evaluation

Observability explains how a run behaved—its latency, token use, tool calls,
retrievals, and failures. It does not determine whether the generated output
was correct, grounded, or useful. Output-quality evaluation is planned as a
separate layer.



### Notion retrieval events

Notion-backed runs add two event types:
- `notion_request` records an interaction with the Notion API, including its
  latency, status category, pagination, and result counts.
- `notion_retrieval` records the application-level lookup and interpretation,
  such as resolving a student page or extracting the latest session notes.

These events share the run’s `run_id` and the triggering tool call’s `turn`
and `tool_use_id`. This makes it possible to follow one operation across the
system’s layers:

```text
Notion request → repository retrieval → tool execution → agent run
```

Notion events are written while the tool runs, so they appear just before the
`tool_execution` event of the same call:

```bash
jq -c 'select(.run_id == "YOUR_RUN_ID" and .tool_use_id == "YOUR_TOOL_USE_ID")' \
  logs/agent-runs.jsonl
```

Notion events contain metadata only and exclude identifying information,
retrieved content, request details, and exception messages. Tracing is
provided through an optional observer, keeping the Notion modules independent
of the tracer.

Student-page resolutions are cached for the lifetime of each CLI process.
A future long-lived service would require a bounded or request-scoped cache.

The examples below require [`jq`](https://jqlang.org/).

```bash
# Summarize recent runs
jq -c 'select(.event == "run_completed" or .event == "run_failed")
       | {run_id, status, turns, total_input_tokens, total_output_tokens, failure_reason}' \
  logs/agent-runs.jsonl

# Reconstruct one run's events in order
jq 'select(.run_id == "YOUR_RUN_ID")' logs/agent-runs.jsonl
```

## Setup

Requires Python 3.10 or later (developed on 3.14).

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Set the Anthropic API key in your terminal:

```bash
export ANTHROPIC_API_KEY="your-api-key"
```

Optionally specify a model:

```bash
export ANTHROPIC_MODEL="your-model-name"
```

For the `local` profile only, also set the Notion variables (the `example`
profile doesn't need them):

```bash
export NOTION_API_KEY="your-notion-integration-token"
export NOTION_PARENT_PAGE_ID="your-students-parent-page-id"
```

Optionally override the Notion API version (default `2026-03-11`) or the
block traversal depth limit (default `6`):

```bash
export NOTION_VERSION="2026-03-11"
export NOTION_MAX_TRAVERSAL_DEPTH="6"
```

The Notion integration must be shared with the parent page so it can read the
student pages beneath it. Don't set `STUDENT_CONTEXT_BACKEND` yourself:
`run.py` sets it for each profile.


## Running the agent

Use `run.py` to select a configuration profile:

```bash
python3 run.py example
python3 run.py local \
  --student Casey \
  --transcript fixtures/private/session-2026-09-08.txt \
  --session-datetime "September 8, 2026 11:00 AM ET"
```

- `example` uses synthetic files in the repository and provides some
  default inputs.
- `local` uses private prompts, references, transcripts, and read-only Notion
  context. It requires explicit student, transcript, session time,
  `NOTION_API_KEY`, and `NOTION_PARENT_PAGE_ID`.


Transcript paths may be absolute or relative to the project root. Inputs can
also be supplied through `MENTOR_STUDENT_NAME`, `TRANSCRIPT_PATH`, and
`SESSION_DATETIME`; command-line arguments take precedence.

`run.py` validates required files and configuration before making API calls.
Credentials and model settings are always read from the shell environment.

You can still run `python3 main.py` directly; it uses the same environment
variables (`PROMPTS_DIR`, `STUDENTS_DATA_PATH`, `REFERENCE_ROOT`,
`STUDENT_CONTEXT_BACKEND`, `TRANSCRIPT_PATH`, `MENTOR_STUDENT_NAME`,
`SESSION_DATETIME`) and falls back to the public example defaults when
they're unset.

Running the program may make paid Anthropic API requests.

### Terminal output

Logs default to `INFO`. Use `LOG_LEVEL=DEBUG` for additional response and
tool metadata. Third-party HTTP loggers remain at `INFO` because their debug
output may include complete API request bodies.

The terminal is not held to the same rules as the trace file:

* The final model response is printed to stdout. That is the program's
  output, and it can contain student information.
* An unhandled error prints a Python traceback, whose exception message may
  include a file path or an API error message.
* `run.py` prints the paths of missing files, so name private transcripts by
  session date (as above), not by student name.

## Running the Tests

The test suite runs offline. It makes no Anthropic or Notion calls and needs
no API keys:

```bash
python3 -m unittest discover -s tests
```

## Troubleshooting

* **`KeyError: 'ANTHROPIC_API_KEY'` on startup:** export `ANTHROPIC_API_KEY`
  (see [Setup](#setup)). `agent.py` reads it at import time.
* **`profile 'local' is missing required files`:** create the gitignored
  `prompts/local/` and `references/local/` files and the transcript (see
  [Local files are gitignored](#local-files-are-gitignored)).
* **`profile 'local' requires these environment variables`:** export
  `NOTION_API_KEY` and `NOTION_PARENT_PAGE_ID`.
* **`StudentNotFoundError` / `AmbiguousStudentMatchError`:** exactly one page
  directly under `NOTION_PARENT_PAGE_ID` must have a title matching the
  student name (case and spacing are ignored), and the integration must be
  shared with that parent page.
* **`Run trace could not be written`:** `logs/` isn't writable. The run still
  completes, without a trace.
* **`Claude reached max_tokens` / `Agent did not finish within 5 turns`:** the
  run failed and is recorded as `run_failed` with `failure_reason`
  `max_tokens` or `max_turns`.

### Private local files

Private prompts, references, transcripts, and legacy local student data are
gitignored and must be created locally when needed. This keeps sensitive or
proprietary material out of the public repository.

JSON student records may include a stable, non-identifying `student_id` for
trace correlation. It is not sent to the model; missing IDs are recorded as
`null`.

## Privacy

This project was developed around a real mentoring workflow, so privacy boundaries are part of the system design.

Any committed fixtures are sanitized, fictional, or composited examples.

## Current Limitations

* Notion access is read-only; there are no retries, and Notion-backed runs record `student_id: null`.
* Path retrieval returns the complete reference guide, which increases token usage.
* Outputs are generated as drafts and are not written back to Notion.
* Tool errors and retry behavior are still being developed.
* Evaluation coverage is currently limited.


## Why This Project Exists

The project is both a practical mentoring assistant and a learning exercise in building agent systems without relying immediately on a high-level orchestration framework.

The focus is not only whether the model can produce a useful answer, but whether the surrounding system makes its behavior understandable, constrained, testable, and reliable.