"""Startup recovery must not upload or publish a video with a failed thumbnail."""
import json
from unittest.mock import Mock, patch

from docurandom.ui import DocuRandomApp
from docurandom.workspace import Workspace


class Var:
    def __init__(self, value): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value


def startup_app(tmp_path):
    workspace = Workspace("startup", "Startup", str(tmp_path / "workspace"), "", "")
    workspace.ensure_layout()
    package = workspace.output_dir / "Video.txt"
    package.write_text("TIÊU ĐỀ\nVideo B\n\nMÔ TẢ\nVideo B description\n\nSEO TAGS\n技術\n", encoding="utf-8")
    video = workspace.output_dir / "Video.mp4"
    video.write_bytes(b"completed video B")
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.workspace = workspace
    app.closed = app.closing_after_cancel = app.youtube_upload_busy = False
    app.last_content_package = None
    app.youtube_thumbnail_auto_ai_var = Var(True)
    app.youtube_status_var = Var("")
    app.youtube_pending_videos = []
    app._effective_youtube_thumbnail = Mock(return_value=None)
    app._cancel_youtube_checkpoint_recovery = Mock()
    app._append_log = Mock()
    app._schedule_log_once = Mock()
    app._save_config = Mock()
    app._automatic_public_promotion_allowed = Mock(return_value=True)
    return app, package, video


def test_startup_queued_upload_checks_package_on_disk_before_opening_profile(tmp_path):
    app, package, video = startup_app(tmp_path)
    checkpoint = app.workspace.state_dir / "youtube_upload.json"
    checkpoint.write_text(json.dumps({"status": "queued", "video_path": str(video)}), encoding="utf-8")
    saved = checkpoint.read_bytes()
    with patch("docurandom.ui.confirmed_manual_upload", return_value=None), \
         patch("docurandom.ui.resolve_donut_mcp_endpoint") as endpoint:
        app._start_youtube_upload(video, automatic=True)
    endpoint.assert_not_called()
    assert app.youtube_pending_videos == [video]
    assert "chờ thumbnail" in app.youtube_status_var.get()
    assert checkpoint.read_bytes() == saved
    assert package.is_file()
    assert app.schedule_active_stage == "ready"


def test_missing_thumbnail_keeps_current_render_out_of_orphan_upload_recovery(tmp_path):
    app, package, video = startup_app(tmp_path)
    app.schedule_active_stage = "upload"
    app.schedule_active_shift_id = "current-shift"
    app.schedule_active_occurrence_id = "current-occurrence"
    app.schedule_stage_upload_deferred = True
    with patch("docurandom.ui.confirmed_manual_upload", return_value=None), \
         patch("docurandom.ui.has_pending_prior_public", return_value=False), \
         patch("docurandom.ui.resolve_donut_mcp_endpoint") as endpoint:
        app._start_youtube_upload(video, automatic=True)
        app._start_youtube_upload(video, automatic=True)
    endpoint.assert_not_called()
    assert app.schedule_active_stage == "ready"
    assert app.schedule_active_shift_id == "current-shift"
    assert app.schedule_active_occurrence_id == "current-occurrence"
    assert app.schedule_stage_upload_deferred is True
    assert app.youtube_pending_videos == [video]
    app._save_config.assert_called_once()
    assert video.read_bytes() == b"completed video B"
    assert package.is_file()


def test_missing_thumbnail_does_not_reclassify_an_already_started_draft(tmp_path):
    app, _package, video = startup_app(tmp_path)
    app.schedule_active_stage = "youtube-retry"
    checkpoint = app.workspace.state_dir / "youtube_upload.json"
    saved = json.dumps({"status": "uncertain", "video_path": str(video),
                        "attempt_id": "same-attempt", "youtube_url": "https://youtu.be/exact-draft"}).encode()
    checkpoint.write_bytes(saved)
    with patch("docurandom.ui.confirmed_manual_upload", return_value=None), \
         patch("docurandom.ui.has_pending_prior_public", return_value=False):
        app._start_youtube_upload(video, automatic=True)
    assert app.schedule_active_stage == "youtube-retry"
    assert checkpoint.read_bytes() == saved
    app._save_config.assert_not_called()


def test_ready_render_waits_before_staging_and_resumes_when_thumbnail_arrives(tmp_path):
    app, _package, _video = startup_app(tmp_path)
    app.schedule_active_stage = "ready"
    app.schedule_last_event = ""
    app.schedule_active_occurrence_id = "keep-public-clock"
    app.schedule_stage_upload_deferred = False
    app._schedules_armed = Mock(return_value=True)
    app._active_schedule_upload_enabled = Mock(return_value=True)
    app._automatic_public_promotion_allowed = Mock(return_value=False)
    app._next_scheduled_upload_at = Mock(return_value=None)
    with patch("docurandom.ui.load_schedulable_deferred_public_queue", return_value=[]):
        assert app._prepare_immediate_render_upload()
        assert app._prepare_immediate_render_upload()
        assert app.schedule_active_stage == "ready"
        assert app.schedule_stage_upload_deferred is False
        assert app.schedule_active_occurrence_id == "keep-public-clock"
        app._save_config.assert_called_once()
        app._automatic_public_promotion_allowed.assert_not_called()
        app._effective_youtube_thumbnail.return_value = tmp_path / "verified.png"
        assert app._prepare_immediate_render_upload()
    assert app.schedule_active_stage == "upload"
    assert app.schedule_stage_upload_deferred is True
    assert app.schedule_active_occurrence_id == "keep-public-clock"


def test_disk_package_guard_releases_when_user_supplies_image(tmp_path):
    app, _package, _video = startup_app(tmp_path)
    assert app._automatic_thumbnail_required_but_missing()
    app._effective_youtube_thumbnail.return_value = tmp_path / "user-thumbnail.png"
    assert not app._automatic_thumbnail_required_but_missing()
    app._effective_youtube_thumbnail.return_value = None
    app.youtube_thumbnail_auto_ai_var.set(False)
    assert app._automatic_thumbnail_required_but_missing()


def test_legacy_unlisted_without_thumbnail_is_not_published_or_removed(tmp_path):
    app, _package, _video = startup_app(tmp_path)
    archive = tmp_path / "Uploaded" / "old-video"
    (archive / "State").mkdir(parents=True)
    state = archive / "State" / "thumbnail-ai.json"
    state.write_text(json.dumps({"status": "terminal_no_image", "submitted": False,
                                 "thumbnail_path": ""}), encoding="utf-8")
    item = {"identity": "same-video", "archive_dir": str(archive),
            "thumbnail_path": "", "youtube_url": "https://youtu.be/sameVideoId",
            "profile_id": "same-profile", "final_privacy": "PUBLIC"}
    queue = app.workspace.state_dir / "deferred_public_queue.json"
    queue.write_text(json.dumps({"version": 1, "items": [item]}), encoding="utf-8")
    saved = queue.read_bytes()
    with patch("docurandom.ui.resolve_donut_mcp_endpoint") as endpoint:
        assert not app._start_deferred_public_promotion(item)
    endpoint.assert_not_called()
    assert "Chưa đủ nguyên liệu Public" in app.schedule_last_event
    assert queue.read_bytes() == saved
    assert state.is_file()
    item["thumbnail_path"] = str(archive / "verified-thumbnail.png")
    assert not app._deferred_public_thumbnail_failed(item)


def test_legacy_unrelated_state_does_not_get_classified_as_failed_thumbnail(tmp_path):
    app, _package, _video = startup_app(tmp_path)
    archive = tmp_path / "Uploaded" / "old-video"
    (archive / "State").mkdir(parents=True)
    state = archive / "State" / "thumbnail-ai.json"
    for status in ("complete", "overridden", "uncertain"):
        state.write_text(json.dumps({"status": status, "submitted": False}), encoding="utf-8")
        assert not app._deferred_public_thumbnail_failed({"archive_dir": str(archive)})
