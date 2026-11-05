"""Persistent notice deduplication; full exception records remain in diagnostic logs."""
from __future__ import annotations
import hashlib,json
from pathlib import Path
from .storage import atomic_write_json

RECOVERY_NOTICE_SECONDS = 12 * 60 * 60
MANUAL_NOTICE_SECONDS = 15 * 60

def failure_notice_key(context: str, code: str, error: object, recovering: bool, identity: list[object]) -> str:
    detail=str(error)
    folded=detail.casefold()
    if recovering and any(marker in folded for marker in ('http 429','http=429','chatgpt_rate_limit','connection=rate-limited')):
        family='chatgpt-rate-limit'
    elif recovering and any(marker in folded for marker in ('authentication-required','access-rejected','http=401','http=403')):
        family='chatgpt-authentication'
    elif recovering and any(marker in folded for marker in ('chatgpt_recovery_pending','chatgpt_image_recovery_pending',
            'chatgpt_conversation_load_failed','chatgpt_pending_submission_locked','không tạo dom phản hồi',
            'không phản hồi probe dom','chưa thể xác minh là đã dừng','chưa có ảnh thumbnail trong đúng lượt',
            'lượt tạo ảnh trong đúng hội thoại vẫn chưa xác nhận hoàn tất',
            'hội thoại cũ chưa rảnh để','chưa có bằng chứng nguồn cho phép chat thumbnail phục hồi')):
        family='chatgpt-unconfirmed-turn'
    else:
        family=code+':'+detail
    return hashlib.sha256(json.dumps([context,family,bool(recovering),*identity],ensure_ascii=False).encode()).hexdigest()

def reserve_failure_notice(notices: dict[str,float], key: str, *, now: float, recovering: bool,
                           journal: Path | None = None) -> tuple[bool,dict[str,float]]:
    stored=dict(notices)
    if journal is not None:
        try:
            saved=json.loads(journal.read_text(encoding='utf-8'))
            values=saved.get('notices',{}) if isinstance(saved,dict) else {}
            if isinstance(values,dict):stored.update({k:float(v) for k,v in values.items() if isinstance(v,(int,float))})
        except (OSError,ValueError,TypeError):pass
    stored={k:v for k,v in stored.items() if isinstance(v,(int,float)) and 0<=now-v<RECOVERY_NOTICE_SECONDS}
    ttl=RECOVERY_NOTICE_SECONDS if recovering else MANUAL_NOTICE_SECONDS
    if key in stored and 0<=now-stored[key]<ttl:return False,stored
    stored[key]=now
    stored=dict(sorted(stored.items(),key=lambda item:item[1])[-128:])
    if journal is not None:
        try:atomic_write_json(journal,{'version':1,'notices':stored})
        except OSError:pass
    return True,stored
