from pathlib import Path
from unittest.mock import Mock
from docurandom.youtube_upload import _SemanticCdpController

def test_pointer_scrolls_offscreen_control_before_reading_geometry():
    class ScrolledEditor:
        def __init__(self):self.scrolled=False;self.clicks=[]
        def command(self,method,params):
            if method=='DOM.scrollIntoViewIfNeeded':
                assert params=={'backendNodeId':10};self.scrolled=True
                return {}
            if method=='DOM.getBoxModel':
                y=100 if self.scrolled else -1500
                return {'model':{'content':[200,y,300,y,300,y+40,200,y+40]}}
            if method=='Input.dispatchMouseEvent':
                assert self.scrolled and params['x']==250 and params['y']==120
                self.clicks.append(params['type']);return {}
            raise AssertionError(method)
    page=ScrolledEditor();controller=_SemanticCdpController(page)
    controller.click_dom({'backendNodeId':10})
    assert page.clicks==['mouseMoved','mousePressed','mouseReleased']

def test_image_arms_only_visible_custom_thumbnail_picker_then_attaches_once(tmp_path):
    c=_SemanticCdpController(Mock());button={'backendNodeId':10};file={'backendNodeId':20}
    c.dom_nodes=Mock(return_value=[]);c.find_dom=Mock(return_value=button)
    c.find_file_input=Mock(return_value=file);c.click_dom=Mock()
    c.set_file(tmp_path/'image.png','image')
    c.click_dom.assert_called_once_with(button)
    methods=[call.args[0]for call in c.cdp.command.call_args_list]
    assert methods==['Page.setInterceptFileChooserDialog','DOM.setFileInputFiles','Page.setInterceptFileChooserDialog']
    assert c.cdp.command.call_args_list[-1].args[1]=={'enabled':False}

def test_video_attachment_does_not_click_thumbnail_or_change_picker_mode(tmp_path):
    c=_SemanticCdpController(Mock());c.dom_nodes=Mock(return_value=[])
    c.find_dom=Mock();c.find_file_input=Mock(return_value={'backendNodeId':20});c.click_dom=Mock()
    c.set_file(tmp_path/'video.mp4','video')
    c.find_dom.assert_not_called();c.click_dom.assert_not_called()
    assert [call.args[0]for call in c.cdp.command.call_args_list]==['DOM.setFileInputFiles']
