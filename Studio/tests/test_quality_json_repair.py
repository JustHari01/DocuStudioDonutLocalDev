import json
from types import SimpleNamespace
import pytest
from docurandom import quality_chat as q
from docurandom.channel_learning import digest


def setup(monkeypatch, tmp_path, *, empty_repair=False, pending=False):
    sends=[]
    thread='thread_docurandom_'+'a'*32
    prompt='Return JSON only {"passed":boolean} for this exact image'
    path=tmp_path/(digest([prompt,'verified_json',thread,[]])+'.json')
    path.write_text(json.dumps({'request':prompt,'thread_id':thread,'status':'uncertain'}))
    monkeypatch.setattr(q,'check_chatgpt_web_bridge',lambda **_:SimpleNamespace(operational=True,endpoint='http://127.0.0.1:17841',access_token='private',available_models=[]))
    monkeypatch.setattr(q,'resolve_chatgpt_web_model',lambda *_:'user-selected-high')
    class Chat:
        def __init__(self,endpoint,model,*args,**kwargs):assert model=='user-selected-high'
        def cancel(self):pass
        def send(self,prompt,**kwargs):sends.append(prompt);return '{}'
    monkeypatch.setattr(q,'ChatGPTWebChat',Chat)
    def read(status,thread,p):
        if pending:raise RuntimeError('Exact review response pending; do not resubmit')
        if p==prompt or empty_repair:raise q.CompletedReviewInvalid({'complete':True,'terminal_ended':True,'assistant_message_id':'ended'})
        return '{"passed":false}',{'complete':True,'terminal_ended':True,'assistant_message_id':'repair-ended'}
    monkeypatch.setattr(q,'exact_text_response',read)
    return thread,prompt,path,sends


def test_empty_final_repairs_once_and_caches_without_claiming_pass(monkeypatch,tmp_path):
    thread,prompt,path,sends=setup(monkeypatch,tmp_path)
    assert q.ask_json(tmp_path,prompt,thread_id=thread)=={'passed':False}
    assert len(sends)==1 and 'Do not call tools' in sends[0]
    record=json.loads(path.read_text());assert record['format_repair']['count']==1
    assert q.ask_json(tmp_path,prompt,thread_id=thread)=={'passed':False}
    assert len(sends)==1


def test_uncertain_turn_is_never_resent(monkeypatch,tmp_path):
    thread,prompt,path,sends=setup(monkeypatch,tmp_path,pending=True)
    with pytest.raises(RuntimeError,match='pending'):q.ask_json(tmp_path,prompt,thread_id=thread)
    assert sends==[] and json.loads(path.read_text())['source_reader_version']==2


def test_invalid_format_repair_does_not_reset_on_restart(monkeypatch,tmp_path):
    thread,prompt,path,sends=setup(monkeypatch,tmp_path,empty_repair=True)
    for _ in range(2):
        with pytest.raises(ValueError,match='REPAIR_EXHAUSTED'):q.ask_json(tmp_path,prompt,thread_id=thread)
    assert len(sends)==1
    assert json.loads(path.read_text())['format_repair']['count']==1


def test_approved_review_stays_bound_to_exact_bytes_and_original_receipt(tmp_path):
    import hashlib
    from docurandom.thumbnail_contract import approved_review_thread
    root=tmp_path/'thumbnail-contract';old=root/'reviews/old.json';old.parent.mkdir(parents=True)
    old.write_text('{}');request='Exact original review request';image_sha='a'*64
    thread='thread_docurandom_'+'b'*32
    approval={'max_requests':1,'image_generation_permitted':False,'image_sha256':image_sha,
              'prompt_sha256':hashlib.sha256(request.encode()).hexdigest(),'new_thread_id':thread,
              'old_receipt':str(old),'old_receipt_sha256':hashlib.sha256(old.read_bytes()).hexdigest()}
    (root/'review-recovery.json').write_text(json.dumps(approval))
    assert approved_review_thread(tmp_path,request,image_sha)==thread
    for wrong_request,wrong_image in [('different',image_sha),(request,'c'*64)]:
        assert approved_review_thread(tmp_path,wrong_request,wrong_image) is None
    old.write_text('{"changed":true}')
    with pytest.raises(ValueError,match='no longer matches'):approved_review_thread(tmp_path,request,image_sha)


def test_known_preflight_rejection_resumes_same_reserved_repair(monkeypatch,tmp_path):
    thread,prompt,path,sends=setup(monkeypatch,tmp_path)
    q.ask_json(tmp_path,prompt,thread_id=thread)
    parent=json.loads(path.read_text());parent['status']='repairing_json';parent.pop('result',None)
    path.write_text(json.dumps(parent))
    repair_prompt=parent['format_repair']['request']
    child=tmp_path/(digest([repair_prompt,'verified_json',thread,[]])+'.json')
    child.write_text(json.dumps({'request':repair_prompt,'thread_id':thread,'status':'uncertain',
        'detail':'ContentAIError: Previous ChatGPT attachment is unresolved in its exact conversation; no new file or prompt was sent'}))
    before=len(sends)
    assert q.ask_json(tmp_path,prompt,thread_id=thread)=={'passed':False}
    assert len(sends)==before+1
    assert json.loads(child.read_text())['pre_submit_rejected'] is True
    assert json.loads(path.read_text())['format_repair']['count']==1


def test_busy_preflight_keeps_repair_reserved_then_rechecks_parent(monkeypatch,tmp_path):
    thread,prompt,path,sends=setup(monkeypatch,tmp_path)
    original_chat=q.ChatGPTWebChat
    class Busy(original_chat):
        def send(self,*args,**kwargs):raise q.ChatGPTPreSubmitRejected('This ChatGPT conversation is already running')
    monkeypatch.setattr(q,'ChatGPTWebChat',Busy)
    with pytest.raises(RuntimeError,match='already running'):
        q.ask_json(tmp_path,prompt,thread_id=thread)
    parent=json.loads(path.read_text());repair=parent['format_repair']
    child=tmp_path/(digest([repair['request'],'verified_json',thread,[]])+'.json')
    assert json.loads(child.read_text())['status']=='reserved'
    reads=[];original_read=q.exact_text_response
    def read(*args):reads.append(args[2]);return original_read(*args)
    monkeypatch.setattr(q,'exact_text_response',read)
    monkeypatch.setattr(q,'ChatGPTWebChat',original_chat)
    assert q.ask_json(tmp_path,prompt,thread_id=thread)=={'passed':False}
    assert reads[0]==prompt and len(sends)==1
    assert json.loads(path.read_text())['format_repair']['count']==1


def test_parent_pending_blocks_even_reserved_repair(monkeypatch,tmp_path):
    thread,prompt,path,sends=setup(monkeypatch,tmp_path)
    q.ask_json(tmp_path,prompt,thread_id=thread)
    parent=json.loads(path.read_text());parent.update(status='repairing_json');parent.pop('result')
    path.write_text(json.dumps(parent));before=len(sends)
    def pending(*args):raise RuntimeError('Exact review response pending; do not resubmit')
    monkeypatch.setattr(q,'exact_text_response',pending)
    with pytest.raises(RuntimeError,match='pending'):q.ask_json(tmp_path,prompt,thread_id=thread)
    assert len(sends)==before
