import io,json,threading,queue
import hashlib,unicodedata
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pytest
from PIL import Image
from docurandom.content_ai import ContentAIError,ChatGPTWebChat,ChatGPTWebStatus,ChatGPTGeneratedImage
from docurandom.ui import DocuRandomApp
from docurandom.workspace import Workspace
from docurandom.thumbnail_ai import (claim_thumbnail_ai_generation,update_thumbnail_ai_state,read_thumbnail_ai_state,
    thumbnail_ai_automatic_recovery_remaining_seconds,
    reserve_thumbnail_recovery_probe,release_thumbnail_recovery_probe,reserve_thumbnail_ai_automatic_retry)
from docurandom.thumbnail_ai import record_thumbnail_recovery_receipt,record_unsubmitted_thumbnail_delivery,thumbnail_generation_request


def test_terminal_image_retry_uses_message_identity_and_content_instead_of_react_markup():
    import copy
    from docurandom.ui import _thumbnail_terminal_retry_idle_signature
    thread='thread_docurandom_exact_image'
    url='https://chatgpt.com/c/01234567-89ab-cdef-0123-456789abcdef'
    diagnostic={'thread_id':thread,'saved_url':url,'current_url':url,
        'context_busy':False,'active_controller':False,'pending_submission':False,'pending_attachment':False,
        'page_snapshot':{'authenticated':True,'ready':True},
        'snapshot':{'url':url,'stopControlCount':0,'streamingMarkerCount':0,
            'users':[{'key':'user-1','text':'Create the exact thumbnail'}],
            'assistants':[{'key':'assistant-1','text':'KHÔNG TẠO ĐƯỢC','markdownCount':1,'htmlLength':100}]}}
    baseline=_thumbnail_terminal_retry_idle_signature(diagnostic,thread)
    assert baseline is not None
    hydrated=copy.deepcopy(diagnostic)
    hydrated['snapshot']['assistants'][0].update(htmlLength=3000,markdownCount=2)
    assert _thumbnail_terminal_retry_idle_signature(hydrated,thread)==baseline
    hydrated['snapshot']['users'][0]['text']='An edited or different prompt'
    assert _thumbnail_terminal_retry_idle_signature(hydrated,thread)!=baseline
    hydrated=copy.deepcopy(diagnostic);hydrated['snapshot']['assistants'][0]['key']='new-assistant'
    assert _thumbnail_terminal_retry_idle_signature(hydrated,thread)!=baseline
    hydrated=copy.deepcopy(diagnostic);hydrated['snapshot']['stopControlCount']=1
    assert _thumbnail_terminal_retry_idle_signature(hydrated,thread) is None
    hydrated=copy.deepcopy(diagnostic);hydrated['pending_submission']=True
    assert _thumbnail_terminal_retry_idle_signature(hydrated,thread) is None

def absence_proof(claim,thread='exact-old-thread'):
    return {'safe_to_resend':True,'thread_id':thread,'prompt_sha256':hashlib.sha256(' '.join(unicodedata.normalize('NFC',thumbnail_generation_request(claim.prompt)).split()).encode()).hexdigest()}

@pytest.mark.parametrize('safe',[False,True,'transport-failed'])
def test_transport_preserves_only_native_safe_unsent_proof_after_its_single_resend(tmp_path,safe):
    requests=[]
    message='ChatGPT chưa xác nhận lệnh gửi sau khi chờ và kiểm tra an toàn; tool dừng để tránh gửi trùng. Checkpoint vẫn được giữ.'
    class Response:
        status=200
        def __init__(self):self.lines=[(json.dumps({'type':'error','error':{'message':message}})+'\n').encode()]
        def readline(self,*a):return self.lines.pop(0) if self.lines else b''
        def getheader(self,name,default=None):return 'text/event-stream' if name.lower()=='content-type' else default
        def close(self):pass
    class Connection:
        def __init__(self,*a,**kw):pass
        def request(self,*a,**kw):requests.append(json.loads(kw['body']))
        def getresponse(self):return Response()
        def close(self):pass
    chat=ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/high',tmp_path,'t'*48)
    recovery_error=ContentAIError('CHATGPT_SUBMISSION_TRANSPORT_UNCERTAIN: HTTP=200;net=net::ERR_FAILED') if safe=='transport-failed' else None
    with patch('docurandom.content_ai.http.client.HTTPConnection',Connection),patch.object(chat,'recover_pending',return_value={'status':'not-submitted','safe_to_resend':safe is True},side_effect=recovery_error):
        with pytest.raises(ContentAIError) as caught:chat.generate_image('Owned image prompt',on_status=lambda *a:None)
    assert len(requests)==(2 if safe is True else 1)
    proof=getattr(caught.value,'unsubmitted_image_proof',None)
    assert bool(proof)==(safe is True)
    if safe=='transport-failed':assert 'CHATGPT_SUBMISSION_TRANSPORT_UNCERTAIN' in str(caught.value)
    if safe is True:
        assert proof['thread_id']==chat.thread_id and proof['prompt_sha256']==hashlib.sha256(b'Owned image prompt').hexdigest()
        assert requests[0]['input']==requests[1]['input']

def test_native_absence_proof_allows_only_one_durable_delivery_repair(tmp_path):
    app,package,path=recovered_app(tmp_path);claim=claim_thumbnail_ai_generation(app.workspace,package)
    raw=package.read_bytes();assert not record_unsubmitted_thumbnail_delivery(app.workspace,package,claim,'exact-old-thread',absence_proof(claim,'wrong-thread'))
    assert record_unsubmitted_thumbnail_delivery(app.workspace,package,claim,'exact-old-thread',absence_proof(claim))
    saved=read_thumbnail_ai_state(app.workspace);assert saved['status']=='delivery_retry_pending' and saved['submitted'] is False
    assert claim_thumbnail_ai_generation(app.workspace,package).action=='blocked'
    saved['updated_at']=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat();path.write_text(json.dumps(saved),encoding='utf-8')
    assert claim_thumbnail_ai_generation(app.workspace,package).action=='start'
    assert read_thumbnail_ai_state(app.workspace)['delivery_retry_count']==1
    assert record_unsubmitted_thumbnail_delivery(app.workspace,package,claim,'exact-old-thread',absence_proof(claim))
    assert read_thumbnail_ai_state(app.workspace)['status']=='delivery_failed'
    assert thumbnail_ai_automatic_recovery_remaining_seconds(read_thumbnail_ai_state(app.workspace)) is None
    assert claim_thumbnail_ai_generation(app.workspace,package).action=='blocked'
    assert package.read_bytes()==raw

def test_new_receipt_has_its_own_read_budget_without_resetting_generation_limit(tmp_path):
    app,package,path=recovered_app(tmp_path);claim=claim_thumbnail_ai_generation(app.workspace,package)
    saved=read_thumbnail_ai_state(app.workspace);saved.update(status='uncertain',submitted=True,automatic_retry_count=3,recovery_receipt_created_at=1000,recovery_probe_count=4,readonly_probe_count=4)
    path.write_text(json.dumps(saved),encoding='utf-8')
    assert record_thumbnail_recovery_receipt(app.workspace,claim,'exact-old-thread',2000)
    saved=read_thumbnail_ai_state(app.workspace);assert saved['recovery_probe_count']==0 and saved['readonly_probe_count']==0 and saved['automatic_retry_count']==3
    assert reserve_thumbnail_recovery_probe(app.workspace,claim,'exact-old-thread')==1
    record_thumbnail_recovery_receipt(app.workspace,claim,'exact-old-thread',2000)
    saved=read_thumbnail_ai_state(app.workspace);assert saved['recovery_probe_count']==1
    saved['updated_at']=(datetime.now(timezone.utc)-timedelta(minutes=40)).isoformat();path.write_text(json.dumps(saved),encoding='utf-8')
    assert claim_thumbnail_ai_generation(app.workspace,package).read_only_collection is False

def test_capped_generation_only_reads_existing_receipt_and_never_stops_or_generates(tmp_path):
    app,package,path=recovered_app(tmp_path)
    saved=read_thumbnail_ai_state(app.workspace);saved.update(automatic_retry_count=3,recovery_receipt_created_at=1000)
    path.write_text(json.dumps(saved),encoding='utf-8');calls=[]
    class Chat:
        def __init__(self,*args,**kw):self.thread_id=kw['thread_id'];self.response_id=''
        def read_existing_generated_image(self,**kwargs):return None
        def diagnose_session(self):return {'pending_submission_details':{'created_at':1000,'output_mode':'generated_image'}}
        def recover_pending(self,**kwargs):calls.append('read');raise ContentAIError('CHATGPT_RECOVERY_PENDING: waiting')
        def recover_image_with_repair(self,*args,**kw):pytest.fail('No Stop/grant when generation is capped')
        def generate_image(self,*args,**kw):pytest.fail('No additional image')
    run_worker(app,package,Chat)
    assert calls==['read'] and read_thumbnail_ai_state(app.workspace)['submitted'] is True

def test_worker_keeps_native_unsent_proof_instead_of_false_submitted_lock(tmp_path):
    app,package,path=recovered_app(tmp_path);saved=read_thumbnail_ai_state(app.workspace);saved.update(status='error',submitted=False);path.write_text(json.dumps(saved),encoding='utf-8')
    claim=claim_thumbnail_ai_generation(app.workspace,package)
    class Chat:
        def __init__(self,*args,**kw):self.thread_id=kw['thread_id'];self.response_id=''
        def read_existing_generated_image(self,**kwargs):return None
        def generate_image(self,*args,**kw):
            kw['on_status']('submission-checkpointed','dispatch')
            error=ContentAIError('Delivery failed');error.unsubmitted_image_proof=absence_proof(claim);raise error
    # Restore the pre-submit state after constructing its request identity.
    saved=read_thumbnail_ai_state(app.workspace);saved.update(status='error',submitted=False);path.write_text(json.dumps(saved),encoding='utf-8')
    run_worker(app,package,Chat)
    saved=read_thumbnail_ai_state(app.workspace)
    assert saved['status']=='delivery_retry_pending' and saved['submitted'] is False

def waiting_app(tmp_path,submitted):
    workspace=Workspace('repair','Repair',str(tmp_path/'workspace'),'','');workspace.ensure_layout()
    package=workspace.output_dir/'Video.txt'
    package.write_text('TIÊU ĐỀ\nVideo\n\nPROMPT THUMBNAIL\nCreate one documentary thumbnail.\n',encoding='utf-8')
    claim=claim_thumbnail_ai_generation(workspace,package)
    update_thumbnail_ai_state(workspace,package,claim,status='submitted',submitted=True,thread_id='exact-old-thread')
    saved=read_thumbnail_ai_state(workspace);saved['isolated_chat']={'policy_version':1,'prompt_sha256':claim.prompt_sha256,'content_thread_id':'content-thread','origin_thread_id':'exact-old-thread'}
    (workspace.state_dir/'thumbnail-ai.json').write_text(json.dumps(saved),encoding='utf-8')
    app=DocuRandomApp.__new__(DocuRandomApp);app.workspace=workspace
    app.closed=app.closing_after_cancel=app.thumbnail_ai_busy=False
    app.youtube_thumbnail_auto_ai_var=Mock();app.youtube_thumbnail_auto_ai_var.get.return_value=True
    for name in ('_effective_youtube_thumbnail','_schedule_thumbnail_ai_retry','_check_codex_async','_notify_remote','_append_log'):
        setattr(app,name,Mock())
    app._effective_youtube_thumbnail.return_value=None
    return app,package

def ready_status():
    return ChatGPTWebStatus(launcher_path=Path('bridge.exe'),installed=True,launcher_running=True,
        setup_complete=True,authenticated=True,runtime_ready=True,endpoint='http://127.0.0.1:17841',
        available_models=('chatgpt-web/auto',),access_token='fixture')

def recovered_app(tmp_path):
    app,package=waiting_app(tmp_path,True)
    state=app.workspace.state_dir/'thumbnail-ai.json'
    value=json.loads(state.read_text());value['updated_at']=(datetime.now(timezone.utc)-timedelta(minutes=20)).isoformat()
    state.write_text(json.dumps(value),encoding='utf-8')
    app.content_cli_status=ready_status()
    app.content_snapshot={'chatgpt_web_model':'chatgpt-web/auto'}
    app.thumbnail_ai_token=0;app.thumbnail_ai_cancel_event=threading.Event();app.thumbnail_ai_chat=None
    app.chatgpt_turn_pool=Mock();app.chatgpt_turn_pool.acquire.return_value=True
    app.events=queue.Queue();app._refresh_thumbnail_status=Mock();app._append_exception_log=Mock()
    app._apply_thumbnail_selection=Mock();app._cancel_thumbnail_ai_retry=Mock()
    app._resolve_video_chat_context=Mock(return_value=('content-thread',''))
    return app,package,state

def run_worker(app,package,chat):
    with patch('docurandom.ui.ChatGPTWebChat',chat),patch('docurandom.ui.threading.Thread') as thread:
        thread.return_value.start.side_effect=lambda:thread.call_args.kwargs['target']()
        app._start_chatgpt_thumbnail_generation(package)

def test_real_thumbnail_worker_reserves_one_replacement_after_bridge_grant(tmp_path):
    app,package,state=recovered_app(tmp_path);raw=package.read_bytes()
    video=app.workspace.output_dir/'Video.mp4';video.write_bytes(b'keep-rendered-video')
    output=io.BytesIO();Image.new('RGB',(1280,720),(100,150,200)).save(output,format='PNG')
    calls=[]
    class Chat:
        def __init__(self,*args,**kw):self.thread_id=kw['thread_id'];self.response_id='';calls.append(('chat',self.thread_id))
        def read_existing_generated_image(self,**kwargs):return None
        def diagnose_session(self):return {'pending_submission_details':{'created_at':1000}}
        def recover_image_with_repair(self,*args,**kw):return {'safe_to_retry':True,'receipt_preserved':True}
        def generate_image(self,*args,**kw):
            calls.append(('generate',self.thread_id));kw['on_status']('submitted','exact-user-prompt')
            return SimpleNamespace(data=output.getvalue(),response_id='image')
        def send(self,*args,**kw):return 'ĐÃ TẠO'
    run_worker(app,package,Chat)
    after=read_thumbnail_ai_state(app.workspace)
    assert after['status']=='complete' and after['automatic_retry_count']==1
    assert after['recovery_probe_count']==0
    assert calls.count(('generate','exact-old-thread'))==1
    assert Path(after['thumbnail_path']).is_file()
    assert package.read_bytes()==raw and video.read_bytes()==b'keep-rendered-video'
    app.chatgpt_turn_pool.release.assert_called_once()

@pytest.mark.parametrize('error',['CHATGPT_RATE_LIMITED: HTTP 429: retry-after-seconds=3600','CHATGPT_IMAGE_RECOVERY_PENDING: not proved'])
def test_unproved_or_rate_limited_receipt_never_generates_another_image(tmp_path,error):
    app,package,state=recovered_app(tmp_path)
    calls=[]
    class Chat:
        def __init__(self,*args,**kw):self.thread_id=kw['thread_id'];self.response_id=''
        def read_existing_generated_image(self,**kwargs):return None
        def diagnose_session(self):return {'pending_submission_details':{'created_at':1000}}
        def recover_image_with_repair(self,*args,**kw):raise ContentAIError(error)
        def generate_image(self,*args,**kw):calls.append('send');pytest.fail('must not send')
    run_worker(app,package,Chat)
    after=read_thumbnail_ai_state(app.workspace)
    assert not calls and after['submitted'] is True and after['automatic_retry_count']==0
    assert after['recovery_probe_count']==(0 if '429' in error else 1)
    app.chatgpt_turn_pool.release.assert_called_once()

def test_recovery_probe_budget_is_durable_and_quota_can_release_its_reservation(tmp_path):
    app,package,state=recovered_app(tmp_path)
    claim=claim_thumbnail_ai_generation(app.workspace,package)
    assert reserve_thumbnail_recovery_probe(app.workspace,claim,'exact-old-thread')==1
    release_thumbnail_recovery_probe(app.workspace,claim,'exact-old-thread',1)
    assert read_thumbnail_ai_state(app.workspace)['recovery_probe_count']==0
    for count in range(1,5):assert reserve_thumbnail_recovery_probe(app.workspace,claim,'exact-old-thread')==count
    assert reserve_thumbnail_recovery_probe(app.workspace,claim,'exact-old-thread') is None
    saved=read_thumbnail_ai_state(app.workspace);saved['updated_at']=(datetime.now(timezone.utc)-timedelta(minutes=40)).isoformat()
    state.write_text(json.dumps(saved),encoding='utf-8')
    assert claim_thumbnail_ai_generation(app.workspace,package).read_only_collection is True

def test_completed_reply_is_kept_and_never_replaced_by_repair_client():
    chat=ChatGPTWebChat.__new__(ChatGPTWebChat)
    chat.repair_stalled_image_turn=Mock(return_value={'safe_to_retry':False})
    expected={'generated_image':{'data':'saved'}}
    chat.recover_pending=Mock(return_value=expected)
    assert chat.recover_image_with_repair('prompt',1000)==expected
    assert chat.repair_stalled_image_turn.call_count==1

def test_missing_completed_receipt_only_reads_and_cannot_authorize_new_send():
    chat=ChatGPTWebChat.__new__(ChatGPTWebChat)
    chat.repair_stalled_image_turn=Mock(side_effect=AssertionError('No receipt means no Stop or grant'))
    chat.recover_pending=Mock(return_value={'status':'not-submitted','safe_to_resend':False})
    assert chat.recover_image_with_repair('owned prompt',None)['safe_to_resend'] is False
    chat.recover_pending.assert_called_once()
    chat.repair_stalled_image_turn.assert_not_called()

@pytest.mark.parametrize('error', ['CHATGPT_SUBMISSION_UNCONFIRMED: echo missing',
                                  'CHATGPT_RATE_LIMITED: HTTP 429; retry-after-seconds=90'])
def test_dispatched_image_without_echo_enters_bounded_recovery_instead_of_presubmit_retry(tmp_path,error):
    app,package,state=recovered_app(tmp_path)
    saved=json.loads(state.read_text());saved.update(status='error',submitted=False)
    state.write_text(json.dumps(saved),encoding='utf-8')
    calls=[]
    class Chat:
        def __init__(self,*args,**kw):self.thread_id=kw['thread_id'];self.response_id=''
        def read_existing_generated_image(self,**kwargs):return None
        def generate_image(self,*args,**kw):
            calls.append('generate')
            kw['on_status']('submission-checkpointed','Trusted dispatch checkpoint saved; waiting for echo')
            raise ContentAIError(error)
        def diagnose_session(self):return {'pending_submission_details':{'created_at':1000}}
        def recover_image_with_repair(self,*args,**kw):
            calls.append('recover');raise ContentAIError('CHATGPT_IMAGE_RECOVERY_PENDING: receipt not proved')
    run_worker(app,package,Chat)
    after=read_thumbnail_ai_state(app.workspace)
    assert after['submitted'] is True and after['status']=='error'
    assert claim_thumbnail_ai_generation(app.workspace,package).action=='blocked'
    assert calls==['generate']
    if '429' in error:
        app.chatgpt_turn_pool.mark_rate_limited.assert_called_once()
    # Failed echo never resets to a fresh send when the application restarts.
    for attempt in range(4):
        app.thumbnail_ai_busy=False
        saved=read_thumbnail_ai_state(app.workspace)
        saved['updated_at']=(datetime.now(timezone.utc)-timedelta(minutes=40)).isoformat()
        state.write_text(json.dumps(saved),encoding='utf-8')
        run_worker(app,package,Chat)
    after=read_thumbnail_ai_state(app.workspace)
    assert after['recovery_probe_count']==4
    saved=after;saved['updated_at']=(datetime.now(timezone.utc)-timedelta(minutes=40)).isoformat()
    state.write_text(json.dumps(saved),encoding='utf-8')
    assert claim_thumbnail_ai_generation(app.workspace,package).read_only_collection is True
    assert calls.count('generate')==1 and calls.count('recover')==4


def test_generated_bytes_survive_failed_confirmation_and_resume_without_regenerating(tmp_path):
    app,package,state=recovered_app(tmp_path)
    output=io.BytesIO();Image.new('RGB',(1280,720),(30,110,160)).save(output,format='PNG')
    payload=output.getvalue();calls=[]
    class Chat:
        def __init__(self,*args,**kw):self.thread_id=kw['thread_id'];self.response_id='image-owned'
        def read_existing_generated_image(self,**kwargs):return None
        def diagnose_session(self):return {'pending_submission_details':{'created_at':1000}}
        def recover_image_with_repair(self,*args,**kw):return {'safe_to_retry':True,'receipt_preserved':True}
        def generate_image(self,*args,**kw):
            calls.append('generate');kw['on_status']('submitted','exact-user-prompt')
            return ChatGPTGeneratedImage(payload,'image/png',1280,720,'','image-owned')
        def send(self,*args,**kw):
            attachments=kw['attachments'];calls.append(('confirm',len(attachments)))
            if not attachments:return 'KHÔNG TẠO ĐƯỢC: Ảnh chưa hiển thị'
            assert attachments[0].data==payload
            raise ContentAIError('CHATGPT_CONVERSATION_LOAD_FAILED: confirmation pending')
    run_worker(app,package,Chat)
    after=read_thumbnail_ai_state(app.workspace)
    assert after['status']=='confirmation_pending' and not after['thumbnail_path']
    candidate=Path(after['staged_image']['path']);assert candidate.read_bytes()==payload
    # A process can exit during confirmation rather than through the error
    # handler. Both persisted intermediate states must schedule recovery.
    for persisted_status in ('image_received','confirming'):
        interrupted=dict(after,status=persisted_status)
        interrupted['updated_at']=(datetime.now(timezone.utc)-timedelta(minutes=40)).isoformat()
        assert thumbnail_ai_automatic_recovery_remaining_seconds(interrupted)==0
    after['updated_at']=(datetime.now(timezone.utc)-timedelta(minutes=40)).isoformat()
    state.write_text(json.dumps(after),encoding='utf-8');app.thumbnail_ai_busy=False
    class Resume:
        def __init__(self,*args,**kw):pytest.fail('Validated owned bytes must not require a new provider request')
    app.content_cli_status=None
    app.chatgpt_turn_pool.acquire.reset_mock()
    run_worker(app,package,Resume)
    after=read_thumbnail_ai_state(app.workspace)
    assert after['status']=='complete' and Path(after['thumbnail_path']).is_file()
    assert calls==['generate',('confirm',0),('confirm',1)]
    app.chatgpt_turn_pool.acquire.assert_not_called()
    app._apply_thumbnail_selection.assert_called_once()
    assert candidate.read_bytes()==payload and after['automatic_retry_count']==1

def test_changed_candidate_blocks_installation_even_when_provider_budget_is_exhausted(tmp_path):
    from docurandom.thumbnail_ai import stage_thumbnail_candidate
    app,package,state=recovered_app(tmp_path)
    claim=claim_thumbnail_ai_generation(app.workspace,package)
    payload=io.BytesIO();Image.new('RGB',(1280,720)).save(payload,format='PNG')
    path=stage_thumbnail_candidate(app.workspace,claim,'exact-old-thread','owned',payload.getvalue())
    path.write_bytes(b'changed-image-bytes')
    saved=read_thumbnail_ai_state(app.workspace);saved.update(status='confirmation_pending',recovery_probe_count=4,readonly_probe_count=4)
    state.write_text(json.dumps(saved),encoding='utf-8');app.content_cli_status=None
    app._start_chatgpt_thumbnail_generation(package)
    app._apply_thumbnail_selection.assert_not_called();app.chatgpt_turn_pool.acquire.assert_not_called()
    after=read_thumbnail_ai_state(app.workspace)
    assert after['recovery_probe_count']==4 and not after['thumbnail_path']
