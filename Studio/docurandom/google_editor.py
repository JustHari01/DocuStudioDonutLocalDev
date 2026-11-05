"""Verify owned Docs/Sheets against server exports; no page-script network reads."""
from __future__ import annotations
import base64
import csv
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import re
import unicodedata
import urllib.parse
import urllib.request
from .google_task_policy import GoogleBlocked,GoogleTaskPending,raise_for_reply


def document_key(url):
    u=urllib.parse.urlsplit(url)
    if u.scheme!='https'or u.hostname!='docs.google.com':raise GoogleBlocked('attention','Sai nguồn tài liệu Google')
    match=re.fullmatch(r'/(document|spreadsheets)/(?:u/\d+/)?d/([\w-]+)(?:/.*)?',u.path)
    if not match:raise GoogleBlocked('attention','Không có đúng mã tài liệu Google')
    return match[1],match[2]


def filename(headers):
    raw=str(headers.get('content-disposition',''))
    extended=re.search(r"(?:^|;)\s*filename\*\s*=\s*UTF-8''([^;]+)",raw,re.I)
    if extended:return urllib.parse.unquote(extended[1])
    plain=re.search(r'(?:^|;)\s*filename\s*=\s*"([^"]*)"',raw,re.I)
    return plain[1]if plain else''


@dataclass(frozen=True)
class Export:
    kind:str
    text:str
    filename:str

    @property
    def empty(self):
        if self.kind=='document':return not self.text.strip()
        return not any(cell.strip()for row in csv.reader(io.StringIO(self.text))for cell in row)

    @property
    def sha256(self):return hashlib.sha256(self.text.encode('utf8')).hexdigest()

    def matches(self,step):
        if self.kind=='document':
            return self.text.replace('\r\n','\n').strip()==step['text'].replace('\r\n','\n').strip()
        rows=list(csv.reader(io.StringIO(self.text)))
        while rows and not any(rows[-1]):rows.pop()
        return rows==step['cells']

    def pending_cells(self,step):
        rows=list(csv.reader(io.StringIO(self.text)));expected=step['cells'];missing=[]
        for y,row in enumerate(rows):
            for x,value in enumerate(row):
                if not value:continue
                if y>=len(expected)or x>=len(expected[y])or value!=expected[y][x]:return None
        for y,row in enumerate(expected):
            for x,value in enumerate(row):
                present=rows[y][x]if y<len(rows)and x<len(rows[y])else''
                if present!=value:missing.append((y,x))
        return missing

    def title_matches(self,title):
        actual=unicodedata.normalize('NFC',self.filename);expected=unicodedata.normalize('NFC',title)
        if self.kind=='document':return actual==expected+'.txt'
        return actual==expected+'.csv'or actual.startswith(expected+' - ')and actual.endswith('.csv')

    def has_default_title(self):
        base=self.filename.rsplit('.',1)[0]
        if self.kind=='spreadsheets':base=base.split(' - ',1)[0]
        return unicodedata.normalize('NFC',base).casefold()in{
            'untitled document','untitled spreadsheet','tài liệu không có tiêu đề',
            'bảng tính không có tiêu đề','無題のドキュメント','無題のスプレッドシート'}


class GoogleEditor:
    def __init__(self,runner,opened,owner):
        self.runner=runner;self.opened=opened;self.owner=owner;self.key=document_key(opened['url'])
        self.cdp=None;self.client=runner.client

    def check_frame(self):
        self.runner.check()
        frame=self.cdp.command('Page.getFrameTree')['frameTree']['frame']
        if document_key(frame['url'])!=self.key:raise GoogleBlocked('attention','Tài liệu đã đổi; dừng trước khi nhập')
        return frame

    @contextmanager
    def session(self):
        if self.client is None:raise GoogleTaskPending('Chưa có kết nối kiểm tra đúng tài liệu')
        debug=self.client.get_debug_connection(self.runner.profile_id)
        endpoint=urllib.parse.urlsplit(debug['targets_url'])
        if endpoint.scheme!='http'or endpoint.hostname not in{'localhost','127.0.0.1','::1'}:
            raise GoogleBlocked('attention','Từ chối kết nối kiểm tra ngoài máy này')
        with urllib.request.urlopen(debug['targets_url'],timeout=8)as response:targets=json.load(response)
        matches=[]
        for target in targets:
            if target.get('type')!='page':continue
            try:
                if document_key(target.get('url',''))==self.key:matches.append(target)
            except GoogleBlocked:pass
        if len(matches)>1 and isinstance(self.opened.get('window_id'),int):
            owned=[]
            for target in matches:
                window=self.client.cdp_command(self.runner.profile_id,'Browser.getWindowForTarget',{'targetId':target['id']})
                if window.get('windowId')==self.opened['window_id']:owned.append(target)
            matches=owned
        if len(matches)!=1:raise GoogleTaskPending('Không có duy nhất tab của đúng tài liệu')
        self.target=matches[0]
        socket=urllib.parse.urlsplit(self.target['webSocketDebuggerUrl'])
        if socket.scheme!='ws'or socket.hostname not in{'localhost','127.0.0.1','::1'}:
            raise GoogleBlocked('attention','Từ chối socket kiểm tra ngoài máy này')
        from .youtube_upload import _CdpSession
        with _CdpSession(self.target['webSocketDebuggerUrl'])as cdp:
            self.cdp=cdp;self.check_frame()
            yield self

    def export(self):
        frame=self.check_frame();kind,identity=self.key
        format='txt'if kind=='document'else'csv'
        resource=self.cdp.command('Network.loadNetworkResource',{
            'frameId':frame['id'],'url':f'https://docs.google.com/{kind}/d/{identity}/export?format={format}',
            'options':{'disableCache':True,'includeCredentials':True}})['resource']
        headers={str(k).lower():str(v)for k,v in resource.get('headers',{}).items()}
        status=int(resource.get('httpStatusCode')or 0)
        stream=resource.get('stream');data=b''
        try:
            raise_for_reply({'http_status':status,'retry_after':headers.get('retry-after')})
            if status!=200 or not resource.get('success')or not stream:
                raise GoogleTaskPending('Google chưa trả bản xuất của đúng tài liệu')
            if 'text/html'in headers.get('content-type','').lower():
                raise GoogleTaskPending('Bản xuất trả HTML; chưa xác nhận nội dung đã lưu')
            for _ in range(17):
                part=self.cdp.command('IO.read',{'handle':stream,'size':65536})
                data+=base64.b64decode(part['data'])if part.get('base64Encoded')else part['data'].encode('utf8')
                if len(data)>1024*1024:raise GoogleBlocked('attention','Bản xuất vượt giới hạn ghi chú của lượt này')
                if part.get('eof'):break
            else:raise GoogleTaskPending('Chưa đọc hết bản xuất')
        finally:
            if stream:
                try:self.cdp.command('IO.close',{'handle':stream})
                except Exception:pass
        return Export(kind,data.decode('utf-8-sig'),filename(headers))

    def saved(self):
        self.check_frame()
        nodes=self.cdp.command('Accessibility.getFullAXTree').get('nodes',[])
        names=[str(n.get('name',{}).get('value',''))for n in nodes]
        pattern=r'đã lưu (?:vào|trên) drive|all changes saved in drive|saved to drive|ドライブに保存(?:しました|済み)|変更をすべてドライブに保存'
        return any(re.search(pattern,n,re.I)for n in names)

    def input(self,method,params):
        self.check_frame();return self.cdp.command(method,params)

    def press(self,key,code,number,modifiers=0):
        for kind in ['keyDown','keyUp']:
            self.input('Input.dispatchKeyEvent',{'type':kind,'key':key,'code':code,
                'windowsVirtualKeyCode':number,'nativeVirtualKeyCode':number,'modifiers':modifiers})

    def focus_input(self,selector):
        self.check_frame();root=self.cdp.command('DOM.getDocument',{'depth':0})['root']['nodeId']
        nodes=self.cdp.command('DOM.querySelectorAll',{'nodeId':root,'selector':selector})['nodeIds']
        if len(nodes)!=1:raise GoogleTaskPending('Không có duy nhất ô nhập của tài liệu')
        self.cdp.command('DOM.focus',{'nodeId':nodes[0]})

    def prepare(self):
        self.check_frame()
        window=self.client.cdp_command(self.runner.profile_id,'Browser.getWindowForTarget',{'targetId':self.target['id']})
        if window.get('bounds',{}).get('windowState')=='minimized':
            self.client.cdp_command(self.runner.profile_id,'Browser.setWindowBounds',{'windowId':window['windowId'],'bounds':{'windowState':'normal'}})
        self.cdp.command('Target.activateTarget',{'targetId':self.target['id']})
        self.cdp.command('Emulation.setFocusEmulationEnabled',{'enabled':True})

    def ensure_title(self,title):
        current=self.export()
        if current.title_matches(title):return
        if not current.has_default_title():
            raise GoogleBlocked('attention','Tên file đã đổi ngoài lượt này; giữ nguyên để đối soát')
        self.prepare();self.focus_input('input.docs-title-input');self.press('a','KeyA',65,2)
        self.input('Input.insertText',{'text':title});self.press('Tab','Tab',9)
        for delay in [.5,1,2,4]:
            if self.runner.cancel.wait(delay):self.runner.check()
            if self.export().title_matches(title):return
        raise GoogleTaskPending('Tên chưa được lưu trên máy chủ; giữ cùng tài liệu')

    def write_body(self,step,before_input,cells=None):
        self.prepare()
        if self.key[0]=='document':
            focus=self.runner.command('google_editor_probe',{'app':'docs','tab_id':self.opened['tab_id'],
                'owner_request_id':self.owner,'action':'focus_editor'})
            if focus.get('focused')is not True or focus.get('editable')is not True:
                raise GoogleTaskPending('Chưa xác nhận con trỏ ở đúng phần nội dung Docs')
            before_input()
            self.press('a','KeyA',65,2)
            self.input('Input.insertText',{'text':step['text']})
        else:
            for y,row in enumerate(step['cells']):
                for x,text in enumerate(row):
                    if cells is not None and (y,x)not in cells:continue
                    if x>=26:raise GoogleBlocked('attention','Bảng ghi chú vượt phạm vi cột đã hỗ trợ')
                    self.focus_input('#t-name-box');self.press('a','KeyA',65,2)
                    self.input('Input.insertText',{'text':chr(65+x)+str(y+1)});self.press('Enter','Enter',13)
                    self.press('F2','F2',113);before_input()
                    self.input('Input.insertText',{'text':"'"+text});self.press('Enter','Enter',13)
