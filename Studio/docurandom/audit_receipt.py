"""Retain exact voice audit requests/responses across pipeline restarts."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
from .storage import atomic_write_json


def send_voice_audit(chat, root: Path, prompt: str, attachment, *, cancel, on_status):
    key = hashlib.sha256(prompt.encode('utf-8') + b'\0' + attachment.data).hexdigest()
    path = root / '.voice-audit-requests' / (key + '.json')
    try:
        row = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        row = {'version': 1, 'key': key, 'thread_id': chat.thread_id, 'status': 'reserved', 'prompt': prompt}
    if row.get('key') != key or row.get('thread_id') != chat.thread_id:
        raise ValueError('Voice audit receipt identity mismatch; keep checkpoint')
    if isinstance(row.get('response'), str):
        return row['response']
    try:
        if row['status'] in {'submitted', 'uncertain'}:
            response = chat.recover_pending(cancel_event=cancel, output_mode='voicevox_audit_json', on_status=on_status)
            if not isinstance(response, str):
                raise RuntimeError('CHATGPT_RECOVERY_PENDING: exact voice audit response unavailable; do not resubmit')
        else:
            row['status'] = 'submitted'
            atomic_write_json(path, row)
            response = chat.send(prompt, cancel_event=cancel, attachments=(attachment,), on_status=on_status,
                                 output_mode='voicevox_audit_json')
        row.update(response=response, response_id=chat.response_id, status='response_received')
        return response
    except Exception as exc:
        row.update(status='uncertain', detail=str(exc))
        raise
    finally:
        row['updated_at'] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(path, row)
