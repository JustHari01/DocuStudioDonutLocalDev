from contextlib import contextmanager
from types import SimpleNamespace
import threading
from unittest import mock

import pytest

from docurandom.youtube_upload import YouTubeStudioUploader, YouTubeUploadError


class ModalEditor:
    """Model the observed custom button: Enter focuses but does not commit."""

    def __init__(self, *, reject_save=False):
        self.popup_open = False
        self.selected = False
        self.saved = False
        self.reject_save = reject_save
        self.values = {}
        self.video_attachments = 0

    def navigate_and_wait(self, *args, **kwargs):
        pass

    def server_upload_progress(self, *args):
        return 100.0, ""

    def wait_dom(self, *, attributes, **kwargs):
        return {"backendNodeId": 3 if attributes == {"id": "save"} else 1}

    def wait_for(self, **kwargs):
        return {"backendNodeId": 2}

    def set_named_text(self, name, value, **kwargs):
        self.values[name] = value

    def named_text(self, name, **kwargs):
        return self.values[name]

    def select_dom_radio(self, *args, **kwargs):
        pass

    def dom_radio_checked(self, *args):
        return self.selected

    def press(self, node, *, space=False):
        if space:
            self.selected = True
        # Enter on Done or Save intentionally does not activate it.

    def press_escape(self):
        self.popup_open = False

    def click_dom(self, node):
        if node["backendNodeId"] == 2:
            self.popup_open = False
        elif node["backendNodeId"] == 3 and not self.popup_open:
            self.saved = not self.reject_save

    def dom_nodes(self):
        return [{"backendNodeId": 3}]

    def find_dom(self, *args, **kwargs):
        # This modal fixture has no sponsorship field. The separate paid
        # promotion suite exercises the real radio group and its readback.
        if str(kwargs.get('attributes', {}).get('name', '')).startswith('VIDEO_PAID_PRODUCT_PLACEMENT'):
            return None
        return {"backendNodeId": 3}

    def _dom_attributes(self, node):
        return {"disabled": ""} if self.saved else {}

    def set_file(self, *args):
        self.video_attachments += 1
        raise AssertionError("Metadata repair must not attach a video")


@contextmanager
def same_video_readback(*args, **kwargs):
    yield args[2]


@pytest.mark.parametrize("reject_save", [False, True])
def test_modal_done_must_commit_before_metadata_save(tmp_path, reject_save):
    uploader = YouTubeStudioUploader(registry_path=tmp_path / "registry.json")
    editor = ModalEditor(reject_save=reject_save)
    request = SimpleNamespace(contains_synthetic_media=False)
    sources = SimpleNamespace(
        metadata=SimpleNamespace(title="Original title", description="Original description", tags=()),
        thumbnail_path=None,
    )
    reports = mock.Mock()

    def open_popup(controller, cancel):
        controller.popup_open = True
        return True

    clock = iter(value / 2 for value in range(1000))
    with (
        mock.patch.object(uploader, "_request_identity", return_value="same-video"),
        mock.patch.object(uploader, "_registry_get", return_value={"transfer_complete": True}),
        mock.patch.object(uploader, "_effective_privacy", return_value="UNLISTED"),
        mock.patch.object(uploader, "_ensure_advanced_edit_fields"),
        mock.patch.object(uploader, "_open_edit_visibility", side_effect=open_popup),
        mock.patch.object(uploader, "_metadata_readback_controller", side_effect=same_video_readback),
        mock.patch.object(uploader, "_saved_metadata_mismatches", side_effect=lambda *args: [] if editor.saved else ["title", "privacy"]),
        mock.patch("docurandom.youtube_upload.time.monotonic", side_effect=lambda: next(clock)),
        mock.patch("docurandom.youtube_upload.time.sleep"),
    ):
        if reject_save:
            with pytest.raises(YouTubeUploadError, match="metadata vẫn không khớp"):
                uploader._persist_and_verify_saved_metadata(
                    request, sources, editor, "https://youtu.be/Exact123abc", reports, threading.Event(),
                )
            assert not any(call.args[0] == 99 for call in reports.call_args_list)
        else:
            uploader._persist_and_verify_saved_metadata(
                request, sources, editor, "https://youtu.be/Exact123abc", reports, threading.Event(),
            )
            assert editor.saved
            assert any(call.args[0] == 99 for call in reports.call_args_list)
    assert editor.video_attachments == 0


def test_enabled_save_cannot_hold_the_ui_confirmation_for_two_minutes():
    editor = ModalEditor(reject_save=True)
    clock = iter(value / 2 for value in range(2000))
    last = [0.0]

    def monotonic():
        last[0] = next(clock)
        return last[0]

    with (
        mock.patch("docurandom.youtube_upload.time.monotonic", side_effect=monotonic),
        mock.patch("docurandom.youtube_upload.time.sleep"),
    ):
        assert YouTubeStudioUploader._wait_edit_save_complete(editor, threading.Event()) is False
    assert last[0] <= 31.0
