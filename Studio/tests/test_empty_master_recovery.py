import hashlib,json
from unittest.mock import Mock,patch
import pytest
from docurandom.content_ai import ChatGPTWebChat,MasterPromptRunner,ChatTurn,ContentAIError


def test_empty_master_permission_is_bound_to_exact_expected_file(tmp_path):
    master='Master data 日本語\n'*7000
    client=ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'t'*48)
    client.thread_id='thread_master_fixture_1234'
    connection=Mock();connection.getresponse.return_value.status=200
    proof={'status':'not-submitted','safe_to_resend':True,'proof':'empty-pre-submit-attachment',
        'attachment_sha256':hashlib.sha256(master.encode()).hexdigest()}
    connection.getresponse.return_value.read.return_value=json.dumps(proof).encode()
    with patch('docurandom.content_ai.http.client.HTTPConnection',return_value=connection):
        result=client.recover_pending(pending_prompt=master)
        assert result['attachment_recovery_verified']is True
        connection.getresponse.return_value.read.return_value=json.dumps({**proof,'attachment_sha256':'0'*64}).encode()
        with pytest.raises(ContentAIError,match='tệp Master Prompt'):client.recover_pending(pending_prompt=master)
        connection.getresponse.return_value.read.return_value=json.dumps(proof).encode()
        with pytest.raises(ContentAIError,match='lần nạp lại'):client.recover_pending(pending_prompt='small text')


@pytest.mark.parametrize('verified',[False,True])
def test_runner_only_resends_empty_master_after_verified_permission(tmp_path,verified):
    master='Master 日本語\n'*7000
    client=ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'t'*48)
    client.thread_id='thread_master_fixture_1234'
    seed=MasterPromptRunner(client,output_dir=tmp_path,project_name='video',minutes=1)
    seed.master_prompt_sha256=hashlib.sha256(master.strip().encode()).hexdigest()
    seed.turns=[ChatTurn('user',master)];seed.pending_turn_transport_started=True;seed._save_checkpoint()
    client.recover_pending=Mock(return_value={'status':'not-submitted','safe_to_resend':True,
        'proof':'empty-pre-submit-attachment','attachment_recovery_verified':verified})
    runner=MasterPromptRunner(client,output_dir=tmp_path,project_name='video',minutes=1,recover_submitted_only=True)
    with patch.object(runner,'_send',side_effect=ContentAIError('fixture-exact-restage'))as send:
        with pytest.raises(ContentAIError,match='fixture-exact-restage'if verified else 'Không có bằng chứng'):
            runner.run(master)
        if verified:send.assert_called_once();assert send.call_args.args==(master,)
        else:send.assert_not_called()
