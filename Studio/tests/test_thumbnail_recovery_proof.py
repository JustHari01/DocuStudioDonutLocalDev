import time
from unittest.mock import Mock
import pytest
from docurandom.ui import _thumbnail_stale_submission_signature, _read_completed_thumbnail_for_claim
from docurandom.thumbnail_ai import ThumbnailAIError

def diagnostic():
    url='https://chatgpt.com/c/exact-chat'
    return {'thread_id':'thread:exact-thread','saved_url':url,'current_url':url,
        'pending_submission':True,'context_busy':False,'active_controller':False,'active_turns':3,
        'pending_submission_details':{'send_attempted':True,'submission_accepted':True,
            'output_mode':'generated_image','created_at':(time.time()-1800)*1000,
            'user_count':5,'assistant_count':5,'assistant_key':'assistant-5','user_key':'user-5'},
        'snapshot':{'url':url,'users':[{'key':'user-6','text':'P'*512}],
            'assistants':[{'key':'assistant-5'}],'userCount':6,'assistantCount':5,
            'stopControlCount':0,'streamingMarkerCount':0},
        'page_snapshot':{'errorMarkerCount':0,'generatedImageCandidateCount':0}}

def test_other_workspaces_do_not_block_exact_idle_thumbnail_recovery():
    assert _thumbnail_stale_submission_signature(diagnostic(),'P'*1000,'exact-thread') is not None

@pytest.mark.parametrize('field',['context_busy','active_controller'])
def test_own_active_turn_still_blocks_retry(field):
    value=diagnostic();value[field]=True
    assert _thumbnail_stale_submission_signature(value,'P'*1000,'exact-thread') is None

def test_explicit_finished_no_image_receipt_reaches_bounded_terminal_retry():
    chat=Mock();chat.read_existing_generated_image.return_value=None
    chat._last_existing_image_evidence={'status':'no-image'}
    with pytest.raises(ThumbnailAIError,match='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE'):
        _read_completed_thumbnail_for_claim(chat,'recover',Mock())
    chat.generate_image.assert_not_called()

def test_missing_evidence_never_authorizes_a_new_thumbnail():
    chat=Mock();chat.read_existing_generated_image.return_value=None
    chat._last_existing_image_evidence={'status':'pending'}
    assert _read_completed_thumbnail_for_claim(chat,'recover',Mock()) is None
    chat.generate_image.assert_not_called()
