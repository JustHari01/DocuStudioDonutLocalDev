import json
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from tests.test_thumbnail_canvas_repair import prepared,digest
from docurandom.thumbnail_ai import (claim_thumbnail_ai_generation,read_thumbnail_ai_state,
    reserve_thumbnail_canvas_repair,thumbnail_canvas_request,reserve_thumbnail_canvas_transport,
    thumbnail_canvas_attachments,thumbnail_canvas_transport_request,update_thumbnail_ai_state)


def test_repair_wrong_extension_once_only_after_exact_finished_negative(tmp_path):
    w,p,proof,data=prepared(tmp_path)
    with patch('docurandom.storage.app_data_dir',return_value=data):
        claim=claim_thumbnail_ai_generation(w,p);thread=read_thumbnail_ai_state(w)['thread_id']
        assert reserve_thumbnail_canvas_repair(w,p,claim,thread,proof)
        state=read_thumbnail_ai_state(w)
        state.update(recovery_chat={'count':3},status='terminal_no_image',submitted=False,
            detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: PNG mang đuôi .txt, không nhận diện được ảnh nguồn.',
            updated_at=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat())
        (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(state))
        repair=claim_thumbnail_ai_generation(w,p);assert repair.action=='repair_canvas_transport'
        exact={**proof,'prompt_sha256':digest(thumbnail_canvas_request(repair.prompt)),
            'terminal_detail':'PNG mang đuôi .txt, không nhận diện được ảnh nguồn.'}
        assert not reserve_thumbnail_canvas_transport(w,p,repair,thread,{**exact,'complete':False})
        assert not reserve_thumbnail_canvas_transport(w,p,repair,thread,{**exact,'image_message_ids':['existing']})
        assert not reserve_thumbnail_canvas_transport(w,p,repair,thread,{**exact,'terminal_detail':'quota limit'})
        assert reserve_thumbnail_canvas_transport(w,p,repair,thread,exact)
        assert not reserve_thumbnail_canvas_transport(w,p,repair,thread,exact)
        after=read_thumbnail_ai_state(w)
        assert after['generation_wire_version']==4 and after['canvas_repair']['count']==1 and after['canvas_transport_repair']['count']==1
        assert after['automatic_retry_count']==3 and thumbnail_canvas_attachments(w,repair)[0].name.endswith('.png')
        resumed=claim_thumbnail_ai_generation(w,p)
        assert resumed.generation_request==thumbnail_canvas_transport_request(repair.prompt)
        update_thumbnail_ai_state(w,p,repair,status='terminal_no_image',submitted=False,detail=state['detail'])
        assert claim_thumbnail_ai_generation(w,p).action=='blocked'


def test_real_worker_corrects_only_the_failed_canvas_file_transport(tmp_path):
    import io
    from types import SimpleNamespace
    from PIL import Image
    from tests.test_stalled_thumbnail_repair import recovered_app,run_worker
    from docurandom.thumbnail_ai import thumbnail_canvas_bytes
    app,package,state_path=recovered_app(tmp_path)
    state=read_thumbnail_ai_state(app.workspace);canvas_path=app.workspace.state_dir/'thumbnail-source-canvas-v3.png'
    canvas=thumbnail_canvas_bytes();canvas_path.write_bytes(canvas)
    state.update(recovery_chat={'count':3},status='terminal_no_image',submitted=False,generation_wire_version=3,
        canvas_repair={'count':1,'canvas_path':str(canvas_path),'canvas_sha256':__import__('hashlib').sha256(canvas).hexdigest()},
        detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: PNG mang đuôi .txt, không đọc được ảnh.',
        updated_at=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat())
    state_path.write_text(json.dumps(state));claim=claim_thumbnail_ai_generation(app.workspace,package)
    cid='6aceaf4f-44cc-83ec-a402-61fadf210822';data=tmp_path/'Data';bridge=data/'chat-bridge';bridge.mkdir(parents=True)
    (bridge/'bridge-state.json').write_text(json.dumps({'savedTurns':{'thread:exact-old-thread':{'chatUrl':'https://chatgpt.com/c/'+cid}}}))
    old=thumbnail_canvas_request(claim.prompt)
    proof={'complete':True,'terminal_no_image':True,'prompt_sha256':digest(old),'conversation_id':cid,
        'image_message_ids':[],'matched_user_ids':['u'],'terminal_detail':'PNG mang đuôi .txt, không đọc được ảnh.'}
    image=io.BytesIO();Image.new('RGB',(1280,720),(20,80,180)).save(image,format='PNG');calls=[]
    class Chat:
        def __init__(self,*args,**kw):self.thread_id=kw['thread_id'];self.response_id='';self._last_existing_image_evidence={'status':'no-image','source_proof':proof}
        def read_existing_generated_image(self,**kw):calls.append(('read',kw['expected_prompt']));return None
        def diagnose_session(self):return {}
        def generate_image(self,prompt,**kw):
            assert read_thumbnail_ai_state(app.workspace)['canvas_transport_repair']['count']==1
            assert kw['attachments'][0].name.endswith('.png') and kw['attachments'][0].data==canvas
            calls.append(('generate',prompt));kw['on_status']('submitted','accepted')
            return SimpleNamespace(data=image.getvalue(),response_id='fixed-image',mime_type='image/png')
        def send(self,*args,**kw):return 'ĐÃ TẠO'
    with patch('docurandom.thumbnail_contract.review_image',side_effect=lambda w,p,b,**k:b) as review, patch('docurandom.storage.app_data_dir',return_value=data),patch('docurandom.ui._thumbnail_terminal_retry_idle_signature',return_value=('idle',)):
        run_worker(app,package,Chat)
        assert review.call_args.kwargs['enforce'] is True
    assert calls==[('read',old),('generate',thumbnail_canvas_transport_request(claim.prompt))]
    assert read_thumbnail_ai_state(app.workspace)['status']=='complete'
