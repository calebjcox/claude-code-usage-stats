# CLAUDE.md — Claude Code Usage Stats

This file is for Claude Code sessions picking up future work. Read this first.

## What this project is

A single-file (`dashboard.py`) local web dashboard that reads Claude Code's own JSONL message logs and visualizes token usage, project activity, and code-editing patterns. No dependencies beyond the Python standard library.

## File structure

```
dashboard.py   — everything: data pipeline, HTTP server, and the full HTML/CSS/JS dashboard
README.md      — user-facing documentation
CLAUDE.md      — this file
```

The entire dashboard HTML/CSS/JS is a Python string constant named `HTML` near the bottom of `dashboard.py` (around line 420). The server serves it directly from memory.

## Key sections of dashboard.py

### 1. Data pipeline (top of file)

**`find_jsonl_files(claude_dir)`** — walks `~/.claude/projects/` recursively and yields all `.jsonl` paths.

**`decode_dir_slug(slug)`** — decodes the URL-encoded directory names (`-home-user-foo` → `/home/user/foo`) that Claude Code uses to encode project paths.

**`get_content_preview(content)`** — extracts a short text preview from a message's content field (handles both string and list-of-blocks formats).

**`load_records(claude_dir, from_date, to_date)`** — the main parser. Does two passes:
- Pass 1: collects all user messages (uuid → `{text, permissionMode}`) while walking files
- Pass 2 (same walk): builds assistant records, then links them to parent user messages via `parentUuid`

Each record includes:
- Token counts: `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `total_tokens`
- Metadata: `uuid`, `timestamp`, `dt`, `slug` (project name), `model`
- Derived: `content_preview`, `tools` (comma-separated tool names), `user_prompt`, `permission_mode`
- LOC: `lines_added`, `lines_removed` (counted from Edit/Write tool inputs)

**`make_bucket()` / `add_to_bucket()` / `buckets_to_series()`** — accumulate records into time-period buckets and convert to sorted series for charting.

**`compute_data(claude_dir, from_str, to_str)`** — orchestrates everything: calls `load_records`, accumulates into per-granularity buckets (5min, 15min, hour, day, week, month) for both overall and per-project views, builds the feed, and returns a single JSON-serialisable dict.

### 2. HTTP server

**`make_handler(claude_dir)`** — returns a `BaseHTTPRequestHandler` subclass.
- `GET /` → serves the `HTML` constant
- `GET /api/data?from=YYYY-MM-DD&to=YYYY-MM-DD` → calls `compute_data`, returns JSON

### 3. HTML constant (`HTML = r"""..."""`)

Single-page app with:
- **Header** (sticky): title, date-range pickers, project dropdown, granularity tabs, weighted toggle, live badge
- **Cards**: messages + per-token-type stats (raw counts or % of weighted total)
- **3 charts** (Chart.js 4.4.7 from CDN): token types over time, tokens by project, lines of code over time
- **Feed table**: last 200 messages, sortable, 14 columns

All state is in JS variables: `D` (data), `gran` (granularity), `proj` (project filter), `sortBy`/`sortDir`, `weighted`, `chart`/`chart2`/`chart3`.

## JSONL log format (key fields)

Each line in `~/.claude/projects/{encoded-path}/{sessionId}.jsonl` is a JSON object.

**User messages** (`type: "user"`):
```json
{
  "uuid": "...", "parentUuid": "...",
  "type": "user",
  "message": {"role": "user", "content": "..."},
  "permissionMode": "plan",   // or "auto", etc.
  "timestamp": "2026-03-24T02:02:42.519Z",
  "cwd": "/home/user/myproject",
  "sessionId": "..."
}
```

**Assistant messages** (`type: "assistant"`):
```json
{
  "uuid": "...", "parentUuid": "...",   // parentUuid links to the triggering user message
  "type": "assistant",
  "message": {
    "role": "assistant",
    "model": "claude-sonnet-4-6",
    "content": [
      {"type": "text", "text": "..."},
      {"type": "tool_use", "name": "Edit", "input": {"file_path": "...", "old_string": "...", "new_string": "..."}},
      {"type": "thinking", "thinking": "..."}
    ],
    "usage": {
      "input_tokens": 1234,
      "output_tokens": 567,
      "cache_creation_input_tokens": 890,
      "cache_read_input_tokens": 12345
    }
  },
  "timestamp": "...",
  "cwd": "/home/user/myproject"
}
```

Key points:
- Only assistant messages with a `usage` block have token counts
- `parentUuid` on an assistant message points to the user message that triggered it
- `permissionMode` ("plan", "auto", etc.) is on the **user** message, not the assistant message
- Tool calls are in `message.content[]` as `{type: "tool_use", name: "...", input: {...}}`
- Subagent logs live in `{sessionId}/subagents/agent-{id}.jsonl`

## Usage weight factors

These are the multipliers used when "Usage weighted" is toggled on:

| Token type | Weight | Field name |
|---|---|---|
| Input | 1.0 | `WEIGHTS.input` |
| Output | 5.0 | `WEIGHTS.output` |
| Cache Write | 1.25 | `WEIGHTS.cache_creation` |
| Cache Read | 0.1 | `WEIGHTS.cache_read` |

Defined as `const WEIGHTS = {...}` in the JS section.

## How to run and test

```bash
# Start the dashboard (opens browser automatically)
python3 dashboard.py

# Start without opening browser (useful for testing)
python3 dashboard.py --no-browser --port 7070

# Syntax check without starting
python3 -c "import dashboard; print('OK')"

# Test the data pipeline directly
python3 -c "
from dashboard import compute_data
d = compute_data('/root/.claude')
print('messages:', d['summary']['total_messages'])
print('feed fields:', list(d['high_utilization_feed'][0].keys()))
"
```

## Current feature set (as of last edit)

- Sticky header with all controls always visible
- Date range filter (defaults to last 7 days)
- Project filter (dropdown + click-to-filter in the feed table)
- Granularity tabs: 5min / 15min / hourly / daily / weekly / monthly
- Usage weighted toggle (default on) — scales token counts by usage-limit weights
- 3 charts: token types over time, tokens by project, lines of code over time
- Summary cards: in raw mode show counts; in weighted mode show % of weighted total
- Feed table (14 columns, all sortable by token/LOC columns): Time, Project, Mode, Model, Input, Cache Write, Cache Read, Output, W-Total/Total, +Lines, −Lines, Tools, User Prompt, Response Preview
- Auto-refresh every 5 seconds
- Deduplication across JSONL files by uuid
- Two-pass parsing to link assistant responses to parent user messages

## Design decisions

- **Single file**: keeps deployment trivial — just `python3 dashboard.py`
- **No dependencies**: avoids any pip install; Chart.js is loaded from CDN
- **In-memory HTML**: the `HTML` constant is baked into the Python file so there's no separate static directory
- **Chart.js in-place update**: charts update data without destroying/recreating to avoid flicker on 5-second poll; exception is the weighted toggle which destroys and recreates so axis titles update
- **Two-pass JSONL parsing**: user messages are collected in a first pass so assistant records can be linked to their parent user prompts regardless of file ordering
- **LOC approximation**: uses Edit/Write tool inputs in the logs rather than git diff, so it's available without running git commands but is less precise
