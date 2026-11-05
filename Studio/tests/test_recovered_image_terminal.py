import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from docurandom.content_ai import ChatGPTWebChat, ContentAIError


@pytest.mark.parametrize('has_text', [True, False])
def test_recovered_finished_no_image_preserves_terminal_proof(tmp_path, has_text):
    value = {'status': 'recovered', 'recovery_error':
             'ChatGPT không tạo được ảnh thumbnail trong lượt này.'}
    if has_text:
        value['output_text'] = 'KHÔNG TẠO ĐƯỢC: chưa có thumbnail nào được tạo.'

    class Connection:
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return self
        status = 200
        def read(self, *args): return json.dumps(value).encode()
        def close(self): pass

    chat = ChatGPTWebChat('http://127.0.0.1:17841', 'chatgpt-web/high',
                         Path(tmp_path), 'test-local-token-' * 4)
    chat.thread_id = 'thread_docurandom_terminal_proof'
    with patch('docurandom.content_ai.http.client.HTTPConnection', Connection):
        result = chat.recover_pending(output_mode='generated_image',
                                      cancel_event=threading.Event())
    assert result == value
    with pytest.raises(ContentAIError, match='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE:'):
        chat.parse_generated_image_response(result)


def test_text_recovery_remains_text(tmp_path):
    value = {'status': 'recovered', 'output_text': 'Nội dung đã thu hồi.',
             'recovery_error': 'diagnostic for a different output mode'}

    class Connection:
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return self
        status = 200
        def read(self, *args): return json.dumps(value).encode()
        def close(self): pass

    chat = ChatGPTWebChat('http://127.0.0.1:17841', 'chatgpt-web/high',
                         Path(tmp_path), 'test-local-token-' * 4)
    chat.thread_id = 'thread_docurandom_terminal_proof'
    with patch('docurandom.content_ai.http.client.HTTPConnection', Connection):
        assert chat.recover_pending() == value['output_text']
