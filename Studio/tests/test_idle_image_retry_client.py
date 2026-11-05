import hashlib,json
from unittest.mock import patch
import pytest
from docurandom.content_ai import ChatGPTWebChat,ContentAIError
from docurandom.thumbnail_ai import thumbnail_ai_pending_receipt_error

def test_bounded_recovery_wait_does_not_mark_an_accepted_image_as_unsent():
    assert thumbnail_ai_pending_receipt_error('CHATGPT_RECOVERY_PENDING: giữ biên nhận')
    assert not thumbnail_ai_pending_receipt_error('CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: Không tạo được')

def test_image_receipt_retirement_uses_exact_thread_prompt_and_timestamp(tmp_path):
    chat=ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'x'*48,thread_id='thread_exact_image')
    sent=[]
    class Transport:
        def __init__(self,*args,**kwargs):pass
        def request(self,method,path,body,headers):sent.append((method,path,json.loads(body)))
        def getresponse(self):return self
        status=200
        def read(self,*args):return b'{"status":"retired-for-one-image-retry"}'
        def close(self):pass
    with patch('docurandom.content_ai.http.client.HTTPConnection',Transport):
        chat.retire_idle_image_turn('Create\nexactly one thumbnail',1234567890000)
    assert sent==[('POST','/v1/session/retire-idle-image-turn',{'thread_id':'thread_exact_image',
        'created_at':1234567890000,'prompt_sha256':hashlib.sha256(b'Create exactly one thumbnail').hexdigest()})]

def test_lost_retirement_ack_retains_the_uncertain_submission_lock(tmp_path):
    chat=ChatGPTWebChat('http://127.0.0.1:17841','chatgpt-web/auto',tmp_path,'x'*48,thread_id='thread_exact_image')
    class Transport:
        def __init__(self,*args,**kwargs):pass
        def request(self,*args,**kwargs):raise OSError('fixture-disconnected')
        def close(self):pass
    with patch('docurandom.content_ai.http.client.HTTPConnection',Transport):
        with pytest.raises(ContentAIError,match='CHATGPT_PENDING_SUBMISSION_LOCKED'):
            chat.retire_idle_image_turn('Create exactly one thumbnail',1234567890000)
