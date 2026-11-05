import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from docurandom import google_workspace as g


@pytest.fixture
def workspace(tmp_path):
    d=tmp_path/'.docurandom';d.mkdir()
    return SimpleNamespace(state_dir=d,workspace_id='workspace-google',name='DaiTa4',settings_path=d/'settings.json')


def test_schedule_is_separate_and_default_off(workspace):
    assert g.config(workspace)['enabled'] is False
    workspace.settings_path.write_text('{"schedule_armed": false}')
    g.save_config(workspace,{'enabled':True,'interval_minutes':60})
    assert json.loads(workspace.settings_path.read_text())=={'schedule_armed':False}
    assert g.config(workspace)['enabled'] is True
    with pytest.raises(ValueError):g.save_config(workspace,{'apps':['unknown']})
    with pytest.raises(ValueError):g.save_config(workspace,{'interval_minutes':0})


def test_round_robin_creation_is_once_per_app_day(workspace):
    c=g.config(workspace);c.update(apps=['docs','sheets','maps'],apps_per_cycle=2,create_files=True)
    day=datetime(2026,10,28,16,tzinfo=g.VN)
    steps,cursor=g.steps_for(workspace,c,{},clock=day)
    assert [s['app']for s in steps]==['docs','sheets']
    assert all(s['action']=='create'for s in steps)
    assert cursor==2
    again,_=g.steps_for(workspace,c,{'cycle':4},clock=day)
    assert steps[1]['once_key']==again[1]['once_key']
    tomorrow,_=g.steps_for(workspace,c,{},clock=day.replace(day=29))
    assert steps[1]['once_key']!=tomorrow[1]['once_key']


def test_thumbnail_upload_is_bound_to_bytes_not_name(workspace):
    image=workspace.state_dir/'thumbnails/current.png';image.parent.mkdir();image.write_bytes(b'one')
    c=g.config(workspace);c.update(apps=['drive'],upload_thumbnail=True)
    steps,_=g.steps_for(workspace,c,{})
    same,_=g.steps_for(workspace,c,{'cycle':8})
    assert steps[1]['once_key']==same[1]['once_key']
    image.write_bytes(b'two');changed,_=g.steps_for(workspace,c,{})
    assert steps[1]['once_key']!=changed[1]['once_key']


class Bridge:
    def __init__(self):self.calls=[];self.fail=False;self.jobs={};self.verifications=0
    def share_file(self,*args,**kwargs):return 'http://127.0.0.1:17843/file/'+'a'*43+'/file.png'
    def command(self,profile,command,params,**kwargs):
        self.calls.append((profile,command,params))
        if command=='google_session':return {'session_id':'session-one'}
        if command=='google_job':return self.jobs.get(params['request_id'],{'status':'missing'})
        if command=='google_snapshot':return {'status':'ok','url':'https://www.google.com/','title':'Google'}
        if command=='google_close':return {'closed':True}
        if command=='google_verify_upload':
            self.verifications+=1
            return {'status':'complete','artifact_url':'https://drive.google.com/file/d/saved/view'}
        if self.fail:raise TimeoutError('Reply lost after submission')
        r={'status':'submitted'if command=='google_upload'else'complete','tab_id':7,'url':params.get('url','https://www.google.com/'),'app':params.get('app'),'browser_session_id':'session-one'}
        self.jobs[params['request_id']]=r
        return r


def test_lost_mutation_reply_never_resends_after_restart(workspace):
    b=Bridge();b.fail=True;job='a'*64
    with pytest.raises(g.GoogleTaskPending):g.GoogleRunner(workspace,b,'profile').call('google_create',{'app':'docs'},job)
    with pytest.raises(g.GoogleTaskPending):g.GoogleRunner(workspace,b,'profile').call('google_create',{'app':'docs'},job)
    assert [c for _,c,_ in b.calls]==['google_create','google_job']
    b.jobs[job]={'status':'complete','tab_id':7,'url':'https://docs.google.com/document/d/exact/edit'}
    r=g.GoogleRunner(workspace,b,'profile').call('google_create',{'app':'docs'},job)
    assert '/exact/'in r['url']
    assert [c for _,c,_ in b.calls].count('google_create')==1


def test_browse_is_id_scoped_and_completed_step_does_not_open_more_tabs(workspace):
    b=Bridge();runner=g.GoogleRunner(workspace,b,'profile');step={'app':'search','action':'browse','url':g.browse_url('search','東京')}
    r=runner.run_step(step,'a'*64);assert r['status']=='complete'
    runner.run_step(step,'a'*64)
    assert [c for _,c,_ in b.calls].count('google_open')==1
    assert [c for _,c,_ in b.calls].count('google_action')==2
    assert [c for _,c,_ in b.calls].count('google_close')==1
    assert all(p.get('owner_request_id') for _,c,p in b.calls if c in ['google_action','google_snapshot','google_close'])


def test_drive_requires_readback_and_reuses_completed_upload(workspace):
    image=workspace.state_dir/'image.png';image.write_bytes(b'png')
    step={'app':'drive','action':'upload','file':str(image),'sha256':g.hashlib.sha256(b'png').hexdigest(),'filename':'exact.png'}
    b=Bridge();runner=g.GoogleRunner(workspace,b,'profile')
    assert runner.run_step(step,'b'*64)['artifact_url'].endswith('/saved/view')
    assert runner.run_step(step,'b'*64)['artifact_url'].endswith('/saved/view')
    assert [c for _,c,_ in b.calls].count('google_upload')==1 and b.verifications==1


def test_queued_thumbnail_resolves_original_bytes_after_current_changes(workspace):
    current=workspace.state_dir/'thumbnails/current.png';current.parent.mkdir();current.write_bytes(b'new image')
    expected=g.hashlib.sha256(b'old image').hexdigest()
    original=workspace.state_dir/'thumbnail-candidates'/(expected+'.png');original.parent.mkdir();original.write_bytes(b'old image')
    step={'file':str(current),'sha256':expected}
    assert g.resolve_upload_source(workspace,step)==original
    assert current.read_bytes()==b'new image'
    original.write_bytes(b'wrong image')
    with pytest.raises(ValueError):g.resolve_upload_source(workspace,step)


def test_lost_upload_reply_keeps_its_tab(workspace):
    image=workspace.state_dir/'image.png';image.write_bytes(b'png')
    step={'app':'drive','action':'upload','file':str(image),'sha256':g.hashlib.sha256(b'png').hexdigest(),'filename':'exact.png'}
    b=Bridge();old=b.command
    def call(profile,command,params,**kwargs):
        if command=='google_upload':raise TimeoutError('Upload response lost')
        return old(profile,command,params,**kwargs)
    b.command=call
    with pytest.raises(g.GoogleTaskPending):g.GoogleRunner(workspace,b,'profile').run_step(step,'c'*64)
    assert not any(c=='google_close'for _,c,_ in b.calls)


def test_busy_upload_lane_does_not_launch_google(workspace):
    g.save_config(workspace,{'enabled':True});workspace.settings_path.write_text('{"youtube_profile_id":"profile"}')
    coordinator=Mock();coordinator.try_acquire.return_value=None
    service=g.GoogleService(SimpleNamespace(list=lambda:[workspace]),coordinator)
    service.tick(workspace)
    coordinator.try_acquire.assert_called_once()
    state=g.read(g.root(workspace)/'state.json')
    assert state['status']=='queued' and state['waiting_since']
    assert 'owned_profile_pid' not in state and not (g.root(workspace)/'jobs').exists()


def test_content_recovery_only_reads_export_and_does_not_type_again(workspace):
    operation='d'*64;path=g.root(workspace)/'jobs'/(g.key([operation,'content'])+'.json')
    path.parent.mkdir(parents=True);path.write_text('{"status":"submitted"}')
    class Probe(Bridge):
        def command(self,profile,command,params,**kwargs):
            assert command=='google_editor_probe'and params['action']=='verify_content'
            return {'matched':True}
    runner=g.GoogleRunner(workspace,Probe(),'profile')
    runner.cancel=SimpleNamespace(wait=lambda _:False,is_set=lambda:False)
    runner.write_editor({'app':'docs','text':'saved content','cells':[]},{'tab_id':7,'url':'https://docs.google.com/document/d/exact/edit'},'owner',operation)
    assert g.read(path)['status']=='complete'


def test_local_google_control_is_authenticated_and_idempotent(tmp_path):
    import urllib.request,urllib.error
    from docurandom.workspace_control import LocalWorkspaceControl
    calls=[];service=LocalWorkspaceControl(tmp_path/'control.json',lambda *_:'old','test',
        google_dispatch=lambda *a:calls.append(a)or'queued')
    desc=g.read(tmp_path/'control.json')
    body={'command':'google_run','workspace_id':'a'*32,'request_id':'b'*32}
    def request(token):
        req=urllib.request.Request(desc['endpoint']+'/v1/workspace/command',data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=3)as r:return r.status
        except urllib.error.HTTPError as e:return e.code
    try:
        assert request('invalid')==401 and not calls
        assert request(desc['token'])==200 and request(desc['token'])==200
        assert calls==[('google_run','a'*32)]
    finally:service.stop()


def test_url_builders_escape_queries_and_reject_unknown_app():
    assert len(g.APPS)==26
    assert '%2F'in g.browse_url('maps','Tokyo/a')
    assert 'search_query='in g.browse_url('youtube','日本 技術')
    with pytest.raises(ValueError):g.browse_url('google.com.evil')


def test_extension_ledger_and_tab_ownership_in_real_javascript():
    import subprocess,shutil
    node=shutil.which('node')or r'C:\Program Files\nodejs\node.exe'
    source=Path(__file__).resolve().parents[1]/'donut-extension/google-workspace.js'
    script=r'''
const vm=require('vm'),fs=require('fs'),crypto=require('crypto').webcrypto;
const local={},session={};let creates=0,removed=0;
const storage=data=>({get:async k=>({[k]:data[k]}),set:async obj=>Object.assign(data,obj)});
const context={URL,TextEncoder,crypto,Date,setTimeout,console,chrome:{storage:{local:storage(local),session:storage(session)},
 windows:{create:async()=>{creates++;return {id:3,tabs:[{id:8}]};}},
 tabs:{get:async()=>({id:8,url:'https://docs.google.com/document/d/exact/edit',status:'complete'}),remove:async()=>{removed++;}},
 scripting:{executeScript:async()=>[{result:{status:'ok',title:'exact'}}]}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const g=context.GoogleWorkspace,request_id='a'.repeat(64);
(async()=>{
 if(g.allowed('docs','https://docs.google.com.evil/document/d/x'))throw Error('host escaped');
 if(g.allowed('docs','https://docs.google.com/spreadsheets/d/x'))throw Error('app escaped');
 const p={app:'docs',url:'https://docs.google.com/document/create',title:'exact',request_id};
 const results=await Promise.all([g.execute('google_create',p),g.execute('google_create',p)]);
 if(creates!==1 || results[0].url!==results[1].url)throw Error('duplicate creation');
 const snapshot={app:'docs',tab_id:8,owner_request_id:request_id};
 await g.execute('google_snapshot',snapshot);
 session.googleBrowserSession='new-browser-session';
 try{await g.execute('google_close',snapshot);throw Error('stale tab closed');}
 catch(e){if(!String(e).includes('OWNERSHIP_UNCERTAIN'))throw e;}
 if(removed!==0)throw Error('closed foreign session tab');
 const b='b'.repeat(64);local.googleTaskReceipts[b]={status:'submitted',payload_sha256:'different'};
 try{await g.execute('google_create',{...p,request_id:b});throw Error('mismatch accepted');}
 catch(e){if(!String(e).includes('REQUEST_MISMATCH'))throw e;}
 console.log('PASS exact ledger, one creation, app boundary, browser restart ownership');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    r=subprocess.run([node,'-e',script,str(source)],capture_output=True,text=True,timeout=10)
    assert r.returncode==0,r.stderr


def test_manifest_uses_fresh_worker_identity_for_extension_update():
    extension=Path(__file__).resolve().parents[1]/'donut-extension'
    manifest=json.loads((extension/'manifest.json').read_text(encoding='utf8'))
    worker=manifest['background']['service_worker']
    assert manifest['version'].replace('.','')in worker
    source=(extension/worker).read_text(encoding='utf8')
    assert "importScripts('google-workspace.js')"in source
    assert 'google-workspace-v1'in source
