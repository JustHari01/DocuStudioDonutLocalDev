import io
import json
import hashlib
import unicodedata
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from docurandom.thumbnail_ai import (
    ThumbnailAIError, read_thumbnail_ai_state, claim_thumbnail_ai_generation,
    thumbnail_text_to_image_request, thumbnail_canvas_request, thumbnail_canvas_bytes,
    thumbnail_request_for_claim, reserve_thumbnail_canvas_repair, thumbnail_canvas_attachments,
    validate_thumbnail_canvas_result, update_thumbnail_ai_state, record_thumbnail_ai_loading_failure,
    thumbnail_canvas_repair_remaining_seconds,
    thumbnail_generation_request,
)
from tests.test_thumbnail_isolated_recovery import setup


def digest(text):
    return hashlib.sha256(' '.join(unicodedata.normalize('NFC', text).split()).encode()).hexdigest()


def prepared(tmp_path):
    w, p, claim, proof, data = setup(tmp_path)
    state = read_thumbnail_ai_state(w)
    state.update(recovery_chat={'count':3},status='terminal_no_image', submitted=False, automatic_retry_count=3, readonly_probe_count=9,
        generation_wire_version=2, wire_repair={'count': 1},
        detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: Công cụ nhận nhầm thành chỉnh sửa ảnh có sẵn, thiếu ảnh nguồn.',
        updated_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
    (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(state))
    path = data/'chat-bridge/bridge-state.json'; bridge = json.loads(path.read_text())
    bridge['savedTurns']['thread:'+state['thread_id']]={'chatUrl':'https://chatgpt.com/c/'+proof['conversation_id']}
    path.write_text(json.dumps(bridge))
    proof = {**proof, 'prompt_sha256': digest(thumbnail_text_to_image_request(claim.prompt))}
    return w, p, proof, data


def test_canvas_reservation_requires_finished_exact_turn_and_survives_restart(tmp_path):
    w, p, proof, data = prepared(tmp_path)
    with patch('docurandom.storage.app_data_dir', return_value=data):
        claim = claim_thumbnail_ai_generation(w, p)
        assert claim.action == 'repair_image_source'
        old = read_thumbnail_ai_state(w); thread = old['thread_id']
        for wrong in ({**proof,'complete':False}, {**proof,'image_message_ids':['found']},
                      {**proof,'prompt_sha256':'f'*64}, {**proof,'terminal_detail':'quota reached'}):
            assert not reserve_thumbnail_canvas_repair(w,p,claim,thread,wrong)
        assert reserve_thumbnail_canvas_repair(w,p,claim,thread,proof)
        assert not reserve_thumbnail_canvas_repair(w,p,claim,thread,proof)
        after = read_thumbnail_ai_state(w)
        assert after['thread_id']==thread and after['generation_wire_version']==3
        assert after['automatic_retry_count']==3 and after['readonly_probe_count']==9
        assert after['canvas_repair']['request_sha256']==digest(thumbnail_canvas_request(claim.prompt))
        resumed=claim_thumbnail_ai_generation(w,p)
        assert thumbnail_request_for_claim(resumed)==thumbnail_request_for_claim(claim)
        assets=thumbnail_canvas_attachments(w,resumed)
        assert len(assets)==1 and assets[0].data==thumbnail_canvas_bytes()
        assert assets[0].mime_type=='image/png'
        update_thumbnail_ai_state(w,p,resumed,status='terminal_no_image',submitted=False,
            detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: thiếu ảnh nguồn')
        assert read_thumbnail_ai_state(w)['canvas_repair']['count']==1
        assert claim_thumbnail_ai_generation(w,p).action=='blocked'


def test_canvas_is_never_a_publishable_result_and_corruption_cannot_be_sent(tmp_path):
    w,p,proof,data=prepared(tmp_path)
    with patch('docurandom.storage.app_data_dir',return_value=data):
        claim=claim_thumbnail_ai_generation(w,p)
        assert reserve_thumbnail_canvas_repair(w,p,claim,read_thumbnail_ai_state(w)['thread_id'],proof)
        with pytest.raises(ThumbnailAIError,match='khung trống'):
            validate_thumbnail_canvas_result(w,claim,thumbnail_canvas_bytes())
        differently_encoded=io.BytesIO();Image.new('RGB',(1280,720),(236,236,236)).save(differently_encoded,format='PNG',compress_level=1)
        with pytest.raises(ThumbnailAIError,match='khung trống'):
            validate_thumbnail_canvas_result(w,claim,differently_encoded.getvalue())
        (w.state_dir/'thumbnail-source-canvas-v3.png').write_bytes(b'corrupt')
        with pytest.raises(ThumbnailAIError,match='nguyên vẹn'):
            thumbnail_canvas_attachments(w,claim)


def test_failed_source_read_backs_off_without_spending_canvas_attempt(tmp_path):
    w,p,proof,data=prepared(tmp_path)
    claim=claim_thumbnail_ai_generation(w,p)
    assert record_thumbnail_ai_loading_failure(w,p,claim,'CHATGPT_CONVERSATION_LOAD_FAILED')
    after=read_thumbnail_ai_state(w)
    assert not after.get('canvas_repair') and after['generation_wire_version']==2
    assert 55<=thumbnail_canvas_repair_remaining_seconds(after)<=60
    assert claim_thumbnail_ai_generation(w,p).action=='blocked'


def test_real_worker_reads_old_source_before_sending_canvas_in_same_chat(tmp_path):
    from tests.test_stalled_thumbnail_repair import recovered_app,run_worker
    app,package,state_path=recovered_app(tmp_path)
    state=read_thumbnail_ai_state(app.workspace)
    state.update(recovery_chat={'count':3},status='terminal_no_image',submitted=False,automatic_retry_count=3,readonly_probe_count=9,
        generation_wire_version=2,wire_repair={'count':1},
        detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: nhận nhầm thành chỉnh sửa ảnh có sẵn, thiếu ảnh nguồn.',
        updated_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
    state_path.write_text(json.dumps(state))
    claim=claim_thumbnail_ai_generation(app.workspace,package)
    cid='6aceaf4f-44cc-83ec-a402-61fadf210822';data=tmp_path/'Data';bridge=data/'chat-bridge';bridge.mkdir(parents=True)
    (bridge/'bridge-state.json').write_text(json.dumps({'savedTurns':{'thread:exact-old-thread':{'chatUrl':'https://chatgpt.com/c/'+cid}}}))
    old=thumbnail_text_to_image_request(claim.prompt)
    proof={'complete':True,'terminal_no_image':True,'prompt_sha256':digest(old),'conversation_id':cid,
        'image_message_ids':[],'matched_user_ids':['u'],'terminal_detail':'thiếu ảnh nguồn'}
    result=io.BytesIO();Image.new('RGB',(1280,720),(80,120,200)).save(result,format='PNG');calls=[]
    class Chat:
        def __init__(self,*args,**kw):
            self.thread_id=kw['thread_id'];self.response_id='';self._last_existing_image_evidence={'status':'no-image','source_proof':proof}
        def read_existing_generated_image(self,**kw):calls.append(('read',kw['expected_prompt']));return None
        def diagnose_session(self):return {}
        def generate_image(self,prompt,**kw):
            assert read_thumbnail_ai_state(app.workspace)['canvas_repair']['count']==1
            assert kw['attachments'][0].data==thumbnail_canvas_bytes()
            calls.append(('generate',prompt));kw['on_status']('submitted','accepted')
            return SimpleNamespace(data=result.getvalue(),response_id='new-image',mime_type='image/png')
        def send(self,*args,**kw):return 'ĐÃ TẠO'
    with patch('docurandom.thumbnail_contract.review_image',side_effect=lambda w,p,b,**k:b) as review, patch('docurandom.storage.app_data_dir',return_value=data),patch('docurandom.ui._thumbnail_terminal_retry_idle_signature',return_value=('idle',)):
        run_worker(app,package,Chat)
        assert review.call_args.kwargs['enforce'] is True
    assert calls==[('read',old),('generate',thumbnail_canvas_request(claim.prompt))]
    after=read_thumbnail_ai_state(app.workspace)
    assert after['status']=='complete' and after['thread_id']=='exact-old-thread'
    assert after['automatic_retry_count']==3 and after['canvas_repair']['count']==1


def test_generated_image_transport_keeps_exact_attachment_bytes():
    from docurandom.content_ai import ChatGPTWebChat, ChatAttachment
    client=object.__new__(ChatGPTWebChat)
    attachments=(ChatAttachment('canvas.png',thumbnail_canvas_bytes(),'image/png'),)
    with patch.object(ChatGPTWebChat,'_send_response',return_value={})as send,patch.object(ChatGPTWebChat,'parse_generated_image_response',return_value='result'):
        assert client.generate_image('exact prompt',attachments=attachments)=='result'
        assert send.call_args.kwargs['attachments']==attachments
        assert send.call_args.kwargs['output_mode']=='generated_image'
        send.reset_mock()
        assert client.generate_image('ordinary image')=='result'
        assert 'attachments'not in send.call_args.kwargs


@pytest.mark.parametrize('wire_version',[1,2])
def test_historical_read_throttle_does_not_permanently_disable_image_repair(tmp_path,wire_version):
    w,p,proof,data=prepared(tmp_path)
    state=read_thumbnail_ai_state(w)
    state.update(generation_wire_version=wire_version,automatic_retry_count=1)
    state['detail']+='\nĐọc lại hội thoại: CHATGPT_RATE_LIMITED: HTTP 429; retry-after-seconds=30'
    (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(state))
    claim=claim_thumbnail_ai_generation(w,p)
    assert claim.action=='repair_image_source'
    if wire_version==1:proof['prompt_sha256']=digest(thumbnail_generation_request(claim.prompt))
    with patch('docurandom.storage.app_data_dir',return_value=data):
        assert reserve_thumbnail_canvas_repair(w,p,claim,read_thumbnail_ai_state(w)['thread_id'],proof)
    # A real generation quota is still blocked; only the old READ suffix is ignored.
    state.update(detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: quota exceeded',canvas_repair=None)
    (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(state))
    assert thumbnail_canvas_repair_remaining_seconds(state)is None
