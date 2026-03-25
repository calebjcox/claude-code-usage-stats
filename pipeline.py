"""Data pipeline for Claude Code Usage Dashboard.

Parses ~/.claude JSONL message logs and returns a JSON-serialisable dict
suitable for the dashboard API endpoint.
"""

import os
import json
from datetime import datetime, timezone
from collections import defaultdict
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
# Helpers
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

# ---------------------------------------------------------------------------
# Core parsing
# ---------------------------------------------------------------------------

def load_records(claude_dir, from_date=None, to_date=None):
    """Walk all JSONL files, deduplicate by uuid, apply date filter.

    Only assistant messages with a usage block are included — these are the
    only records that carry token counts. The input_tokens field already
    accounts for all tokens consumed in each exchange (including user input),
    so including other message types would double-count.

    Returns (records list, file_count int).
    """
    seen_uuids        = set()
    user_msgs         = {}   # uuid -> {'text': str, 'mode': str}
    msg_parent        = {}   # uuid -> parentUuid for ALL message types (for chain tracing)
    user_prompt_uuids = set()  # UUIDs of user messages that have actual text content
    pending           = []   # assistant records before user-message linkage
    file_count        = 0

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

                    # Track parent chain for ALL message types so we can trace
                    # multi-step assistant responses back to the root user prompt.
                    m_uid   = obj.get('uuid')
                    m_puuid = obj.get('parentUuid') or ''
                    if m_uid:
                        msg_parent[m_uid] = m_puuid

                    # Collect user messages so we can look up the prompt and
                    # permission mode that preceded each assistant response.
                    if msg_type == 'user':
                        uid = obj.get('uuid')
                        if uid and uid not in user_msgs:
                            umsg = obj.get('message') or {}
                            text = get_content_preview(umsg.get('content', []))
                            user_msgs[uid] = {
                                'text': text,
                                'mode': obj.get('permissionMode', ''),
                            }
                            if text:
                                user_prompt_uuids.add(uid)
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

                    input_tok   = int(usage.get('input_tokens') or 0)
                    output_tok  = int(usage.get('output_tokens') or 0)
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

    # Trace a UUID back to the root user-prompt UUID by following parentUuid
    # links.  This handles multi-step chains where each assistant turn's parent
    # is a tool-result user message rather than the original text prompt.
    def find_root_prompt(start):
        visited, current = set(), start
        while current and current not in visited:
            if current in user_prompt_uuids:
                return current
            visited.add(current)
            current = msg_parent.get(current, '')
        return start  # fallback: no text prompt found in chain

    # Link each assistant record to its parent user message and root prompt.
    records = []
    for rec in pending:
        parent = user_msgs.get(rec['parent_uuid'], {})
        rec['user_prompt']      = parent.get('text', '')
        rec['permission_mode']  = parent.get('mode', '')
        rec['root_prompt_uuid'] = find_root_prompt(rec['parent_uuid'] or rec['uuid'])
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

    # Feed: group by (slug, root_prompt_uuid) — all assistant responses that trace
    # back to the same original user text prompt belong to one group.
    group_map = defaultdict(list)
    for rec in records:
        key = (rec['slug'], rec['root_prompt_uuid'] or rec['uuid'])
        group_map[key].append(rec)

    feed_groups = []
    for (slug, _puuid), msgs in group_map.items():
        msgs_sorted = sorted(msgs, key=lambda r: r['timestamp'])
        first = msgs_sorted[0]
        # Use the first non-empty user_prompt / permission_mode in the group
        prompt_rec = next((m for m in msgs_sorted if m['user_prompt']), first)
        tools = ', '.join(dict.fromkeys(
            t for m in msgs_sorted for t in (m['tools'].split(', ') if m['tools'] else [])
        ))
        total_in  = sum(m['input_tokens']           for m in msgs_sorted)
        total_out = sum(m['output_tokens']           for m in msgs_sorted)
        total_cw  = sum(m['cache_creation_tokens']   for m in msgs_sorted)
        total_cr  = sum(m['cache_read_tokens']       for m in msgs_sorted)
        feed_groups.append({
            'id':                          first['uuid'],
            'slug':                        slug,
            'timestamp':                   first['timestamp'],
            'model':                       first['model'],
            'user_prompt':                 prompt_rec['user_prompt'],
            'permission_mode':             prompt_rec['permission_mode'],
            'tools':                       tools,
            'total_input_tokens':          total_in,
            'total_output_tokens':         total_out,
            'total_cache_creation_tokens': total_cw,
            'total_cache_read_tokens':     total_cr,
            'total_tokens':                total_in + total_out + total_cw + total_cr,
            'total_lines_added':           sum(m['lines_added']   for m in msgs_sorted),
            'total_lines_removed':         sum(m['lines_removed'] for m in msgs_sorted),
            'message_count':               len(msgs_sorted),
            'messages': [{k: m[k] for k in (
                'uuid', 'timestamp', 'model',
                'input_tokens', 'output_tokens', 'cache_creation_tokens', 'cache_read_tokens',
                'total_tokens', 'content_preview', 'tools', 'lines_added', 'lines_removed',
            )} for m in msgs_sorted],
        })

    feed_groups.sort(key=lambda g: g['timestamp'], reverse=True)

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
        'by_project':  by_project_out,
        'feed_total':  len(feed_groups),
        'feed_groups': feed_groups,
    }
