# Mentor Agent

A privacy-conscious agentic workflow for turning mentoring-session transcripts
into structured post-session documentation grounded in student and project
context.

## Why this project exists

This workflow originally ran inside Claude Desktop using an MCP connection to
Notion. That version was useful, but the host application handled most of the
orchestration invisibly.

I rebuilt it using the Anthropic Messages API to explore the full execution
path: model turns, tool calls, context retrieval, failure handling, testing,
and observability—without relying on a high-level agent framework.

## Current milestone

`v0.1.0-observability` implements the post-session workflow as a local Python
application. It can:

- retrieve student context from synthetic JSON or a private Notion workspace;
- retrieve the student’s latest-session notes;
- select the appropriate learning-path reference;
- generate grounded post-session documentation for human review; and
- record privacy-conscious traces across model, tool, and Notion operations.

The prompt library also contains pre-session and new-student instructions, but
the current command-line workflow runs post-session requests only.

## How it works

A typical run follows this sequence:

1. The user supplies a student name, transcript, and session date.
2. The model requests student context and previous-session notes.
3. The application retrieves that context from JSON or Notion.
4. The model requests the reference guide for the student’s learning path.
5. Tool results are appended to the conversation and returned to the model.
6. The loop continues until the model produces the final draft or reaches a
   configured limit.

The second retrieval depends on information returned by the first, allowing
the model to determine which reference document it needs.

## Project structure

```text
mentor-agent/
├── run.py                # Profile-based launcher
├── main.py               # Application entry point
├── agent.py              # Agent loop and Anthropic API calls
├── tools.py              # Tool definitions and backend dispatch
├── prompts.py            # Instruction loading
├── notion_client.py      # Read-only Notion HTTP client
├── notion_repository.py  # Student and session retrieval logic
├── observability.py      # Structured JSONL tracing
├── data/                 # Synthetic student records
├── fixtures/             # Sanitized example inputs
├── prompts/              # Shared and mode-specific instructions
├── references/           # Learning-path references
└── tests/                # Offline automated tests
```

## Tools and context backends

The model can request three tools:

- `get_student_context` retrieves a student’s learning path and current
  project.
- `get_latest_session_notes` retrieves the most recent session notes without
  retrieving its transcript.
- `get_path_context` retrieves the reference document for a learning path.

`run.py` selects the student-context backend explicitly. Credentials never
determine the backend, and a failed Notion lookup never falls back silently to
JSON.

| Profile | Backend | Source |
| --- | --- | --- |
| `example` | JSON | Synthetic records committed to the repository |
| `local` | Notion | Student pages under `NOTION_PARENT_PAGE_ID` |

### Read-only Notion retrieval

The Notion backend resolves a student through an exact normalized page-title
match beneath the configured parent page. It extracts the learning path and
current project, identifies the latest timestamped session, reads its `Notes`
branch when present, and skips transcript branches.

Missing or ambiguous matches fail explicitly rather than using fuzzy results.
Notion page and block IDs are excluded from tool results and logs.

## Observability

Every valid run appends structured events to `logs/agent-runs.jsonl`, alongside
human-readable terminal logs. The JSONL file is gitignored, and each line is
one event.

Events from the same run share a `run_id`. Tool-related events also carry the
relevant `turn` and `tool_use_id`, making it possible to reconstruct execution
across system boundaries.

| Event | Records |
| --- | --- |
| `run_started` | Pseudonymous student ID and transcript fingerprint metadata |
| `model_response` | Turn, latency, stop reason, and token usage |
| `tool_execution` | Tool, latency, status, and error type |
| `notion_request` | Notion operation, request counts, latency, and status |
| `notion_retrieval` | Retrieval type, cache use, traversal counts, and found flags |
| `run_completed` | Total duration, turns, tokens, and tool calls |
| `run_failed` | Run totals, error type, and failure category |

The Notion events distinguish transport behavior from application behavior:

```text
Notion request → repository retrieval → tool execution → agent run
```

This makes it possible to tell, for example, whether Notion failed to answer
or whether the application received a valid response but could not resolve a
student.

### Privacy-conscious tracing

Traces record operational metadata rather than the content being processed.
They exclude names, transcript text and paths, prompts, tool inputs and
results, model responses, Notion identifiers and URLs, exception messages,
and credentials.

A pseudonymous `student_id` supports correlation. Transcript length and a
SHA-256 fingerprint distinguish inputs without storing transcript text; the
fingerprint is an identifier, not a security mechanism.

Tracing is fail-open: if an event cannot be written, the application emits a
warning and the agent run continues.

For optional fields, `true` or `false` means the value was evaluated, while
`null` means it was unavailable, not applicable, or not reached. For example,
`path_found` is `null` during a latest-session retrieval because that operation
does not inspect the student’s path.

### Observability is not evaluation

These traces describe how a run behaved—its latency, token use, tool calls,
retrievals, and failures. They do not determine whether the output was correct,
grounded, or useful. Output-quality evaluation is planned as a separate layer.

### Inspecting traces

The following examples require [`jq`](https://jqlang.org/):

```bash
# Reconstruct one run
jq 'select(.run_id == "YOUR_RUN_ID")' logs/agent-runs.jsonl

# Inspect one tool call
jq 'select(
  .run_id == "YOUR_RUN_ID"
  and .tool_use_id == "YOUR_TOOL_USE_ID"
)' logs/agent-runs.jsonl
```

Student-page resolutions are cached for the lifetime of each CLI process. A
future long-lived service would require a bounded or request-scoped cache.

## Setup

Requires Python 3.10 or later.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-api-key"
```

Optionally specify a model:

```bash
export ANTHROPIC_MODEL="your-model-name"
```

The local Notion profile also requires:

```bash
export NOTION_API_KEY="your-notion-integration-token"
export NOTION_PARENT_PAGE_ID="your-students-parent-page-id"
```

The integration must have access to the configured parent page.

## Running the agent

Run the synthetic public example:

```bash
python3 run.py example
```

Run the private local profile:

```bash
python3 run.py local \
  --student Casey \
  --transcript fixtures/private/session-2026-09-08.txt \
  --session-datetime "September 8, 2026 11:00 AM ET"
```

The local profile uses private prompts, references, transcripts, and read-only
Notion context. Transcript paths may be absolute or relative to the project
root.

Inputs can alternatively come from `MENTOR_STUDENT_NAME`, `TRANSCRIPT_PATH`,
and `SESSION_DATETIME`; command-line arguments take precedence. `run.py`
validates required files and configuration before making API calls.

Running the agent may incur Anthropic API charges.

### Terminal output

Logs default to `INFO`. Use `LOG_LEVEL=DEBUG` for additional response and tool
metadata. Third-party HTTP loggers remain at `INFO` because their debug output
may include complete API request bodies.

Unlike structured traces, terminal output may contain private information:
the final response is printed to stdout, and errors may display file paths or
API messages. Name private transcript files by date rather than by student.

## Tests

The test suite runs offline without Anthropic or Notion credentials:

```bash
python3 -m unittest discover -s tests
```

## Private local files

Private prompts, references, transcripts, and legacy local student data are
gitignored and must be created locally when needed. This keeps sensitive and
proprietary material out of the public repository.

JSON student records may include a stable, non-identifying `student_id` for
trace correlation. It is not sent to the model; missing IDs are recorded as
`null`.

## Current limitations

- The CLI currently supports post-session runs only.
- Notion access is read-only and requests are not retried automatically.
- Notion-backed runs currently record `student_id: null`.
- Path retrieval returns the complete reference guide, increasing token use.
- Outputs are drafts for human review and are not written back to Notion.
- Output-quality evaluation has not yet been implemented.
- Traces are stored locally rather than in a persistent observability service.