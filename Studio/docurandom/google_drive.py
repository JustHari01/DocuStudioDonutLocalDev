"""Native file input on one receipt-owned Drive tab; submitted bytes are never replayed."""
from contextlib import contextmanager
import hashlib,json,re,time,urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from .google_task_policy import GoogleBlocked,GoogleTaskPending

def drive_page(url):
    u=urlsplit(url)
    if u.scheme!='https' or u.hostname!='drive.google.com':
        raise GoogleBlocked('attention','Sai nguồn Google Drive')
    return u.path,u.query

def exact_file(snapshot,filename):
    if snapshot.get('upload_in_progress'):return None
    ids={r['id'] for r in snapshot.get('items',[]) if re.fullmatch(r'[\w-]{15,}',str(r.get('id','')))
         and filename in str(r.get('text',''))}
    if len(ids)!=1:return None
    return 'https://drive.google.com/file/d/'+next(iter(ids))+'/view'

class GoogleDriveInput:
    def __init__(self,runner,opened,owner):
        self.runner=runner;self.opened=opened;self.owner=owner;self.cdp=None
        self.key=drive_page(opened['url'])

    def check(self):
        self.runner.check()
        if drive_page(self.cdp.command('Page.getFrameTree')['frameTree']['frame']['url'])!=self.key:
            raise GoogleBlocked('attention','Tab Drive đã đổi; dừng trước khi gắn file')

    @contextmanager
    def session(self):
        # The extension proves tab/owner/session before any CDP input.
        self.runner.snapshot('drive',self.opened['tab_id'],self.owner)
        debug=self.runner.client.get_debug_connection(self.runner.profile_id)
        u=urlsplit(debug['targets_url'])
        if u.scheme!='http' or u.hostname not in {'127.0.0.1','localhost','::1'}:
            raise GoogleBlocked('attention','Từ chối kết nối Drive ngoài máy này')
        with urllib.request.urlopen(debug['targets_url'],timeout=8)as h:targets=json.load(h)
        matches=[]
        for t in targets:
            if t.get('type')!='page':continue
            try:
                if drive_page(t.get('url',''))==self.key:matches.append(t)
            except GoogleBlocked:pass
        owned=[]
        for t in matches:
            window=self.runner.client.cdp_command(self.runner.profile_id,'Browser.getWindowForTarget',{'targetId':t['id']})
            if window.get('windowId')==self.opened.get('window_id'):owned.append((t,window))
        if len(owned)!=1:raise GoogleTaskPending('Chưa có duy nhất tab Drive thuộc lượt này')
        target,window=owned[0];socket=urlsplit(target['webSocketDebuggerUrl'])
        if socket.scheme!='ws' or socket.hostname not in {'127.0.0.1','localhost','::1'}:
            raise GoogleBlocked('attention','Từ chối socket Drive ngoài máy này')
        if window.get('bounds',{}).get('windowState')=='minimized':
            self.runner.client.cdp_command(self.runner.profile_id,'Browser.setWindowBounds',{'windowId':window['windowId'],'bounds':{'windowState':'normal'}})
        from .youtube_upload import _CdpSession
        with _CdpSession(target['webSocketDebuggerUrl'])as cdp:
            self.cdp=cdp;self.check()
            cdp.command('Target.activateTarget',{'targetId':target['id']})
            cdp.command('Emulation.setFocusEmulationEnabled',{'enabled':True})
            yield self

    def submit(self,image,filename,before_input):
        if Path(filename).name!=filename or not filename.lower().endswith('.png'):
            raise ValueError('Tên file Drive không hợp lệ')
        from .youtube_upload import _SemanticCdpController
        controller=_SemanticCdpController(self.cdp)
        self.check();self.cdp.command('Page.setInterceptFileChooserDialog',{'enabled':True})
        try:
            new=controller.find(controller.nodes(),role='button',names=('New','Mới','新規'))
            if new is None:raise GoogleTaskPending('Drive chưa hiện nút tạo/tải tệp')
            controller.click_dom(new)
            menu=None
            for _ in range(30):
                self.check()
                menu=controller.find(controller.nodes(),role='menuitem',names=('File upload','Tải tệp lên','ファイルのアップロード'),contains=True)
                if menu is not None:break
                if self.runner.cancel.wait(.2):self.runner.check()
            if menu is None:raise GoogleTaskPending('Drive chưa hiện mục Tải tệp lên')
            controller.click_dom(menu)
            node=None
            for _ in range(30):
                self.check();doc=self.cdp.command('DOM.getDocument',{'depth':0})['root']['nodeId']
                ids=self.cdp.command('DOM.querySelectorAll',{'nodeId':doc,'selector':'input[type="file"]:not([webkitdirectory]):not([disabled])'})['nodeIds']
                if len(ids)==1:node=ids[0];break
                if len(ids)>1:raise GoogleBlocked('attention','Có nhiều ô tải file Drive; không chọn đoán')
                if self.runner.cancel.wait(.2):self.runner.check()
            if node is None:raise GoogleTaskPending('Chưa xác minh được ô file Drive; chưa gắn file')
            data=image.read_bytes()
            if not data or len(data)>25*1024*1024:raise ValueError('Ảnh Drive vượt giới hạn')
            staged=self.runner.workspace.state_dir/'google-workspace/upload-input'/hashlib.sha256(data).hexdigest()/filename
            staged.parent.mkdir(parents=True,exist_ok=True)
            if not staged.exists():staged.write_bytes(data)
            if hashlib.sha256(staged.read_bytes()).digest()!=hashlib.sha256(data).digest():
                raise GoogleBlocked('attention','Bytes ảnh thay đổi trước khi tải Drive')
            self.check();before_input()
            self.cdp.command('DOM.setFileInputFiles',{'nodeId':node,'files':[str(staged.resolve())]})
        finally:
            try:self.cdp.command('Page.setInterceptFileChooserDialog',{'enabled':False})
            except Exception:pass
