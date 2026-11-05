"""Bound consecutive restarts after an explicit provider conversation limit."""
from __future__ import annotations
import hashlib,re
from datetime import datetime,timezone
from .storage import atomic_write_json
from .workspace import Workspace
import json

def _read(workspace):
    try:
        value=json.loads((workspace.state_dir/'conversation-capacity-recovery.json').read_text(encoding='utf-8'))
        return value if isinstance(value,dict)else{}
    except(OSError,ValueError):return {}

def reserve_full_conversation_recovery(workspace:Workspace,master_prompt:str,thread_id:str)->bool:
    if not master_prompt.strip()or not re.fullmatch(r'[A-Za-z0-9_-]{8,160}',thread_id):return False
    previous=_read(workspace)
    digest=hashlib.sha256(master_prompt.encode('utf-8')).hexdigest()
    if previous.get('master_sha256')==digest and previous.get('status')=='reserved':return False
    record={'version':1,'master_sha256':digest,'thread_id':thread_id,'status':'reserved',
        'reserved_at':datetime.now(timezone.utc).isoformat(),
        'previous':[{k:v for k,v in previous.items()if k!='previous'},*(previous.get('previous')or[])][:16]if previous else[]}
    atomic_write_json(workspace.state_dir/'conversation-capacity-recovery.json',record)
    return True

def complete_full_conversation_recovery(workspace:Workspace,thread_id:str)->None:
    previous=_read(workspace)
    if previous.get('status')!='reserved' or previous.get('thread_id')==thread_id:return
    previous.update(status='content_completed',completed_thread_id=thread_id,
        completed_at=datetime.now(timezone.utc).isoformat())
    atomic_write_json(workspace.state_dir/'conversation-capacity-recovery.json',previous)
