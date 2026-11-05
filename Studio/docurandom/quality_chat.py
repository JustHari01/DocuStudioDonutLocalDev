"""Durable bounded review jobs through the operator's selected ChatGPT Web model."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import threading
import unicodedata
import urllib.request
from urllib.parse import urlsplit

from .channel_learning import digest, now_iso
from .content_ai import ChatGPTWebChat, check_chatgpt_web_bridge, resolve_chatgpt_web_model, CHATGPT_WEB_DEFAULT_MODEL_LABEL
from .conversation_links import saved_conversation_url
from .storage import app_data_dir, atomic_write_json


class CompletedReviewInvalid(ValueError):
    """Exact provider turn ended; its empty output is not pending work."""
    def __init__(self, proof):
        super().__init__('Exact review ended without a JSON text response')
        self.source_proof = proof


def review_retry_delay(detail, *, now=None):
    """Persisted provider/read budget also gates UI callbacks after restart."""
    import re, math
    from datetime import datetime, timezone
    match = re.search(r'quality review (wait|exhausted|blocked); next-probe-at=([^; ]+)',str(detail))
    if not match:return None
    if match[1] != 'wait':return -1
    try:
        due=datetime.fromisoformat(match[2])
        return max(0,math.ceil((due-(now or datetime.now(timezone.utc))).total_seconds()))
    except (ValueError,TypeError,OverflowError):return -1


def exact_text_response(status,thread,prompt):
    parsed=urlsplit(status.endpoint)
    body={'thread_id':thread,'expected_prompt':prompt,
          'prompt_sha256':hashlib.sha256(' '.join(unicodedata.normalize('NFC',prompt).split()).encode()).hexdigest()}
    req=urllib.request.Request(f'{parsed.scheme}://{parsed.netloc}/v1/session/exact-text',data=json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+status.access_token,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=18)as handle:bound=json.load(handle)
    if bound.get('status')=='completed-invalid' and bound.get('source_proof',{}).get('complete'):
        raise CompletedReviewInvalid(bound['source_proof'])
    if bound.get('status')!='completed':raise RuntimeError('Exact review response pending; do not resubmit')
    return bound['output_text'],bound['source_proof']


def ask_json(root: Path, prompt: str, *, attachments=(), cancel=None, timeout=240, pool=None, thread_id=None,
             allow_format_repair=True, validate=None):
    cancel = cancel or threading.Event()
    output_mode = "verified_json"
    key = digest([prompt, output_mode, thread_id, [(a.name, hashlib.sha256(a.data).hexdigest()) for a in attachments]])
    path = root / (key + ".json")
    legacy_mode="thumbnail_confirmation" if any(a.mime_type.startswith("image/") for a in attachments) else ""
    legacy_path=root/(digest([prompt,legacy_mode,thread_id,[(a.name,hashlib.sha256(a.data).hexdigest())for a in attachments]])+'.json')
    if not path.exists()and legacy_path.exists():path=legacy_path
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if old.get("status") == "complete":
        try:
            if validate: validate(old['result'])
            return old['result']
        except ValueError:
            old['status']='invalid_response'
    status = check_chatgpt_web_bridge(auto_start=False, timeout=10)
    if not status.operational: raise RuntimeError("Bridge unavailable; exact review request retained")
    thread = thread_id or "thread_docurandom_" + key[:32]
    chat = ChatGPTWebChat(status.endpoint, resolve_chatgpt_web_model(CHATGPT_WEB_DEFAULT_MODEL_LABEL, status.available_models),
                         root, status.access_token, thread_id=thread)
    record = old or {"request": prompt, "request_sha256": key, "thread_id": thread,
                     "created_at": now_iso(), "status": "reserved"}
    rejection = 'Previous ChatGPT attachment is unresolved in its exact conversation; no new file or prompt was sent'
    if rejection in str(record.get('detail', '')) and record.get('status') == 'uncertain':
        # This exact Bridge 409 is emitted before staging or sending a prompt.
        # Keep the same reserved repair request, not a fresh retry budget.
        record.update(status='reserved', pre_submit_rejected=True)
        old = record
    acquired = False; timer = None
    def repair_json():
        # One durable follow-up, only after a source-bound completed response.
        # Restart resumes this child request; it cannot replenish its budget.
        if not allow_format_repair:
            raise ValueError('QUALITY_JSON_REPAIR_EXHAUSTED: completed response is still invalid')
        repair = record.get('format_repair')
        if not repair:
            proof = record.get('source_proof') or {}
            if proof.get('complete') is not True:
                raise ValueError('Cannot repair JSON without exact completed source evidence')
            repair_prompt = ('[DOCURANDOM_STAGE=quality-json-format-repair]\n'
                'The previous request ended with an empty or invalid JSON response. '
                'Return only the JSON object required by that request. Do not call tools, '
                'generate/edit images, change dialogue, or invent missing observations. '
                'For an image audit, inspect only the image already attached to the previous request; '
                'if it cannot be inspected, return failed/unknown fields and explain in issues. '
                'Original request SHA256: '+hashlib.sha256(prompt.encode()).hexdigest()+
                '\nOriginal audit request (data and required schema):\n'+prompt)
            repair = {'count': 1, 'thread_id': thread, 'request': repair_prompt,
                      'original_source_proof': proof, 'reserved_at': now_iso()}
            record['format_repair'] = repair
            record['status'] = 'repairing_json'
            atomic_write_json(path, record)
        if repair.get('count') != 1 or repair.get('thread_id') != thread:
            raise ValueError('Invalid durable JSON repair identity')
        if timer: timer.cancel()
        result = ask_json(root, repair['request'], cancel=cancel, timeout=timeout,
                          thread_id=thread, allow_format_repair=False, validate=validate)
        repair['completed_at'] = now_iso()
        record.update(status='complete', result=result)
        return result
    try:
        if pool:
            acquired = pool.acquire(cancel_event=cancel, label="Quality review", on_wait=lambda *_: None)
            if not acquired: raise InterruptedError("Quality review cancelled in queue")
        timer = threading.Timer(timeout, chat.cancel); timer.daemon = True; timer.start()
        if record.get('format_repair'):
            return repair_json()
        def exact_text():
            text,record['source_proof']=exact_text_response(status,record['thread_id'],prompt)
            return text
        if old.get("status") == 'uncertain':
            from .recovery_policy import RecoveryJournal, classify
            journal = RecoveryJournal(root)
            journal.record(key, classify(old.get('detail', 'uncertain')), old.get('detail', ''))
            # One source-only migration read can recognize ended empty finals.
            # It neither resets the existing retry journal nor sends a prompt.
            if record.get('source_reader_version', 0) < 2:
                record['source_reader_version'] = 2
                atomic_write_json(path, record)
                decision = 'reserved'
            else:
                decision = journal.reserve_probe(key)
            if decision != 'reserved':
                due=journal.load()['jobs'][key]['next_probe_at']
                raise RuntimeError('CHATGPT_RECOVERY_PENDING: quality review ' + decision + '; next-probe-at='+due+'; preserve exact receipt')
        if old.get("response")or old.get("status")in {'submitted','uncertain','invalid_response'}:
            response=exact_text()
        else:
            record["status"] = "submitted"; atomic_write_json(path, record)
            response = chat.send(prompt, attachments=attachments, cancel_event=cancel, output_mode=output_mode)
            response=exact_text()
        if old.get('response')and old['response']!=response:
            record.setdefault('rejected_unbound_responses',[]).append(old['response'])
        record["response"] = response
        raw = response.strip()
        if raw.startswith("```"): raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            result = json.loads(raw)
            if not isinstance(result, dict): raise ValueError("JSON object required")
            if validate: validate(result)
        except ValueError:
            return repair_json()
        record.update(status="complete", result=result)
        return result
    except CompletedReviewInvalid as exc:
        record['source_proof'] = exc.source_proof
        record['status'] = 'invalid_response'
        try:
            return repair_json()
        except Exception as repair_error:
            record['detail'] = f'{type(repair_error).__name__}: {repair_error}'
            raise
    except Exception as exc:
        if rejection in str(exc):
            record.update(status='reserved', pre_submit_rejected=True)
        elif record["status"] != "reserved": record["status"] = "uncertain"
        record["detail"] = f"{type(exc).__name__}: {exc}"
        from .content_ai import ContentAICancelled
        if isinstance(exc, ContentAICancelled) and not cancel.is_set():
            # A local review watchdog is not a user cancellation. Preserve the
            # submitted receipt and wake bounded recovery of the saved image.
            raise RuntimeError('CHATGPT_RECOVERY_PENDING: quality review local timeout; retain exact receipt, do not resubmit') from exc
        raise
    finally:
        if timer: timer.cancel()
        record.update(conversation_url=saved_conversation_url(app_data_dir() / "chat-bridge/bridge-state.json", thread), updated_at=now_iso())
        atomic_write_json(path, record)
        if acquired: pool.release()
