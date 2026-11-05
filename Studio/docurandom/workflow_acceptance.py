"""Read-only evidence for an authorized A Public -> B Unlisted -> C production trial."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
from .channel_learning import now_iso
from .storage import atomic_write_json


def _read(path):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def observe(workspace):
    path=workspace.state_dir/'workflow-acceptance.json'
    if not path.is_file():return None
    record=_read(path)
    if record.get('workspace_id')!=workspace.workspace_id:raise ValueError('Acceptance workspace mismatch')
    if record.get('status')=='complete':return record
    evidence=record.setdefault('evidence',{})
    sessions=_read(workspace.state_dir/'uploaded_sessions.json').get('sessions',{})
    a=sessions.get(record['a_identity'],{})
    if a.get('visibility_status')=='PUBLIC' and a.get('published_at'):
        evidence.setdefault('a_public',{'youtube_url':a['youtube_url'],'published_at':a['published_at'],
                                       'source':'uploaded_sessions.json','identity':record['a_identity']})
    queue=_read(workspace.state_dir/'deferred_public_queue.json').get('items',[])
    if 'a_public' in evidence:
        for item in queue:
            if item.get('identity')==record['a_identity']:continue
            package=Path(item.get('package_path',''))
            if package.is_file() and hashlib.sha256(package.read_bytes()).hexdigest()==record['b_package_sha256']:
                if datetime.fromisoformat(item['queued_at'])>=datetime.fromisoformat(evidence['a_public']['published_at']):
                    evidence.setdefault('b_unlisted',{'youtube_url':item['youtube_url'],'queued_at':item['queued_at'],
                        'identity':item['identity'],'package_sha256':record['b_package_sha256'],
                        'source':'deferred_public_queue.json'})
    checkpoint=_read(workspace.state_dir/'pipeline_checkpoint.json')
    chat=checkpoint.get('stages',{}).get('chatgpt',{})
    thread=chat.get('artifacts',{}).get('thread_id')
    if ('b_unlisted' in evidence and thread and thread!=record['b_content_thread_id']
            and checkpoint.get('created_at')
            and datetime.fromisoformat(checkpoint['created_at'])>=datetime.fromisoformat(evidence['b_unlisted']['queued_at'])):
        evidence.setdefault('c_started',{'thread_id':thread,'created_at':checkpoint['created_at'],
                                         'stage_status':chat.get('status'),'source':'pipeline_checkpoint.json'})
    record['status']='complete' if all(k in evidence for k in ('a_public','b_unlisted','c_started')) else 'waiting_for_scheduled_evidence'
    record['checked_at']=now_iso()
    atomic_write_json(path,record)
    return record
