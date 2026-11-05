from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
import json
import hashlib
import pytest
from docurandom.content_ai import ChatGPTWebChat, ContentAIError
from docurandom.ui import _is_chatgpt_rate_limit_error, _chatgpt_rate_limit_cooldown_seconds

NOTICE = "We're doing a quick check to keep ChatGPT reliable. Try again after 10:17\u202fPM."

def test_provider_reset_time_is_honored_without_changing_web_model():
    now = datetime(2026, 10, 11, 21, 45, tzinfo=timezone(timedelta(hours=7)))
    assert _is_chatgpt_rate_limit_error(NOTICE)
    assert _chatgpt_rate_limit_cooldown_seconds(NOTICE, now=now) == 32 * 60 + 60
    assert _chatgpt_rate_limit_cooldown_seconds(
        "Limit reached. Try again after 11:45 PM tomorrow.", now=now
    ) == (26 * 60 * 60) + 60

def test_http_limit_keeps_exact_notice_in_actionable_error():
    message = ChatGPTWebChat._error_message(429, json.dumps({'error': {'message': NOTICE}}).encode())
    assert 'CHATGPT_RATE_LIMITED' in message
    assert NOTICE in message

def test_recovery_limit_is_not_hidden_by_old_pending_lock(tmp_path):
    requests = []
    class Response:
        status = 409
        def read(self, _limit):
            return b'{"error":{"message":"This workspace has an interrupted ChatGPT turn; recover its DOM before sending another prompt"}}'
        def close(self):
            pass
    class Connection:
        sock = None
        def __init__(self, *_args, **_kwargs):
            pass
        def request(self, method, route, **_kwargs):
            requests.append((method, route))
        def getresponse(self):
            return Response()
        def close(self):
            pass
    chat = ChatGPTWebChat('http://127.0.0.1:17841', 'chatgpt-web/auto', Path(tmp_path), 't' * 48,
                         thread_id='exact-thread')
    with patch('docurandom.content_ai.http.client.HTTPConnection', Connection), patch.object(
        chat, 'recover_pending', side_effect=ContentAIError('CHATGPT_RATE_LIMITED: ' + NOTICE)
    ) as recovery:
        with pytest.raises(ContentAIError, match='CHATGPT_RATE_LIMITED') as exc:
            chat.send('same pending prompt')
    assert NOTICE in str(exc.value)
    assert requests == [('POST', '/v1/responses')]
    recovery.assert_called_once()
    assert chat.model == 'chatgpt-web/auto'
    assert chat.thread_id == 'exact-thread'

def test_thumbnail_loading_failures_back_off_without_exhausting_image_budget():
    from docurandom.thumbnail_ai import thumbnail_ai_terminal_retry_remaining_seconds
    state = {'status':'terminal_no_image', 'submitted':False, 'thread_id':'exact-thread',
             'automatic_retry_count':0, 'readonly_probe_count':4,
             'detail':'CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: no image\nĐọc lại hội thoại: CHATGPT_CONVERSATION_LOAD_FAILED',
             'updated_at':(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()}
    assert 19*60 <= thumbnail_ai_terminal_retry_remaining_seconds(state) <= 20*60
    state['updated_at'] = (datetime.now(timezone.utc)-timedelta(minutes=31)).isoformat()
    assert thumbnail_ai_terminal_retry_remaining_seconds(state) == 0
    state['automatic_retry_count'] = 3
    assert thumbnail_ai_terminal_retry_remaining_seconds(state) is None

def test_direct_master_transport_preserves_exact_bytes_and_pending_file_identity():
    from docurandom.content_ai import _prepare_direct_text_prompt
    original = '日本語 Tiếng Việt 😀\r\n' * 5000
    wire, attachments = _prepare_direct_text_prompt(original)
    assert len(attachments) == 1
    assert attachments[0].data == original.encode('utf-8')
    digest = hashlib.sha256(original.encode('utf-8')).hexdigest()
    assert attachments[0].name == f'DocuRandom-Master-Prompt-{digest[:12]}.txt'
    assert f'{len(original.encode("utf-16-le"))//2:,} ký tự' in wire
    assert digest in wire
    assert _prepare_direct_text_prompt('BẮT ĐẦU') == ('BẮT ĐẦU', ())


def test_seo_tags_stop_at_marked_images_and_narration_blocks():
    from docurandom.content_ai import _export_sections, _parse_seo_tags
    response = ('SEO TAGS\n日本製ミシン, バングラデシュ, 差動送り\n\n'
                '[[prompt-hinh-anh]]\nImage 1. Factory, cotton, documentary\n'
                '[[end-prompt-hinh-anh]]\n[[PART-1]]\nReal narration\n[[END-PART-1]]')
    sections = _export_sections(response)
    assert _parse_seo_tags(sections['seo_tags']) == ('日本製ミシン', 'バングラデシュ', '差動送り')
    assert 'Factory' not in sections['seo_tags']


def test_original_crlf_pending_master_is_distinct_from_older_missing_file_answer():
    from docurandom.content_ai import ChatTurn, master_prompt_attachment_failure_in_turns
    master = 'Same master\nOriginal content'
    old = [ChatTurn('user', master), ChatTurn('assistant', 'The attached Master Prompt file is not available.')]
    assert master_prompt_attachment_failure_in_turns(old, master)
    pending = old + [ChatTurn('user', master.replace('\n', '\r\n'))]
    assert not master_prompt_attachment_failure_in_turns(pending, master)


def test_direct_pending_master_recovery_sends_only_file_identity_and_prompt_digest(tmp_path):
    requests = []
    class Response:
        status = 200
        def read(self, _limit):
            return json.dumps({'status':'not-submitted','safe_to_resend':True,
                               'attachment_sha256':hashlib.sha256(master.encode()).hexdigest()}).encode()
    class Connection:
        def __init__(self, *_args, **_kwargs): pass
        def request(self, method, route, **kwargs):
            requests.append((route, json.loads(kwargs['body'])))
        def getresponse(self): return Response()
        def close(self): pass
    master = 'Exact original bytes\r\n' * 5000
    chat = ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'t'*48,thread_id='exact-thread')
    with patch('docurandom.content_ai.http.client.HTTPConnection',Connection):
        result = chat.recover_pending(pending_prompt=master)
    assert result['safe_to_resend']
    digest = hashlib.sha256(master.encode()).hexdigest()
    assert len(requests) == 1
    pending_digest = requests[0][1].pop('pending_prompt_sha256')
    assert len(pending_digest) == 64 and all(char in '0123456789abcdef' for char in pending_digest)
    assert master not in json.dumps(requests)
    assert requests == [('/v1/turns/recover', {'thread_id':'exact-thread','pending_attachment':{
        'name':f'DocuRandom-Master-Prompt-{digest[:12]}.txt','size':len(master.encode()),'sha256':digest}})]
