"""Offline, durable links for exact thumbnail threads; no prompt or credential data."""
import hashlib,json,logging,re
from pathlib import Path
from threading import RLock
from .storage import atomic_write_json,atomic_write_text

_INDEX_LOCK=RLock()

def saved_conversation_url(bridge_state_path: Path,thread_id: str)->str:
    try:
        state=json.loads(bridge_state_path.read_text(encoding='utf-8-sig'))
        value=str((state.get('savedTurns',{}).get('thread:'+thread_id)or{}).get('chatUrl')or'')
    except(OSError,ValueError,TypeError):value=''
    pattern=r'https://chatgpt\.com/c/[a-f0-9-]{36}'
    if re.fullmatch(pattern,value):return value
    result=''
    try:
        with bridge_state_path.with_name('conversation-links.jsonl').open(encoding='utf-8')as stream:
            for line in stream:
                try:row=json.loads(line)
                except ValueError:continue
                if not isinstance(row,dict):continue
                if row.get('thread_id')==thread_id and re.fullmatch(pattern,str(row.get('url')or'')):result=row['url']
    except OSError:pass
    return result

def retain_thumbnail_link(workspace,state,bridge_state_path):
    thread=str(state.get('thread_id')or'')
    url=saved_conversation_url(bridge_state_path,thread)if thread else ''
    if not url and not state.get('isolated_chat'):return
    if url:state['conversation_url']=url
    else:state.pop('conversation_url',None)
    row={'workspace':workspace.name,'workspace_id':workspace.workspace_id,
         'package_path':state['package_path'],'package_sha256':state['package_sha256'],
         'prompt_sha256':state['prompt_sha256'],'thread_id':thread,'url':url,
         'recorded_at':state['updated_at'],'observed_at':state['updated_at'],
         'status':state.get('status')or'', 'thumbnail_path':state.get('thumbnail_path')or'',
         'dedicated_chat':bool(state.get('isolated_chat')),
         'content_thread_id':(state.get('isolated_chat')or{}).get('content_thread_id')or''}
    package=Path(state['package_path'])
    row['package_name']=package.name
    row['video_label']=package.stem
    try:
        if hashlib.sha256(package.read_bytes()).hexdigest()==state['package_sha256']:
            from .youtube_upload import parse_content_package
            row['video_label']=parse_content_package(package).title or package.stem
    except (OSError,ValueError,RuntimeError):pass
    with _INDEX_LOCK:
        if url:_retain_local_thumbnail_link(workspace,row)
        try:_update_thumbnail_index(row,bridge_state_path)
        except OSError:
            logging.getLogger(__name__).warning('Could not refresh thumbnail URL index; per-workspace URL retained.',exc_info=True)


def _retain_local_thumbnail_link(workspace,row):
    path=workspace.state_dir/'thumbnail-conversations.json'
    try:
        value=json.loads(path.read_text(encoding='utf-8'));records=value.get('records',[])
        if not isinstance(records,list):records=[]
    except(OSError,ValueError,TypeError):records=[]
    key=tuple(row[k]for k in ['package_sha256','prompt_sha256','thread_id','url'])
    old=next((r for r in records if isinstance(r,dict) and tuple(r.get(k)for k in ['package_sha256','prompt_sha256','thread_id','url'])==key),None)
    if old is None:records.append(dict(row))
    else:old.update(row)
    atomic_write_json(path,{'version':1,'workspace_id':workspace.workspace_id,'records':records})


def _update_thumbnail_index(row,bridge_state_path):
    data_root=bridge_state_path.parent.parent if bridge_state_path.parent.name=='chat-bridge'else bridge_state_path.parent
    output=data_root/'reports';path=output/'thumbnail-chat-links.json'
    try:
        report=json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(report,dict):report={}
    except (OSError,ValueError):report={}
    current=report.get('current')or[];history=report.get('history')or[]
    if not isinstance(current,list):current=[]
    if not isinstance(history,list):history=[]
    current=[r for r in current if isinstance(r,dict) and r.get('workspace_id')!=row['workspace_id']]
    current.append(dict(row))
    if row['url']:
        key=tuple(row[k]for k in ['workspace_id','package_sha256','prompt_sha256','thread_id','url'])
        old=next((r for r in history if isinstance(r,dict) and tuple(r.get(k)for k in ['workspace_id','package_sha256','prompt_sha256','thread_id','url'])==key),None)
        if old is None:history.append(dict(row))
        else:old.update(row)
    report.update(snapshot_at=row['observed_at'],kind='runtime-thumbnail-chat-index',
        future_auto_export_enabled=True,runtime_auto_retention_enabled=True,current=current,history=history)
    atomic_write_json(path,report)
    def cell(value):return str(value or'').replace('|','\\|').replace('\r',' ').replace('\n',' ')
    lines=['# Chat thumbnail theo video','','Bảng tự cập nhật khi Studio lưu trạng thái/URL thumbnail. Thời điểm: '+row['observed_at'],'',
        '| Workspace | Video / tiêu đề | File Tổng hợp | Trạng thái | Chat thumbnail |',
        '|---|---|---|---|---|']
    for r in sorted(current,key=lambda r:str(r.get('workspace')or'')):
        link='[Mở chat]('+r['url']+')'if r.get('url')else'Đã dành chat; chờ URL thật'
        lines.append('| '+cell(r.get('workspace'))+' | '+cell(r.get('video_label')or Path(r.get('package_path')or'').stem)+' | '+cell(r.get('package_name')or Path(r.get('package_path')or'').name)+' | '+cell(r.get('status'))+' | '+link+' |')
    lines+=['','## Lịch sử thumbnail','','| Workspace | Video / file Tổng hợp | SHA-256 gói | Chat |','|---|---|---|---|']
    for r in history:
        if not isinstance(r,dict)or not r.get('url'):continue
        lines.append('| '+cell(r.get('workspace'))+' | '+cell(r.get('video_label')or Path(r.get('package_path')or'').name)+' | '+cell(r.get('package_sha256'))+' | [Mở chat]('+r['url']+') |')
    atomic_write_text(output/'THUMBNAIL-CHAT-LINKS.md','\n'.join(lines)+'\n',encoding='utf-8')
