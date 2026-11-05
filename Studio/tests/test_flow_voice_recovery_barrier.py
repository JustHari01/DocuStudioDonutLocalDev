import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from docurandom.ui import DocuRandomApp


class Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def scheduled_app(tmp_path):
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.workspace = SimpleNamespace(
        workspace_id="workspace-9", flow_history_path=tmp_path / "flow.json",
    )
    app.schedule_manual_resume_required = True
    app.schedule_manual_resume_reason = "Flow: Flow chưa ghép được với project đã lưu."
    app.schedule_active_shift_id = "shift-9"
    app.schedule_active_stage = "error"
    app._schedules_armed = Mock(return_value=True)
    app._has_safe_pending_chatgpt_turn = Mock(return_value=False)
    app._clear_manual_pipeline_resume_barrier = Mock()
    app._start_content_pipeline = Mock()
    app._append_content_chat = Mock()
    app._save_config = Mock()
    return app


@pytest.mark.parametrize("checkpoint", [
    None, {}, [],
    {"workspace_id": "workspace-9", "flow_project_id": ""},
    {"workspace_id": "other-workspace", "flow_project_id": "project-1"},
])
def test_scheduler_does_not_retry_an_unbound_or_foreign_flow_checkpoint(tmp_path, checkpoint):
    app = scheduled_app(tmp_path)
    if checkpoint is not None:
        app.workspace.flow_history_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    for _ in range(5):
        app._resume_active_scheduled_pipeline_if_idle()
    assert app.schedule_manual_resume_required
    app._clear_manual_pipeline_resume_barrier.assert_not_called()
    app._start_content_pipeline.assert_not_called()


def test_scheduler_releases_flow_barrier_after_exact_workspace_binding_is_saved(tmp_path):
    app = scheduled_app(tmp_path)
    app.workspace.flow_history_path.write_text(json.dumps({
        "workspace_id": "workspace-9", "flow_project_id": "exact-project",
    }), encoding="utf-8")
    # Stop after the barrier check; other scheduler behavior is covered separately.
    app.youtube_profile_launch_blocked = True
    app._resume_active_scheduled_pipeline_if_idle()
    app._clear_manual_pipeline_resume_barrier.assert_called_once_with()


def test_pipeline_entry_cannot_bypass_barrier_or_reset_the_current_session():
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.schedule_manual_resume_required = True
    app.content_status_var = Var()
    app._prepare_fresh_content_session = Mock()
    app._clear_manual_pipeline_resume_barrier = Mock()
    assert app._start_content_pipeline(resume=False) is False
    assert app._start_content_pipeline(resume=True, show_error_dialog=False) is False
    app._prepare_fresh_content_session.assert_not_called()
    app._clear_manual_pipeline_resume_barrier.assert_not_called()


@pytest.mark.parametrize("retry_timer,expected_stage", [(None, "error"), ("timer", "retry")])
def test_flow_completion_preserves_voice_failure_and_recovery_timer(tmp_path, retry_timer, expected_stage):
    checkpoint_path = tmp_path / "pipeline.json"
    checkpoint_path.write_text(json.dumps({"stages": {"voicevox": {
        "status": "error", "detail": "ChatGPT giữ lượt chưa thu hồi",
    }}}), encoding="utf-8")
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.pipeline_voice_result = None
    app.pipeline_voice_busy = False
    app.content_snapshot = {"project_name": "project"}
    app._pipeline_checkpoint_path = Mock(return_value=checkpoint_path)
    app.pipeline_auto_resume_after_id = retry_timer
    app.schedule_active_shift_id = "shift-3"
    app.flow_progress_var = Var()
    app.flow_percent_var = Var()
    app.source_var = Var()
    app.content_status_var = Var()
    app._record_pipeline_stage = Mock()
    app._refresh_flow_controls = Mock()
    app._append_content_chat = Mock()
    app._save_config = Mock()
    app.main_notebook = Mock()
    app.render_tab = object()
    app.start_scan = Mock()
    app._apply_flow_generation_done(tmp_path, 20, 20)
    assert app.schedule_active_stage == expected_stage
    assert app.pipeline_auto_resume_after_id == retry_timer
    assert app.flow_render_pending
    assert "lỗi đã lưu" in app.content_status_var.get()
    assert all(call.args[0] != "voicevox" for call in app._record_pipeline_stage.call_args_list)
    assert json.loads(checkpoint_path.read_text())['stages']['voicevox']['status'] == 'error'
