import json
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from docurandom.donut_mcp import DonutMcpEndpoint
from docurandom.workspace import Workspace
from docurandom.youtube_upload import (
    WorkspaceUploadSources, YouTubeMetadata, YouTubeStudioUploader,
    YouTubeUploadCancelled, YouTubeUploadRequest, YouTubeUploadUncertain,
)


@pytest.mark.parametrize("cancel_at_save", [False, True])
def test_save_and_shutdown_keep_the_allocated_video_and_original_target(tmp_path, cancel_at_save):
    workspace = Workspace("workspace", "Demo", str(tmp_path), "", "")
    workspace.ensure_layout()
    video = workspace.output_dir / "original.mp4"
    video.write_bytes(b"original attached file")
    package = workspace.output_dir / "original.txt"
    package.write_text("original package", encoding="utf-8")
    sources = WorkspaceUploadSources(video, package, None, YouTubeMetadata("title", "description"))
    request = YouTubeUploadRequest(workspace,
        DonutMcpEndpoint.parse("http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"),
        "profile", video_path=video, package_path=package, privacy="UNLISTED")
    uploader = YouTubeStudioUploader(registry_path=tmp_path / "registry.json")
    identity = uploader._request_identity(request, sources)
    uploader._registry_set(identity, {"attempt_id": "original-attempt", "profile_id": "profile",
        "workspace_id": "workspace", "status": "uploading"})
    controller = mock.Mock(bridge_tab_id=177, automation_target_id="original-target")
    controller.youtube_url.return_value = "https://youtu.be/Exact123abc"
    if cancel_at_save:
        controller.press.side_effect = YouTubeUploadCancelled("normal shutdown")

    with (
        mock.patch.object(uploader, "_source_video_duration_seconds", return_value=None),
        mock.patch.object(uploader, "_try_completed_wizard_handoff", return_value=None),
        mock.patch.object(uploader, "_advance_upload_wizard_to_visibility"),
        mock.patch.object(uploader, "_wait_for_upload_transfer_complete",
                          side_effect=YouTubeUploadCancelled("normal shutdown")),
    ):
        with pytest.raises(YouTubeUploadCancelled):
            uploader._continue_attached_upload(request, sources,
                SimpleNamespace(name="Demo"), "Channel", identity, "original-attempt",
                controller, mock.Mock(), {}, mock.Mock(), threading.Event())

    # The cancellation handler must preserve the checkpoint created before Save.
    with (
        mock.patch("docurandom.youtube_upload.resolve_workspace_upload_sources", return_value=sources),
        mock.patch("docurandom.youtube_upload._wait_for_render_upload_ready"),
        mock.patch.object(uploader, "_wait_for_existing_file_transfers"),
        mock.patch.object(uploader, "_execute_locked_upload",
                          side_effect=YouTubeUploadCancelled("normal shutdown")),
    ):
        with pytest.raises(YouTubeUploadUncertain):
            uploader.upload(request)

    checkpoint = json.loads(uploader._checkpoint_path(workspace).read_text(encoding="utf-8"))
    assert checkpoint["status"] == "uncertain"
    assert checkpoint["attempt_id"] == "original-attempt"
    assert checkpoint["youtube_url"] == "https://youtu.be/Exact123abc"
    assert checkpoint["target_id"] == "original-target"
    assert checkpoint["bridge_tab_id"] == 177
    assert checkpoint.get("transfer_complete") is not True
    assert uploader._registry_get(identity)["attempt_id"] == "original-attempt"
    controller.set_file.assert_not_called()
