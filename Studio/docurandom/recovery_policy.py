"""Durable recovery decisions. Unknown delivery never grants permission to resend."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
import threading
from functools import wraps

from .storage import atomic_write_json

_LOCK = threading.RLock()


def serialized(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with _LOCK:
            return fn(*args, **kwargs)
    return call


class Outcome(StrEnum):
    AUTH_REQUIRED = 'auth_required'
    RATE_LIMITED = 'rate_limited'
    PROCESSING = 'processing'
    UNCERTAIN = 'uncertain'
    INVALID_RESPONSE = 'invalid_response'
    FAILED = 'failed'
    COMPLETE = 'complete'


def classify(detail: object) -> Outcome:
    text = str(detail).casefold()
    if any(s in text for s in ('accounts.google.com', 'mất phiên đăng nhập youtube', 'authentication-required', 'http 401')):
        return Outcome.AUTH_REQUIRED
    if any(s in text for s in ('http 429', 'http=429', 'chatgpt_rate_limited', 'rate-limited')):
        return Outcome.RATE_LIMITED
    if any(s in text for s in ('schema', 'jsondecodeerror', 'sai cấu trúc')):
        return Outcome.INVALID_RESPONSE
    if any(s in text for s in ('đang xử lý', 'streaming=true')):
        return Outcome.PROCESSING
    if any(s in text for s in ('pending', 'uncertain', 'chưa xác nhận', 'chưa thể xác minh', 'probe dom', 'timeout', 'timed out')):
        return Outcome.UNCERTAIN
    return Outcome.FAILED


def job_key(workspace: str, video: str, stage: str, request: str) -> str:
    return hashlib.sha256(json.dumps([workspace, video, stage, request], ensure_ascii=False).encode()).hexdigest()


class RecoveryJournal:
    DELAYS = (120, 300, 900)

    def __init__(self, state_dir: Path):
        self.path = state_dir / 'recovery-policy.json'

    def load(self):
        try:
            value = json.loads(self.path.read_text(encoding='utf-8-sig'))
            if value.get('version') != 1 or not isinstance(value.get('jobs'), dict):
                raise ValueError('Recovery journal schema mismatch')
            return value
        except FileNotFoundError:
            return {'version': 1, 'jobs': {}}

    @serialized
    def record(self, key, outcome, detail='', *, now=None):
        clock = now or datetime.now(timezone.utc)
        data = self.load()
        old = data['jobs'].get(key, {})
        row = dict(old, outcome=str(outcome), detail=str(detail), updated_at=clock.isoformat())
        row.setdefault('attempts', 0)
        row.setdefault('next_probe_at', (clock + timedelta(seconds=self.DELAYS[0])).isoformat())
        if outcome == Outcome.RATE_LIMITED and not old:
            match = re.search(r'retry-after(?:-seconds)?[=: ]+(\d+)', str(detail), re.I)
            row['next_probe_at'] = (clock + timedelta(seconds=int(match[1]) if match else 120)).isoformat()
        data['jobs'][key] = row
        atomic_write_json(self.path, data)
        return old.get('outcome') != row['outcome']

    @serialized
    def reserve_probe(self, key, *, now=None):
        clock = now or datetime.now(timezone.utc)
        data = self.load()
        row = data['jobs'][key]
        if row['outcome'] in {Outcome.AUTH_REQUIRED, Outcome.COMPLETE}:
            return 'blocked'
        count = row['attempts']
        if type(count) is not int or count < 0 or count >= len(self.DELAYS):
            return 'exhausted'
        if clock < datetime.fromisoformat(row['next_probe_at']):
            return 'wait'
        row.update(attempts=count + 1, next_probe_at=(clock + timedelta(seconds=self.DELAYS[min(count + 1, 2)])).isoformat())
        atomic_write_json(self.path, data)
        return 'reserved'


def youtube_auth_blocked(state_dir: Path, profile_id: str) -> bool:
    try:
        row = json.loads((state_dir / 'youtube-auth-required.json').read_text(encoding='utf-8-sig'))
        return row.get('profile_id') == profile_id and row.get('status') == 'auth_required'
    except FileNotFoundError:
        return False
    except (ValueError, OSError):
        return True  # Corrupt barriers cannot silently authorize automation.


def hold_youtube_auth(state_dir: Path, profile_id: str, detail: str) -> bool:
    changed = not youtube_auth_blocked(state_dir, profile_id)
    atomic_write_json(state_dir / 'youtube-auth-required.json', {
        'version': 1, 'status': 'auth_required', 'profile_id': profile_id,
        'detail': detail, 'updated_at': datetime.now(timezone.utc).isoformat(),
    })
    return changed


def clear_youtube_auth(state_dir: Path, profile_id: str) -> None:
    if youtube_auth_blocked(state_dir, profile_id):
        atomic_write_json(state_dir / 'youtube-auth-required.json', {
            'version': 1, 'status': 'verified', 'profile_id': profile_id,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })


def request_youtube_auth_probe(state_dir: Path) -> None:
    path = state_dir / 'youtube-auth-required.json'
    try:
        row = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return
    if row.get('status') == 'auth_required':
        row.update(status='probe_requested', updated_at=datetime.now(timezone.utc).isoformat())
        atomic_write_json(path, row)
