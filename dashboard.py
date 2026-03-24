#!/usr/bin/env python3
"""Claude Code Token Usage Dashboard

Reads ~/.claude message logs and serves a live token usage dashboard.

Usage:
    python3 dashboard.py [--port PORT] [--no-browser] [--claude-dir PATH]
"""

import os
import sys
import json
import socket
import argparse
import threading
import webbrowser
from datetime import datetime, timezone
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GRANULARITIES = ['5min', '15min', 'hour', 'day', 'week', 'month']
GRAN_KEYS = {
    '5min':  'by_5min',
    '15min': 'by_15min',
    'hour':  'by_hour',
    'day':   'by_day',
    'week':  'by_week',
    'month': 'by_month',
}
FEED_SIZE = 200

# ---------------------------------------------------------------------------
# Data pipeline
# ---------------------------------------------------------------------------

def get_period_key(dt, granularity):
    """Truncate a datetime to a granularity bucket (floor/truncation)."""
    if granularity == '5min':
        m = (dt.minute // 5) * 5
        return dt.strftime('%Y-%m-%dT%H:') + f'{m:02d}'
    if granularity == '15min':
        m = (dt.minute // 15) * 15
        return dt.strftime('%Y-%m-%dT%H:') + f'{m:02d}'
    if granularity == 'hour':
        return dt.strftime('%Y-%m-%dT%H')
    if granularity == 'day':
        return dt.strftime('%Y-%m-%d')
    if granularity == 'week':
        iso = dt.isocalendar()
        return f'{iso[0]}-W{iso[1]:02d}'
    if granularity == 'month':
        return dt.strftime('%Y-%m')
    return dt.isoformat()


def decode_dir_slug(dir_name):
    """Best-effort decode a project directory name to a readable fallback.

    e.g. '-home-user-my-project' -> '/home/user/my-project'
    This is lossy for paths with hyphens but better than the raw dir name.
    """
    if dir_name.startswith('-'):
        return '/' + dir_name[1:].replace('-', '/')
    return dir_name


def find_jsonl_files(claude_dir):
    """Recursively find all .jsonl files under claude_dir/projects/."""
    projects_dir = Path(claude_dir) / 'projects'
    if not projects_dir.exists():
        return []
    return list(projects_dir.rglob('*.jsonl'))


def get_content_preview(content, max_len=200):
    """Extract the first text content block as a short preview string."""
    if isinstance(content, str):
        return content[:max_len]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text':
                return (block.get('text') or '')[:max_len]
    return ''


def parse_date_str(s):
    """Parse a YYYY-MM-DD string to a date, or return None."""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


def load_records(claude_dir, from_date=None, to_date=None):
    """Walk all JSONL files, deduplicate by uuid, apply date filter.

    Only assistant messages with a usage block are included — these are the
    only records that carry token counts. The input_tokens field already
    accounts for all tokens consumed in each exchange (including user input),
    so including other message types would double-count.

    Returns (records list, file_count int).
    """
    seen_uuids  = set()
    user_msgs   = {}   # uuid -> {'text': str, 'mode': str}
    pending     = []   # assistant records before user-message linkage
    file_count  = 0

    for jsonl_path in find_jsonl_files(claude_dir):
        file_count += 1

        # Derive a fallback slug from the project directory name
        try:
            rel_parts = jsonl_path.relative_to(
                Path(claude_dir) / 'projects'
            ).parts
            dir_fallback = decode_dir_slug(rel_parts[0]) if rel_parts else 'unknown'
        except ValueError:
            dir_fallback = 'unknown'

        try:
            with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg_type = obj.get('type')

                    # Collect user messages so we can look up the prompt and
                    # permission mode that preceded each assistant response.
                    if msg_type == 'user':
                        uid = obj.get('uuid')
                        if uid and uid not in user_msgs:
                            umsg = obj.get('message') or {}
                            user_msgs[uid] = {
                                'text': get_content_preview(umsg.get('content', [])),
                                'mode': obj.get('permissionMode', ''),
                            }
                        continue

                    if msg_type != 'assistant':
                        continue
                    msg = obj.get('message') or {}
                    usage = msg.get('usage')
                    if not usage:
                        continue

                    uid = obj.get('uuid')
                    if not uid or uid in seen_uuids:
                        continue
                    seen_uuids.add(uid)

                    ts_str = obj.get('timestamp', '')
                    try:
                        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    except (ValueError, AttributeError):
                        continue

                    rec_date = dt.date()
                    if from_date and rec_date < from_date:
                        continue
                    if to_date and rec_date > to_date:
                        continue

                    input_tok  = int(usage.get('input_tokens') or 0)
                    output_tok = int(usage.get('output_tokens') or 0)
                    cache_write = int(usage.get('cache_creation_input_tokens') or 0)
                    cache_read  = int(usage.get('cache_read_input_tokens') or 0)

                    # Use the basename of cwd as the project name — that's the
                    # folder the user actually launched Claude in.  Fall back to
                    # the internal slug, then to the decoded directory name.
                    cwd = obj.get('cwd') or ''
                    project = os.path.basename(cwd) if cwd else (obj.get('slug') or dir_fallback)

                    # Extract unique tool names, and count lines touched via
                    # Edit (old_string → removed, new_string → added) and
                    # Write (content → added).  These are approximations based
                    # on the tool call inputs stored in the log.
                    content = msg.get('content') or []
                    seen_t, tools = set(), []
                    lines_added = lines_removed = 0
                    for block in content:
                        if not isinstance(block, dict) or block.get('type') != 'tool_use':
                            continue
                        name = block.get('name', '')
                        if name and name not in seen_t:
                            tools.append(name)
                            seen_t.add(name)
                        inp = block.get('input') or {}
                        if name == 'Edit':
                            old_s = inp.get('old_string') or ''
                            new_s = inp.get('new_string') or ''
                            if old_s:
                                lines_removed += old_s.count('\n') + 1
                            if new_s:
                                lines_added += new_s.count('\n') + 1
                        elif name == 'Write':
                            c = inp.get('content') or ''
                            if c:
                                lines_added += c.count('\n') + 1

                    pending.append({
                        'uuid':                  uid,
                        'dt':                    dt,
                        'timestamp':             ts_str,
                        'slug':                  project,
                        'model':                 msg.get('model') or 'unknown',
                        'input_tokens':          input_tok,
                        'output_tokens':         output_tok,
                        'cache_creation_tokens': cache_write,
                        'cache_read_tokens':     cache_read,
                        'total_tokens':          input_tok + output_tok + cache_write + cache_read,
                        'content_preview':       get_content_preview(content),
                        'tools':                 ', '.join(tools),
                        'lines_added':           lines_added,
                        'lines_removed':         lines_removed,
                        'parent_uuid':           obj.get('parentUuid') or '',
                    })
        except OSError:
            continue

    # Link each assistant record to its parent user message.
    records = []
    for rec in pending:
        parent = user_msgs.get(rec.pop('parent_uuid'), {})
        rec['user_prompt']     = parent.get('text', '')
        rec['permission_mode'] = parent.get('mode', '')
        records.append(rec)

    return records, file_count


def make_bucket():
    return {'input': 0, 'output': 0, 'cache_creation': 0, 'cache_read': 0, 'messages': 0,
            'lines_added': 0, 'lines_removed': 0}


def add_to_bucket(bucket, rec):
    bucket['input']          += rec['input_tokens']
    bucket['output']         += rec['output_tokens']
    bucket['cache_creation'] += rec['cache_creation_tokens']
    bucket['cache_read']     += rec['cache_read_tokens']
    bucket['messages']       += 1
    bucket['lines_added']    += rec['lines_added']
    bucket['lines_removed']  += rec['lines_removed']


def buckets_to_series(buckets_dict):
    """Convert a period→bucket mapping to a sorted list."""
    return [{'period': k, **v} for k, v in sorted(buckets_dict.items())]


def compute_data(claude_dir, from_str=None, to_str=None):
    """Parse logs and return the full API response dict."""
    from_date = parse_date_str(from_str)
    to_date   = parse_date_str(to_str)

    records, file_count = load_records(claude_dir, from_date, to_date)

    # Aggregation structures
    overall     = {g: defaultdict(make_bucket) for g in GRANULARITIES}
    by_project  = defaultdict(lambda: {g: defaultdict(make_bucket) for g in GRANULARITIES})
    proj_summ   = defaultdict(lambda: {
        'total_messages': 0,
        'total_input_tokens': 0,
        'total_output_tokens': 0,
        'total_cache_creation_tokens': 0,
        'total_cache_read_tokens': 0,
        'total_tokens': 0,
    })

    total_messages       = 0
    total_input          = 0
    total_output         = 0
    total_cache_creation = 0
    total_cache_read     = 0

    for rec in records:
        dt   = rec['dt']
        slug = rec['slug']

        total_messages       += 1
        total_input          += rec['input_tokens']
        total_output         += rec['output_tokens']
        total_cache_creation += rec['cache_creation_tokens']
        total_cache_read     += rec['cache_read_tokens']

        ps = proj_summ[slug]
        ps['total_messages']               += 1
        ps['total_input_tokens']           += rec['input_tokens']
        ps['total_output_tokens']          += rec['output_tokens']
        ps['total_cache_creation_tokens']  += rec['cache_creation_tokens']
        ps['total_cache_read_tokens']      += rec['cache_read_tokens']
        ps['total_tokens']                 += rec['total_tokens']

        for g in GRANULARITIES:
            period = get_period_key(dt, g)
            add_to_bucket(overall[g][period], rec)
            add_to_bucket(by_project[slug][g][period], rec)

    # Feed: most recent FEED_SIZE messages (client sorts further)
    feed_records = sorted(records, key=lambda r: r['timestamp'], reverse=True)[:FEED_SIZE]
    feed_out = [
        {k: r[k] for k in (
            'uuid', 'timestamp', 'slug', 'model',
            'input_tokens', 'output_tokens',
            'cache_creation_tokens', 'cache_read_tokens',
            'total_tokens', 'content_preview',
            'tools', 'user_prompt', 'permission_mode',
            'lines_added', 'lines_removed',
        )}
        for r in feed_records
    ]

    # Per-project output
    by_project_out = {
        slug: {
            'summary': dict(ps),
            **{GRAN_KEYS[g]: buckets_to_series(gran_data[g]) for g in GRANULARITIES},
        }
        for slug, gran_data in by_project.items()
        for ps in [proj_summ[slug]]
    }

    total_all = total_input + total_output + total_cache_creation + total_cache_read

    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'date_range':   {'from': from_str, 'to': to_str},
        'meta':         {'file_count': file_count, 'message_count': total_messages},
        'summary': {
            'total_messages':              total_messages,
            'total_input_tokens':          total_input,
            'total_output_tokens':         total_output,
            'total_cache_creation_tokens': total_cache_creation,
            'total_cache_read_tokens':     total_cache_read,
            'total_tokens':                total_all,
            'projects':                    sorted(proj_summ.keys()),
        },
        **{GRAN_KEYS[g]: buckets_to_series(overall[g]) for g in GRANULARITIES},
        'by_project':            by_project_out,
        'high_utilization_feed': feed_out,
    }

# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def make_handler(claude_dir):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)

            if parsed.path == '/':
                body = HTML.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif parsed.path == '/api/data':
                params   = parse_qs(parsed.query)
                from_str = (params.get('from') or [None])[0]
                to_str   = (params.get('to')   or [None])[0]
                try:
                    data = compute_data(claude_dir, from_str, to_str)
                    body = json.dumps(data).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as exc:
                    msg = str(exc).encode('utf-8')
                    self.send_response(500)
                    self.send_header('Content-Type', 'text/plain')
                    self.send_header('Content-Length', str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)

            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):  # suppress request logging
            pass

    return Handler


def find_free_port(start):
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('', port))
                return port
            except OSError:
                continue
    raise RuntimeError(f'No free port found starting at {start}')

# ---------------------------------------------------------------------------
# Dashboard HTML (served at /)
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Claude Code Usage</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg:      #0f1117;
  --surface: #1a1d27;
  --border:  #2d3148;
  --text:    #e2e8f0;
  --muted:   #8892a4;
  --blue:    #4a9eff;
  --amber:   #f59e0b;
  --emerald: #10b981;
  --purple:  #a855f7;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px;min-height:100vh}

/* ---- Header ---- */
header{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:100}
header h1{font-size:17px;font-weight:600;white-space:nowrap}
.hc{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.hc label{color:var(--muted);font-size:12px}
.hc input[type=date],.hc select{background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:13px}
.hc select{cursor:pointer}
button{background:var(--blue);border:1px solid transparent;border-radius:6px;color:#fff;cursor:pointer;font-size:12px;padding:5px 11px;transition:opacity .15s;vertical-align:middle}
button:hover{opacity:.82}
button.sec{background:transparent;border-color:var(--border);color:var(--muted);margin:0}
.hr{margin-left:auto;display:flex;align-items:center;gap:10px;flex-shrink:0}
#badge{font-size:12px;padding:3px 9px;border-radius:12px;background:rgba(16,185,129,.15);color:var(--emerald);border:1px solid rgba(16,185,129,.3)}
#badge.err{background:rgba(239,68,68,.15);color:#ef4444;border-color:rgba(239,68,68,.3)}
#upd{color:var(--muted);font-size:12px}

/* ---- Main ---- */
main{padding:18px 20px}

/* ---- Cards ---- */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.cl{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-bottom:5px}
.cv{font-size:22px;font-weight:700;letter-spacing:-.02em}
.cv.blue{color:var(--blue)}.cv.amber{color:var(--amber)}.cv.emerald{color:var(--emerald)}.cv.purple{color:var(--purple)}.cv.muted{color:var(--muted)}

/* ---- Section ---- */
.sec{background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:18px;overflow:hidden}
.sh{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.st{font-size:14px;font-weight:600}

/* ---- Tabs ---- */
.tabs{display:flex;gap:4px;flex-wrap:wrap}
.tab{background:transparent;border:1px solid var(--border);border-radius:6px;color:var(--muted);font-size:12px;padding:4px 10px}
.tab.on{background:var(--blue);border-color:var(--blue);color:#fff}

/* ---- Chart ---- */
.cw{padding:16px;height:320px;position:relative}
#nodata{display:none;position:absolute;inset:0;align-items:center;justify-content:center;color:var(--muted);font-size:14px;pointer-events:none}

/* ---- Feed table ---- */
.fw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{padding:9px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);border-bottom:1px solid var(--border);white-space:nowrap}
thead th.sort{cursor:pointer;user-select:none}
thead th.sort:hover{color:var(--text)}
tbody tr{border-bottom:1px solid rgba(45,49,72,.5);transition:background .1s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:rgba(255,255,255,.03)}
tbody td{padding:8px 12px;white-space:nowrap}
.rg td:first-child{border-left:3px solid rgba(16,185,129,.5)}
.ry td:first-child{border-left:3px solid rgba(245,158,11,.5)}
.ro td:first-child{border-left:3px solid rgba(249,115,22,.5)}
.rr td:first-child{border-left:3px solid rgba(239,68,68,.5)}
.mc{color:var(--muted);font-size:11px;max-width:160px;overflow:hidden;text-overflow:ellipsis}
.tc{color:var(--text)}
.ic{color:var(--blue)}.wc{color:var(--amber)}.rc{color:var(--emerald)}.oc{color:var(--purple)}
.pc{color:var(--muted);font-size:12px;max-width:260px;overflow:hidden;text-overflow:ellipsis}
.toolsc{color:var(--muted);font-size:11px;max-width:160px;overflow:hidden;text-overflow:ellipsis}
.modec{font-size:11px;padding:2px 6px;border-radius:4px;background:rgba(74,158,255,.15);color:var(--blue);white-space:nowrap}
.upc{color:var(--text);font-size:12px;max-width:260px;overflow:hidden;text-overflow:ellipsis}
.wt-lbl{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);cursor:pointer;user-select:none}
.wt-lbl input{accent-color:var(--blue);cursor:pointer}
.hdiv{width:1px;height:20px;background:var(--border);align-self:center;flex-shrink:0}
.projlink{cursor:pointer;text-underline-offset:2px;text-decoration:underline;text-decoration-style:dotted}
.projlink:hover{color:var(--blue)}
</style>
</head>
<body>
<header>
  <h1>Claude Code Usage</h1>
  <div class="hc">
    <label>From</label><input type="date" id="fd">
    <label>To</label><input type="date" id="td">
    <button id="ab">Apply</button>
    <button class="sec" id="cb">Clear</button>
    <select id="ps"><option value="">All Projects</option></select>
  </div>
  <div class="hdiv"></div>
  <div class="tabs" id="tabs">
    <button class="tab" data-g="5min">5 min</button>
    <button class="tab" data-g="15min">15 min</button>
    <button class="tab on" data-g="hour">Hourly</button>
    <button class="tab" data-g="day">Daily</button>
    <button class="tab" data-g="week">Weekly</button>
    <button class="tab" data-g="month">Monthly</button>
  </div>
  <label class="wt-lbl" title="Multiply each token type by its weight toward Anthropic usage limits: Output ×5, Cache Write ×1.25, Cache Read ×0.1, Input ×1">
    <input type="checkbox" id="wchk" checked> Usage weighted
  </label>
  <div class="hr">
    <span id="badge">Loading…</span>
    <span id="upd"></span>
  </div>
</header>

<main>
  <div class="cards">
    <div class="card"><div class="cl">Messages</div><div class="cv muted" id="sm">—</div></div>
    <div class="card" title="Tokens in the prompt that were not cached — your actual message text, tool results, and any context Claude had to read fresh."><div class="cl">Input Tokens</div><div class="cv blue" id="si">—</div></div>
    <div class="card" title="Tokens written into the prompt cache this session. Charged at a higher rate than regular input, but future reads of the same content are much cheaper."><div class="cl">Cache Write</div><div class="cv amber" id="sw">—</div></div>
    <div class="card" title="Tokens served from the prompt cache — previously cached context that Claude reused instead of re-reading. Much cheaper than regular input tokens."><div class="cl">Cache Read</div><div class="cv emerald" id="sr">—</div></div>
    <div class="card" title="Tokens in Claude's response — the text, code, and tool calls Claude generated."><div class="cl">Output Tokens</div><div class="cv purple" id="so">—</div></div>
  </div>

  <div class="sec">
    <div class="sh">
      <span class="st">Token Usage Over Time</span>
    </div>
    <div class="cw">
      <canvas id="ch"></canvas>
      <div id="nodata">No data for this view</div>
    </div>
  </div>

  <div class="sec">
    <div class="sh">
      <span class="st">Tokens by Project</span>
      <span style="font-size:12px;color:var(--muted)">Total tokens per period — one bar per project</span>
    </div>
    <div class="cw">
      <canvas id="ch2"></canvas>
      <div id="nodata2">No project data for this view</div>
    </div>
  </div>

  <div class="sec">
    <div class="sh">
      <span class="st">Lines of Code Over Time</span>
      <span style="font-size:12px;color:var(--muted)">Approximate — counted from Edit (old/new string) and Write (content) tool inputs</span>
    </div>
    <div class="cw">
      <canvas id="ch3"></canvas>
      <div id="nodata3">No edit/write activity for this view</div>
    </div>
  </div>

  <div class="sec">
    <div class="sh"><span class="st">Recent Messages</span></div>
    <div class="fw">
      <table>
        <thead><tr>
          <th class="sort" data-s="timestamp">Time <span class="si">▼</span></th>
          <th>Project</th>
          <th>Mode</th>
          <th>Model</th>
          <th class="sort" data-s="input">Input <span class="si"></span></th>
          <th class="sort" data-s="cache_write">Cache Write <span class="si"></span></th>
          <th class="sort" data-s="cache_read">Cache Read <span class="si"></span></th>
          <th class="sort" data-s="output">Output <span class="si"></span></th>
          <th class="sort" data-s="total"><span id="th-total-lbl">Total</span> <span class="si"></span></th>
          <th class="sort" data-s="lines_added">+Lines <span class="si"></span></th>
          <th class="sort" data-s="lines_removed">-Lines <span class="si"></span></th>
          <th>Tools</th>
          <th>User Prompt</th>
          <th>Response Preview</th>
        </tr></thead>
        <tbody id="fb"><tr><td colspan="14" style="text-align:center;padding:20px;color:#8892a4">Loading…</td></tr></tbody>
      </table>
    </div>
  </div>
</main>

<script>
// ---- State ----
let D = null, gran = 'hour', proj = '', sortBy = 'timestamp', sortDir = 'desc', chart = null, chart2 = null, chart3 = null, weighted = true;

const PROJ_COLORS = ['#4a9eff','#f59e0b','#10b981','#a855f7','#ef4444','#06b6d4','#84cc16','#f97316','#ec4899','#6366f1'];
// Weights each token type contributes toward Anthropic usage limits.
const WEIGHTS = {input: 1.0, output: 5.0, cache_creation: 1.25, cache_read: 0.1};

const GK = {
  '5min':'by_5min','15min':'by_15min','hour':'by_hour',
  'day':'by_day','week':'by_week','month':'by_month'
};

// ---- Utils ----
function fmt(n) {
  if (n==null) return '0';
  if (n>=1e9) return (n/1e9).toFixed(1)+'B';
  if (n>=1e6) return (n/1e6).toFixed(1)+'M';
  if (n>=1e3) return (n/1e3).toFixed(1)+'K';
  return ''+n;
}
function rel(iso) {
  const s=(Date.now()-new Date(iso))/1000;
  if(s<60) return Math.floor(s)+'s ago';
  if(s<3600) return Math.floor(s/60)+'m ago';
  if(s<86400) return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
function esc(s) {
  return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function rowCls(t) {
  if(t>500000) return 'rr';
  if(t>100000) return 'ro';
  if(t>10000)  return 'ry';
  return 'rg';
}
function wtotal(m) {
  if(!weighted) return m.total_tokens;
  return Math.round(
    m.input_tokens            * WEIGHTS.input +
    m.output_tokens           * WEIGHTS.output +
    m.cache_creation_tokens   * WEIGHTS.cache_creation +
    m.cache_read_tokens       * WEIGHTS.cache_read
  );
}
function summ() {
  if(proj && D.by_project[proj]) return D.by_project[proj].summary;
  return D.summary;
}
function series() {
  const k=GK[gran];
  if(proj && D.by_project[proj]) return D.by_project[proj][k]||[];
  return D[k]||[];
}

// ---- Render cards ----
function renderCards() {
  const s=summ();
  document.getElementById('sm').textContent=fmt(s.total_messages);
  if(weighted) {
    const W=WEIGHTS;
    const wt=s.total_input_tokens*W.input + s.total_output_tokens*W.output +
             s.total_cache_creation_tokens*W.cache_creation + s.total_cache_read_tokens*W.cache_read;
    const pct=(v,w)=>wt>0?(v*w/wt*100).toFixed(2)+'%':'—';
    document.getElementById('si').textContent=pct(s.total_input_tokens,        W.input);
    document.getElementById('sw').textContent=pct(s.total_cache_creation_tokens,W.cache_creation);
    document.getElementById('sr').textContent=pct(s.total_cache_read_tokens,   W.cache_read);
    document.getElementById('so').textContent=pct(s.total_output_tokens,        W.output);
  } else {
    document.getElementById('si').textContent=fmt(s.total_input_tokens);
    document.getElementById('sw').textContent=fmt(s.total_cache_creation_tokens);
    document.getElementById('sr').textContent=fmt(s.total_cache_read_tokens);
    document.getElementById('so').textContent=fmt(s.total_output_tokens);
  }
}

// ---- Render chart ----
function renderChart() {
  const s=series();
  const nd=document.getElementById('nodata');
  const cv=document.getElementById('ch');
  if(!s.length){
    if(chart){chart.destroy();chart=null;}
    cv.style.display='none'; nd.style.display='flex'; return;
  }
  cv.style.display=''; nd.style.display='none';

  const W=WEIGHTS, yTitle=weighted?'Usage Weight':'Tokens';
  const labels=s.map(d=>d.period);
  const rows=[
    s.map(d=>weighted?Math.round(d.cache_read*W.cache_read):d.cache_read),
    s.map(d=>weighted?Math.round(d.cache_creation*W.cache_creation):d.cache_creation),
    s.map(d=>weighted?Math.round(d.input*W.input):d.input),
    s.map(d=>weighted?Math.round(d.output*W.output):d.output),
    s.map(d=>d.messages),
  ];

  // Update in place to avoid re-running the draw animation on every poll
  if(chart){
    chart.data.labels=labels;
    chart.data.datasets.forEach((ds,i)=>{ds.data=rows[i];});
    chart.options.scales.y.title.text=yTitle;
    chart.update('none');
    return;
  }

  chart=new Chart(cv.getContext('2d'),{
    data:{
      labels,
      datasets:[
        {type:'bar',label:'Cache Read',   data:rows[0],backgroundColor:'#10b981',stack:'t',yAxisID:'y'},
        {type:'bar',label:'Cache Write',  data:rows[1],backgroundColor:'#f59e0b',stack:'t',yAxisID:'y'},
        {type:'bar',label:'Input',        data:rows[2],backgroundColor:'#4a9eff',stack:'t',yAxisID:'y'},
        {type:'bar',label:'Output',       data:rows[3],backgroundColor:'#a855f7',stack:'t',yAxisID:'y'},
        {type:'line',label:'Messages',    data:rows[4],
         borderColor:'#94a3b8',backgroundColor:'transparent',
         yAxisID:'y2',pointRadius:3,borderWidth:2,tension:.3,pointBackgroundColor:'#94a3b8'}
      ]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      scales:{
        x:{stacked:true,ticks:{color:'#8892a4',maxRotation:45},grid:{color:'#2d3148'}},
        y:{stacked:true,ticks:{color:'#8892a4',callback:v=>fmt(v)},grid:{color:'#2d3148'},
           title:{display:true,text:yTitle,color:'#8892a4'}},
        y2:{position:'right',ticks:{color:'#8892a4'},grid:{drawOnChartArea:false},
            title:{display:true,text:'Messages',color:'#8892a4'}}
      },
      plugins:{
        legend:{labels:{color:'#e2e8f0',boxWidth:12,padding:14}},
        tooltip:{
          backgroundColor:'#1a1d27',borderColor:'#2d3148',borderWidth:1,
          titleColor:'#e2e8f0',bodyColor:'#8892a4',
          callbacks:{label:c=>' '+c.dataset.label+': '+fmt(c.parsed.y)}
        }
      }
    }
  });
}

// ---- Render project chart ----
function renderProjectChart() {
  const key=GK[gran];
  const nd=document.getElementById('nodata2');
  const cv=document.getElementById('ch2');
  const projects=Object.keys(D.by_project||{});

  if(!projects.length){
    if(chart2){chart2.destroy();chart2=null;}
    cv.style.display='none'; nd.style.display='flex'; return;
  }
  cv.style.display=''; nd.style.display='none';

  // Collect all periods across all projects, sorted
  const periodSet=new Set();
  projects.forEach(p=>{(D.by_project[p][key]||[]).forEach(d=>periodSet.add(d.period));});
  const labels=[...periodSet].sort();

  const W=WEIGHTS, yTitle2=weighted?'Usage Weight':'Total Tokens';
  const datasets=projects.map((p,i)=>{
    const byPeriod={};
    (D.by_project[p][key]||[]).forEach(d=>{
      byPeriod[d.period]=weighted
        ? Math.round(d.input*W.input+d.output*W.output+d.cache_creation*W.cache_creation+d.cache_read*W.cache_read)
        : d.input+d.output+d.cache_creation+d.cache_read;
    });
    return {
      type:'bar', label:p,
      data:labels.map(l=>byPeriod[l]||0),
      backgroundColor:PROJ_COLORS[i%PROJ_COLORS.length],
      stack:'s',
    };
  });

  if(chart2){
    chart2.data.labels=labels;
    // Rebuild datasets (project list may change between polls)
    chart2.data.datasets=datasets;
    chart2.options.scales.y.title.text=yTitle2;
    chart2.update('none');
    return;
  }

  chart2=new Chart(cv.getContext('2d'),{
    data:{labels,datasets},
    options:{
      responsive:true,maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      scales:{
        x:{stacked:true,ticks:{color:'#8892a4',maxRotation:45},grid:{color:'#2d3148'}},
        y:{stacked:true,ticks:{color:'#8892a4',callback:v=>fmt(v)},grid:{color:'#2d3148'},
           title:{display:true,text:yTitle2,color:'#8892a4'}}
      },
      plugins:{
        legend:{labels:{color:'#e2e8f0',boxWidth:12,padding:14}},
        tooltip:{
          backgroundColor:'#1a1d27',borderColor:'#2d3148',borderWidth:1,
          titleColor:'#e2e8f0',bodyColor:'#8892a4',
          callbacks:{label:c=>' '+c.dataset.label+': '+fmt(c.parsed.y)}
        }
      }
    }
  });
}

// ---- Render LOC chart ----
function renderLocChart() {
  const s=series();
  const nd=document.getElementById('nodata3');
  const cv=document.getElementById('ch3');
  const hasData=s.some(d=>d.lines_added||d.lines_removed);

  if(!s.length||!hasData){
    if(chart3){chart3.destroy();chart3=null;}
    cv.style.display='none'; nd.style.display='flex'; return;
  }
  cv.style.display=''; nd.style.display='none';

  const labels=s.map(d=>d.period);
  const added  =s.map(d=>d.lines_added||0);
  const removed=s.map(d=>d.lines_removed||0);

  if(chart3){
    chart3.data.labels=labels;
    chart3.data.datasets[0].data=added;
    chart3.data.datasets[1].data=removed;
    chart3.update('none');
    return;
  }

  chart3=new Chart(cv.getContext('2d'),{
    data:{
      labels,
      datasets:[
        {type:'bar',label:'Lines Added',  data:added,  backgroundColor:'rgba(16,185,129,.75)',stack:'l'},
        {type:'bar',label:'Lines Removed',data:removed,backgroundColor:'rgba(239,68,68,.75)',stack:'r'},
      ]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      scales:{
        x:{ticks:{color:'#8892a4',maxRotation:45},grid:{color:'#2d3148'}},
        y:{ticks:{color:'#8892a4'},grid:{color:'#2d3148'},
           title:{display:true,text:'Lines',color:'#8892a4'}}
      },
      plugins:{
        legend:{labels:{color:'#e2e8f0',boxWidth:12,padding:14}},
        tooltip:{
          backgroundColor:'#1a1d27',borderColor:'#2d3148',borderWidth:1,
          titleColor:'#e2e8f0',bodyColor:'#8892a4',
          callbacks:{label:c=>' '+c.dataset.label+': '+c.parsed.y.toLocaleString()}
        }
      }
    }
  });
}

// ---- Render feed ----
function renderFeed() {
  let feed=[...(D.high_utilization_feed||[])];
  if(proj) feed=feed.filter(m=>m.slug===proj);
  const tokenKey={input:'input_tokens',cache_write:'cache_creation_tokens',
                  cache_read:'cache_read_tokens',output:'output_tokens',
                  lines_added:'lines_added',lines_removed:'lines_removed'};
  feed.sort((a,b)=>{
    let d = sortBy==='timestamp'
      ? new Date(b.timestamp)-new Date(a.timestamp)
      : sortBy==='total'
        ? wtotal(b)-wtotal(a)
        : (b[tokenKey[sortBy]]||0)-(a[tokenKey[sortBy]]||0);
    return sortDir==='desc'?d:-d;
  });
  document.getElementById('th-total-lbl').textContent=weighted?'W-Total':'Total';
  const tb=document.getElementById('fb');
  if(!feed.length){
    tb.innerHTML='<tr><td colspan="14" style="text-align:center;padding:20px;color:#8892a4">No messages</td></tr>';
    return;
  }
  tb.innerHTML=feed.map(m=>{
    const ts=new Date(m.timestamp);
    const modeCell=m.permission_mode
      ? '<span class="modec">'+esc(m.permission_mode)+'</span>'
      : '—';
    return '<tr class="'+rowCls(m.total_tokens)+'">'
      +'<td title="'+esc(ts.toLocaleString())+'">'+esc(rel(m.timestamp))+'</td>'
      +'<td><span class="projlink" data-p="'+esc(m.slug||'')+'">'+esc(m.slug||'—')+'</span></td>'
      +'<td>'+modeCell+'</td>'
      +'<td class="mc" title="'+esc(m.model)+'">'+esc(m.model||'—')+'</td>'
      +'<td class="ic">'+fmt(m.input_tokens)+'</td>'
      +'<td class="wc">'+fmt(m.cache_creation_tokens)+'</td>'
      +'<td class="rc">'+fmt(m.cache_read_tokens)+'</td>'
      +'<td class="oc">'+fmt(m.output_tokens)+'</td>'
      +'<td class="tc"><strong>'+fmt(wtotal(m))+'</strong></td>'
      +'<td style="color:#10b981">'+((m.lines_added||0)||'—')+'</td>'
      +'<td style="color:#ef4444">'+((m.lines_removed||0)||'—')+'</td>'
      +'<td class="toolsc" title="'+esc(m.tools)+'">'+esc(m.tools||'—')+'</td>'
      +'<td class="upc" title="'+esc(m.user_prompt)+'">'+esc(m.user_prompt||'—')+'</td>'
      +'<td class="pc" title="'+esc(m.content_preview)+'">'+esc(m.content_preview||'—')+'</td>'
      +'</tr>';
  }).join('');
}

function updateSortUI() {
  document.querySelectorAll('thead th.sort').forEach(th=>{
    th.querySelector('.si').textContent=th.dataset.s===sortBy?(sortDir==='desc'?' ▼':' ▲'):'';
  });
}

function updateProjectDropdown() {
  const sel=document.getElementById('ps');
  const prev=sel.value;
  while(sel.options.length>1) sel.remove(1);
  (D.summary.projects||[]).forEach(p=>{
    const o=new Option(p,p);
    if(p===prev) o.selected=true;
    sel.add(o);
  });
}

function renderAll() {
  updateProjectDropdown();
  renderCards();
  renderChart();
  renderProjectChart();
  renderLocChart();
  renderFeed();
  updateSortUI();
}

// ---- Fetch ----
async function fetchData() {
  const p=new URLSearchParams();
  const fv=document.getElementById('fd').value;
  const tv=document.getElementById('td').value;
  if(fv) p.set('from',fv);
  if(tv) p.set('to',tv);
  try {
    const r=await fetch('/api/data?'+p);
    if(!r.ok) throw new Error('HTTP '+r.status);
    D=await r.json();
    const b=document.getElementById('badge');
    b.textContent='● Live'; b.className='';
    document.getElementById('upd').textContent='Updated '+new Date(D.generated_at).toLocaleTimeString();
    renderAll();
  } catch(e) {
    const b=document.getElementById('badge');
    b.textContent='● Disconnected'; b.className='err';
  }
}

// ---- Events ----
document.getElementById('ab').addEventListener('click',fetchData);
document.getElementById('cb').addEventListener('click',()=>{
  document.getElementById('fd').value='';
  document.getElementById('td').value='';
  fetchData();
});
document.getElementById('ps').addEventListener('change',e=>{
  proj=e.target.value;
  if(D) renderAll();
});
document.getElementById('wchk').addEventListener('change',e=>{
  weighted=e.target.checked;
  if(chart){chart.destroy();chart=null;}
  if(chart2){chart2.destroy();chart2=null;}
  if(D){renderCards();renderChart();renderProjectChart();renderFeed();}
});
document.getElementById('tabs').addEventListener('click',e=>{
  const t=e.target.closest('[data-g]');
  if(!t) return;
  gran=t.dataset.g;
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  t.classList.add('on');
  if(D){renderChart();renderProjectChart();renderLocChart();}
});
document.getElementById('fb').addEventListener('click',e=>{
  const el=e.target.closest('.projlink');
  if(!el||!el.dataset.p) return;
  proj=el.dataset.p;
  document.getElementById('ps').value=proj;
  if(D) renderAll();
});
document.querySelector('thead').addEventListener('click',e=>{
  const th=e.target.closest('th.sort');
  if(!th) return;
  if(sortBy===th.dataset.s) sortDir=sortDir==='desc'?'asc':'desc';
  else{sortBy=th.dataset.s;sortDir='desc';}
  updateSortUI();
  if(D) renderFeed();
});

// ---- Init ----
{
  const d=new Date();
  d.setDate(d.getDate()-7);
  document.getElementById('fd').value=d.toISOString().slice(0,10);
}
fetchData();
setInterval(fetchData,5000);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Claude Code Token Usage Dashboard')
    parser.add_argument('--port',       type=int, default=7070,
                        help='Port to listen on (default: 7070)')
    parser.add_argument('--no-browser', action='store_true',
                        help='Do not open the browser automatically')
    parser.add_argument('--claude-dir', default=os.path.expanduser('~/.claude'),
                        help='Path to Claude directory (default: ~/.claude)')
    args = parser.parse_args()

    claude_dir = args.claude_dir
    if not os.path.isdir(claude_dir):
        print(f'Error: Claude directory not found: {claude_dir}', file=sys.stderr)
        sys.exit(1)

    port    = find_free_port(args.port)
    url     = f'http://localhost:{port}'
    Handler = make_handler(claude_dir)
    server  = ThreadingHTTPServer(('', port), Handler)

    print(f'Claude Code Usage Dashboard')
    print(f'URL:  {url}')
    print(f'Logs: {claude_dir}')
    print(f'Press Ctrl+C to stop')

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nDashboard stopped.')
        server.shutdown()


if __name__ == '__main__':
    main()
