import threading
import pytest
from docurandom.youtube_upload import YouTubeStudioUploader,YouTubeUploadError
from unittest.mock import Mock, MagicMock, patch
from pathlib import Path
from contextlib import ExitStack
from docurandom.donut_mcp import DonutProfile, DonutMcpEndpoint
from docurandom.youtube_upload import _SemanticCdpController, WorkspaceUploadSources, YouTubeMetadata

class Editor:
    def __init__(self, choice=None, legacy=False, accepts=True):
        self.choice=choice;self.legacy=legacy;self.accepts=accepts;self.changes=[]
    def dom_nodes(self):return []
    def find_dom(self,*args,**kwargs):return None if self.legacy else {'name':'VIDEO_PAID_PRODUCT_PLACEMENT_NO'}
    def dom_radio_checked(self,name):
        return self.choice==('no' if name=='VIDEO_PAID_PRODUCT_PLACEMENT_NO' else 'yes')
    def select_dom_radio(self,name,**kwargs):
        self.changes.append(name)
        if self.accepts:self.choice='no'

def test_unset_new_required_disclosure_gets_the_existing_no_sponsor_default():
    editor=Editor();YouTubeStudioUploader._ensure_paid_promotion_choice(editor,threading.Event())
    assert editor.changes==['VIDEO_PAID_PRODUCT_PLACEMENT_NO']
    assert YouTubeStudioUploader._paid_promotion_choice(editor) is True

@pytest.mark.parametrize('choice',['yes','no'])
def test_existing_disclosure_is_preserved(choice):
    editor=Editor(choice);YouTubeStudioUploader._ensure_paid_promotion_choice(editor,threading.Event())
    assert editor.changes==[] and editor.choice==choice

def test_legacy_checkbox_interface_is_not_changed():
    editor=Editor(legacy=True);YouTubeStudioUploader._ensure_paid_promotion_choice(editor,threading.Event())
    assert editor.changes==[] and YouTubeStudioUploader._paid_promotion_choice(editor) is None

def test_a_declined_required_choice_blocks_metadata_save():
    editor=Editor(accepts=False)
    with pytest.raises(YouTubeUploadError,match='chưa được lưu metadata'):
        YouTubeStudioUploader._ensure_paid_promotion_choice(editor,threading.Event())


@pytest.mark.parametrize('repair_fails', [False, True])
def test_already_public_exact_video_repairs_missing_disclosure_without_upload(repair_fails, tmp_path):
    uploader=YouTubeStudioUploader(registry_path=tmp_path/'registry.json')
    uploader.coordinator=Mock()
    uploader.coordinator.acquire.return_value=MagicMock()
    client=MagicMock();client.__enter__.return_value=client
    client.get_profile.return_value=DonutProfile('profile-1','Profile 1','wayfern',process_id=123)
    client.get_debug_connection.return_value={'targets_url':'http://127.0.0.1:9222/json'}
    controller=MagicMock(spec=_SemanticCdpController)
    controller.wait_dom.return_value={'backendDOMNodeId':1}
    controller._dom_attributes.return_value={}
    sources=WorkspaceUploadSources(Path('video.mp4'),Path('package.txt'),Path('image.png'),
        YouTubeMetadata('Title','Description',tags=('tag',)))
    with ExitStack() as stack:
        def patched(name, **kwargs):return stack.enter_context(patch('docurandom.youtube_upload.'+name,**kwargs))
        patched('DonutMcpClient',return_value=client)
        patched('_mcp_open_minimized_cdp_window_ws_url',return_value='ws://127.0.0.1:9222/devtools/page/exact')
        patched('_cdp_page_target_id',return_value='exact')
        patched('_mcp_set_cdp_window_state')
        patched('_ensure_youtube_browser_bridge',return_value=Mock())
        patched('_browser_extension_tab_for_target',return_value=(Mock(),77))
        patched('_CdpSession')
        patched('_SemanticCdpController',return_value=controller)
        patched('_mcp_close_cdp_page_target')
        stack.enter_context(patch('docurandom.publication_assets.valid_thumbnail',return_value=True))
        stack.enter_context(patch.object(uploader,'_prepare_exact_draft_edit_target'))
        stack.enter_context(patch.object(uploader,'_verify_edit_visibility',return_value=True))
        stack.enter_context(patch.object(uploader,'_saved_metadata_mismatches',return_value=['paid promotion declaration']))
        stack.enter_context(patch.object(uploader,'_assert_public_video_not_superseded'))
        stack.enter_context(patch.object(uploader,'_set_edit_visibility'))
        stack.enter_context(patch.object(uploader,'_wait_edit_save_complete'))
        registry=stack.enter_context(patch.object(uploader,'_registry_update_attempt'))
        repair=stack.enter_context(patch.object(uploader,'_persist_and_verify_saved_metadata',
            side_effect=YouTubeUploadError('saved metadata still mismatched') if repair_fails else None))
        kwargs=dict(endpoint=DonutMcpEndpoint('http://127.0.0.1:17841/mcp/abcdefghijklmnop','http://127.0.0.1:17841','…mnop'),
            profile_id='profile-1',youtube_url='https://youtu.be/Queue123',identity='i'*64,
            attempt_id='attempt-1',report=Mock(),cancel_event=threading.Event(),
            publication_sources=sources,publication_workspace=Mock())
        if repair_fails:
            with pytest.raises(YouTubeUploadError,match='still mismatched'):
                uploader.promote_deferred_public(**kwargs)
            registry.assert_not_called()
        else:
            assert uploader.promote_deferred_public(**kwargs).youtube_url=='https://youtu.be/Queue123'
            registry.assert_called_once()
        repair.assert_called_once()
        assert repair.call_args.args[0].privacy=='PUBLIC'
        assert repair.call_args.args[3]=='https://youtu.be/Queue123'
        controller.set_file.assert_not_called()

