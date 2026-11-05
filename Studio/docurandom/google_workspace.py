"""Opt-in Google web routines. Durable writes, isolated tabs, shared profile lane."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid
from urllib.parse import quote, urlencode, urlsplit

from .storage import atomic_write_json
from .google_task_policy import (
    GoogleTaskPending, GoogleBlocked, GoogleTransient,
    classify_exception, raise_for_reply,
)
from . import google_routine_policy as routine

VN = timezone(timedelta(hours=7))
APPS = {
    'search': ('Google Search', 'https://www.google.com/'),
    'maps': ('Google Maps', 'https://www.google.com/maps'),
    'youtube': ('YouTube', 'https://www.youtube.com/'),
    'gmail': ('Gmail', 'https://mail.google.com/mail/u/0/#inbox'),
    'drive': ('Google Drive', 'https://drive.google.com/drive/u/0/my-drive'),
    'docs': ('Google Docs', 'https://docs.google.com/document/u/0/'),
    'sheets': ('Google Sheets', 'https://docs.google.com/spreadsheets/u/0/'),
    'slides': ('Google Slides', 'https://docs.google.com/presentation/u/0/'),
    'forms': ('Google Forms', 'https://docs.google.com/forms/u/0/'),
    'calendar': ('Google Calendar', 'https://calendar.google.com/calendar/u/0/r'),
    'keep': ('Google Keep', 'https://keep.google.com/'),
    'photos': ('Google Photos', 'https://photos.google.com/'),
    'translate': ('Google Translate', 'https://translate.google.com/'),
    'news': ('Google News', 'https://news.google.com/'),
    'books': ('Google Books', 'https://books.google.com/'),
    'earth': ('Google Earth', 'https://earth.google.com/web/'),
    'finance': ('Google Finance', 'https://www.google.com/finance/'),
    'contacts': ('Google Contacts', 'https://contacts.google.com/'),
    'tasks': ('Google Tasks', 'https://tasks.google.com/'),
    'meet': ('Google Meet', 'https://meet.google.com/'),
    'chat': ('Google Chat', 'https://chat.google.com/'),
    'sites': ('Google Sites', 'https://sites.google.com/'),
    'groups': ('Google Groups', 'https://groups.google.com/'),
    'blogger': ('Blogger', 'https://www.blogger.com/'),
    'classroom': ('Google Classroom', 'https://classroom.google.com/'),
    'arts': ('Google Arts & Culture', 'https://artsandculture.google.com/'),
}
CREATES = {'docs': 'document', 'sheets': 'spreadsheets', 'slides': 'presentation', 'forms': 'forms'}


def now():
    return datetime.now(VN)


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError('Google workspace state must be an object')
        return value
    except FileNotFoundError:
        return {}


def root(workspace):
    return workspace.state_dir / 'google-workspace'


def config(workspace):
    value = {**routine.DEFAULTS, 'version': 2, 'enabled': False, 'interval_minutes': 120,
             'apps': ['search', 'maps', 'youtube', 'gmail', 'drive', 'docs', 'sheets', 'translate', 'news'],
             'apps_per_cycle': 3, 'queries': ['工作機械 日本', '東京都 科学博物館'],
             'create_files': False, 'upload_thumbnail': False}
    value.update(read(root(workspace)/'config.json'))
    return value


def validated_config(workspace, value):
    c = config(workspace)
    c.update(value)
    c = routine.validate(c)
    if type(c['enabled']) is not bool or type(c['create_files']) is not bool or type(c['upload_thumbnail']) is not bool:
        raise ValueError('Bật/tắt chức năng phải là true/false')
    if type(c['interval_minutes']) is not int or not 15 <= c['interval_minutes'] <= 10080:
        raise ValueError('Chu kỳ phải từ 15 phút đến 7 ngày')
    if not isinstance(c['apps'], list) or not c['apps'] or any(a not in APPS for a in c['apps']):
        raise ValueError('Chọn ít nhất một ứng dụng Google có trong danh mục')
    c['apps'] = list(dict.fromkeys(c['apps']))
    if type(c['apps_per_cycle']) is not int or not 1 <= c['apps_per_cycle'] <= 5:
        raise ValueError('Mỗi lượt chạy từ 1 đến 5 ứng dụng')
    if not isinstance(c['queries'], list) or any(not isinstance(q,str) or not q.strip() or len(q)>500 for q in c['queries']):
        raise ValueError('Mỗi từ khóa phải có nội dung và tối đa 500 ký tự')
    return c


def save_config(workspace, value):
    c=validated_config(workspace,value)
    atomic_write_json(root(workspace)/'config.json',c)
    return c


def browse_url(app, query=''):
    if app not in APPS:
        raise ValueError('Ứng dụng Google chưa được hỗ trợ')
    if not query:
        return APPS[app][1]
    if app == 'search': return 'https://www.google.com/search?' + urlencode({'q':query})
    if app == 'maps': return 'https://www.google.com/maps/search/' + quote(query, safe='')
    if app == 'youtube': return 'https://www.youtube.com/results?' + urlencode({'search_query':query})
    if app == 'drive': return 'https://drive.google.com/drive/u/0/search?q=' + quote(query, safe='')
    if app == 'translate': return 'https://translate.google.com/?' + urlencode({'sl':'auto','tl':'vi','text':query,'op':'translate'})
    if app == 'news': return 'https://news.google.com/search?' + urlencode({'q':query})
    return APPS[app][1]


def steps_for(workspace, c, state, *, clock=None):
    clock = clock or now()
    apps = c['apps']; offset = int(state.get('cursor',0)) % len(apps)
    chosen = [apps[(offset+i)%len(apps)] for i in range(min(len(apps),c['apps_per_cycle']))]
    queries = c['queries'] or ['日本 技術']
    query = queries[int(state.get('cycle',0)) % len(queries)]
    day = clock.astimezone(VN).date().isoformat()
    result=[]
    for app in chosen:
        creating=c['create_files'] and app in CREATES
        # A requested artifact opens its editor directly. Loading its home page
        # first has no work to perform and costs another window and page load.
        if not creating:
            result.append({'app':app,'action':'browse','url':browse_url(app,query if app in {'search','maps','youtube','translate','news'} else '')})
        if creating:
            title=f'DocuRandom · {workspace.name} · {day} · {APPS[app][0]}'
            result.append({'app':app,'action':'create','url':f'https://docs.google.com/{CREATES[app]}/create',
                           'title':title,'text':f'Ghi chú nghiên cứu · {workspace.name}\nNgày: {day}\nChủ đề: {query}',
                           'cells':[['Ngày','Workspace','Chủ đề'],[day,workspace.name,query]],
                           'once_key':key([workspace.workspace_id,c.get('profile_id'),day,app,'create'])})
        if c['upload_thumbnail'] and app=='drive':
            image=workspace.state_dir/'thumbnails/current.png'
            if image.is_file():
                sha=hashlib.sha256(image.read_bytes()).hexdigest()
                legacy=key([workspace.workspace_id,app,'upload',sha])
                identity=key([workspace.workspace_id,c.get('profile_id'),app,'upload',sha])
                # Retain pre-upgrade identities, including uncertain uploads.
                if (root(workspace)/'jobs'/(legacy+'.json')).is_file():identity=legacy
                result.append({'app':app,'action':'upload','file':str(image),'sha256':sha,
                               'filename':f'DocuRandom_{workspace.name}_{sha[:12]}.png',
                               'once_key':identity})
    return result, (offset+len(chosen)) % len(apps)


def preview(workspace, value=None):
    """Describe the retained/new plan without reserving or submitting a job."""
    c=validated_config(workspace,value or {})
    s=read(root(workspace)/'state.json');settings=read(workspace.settings_path)
    profile=str(settings.get('youtube_profile_id')or settings.get('youtube_selected_profile_id')or'')
    c['profile_id']=profile
    steps=s.get('plan')or steps_for(workspace,c,s)[0]
    rows=[]
    for step in steps:
        prior=read(root(workspace)/'jobs'/(step['once_key']+'.json'))if step.get('once_key')else{}
        rows.append({'app':step['app'],'label':APPS[step['app']][0],'action':step['action'],
                     'writes_file':step['action']in{'create','upload'},
                     'title':step.get('title')or step.get('filename')or'',
                     'receipt_status':prior.get('status','not_started')})
    return {'workspace':workspace.name,'profile_selected':bool(profile),
            'retained_plan':bool(s.get('plan')),'enabled':c['enabled'],
            'policy':{k:c[k]for k in routine.DEFAULTS},'gate':routine.gate(c,s,now()),'steps':rows,
            'current_status':s.get('status'),'blocked_until_user_action':s.get('status')in{
                'auth_required','account_required','permission_required','verification_required','attention'}}


def can_release_profile(baseline, current):
    """New/changed non-owned tabs mean a user may be working in this profile."""
    if not baseline or baseline.get('session_id') != current.get('session_id'):
        return False
    prior={tab['id']:tab.get('url_digest') for tab in baseline.get('tabs',[])}
    owned=set(current.get('owned_tab_ids',[]))
    return all(tab['id'] in owned or prior.get(tab['id']) == tab.get('url_digest')
               for tab in current.get('tabs',[]))


def resolve_upload_source(workspace,step):
    """A queued thumbnail is identified by its bytes, not the mutable current name."""
    expected=step['sha256']
    if len(expected)!=64 or any(c not in'0123456789abcdef'for c in expected):raise ValueError('Mã ảnh không hợp lệ')
    candidates=[Path(step['file']),workspace.state_dir/'thumbnail-candidates'/(expected+'.png')]
    history=workspace.state_dir/'thumbnails/history'
    if history.is_dir():candidates+=sorted(history.glob('*.png'),reverse=True)[:100]
    for image in candidates:
        if image.is_file()and image.stat().st_size<=25*1024*1024 and hashlib.sha256(image.read_bytes()).hexdigest()==expected:
            return image
    raise ValueError('Chưa tìm được đúng ảnh trong checkpoint; không tải ảnh mới thay thế')


def record_activity(workspace, state):
    path=root(workspace)/'activity.json'
    history=read(path).get('events',[])
    event={k:state[k] for k in (
        'status','detail','current_app','current_action','current_step','step_count',
        'recovery_attempts','issue_status','profile_launches','profile_releases',
        'profile_retained_reason','server_not_before','next_run_at',
        'activity_cycle_id','last_cycle_elapsed_seconds',
        'profile_cache_hits',
    ) if k in state}
    event['at']=now().isoformat()
    events=(history+[event])[-100:]
    atomic_write_json(path,{'events':events})
    atomic_write_json(root(workspace)/'daily-summary.json',routine.summary(state,events,now()))


class GoogleRunner:
    def __init__(self, workspace, bridge, profile_id, cancel=None, client=None):
        self.workspace=workspace; self.bridge=bridge; self.profile_id=profile_id
        self.cancel=cancel or threading.Event()
        self.client=client
        self._session_id=None
        self.deadline=None

    def session_id(self):
        if self._session_id is None:
            value=self.command('google_session',{},timeout=10)
            self._session_id=value['session_id']
        return self._session_id

    def check(self):
        if self.cancel.is_set(): raise InterruptedError('Đã nhường/dừng tác vụ Google')
        if self.deadline is not None and time.monotonic()>=self.deadline:
            raise InterruptedError('Đã hết thời gian phiên Google; giữ checkpoint để nối tiếp lượt này')

    def command(self, command, params, *, timeout=20):
        self.check()
        try:
            result = self.bridge.command(self.profile_id, command, params, timeout=timeout)
        except Exception as exc:
            classified = classify_exception(exc)
            if classified is exc:
                raise
            raise classified from exc
        if not isinstance(result, dict):
            raise GoogleTaskPending('GOOGLE_RESPONSE_PENDING: chưa đọc được kết quả đúng cấu trúc')
        raise_for_reply(result)
        return result

    def call(self, command, params, request_id):
        """Both sides persist before a write; a lost reply is never replayed."""
        self.check()
        path=root(self.workspace)/'jobs'/(request_id+'.json')
        old=read(path)
        digest = key([command, params])
        context = {'workspace_id':self.workspace.workspace_id, 'profile_id':self.profile_id,
                   'command':command, 'request_digest':digest}
        if any(k in old and old[k] != value for k,value in context.items()):
            raise GoogleBlocked('attention', 'Biên nhận không khớp profile hoặc yêu cầu; giữ nguyên để đối soát')
        deadline = old.get('server_not_before')
        if deadline and now() < datetime.fromisoformat(deadline):
            remaining = int((datetime.fromisoformat(deadline)-now()).total_seconds())+1
            raise GoogleTransient(old.get('last_issue','rate_limited'), 'Chưa đến thời gian máy chủ cho phép thử lại', remaining)
        if old.get('status')=='complete':return old['result']
        try:
            if old:
                result=self.command('google_job',{'request_id':request_id,'observe':True},timeout=20)
            else:
                atomic_write_json(path,{**context,'status':'submitted','request_id':request_id,'created_at':now().isoformat()})
                result=self.command(command,{**params,'request_id':request_id},timeout=90)
        except GoogleTransient as exc:
            saved=read(path);saved['last_issue']=exc.status
            if exc.retry_after is not None:
                saved['server_not_before']=(now()+timedelta(seconds=exc.retry_after)).isoformat()
            atomic_write_json(path,saved)
            raise
        except GoogleBlocked as exc:
            if command=='google_create':exc.preserve_profile=True
            raise
        except InterruptedError:
            raise
        except Exception as exc:
            raise GoogleTaskPending('GOOGLE_RECOVERY_PENDING: chờ đọc đúng biên nhận '+request_id[:12])from exc
        if result.get('status') not in {'complete','created'}:
            raise GoogleTaskPending('GOOGLE_RECOVERY_PENDING: chưa xác nhận đúng biên nhận '+request_id[:12])
        atomic_write_json(path,{**context,'status':'complete','request_id':request_id,'result':result,'updated_at':now().isoformat()})
        return result

    def snapshot(self,app,tab_id,owner):
        self.check()
        r=self.command('google_snapshot',{'app':app,'tab_id':tab_id,'owner_request_id':owner},timeout=20)
        if r.get('status')!='ok':raise GoogleTaskPending('GOOGLE_PAGE_PENDING: giao diện chưa sẵn sàng')
        return r

    def write_editor(self,step,opened,owner,operation_id):
        """Read server evidence first; repair only an empty/saved owned document."""
        path=root(self.workspace)/'jobs'/(key([operation_id,'content'])+'.json')
        prior=read(path)
        content_digest=key([step['text'],step['cells']])
        for field,expected in [('artifact_url',opened['url']),('text_sha256',content_digest)]:
            if prior.get(field) and prior[field]!=expected:
                raise GoogleBlocked('attention','Nội dung hoặc mã tài liệu đã đổi; không nhập tiếp vào file khác')
        if prior.get('status')=='complete':return
        # Compatibility for callers which can only read an existing receipt.
        if self.client is None:
            result=self.command('google_editor_probe',{'app':step['app'],'tab_id':opened['tab_id'],
                'owner_request_id':owner,'action':'verify_content','text':step['text'],'cells':step['cells']})
            if result.get('matched')is True:
                atomic_write_json(path,{'status':'complete','artifact_url':opened['url'],'text_sha256':content_digest,'verified_at':now().isoformat()});return
            raise GoogleTaskPending('Chưa có bản xuất xác nhận nội dung; không nhập lại')
        from .google_editor import GoogleEditor
        journal={**prior,'artifact_url':opened['url'],'text_sha256':content_digest}
        try:
            with GoogleEditor(self,opened,owner).session()as editor:
                if step['app']=='sheets':editor.settle_sheet_edit(step)
                exported=editor.export()
                if exported.matches(step):
                    journal.update(status='complete',verified_at=now().isoformat(),server_sha256=exported.sha256)
                    atomic_write_json(path,journal);return
                missing=exported.pending_cells(step)if step['app']=='sheets'else None
                if not exported.empty and (step['app']!='sheets'or missing is None):
                    raise GoogleBlocked('attention','File có nội dung khác; không ghi đè hoặc tạo file mới')
                for delay in [.5,1,2,4]:
                    if editor.saved():break
                    if self.cancel.wait(delay):self.check()
                else:raise GoogleTaskPending('File chưa xác nhận đã lưu; chưa sửa nội dung')
                if prior.get('status')=='submitted':
                    if int(prior.get('empty_repair_attempts',0))>=1:
                        raise GoogleTaskPending('Đã dùng lượt sửa nội dung hữu hạn; giữ đúng file để kiểm tra')
                    journal['empty_repair_attempts']=int(prior.get('empty_repair_attempts',0))+1
                journal.update(status='prepared',phase='focus')
                atomic_write_json(path,journal)
                def before_input():
                    if journal.get('status')!='submitted':
                        journal.update(status='submitted',phase='content-input',submitted_at=now().isoformat())
                        atomic_write_json(path,journal)
                editor.write_body(step,before_input,cells=missing)
                for delay in [1,2,4,8]:
                    if self.cancel.wait(delay):self.check()
                    exported=editor.export()
                    if exported.matches(step):
                        journal.update(status='complete',verified_at=now().isoformat(),server_sha256=exported.sha256)
                        atomic_write_json(path,journal);return
                raise GoogleTaskPending('Máy chủ chưa xác nhận đủ nội dung; giữ cùng file và biên nhận')
        except Exception as exc:
            journal.update(last_error=str(exc)[:500],last_checked_at=now().isoformat())
            if not journal.get('status'):journal['status']='prepared'
            atomic_write_json(path,journal)
            raise

    def ensure_title(self, step, opened, owner, operation_id):
        if step['app'] not in {'docs','sheets','slides'}:
            return
        if self.client is not None and step['app']in{'docs','sheets'}:
            from .google_editor import GoogleEditor
            with GoogleEditor(self,opened,owner).session()as editor:editor.ensure_title(step['title'])
            return
        for attempt in range(3):
            snapshot=self.snapshot(step['app'],opened['tab_id'],owner)
            if snapshot.get('editorTitle')==step['title']:
                return
            if attempt==0:
                self.call('google_action',{'app':step['app'],'tab_id':opened['tab_id'],
                    'owner_request_id':owner,'action':'set_title','title':step['title']},
                    key([operation_id,'title',owner]))
            if self.cancel.wait(1):self.check()
        raise GoogleTaskPending('GOOGLE_TITLE_PENDING: chưa đọc lại đúng tên tài liệu; giữ URL, không tạo file khác')

    def run_step(self, step, operation_id):
        self.check(); app=step['app']; action=step['action']
        existing=read(root(self.workspace)/'jobs'/(operation_id+'.json'))
        if existing.get('profile_id') and existing['profile_id']!=self.profile_id:
            raise GoogleBlocked('attention','Lượt Google thuộc profile khác; giữ biên nhận cũ')
        if existing.get('status')=='complete':return existing['result']
        command='google_create' if action=='create' else 'google_open'
        p={'app':app,'url':step.get('url',APPS[app][1])}
        if action=='create':p['title']=step['title']
        elif action=='browse':p['purpose']='browse'
        owner=key([operation_id,'create'])if action=='create'else key([operation_id,'open',self.session_id()])
        opened=self.call(command,p,owner)
        if action=='create' and app in {'docs','sheets'} and step.get('text') and opened.get('browser_session_id')!=self.session_id():
            owner=key([operation_id,'resume-editor',self.session_id()])
            opened=self.call('google_open',{'app':app,'url':opened['url']},owner)
        tab=opened['tab_id']; preserve=False
        try:
            if action=='create':
                preserve=True
                self.ensure_title(step,opened,owner,operation_id)
                if app in {'docs','sheets'} and step.get('text'):
                    self.write_editor(step,opened,owner,operation_id)
                preserve=False
                result={**opened,'action':action}
                atomic_write_json(root(self.workspace)/'jobs'/(operation_id+'.json'),{'status':'complete','profile_id':self.profile_id,'result':result})
                return result
            snapshot=self.snapshot(app,tab,owner)
            if action=='browse':
                for index in range(2):
                    self.call('google_action',{'app':app,'tab_id':tab,'owner_request_id':owner,'action':'scroll','pixels':650},key([operation_id,'scroll',owner,index]))
                result={'status':'complete','url':snapshot['url'],'title':snapshot['title'],'app':app,'action':action}
                atomic_write_json(root(self.workspace)/'jobs'/(operation_id+'.json'),{'status':'complete','profile_id':self.profile_id,'result':result})
                return result
            image=resolve_upload_source(self.workspace,step)
            path=root(self.workspace)/'jobs'/(operation_id+'.json')
            old=read(path)
            preserve=True  # Any lost reply can mean the bytes are already transferring.
            if self.client is not None and (not old or old.get('native_file_input')):
                from .google_drive import GoogleDriveInput,exact_file
                native=old or {'status':'prepared','profile_id':self.profile_id,'file_sha256':step['sha256'],
                    'request_id':operation_id,'native_file_input':True,'tab_id':tab,'created_at':now().isoformat()}
                atomic_write_json(path,native)
                if native['status']=='prepared':
                    def before_input():
                        native.update(status='submitted',submitted_at=now().isoformat())
                        atomic_write_json(path,native)
                    with GoogleDriveInput(self,opened,owner).session()as drive:
                        drive.submit(image,step['filename'],before_input)
                found=None
                for _ in range(30):
                    self.check();snapshot=self.snapshot('drive',tab,owner)
                    url=exact_file(snapshot,step['filename'])
                    if url and found==url:
                        result={'status':'complete','app':'drive','action':'upload','artifact_url':url,'filename':step['filename'],
                            'verified_at':now().isoformat(),'source_sha256':step['sha256'],'verification':'two-live-owned-tab-observations'}
                        native.update(status='complete',result=result);atomic_write_json(path,native)
                        preserve=False;return result
                    found=url
                    if self.cancel.wait(2):self.check()
                raise GoogleTaskPending('GOOGLE_UPLOAD_PENDING: file đã gắn một lần; giữ biên nhận native, không tải lại')
            if not old:
                atomic_write_json(path,{'status':'submitted','profile_id':self.profile_id,'file_sha256':step['sha256'],
                    'request_id':operation_id,'command':'google_upload','tab_id':tab,'created_at':now().isoformat()})
                result=self.command('google_upload',{'app':'drive','tab_id':tab,'owner_request_id':owner,'request_id':operation_id,
                    'file_url':self.bridge.share_file(image,ttl=120),'filename':step['filename'],'mime':'image/png'},timeout=40)
            else:
                result=self.command('google_job',{'request_id':operation_id,'observe':True},timeout=15)
                if result.get('status')!='complete'and result.get('browser_session_id')!=self.session_id():
                    raise GoogleTaskPending('GOOGLE_UPLOAD_PENDING: browser cũ đã đóng; giữ biên nhận và đối soát file trên Drive')
            if result.get('status')=='complete':
                atomic_write_json(path,{'status':'complete','profile_id':self.profile_id,'request_id':operation_id,'result':result});preserve=False;return result
            for _ in range(30):
                self.check()
                result=self.command('google_verify_upload',{'app':'drive','tab_id':tab,'owner_request_id':owner,
                    'upload_request_id':operation_id,'filename':step['filename']},timeout=15)
                if result.get('status')=='complete':
                    atomic_write_json(path,{'status':'complete','profile_id':self.profile_id,'request_id':operation_id,'result':result});preserve=False;return result
                if self.cancel.wait(2):self.check()
            raise GoogleTaskPending('GOOGLE_UPLOAD_PENDING: giữ tab và biên nhận, chưa upload lại')
        except (GoogleTaskPending, GoogleBlocked, InterruptedError) as exc:
            preserve=action in {'upload','create'}
            if preserve:exc.preserve_profile=True
            raise
        except Exception as exc:
            if preserve:raise GoogleTaskPending('GOOGLE_RECOVERY_PENDING: giữ file và biên nhận, chưa gửi lại')from exc
            raise
        finally:
            if not preserve:
                try:self.bridge.command(self.profile_id,'google_close',{'app':app,'tab_id':tab,'owner_request_id':owner},timeout=8)
                except Exception:pass


class GoogleService:
    """A single idle-only worker shared by enabled workspaces, separate schedule."""
    def __init__(self, store, coordinator):
        self.store=store;self.coordinator=coordinator;self.cancel=threading.Event()
        self.active_cancel=None;self.active_workspace_id='';self.busy=False;self.thread=None;self.requested=set();self.lock=threading.RLock();self.waiter_keys=set()

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=self._loop,daemon=True,name='GoogleWorkspaceScheduler');self.thread.start()

    def stop(self):
        self.cancel.set()
        if self.active_cancel:self.active_cancel.set()
        for key in tuple(self.waiter_keys):self.coordinator.cancel_idle_waiter(key)

    def run_now(self, workspace):
        with self.lock:
            if self.active_workspace_id==workspace.workspace_id:return
            self.requested.add(workspace.workspace_id)
            state=read(root(workspace)/'state.json')
            state.update(status='queued',detail='Đang chờ profile rảnh để chạy Google',
                         manual_requested=True,waiting_since=state.get('waiting_since')or now().isoformat())
            atomic_write_json(root(workspace)/'state.json',state)

    def cancel_workspace(self,workspace_id):
        with self.lock:
            key='google:'+workspace_id
            self.coordinator.cancel_idle_waiter(key);self.waiter_keys.discard(key)
            self.requested.discard(workspace_id)
            if self.active_workspace_id==workspace_id and self.active_cancel:self.active_cancel.set()
            workspace=self.store.get(workspace_id)
            if workspace is not None:
                state=read(root(workspace)/'state.json');state['manual_requested']=False
                state['cancelled_at']=now().isoformat()
                atomic_write_json(root(workspace)/'state.json',state)

    def control(self,command,workspace_id):
        workspace=self.store.get(workspace_id)
        if workspace is None:raise ValueError('Workspace không tồn tại')
        if command=='google_status':
            return json.dumps({'workspace':workspace.name,'config':config(workspace),'state':read(root(workspace)/'state.json')},ensure_ascii=False)
        if command=='google_run':
            self.run_now(workspace)
            return 'Đã xếp đúng một lượt Google cho '+workspace.name
        if command=='google_cancel':
            self.cancel_workspace(workspace.workspace_id)
            return 'Đã dừng/nhường lượt Google; giữ kế hoạch và biên nhận của '+workspace.name
        if command=='google_preview':
            return json.dumps(preview(workspace),ensure_ascii=False)
        if command=='google_diagnostics':
            return json.dumps(self.diagnostics(workspace),ensure_ascii=False)
        raise ValueError('Lệnh Google không được hỗ trợ')

    def diagnostics(self, workspace):
        """Only query an already paired extension; never launch, pair, or navigate."""
        from .browser_extension_bridge import get_browser_extension_bridge
        settings=read(workspace.settings_path)
        profile=str(settings.get('youtube_profile_id')or settings.get('youtube_selected_profile_id')or'')
        state=read(root(workspace)/'state.json');connection={'connected':False}
        if profile:
            bridge=get_browser_extension_bridge();status=bridge.status(profile)
            if status.connected:
                ping=bridge.command(profile,'ping',{},timeout=5)
                inventory=bridge.command(profile,'google_inventory',{},timeout=5)
                connection={'connected':True,'extension_version':status.extension_version,
                            'google_protocol_ready':'google-workspace-v2'in ping.get('capabilities',[]),
                            'tab_count':len(inventory.get('tabs',[]))}
        results={r['app']:r for r in state.get('results',[])if r.get('app')in APPS}
        matrix=[]
        for app,(label,_)in APPS.items():
            actions=['browse']
            if app in CREATES:actions.append('create')
            if app in {'docs','sheets'}:actions.append('write_and_verify_content')
            if app=='drive':actions.append('upload_and_verify')
            success=state.get('last_success_by_app',{}).get(app)
            if not success and app in results:success={'action':results[app].get('action'),'at':state.get('updated_at')}
            matrix.append({'app':app,'label':label,'implemented_actions':actions,
                           'last_completed_google_task':success,'authenticated_now':'not_verified'})
        report={'at':now().isoformat(),'workspace':workspace.name,'connection':connection,
                'task_status':state.get('status'),'apps':matrix,'preview':preview(workspace),
                'account_reputation':'not_measured','creates_files':False,'launches_profile':False}
        atomic_write_json(root(workspace)/'diagnostics.json',report)
        return report

    def _loop(self):
        while not self.cancel.wait(10):
            for w in self.store.list():
                if self.cancel.is_set():return
                try:self.tick(w)
                except Exception as exc:
                    # Bounded per-workspace state, no Telegram traffic.
                    atomic_write_json(root(w)/'scheduler-error.json',{'detail':str(exc)[:500],'updated_at':now().isoformat()})

    def tick(self,w):
        c=config(w);s=read(root(w)/'state.json')
        with self.lock:forced=w.workspace_id in self.requested
        if not c['enabled'] and not forced and not s.get('manual_requested'):return
        if s.get('status') in {'auth_required','account_required','permission_required','verification_required','attention'} and not forced:return
        if s.get('server_not_before') and now()<datetime.fromisoformat(s['server_not_before']):return
        manual_queued=bool(s.get('manual_requested')and s.get('status')=='queued')
        if not(forced or manual_queued) and s.get('status')not in{'waiting_window','daily_limit'} and s.get('next_run_at') and now()<datetime.fromisoformat(s['next_run_at']):return
        policy_wait=routine.gate(c,s,now())
        if policy_wait:
            if any(s.get(k)!=v for k,v in policy_wait.items()):
                s.update(policy_wait,updated_at=now().isoformat())
                atomic_write_json(root(w)/'state.json',s)
            with self.lock:self.requested.discard(w.workspace_id)
            return
        settings=read(w.settings_path);profile=str(settings.get('youtube_profile_id')or settings.get('youtube_selected_profile_id')or'')
        if not profile:
            s.update(status='attention',detail='Chưa chọn profile Donut của workspace');atomic_write_json(root(w)/'state.json',s);return
        if s.get('cycle_id')and s.get('profile_id') not in {None,profile}:
            s.update(status='attention',detail='Profile đã đổi khi còn biên nhận Google; cần đối soát lượt cũ')
            atomic_write_json(root(w)/'state.json',s);return
        c={**c,'profile_id':profile};s['profile_id']=profile
        wait_key='google:'+w.workspace_id
        # A fresh request containing only already-verified daily artifacts has
        # no browser work. Keep pending plans on their original recovery path.
        if not(s.get('cycle_id')and s.get('plan')):
            planned,next_cursor=steps_for(w,c,s)
            cached=[]
            for step in planned:
                receipt=read(root(w)/'jobs'/(step['once_key']+'.json'))if step.get('once_key')else{}
                if receipt.get('status')!='complete'or receipt.get('profile_id')!=profile:
                    break
                result=receipt.get('result')
                if not isinstance(result,dict)or result.get('status')not in{'complete','created'}or result.get('app')!=step['app']or result.get('action')!=step['action']:
                    break
                cached.append({k:result[k]for k in ['app','action','url','artifact_url','title','title_saved']if k in result})
            if planned and len(cached)==len(planned):
                self.coordinator.cancel_idle_waiter(wait_key);self.waiter_keys.discard(wait_key)
                with self.lock:self.requested.discard(w.workspace_id)
                s.update(status='cached',detail='Các file của lượt này đã có kết quả hoàn tất được lưu; không mở profile hoặc kiểm lại máy chủ',
                         manual_requested=False,results=cached,cursor=next_cursor,
                         profile_cache_hits=int(s.get('profile_cache_hits',0))+1,
                         updated_at=now().isoformat(),next_run_at=(now()+timedelta(minutes=c['interval_minutes'])).isoformat())
                s.pop('waiting_since',None)
                atomic_write_json(root(w)/'state.json',s);record_activity(w,s)
                return
        self.waiter_keys.add(wait_key)
        cancel=threading.Event();lease=self.coordinator.try_acquire(profile,cancel,job_kind='google-workspace',priority=-50,wait_key=wait_key)
        if lease is None:
            s.update(status='queued',detail='Profile đang được tác vụ khác giữ; Google tiếp tục chờ',
                     waiting_since=s.get('waiting_since')or now().isoformat(),updated_at=now().isoformat())
            atomic_write_json(root(w)/'state.json',s)
            return
        self.waiter_keys.discard(wait_key)
        with lease:
            with self.lock:
                self.requested.discard(w.workspace_id)
                self.active_cancel=cancel;self.active_workspace_id=w.workspace_id;self.busy=True
            s.update(status='running',detail='Đang nối đúng profile và biên nhận',started_at=now().isoformat(),
                     current_operation='connection',current_app='',current_action='')
            s.pop('waiting_since',None)
            opened=False;keep_profile=False;began=time.monotonic()
            retry_delay=None
            try:
                if not(s.get('cycle_id')and s.get('plan')):
                    steps,cursor=steps_for(w,c,s)
                    s.update(cycle_id=key([w.workspace_id,uuid.uuid4().hex]),plan=steps,next_cursor=cursor)
                s['activity_cycle_id']=s['cycle_id']
                routine.reserve(s,s['cycle_id'],now())
                atomic_write_json(root(w)/'state.json',s)
                from .donut_manager import resolve_donut_mcp_endpoint
                from .donut_mcp import DonutMcpClient
                from .youtube_upload import _ensure_youtube_browser_bridge, YouTubeStudioUploader
                with DonutMcpClient(resolve_donut_mcp_endpoint(),timeout=30)as client:
                    client.require_docurandom_local();p=client.get_profile(profile)
                    prior_pid=s.get('owned_profile_pid')
                    candidate_owned=type(prior_pid)is int and prior_pid>0 and p.process_id==prior_pid
                    if not p.running:
                        client.run_profile(profile,'about:blank');opened=True
                        p=client.get_profile(profile);s['owned_profile_pid']=p.process_id
                        s['profile_launches']=int(s.get('profile_launches',0))+1
                        s.pop('profile_baseline',None)
                        atomic_write_json(root(w)/'state.json',s)
                    bridge=_ensure_youtube_browser_bridge(client,profile,required_capabilities={'google-workspace-v3'})
                    runner=GoogleRunner(w,bridge,profile,cancel,client)
                    runner.deadline=began+c['max_cycle_minutes']*60
                    if not opened and candidate_owned:
                        inventory=runner.command('google_inventory',{},timeout=15)
                        if (s.get('profile_baseline')or{}).get('session_id')==inventory.get('session_id')and inventory.get('session_id'):
                            opened=True
                        else:
                            s.pop('owned_profile_pid',None);s.pop('profile_baseline',None)
                            s['profile_retained_reason']='Phiên trình duyệt đã đổi; không nhận quyền đóng profile từ PID cũ'
                    if opened and not s.get('profile_baseline'):
                        s['profile_baseline']=runner.command('google_inventory',{},timeout=15)
                    steps=s['plan'];cursor=s['next_cursor'];cycle=s['cycle_id']
                    atomic_write_json(root(w)/'state.json',s)
                    results=[]
                    for i,step in enumerate(steps):
                        runner.check();identity=step.get('once_key')or key([cycle,i,step])
                        s.update(current_app=step['app'],current_action=step['action'],current_step=i+1,
                                 step_count=len(steps),detail='Đang làm '+APPS[step['app']][0],current_operation=identity)
                        atomic_write_json(root(w)/'state.json',s)
                        result=runner.run_step(step,identity)
                        s.setdefault('last_success_by_app',{})[step['app']]={
                            'action':step['action'],'at':now().isoformat(),'operation_id':identity,
                            **({'artifact_url':result['artifact_url']}if result.get('artifact_url')else{})}
                        results.append({k:result[k]for k in ['app','action','url','artifact_url','title','title_saved']if k in result})
                        s['results']=results;atomic_write_json(root(w)/'state.json',s)
                    s.update(status='complete',detail='Đã hoàn tất lượt Google',cursor=cursor,cycle=int(s.get('cycle',0))+1,recovery_attempts=0)
                    s['manual_requested']=False
                    s.pop('server_not_before',None)
                    s.pop('cycle_id',None)
                    s.pop('plan',None);s.pop('next_cursor',None)
            except GoogleBlocked as exc:
                s.update(status=exc.status,detail=str(exc),manual_requested=False)
                keep_profile=bool(getattr(exc,'preserve_profile',False))
            except PermissionError:
                s.update(status='auth_required',detail='Cần đăng nhập ứng dụng trong đúng profile',manual_requested=False)
            except GoogleTaskPending as exc:
                operation=s.get('current_operation','connection')
                budgets=s.setdefault('recovery_by_operation',{})
                attempts=int(budgets.get(operation,s.get('recovery_attempts',0)if not budgets else 0))+1
                budgets[operation]=attempts
                # Acknowledging progress never resets another operation's budget.
                s.update(status='recovering'if attempts<=3 else'attention',detail=str(exc),recovery_attempts=attempts);keep_profile=True
                if isinstance(exc,GoogleTransient):
                    s['issue_status']=exc.status
                    if exc.retry_after is not None:
                        retry_delay=exc.retry_after
                        s['server_not_before']=(now()+timedelta(seconds=retry_delay)).isoformat()
                if attempts>3:s['manual_requested']=False
            except InterruptedError as exc:
                s.update(status='yielded',detail=str(exc)or'Đã nhường profile cho tác vụ ưu tiên')
                keep_profile=bool(getattr(exc,'preserve_profile',False))
            except Exception as exc:
                classified=classify_exception(exc)
                if isinstance(classified,GoogleBlocked):
                    s.update(status=classified.status,detail=str(classified),manual_requested=False)
                elif isinstance(classified,GoogleTransient):
                    attempts=int(s.get('recovery_attempts',0))+1
                    s.update(status='recovering'if attempts<=3 else'attention',issue_status=classified.status,
                             detail=str(classified),recovery_attempts=attempts)
                    retry_delay=classified.retry_after
                    if retry_delay is not None:
                        s['server_not_before']=(now()+timedelta(seconds=retry_delay)).isoformat()
                    if attempts>3:s['manual_requested']=False
                else:s.update(status='attention',detail='Chưa xác nhận được kết quả; xem biên nhận trong thư mục Google',manual_requested=False)
                keep_profile=True
            finally:
                try:
                    elapsed=max(0,time.monotonic()-began)
                    s['last_cycle_elapsed_seconds']=round(elapsed,3)
                    routine.account_elapsed(s,now(),elapsed)
                    delay=timedelta(seconds=(120,300,900)[min(max(0,int(s.get('recovery_attempts',1))-1),2)]) if s.get('status')=='recovering'else timedelta(minutes=c['interval_minutes'])
                    if retry_delay is not None:
                        delay=max(delay,timedelta(seconds=retry_delay))
                    s.update(updated_at=now().isoformat(),next_run_at=(now()+delay).isoformat())
                    disk_state=read(root(w)/'state.json')
                    if disk_state.get('cancelled_at') and disk_state['cancelled_at']!=s.get('cancelled_at'):
                        s.update(cancelled_at=disk_state['cancelled_at'],manual_requested=False)
                    if not c['enabled'] and not s.get('manual_requested'):
                        with self.lock:
                            # Cancel from the UI must also remain cancelled in the final snapshot.
                            self.requested.discard(w.workspace_id)
                    atomic_write_json(root(w)/'state.json',s)
                    if opened and not keep_profile:
                        try:
                            from .donut_manager import resolve_donut_mcp_endpoint
                            from .donut_mcp import DonutMcpClient
                            from .youtube_upload import YouTubeStudioUploader
                            with DonutMcpClient(resolve_donut_mcp_endpoint(),timeout=20)as client:
                                live=client.get_profile(profile)
                                inventory=bridge.command(profile,'google_inventory',{},timeout=15)
                                owned_pid=s.get('owned_profile_pid')
                                if type(owned_pid)is int and owned_pid>0 and live.process_id==owned_pid and can_release_profile(s.get('profile_baseline'),inventory):
                                    YouTubeStudioUploader._close_profile_started_by_workflow(client,profile)
                                    s['profile_releases']=int(s.get('profile_releases',0))+1
                                    s.pop('owned_profile_pid',None);s.pop('profile_baseline',None);s.pop('profile_release_pending',None)
                                else:s['profile_retained_reason']='Giữ profile vì có thay đổi/tab ngoài lượt Google'
                                atomic_write_json(root(w)/'state.json',s)
                        except Exception:
                            s['profile_release_pending']=True
                            atomic_write_json(root(w)/'state.json',s)
                    record_activity(w,s)
                finally:
                    self.active_cancel=None;self.active_workspace_id='';self.busy=False
