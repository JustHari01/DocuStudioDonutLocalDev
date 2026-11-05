import hashlib,json,unicodedata
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from docurandom.workspace import Workspace
from docurandom.thumbnail_ai import (claim_thumbnail_ai_generation,update_thumbnail_ai_state,
    read_thumbnail_ai_state,reserve_thumbnail_recovery_chat,thumbnail_generation_request,
    thumbnail_recovery_chat_remaining_seconds)

def setup(tmp_path):
    w=Workspace('workspace','Demo',str(tmp_path/'workspace'),'','');w.ensure_layout()
    p=w.output_dir/'video.txt';p.write_text('TIÊU ĐỀ\nReal title\nMÔ TẢ\nA description\nPROMPT THUMBNAIL\nPROMPT THUMBNAIL HOÀN CHỈNH\nCreate exactly this picture\nTHOẠI VOICEVOX\nA: demo',encoding='utf-8')
    data=tmp_path/'Data';bridge=data/'chat-bridge';bridge.mkdir(parents=True)
    cid='6aceaf4f-44cc-83ec-a402-61fadf210822'
    (bridge/'bridge-state.json').write_text(json.dumps({'savedTurns':{'thread:old_video_thread':{'chatUrl':'https://chatgpt.com/c/'+cid}}}))
    with patch('docurandom.storage.app_data_dir',return_value=data):
        claim=claim_thumbnail_ai_generation(w,p)
        update_thumbnail_ai_state(w,p,claim,status='terminal_no_image',submitted=False,thread_id='old_video_thread',
            automatic_retry_count=3,detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: Công cụ nhận nhầm thành chỉnh sửa ảnh có sẵn.')
    state=read_thumbnail_ai_state(w);state['updated_at']=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
    (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(state))
    claim=claim_thumbnail_ai_generation(w,p)
    digest=hashlib.sha256(' '.join(unicodedata.normalize('NFC',thumbnail_generation_request(claim.prompt)).split()).encode()).hexdigest()
    proof={'complete':True,'terminal_no_image':True,'prompt_sha256':digest,'conversation_id':cid,
           'image_message_ids':[],'matched_user_ids':['request-one'],'terminal_detail':'KHÔNG TẠO ĐƯỢC: nhận nhầm thành chỉnh sửa ảnh.'}
    return w,p,claim,proof,data

def test_only_one_new_chat_after_exact_finished_absence_and_old_links_are_retained(tmp_path):
    w,p,claim,proof,data=setup(tmp_path)
    assert claim.action=='recover_new_chat'
    with patch('docurandom.storage.app_data_dir',return_value=data):
        assert reserve_thumbnail_recovery_chat(w,p,claim,'old_video_thread','new_recovery_thread',proof)
        assert not reserve_thumbnail_recovery_chat(w,p,claim,'old_video_thread','second_recovery_thread',proof)
    state=read_thumbnail_ai_state(w)
    assert state['automatic_retry_count']==3
    assert state['recovery_chat']['count']==1
    assert state['recovery_chat']['old_url'].endswith(proof['conversation_id'])
    assert json.loads((w.state_dir/'thumbnail-conversations.json').read_text())['records'][0]['thread_id']=='old_video_thread'
    state.update(status='terminal_no_image',submitted=False)
    assert thumbnail_recovery_chat_remaining_seconds(state)is None

def test_pending_wrong_prompt_or_existing_image_never_authorizes_new_chat(tmp_path):
    for field,value in [('terminal_no_image',False),('complete',False),('prompt_sha256','0'*64),('image_message_ids',['existing']),('terminal_detail','quota limit')]:
        w,p,claim,proof,data=setup(tmp_path/field)
        before=(w.state_dir/'thumbnail-ai.json').read_bytes();proof[field]=value
        with patch('docurandom.storage.app_data_dir',return_value=data):
            assert not reserve_thumbnail_recovery_chat(w,p,claim,'old_video_thread','new_recovery_thread',proof)
        assert (w.state_dir/'thumbnail-ai.json').read_bytes()==before

def test_real_worker_collects_old_chat_before_generating_once_in_new_chat(tmp_path):
    import io
    from types import SimpleNamespace
    from PIL import Image
    from tests.test_stalled_thumbnail_repair import recovered_app,run_worker
    app,package,path=recovered_app(tmp_path)
    state=read_thumbnail_ai_state(app.workspace)
    state.update(status='terminal_no_image',submitted=False,automatic_retry_count=3,
        detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: nhận nhầm thành chỉnh sửa ảnh có sẵn.')
    path.write_text(json.dumps(state))
    claim=claim_thumbnail_ai_generation(app.workspace,package)
    digest=hashlib.sha256(' '.join(unicodedata.normalize('NFC',thumbnail_generation_request(claim.prompt)).split()).encode()).hexdigest()
    cid='6aceaf4f-44cc-83ec-a402-61fadf210822';data=tmp_path/'Data';bridge=data/'chat-bridge';bridge.mkdir(parents=True)
    (bridge/'bridge-state.json').write_text(json.dumps({'savedTurns':{'thread:exact-old-thread':{'chatUrl':'https://chatgpt.com/c/'+cid}}}))
    proof={'complete':True,'terminal_no_image':True,'prompt_sha256':digest,'conversation_id':cid,'image_message_ids':[],'matched_user_ids':['u'],'terminal_detail':'KHÔNG TẠO ĐƯỢC'}
    output=io.BytesIO();Image.new('RGB',(1280,720),(20,30,40)).save(output,format='PNG')
    calls=[]
    class Chat:
        def __init__(self,*args,**kw):
            self.thread_id=kw.get('thread_id')or'new-recovery-chat';self.response_id=''
            self._last_existing_image_evidence={'status':'no-image','source_proof':proof}
        def read_existing_generated_image(self,**kw):calls.append(('read',self.thread_id));return None
        def diagnose_session(self):return {}
        def generate_image(self,prompt,**kw):
            calls.append(('generate',self.thread_id));kw['on_status']('submitted','accepted')
            return SimpleNamespace(data=output.getvalue(),response_id='image',mime_type='image/png')
        def send(self,*args,**kw):return 'ĐÃ TẠO'
    with patch('docurandom.storage.app_data_dir',return_value=data),patch('docurandom.ui._thumbnail_terminal_retry_idle_signature',return_value=('idle',)):
        run_worker(app,package,Chat)
    after=read_thumbnail_ai_state(app.workspace)
    assert calls==[('read','exact-old-thread'),('generate','new-recovery-chat')]
    assert after['status']=='complete'and after['recovery_chat']['count']==1
    assert after['automatic_retry_count']==3
