from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from docurandom.manual_upload import (
    clear_completed_manual_launch_block,
    confirmed_manual_upload,
    manual_upload_marker,
)
from docurandom.ui import DocuRandomApp
from docurandom.workspace import Workspace
from docurandom.workspace_scheduler import ScheduleEvaluation, ScheduleShift, VIETNAM_TIMEZONE


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def session(tmp_path):
    w = Workspace("manual-test", "Manual", str(tmp_path / "workspace"), "", "")
    w.ensure_layout()
    video = w.output_dir / "Video.mp4"
    package = w.output_dir / "Video.txt"
    video.write_bytes(b"original rendered video")
    package.write_text("TIÊU ĐỀ\nManual title\n\nMÔ TẢ\nExample video description.\n\nTỪ KHÓA SEO\ntest\n", encoding="utf-8")
    before = datetime.now(timezone.utc) - timedelta(hours=1)
    for path in (video, package):
        os.utime(path, (before.timestamp(), before.timestamp()))
    write(w.pipeline_checkpoint_path, {"stages": {"render": {
        "status": "complete", "updated_at": before.isoformat(),
        "artifacts": {"video": str(video)},
    }}})
    marker = manual_upload_marker(video, package, title="Manual title", profile_id="profile-4")
    write(w.state_dir / "manual_youtube_upload.json", marker)
    settings = {
        "youtube_profile_id": "profile-4",
        "youtube_profile_launch_blocked": True,
        "youtube_profile_launch_block_reason": "old refusal",
        "youtube_profile_launch_blocked_profile_id": "profile-4",
        "schedule_armed": True, "schedule_shifts": [{"workflow_at": "04:30", "upload_at": "08:30"}],
        "unrelated_setting": {"preserve": True},
    }
    write(w.settings_path, settings)
    return w, video, package, marker, settings


@pytest.mark.parametrize("legacy", [False, True])
def test_exact_manual_confirmation_retires_only_old_error(session, legacy):
    w, video, package, marker, settings = session
    if legacy:
        for key in ("profile_id", "video_identity", "package_identity"):
            marker.pop(key)
        marker["version"] = 1
        write(w.state_dir / "manual_youtube_upload.json", marker)
    before = {p: p.read_bytes() for p in (video, package, w.pipeline_checkpoint_path)}
    assert confirmed_manual_upload(w) is not None
    assert clear_completed_manual_launch_block(w, settings)
    assert settings["youtube_profile_launch_blocked"] is False
    assert settings["schedule_armed"] is True
    assert settings["schedule_shifts"] == [{"workflow_at": "04:30", "upload_at": "08:30"}]
    assert settings["unrelated_setting"] == {"preserve": True}
    assert all(p.read_bytes() == data for p, data in before.items())
    assert not (w.state_dir / "youtube_upload.json").exists()


@pytest.mark.parametrize("change", ["title", "path", "profile", "video-content", "new-render", "new-refusal", "unresolved-upload", "malformed-upload"])
def test_stale_mismatched_or_uncertain_evidence_keeps_block(session, change):
    w, video, package, marker, settings = session
    if change == "title":
        marker["title"] = "Other title"
    elif change == "path":
        marker["video_path"] = str(video.parent / "Other.mp4")
    elif change == "profile":
        marker["profile_id"] = "profile-16"
    elif change == "video-content":
        old = video.stat()
        video.write_bytes(b"a different video with the same file name")
        os.utime(video, ns=(old.st_atime_ns, old.st_mtime_ns))
    elif change == "new-render":
        pipeline = json.loads(w.pipeline_checkpoint_path.read_text("utf-8"))
        pipeline["stages"]["render"]["updated_at"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        write(w.pipeline_checkpoint_path, pipeline)
    elif change == "new-refusal":
        settings["youtube_profile_launch_blocked_at"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    elif change == "unresolved-upload":
        write(w.state_dir / "youtube_upload.json", {"status": "uploading", "video_path": str(video)})
    elif change == "malformed-upload":
        (w.state_dir / "youtube_upload.json").write_text("{broken", encoding="utf-8")
    write(w.state_dir / "manual_youtube_upload.json", marker)
    before = dict(settings)
    assert not clear_completed_manual_launch_block(w, settings)
    assert settings == before


def test_load_config_recovers_old_block_without_touching_checkpoint(session):
    w, video, package, marker, settings = session
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.workspace = w
    checkpoint = w.pipeline_checkpoint_path.read_bytes()
    restored = app._load_config()
    assert restored["youtube_profile_launch_blocked"] is False
    assert json.loads(w.settings_path.read_text("utf-8")) == restored
    assert w.pipeline_checkpoint_path.read_bytes() == checkpoint
    assert video.is_file()


def test_manual_completion_never_launches_another_browser_upload(session):
    w, video, package, marker, settings = session
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.workspace = w
    app.closed = app.closing_after_cancel = False
    app.youtube_status_var = Mock()
    app.youtube_percent_var = Mock()
    app._cancel_youtube_checkpoint_recovery = Mock()
    with patch("docurandom.ui.resolve_donut_mcp_endpoint") as endpoint:
        app._start_youtube_upload(automatic=True)
    endpoint.assert_not_called()
    app._cancel_youtube_checkpoint_recovery.assert_called_once_with(reset_count=True)
    app.youtube_percent_var.set.assert_called_once_with("100%")
    assert not app._scheduled_video_is_ready()


def test_fresh_workflow_archives_exact_manual_session_without_losing_data(session):
    w, video, package, marker, settings = session
    media = w.media_dir / "source.png"
    media.write_bytes(b"original image")
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.workspace = w
    for key in ("rendering", "voicevox_busy", "content_busy", "flow_generation_busy", "youtube_upload_busy"):
        setattr(app, key, False)
    app.has_update_blocking_tasks = Mock(return_value=False)
    app._cancel_youtube_checkpoint_recovery = Mock()
    app._reset_video_chat_session = Mock()
    app._save_config = Mock()
    app._append_log = Mock()
    app.job_results, app.job_states, app.youtube_pending_videos = {}, {}, []
    assert app._prepare_fresh_content_session(confirm=False)
    archives = list((w.root / "Skipped").iterdir())
    assert len(archives) == 1
    archive = archives[0]
    assert (archive / "Output" / video.name).read_bytes() == b"original rendered video"
    assert (archive / "Media" / media.name).read_bytes() == b"original image"
    assert json.loads((archive / "State" / "manual_youtube_upload.json").read_text("utf-8")) == marker
    assert not video.exists()
    assert not w.pipeline_checkpoint_path.exists()
    assert not (w.state_dir / "youtube_upload.json").exists()
    assert app.last_video is None
    assert app.schedule_active_stage == ""
    assert json.loads(w.settings_path.read_text("utf-8"))["schedule_armed"] is True


def test_next_workflow_clock_starts_fresh_instead_of_reusing_manual_mp4(session):
    w, video, package, marker, settings = session
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.workspace = w
    app.closed = app.closing_after_cancel = False
    for key in ("content_busy", "flow_generation_busy", "pipeline_voice_busy", "voicevox_busy", "rendering", "scanning", "audio_preflight_active", "youtube_upload_busy"):
        setattr(app, key, False)
    app.youtube_selected_profile_id = "profile-4"
    app.youtube_profile_launch_blocked = True
    app.youtube_profile_launch_blocked_profile_id = "profile-4"
    app.schedule_active_shift_id = ""
    app.schedule_active_stage = ""
    app.schedule_shifts = [ScheduleShift("morning", "02/10/2026 04:30", "02/10/2026 08:30")]
    app.has_update_blocking_tasks = Mock(return_value=False)
    app._cancel_youtube_checkpoint_recovery = Mock()
    app._reset_video_chat_session = Mock()
    app._save_config = Mock()
    app._append_log = Mock()
    app._effective_youtube_thumbnail = Mock(return_value=None)
    app._content_input_snapshot = Mock(return_value={})
    app._pipeline_checkpoint_path = Mock(return_value=w.pipeline_checkpoint_path)
    app._claim_schedule = Mock()
    app._start_content_pipeline = Mock()
    app._start_youtube_upload = Mock()
    app.job_results, app.job_states, app.youtube_pending_videos = {}, {}, []
    due_at = datetime(2026, 10, 2, 4, 30, tzinfo=VIETNAM_TIMEZONE)
    due = ScheduleEvaluation("due", scheduled_at=due_at, occurrence_id="morning-20261002")
    with patch("docurandom.ui.vietnam_now", return_value=due_at):
        app._trigger_scheduled_workflow(due, shift_id="morning")
    app._start_content_pipeline.assert_called_once_with(resume=False)
    app._start_youtube_upload.assert_not_called()
    assert not video.exists()
    assert app.youtube_profile_launch_blocked is False
    assert len(list((w.root / "Skipped").iterdir())) == 1
