import threading
import shutil
import subprocess
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from docurandom import google_workspace as g
from docurandom import google_editor as module
from docurandom.google_editor import Export,GoogleEditor,document_key,filename


def test_export_exact_content_detects_duplicates_and_validates_unicode_title():
    value=Export('document','日本の技術\r\n','Đề tài Nhật.txt')
    assert value.matches({'text':'日本の技術','cells':[]})
    assert value.title_matches('Đề tài Nhật')
    assert not Export('document','日本の技術\n日本の技術','x.txt').matches({'text':'日本の技術','cells':[]})
    assert Export('document','\n\r','x.txt').empty
    assert filename({'content-disposition':"attachment; filename=\"bad.txt\"; filename*=UTF-8''%E6%97%A5%E6%9C%AC.txt"})=='日本.txt'
    assert Export('document','','Tài liệu không có tiêu đề.txt').has_default_title()
    assert Export('spreadsheets','','Untitled spreadsheet - Sheet1.csv').has_default_title()
    assert not Export('document','','My private document.txt').has_default_title()


def test_partial_sheet_only_allows_missing_cells_and_never_overwrites_other_values():
    step={'cells':[['日付','名前'],['2026-10-28','DaiTa4']]}
    assert Export('spreadsheets','日付,名前\n2026-10-28,','sheet.csv').pending_cells(step)==[(1,1)]
    assert Export('spreadsheets','日付,OTHER\n','sheet.csv').pending_cells(step)is None
    assert Export('spreadsheets','日付,名前,PRIVATE\n','sheet.csv').pending_cells(step)is None
    assert Export('spreadsheets',',\r\n,\r\n','sheet.csv').empty
    assert Export('spreadsheets','日付,名前\n2026-10-28,DaiTa4\r\n','sheet.csv').matches(step)


def test_document_identity_allows_tab_query_but_rejects_another_origin():
    assert document_key('https://docs.google.com/document/d/same/edit?tab=t.0')==document_key('https://docs.google.com/document/u/0/d/same/edit')
    with pytest.raises(g.GoogleBlocked):document_key('https://evil.test/document/d/same/edit')


def test_html_export_never_passes_and_closes_stream_without_reading_body():
    runner=Mock();runner.client=Mock()
    editor=GoogleEditor(runner,{'url':'https://docs.google.com/document/d/same/edit'},'owner')
    def command(method,params=None):
        if method=='Page.getFrameTree':return {'frameTree':{'frame':{'id':'frame','url':'https://docs.google.com/document/d/same/edit'}}}
        if method=='Network.loadNetworkResource':return {'resource':{'httpStatusCode':200,'success':True,'stream':'stream','headers':{'Content-Type':'text/html'}}}
        assert method=='IO.close'
        return {}
    editor.cdp=Mock();editor.cdp.command.side_effect=command
    with pytest.raises(g.GoogleTaskPending):editor.export()
    methods=[c.args[0]for c in editor.cdp.command.call_args_list]
    assert 'IO.read'not in methods and methods[-1]=='IO.close'
    assert not any(m.startswith('Runtime.')for m in methods)


@pytest.fixture
def setup_editor(tmp_path,monkeypatch):
    d=tmp_path/'.docurandom';d.mkdir()
    w=SimpleNamespace(state_dir=d,workspace_id='w',name='DaiTa4')
    runner=g.GoogleRunner(w,Mock(),'profile',client=Mock())
    runner.cancel=SimpleNamespace(is_set=lambda:False,wait=lambda _:False)
    cloud=SimpleNamespace(export=Export('document','','old.txt'),saved=True,writes=0,focus_fail=False,cells=None)
    step={'app':'docs','text':'日本語の声','cells':[]}
    class Editor:
        def __init__(self,*a):pass
        @contextmanager
        def session(self):yield self
        def export(self):return cloud.export
        def saved(self):return cloud.saved
        def settle_sheet_edit(self,step):
            if getattr(cloud,'pending_exact_cell',False):
                cloud.export=Export('spreadsheets','A,B\n','same.csv')
                cloud.pending_exact_cell=False
        def write_body(self,step,before_input,cells=None):
            if cloud.focus_fail:raise g.GoogleTaskPending('focus not verified')
            before_input();cloud.writes+=1;cloud.cells=cells
            cloud.export=Export('document',step['text'],'same.txt')if step['app']=='docs'else Export('spreadsheets','A,B\n','same.csv')
    monkeypatch.setattr(module,'GoogleEditor',Editor)
    opened={'url':'https://docs.google.com/document/d/same/edit','tab_id':1}
    path=g.root(w)/'jobs'/(g.key(['operation','content'])+'.json')
    return runner,cloud,step,opened,path


def test_pending_exact_sheet_cell_is_committed_before_deciding_to_retype(setup_editor):
    runner,cloud,step,opened,path=setup_editor
    step.update(app='sheets',cells=[['A','B']]);cloud.pending_exact_cell=True
    cloud.export=Export('spreadsheets','A,\n','same.csv')
    g.atomic_write_json(path,{'status':'submitted','artifact_url':opened['url'],'empty_repair_attempts':1})
    runner.write_editor(step,opened,'owner','operation')
    assert cloud.writes==0
    assert g.read(path)['status']=='complete' and g.read(path)['empty_repair_attempts']==1


@pytest.mark.parametrize('value,accepted',[("'東京都 科学博物館\n",True),("'東京都 科学博物館'東京都 科学博物館\n",False)])
def test_sheet_pending_buffer_must_match_exact_cell_before_commit(value,accepted):
    editor=GoogleEditor(Mock(),{'url':'https://docs.google.com/spreadsheets/d/same/edit'},'owner')
    editor.check_frame=Mock();editor.press=Mock();editor.cdp=Mock()
    editor.cdp.command.return_value={'nodes':[{'role':{'value':'combobox'},'name':{'value':'C2'},
        'value':{'value':value},'properties':[{'name':'focused','value':{'value':True}}]}]}
    step={'cells':[['日付','名前','主題'],['2026-10-28','DaiTa4','東京都 科学博物館']]}
    if accepted:
        editor.settle_sheet_edit(step);editor.press.assert_called_once_with('Tab','Tab',9)
    else:
        with pytest.raises(g.GoogleBlocked):editor.settle_sheet_edit(step)
        editor.press.assert_not_called()


def test_legacy_uncertain_empty_saved_doc_gets_one_bounded_repair(setup_editor):
    runner,cloud,step,opened,path=setup_editor
    g.atomic_write_json(path,{'status':'submitted','artifact_url':opened['url']})
    runner.write_editor(step,opened,'owner','operation')
    state=g.read(path)
    assert cloud.writes==1 and state['status']=='complete'and state['empty_repair_attempts']==1
    assert state['server_sha256']==cloud.export.sha256
    runner.write_editor(step,opened,'owner','operation');assert cloud.writes==1


def test_server_already_contains_exact_body_is_verified_without_typing(setup_editor):
    runner,cloud,step,opened,path=setup_editor;cloud.export=Export('document',step['text'],'same.txt')
    g.atomic_write_json(path,{'status':'submitted','artifact_url':opened['url']})
    runner.write_editor(step,opened,'owner','operation')
    assert cloud.writes==0 and g.read(path)['status']=='complete'


def test_nonempty_unknown_document_content_is_preserved(setup_editor):
    runner,cloud,step,opened,path=setup_editor;cloud.export=Export('document','user words','same.txt')
    with pytest.raises(g.GoogleBlocked):runner.write_editor(step,opened,'owner','operation')
    assert cloud.writes==0


def test_empty_repair_budget_and_unsaved_state_do_not_allow_another_write(setup_editor):
    runner,cloud,step,opened,path=setup_editor
    g.atomic_write_json(path,{'status':'submitted','empty_repair_attempts':1})
    with pytest.raises(g.GoogleTaskPending):runner.write_editor(step,opened,'owner','operation')
    assert cloud.writes==0
    g.atomic_write_json(path,{'status':'submitted'});cloud.saved=False
    with pytest.raises(g.GoogleTaskPending):runner.write_editor(step,opened,'owner','operation')
    assert cloud.writes==0


def test_focus_failure_remains_prepared_not_unknown_submitted(setup_editor):
    runner,cloud,step,opened,path=setup_editor;cloud.focus_fail=True
    with pytest.raises(g.GoogleTaskPending):runner.write_editor(step,opened,'owner','operation')
    assert g.read(path)['status']=='prepared'and cloud.writes==0
    cloud.focus_fail=False;runner.write_editor(step,opened,'owner','operation')
    assert cloud.writes==1 and g.read(path)['status']=='complete'


def test_partial_sheet_repairs_only_blank_cells(setup_editor):
    runner,cloud,step,opened,path=setup_editor
    step={'app':'sheets','text':'x','cells':[['A','B']]}
    cloud.export=Export('spreadsheets','A,\n','same.csv')
    g.atomic_write_json(path,{'status':'submitted'})
    runner.write_editor(step,opened,'owner','operation')
    assert cloud.cells==[(0,1)]and cloud.writes==1 and g.read(path)['status']=='complete'


def test_docs_focus_uses_editable_descendant_not_noneditable_iframe_body():
    source=Path(__file__).resolve().parents[1]/'donut-extension/google-workspace.js'
    script=r'''
const fs=require('fs'),vm=require('vm');let bodyFocused=0;
const frame={activeElement:null,body:{isContentEditable:false,focus:()=>{bodyFocused++;}},querySelectorAll:()=>[editor]};
const editor={isContentEditable:true,tagName:'DIV',focus:()=>frame.activeElement=editor};
const iframe={contentDocument:frame,focus:()=>document.activeElement=iframe};
const document={activeElement:null,body:{innerText:''},querySelector:s=>s==='iframe.docs-texteventtarget-iframe'?iframe:null,querySelectorAll:()=>[]};
const context={URL,Array,document,location:{hostname:'docs.google.com',pathname:'/document/d/same/edit',href:'https://docs.google.com/document/d/same/edit'},performance:{getEntriesByType:()=>[]}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{const result=await context.GoogleWorkspace.inPage('focus_editor',{});
if(!result.focused||!result.editable||bodyFocused)throw Error('wrong input focused');
frame.querySelectorAll=()=>[];
try{await context.GoogleWorkspace.inPage('focus_editor',{});throw Error('noneditable body accepted');}
catch(e){if(!String(e).includes('EDITOR_MISSING'))throw e;}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([shutil.which('node')or r'C:\Program Files\nodejs\node.exe','-e',script,str(source)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
