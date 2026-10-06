# Mentor Agent

An exploratory agentic workflow for preparing for mentoring sessions and processing post-session transcripts.

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

`get_student_context` and `get_latest_session_notes` read from a backend chosen
explicitly by `STUDENT_CONTEXT_BACKEND`, which `run.py` sets per profile. The
backend is never inferred from which credentials happen to be set, and a
failing Notion backend never falls back to the JSON file.

| Profile | Backend | Source |
|---|---|---|
| `example` | `json` | `data/students.example.json` (`get_latest_session_notes` returns the record's `previous_session`) |
| `local` | `notion` | Pages under `NOTION_PARENT_PAGE_ID`, read-only |

### Notion backend (read-only)

Configuration comes from the environment, never from code:

| Variable | Required | Purpose |
|---|---|---|
| `NOTION_API_KEY` | yes | Integration token |
| `NOTION_PARENT_PAGE_ID` | yes | Page whose child pages are the student pages |
| `NOTION_VERSION` | no | API version header, default `2026-03-11` |
| `NOTION_MAX_TRAVERSAL_DEPTH` | no | Block nesting limit, default `6` |

Behavior:

* **Student page:** searches Notion by name, then requires exactly one page
  whose title matches case-insensitively (whitespace-collapsed) and whose
  parent is `NOTION_PARENT_PAGE_ID`. No match or multiple matches raise
  `StudentNotFoundError` / `AmbiguousStudentMatchError`; fuzzy results are
  never used.
* **Student context:** the page's blocks rendered as text in document order
  (headings, paragraphs, list items, to-dos, child-page titles, and nested
  blocks), plus `path` and `current_project` parsed from lines such as
  `Path: ...` and `Current project: ...` (`null` when absent). Child pages
  (sessions) are not entered.
* **Latest session:** the student page's child pages whose titles are
  timezone-aware ISO timestamps (e.g. `2026-09-29T11:00:00.000-04:00`); the
  latest by parsed datetime wins. Other titles, date-only and timezone-less
  timestamps are ignored. No sessions yields `latest_session: null`.
* **Session notes:** if the session contains a block labelled exactly `Notes`
  (e.g. a tab), only its contents are returned; otherwise the whole page is.
  Any block labelled `Transcript` is skipped without fetching its contents.
  Nesting beyond the depth limit raises `TraversalDepthExceededError` rather
  than truncating.
* **Errors:** `NotionConfigError`, `NotionRequestError` (non-2xx or transport
  failure; no retries), `MalformedNotionResponseError`, and the errors above.
  Messages are fixed text with no names, ids, URLs, or content.
* **Privacy:** Notion page and block ids are not included in tool results.
  Terminal logs record only operation names, status categories, durations,
  counts, and match/notes-found flags. The `httpx2` per-request log line is
  suppressed for Notion URLs because those URLs contain block ids.

The Notion backend has no `student_id` yet, so its run traces record
`student_id: null`.

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

In addition to the human-readable terminal logs, every valid agent run appends
structured events to `logs/agent-runs.jsonl` (gitignored). The file uses JSONL
(newline-delimited JSON): each line is one complete JSON object describing a
single event. Calls rejected by argument validation are not traced. All events
from a run share a `run_id`, plus `timestamp`, `mode`, and `model`.

| Event | Records |
|---|---|
| `run_started` | `student_id`, `transcript_char_count`, `transcript_sha256` |
| `model_response` | turn, call duration, `stop_reason`, input/output tokens |
| `tool_execution` | turn, tool name, `tool_use_id`, duration, `success`/`error`, error type |
| `run_completed` | total duration, turns, token totals, tool calls |
| `run_failed` | total duration, turns, token totals, tool calls, error type, `failure_reason` (`api_error`, `tool_error`, `max_tokens`, `max_turns`, `unexpected_error`) |

Traces contain operational metadata only. Student names, transcript text or
file paths, prompts, tool inputs and results, model responses, exception
messages, and API keys are never recorded. Each event has a fixed allowlist of
fields, so other data is dropped rather than written. `student_id` comes from
the student record, and `transcript_sha256` is a fingerprint for telling
whether two runs used the same transcript, not a security mechanism. Transcript
fields are `null` outside `post_session` mode.

Tracing never changes a run's outcome: if the log file can't be written, a
single warning is emitted and the run continues.

### Observability is not evaluation

Traces describe what a run did: its latency, token usage, tool calls, Notion
requests, and how it failed. They say nothing about whether the output was
correct, grounded, or useful. Judging output quality is evaluation, which is
separate work and not part of this milestone.

### true, false, and null

`true` and `false` are recorded only when the value was actually determined.
Every allowlisted field is always present, and `null` means one of:

* **Not applicable to this event:** for example, transcript fields outside
  `post_session`, `path_found`/`project_found` on a `latest_session_notes`
  retrieval, or `session_found` on a `student_context` retrieval.
* **Not reached because of an earlier failure:** for example, `cache_hit`
  when the student name was blank, `results_returned` on a failed request, or
  `status_category` when no request was sent.
* **Not available:** `student_id` when the record has none (always, for the
  Notion backend), or `stop_reason` when the API returned none.
* **Rejected by the allowlist:** a value that wasn't a plain scalar, or a
  Notion field value outside its fixed set, is written as `null` rather than
  dropped.

`notes_source: "none"` is different from `null`: it means the lookup ran and
the student had no session pages. On those events `notes_char_count` is
`null`; a session whose notes are empty records `0`.

### Notion retrieval events

With the Notion backend, two more event types are written to the same file,
with the same `run_id`, plus the `turn` and `tool_use_id` of the tool call
that triggered them. The JSON backend writes neither.

| Event | Layer | Records |
|---|---|---|
| `notion_request` | One logical Notion API operation (`search_pages` or `list_block_children`), however many result pages it took | duration, `success`/`error`, `status_category` (`2xx`, `4xx`, `5xx`, `transport_error`, `malformed_response`, or `null`), request and page counts, results returned, error type |
| `notion_retrieval` | One repository retrieval (`student_context` or `latest_session_notes`): page lookup plus interpretation | duration, status (`success`, `not_found`, `ambiguous`, `no_session`, `error`), requests and pages fetched, blocks examined, deepest nesting level listed, `cache_hit`, whether a path / current project / session was found, `notes_source` (`notes_branch`, `page`, `none`), notes character count, error type |

Together with the existing events, each layer reports its own boundary:
`notion_request` is the interaction with the Notion API, `notion_retrieval`
is the repository's lookup and interpretation, `tool_execution` is the agent
tool boundary, and `run_completed`/`run_failed` is the whole run. A failure
is reported once per layer it crosses. For example, a Notion 5xx yields
`notion_request:error`, `notion_retrieval:error`, `tool_execution:error`, and
`run_failed`. A failure raised by the repository itself (student not found,
ambiguous match, traversal depth) has no `notion_request` error, because every
request it made succeeded. A pagination-limit failure keeps
`status_category: "2xx"` with `error_type: "PaginationLimitExceededError"`:
the API answered successfully, and the application's safety limit stopped it.

Notion events are written while the tool runs, so they appear just before the
`tool_execution` event of the same call:

```bash
jq -c 'select(.run_id == "YOUR_RUN_ID" and .tool_use_id == "YOUR_TOOL_USE_ID")' \
  logs/agent-runs.jsonl
```

These events never contain student names, search queries, page titles,
retrieved text, session datetimes, Notion ids, cursors, URLs, file paths, or
exception messages. The Notion modules don't depend on the tracer. The agent
passes each tool call a fresh observer, and the Notion layer reports
metadata-only events to it without knowing the run, turn, or tool call.
Standalone repository calls without an observer record nothing.

Resolved student pages are cached for the lifetime of the process, so a
second lookup of the same student shows `cache_hit: true` and no
`search_pages` request. This suits the CLI, where each invocation is a fresh
process. A long-lived service would need per-run scoping or invalidation of
that cache.

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

## Running the Agent

Run the agent through `run.py`, which selects a configuration **profile** and
sets its environment variables before starting the agent loop:

```bash
python3 run.py example
python3 run.py local
```

* **`example`** — the public, synthetic configuration committed to this repo:
  `prompts/example/`, `data/students.example.json`, `references/example/`,
  and `fixtures/example/post_session_transcript.txt`. Defaults to student
  `Jordan`, that transcript, and session date/time
  `September 8, 2026 11:00 AM ET`.
* **`local`** — a private, gitignored configuration for real use:
  `prompts/local/`, `references/local/`, student context from Notion (see
  [Notion backend](#notion-backend-read-only)), and a transcript of your
  choosing (conventionally under `fixtures/private/`). There are no defaults
  for this profile — the student, transcript, and session date/time must all
  be supplied explicitly, and `NOTION_API_KEY` and `NOTION_PARENT_PAGE_ID`
  must be exported in your shell:

  ```bash
  python3 run.py local \
    --student Casey \
    --transcript fixtures/private/session-2026-09-08.txt \
    --session-datetime "September 8, 2026 11:00 AM ET"
  ```

  `--transcript` may be a path relative to the project root (as above) or
  an absolute path; relative paths are resolved against the project
  directory regardless of your current working directory.

  Each of `--student`, `--transcript`, and `--session-datetime` can
  instead be supplied via the environment (`MENTOR_STUDENT_NAME`,
  `TRANSCRIPT_PATH`, `SESSION_DATETIME`); the CLI flag always takes
  precedence over the environment variable when both are set.

`run.py` resolves and validates a profile's required files — including the
transcript — and, for the Notion backend, checks that the required `NOTION_*`
variables are set (it prints only their names) before making any API call. It
never modifies `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, or `NOTION_*` — those
always come from your shell's environment.

You can still run `python3 main.py` directly; it uses the same environment
variables (`PROMPTS_DIR`, `STUDENTS_DATA_PATH`, `REFERENCE_ROOT`,
`STUDENT_CONTEXT_BACKEND`, `TRANSCRIPT_PATH`, `MENTOR_STUDENT_NAME`,
`SESSION_DATETIME`) and falls back to the public example defaults when
they're unset.

Running the program may make paid Anthropic API requests.

### Terminal output

Terminal logs default to `INFO`. Set `LOG_LEVEL=DEBUG` for more detail (tool-use
ids, tool input field names, response metadata). Third-party HTTP loggers
(`anthropic`, `httpx2`, `httpcore2`) are never set below `INFO`, because at
`DEBUG` the Anthropic SDK logs full request bodies.

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

### Local files are gitignored

`prompts/local/`, `references/local/`, `data/students.local.json` (no longer
used by the `local` profile, which reads Notion), and `fixtures/private/` are
excluded from version control (see `.gitignore`).
They don't exist in a fresh checkout — create them yourself before running
`python3 run.py local`. This keeps any real student data, transcripts, or
proprietary reference material out of the repository while still letting the
same codebase run against them locally.

For the JSON backend, each student record should include a stable, non-identifying `student_id`
(e.g. `"student_id": "student_007"`). It is used only to tag run traces in
`logs/agent-runs.jsonl` and is removed from the context returned to the
model. Records without one still run; their traces record `student_id: null`.

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