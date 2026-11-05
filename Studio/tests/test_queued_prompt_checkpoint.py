import json,hashlib,threading
from unittest.mock import Mock,patch
import pytest
from docurandom.content_ai import ChatGPTWebChat,MasterPromptRunner,ContentAIError,ContentAICancelled
from docurandom.chatgpt_turn_queue import QueuedChatGPTWebChat
from docurandom.ui import _SharedChatGPTTurnPool

def test_cancelled_fifo_wait_remains_durably_unstarted_and_can_resume(tmp_path):
    pool=_SharedChatGPTTurnPool(1);assert pool.acquire()
    waiting=threading.Event();cancel=threading.Event();failures=[]
    chat=QueuedChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'t'*48,
                             turn_pool=pool,on_wait=lambda *_:waiting.set())
    runner=MasterPromptRunner(chat,output_dir=tmp_path,project_name='video',minutes=1)
    runner.master_prompt_sha256=hashlib.sha256(b'MASTER').hexdigest()
    def send():
        try:runner._send('MASTER',cancel_event=cancel,on_turn=None,on_partial=None)
        except Exception as exc:failures.append(exc)
    with patch('docurandom.content_ai.http.client.HTTPConnection') as transport:
        worker=threading.Thread(target=send);worker.start();assert waiting.wait(2)
        assert json.loads(runner.checkpoint_path.read_text())['pending_turn_transport_started'] is False
        cancel.set();worker.join(3);assert not worker.is_alive()
        transport.assert_not_called()
    assert isinstance(failures[0],ContentAICancelled);pool.release()
    resumed=MasterPromptRunner(chat,output_dir=tmp_path,project_name='video',minutes=1,recover_submitted_only=True)
    chat.recover_pending=Mock(side_effect=AssertionError('Unstarted FIFO ticket must not pretend to be submitted'))
    with patch.object(resumed,'_send',side_effect=ContentAIError('stop-after-queued-resume')) as once:
        with pytest.raises(ContentAIError,match='stop-after-queued-resume'):resumed.run('MASTER')
    assert once.call_args.args==('MASTER',);assert once.call_count==1
    chat.recover_pending.assert_not_called()

def test_transport_start_is_saved_before_any_http_request(tmp_path):
    chat=ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'t'*48)
    runner=MasterPromptRunner(chat,output_dir=tmp_path,project_name='video',minutes=1)
    class Transport:
        sock=None
        def __init__(self,*args,**kwargs):pass
        def request(self,*args,**kwargs):
            saved=json.loads(runner.checkpoint_path.read_text())
            assert saved['pending_turn_transport_started'] is True
            assert saved['turns'][-1]['text']=='queued-once'
            raise ContentAIError('fixture-request-stopped')
        def close(self):pass
    with patch('docurandom.content_ai.http.client.HTTPConnection',Transport):
        with pytest.raises(ContentAIError,match='fixture-request-stopped'):
            runner._send('queued-once',cancel_event=threading.Event(),on_turn=None,on_partial=None)
    assert json.loads(runner.checkpoint_path.read_text())['pending_turn_transport_started'] is True

def test_invalid_queue_hash_cannot_authorize_resend(tmp_path):
    chat=ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'t'*48)
    runner=MasterPromptRunner(chat,output_dir=tmp_path,project_name='video',minutes=1)
    from docurandom.content_ai import ChatTurn
    runner.master_prompt_sha256=hashlib.sha256(b'MASTER').hexdigest();runner.turns=[ChatTurn('user','MASTER')]
    runner.pending_turn_transport_started=False;runner._save_checkpoint()
    saved=json.loads(runner.checkpoint_path.read_text());saved['pending_turn_queue_hash']='0'*64
    runner.checkpoint_path.write_text(json.dumps(saved),encoding='utf8')
    restored=MasterPromptRunner(chat,output_dir=tmp_path,project_name='video',minutes=1)
    assert restored._load_checkpoint('MASTER') is not None
    assert restored.pending_turn_transport_started is None

def test_verified_old_checkpoint_queue_proof_restarts_only_its_unsent_prompt(tmp_path):
    from types import SimpleNamespace
    from docurandom.content_ai import ChatTurn
    chat=SimpleNamespace(thread_id='old-thread',recover_pending=Mock(return_value={
        'status':'not-submitted','safe_to_resend':True,'proof':'queued-after-completed-turn'}),
        send=Mock(side_effect=ContentAIError('fixture-proved-send-once')))
    seed=MasterPromptRunner(chat,output_dir=tmp_path,project_name='video',minutes=1)
    seed.master_prompt_sha256=hashlib.sha256(b'MASTER').hexdigest()
    seed.turns=[ChatTurn('user','MASTER'),ChatTurn('assistant','ĐÃ NẠP'),ChatTurn('user','BẮT ĐẦU')]
    seed._save_checkpoint()
    resumed=MasterPromptRunner(chat,output_dir=tmp_path,project_name='video',minutes=1,recover_submitted_only=True)
    with pytest.raises(ContentAIError,match='fixture-proved-send-once'):resumed.run('MASTER')
    assert chat.send.call_count==1 and chat.send.call_args.args==('BẮT ĐẦU',)
    assert len([t for t in resumed.turns if t.role=='user' and t.text=='BẮT ĐẦU'])==1


@pytest.mark.parametrize('baseline_user',['Previously completed prompt','日本の設備を検証します。'*6000],ids=['inline','attachment'])
def test_recovery_transmits_exact_completed_attachment_baseline(tmp_path,baseline_user):
    chat=ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'t'*48)
    chat.thread_id='thread_docurandom_baseline_fixture'
    connection=Mock();connection.getresponse.return_value.status=200
    connection.getresponse.return_value.read.return_value=b'{"status":"not-submitted","safe_to_resend":true,"proof":"queued-after-completed-turn"}'
    with patch('docurandom.content_ai.http.client.HTTPConnection',return_value=connection):
        result=chat.recover_pending(pending_prompt='Next exact prompt',
            completed_turn_baseline={'user':baseline_user,'assistant':'Saved answer'})
    assert result['proof']=='queued-after-completed-turn'
    method,route=connection.request.call_args.args
    assert method=='POST' and route=='/v1/turns/recover'
    payload=json.loads(connection.request.call_args.kwargs['body'])
    if len(baseline_user.encode('utf-16-le'))//2 >=64000:
        assert payload['completed_turn_baseline']['user_attachment']=={
            'sha256':hashlib.sha256(baseline_user.encode()).hexdigest(),
            'char_length':len(baseline_user.encode('utf-16-le'))//2}
    else:
        assert 'user_attachment' not in payload['completed_turn_baseline']
    assert payload['pending_prompt_sha256']==hashlib.sha256(b'Next exact prompt').hexdigest()
    connection.close.assert_called_once()
