import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from docurandom.ui import DocuRandomApp


class Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def waiting_app(tmp_path):
    package = tmp_path / "Video.txt"
    package.write_text("saved package", encoding="utf-8")
    checkpoint = tmp_path / "pipeline.json"
    checkpoint.write_text(json.dumps({"stages": {
        "flow_images": {"status": "complete"},
        "voicevox": {"status": "pending"},
    }}), encoding="utf-8")
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.closed = app.closing_after_cancel = False
    app.pipeline_pause_requested = app.schedule_manual_resume_required = False
    app.youtube_profile_launch_blocked = False
    app.schedule_active_shift_id = "current-shift"
    app.schedule_active_stage = "workflow"
    app.pipeline_auto_resume_after_id = None
    app.content_busy = app.flow_generation_busy = app.pipeline_voice_busy = False
    app.thumbnail_ai_busy = app.rendering = app.scanning = False
    app.audio_preflight_active = app.youtube_upload_busy = False
    app.flow_render_pending = True
    app.pipeline_voice_result = None
    app.pipeline_voice_deferred_for_thumbnail = True
    app.pipeline_voice_deferred_package = package
    app._schedules_armed = Mock(return_value=True)
    app._content_input_snapshot = Mock(return_value={"project_name": "Video"})
    app._pipeline_checkpoint_path = Mock(return_value=checkpoint)
    app._start_content_pipeline = Mock()
    app._save_config = Mock()
    app._append_log = Mock()
    app._start_pipeline_voice_prefetch = Mock()
    app._effective_youtube_thumbnail = Mock(return_value=None)
    return app, checkpoint


@pytest.mark.parametrize("stage", ["workflow", "retry", "error", "render"])
def test_waiting_thumbnail_does_not_replay_pipeline_or_change_checkpoint(tmp_path, stage):
    app, checkpoint = waiting_app(tmp_path)
    app.schedule_active_stage = stage
    saved = checkpoint.read_bytes()
    for _ in range(30):
        app._resume_active_scheduled_pipeline_if_idle()
    app._content_input_snapshot.assert_not_called()
    app._start_content_pipeline.assert_not_called()
    app._save_config.assert_not_called()
    assert checkpoint.read_bytes() == saved
    assert app.schedule_active_stage == stage


def test_idle_pipeline_still_resumes_when_it_is_not_waiting_for_thumbnail(tmp_path):
    app, checkpoint = waiting_app(tmp_path)
    app.pipeline_voice_deferred_for_thumbnail = False
    with patch("docurandom.ui.pipeline_resume_summary", return_value=("voicevox", [])):
        app._resume_active_scheduled_pipeline_if_idle()
    app._start_content_pipeline.assert_called_once_with(resume=True)
    assert checkpoint.exists()


def test_unknown_submitted_thumbnail_keeps_shared_chat_and_scheduler_waiting(tmp_path):
    app, checkpoint = waiting_app(tmp_path)
    app.workspace = object()
    with patch("docurandom.ui.read_thumbnail_ai_state", return_value={
        "package_path": str(app.pipeline_voice_deferred_package),
        "status": "error", "submitted": True,
    }):
        app._maybe_start_deferred_pipeline_voice()
    app._resume_active_scheduled_pipeline_if_idle()
    app._start_pipeline_voice_prefetch.assert_not_called()
    app._start_content_pipeline.assert_not_called()
    assert app.pipeline_voice_deferred_for_thumbnail
    assert json.loads(checkpoint.read_text())['stages']['voicevox']['status'] == 'pending'


def test_selected_thumbnail_releases_voice_without_restarting_pipeline(tmp_path):
    app, _checkpoint = waiting_app(tmp_path)
    app._effective_youtube_thumbnail.return_value = tmp_path / "thumbnail.png"
    app._maybe_start_deferred_pipeline_voice()
    assert not app.pipeline_voice_deferred_for_thumbnail
    assert app.pipeline_voice_deferred_package is None
    app._start_pipeline_voice_prefetch.assert_called_once_with()
    app._start_content_pipeline.assert_not_called()


def test_flow_completion_reports_thumbnail_wait_and_preserves_completed_images(tmp_path):
    app, _checkpoint = waiting_app(tmp_path)
    app.content_snapshot = {"project_name": "Video"}
    app.flow_progress_var = Var()
    app.flow_percent_var = Var()
    app.source_var = Var()
    app.content_status_var = Var()
    app._refresh_flow_controls = Mock()
    app._record_pipeline_stage = Mock()
    app._append_content_chat = Mock()
    app.main_notebook = Mock()
    app.render_tab = object()
    app.start_scan = Mock()
    app._apply_flow_generation_done(tmp_path, 20, 20)
    assert "thumbnail" in app.content_status_var.get()
    voice_calls = [c for c in app._record_pipeline_stage.call_args_list if c.args[0] == "voicevox"]
    assert len(voice_calls) == 1
    assert voice_calls[0].args[1] == "pending"
    assert "thumbnail" in voice_calls[0].args[2]
    app._resume_active_scheduled_pipeline_if_idle()
    app._start_content_pipeline.assert_not_called()
