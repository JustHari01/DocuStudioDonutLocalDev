import json,hashlib,unicodedata
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from tests.test_thumbnail_isolated_recovery import setup
from docurandom.thumbnail_ai import (read_thumbnail_ai_state,claim_thumbnail_ai_generation,
    record_thumbnail_ai_loading_failure,thumbnail_generation_request,thumbnail_request_for_claim,
    thumbnail_text_to_image_request,thumbnail_prompt_repair_remaining_seconds,reserve_thumbnail_prompt_repair,
    update_thumbnail_ai_state)

def prepared(tmp_path):
    w,p,claim,proof,data=setup(tmp_path)
    state=read_thumbnail_ai_state(w)
    state.update(recovery_chat={'count':3},status='terminal_no_image',submitted=False,readonly_probe_count=28,
        detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: Công cụ tạo ảnh nhận nhầm yêu cầu thành chỉnh sửa ảnh có sẵn.',
        updated_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
    (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(state))
    return w,p,proof,data

def test_backoff_survives_queueing_state_writes_and_does_not_reset_generation_budget(tmp_path):
    w,p,proof,data=prepared(tmp_path)
    claim=claim_thumbnail_ai_generation(w,p)
    assert claim.action=='repair_prompt_transport'
    assert record_thumbnail_ai_loading_failure(w,p,claim,'CHATGPT_CONVERSATION_LOAD_FAILED')
    after=read_thumbnail_ai_state(w)
    assert after['automatic_retry_count']==3 and after['retry_probe_failures']==1
    assert 55<=thumbnail_prompt_repair_remaining_seconds(after)<=60
    assert claim_thumbnail_ai_generation(w,p).action=='blocked'
    update_thumbnail_ai_state(w,p,claim,status='terminal_no_image',submitted=False,detail=after['detail'])
    assert 50<=thumbnail_prompt_repair_remaining_seconds(read_thumbnail_ai_state(w))<=60

def test_wire_repair_needs_original_exact_source_and_is_once_per_material(tmp_path):
    w,p,proof,data=prepared(tmp_path)
    with patch('docurandom.storage.app_data_dir',return_value=data):
        claim=claim_thumbnail_ai_generation(w,p)
        state=read_thumbnail_ai_state(w)
        path=data/'chat-bridge/bridge-state.json';b=json.loads(path.read_text());b['savedTurns']['thread:'+state['thread_id']]={'chatUrl':'https://chatgpt.com/c/'+proof['conversation_id']};path.write_text(json.dumps(b))
        assert not reserve_thumbnail_prompt_repair(w,p,claim,state['thread_id'],{**proof,'complete':False})
        assert not reserve_thumbnail_prompt_repair(w,p,claim,state['thread_id'],{**proof,'image_message_ids':['existing']})
        assert reserve_thumbnail_prompt_repair(w,p,claim,state['thread_id'],proof)
        assert not reserve_thumbnail_prompt_repair(w,p,claim,state['thread_id'],proof)
        after=read_thumbnail_ai_state(w)
        assert after['automatic_retry_count']==3 and after['readonly_probe_count']==28
        assert after['generation_wire_version']==2 and after['wire_repair']['count']==1
        assert after['thread_id']==state['thread_id']
        again=claim_thumbnail_ai_generation(w,p)
        assert thumbnail_request_for_claim(again)==thumbnail_request_for_claim(claim)
        assert after['wire_repair']['previous_request_sha256']==hashlib.sha256(' '.join(unicodedata.normalize('NFC',thumbnail_generation_request(claim.prompt)).split()).encode()).hexdigest()

def test_text_to_image_changes_edit_boilerplate_preserves_japanese_and_layout():
    prompt='1280×720 Photoshop composite. YELLOW: 日本製は高くて遅いw\nCYAN: ブラジル工場\nRED: 三つの山!?'
    new=thumbnail_text_to_image_request(prompt)
    assert 'Photoshop'not in new and 'composite'not in new
    for text in ['1280×720','日本製は高くて遅いw','ブラジル工場','三つの山!?']:assert text in new

def test_real_worker_reads_old_request_then_sends_corrected_request_in_same_chat(tmp_path):
    import io
    from types import SimpleNamespace
    from PIL import Image
    from tests.test_stalled_thumbnail_repair import recovered_app,run_worker
    app,package,state_path=recovered_app(tmp_path)
    state=read_thumbnail_ai_state(app.workspace)
    state.update(recovery_chat={'count':3},status='terminal_no_image',submitted=False,automatic_retry_count=3,readonly_probe_count=9,
        detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: Công cụ tạo ảnh nhận nhầm thành chỉnh sửa ảnh có sẵn.',
        updated_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
    state_path.write_text(json.dumps(state))
    claim=claim_thumbnail_ai_generation(app.workspace,package)
    assert claim.action=='repair_prompt_transport'
    old=thumbnail_generation_request(claim.prompt)
    digest=hashlib.sha256(' '.join(unicodedata.normalize('NFC',old).split()).encode()).hexdigest()
    cid='6aceaf4f-44cc-83ec-a402-61fadf210822';data=tmp_path/'Data';bridge=data/'chat-bridge';bridge.mkdir(parents=True)
    (bridge/'bridge-state.json').write_text(json.dumps({'savedTurns':{'thread:exact-old-thread':{'chatUrl':'https://chatgpt.com/c/'+cid}}}))
    proof={'complete':True,'terminal_no_image':True,'prompt_sha256':digest,'conversation_id':cid,
        'image_message_ids':[],'matched_user_ids':['u'],'terminal_detail':'nhận nhầm chỉnh sửa ảnh có sẵn'}
    image=io.BytesIO();Image.new('RGB',(1280,720)).save(image,format='PNG');calls=[]
    class Chat:
        def __init__(self,*args,**kw):self.thread_id=kw['thread_id'];self.response_id='';self._last_existing_image_evidence={'status':'no-image','source_proof':proof}
        def read_existing_generated_image(self,**kw):calls.append(('read',kw['expected_prompt']));return None
        def diagnose_session(self):return {}
        def generate_image(self,prompt,**kw):
            assert read_thumbnail_ai_state(app.workspace)['wire_repair']['count']==1
            calls.append(('generate',prompt));kw['on_status']('submitted','accepted')
            return SimpleNamespace(data=image.getvalue(),response_id='image',mime_type='image/png')
        def send(self,*args,**kw):return 'ĐÃ TẠO'
    with patch('docurandom.storage.app_data_dir',return_value=data),patch('docurandom.ui._thumbnail_terminal_retry_idle_signature',return_value=('idle',)):
        run_worker(app,package,Chat)
    assert calls==[('read',old),('generate',thumbnail_request_for_claim(claim))]
    after=read_thumbnail_ai_state(app.workspace)
    assert after['status']=='complete' and after['thread_id']=='exact-old-thread'
    assert after['automatic_retry_count']==3 and after['generation_wire_version']==2
