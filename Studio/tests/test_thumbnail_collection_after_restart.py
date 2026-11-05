from unittest.mock import Mock
import pytest
from docurandom.content_ai import ContentAIError
from docurandom.ui import _read_completed_thumbnail_for_claim

@pytest.mark.parametrize("action",["recover","retry_terminal"])
def test_completed_image_is_collected_even_when_original_receipt_was_lost(action):
    chat=Mock(); image=object(); chat.read_existing_generated_image.return_value=image
    assert _read_completed_thumbnail_for_claim(chat,action,Mock()) is image
    chat.read_existing_generated_image.assert_called_once_with()
    chat.recover_pending.assert_not_called()
    chat.generate_image.assert_not_called()

def test_pending_recovery_retains_old_recovery_path_without_sending():
    chat=Mock(); chat.read_existing_generated_image.side_effect=ContentAIError("unconfirmed")
    assert _read_completed_thumbnail_for_claim(chat,"recover",Mock()) is None
    chat.generate_image.assert_not_called()

def test_unknown_terminal_probe_stays_locked():
    chat=Mock(); chat.read_existing_generated_image.side_effect=ContentAIError("unconfirmed")
    with pytest.raises(ContentAIError): _read_completed_thumbnail_for_claim(chat,"retry_terminal",Mock())
    chat.generate_image.assert_not_called()

def test_new_request_does_not_use_previous_conversation_image():
    chat=Mock(); assert _read_completed_thumbnail_for_claim(chat,"start",Mock()) is None
    chat.read_existing_generated_image.assert_not_called()
