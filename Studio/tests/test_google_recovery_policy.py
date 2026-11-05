import io
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from docurandom import google_workspace as g
from docurandom import google_task_policy as policy


@pytest.fixture
def workspace(tmp_path):
    folder=tmp_path/'.docurandom';folder.mkdir()
    return SimpleNamespace(state_dir=folder,workspace_id='w',name='DaiTa4',settings_path=folder/'settings.json')


@pytest.mark.parametrize('status',list(policy.BLOCKED))
def test_page_blockers_are_not_counted_as_uncertain_writes(status):
    with pytest.raises(g.GoogleBlocked)as error:
        policy.raise_for_reply({'status':'submitted','error':'GOOGLE_'+status.upper()})
    assert error.value.status==status


def test_retry_after_seconds_and_http_date_do_not_shorten_wait():
    clock=datetime(2026,10,28,13,tzinfo=timezone.utc)
    assert policy.retry_after_seconds('33',clock=clock)==33
    assert policy.retry_after_seconds('Wed, 28 Oct 2026 13:02:00 GMT',clock=clock)==120
    assert policy.retry_after_seconds('Tue, 27 Oct 2026 13:00:00 GMT',clock=clock)==0
    assert policy.retry_after_seconds('bad')is None
    with pytest.raises(g.GoogleBlocked):policy.retry_after_seconds('999999999999999')


def test_old_auth_receipt_stays_auth_and_is_observed_without_resubmission(workspace):
    request='a'*64
    g.atomic_write_json(g.root(workspace)/'jobs'/(request+'.json'),{'status':'submitted','command':'google_open'})
    bridge=Mock();bridge.command.return_value={'status':'submitted','error':'GOOGLE_ACCOUNT_REQUIRED'}
    with pytest.raises(g.GoogleBlocked)as error:
        g.GoogleRunner(workspace,bridge,'profile').call('google_open',{'app':'docs'},request)
    assert error.value.status=='account_required'
    assert bridge.command.call_args.args[1:] == ('google_job',{'request_id':request,'observe':True})


def test_persisted_server_wait_blocks_manual_resubmission_after_restart(workspace,monkeypatch):
    clock=datetime(2026,10,28,20,tzinfo=g.VN);monkeypatch.setattr(g,'now',lambda:clock)
    bridge=Mock();bridge.command.return_value={'status':'submitted','http_status':429,'retry_after':'600'}
    request='b'*64
    with pytest.raises(g.GoogleTransient):
        g.GoogleRunner(workspace,bridge,'profile').call('google_create',{'app':'docs'},request)
    assert bridge.command.call_count==1
    with pytest.raises(g.GoogleTransient):
        g.GoogleRunner(workspace,bridge,'profile').call('google_create',{'app':'docs'},request)
    assert bridge.command.call_count==1
    clock+=timedelta(seconds=601)
    bridge.command.return_value={'status':'created','url':'https://docs.google.com/document/d/same/edit'}
    assert g.GoogleRunner(workspace,bridge,'profile').call('google_create',{'app':'docs'},request)['status']=='created'
    assert bridge.command.call_args.args[1]=='google_job'


def test_receipt_cannot_be_reused_with_different_account_or_payload(workspace):
    bridge=Mock();bridge.command.return_value={'status':'complete','url':'https://docs.google.com/document/d/same/edit'}
    request='c'*64
    g.GoogleRunner(workspace,bridge,'profile-one').call('google_create',{'app':'docs','title':'one'},request)
    for profile,params in [('profile-two',{'app':'docs','title':'one'}),('profile-one',{'app':'docs','title':'changed'})]:
        with pytest.raises(g.GoogleBlocked):g.GoogleRunner(workspace,bridge,profile).call('google_create',params,request)
    assert bridge.command.call_count==1


def test_export_access_denied_is_not_mistaken_for_missing_content(workspace):
    bridge=Mock();bridge.command.return_value={'matched':False,'http_status':403}
    with pytest.raises(g.GoogleBlocked)as error:
        g.GoogleRunner(workspace,bridge,'profile').command('google_editor_probe',{})
    assert error.value.status=='permission_required'


def test_existing_title_is_read_without_sending_another_rename(workspace):
    bridge=Mock();bridge.command.return_value={'status':'ok','editorTitle':'exact'}
    g.GoogleRunner(workspace,bridge,'profile').ensure_title({'app':'docs','title':'exact'}, {'tab_id':7},'owner','op')
    assert bridge.command.call_count==1 and bridge.command.call_args.args[1]=='google_snapshot'


@pytest.mark.parametrize('current,allowed',[
    ({'session_id':'s','tabs':[{'id':1,'url_digest':'same'}],'owned_tab_ids':[]},True),
    ({'session_id':'new','tabs':[{'id':1,'url_digest':'same'}],'owned_tab_ids':[]},False),
    ({'session_id':'s','tabs':[{'id':1,'url_digest':'changed'}],'owned_tab_ids':[]},False),
    ({'session_id':'s','tabs':[{'id':1,'url_digest':'same'},{'id':2,'url_digest':'user'}],'owned_tab_ids':[]},False),
    ({'session_id':'s','tabs':[{'id':2,'url_digest':'own'}],'owned_tab_ids':[2]},True),
])
def test_profile_release_preserves_new_or_changed_user_tabs(current,allowed):
    baseline={'session_id':'s','tabs':[{'id':1,'url_digest':'same'}]}
    assert g.can_release_profile(baseline,current)is allowed


def test_changed_cdp_document_stops_before_typing(workspace,monkeypatch):
    from docurandom import youtube_upload as youtube
    import urllib.request
    page='https://docs.google.com/document/d/exact/edit'
    targets=[{'type':'page','url':page,'id':'exact','webSocketDebuggerUrl':'ws://127.0.0.1:1/exact'}]
    monkeypatch.setattr(urllib.request,'urlopen',lambda *a,**k:io.BytesIO(json.dumps(targets).encode()))
    calls=[]
    class Session:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def command(self,method,params=None):
            calls.append(method)
            return {'frameTree':{'frame':{'url':'https://docs.google.com/document/d/different/edit'}}}
    monkeypatch.setattr(youtube,'_CdpSession',Session)
    client=Mock();client.get_debug_connection.return_value={'targets_url':'http://127.0.0.1/targets'}
    client.cdp_command.return_value={'windowId':1}
    bridge=Mock();bridge.command.return_value={'status':'ok'}
    runner=g.GoogleRunner(workspace,bridge,'profile',client=client)
    with pytest.raises(g.GoogleBlocked):
        runner.write_editor({'app':'docs','text':'private text','cells':[]},{'tab_id':7,'url':page},'owner','op')
    assert not any(method.startswith('Input.')for method in calls)
    assert g.read(g.root(workspace)/'jobs'/(g.key(['op','content'])+'.json'))['status']=='prepared'


def service_fixture(workspace,monkeypatch,execute):
    from docurandom import donut_mcp,donut_manager,youtube_upload
    workspace.settings_path.write_text('{"youtube_profile_id":"profile"}')
    g.save_config(workspace,{'enabled':False,'apps':['search'],'apps_per_cycle':1})
    store=SimpleNamespace(list=lambda:[workspace],get=lambda _:workspace)
    from unittest.mock import MagicMock
    coordinator=Mock();lease=MagicMock();coordinator.try_acquire.return_value=lease
    client=Mock();client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
    client.get_profile.return_value=SimpleNamespace(running=True,process_id=123)
    monkeypatch.setattr(donut_mcp,'DonutMcpClient',lambda *a,**k:client)
    monkeypatch.setattr(donut_manager,'resolve_donut_mcp_endpoint',lambda:'local')
    def ensure(*args,**kwargs):
        assert kwargs['required_capabilities']=={'google-workspace-v3'}
        return Mock()
    monkeypatch.setattr(youtube_upload,'_ensure_youtube_browser_bridge',ensure)
    class Runner:
        def __init__(self,*a):pass
        def check(self):pass
        def run_step(self,step,identity):return execute(step,identity)
    monkeypatch.setattr(g,'GoogleRunner',Runner)
    return store,coordinator,lease


def test_manual_request_survives_restart_and_obeys_recovery_clock(workspace,monkeypatch):
    identities=[]
    def execute(step,identity):
        identities.append(identity)
        if len(identities)==1:raise g.GoogleTaskPending('pending')
        return {'status':'complete','app':'search','url':step['url']}
    store,coordinator,_=service_fixture(workspace,monkeypatch,execute)
    clock=datetime(2026,10,28,20,tzinfo=g.VN);monkeypatch.setattr(g,'now',lambda:clock)
    service=g.GoogleService(store,coordinator);service.run_now(workspace);service.tick(workspace)
    assert g.read(g.root(workspace)/'state.json')['manual_requested']is True
    restarted=g.GoogleService(store,coordinator);restarted.tick(workspace)
    assert len(identities)==1
    clock+=timedelta(seconds=121);restarted.tick(workspace)
    assert len(identities)==2 and identities[0]==identities[1]
    state=g.read(g.root(workspace)/'state.json')
    assert state['status']=='complete'and state['manual_requested']is False


def test_completed_cycles_are_unique_and_daily_cap_blocks_fourth(workspace,monkeypatch):
    ids=[]
    def execute(step,identity):ids.append(identity);return {'app':'search','url':step['url']}
    store,coordinator,_=service_fixture(workspace,monkeypatch,execute)
    monkeypatch.setattr(g,'now',lambda:datetime(2026,10,28,20,tzinfo=g.VN))
    service=g.GoogleService(store,coordinator)
    for _ in range(4):service.run_now(workspace);service.tick(workspace)
    state=g.read(g.root(workspace)/'state.json')
    assert len(set(ids))==3 and len(ids)==3 and coordinator.try_acquire.call_count==3
    assert state['status']=='daily_limit'
    assert len(state['daily_usage']['2026-10-28']['cycles'])==3


def test_queued_manual_request_survives_restart_without_old_interval_delay(workspace,monkeypatch):
    calls=[]
    store,coordinator,_=service_fixture(workspace,monkeypatch,lambda step,identity:calls.append(identity)or{'app':'search','url':step['url']})
    clock=datetime(2026,10,28,20,tzinfo=g.VN);monkeypatch.setattr(g,'now',lambda:clock)
    g.atomic_write_json(g.root(workspace)/'state.json',{'status':'complete','next_run_at':(clock+timedelta(days=1)).isoformat()})
    service=g.GoogleService(store,coordinator);service.run_now(workspace)
    restarted=g.GoogleService(store,coordinator);restarted.tick(workspace)
    assert len(calls)==1 and g.read(g.root(workspace)/'state.json')['status']=='complete'


def test_queued_manual_request_after_restart_still_obeys_server_wait(workspace,monkeypatch):
    store,coordinator,_=service_fixture(workspace,monkeypatch,lambda *a:None)
    clock=datetime(2026,10,28,20,tzinfo=g.VN);monkeypatch.setattr(g,'now',lambda:clock)
    g.atomic_write_json(g.root(workspace)/'state.json',{'status':'queued','manual_requested':True,
        'server_not_before':(clock+timedelta(minutes=5)).isoformat()})
    g.GoogleService(store,coordinator).tick(workspace)
    coordinator.try_acquire.assert_not_called()


def test_auth_does_not_consume_retry_budget_or_reopen_periodically(workspace,monkeypatch):
    def execute(*args):raise g.GoogleBlocked('account_required','choose account')
    store,coordinator,_=service_fixture(workspace,monkeypatch,execute)
    service=g.GoogleService(store,coordinator);service.run_now(workspace);service.tick(workspace)
    state=g.read(g.root(workspace)/'state.json')
    assert state['status']=='account_required'and not state.get('recovery_attempts')
    service.tick(workspace);assert coordinator.try_acquire.call_count==1


def test_uncertain_operation_budget_is_not_reset_by_manual_retry(workspace,monkeypatch):
    ids=[]
    def execute(step,identity):ids.append(identity);raise g.GoogleTaskPending('pending')
    store,coordinator,_=service_fixture(workspace,monkeypatch,execute)
    service=g.GoogleService(store,coordinator)
    for _ in range(4):service.run_now(workspace);service.tick(workspace)
    state=g.read(g.root(workspace)/'state.json')
    assert state['status']=='attention'and state['recovery_attempts']==4 and len(set(ids))==1
    assert not state['manual_requested']


def test_cancelling_manual_cycle_remains_cancelled_on_disk(workspace,monkeypatch):
    service=None
    def execute(*args):
        service.cancel_workspace(workspace.workspace_id)
        raise InterruptedError()
    store,coordinator,_=service_fixture(workspace,monkeypatch,execute)
    service=g.GoogleService(store,coordinator);service.run_now(workspace);service.tick(workspace)
    state=g.read(g.root(workspace)/'state.json')
    assert state['status']=='yielded'and state['manual_requested']is False and state['cancelled_at']


def test_failed_activity_write_always_releases_busy_flag_and_lease(workspace,monkeypatch):
    store,coordinator,lease=service_fixture(workspace,monkeypatch,lambda step,identity:{'app':'search','url':step['url']})
    monkeypatch.setattr(g,'record_activity',Mock(side_effect=OSError('disk full')))
    service=g.GoogleService(store,coordinator);service.run_now(workspace)
    with pytest.raises(OSError):service.tick(workspace)
    assert not service.busy and not service.active_workspace_id and service.active_cancel is None
    lease.__exit__.assert_called_once()


def test_connection_retry_after_cannot_be_bypassed_by_manual_run(workspace,monkeypatch):
    from docurandom import youtube_upload
    store,coordinator,_=service_fixture(workspace,monkeypatch,lambda *a:None)
    clock=datetime(2026,10,28,20,tzinfo=g.VN);monkeypatch.setattr(g,'now',lambda:clock)
    failing=Mock(side_effect=g.GoogleTransient('rate_limited','wait',600))
    monkeypatch.setattr(youtube_upload,'_ensure_youtube_browser_bridge',failing)
    service=g.GoogleService(store,coordinator);service.run_now(workspace);service.tick(workspace)
    assert g.read(g.root(workspace)/'state.json')['server_not_before']==(clock+timedelta(seconds=600)).isoformat()
    service.run_now(workspace);service.tick(workspace)
    assert failing.call_count==1


def test_cancel_during_command_remains_cancellation(workspace):
    bridge=Mock();bridge.command.side_effect=InterruptedError('cancelled')
    with pytest.raises(InterruptedError):
        g.GoogleRunner(workspace,bridge,'profile').call('google_create',{'app':'docs'},'d'*64)


@pytest.mark.parametrize('status',['complete','submitted'])
def test_pre_upgrade_upload_identity_is_retained(workspace,status):
    image=workspace.state_dir/'thumbnails/current.png';image.parent.mkdir();image.write_bytes(b'old image')
    sha=g.hashlib.sha256(image.read_bytes()).hexdigest()
    legacy=g.key([workspace.workspace_id,'drive','upload',sha])
    g.atomic_write_json(g.root(workspace)/'jobs'/(legacy+'.json'),{'status':status})
    c=g.config(workspace);c.update(apps=['drive'],upload_thumbnail=True,profile_id='profile')
    steps,_=g.steps_for(workspace,c,{})
    assert steps[1]['once_key']==legacy


def test_auth_hold_ui_does_not_promise_an_automatic_retry():
    from docurandom.google_workspace_ui import status_text
    text=status_text({'status':'account_required','current_app':'docs','current_step':3,'step_count':8,
                      'next_run_at':'tomorrow','detail':'choose account'})
    assert 'Chờ chọn tài khoản'in text and 'Google Docs'in text and '3/8'in text
    assert 'tomorrow'not in text


def test_javascript_readonly_recovery_checks_ownership_and_never_creates_again():
    source=Path(__file__).resolve().parents[1]/'donut-extension/google-workspace.js'
    script=r'''
const vm=require('vm'),fs=require('fs'),crypto=require('crypto').webcrypto;
const storage={},session={googleBrowserSession:'same-session'};let created=0,actions=[];
let current={id:8,url:'https://docs.google.com/document/u/0/',status:'complete'};
const data=store=>({get:async k=>({[k]:store[k]}),set:async v=>Object.assign(store,v)});
const context={URL,TextEncoder,crypto,Date,setTimeout,console,chrome:{storage:{local:data(storage),session:data(session)},
 windows:{create:async()=>{created++;throw Error('must not create');}},
 tabs:{get:async()=>current,query:async()=>[current]},
 scripting:{executeScript:async p=>{actions.push(p.args[0]);return [{result:{status:'ok'}}];}}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const g=context.GoogleWorkspace,req='a'.repeat(64);
storage.googleTaskReceipts={[req]:{command:'google_open',status:'submitted',app:'docs',tab_id:8,
 browser_session_id:'same-session',error:'GOOGLE_AUTH_REQUIRED'}};
(async()=>{
 if(g.urlState('https://www.google.com/sorry/index')!=='verification_required')throw Error('captcha misclassified');
 if(g.urlState('https://accounts.google.com/v3/signin/accountchooser')!=='account_required')throw Error('account misclassified');
 if(g.urlState('https://consent.google.com/')!=='permission_required')throw Error('consent misclassified');
 const recovered=await g.execute('google_job',{request_id:req,observe:true});
 if(recovered.status!=='complete'||created!==0||actions.join()!=='snapshot')throw Error('not readonly recovery');
 storage.googleTaskReceipts[req]={...recovered,command:'google_create',status:'created',url:'https://docs.google.com/document/d/original/edit'};
 current.url='https://docs.google.com/document/d/other/edit';
 const mismatch=await g.execute('google_job',{request_id:req,observe:true});
 if(mismatch.error!=='GOOGLE_TAB_DOCUMENT_CHANGED')throw Error('adopted wrong document');
 current.url='https://docs.google.com/document/d/original/edit';
 try{await g.execute('google_editor_probe',{app:'docs',tab_id:8,owner_request_id:req,action:'set_field'});throw Error('write accepted as readback');}
 catch(e){if(!String(e).includes('READBACK_ACTION_UNSUPPORTED'))throw e;}
 try{await g.execute('google_snapshot',{app:'sheets',tab_id:8,owner_request_id:req});throw Error('wrong app accepted');}
 catch(e){if(!String(e).includes('OWNERSHIP_UNCERTAIN'))throw e;}
 storage.googleTaskReceipts[req]={...recovered,command:'google_create',status:'submitted',url:undefined};
 if((await g.execute('google_job',{request_id:req,observe:true})).error!=='GOOGLE_CREATED_DOCUMENT_UNCERTAIN')throw Error('unknown created ID adopted');
 const inventory=await g.execute('google_inventory',{});
 if(inventory.tabs[0].url_digest.length!==64||inventory.tabs[0].url)throw Error('raw URL leaked in inventory');
 storage.googleTaskReceipts[req]={...recovered,command:'google_open',app:'search',url:'https://www.google.com/search?q=one'};
 current.url='https://www.google.com/search?q=manual';
 const changed=await g.execute('google_inventory',{});
 if(changed.owned_tab_ids.includes(8))throw Error('manual navigation counted as owned');
 try{await g.execute('google_close',{app:'search',tab_id:8,owner_request_id:req});throw Error('closed manually changed tab');}
 catch(e){if(!String(e).includes('DOCUMENT_CHANGED'))throw e;}
 context.location={hostname:'docs.google.com',pathname:'/document/d/exact/edit',href:current.url};
 context.document={body:{innerText:''},querySelector:()=>null,querySelectorAll:()=>[]};
 context.performance={getEntriesByType:()=>[{responseStatus:429}]};
 if((await g.inPage('set_title',{title:'must not write'})).status!=='rate_limited')throw Error('write on rate limited page');
 if(created!==0||actions.join()!=='snapshot')throw Error('unexpected mutation');
 console.log('PASS authenticated observation, same document, readback separation, no duplicate artifact');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([shutil.which('node')or r'C:\Program Files\nodejs\node.exe','-e',script,str(source)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
