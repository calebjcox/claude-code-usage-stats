# Claude Code Usage Stats

A single-file dashboard for exploring token usage from your Claude Code message logs.

## Requirements

- Python 3.6+
- No extra packages — uses only the standard library
- An internet connection the first time you open the dashboard (loads Chart.js from a CDN)

## Usage

```bash
python3 dashboard.py
```

This starts a local web server and opens the dashboard in your browser automatically.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port PORT` | `7070` | Port to listen on (auto-increments if taken) |
| `--no-browser` | — | Don't open the browser automatically |
| `--claude-dir PATH` | `~/.claude` | Path to Claude directory |

## What it shows

### Summary cards
Total messages, and for each token type (Input, Cache Write, Cache Read, Output): raw counts in normal mode, or percentage of usage-limit weight in weighted mode.

### Charts (three total, all share the granularity tabs and auto-refresh)
- **Token Usage Over Time** — stacked bar chart per token type, with an overlay line for message count
- **Tokens by Project** — stacked bar chart, one series per project, showing relative token usage across all your projects
- **Lines of Code Over Time** — grouped bars showing lines added (green) and lines removed (red), derived from `Edit` and `Write` tool call inputs

### Recent messages feed
The last 200 messages, sortable by any column:

| Column | Description |
|--------|-------------|
| Time | Relative timestamp (click for exact) |
| Project | Project folder name — **click to filter** the entire dashboard to that project |
| Mode | `plan` badge when the session was in plan mode |
| Model | Model ID (e.g. `claude-sonnet-4-6`) |
| Input / Cache Write / Cache Read / Output | Raw token counts per type |
| W-Total / Total | Weighted or raw total tokens |
| +Lines / −Lines | Approximate lines added/removed via Edit and Write tools |
| Tools | Unique tool names called in this response |
| User Prompt | The message the user sent that triggered this response |
| Response Preview | The first text content of Claude's response |

### Controls (always visible in the sticky header)
- **Date range** — From / To pickers + Apply / Clear; defaults to the last 7 days
- **Project dropdown** — filter all views to a single project (also set by clicking a project name in the table)
- **Granularity tabs** — 5 min / 15 min / Hourly / Daily / Weekly / Monthly; applies to all three charts
- **Usage weighted toggle** — when on (default), multiplies each token type by its approximate weight toward Anthropic usage limits before rendering charts and the Total column

### Usage weight factors

| Token type | Weight | Why |
|------------|--------|-----|
| Output | ×5 | Output tokens are the most expensive per token |
| Cache Write | ×1.25 | Slightly above input cost |
| Cache Read | ×0.1 | Much cheaper — cached context reuse |
| Input | ×1 | Baseline |

## How it works

Logs live at `~/.claude/projects/`. Each project directory contains `.jsonl` files — one per session, plus subagent logs in subdirectories. The script:

1. Walks all JSONL files recursively
2. Deduplicates messages by `uuid` (the same message can appear in multiple files)
3. Aggregates token counts from the `usage` field of assistant messages
4. Derives user prompt text and `permissionMode` by following each message's `parentUuid` to its parent user message
5. Counts approximate lines changed by reading `old_string`/`new_string` from `Edit` calls and `content` from `Write` calls
6. Serves everything as JSON from a local HTTP server with 5-second auto-refresh

## Color coding (feed rows)

| Border color | Raw total tokens |
|---|---|
| Green | < 10K |
| Yellow | 10K – 100K |
| Orange | 100K – 500K |
| Red | > 500K |

## Lines of code — caveats

The LOC counts are approximate and based on tool call inputs stored in the JSONL logs, not a git diff:
- `Edit` calls: `old_string` → lines removed, `new_string` → lines added
- `Write` calls: `content` → all lines counted as added (even if overwriting an existing file)
- Changes made via `Bash` (e.g. `sed`, `git checkout`) are not tracked
