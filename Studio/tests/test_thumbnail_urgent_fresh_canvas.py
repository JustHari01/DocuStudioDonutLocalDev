import hashlib
import io
import json
import threading
import unicodedata
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image
from docurandom.ui import _SharedChatGPTTurnPool
from docurandom.thumbnail_ai import (read_thumbnail_ai_state,thumbnail_generation_request,
    thumbnail_canvas_bytes,thumbnail_canvas_request)


def test_two_urgent_repairs_precede_content_but_cannot_starve_it():
    pool=_SharedChatGPTTurnPool(1);assert pool.acquire()
    order=[];threads=[]
    def queue(label,priority):
        waiting=threading.Event()
        def worker():
            assert pool.acquire(label=label,priority=priority,on_wait=lambda *_:waiting.set())
            order.append(label);pool.release()
        t=threading.Thread(target=worker,daemon=True);threads.append(t);t.start()
        assert waiting.wait(2)
    queue('content',False)
    queue('thumbnail-one',True)
    queue('thumbnail-two',True)
    queue('thumbnail-three',True)
    pool.release()
    for t in threads:t.join(2);assert not t.is_alive()
    assert order==['thumbnail-one','thumbnail-two','content','thumbnail-three']
    assert pool.active_count==0


def test_urgent_request_still_respects_provider_cooldown_and_can_be_cancelled():
    clock=[0.0];pool=_SharedChatGPTTurnPool(1,monotonic=lambda:clock[0])
    pool.mark_rate_limited(cooldown_seconds=1200)
    cancelled=threading.Event();observations=[]
    def waiting(position,active,limit):
        observations.append((active,limit));cancelled.set()
    assert not pool.acquire(priority=True,cancel_event=cancelled,on_wait=waiting)
    assert observations==[(0,0)]and pool.active_count==0 and pool.pending_count==0


@pytest.mark.parametrize('unchanged_blank',[False,True])
@pytest.mark.parametrize('generic_after_failed_fresh_chat',[False,True])
def test_real_worker_sends_exact_design_and_valid_png_in_a_new_chat(tmp_path,unchanged_blank,generic_after_failed_fresh_chat):
    from tests.test_stalled_thumbnail_repair import recovered_app,run_worker
    app,p,state_path=recovered_app(tmp_path)
    s=read_thumbnail_ai_state(app.workspace)
    s.update(status='terminal_no_image',submitted=False,automatic_retry_count=0,
        detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: KHÔNG TẠO ĐƯỢC: thiếu ảnh nguồn.',
        updated_at=(datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat())
    if generic_after_failed_fresh_chat:
        s.update(recovery_chat={'count':1,'origin_thread_id':'content-thread'},
            detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: Công cụ tạo ảnh gặp lỗi khi xử lý yêu cầu tạo ảnh mới.')
    state_path.write_text(json.dumps(s))
    from docurandom.thumbnail_ai import claim_thumbnail_ai_generation
    claim=claim_thumbnail_ai_generation(app.workspace,p)
    request=thumbnail_generation_request(claim.prompt)
    sha=hashlib.sha256(' '.join(unicodedata.normalize('NFC',request).split()).encode()).hexdigest()
    data=tmp_path/'Data';bridge=data/'chat-bridge';bridge.mkdir(parents=True)
    cid='6ad183c4-3e4c-83ec-85e9-cf2223170fe2'
    (bridge/'bridge-state.json').write_text(json.dumps({'savedTurns':{'thread:exact-old-thread':{'chatUrl':'https://chatgpt.com/c/'+cid}}}))
    proof={'complete':True,'terminal_no_image':True,'prompt_sha256':sha,
        'conversation_id':cid,'matched_user_ids':['u'],'image_message_ids':[],
        'terminal_detail':'KHÔNG TẠO ĐƯỢC: thiếu ảnh nguồn.'}
    if generic_after_failed_fresh_chat:
        proof['terminal_detail']='KHÔNG TẠO ĐƯỢC: công cụ tạo ảnh gặp lỗi khi xử lý yêu cầu.'
    output=io.BytesIO();Image.new('RGB',(1280,720),(10,30,60)).save(output,format='PNG')
    calls=[]
    class Chat:
        def __init__(self,*args,**kw):
            self.thread_id=kw.get('thread_id')or'fresh-canvas-chat';self.response_id=''
            self._last_existing_image_evidence={'status':'no-image','source_proof':proof}
        def read_existing_generated_image(self,**kw):
            assert kw['expected_prompt']==request
            calls.append(('read',self.thread_id));return None
        def diagnose_session(self):return {}
        def generate_image(self,prompt,**kw):
            assert self.thread_id=='fresh-canvas-chat'
            assert prompt==thumbnail_canvas_request(claim.prompt)
            attachments=kw['attachments'];assert len(attachments)==1
            assert attachments[0].name.endswith('.png')and attachments[0].data==thumbnail_canvas_bytes()
            calls.append(('generate',self.thread_id));kw['on_status']('submitted','accepted')
            return SimpleNamespace(data=thumbnail_canvas_bytes()if unchanged_blank else output.getvalue(),response_id='img',mime_type='image/png')
        def send(self,*args,**kw):return 'ĐÃ TẠO'
    with patch('docurandom.storage.app_data_dir',return_value=data),patch('docurandom.ui._thumbnail_terminal_retry_idle_signature',return_value=('idle',)):
        run_worker(app,p,Chat)
    after=read_thumbnail_ai_state(app.workspace)
    assert calls==[('read','exact-old-thread'),('generate','fresh-canvas-chat')]
    assert after['thread_id']=='fresh-canvas-chat'and after['generation_wire_version']==3
    assert after['canvas_repair']['fresh_chat']is True
    assert app.chatgpt_turn_pool.acquire.call_args.kwargs['priority']is True
    if unchanged_blank:
        assert after['status']!='complete'and not after['thumbnail_path']
    else:
        assert after['status']=='complete'
