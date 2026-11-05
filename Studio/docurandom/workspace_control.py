"""Authenticated local maintenance commands, dispatched on the existing UI queue."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from .storage import atomic_write_json


class LocalWorkspaceControl:
    def __init__(self, descriptor: Path, dispatch: Callable[..., str], version: str, *, google_dispatch=None):
        self.descriptor = descriptor
        self.token = secrets.token_urlsafe(32)
        self.instance = secrets.token_hex(16)
        self.dispatch = dispatch
        self.google_dispatch = google_dispatch
        self.lock = threading.Lock()
        self.results: dict[str, tuple[dict, str]] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                if self.client_address[0] != '127.0.0.1' or not secrets.compare_digest(
                    self.headers.get('Authorization', ''), 'Bearer ' + owner.token
                ):
                    self.reply(401, {'error': 'Local authorization required'})
                    return
                if self.path != '/v1/workspace/command':
                    self.reply(404, {'error': 'Unknown maintenance route'})
                    return
                try:
                    self.connection.settimeout(5)
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 2048:
                        raise ValueError('Invalid request size')
                    body = json.loads(self.rfile.read(size))
                    command = body['command']; workspace = body['workspace_id']; request_id = body['request_id']
                    if command not in {'status', 'errors', 'continue', 'skip', 'resume_unlisted', 'google_status', 'google_run', 'google_preview', 'google_diagnostics'} or not re.fullmatch(r'[a-f0-9]{32}', workspace) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
                        raise ValueError('Exact workspace and supported command required')
                    expected_thread = body.get('expected_thread_id', '')
                    if not isinstance(expected_thread, str) or (expected_thread and (command != 'skip'
                            or not re.fullmatch(r'[A-Za-z0-9_-]{8,160}', expected_thread))):
                        raise ValueError('An exact expected thread is supported only for Skip')
                    with owner.lock:
                        previous = owner.results.get(request_id)
                        if previous and previous[0] != body:
                            self.reply(409, {'error': 'Request identity already used'})
                            return
                        if previous is None and len(owner.results) >= 1024:
                            self.reply(429, {'error': 'Maintenance request budget exhausted for this run'})
                            return
                        if previous:
                            result=previous[1]
                        elif command.startswith('google_'):
                            if owner.google_dispatch is None:raise ValueError('Google service unavailable')
                            result=owner.google_dispatch(command,workspace)
                        else:
                            result=(owner.dispatch(command,workspace,expected_thread)
                                    if expected_thread else owner.dispatch(command,workspace))
                        owner.results[request_id] = (body, result)
                    self.reply(200, {'response': result, 'request_id': request_id})
                except (ValueError, KeyError, TypeError, OSError):
                    self.reply(400, {'error': 'Invalid maintenance request'})
                except Exception:
                    self.reply(500, {'error': 'Maintenance command failed; inspect local log'})

            def reply(self, status, value):
                data = json.dumps(value, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name='WorkspaceLocalControl', daemon=True)
        self.thread.start()
        atomic_write_json(descriptor, {'endpoint': f'http://127.0.0.1:{self.server.server_port}',
            'token': self.token, 'pid': os.getpid(), 'version': version, 'instance': self.instance})

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        try:
            current = json.loads(self.descriptor.read_text(encoding='utf-8'))
            if current.get('instance') == self.instance:
                self.descriptor.unlink()
        except (OSError, ValueError):
            pass
