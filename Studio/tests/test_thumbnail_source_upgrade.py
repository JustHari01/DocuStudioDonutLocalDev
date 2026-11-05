import json
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from tests.test_thumbnail_isolated_recovery import setup
from docurandom.thumbnail_ai import (read_thumbnail_ai_state,claim_thumbnail_ai_generation,
    reserve_thumbnail_source_probe,reserve_thumbnail_recovery_chat,reserve_thumbnail_recovery_retry,
    thumbnail_recovery_retry_remaining_seconds,update_thumbnail_ai_state)

def test_one_source_only_upgrade_does_not_reset_generation_or_read_counters(tmp_path):
    w,p,claim,proof,data=setup(tmp_path)
    state=read_thumbnail_ai_state(w);state.update(status='error',submitted=True,readonly_probe_count=4)
    (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(state))
    upgrade=claim_thumbnail_ai_generation(w,p)
    assert upgrade.read_only_collection and upgrade.source_only
    assert reserve_thumbnail_source_probe(w,upgrade,state['thread_id'])
    assert not reserve_thumbnail_source_probe(w,upgrade,state['thread_id'])
    after=read_thumbnail_ai_state(w)
    assert after['readonly_probe_count']==4 and after['automatic_retry_count']==3
    assert after['source_read_version']==2
    assert claim_thumbnail_ai_generation(w,p).action=='blocked'

def test_legacy_transient_failure_gets_one_same_recovery_chat_retry_with_source_proof(tmp_path):
    w,p,claim,proof,data=setup(tmp_path)
    with patch('docurandom.storage.app_data_dir',return_value=data):
        assert reserve_thumbnail_recovery_chat(w,p,claim,'old_video_thread','recovery',proof)
        state=read_thumbnail_ai_state(w)
        state['recovery_chat']['policy_version']=2
        state.update(status='terminal_no_image',submitted=False,detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: Công cụ tạo ảnh gặp lỗi khi xử lý yêu cầu.',
            updated_at=(datetime.now(timezone.utc)-timedelta(minutes=25)).isoformat())
        state['recovery_chat']['count']=3
        state['recovery_chat']['reserved_at']=state['updated_at']
        (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(state))
        path=data/'chat-bridge/bridge-state.json';b=json.loads(path.read_text());b['savedTurns']['thread:recovery']={'chatUrl':'https://chatgpt.com/c/'+proof['conversation_id']};path.write_text(json.dumps(b))
        retry=claim_thumbnail_ai_generation(w,p)
        assert retry.action=='retry_recovery_chat'
        assert not reserve_thumbnail_recovery_retry(w,p,retry,'recovery',{**proof,'image_message_ids':['image-exists']})
        assert reserve_thumbnail_recovery_retry(w,p,retry,'recovery',proof)
        assert not reserve_thumbnail_recovery_retry(w,p,retry,'recovery',proof)
        update_thumbnail_ai_state(w,p,retry,status='terminal_no_image',submitted=False,detail=state['detail'])
    after=read_thumbnail_ai_state(w)
    assert after['recovery_chat']['retry_count']==1 and after['automatic_retry_count']==3
    assert after['thread_id']=='recovery'
    assert thumbnail_recovery_retry_remaining_seconds(after)is None

def test_legacy_completed_source_read_does_not_block_final_retry_with_old_read_budget(tmp_path):
    w,p,claim,proof,data=setup(tmp_path)
    with patch('docurandom.storage.app_data_dir',return_value=data):
        assert reserve_thumbnail_recovery_chat(w,p,claim,'old_video_thread','recovery',proof)
        state=read_thumbnail_ai_state(w)
        state['recovery_chat']['policy_version']=2
        state.update(status='terminal_no_image',submitted=False,readonly_probe_count=4,source_read_version=2,
            detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: Công cụ tạo ảnh gặp lỗi khi xử lý yêu cầu.',
            updated_at=datetime.now(timezone.utc).isoformat())
        state['recovery_chat']['count']=3
        state['recovery_chat']['reserved_at']=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
        (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(state))
        path=data/'chat-bridge/bridge-state.json';b=json.loads(path.read_text());b['savedTurns']['thread:recovery']={'chatUrl':'https://chatgpt.com/c/'+proof['conversation_id']};path.write_text(json.dumps(b))
        retry=claim_thumbnail_ai_generation(w,p)
        assert retry.action=='retry_recovery_chat'
        assert not reserve_thumbnail_recovery_retry(w,p,retry,'recovery',{**proof,'complete':False})
        assert not reserve_thumbnail_recovery_retry(w,p,retry,'recovery',{**proof,'image_message_ids':['existing-image']})
        assert reserve_thumbnail_recovery_retry(w,p,retry,'recovery',proof)
    after=read_thumbnail_ai_state(w)
    assert after['readonly_probe_count']==4 and after['automatic_retry_count']==3
    assert after['recovery_chat']['retry_count']==1

def test_real_worker_retries_same_recovery_chat_once_without_opening_another(tmp_path):
    import io,hashlib,unicodedata
    from types import SimpleNamespace
    from PIL import Image
    from tests.test_stalled_thumbnail_repair import recovered_app,run_worker
    from docurandom.thumbnail_ai import thumbnail_generation_request
    app,package,state_path=recovered_app(tmp_path)
    state=read_thumbnail_ai_state(app.workspace)
    state.update(status='terminal_no_image',submitted=False,automatic_retry_count=3,
        detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: Công cụ tạo ảnh gặp lỗi khi xử lý yêu cầu.',
        recovery_chat={'count':3,'new_thread_id':state['thread_id'],'old_thread_id':'older-chat'},
        updated_at=(datetime.now(timezone.utc)-timedelta(minutes=25)).isoformat())
    state_path.write_text(json.dumps(state));claim=claim_thumbnail_ai_generation(app.workspace,package)
    assert claim.action=='retry_recovery_chat'
    digest=hashlib.sha256(' '.join(unicodedata.normalize('NFC',thumbnail_generation_request(claim.prompt)).split()).encode()).hexdigest()
    cid='6aceaf4f-44cc-83ec-a402-61fadf210822';data=tmp_path/'Data';bridge=data/'chat-bridge';bridge.mkdir(parents=True)
    (bridge/'bridge-state.json').write_text(json.dumps({'savedTurns':{'thread:exact-old-thread':{'chatUrl':'https://chatgpt.com/c/'+cid}}}))
    proof={'complete':True,'terminal_no_image':True,'prompt_sha256':digest,'conversation_id':cid,'image_message_ids':[],'matched_user_ids':['u'],'terminal_detail':'Công cụ tạo ảnh gặp lỗi'}
    image=io.BytesIO();Image.new('RGB',(1280,720)).save(image,format='PNG');calls=[]
    class Chat:
        def __init__(self,*args,**kw):self.thread_id=kw['thread_id'];self.response_id='';self._last_existing_image_evidence={'status':'no-image','source_proof':proof}
        def read_existing_generated_image(self,**kw):calls.append(('read',self.thread_id));return None
        def diagnose_session(self):return {}
        def generate_image(self,prompt,**kw):calls.append(('generate',self.thread_id));kw['on_status']('submitted','accepted');return SimpleNamespace(data=image.getvalue(),response_id='image',mime_type='image/png')
        def send(self,*args,**kw):return 'ĐÃ TẠO'
    with patch('docurandom.storage.app_data_dir',return_value=data),patch('docurandom.ui._thumbnail_terminal_retry_idle_signature',return_value=('idle',)):
        run_worker(app,package,Chat)
    assert calls==[('read','exact-old-thread'),('generate','exact-old-thread')]
    after=read_thumbnail_ai_state(app.workspace)
    assert after['status']=='complete'and after['recovery_chat']['retry_count']==1 and after['automatic_retry_count']==3
