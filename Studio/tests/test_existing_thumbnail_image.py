import base64
import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch
import pytest
from docurandom.content_ai import ChatGPTWebChat, ContentAIError


def test_reader_preserves_loading_failure_detail_instead_of_opaque_http_500():
    chat = ChatGPTWebChat('http://127.0.0.1:17841', 'chatgpt-web/auto', Path('output'),
                          'token_'*10, thread_id='exact-thread')
    body = json.dumps({'error': {'message': 'CHATGPT_CONVERSATION_LOAD_FAILED: Preserve receipt'}}).encode()
    error = urllib.error.HTTPError('http://127.0.0.1:17841/v1/session/existing-image',
                                    500, 'Internal Server Error', {}, io.BytesIO(body))
    with patch('docurandom.content_ai.urllib.request.urlopen', side_effect=error), \
         patch.object(chat, 'generate_image') as generate, \
         pytest.raises(ContentAIError, match='CHATGPT_CONVERSATION_LOAD_FAILED'):
        chat.read_existing_generated_image()
    generate.assert_not_called()

def test_existing_image_reader_fetches_exact_thread_and_never_submits_prompt():
    chat = ChatGPTWebChat('http://127.0.0.1:17841', 'chatgpt-web/auto', Path('output'), 'token_'*10, thread_id='exact-thread')
    response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
    image = {'status':'ready','generated_image':{'data_base64':base64.b64encode(b'image').decode(),
             'width':1280,'height':720,'mime_type':'image/png'}}
    with patch('docurandom.content_ai.urllib.request.urlopen', return_value=response) as urlopen, \
         patch('docurandom.content_ai._read_limited_json_response',return_value=image), \
         patch.object(chat,'generate_image') as generate:
        value = chat.read_existing_generated_image()
    request = urlopen.call_args.args[0]
    assert request.full_url.endswith('/v1/session/existing-image')
    assert json.loads(request.data)=={'thread_id':'exact-thread'}
    assert value.data==b'image'
    generate.assert_not_called()

@pytest.mark.parametrize('status',['pending','unknown'])
def test_reader_waits_on_unconfirmed_manual_turn_without_resending(status):
    chat = ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',Path('output'),'token_'*10,thread_id='exact')
    response = Mock(); response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
    with patch('docurandom.content_ai.urllib.request.urlopen',return_value=response), \
         patch('docurandom.content_ai._read_limited_json_response',return_value={'status':status}), \
         pytest.raises(ContentAIError,match='không gửi lại'):
        chat.read_existing_generated_image()


def test_finished_voice_audit_does_not_erase_zero_image_evidence():
    from docurandom.ui import _thumbnail_terminal_probe_after_text_turn
    chat = ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',Path('output'),'token_'*10,thread_id='exact')
    payload = {'status':'pending','image_ready':False,'image_busy':False,
               'candidate_count':0,'after_latest_user':False,
               'latest_assistant_after_user':True,'assistant_count':5,'user_count':5}
    response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
    with patch('docurandom.content_ai.urllib.request.urlopen',return_value=response), \
         patch('docurandom.content_ai._read_limited_json_response',return_value=payload), \
         pytest.raises(ContentAIError) as caught:
        chat.read_existing_generated_image()
    assert _thumbnail_terminal_probe_after_text_turn(caught.value)


@pytest.mark.parametrize('patch_value',[
    {'candidate_count':1},{'image_busy':True},{'image_ready':True},
    {'latest_assistant_after_user':False},{'after_latest_user':True},
    {'assistant_count':0},{'candidate_count':None},{'status':'unknown'},
])
def test_uncertain_image_or_active_turn_cannot_bypass_recovery_proof(patch_value):
    from docurandom.ui import _thumbnail_terminal_probe_after_text_turn
    error=ContentAIError('pending')
    error.image_recovery_evidence={'status':'pending','image_ready':False,'image_busy':False,
        'candidate_count':0,'after_latest_user':False,'latest_assistant_after_user':True,
        'assistant_count':5,'user_count':5}|patch_value
    assert not _thumbnail_terminal_probe_after_text_turn(error)
