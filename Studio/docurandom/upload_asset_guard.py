"""Hold an unchanged incomplete upload without restarting browser workers."""
from __future__ import annotations
import hashlib,json
from pathlib import Path
from .storage import atomic_write_json

FILENAME='youtube-asset-wait.json'

def _read(path):
    try:
        value=json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value,dict) else {}
    except(OSError,ValueError):return {}

def fingerprint(paths):
    values=[]
    for key in ('video_path','package_path','thumbnail_path'):
        value=str(paths.get(key)or'')
        path=Path(value).resolve()if value else None
        try:
            stat=path.stat()if path else None
            signature=[str(path),stat.st_size,stat.st_mtime_ns]if stat else ['',None]
            if path and key=='package_path':
                signature.append(hashlib.sha256(path.read_bytes()).hexdigest())
                supplement = path.with_suffix('.metadata-repair.json')
                if supplement.is_file():signature.append(hashlib.sha256(supplement.read_bytes()).hexdigest())
        except OSError:signature=[str(path),'missing']
        values.append([key,signature])
    return hashlib.sha256(json.dumps(values,ensure_ascii=False).encode()).hexdigest()

def record_asset_wait(workspace,sources,message):
    paths={k:str(getattr(sources,k)or'')for k in ('video_path','package_path','thumbnail_path')}
    value={'version':1,'status':'waiting_for_assets','workspace_id':workspace.workspace_id,
           'paths':paths,'fingerprint':fingerprint(paths),'detail':str(message)}
    atomic_write_json(workspace.state_dir/FILENAME,value)
    return value

def current_asset_wait(workspace,video_path=None,thumbnail_path=...):
    if workspace is None:return None
    value=_read(workspace.state_dir/FILENAME)
    if value.get('status')!='waiting_for_assets' or value.get('workspace_id')!=workspace.workspace_id:return None
    paths=value.get('paths')or{}
    if video_path is not None and Path(video_path).resolve()!=Path(str(paths.get('video_path')or'')).resolve():return None
    cp=_read(workspace.pipeline_checkpoint_path)
    current_package=cp.get('stages',{}).get('package',{}).get('artifacts',{}).get('path')
    changed=(current_package and Path(current_package).resolve()!=Path(str(paths.get('package_path')or'')).resolve())
    if thumbnail_path is not ...:
        changed=changed or str(thumbnail_path or '')!=str(paths.get('thumbnail_path')or'')
    if changed or value.get('fingerprint')!=fingerprint(paths):
        atomic_write_json(workspace.state_dir/FILENAME,{**value,'status':'assets_changed'})
        return None
    return value
