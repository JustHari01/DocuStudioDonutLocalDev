import threading
from unittest.mock import Mock, patch
import pytest
from docurandom.youtube_upload import YouTubeStudioUploader


def test_exact_transferred_draft_commits_private_with_click_and_does_not_attach_file():
    u=YouTubeStudioUploader();c=Mock();cancel=threading.Event()
    dialog={'attributes':['video-id','VideoExact1']};c.find_dom.side_effect=[dialog,dialog,None]
    done={'nodeId':42};c.wait_dom.return_value=done
    r={'transfer_complete':True,'attempt_id':'owned','youtube_url':'https://youtu.be/VideoExact1'}
    with patch.object(u,'_advance_upload_wizard_to_visibility')as advance:
        assert u._commit_transferred_private_draft(c,r['youtube_url'],r,Mock(),cancel)
    advance.assert_called_once()
    c.select_dom_radio.assert_called_once_with('PRIVATE',timeout=30,cancel_event=cancel)
    c.click_dom.assert_called_once_with(done)
    c.set_file.assert_not_called();c.press.assert_not_called()


@pytest.mark.parametrize('change',[{'transfer_complete':False},{'attempt_id':''},{'youtube_url':'https://youtu.be/OtherVideo1'}])
def test_unproven_transfer_or_other_video_cannot_commit(change):
    c=Mock();r={'transfer_complete':True,'attempt_id':'owned','youtube_url':'https://youtu.be/VideoExact1',**change}
    assert not YouTubeStudioUploader()._commit_transferred_private_draft(c,'https://youtu.be/VideoExact1',r,Mock(),threading.Event())
    c.click_dom.assert_not_called();c.select_dom_radio.assert_not_called()


def test_no_exact_wizard_does_not_change_published_video():
    c=Mock();c.find_dom.return_value=None
    r={'transfer_complete':True,'attempt_id':'owned','youtube_url':'https://youtu.be/VideoExact1'}
    assert not YouTubeStudioUploader()._commit_transferred_private_draft(c,r['youtube_url'],r,Mock(),threading.Event())
    c.click_dom.assert_not_called();c.select_dom_radio.assert_not_called()

def test_reopened_exact_draft_editor_commits_same_allocated_video():
    u=YouTubeStudioUploader();c=Mock();cancel=threading.Event()
    dialog={'attributes':['video-id','VideoExact1']};c.find_dom.side_effect=[None,dialog,None]
    c.page_url.return_value='https://studio.youtube.com/video/VideoExact1/edit'
    banner={'backendNodeId':10};done={'backendNodeId':20};c.find.return_value=banner
    c.wait_dom.side_effect=[dialog,done]
    r={'transfer_complete':True,'attempt_id':'owned','youtube_url':'https://youtu.be/VideoExact1'}
    with patch.object(u,'_advance_upload_wizard_to_visibility'):
        assert u._commit_transferred_private_draft(c,r['youtube_url'],r,Mock(),cancel)
    c.press.assert_called_once_with(banner)
    c.click_dom.assert_called_once_with(done)
    c.select_dom_radio.assert_called_once_with('PRIVATE',timeout=30,cancel_event=cancel)
    c.set_file.assert_not_called()


def test_reopened_editor_for_different_video_never_opens_draft():
    c=Mock();c.find_dom.return_value=None;c.page_url.return_value='https://studio.youtube.com/video/WrongVideo1/edit'
    r={'transfer_complete':True,'attempt_id':'owned','youtube_url':'https://youtu.be/VideoExact1'}
    assert not YouTubeStudioUploader()._commit_transferred_private_draft(c,r['youtube_url'],r,Mock(),threading.Event())
    c.click_dom.assert_not_called();c.select_dom_radio.assert_not_called()

