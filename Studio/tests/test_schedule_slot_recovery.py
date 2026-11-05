from datetime import datetime, timedelta
import json
import os
from unittest.mock import Mock, patch

import pytest

from docurandom.pipeline_checkpoint import render_completed_by
from docurandom.ui import DocuRandomApp
from docurandom.workspace import Workspace
from docurandom.workspace_scheduler import ScheduleShift, VIETNAM_TIMEZONE


class Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


UPLOAD = datetime(2026, 10, 2, 4, 30, tzinfo=VIETNAM_TIMEZONE)


@pytest.fixture
def ready_session(tmp_path):
    workspace = Workspace("slot-test", "Slot", str(tmp_path / "workspace"), "", "")
    workspace.ensure_layout()
    video = workspace.output_dir / "Video.mp4"
    package = workspace.output_dir / "Video.txt"
    video.write_bytes(b"completed video")
    package.write_text("TIÊU ĐỀ\n\nCurrent video\n\nMÔ TẢ\n\nBody text for the upload.\n\nTỪ KHÓA SEO\n\ntest\n", encoding="utf-8")
    completed = UPLOAD - timedelta(hours=1, minutes=38)
    os.utime(video, (completed.timestamp()-1, completed.timestamp()-1))
    workspace.pipeline_checkpoint_path.write_text(json.dumps({"stages": {"render": {
        "status": "complete", "updated_at": completed.isoformat(),
        "artifacts": {"video": str(video)},
    }}}), encoding="utf-8")
    return workspace, video, package


def scheduled_app(session):
    workspace, video, package = session
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.workspace = workspace
    app.closed = app.closing_after_cancel = False
    app.schedule_armed_var = Var(True)
    app.schedule_shifts = [
        ScheduleShift("night", "01/10/2026 13:30", "02/10/2026 04:30"),
        ScheduleShift("morning", "02/10/2026 04:30", "02/10/2026 08:30"),
    ]
    app.schedule_active_shift_id = "night"
    app.schedule_active_occurrence_id = "workflow:night:2026-10-01T13:30+07:00"
    app.schedule_active_stage = "ready"
    app.schedule_deferred_upload = app.schedule_deferred_public = False
    app.schedule_stage_after_public = app.schedule_stage_upload_deferred = False
    app.schedule_empty_queue_public_fallback = False
    app.schedule_manual_resume_required = app.youtube_profile_launch_blocked = False
    app.pipeline_pause_requested = app.flow_render_pending = False
    app.youtube_upload_busy = app.content_busy = app.flow_generation_busy = False
    app.pipeline_voice_busy = app.thumbnail_ai_busy = app.rendering = False
    app.scanning = app.audio_preflight_active = False
    app.youtube_auto_upload_var = Var(False)
    app.youtube_selected_profile_id = "test-profile"
    app.last_content_package = package
    app.last_video = video
    app.schedule_last_event = ""
    for name in ("_save_config", "_refresh_schedule_status", "_append_log",
                 "_schedule_log_once", "_append_exception_log", "_claim_schedule",
                 "_ensure_schedule_queue_epoch", "_repair_deferred_public_clock_drift",
                 "_resume_active_scheduled_pipeline_if_idle", "_trigger_scheduled_workflow"):
        setattr(app, name, Mock())
    app._effective_youtube_thumbnail = Mock(return_value=None)
    app._automatic_thumbnail_required_but_missing = Mock(return_value=False)
    app._start_youtube_upload = Mock(side_effect=lambda **kwargs: setattr(app, "youtube_upload_busy", True))
    return app


@pytest.mark.parametrize("wake_delay", [0, 1, 5, 60])
def test_ready_output_stages_unlisted_once_without_consuming_public_clock(ready_session, wake_delay):
    app = scheduled_app(ready_session)
    now = UPLOAD + timedelta(seconds=wake_delay)
    with patch("docurandom.ui.vietnam_now", return_value=now), patch(
        "docurandom.ui.resolve_donut_mcp_endpoint", return_value=object()
    ):
        app._schedule_tick(now=UPLOAD-timedelta(seconds=1), reschedule=False)
        app._start_youtube_upload.assert_called_once_with(
            automatic=True, defer_public_until_slot=True,
        )
        app._schedule_tick(now=now, reschedule=False)
        app._schedule_tick(now=now+timedelta(seconds=5), reschedule=False)
    app._append_exception_log.assert_not_called()
    app._start_youtube_upload.assert_called_once_with(
        automatic=True, defer_public_until_slot=True
    )
    app._claim_schedule.assert_not_called()
    assert app.schedule_shifts[0].upload_at == "02/10/2026 04:30"


def test_already_deferred_daita4_can_use_next_other_shift(ready_session):
    app = scheduled_app(ready_session)
    app.schedule_shifts[0] = ScheduleShift("night", "02/10/2026 13:30", "03/10/2026 04:30")
    app.schedule_active_stage = "deferred-upload"
    app.schedule_deferred_upload = True
    app.schedule_active_occurrence_id = "youtube-upload:night:2026-10-02T04:30+07:00"
    now = UPLOAD.replace(hour=8)
    with patch("docurandom.ui.vietnam_now", return_value=now), patch(
        "docurandom.ui.resolve_donut_mcp_endpoint", return_value=object()
    ):
        app._schedule_tick(now=now, reschedule=False)
    app._append_exception_log.assert_not_called()
    app._start_youtube_upload.assert_called_once()
    app._claim_schedule.assert_not_called()
    assert not app.schedule_deferred_upload
    assert app.schedule_shifts[1].upload_at == "02/10/2026 08:30"


def test_due_public_slot_keeps_idle_thumbnail_recovery_in_its_own_lane(ready_session):
    app = scheduled_app(ready_session)
    app.schedule_active_stage = "workflow"
    app.schedule_deferred_upload = True
    app.pipeline_voice_deferred_for_thumbnail = True
    app._scheduled_video_is_ready = Mock(return_value=False)
    app._start_content_pipeline = Mock()
    app._content_input_snapshot = Mock()
    before = app.workspace.pipeline_checkpoint_path.read_bytes()
    now = UPLOAD + timedelta(minutes=20)
    with patch("docurandom.ui.vietnam_now", return_value=now):
        for _ in range(30):
            app._schedule_tick(now=now, reschedule=False)
    app._start_content_pipeline.assert_not_called()
    app._content_input_snapshot.assert_not_called()
    app._start_youtube_upload.assert_not_called()
    app._claim_schedule.assert_not_called()
    app._append_exception_log.assert_not_called()
    assert app.workspace.pipeline_checkpoint_path.read_bytes() == before
    assert app.schedule_active_stage == "workflow"


def test_completed_late_render_can_stage_unlisted_without_moving_public_clock(ready_session):
    app = scheduled_app(ready_session)
    path = app.workspace.pipeline_checkpoint_path
    document = json.loads(path.read_text(encoding="utf-8"))
    document["stages"]["render"]["updated_at"] = (UPLOAD+timedelta(seconds=30)).isoformat()
    path.write_text(json.dumps(document), encoding="utf-8")
    app._defer_scheduled_upload_to_next_slot = Mock()
    app._prepare_immediate_render_upload(UPLOAD+timedelta(minutes=1))
    app._defer_scheduled_upload_to_next_slot.assert_not_called()
    assert app.schedule_active_stage == "upload"
    assert app.schedule_stage_upload_deferred
    app._start_youtube_upload.assert_not_called()


@pytest.mark.parametrize('stage',['workflow','retry','render','error'])
def test_successor_not_ready_cannot_consume_oldest_unlisted_public_slot(ready_session,stage):
    app=scheduled_app(ready_session)
    app.schedule_active_stage=stage
    app._scheduled_video_is_ready=Mock(return_value=False)
    app._defer_scheduled_upload_to_next_slot=Mock()
    queued={'identity':'oldest-confirmed','profile_id':'test-profile',
            'youtube_url':'https://youtu.be/OldestVideo','privacy':'UNLISTED','final_privacy':'PUBLIC'}
    (app.workspace.state_dir/'deferred_public_queue.json').write_text(
        json.dumps({'items':[queued]}),encoding='utf-8')
    assert app._skip_active_schedule_past_readiness_deadline(UPLOAD+timedelta(seconds=14)) is False
    app._defer_scheduled_upload_to_next_slot.assert_not_called()
    app._claim_schedule.assert_not_called()
    assert app.schedule_shifts[0].upload_at=='02/10/2026 04:30'


def test_unknown_public_queue_does_not_consume_readiness_deadline(ready_session):
    app=scheduled_app(ready_session)
    app.schedule_active_stage='render'
    app._scheduled_video_is_ready=Mock(return_value=False)
    app._defer_scheduled_upload_to_next_slot=Mock()
    with patch('docurandom.ui.load_schedulable_deferred_public_queue',side_effect=OSError('locked')):
        assert app._skip_active_schedule_past_readiness_deadline(UPLOAD+timedelta(hours=2)) is False
    app._defer_scheduled_upload_to_next_slot.assert_not_called()
    app._append_exception_log.assert_called_once()


def test_empty_public_queue_keeps_unfinished_render_defer_behavior(ready_session):
    app=scheduled_app(ready_session)
    app.schedule_active_stage='render'
    app._scheduled_video_is_ready=Mock(return_value=False)
    app._defer_scheduled_upload_to_next_slot=Mock(return_value=True)
    assert app._skip_active_schedule_past_readiness_deadline(UPLOAD+timedelta(hours=2)) is True
    app._defer_scheduled_upload_to_next_slot.assert_called_once()


def test_missed_public_slot_resumes_unfinished_production_without_moving_clock(ready_session):
    app = scheduled_app(ready_session)
    checkpoint = app.workspace.pipeline_checkpoint_path
    checkpoint.write_text(json.dumps({'stages': {'chatgpt': {'status': 'cancelled'}}}))
    before = checkpoint.read_bytes()
    app.schedule_active_stage = 'deferred-upload'
    app.schedule_deferred_upload = True
    app._scheduled_video_is_ready = Mock(return_value=False)
    app._content_input_snapshot = Mock(return_value={'output_dir': app.workspace.output_dir})
    app._pipeline_checkpoint_path = Mock(return_value=checkpoint)
    app._start_content_pipeline = Mock(side_effect=lambda **kw: setattr(app, 'content_busy', True))
    shifts = list(app.schedule_shifts)
    occurrence = app.schedule_active_occurrence_id
    DocuRandomApp._resume_active_scheduled_pipeline_if_idle(app)
    DocuRandomApp._resume_active_scheduled_pipeline_if_idle(app)
    app._start_content_pipeline.assert_called_once_with(resume=True)
    app._start_youtube_upload.assert_not_called()
    app._claim_schedule.assert_not_called()
    assert app.schedule_active_stage == 'workflow'
    assert app.schedule_deferred_upload
    assert app.schedule_shifts == shifts
    assert app.schedule_active_occurrence_id == occurrence
    assert checkpoint.read_bytes() == before


@pytest.mark.parametrize('gate', ['content_busy', 'thumbnail_ai_busy', 'pipeline_voice_busy',
    'rendering', 'schedule_manual_resume_required', 'pipeline_pause_requested'])
def test_deferred_production_does_not_start_over_active_or_manual_gate(ready_session, gate):
    app = scheduled_app(ready_session)
    app.schedule_active_stage = 'deferred-upload'
    app.schedule_deferred_upload = True
    app._scheduled_video_is_ready = Mock(return_value=False)
    app._start_content_pipeline = Mock()
    setattr(app, gate, True)
    DocuRandomApp._resume_active_scheduled_pipeline_if_idle(app)
    app._start_content_pipeline.assert_not_called()
    app._start_youtube_upload.assert_not_called()
    assert app.schedule_deferred_upload


@pytest.mark.parametrize("invalid", ["replaced-video", "outside-output", "missing-video", "naive-time", "incomplete", "malformed"])
def test_old_or_invalid_completion_does_not_prove_ready_by_slot(ready_session, invalid, tmp_path):
    workspace, video, _package = ready_session
    path = workspace.pipeline_checkpoint_path
    document = json.loads(path.read_text(encoding="utf-8"))
    render = document["stages"]["render"]
    if invalid == "replaced-video":
        os.utime(video, (UPLOAD.timestamp(), UPLOAD.timestamp()))
    elif invalid == "outside-output":
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(video.read_bytes())
        os.utime(outside, (UPLOAD.timestamp()-7200, UPLOAD.timestamp()-7200))
        render["artifacts"]["video"] = str(outside)
    elif invalid == "missing-video":
        render["artifacts"]["video"] = str(video.with_name("missing.mp4"))
    elif invalid == "naive-time":
        render["updated_at"] = "2026-10-02T02:51:00"
    elif invalid == "incomplete":
        render["status"] = "running"
    if invalid == "malformed":
        path.write_text("{bad", encoding="utf-8")
    else:
        path.write_text(json.dumps(document), encoding="utf-8")
    assert not render_completed_by(path, UPLOAD, output_dir=workspace.output_dir)


def test_skip_old_session_preserves_future_workflow_and_upload_clocks(ready_session):
    app = scheduled_app(ready_session)
    future = ScheduleShift(
        "night", "05/10/2026 08:30", "02/10/2026 13:30",
        workflow_claimed_for="workflow:night:2026-10-04T08:30+07:00",
        upload_claimed_for="youtube-upload:night:2026-10-01T13:30+07:00",
    )
    app.schedule_shifts[0] = future
    app.schedule_selected_shift_id = "night"
    app._load_schedule_shift_into_editor = Mock()
    with patch("docurandom.ui.vietnam_now", return_value=UPLOAD.replace(hour=7)):
        app._consume_explicit_skip_schedule_slot()
    assert app.schedule_shifts[0] == future
    assert app.schedule_shifts[1].upload_at == "02/10/2026 08:30"
    assert app.schedule_armed_var.get()


def test_skip_due_session_advances_only_that_occurrence(ready_session):
    app = scheduled_app(ready_session)
    app.schedule_selected_shift_id = "night"
    app._load_schedule_shift_into_editor = Mock()
    with patch("docurandom.ui.vietnam_now", return_value=UPLOAD.replace(hour=7)):
        app._consume_explicit_skip_schedule_slot()
    first, next_shift = app.schedule_shifts
    assert first.workflow_at == "02/10/2026 13:30"
    assert first.upload_at == "03/10/2026 04:30"
    assert first.workflow_claimed_for == "workflow:night:2026-10-01T13:30+07:00"
    assert first.upload_claimed_for == "youtube-upload:night:2026-10-02T04:30+07:00"
    assert next_shift.upload_at == "02/10/2026 08:30"


def test_explicit_skip_replacement_starts_fresh_without_claiming_any_future_clock(ready_session):
    app = scheduled_app(ready_session)
    workspace, video, package = ready_session
    workspace.pipeline_checkpoint_path.unlink()
    video.unlink()
    package.unlink()
    app.schedule_active_shift_id = ""
    app.schedule_active_stage = ""
    app.schedule_skip_replacement_pending = True
    app.schedule_skip_replacement_shift_id = "night"
    app._scheduled_video_is_ready = Mock(return_value=False)
    app._content_input_snapshot = Mock(return_value={})
    app._pipeline_checkpoint_path = Mock(return_value=workspace.pipeline_checkpoint_path)
    app._start_content_pipeline = Mock()
    before = tuple(app.schedule_shifts)
    with patch("docurandom.ui.vietnam_now", return_value=UPLOAD.replace(hour=7)):
        assert app._start_next_continuous_workflow("Explicit Skip", replace_skipped=True)
    app._claim_schedule.assert_not_called()
    app._start_content_pipeline.assert_called_once_with(resume=False)
    assert tuple(app.schedule_shifts) == before
    assert app.schedule_active_shift_id == "night"
    assert app.schedule_active_stage == "workflow"
    assert not app.schedule_skip_replacement_pending


def test_skip_replacement_will_not_overwrite_unresolved_checkpoint(ready_session):
    app = scheduled_app(ready_session)
    app.schedule_active_shift_id = ""
    app.schedule_skip_replacement_pending = True
    app.schedule_skip_replacement_shift_id = "night"
    app._scheduled_video_is_ready = Mock(return_value=False)
    app._content_input_snapshot = Mock(return_value={})
    app._pipeline_checkpoint_path = Mock(return_value=app.workspace.pipeline_checkpoint_path)
    app._start_content_pipeline = Mock()
    assert not app._start_next_continuous_workflow("Explicit Skip", replace_skipped=True)
    app._start_content_pipeline.assert_not_called()
    app._claim_schedule.assert_not_called()
    assert app.schedule_skip_replacement_pending


def test_pending_skip_replacement_is_resumed_after_restart(ready_session):
    app = scheduled_app(ready_session)
    app.schedule_skip_replacement_pending = True
    app._start_next_continuous_workflow = Mock(return_value=True)
    DocuRandomApp._resume_active_scheduled_pipeline_if_idle(app)
    app._start_next_continuous_workflow.assert_called_once_with(
        "Tiếp tục ngay sau Skip", replace_skipped=True,
    )


@pytest.mark.parametrize("hour,minute", [(9, 0), (13, 20), (23, 0)])
def test_ready_successor_waits_for_older_video_to_be_public(ready_session, hour, minute):
    app = scheduled_app(ready_session)
    app.schedule_empty_queue_public_fallback = True
    app.schedule_shifts = [
        ScheduleShift("night", "02/10/2026 13:30", "03/10/2026 04:30"),
        ScheduleShift("afternoon", "02/10/2026 08:30", "02/10/2026 13:30"),
    ]
    item = {"identity": "older-confirmed-upload", "attempt_id": "older-attempt",
            "workspace_id": app.workspace.workspace_id, "profile_id": "test-profile",
            "youtube_url": "https://youtu.be/OldVideo123", "privacy": "UNLISTED",
            "final_privacy": "PUBLIC"}
    queue_path = app.workspace.state_dir / "deferred_public_queue.json"
    queue_path.write_text(json.dumps({"version": 1, "items": [item]}), encoding="utf-8")
    queue_before = queue_path.read_bytes()
    clocks_before = tuple(app.schedule_shifts)
    now = UPLOAD.replace(hour=hour, minute=minute)
    assert not app._stage_ready_video_for_schedule_lane(now)
    assert not app._stage_ready_video_for_schedule_lane(now + timedelta(seconds=5))
    app._start_youtube_upload.assert_not_called()
    app._claim_schedule.assert_not_called()
    assert queue_path.read_bytes() == queue_before
    assert tuple(app.schedule_shifts) == clocks_before
    assert app.schedule_active_stage == "ready"
    assert not app.schedule_stage_upload_deferred


@pytest.mark.parametrize("fallback_enabled", [False, True])
def test_due_fifo_public_slot_goes_before_successor_staging(ready_session, fallback_enabled):
    app = scheduled_app(ready_session)
    app.schedule_empty_queue_public_fallback = fallback_enabled
    item = {"identity": "old-ready", "attempt_id": "old-attempt",
            "workspace_id": app.workspace.workspace_id, "profile_id": "test-profile",
            "youtube_url": "https://youtu.be/OldVideo123", "privacy": "UNLISTED",
            "final_privacy": "PUBLIC"}
    queue_path = app.workspace.state_dir / "deferred_public_queue.json"
    queue_path.write_text(json.dumps({"version": 1, "items": [item]}), encoding="utf-8")
    before = queue_path.read_bytes()
    assert not app._stage_ready_video_for_schedule_lane(UPLOAD)
    app._prepare_immediate_render_upload(UPLOAD)
    assert app.schedule_active_stage == "ready"
    app._start_youtube_upload.assert_not_called()
    app._claim_schedule.assert_not_called()
    assert queue_path.read_bytes() == before
