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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from pipeline import compute_data

# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

_HTML = (Path(__file__).parent / 'dashboard.html').read_text(encoding='utf-8')


def make_handler(claude_dir):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)

            if parsed.path == '/':
                body = _HTML.encode('utf-8')
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
