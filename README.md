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

- **Summary cards** — total messages, input tokens, cache write tokens, cache read tokens, output tokens
- **Token usage chart** — stacked bar chart with selectable granularity: 5-min, 15-min, hourly, daily, weekly, monthly
- **Recent messages feed** — the last 200 messages with token breakdown, sortable by time or total tokens

All views can be filtered by **project** (dropdown) and **date range** (date pickers). The dashboard auto-refreshes every 5 seconds.

## How it works

Logs live at `~/.claude/projects/`. Each project directory contains `.jsonl` files — one per session, plus subagent logs. The script walks all of them recursively, deduplicates messages by their `uuid` field (the same message can appear in multiple files), and aggregates token counts from the `usage` field of assistant messages.

## Color coding (feed)

| Color | Total tokens |
|-------|-------------|
| Green border  | < 10K  |
| Yellow border | 10K – 100K |
| Orange border | 100K – 500K |
| Red border    | > 500K |
