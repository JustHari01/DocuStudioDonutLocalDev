import hashlib,json,threading
from unittest.mock import Mock,patch
import pytest
from docurandom.content_ai import ChatGPTWebChat,MasterPromptRunner,ChatTurn,ContentAIError,_prepare_direct_text_prompt

MASTER='日本の技術を確認します。'*7000
def packet():
    wire,files=_prepare_direct_text_prompt(MASTER)
    return {'status':'not-submitted','safe_to_resend':True,'proof':'failed-before-master-input',
        'attachment_sha256':hashlib.sha256(files[0].data).hexdigest(),
        'prompt_sha256':hashlib.sha256(' '.join(wire.split()).encode()).hexdigest()}

@pytest.mark.parametrize('wrong',['file','prompt','inline','unsafe','none'])
def test_early_master_recovery_requires_the_exact_pending_request(tmp_path,wrong):
    chat=ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'t'*48,thread_id='thread_early_master_fixture')
    value=packet()
    if wrong=='file':value['attachment_sha256']='a'*64
    if wrong=='prompt':value['prompt_sha256']='a'*64
    if wrong=='unsafe':value['safe_to_resend']=False
    connection=Mock();connection.getresponse.return_value.status=200
    connection.getresponse.return_value.read.return_value=json.dumps(value).encode()
    with patch('docurandom.content_ai.http.client.HTTPConnection',return_value=connection):
        if wrong!='none':
            with pytest.raises(ContentAIError):chat.recover_pending(pending_prompt='small' if wrong=='inline' else MASTER)
        else:
            result=chat.recover_pending(pending_prompt=MASTER)
            assert result['early_master_recovery_verified'] is True

def test_verified_first_master_is_submitted_once_even_in_submitted_only_recovery(tmp_path):
    chat=ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'t'*48,thread_id='thread_early_master_fixture')
    seed=MasterPromptRunner(chat,output_dir=tmp_path,project_name='video',minutes=1)
    seed.master_prompt_sha256=hashlib.sha256(MASTER.strip().encode()).hexdigest()
    seed.turns=[ChatTurn('user',MASTER)];seed.pending_turn_transport_started=True;seed._save_checkpoint()
    response=packet();response['early_master_recovery_verified']=True
    chat.recover_pending=Mock(return_value=response)
    chat.send=Mock(side_effect=ContentAIError('stop-after-approved-master-send'))
    resumed=MasterPromptRunner(chat,output_dir=tmp_path,project_name='video',minutes=1,recover_submitted_only=True)
    with pytest.raises(ContentAIError,match='stop-after-approved-master-send'):resumed.run(MASTER)
    assert chat.send.call_count==1 and chat.send.call_args.args==(MASTER,)
    assert chat.thread_id=='thread_early_master_fixture'
    saved=json.loads(seed.checkpoint_path.read_text())
    assert len(saved['turns'])==1 and saved['turns'][0]['text']==MASTER

def test_generic_no_receipt_never_grants_first_master_resubmission(tmp_path):
    chat=ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'t'*48,thread_id='thread_early_master_fixture')
    seed=MasterPromptRunner(chat,output_dir=tmp_path,project_name='video',minutes=1)
    seed.master_prompt_sha256=hashlib.sha256(MASTER.strip().encode()).hexdigest()
    seed.turns=[ChatTurn('user',MASTER)];seed._save_checkpoint()
    chat.recover_pending=Mock(return_value={'status':'not-submitted','output_text':''});chat.send=Mock()
    with pytest.raises(ContentAIError,match='Không có bằng chứng'):
        MasterPromptRunner(chat,output_dir=tmp_path,project_name='video',minutes=1,recover_submitted_only=True).run(MASTER)
    chat.send.assert_not_called()
