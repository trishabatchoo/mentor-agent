# Mentor Agent

An exploratory agentic workflow for preparing for mentoring sessions and processing post-session transcripts.

This project is an independent implementation of a mentoring workflow that previously ran inside Claude Desktop. The goal is to explore agent orchestration, tool use, grounding, observability, reliability, and evaluation using the Anthropic Messages API directly.

## Current Status

This is an early working baseline.

The agent currently:

* Supports pre-session and post-session modes
* Loads shared and mode-specific instructions
* Accepts runtime information such as a student name, transcript, and session date
* Uses an agent loop that can handle multiple tool calls
* Retrieves mock student context from local data
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
├── main.py           # Entry point (reads its config from the environment)
├── prompts.py        # Prompt loading and request construction
├── tools.py          # Tool definitions and implementations
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

### `get_path_context`

Retrieves the complete reference document for a specified learning path.

In this baseline version, the tool layer handles deterministic document selection while the model identifies and interprets the section relevant to the student’s current project.

## Agent Loop

The project uses the Anthropic Messages API directly rather than an agent framework.

On each turn, the loop:

1. Sends the conversation history, instructions, and tool definitions to the model.
2. Inspects the response’s stop reason.
3. Returns the final response when the model finishes.
4. Executes requested tools when the model returns `tool_use`.
5. Appends the assistant tool request and corresponding tool result to the message history.
6. Continues until the model completes the task or reaches the maximum-turn limit.

## Setup

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
  `prompts/local/`, `data/students.local.json`, `references/local/`, and a
  transcript of your choosing (conventionally under `fixtures/private/`).
  There are no defaults for this profile — the student, transcript, and
  session date/time must all be supplied explicitly:

  ```bash
  python3 run.py local \
    --student Amya \
    --transcript fixtures/private/amya-september-8.txt \
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
transcript — before making any API call, and it never modifies
`ANTHROPIC_API_KEY` or `ANTHROPIC_MODEL` — those always come from your
shell's environment.

You can still run `python3 main.py` directly; it uses the same environment
variables (`PROMPTS_DIR`, `STUDENTS_DATA_PATH`, `REFERENCE_ROOT`,
`TRANSCRIPT_PATH`, `MENTOR_STUDENT_NAME`, `SESSION_DATETIME`) and falls back
to the public example defaults when they're unset.

Running the program may make paid Anthropic API requests.

### Local files are gitignored

`prompts/local/`, `references/local/`, `data/students.local.json`, and
`fixtures/private/` are excluded from version control (see `.gitignore`).
They don't exist in a fresh checkout — create them yourself before running
`python3 run.py local`. This keeps any real student data, transcripts, or
proprietary reference material out of the repository while still letting the
same codebase run against them locally.

## Privacy

This project was developed around a real mentoring workflow, so privacy boundaries are part of the system design.

Any committed fixtures are sanitized, fictional, or composited examples.

## Current Limitations

* Student context is retrieved from local mock data rather than Notion or other local database.
* Path retrieval returns the complete reference guide, which increases token usage.
* Outputs are generated as drafts and are not written back to Notion.
* Tool errors and retry behavior are still being developed.
* Evaluation coverage is currently limited.


## Why This Project Exists

The project is both a practical mentoring assistant and a learning exercise in building agent systems without relying immediately on a high-level orchestration framework.

The focus is not only whether the model can produce a useful answer, but whether the surrounding system makes its behavior understandable, constrained, testable, and reliable.