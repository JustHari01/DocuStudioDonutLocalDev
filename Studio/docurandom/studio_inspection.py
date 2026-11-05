"""Owned-tab read-only Studio inspection with durable per-video cursors."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

from .channel_analytics import ax_texts, parse_number
from .channel_learning import CHANNEL_RE, now_iso
from .donut_mcp import DonutMcpClient
from .youtube_like_monitor import _read_debug_targets
from .youtube_upload import _CdpSession, ProfileUploadCoordinator
from .storage import atomic_write_json


class StudioInspector:
    def __init__(self, endpoint, profile_id, channel_id, *, coordinator=None, cancel=None):
        if not CHANNEL_RE.fullmatch(channel_id): raise ValueError("Exact channel required")
        self.endpoint, self.profile_id, self.channel_id = endpoint, profile_id, channel_id
        self.coordinator = coordinator or ProfileUploadCoordinator()
        self.cancel = cancel or threading.Event()
        self.started = False; self.created = []

    def __enter__(self):
        self.lease = self.coordinator.try_acquire(self.profile_id, self.cancel, job_kind="monitor", priority=0)
        if self.lease is None: raise RuntimeError("Profile reserved; defer analytics")
        self.lease.__enter__()
        try:
            self.client = DonutMcpClient(self.endpoint, timeout=15, shared_session=False)
            self.client.__enter__(); self.client.require_docurandom_local()
            if not self.client.get_profile(self.profile_id).process_id:
                self.client.run_profile(self.profile_id, "about:blank"); self.started = True
            return self
        except Exception:
            if hasattr(self,'client'):self.client.__exit__(None,None,None)
            self.lease.__exit__(None,None,None); raise

    def __exit__(self,*args):
        try:
            for target in self.created:
                try:self.client.cdp_command(self.profile_id,"Target.closeTarget",{"targetId":target})
                except Exception:pass
            if self.started:
                pages=[t for t in _read_debug_targets(self.client.get_debug_connection(self.profile_id)) if t.get('type')=='page' and t.get('url')not in {'about:blank','chrome://newtab/'}]
                if not pages:self.client.call_tool('batch_stop_profiles',{'profile_ids':[self.profile_id]},timeout=15)
        finally:
            self.client.__exit__(*args);self.lease.__exit__(*args)

    @contextmanager
    def page(self, url, *, timeout=20, tab_labels=()):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname!='studio.youtube.com':raise ValueError('Studio URL required')
        target_id=self.client.cdp_command(self.profile_id,'Target.createTarget',{'url':url})['targetId']
        self.created.append(target_id)
        deadline=time.monotonic()+timeout
        try:
            while True:
                targets=_read_debug_targets(self.client.get_debug_connection(self.profile_id))
                target=next((t for t in targets if t.get('id')==target_id),None)
                if target:break
                if self.cancel.wait(.25) or time.monotonic()>deadline:raise TimeoutError('Owned target unavailable')
            with _CdpSession(target['webSocketDebuggerUrl']) as cdp:
                started=time.monotonic()
                while time.monotonic()<deadline and not self.cancel.is_set():
                    frame=cdp.command('Page.getFrameTree',timeout=5)['frameTree']['frame']
                    actual=frame.get('url','')
                    if 'accounts.google.com' in actual:raise RuntimeError('YouTube login required')
                    if actual.startswith('https://studio.youtube.com/channel/') and self.channel_id not in actual:
                        raise ValueError('Wrong channel; no analytics accepted')
                    nodes=cdp.command('Accessibility.getFullAXTree',timeout=5).get('nodes',[])
                    rows=ax_texts(nodes)
                    if time.monotonic()-started>=3 and len(rows)>=20:
                        if tab_labels:
                            match=[n for n in nodes if n.get('role',{}).get('value')=='tab'
                                   and str(n.get('name',{}).get('value','')).strip()in tab_labels and not n.get('ignored')]
                            if len(match)!=1:raise ValueError('Exact analytics tab not available: '+json.dumps([(n.get('name'),n.get('ignored'))for n in nodes if n.get('role',{}).get('value')=='tab'],ensure_ascii=False))
                            box=cdp.command('DOM.getBoxModel',{'backendNodeId':match[0]['backendDOMNodeId']},timeout=5)['model']['content']
                            x,y=sum(box[0::2])/4,sum(box[1::2])/4
                            for typ in ('mousePressed','mouseReleased'):
                                cdp.command('Input.dispatchMouseEvent',{'type':typ,'x':x,'y':y,'button':'left','clickCount':1},timeout=5)
                            tab_labels=();started=time.monotonic()
                            continue
                        yield cdp,{'url':actual,'observations':rows,'collected_at':now_iso(),'channel_id':self.channel_id},nodes
                        return
                    if self.cancel.wait(.5):break
                raise TimeoutError('Studio report not ready; keep cursor: '+json.dumps(
                    {'url':actual,'node_count':len(nodes),'row_count':len(rows),
                     'labels':[str(n.get('name',{}).get('value',''))[:90]for n in nodes if n.get('role',{}).get('value')in {'heading','StaticText'}][:16]},ensure_ascii=False))
        finally:
            try:
                self.client.cdp_command(self.profile_id,'Target.closeTarget',{'targetId':target_id})
            except Exception:
                # A closed browser must not mask the original read failure.
                # __exit__ retries cleanup of this exact owned target.
                pass
            else:
                self.created.remove(target_id)


def video_ids(page:dict)->list[str]:
    result=[]
    for row in page['observations']:
        m=re.search(r'https://studio\.youtube\.com/video/([A-Za-z0-9_-]{11})/',row.get('url',''))
        if m and m[1] not in result:result.append(m[1])
    return result


def select_seven_days(cdp,cancel):
    """Use only the date picker observed in Studio; never invent a URL range."""
    def tree():return cdp.command('Accessibility.getFullAXTree',timeout=5).get('nodes',[])
    def name(n):return str(n.get('name',{}).get('value','')).strip()
    from .native_ab import click
    ns=tree()
    labels=('Từ khi xuất bản','Since published','公開後','ngày qua','Last ','過去 ')
    buttons=[n for n in ns if not n.get('ignored')and n.get('role',{}).get('value')=='button'
             and any(t in name(n)for t in labels)]
    # Studio exposes a parent button and its nested button with the same bounds.
    # Accept that one geometric control, never two independent date controls.
    visible=[]
    for n in buttons:
        try:
            model=cdp.command('DOM.getBoxModel',{'backendNodeId':n['backendDOMNodeId']},timeout=3)['model']
            if model['width']>0 and model['height']>0:visible.append((n,model['border']))
        except Exception:continue
    if not visible or len({tuple(round(v,1)for v in b)for n,b in visible})!=1:
        raise ValueError('One exact date range control required')
    click(cdp,visible[-1][0])
    deadline=time.monotonic()+8
    while True:
        options=[n for n in tree()if not n.get('ignored')and n.get('role',{}).get('value')=='option'
                 and name(n)in {'7 ngày qua','Last 7 days','過去 7 日間'}]
        if len(options)==1:break
        if len(options)>1 or time.monotonic()>=deadline:raise ValueError('Exact seven-day option unavailable')
        if cancel.wait(.3):raise InterruptedError('Date-range read cancelled')
    click(cdp,options[0])
    if cancel.wait(3):raise InterruptedError('Date-range read cancelled')
    ns=tree();rows=ax_texts(ns)
    if not any(r['text']in {'7 ngày qua','Last 7 days','過去 7 日間'}for r in rows):
        raise ValueError('Seven-day range not confirmed by readback')
    return ns,rows


def content_records(page):
    ids=video_ids(page);rows=page['observations'];result=[]
    starts=[]
    for vid in ids:
        starts.append(next(i for i,r in enumerate(rows)if f'/video/{vid}/edit'in r.get('url','')))
    for n,(vid,start)in enumerate(zip(ids,starts)):
        segment=rows[start:starts[n+1]if n+1<len(starts)else len(rows)]
        texts=[r['text']for r in segment if r['role']=='StaticText']
        visibility=next((t for t in texts if t in {'Công khai','Không công khai','Riêng tư','Public','Unlisted','Private','公開','限定公開','非公開'}),None)
        result.append({'video_id':vid,'title':next((r['text']for r in segment if r['role']=='link'and not r['text'].startswith(('Hình thu nhỏ','Video thumbnail'))),''),
                       'visibility':visibility,'published_date_raw':next((t for t in texts if re.search(r'\d.*(?:thg|202\d|年)',t)),None),
                       'duration_text':next((t for t in texts if re.fullmatch(r'\d+:\d\d(?::\d\d)?',t)),None)})
    return result


def collect_video_details(workspace, *, endpoint, policy, coordinator=None, cancel=None, budget=90):
    root=workspace.state_dir/'analytics'/policy['channel_id']/'video-details';root.mkdir(parents=True,exist_ok=True)
    cursor_path=root/'cursor.json'
    cursor=json.loads(cursor_path.read_text(encoding='utf-8')) if cursor_path.exists() else {}
    current=datetime.now(timezone.utc)
    if cursor.get('status')=='complete'and cursor.get('schema_version')!=2:
        cursor.update(schema_version=2,status='running',pending=[[v,'reach']for v in cursor['video_ids']],
                      completed=[r for r in cursor.get('completed',[])if r[1]!='reach'])
        atomic_write_json(cursor_path,cursor)
    if cursor.get('status')=='complete' and (current-datetime.fromisoformat(cursor['updated_at'])).total_seconds()<6*3600:return cursor
    deadline=time.monotonic()+budget
    with StudioInspector(endpoint,policy['profile_id'],policy['channel_id'],coordinator=coordinator,cancel=cancel) as inspector:
        if not cursor or cursor.get('status')=='complete':
            with inspector.page(f"https://studio.youtube.com/channel/{policy['channel_id']}/videos/upload") as (_cdp,page,_nodes):
                records=content_records(page)
                ids=[r['video_id']for r in records if r['visibility']in {'Công khai','Public','公開'}]
                if not ids:raise ValueError('No video links observed; do not invent IDs')
                page['videos']=records
                atomic_write_json(root/'content-list.json',page)
                cursor={'schema_version':2,'status':'running','channel_id':policy['channel_id'],'video_ids':ids[:20],
                        'pending':[[v,tab]for v in ids[:20]for tab in ('overview','reach','engagement','audience','edit')],
                        'completed':[],'updated_at':now_iso()}
                atomic_write_json(cursor_path,cursor)
        while cursor['pending'] and time.monotonic()<deadline-22:
            vid,tab=cursor['pending'][0]
            url=f'https://studio.youtube.com/video/{vid}/'+('edit'if tab=='edit'else 'analytics/tab-overview/period-default')
            labels={'reach':('Phạm vi tiếp cận','Reach','リーチ'),'engagement':('Mức độ tương tác','Engagement','エンゲージメント'),
                    'audience':('Đối tượng người xem','Audience','視聴者')}.get(tab,())
            with inspector.page(url,tab_labels=labels) as (_cdp,page,nodes):
                if tab=='reach':
                    nodes,page['observations']=select_seven_days(_cdp,inspector.cancel)
                    page['url']=_cdp.command('Page.getFrameTree',timeout=5)['frameTree']['frame']['url']
                if not page['url'].startswith('https://studio.youtube.com/video/'+vid+'/'):raise ValueError('Video navigation mismatch')
                # ID was observed in the verified channel's own Content page.
                page.update(video_id=vid,report=tab,channel_identity_source='content-list.json',
                            missing_data_is_zero=False,engagement_provenance='may_include_assisted_engagement')
                page['ab_controls']=[r for r in page['observations']if re.search(r'A/B|thử nghiệm|test.*compare|テスト',r['text'],re.I)]
                page['form_fields']=[{'label':str(n.get('name',{}).get('value','')).strip(),
                                      'value':n.get('value',{}).get('value')}
                                     for n in nodes if n.get('role',{}).get('value')=='textbox']
                atomic_write_json(root/f'{vid}-{tab}.json',page)
            cursor['pending'].pop(0);cursor['completed'].append([vid,tab]);cursor['updated_at']=now_iso()
            atomic_write_json(cursor_path,cursor)
    if not cursor['pending']:cursor['status']='complete'
    cursor['updated_at']=now_iso();atomic_write_json(cursor_path,cursor)
    return cursor
