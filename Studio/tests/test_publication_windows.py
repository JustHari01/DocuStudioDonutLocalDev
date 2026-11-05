"""Two-hour publication windows, exact-video deadlines and FIFO handoff."""
from datetime import datetime, timedelta
import json
from unittest.mock import Mock, patch

import pytest

from docurandom import ui as ui_module
from docurandom.ui import DocuRandomApp
from docurandom.workspace import Workspace
from docurandom.workspace_scheduler import ScheduleShift, ScheduleEvaluation, VIETNAM_TIMEZONE
from docurandom.youtube_upload import YouTubeStudioUploader, YouTubeUploadCancelled


class Var:
    def __init__(self, value): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value


def at(hour, minute=0, day=5, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=VIETNAM_TIMEZONE)


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Recurring-clock advancement must use the same simulated time as the UI.
    monkeypatch.setattr('docurandom.workspace_scheduler.vietnam_now',
                        lambda: ui_module.vietnam_now())
    a = DocuRandomApp.__new__(DocuRandomApp)
    a.workspace = Workspace('window', 'Window', str(tmp_path/'workspace'), '', '')
    a.workspace.ensure_layout()
    a.closed = a.closing_after_cancel = a.youtube_upload_busy = False
    a.schedule_manual_resume_required = False
    a.schedule_armed_var = Var(True)
    a.schedule_shifts = [ScheduleShift(str(h), '05/10/2026 01:00', f'05/10/2026 {h:02}:30')
                         for h in (4, 8, 13)]
    a.schedule_selected_shift_id = '13'
    a.schedule_active_shift_id = 'production-B'
    a.schedule_active_stage = 'render'
    a.schedule_waiting_log_keys = set()
    a._scheduled_video_is_ready = Mock(return_value=False)
    a._start_deferred_public_promotion = Mock(return_value=True)
    for name in ('_save_config', '_refresh_schedule_shift_tree', '_refresh_schedule_controls',
                 '_append_log', '_schedule_log_once', '_load_schedule_shift_into_editor'):
        setattr(a, name, Mock())
    item = {'identity':'A', 'attempt_id':'attempt-A', 'profile_id':'profile',
            'youtube_url':'https://youtu.be/VideoA123', 'privacy':'UNLISTED', 'final_privacy':'PUBLIC'}
    (a.workspace.state_dir/'deferred_public_queue.json').write_text(
        json.dumps({'version':1, 'items':[item]}), encoding='utf-8')
    return a


@pytest.mark.parametrize('hour,minute,allowed', [
    (0,0,False),(4,29,False),(4,30,True),(6,29,True),(6,30,False),
    (8,29,False),(8,30,True),(10,29,True),(10,30,False),(12,0,False),
    (13,29,False),(13,30,True),(14,53,True),(15,0,True),(15,29,True),
    (15,30,False),(23,59,False),
])
def test_exact_two_hour_windows(app, hour, minute, allowed):
    assert app._automatic_public_promotion_allowed(at(hour, minute)) is allowed


def test_latest_due_slot_cannot_replay_an_expired_morning_slot(app):
    queue = app.workspace.state_dir/'deferred_public_queue.json'
    before = queue.read_bytes()
    with patch('docurandom.ui.vietnam_now', return_value=at(9)):
        app._trigger_scheduled_upload(ScheduleEvaluation('due', at(4,30), 'old-0430'), shift_id='4')
        app._start_deferred_public_promotion.assert_not_called()
        assert app.schedule_shifts[0].upload_at == '06/10/2026 04:30'
        app._trigger_scheduled_upload(ScheduleEvaluation('due', at(8,30), 'current-0830'), shift_id='8')
    app._start_deferred_public_promotion.assert_called_once()
    assert queue.read_bytes() == before
    assert app.schedule_active_stage == 'render'


@pytest.mark.parametrize('hour,minute', [(6,30),(10,30),(15,30)])
def test_expired_slot_advances_only_its_clock_and_preserves_queue(app, hour, minute):
    start = at(hour-2, minute)
    before = (app.workspace.state_dir/'deferred_public_queue.json').read_bytes()
    with patch('docurandom.ui.vietnam_now', return_value=at(hour, minute)):
        app._trigger_scheduled_upload(ScheduleEvaluation('due',start,'expired'), shift_id=str(hour-2))
    app._start_deferred_public_promotion.assert_not_called()
    assert (app.workspace.state_dir/'deferred_public_queue.json').read_bytes() == before
    assert app.schedule_active_shift_id == 'production-B'


def test_afternoon_1453_is_eligible_and_claim_blocks_second_video(app):
    occurrence = ScheduleEvaluation('due', at(13,30), 'youtube-upload:13:2026-10-05T13:30+07:00')
    with patch('docurandom.ui.vietnam_now', return_value=at(14,53)):
        app._trigger_scheduled_upload(occurrence, shift_id='13')
    app._start_deferred_public_promotion.assert_called_once()
    assert app.schedule_shifts[2].upload_at == '06/10/2026 13:30'
    assert app.schedule_shifts[2].upload_claimed_for == occurrence.occurrence_id
    assert not app._all_schedule_evaluations(at(14,54))[2][2].due


def test_retry_restores_same_slot_with_backoff_only_before_deadline(app):
    occurrence = ScheduleEvaluation('due', at(13,30), 'public-1330')
    app._remember_deferred_public_schedule_slot(occurrence, shift_id='13')
    with patch('docurandom.ui.vietnam_now', return_value=at(14)):
        app._claim_deferred_public_schedule_slot(occurrence, shift_id='13')
        assert app._retry_deferred_public_within_window()
        assert app.schedule_shifts[2].upload_at == '05/10/2026 13:30'
        app._trigger_scheduled_upload(occurrence, shift_id='13')
    app._start_deferred_public_promotion.assert_not_called()
    app._remember_deferred_public_schedule_slot(occurrence, shift_id='13')
    with patch('docurandom.ui.vietnam_now', return_value=at(15,30)):
        assert not app._retry_deferred_public_within_window()


def test_restart_keeps_unclaimed_afternoon_window_and_its_queue(app):
    app.saved_schedule_startup_intent = True
    app._refresh_schedule_status = Mock()
    queue = app.workspace.state_dir/'deferred_public_queue.json'
    before = queue.read_bytes()
    with patch('docurandom.ui.vietnam_now', return_value=at(14,53)):
        app._restore_persisted_schedule_on_startup()
    assert app.schedule_shifts[2].upload_at == '05/10/2026 13:30'
    assert queue.read_bytes() == before


def test_unfinished_render_has_full_two_hours_and_is_never_discarded(app):
    app.schedule_active_shift_id = '13'
    app._defer_scheduled_upload_to_next_slot = Mock(return_value=True)
    for current in (at(13,30),at(14,53),at(15,29)):
        assert not app._skip_active_schedule_past_readiness_deadline(current)
    app._defer_scheduled_upload_to_next_slot.assert_not_called()
    # The queued A still owns Public even when B is incomplete at the deadline.
    assert not app._skip_active_schedule_past_readiness_deadline(at(15,30))
    app._defer_scheduled_upload_to_next_slot.assert_not_called()


def test_expired_worker_never_acquires_profile_or_changes_visibility():
    uploader = YouTubeStudioUploader.__new__(YouTubeStudioUploader)
    uploader.coordinator = Mock()
    with patch('docurandom.youtube_upload.datetime') as clock:
        clock.now.return_value = at(15,30)
        with pytest.raises(YouTubeUploadCancelled, match='2 tiếng'):
            uploader.promote_deferred_public(endpoint=Mock(),profile_id='p',
                youtube_url='https://youtu.be/VideoA123',public_deadline_at=at(15,30))
    uploader.coordinator.acquire.assert_not_called()


def test_window_closing_during_modal_wait_never_commits_public():
    uploader = YouTubeStudioUploader.__new__(YouTubeStudioUploader)
    uploader._open_edit_visibility = Mock(return_value=True)
    controller = Mock()
    controller.dom_radio_checked.side_effect = [False, True]
    controller.has_background_bridge = True
    with patch('docurandom.youtube_upload.datetime') as clock:
        clock.now.side_effect = [at(15,29),at(15,30),at(15,30)]
        with pytest.raises(YouTubeUploadCancelled):
            uploader._set_edit_visibility(controller,'PUBLIC',Mock(is_set=Mock(return_value=False)),
                                           public_deadline_at=at(15,30))
    controller.click_dom.assert_not_called()
    assert not any(call.args[0]=='youtube_click' for call in controller._bridge_command.call_args_list)


@pytest.mark.parametrize('stage', ['chatgpt', 'Flow', 'VOICEVOX', 'thumbnail', 'render', 'precheck đầu vào'])
def test_content_resume_barrier_does_not_block_confirmed_fifo_public(app, stage):
    app.schedule_manual_resume_required = True
    app.schedule_manual_resume_reason = f'{stage}: cần kiểm tra checkpoint'
    app.schedule_active_stage = 'error'
    before = (app.workspace.state_dir/'deferred_public_queue.json').read_bytes()
    occurrence = ScheduleEvaluation('due', at(13,30), 'youtube-upload:13:2026-10-05T13:30+07:00')
    with patch('docurandom.ui.vietnam_now', return_value=at(14)):
        app._trigger_scheduled_upload(occurrence, shift_id='13')
    app._start_deferred_public_promotion.assert_called_once()
    assert app.schedule_shifts[2].upload_claimed_for == occurrence.occurrence_id
    assert app.schedule_manual_resume_required
    assert app.schedule_manual_resume_reason == f'{stage}: cần kiểm tra checkpoint'
    assert app.schedule_active_stage == 'error'
    assert app.schedule_active_shift_id == 'production-B'
    assert (app.workspace.state_dir/'deferred_public_queue.json').read_bytes() == before
    assert not app._all_schedule_evaluations(at(14,1))[2][2].due


@pytest.mark.parametrize('reason', ['YouTube checkpoint sai cấu trúc', 'Sổ chống đăng trùng còn lượt chưa rõ', '', 'manual: dừng kiểm tra'])
def test_youtube_or_unknown_resume_barrier_still_blocks_public(app, reason):
    app.schedule_manual_resume_required = True
    app.schedule_manual_resume_reason = reason
    with patch('docurandom.ui.vietnam_now', return_value=at(14)):
        app._trigger_scheduled_upload(ScheduleEvaluation('due', at(13,30), 'public-1330'), shift_id='13')
    app._start_deferred_public_promotion.assert_not_called()
    assert app.schedule_shifts[2].upload_at == '05/10/2026 13:30'


def test_content_barrier_without_confirmed_queue_cannot_start_upload(app):
    app.schedule_manual_resume_required = True
    app.schedule_manual_resume_reason = 'chatgpt: cần kiểm tra checkpoint'
    (app.workspace.state_dir/'deferred_public_queue.json').unlink()
    app._start_youtube_upload = Mock()
    app._scheduled_video_is_ready.return_value = True
    with patch('docurandom.ui.vietnam_now', return_value=at(14)):
        app._trigger_scheduled_upload(ScheduleEvaluation('due', at(13,30), 'public-1330'), shift_id='13')
    app._start_deferred_public_promotion.assert_not_called()
    app._start_youtube_upload.assert_not_called()
    assert app.schedule_manual_resume_required


@pytest.mark.parametrize('pause_kind', ['content-error', 'user-pause'])
def test_public_success_preserves_successor_pause_without_resending(app, pause_kind):
    app.schedule_manual_resume_required = pause_kind == 'content-error'
    app.pipeline_pause_requested = pause_kind == 'user-pause'
    app.schedule_manual_resume_reason = 'chatgpt: cần kiểm tra checkpoint'
    app.schedule_active_stage = 'error'
    app._scheduled_video_is_ready.return_value = True
    app._prepare_immediate_render_upload = Mock()
    app._start_youtube_upload = Mock()
    app._start_next_continuous_workflow = Mock()
    app._continue_after_public_promotion(automatic=True)
    app._prepare_immediate_render_upload.assert_not_called()
    app._start_youtube_upload.assert_not_called()
    app._start_next_continuous_workflow.assert_not_called()
    assert app.schedule_active_stage == 'error'
    assert app.schedule_active_shift_id == 'production-B'
    assert app.schedule_stage_after_public


def test_content_resume_barrier_cannot_publish_outside_two_hour_window(app):
    app.schedule_manual_resume_required = True
    app.schedule_manual_resume_reason = 'chatgpt: cần kiểm tra checkpoint'
    with patch('docurandom.ui.vietnam_now', return_value=at(15,30)):
        app._trigger_scheduled_upload(ScheduleEvaluation('due', at(13,30), 'public-1330'), shift_id='13')
    app._start_deferred_public_promotion.assert_not_called()
    assert app.schedule_manual_resume_required


def test_content_resume_barrier_can_repair_public_clock_without_releasing_production(app):
    from dataclasses import replace
    app.schedule_manual_resume_required = True
    app.schedule_manual_resume_reason = 'chatgpt: cần kiểm tra checkpoint'
    app.schedule_active_stage = 'error'
    app.schedule_shifts = [replace(s, upload_at=f'09/10/2026 {s.shift_id.zfill(2)}:30') for s in app.schedule_shifts]
    assert app._repair_deferred_public_clock_drift(at(14))
    assert app.schedule_shifts[2].upload_at == '05/10/2026 13:30'
    assert app.schedule_active_shift_id == 'production-B'
    assert app.schedule_active_stage == 'error'
    assert app.schedule_manual_resume_required


def test_public_success_uses_pending_skip_replacement_without_claiming_future_workflow(app):
    app.schedule_skip_replacement_pending = True
    app._start_next_continuous_workflow = Mock()
    app._prepare_immediate_render_upload = Mock()
    before = list(app.schedule_shifts)
    app._continue_after_public_promotion(automatic=True)
    app._start_next_continuous_workflow.assert_called_once_with(
        'Tiếp tục ngay sau Skip', replace_skipped=True)
    app._prepare_immediate_render_upload.assert_not_called()
    assert app.schedule_shifts == before

def test_restart_recovers_exact_claimed_public_slot_from_disk(app):
    occurrence=ScheduleEvaluation('due',at(13,30),'youtube-upload:13:2026-10-05T13:30+07:00')
    with patch('docurandom.ui.vietnam_now',return_value=at(14)):
        app._remember_deferred_public_schedule_slot(occurrence,shift_id='13')
        app._claim_deferred_public_schedule_slot(occurrence,shift_id='13')
        app._deferred_public_schedule_context=None  # actual state lost at restart
        app.saved_schedule_startup_intent=True;app._refresh_schedule_status=Mock()
        app._restore_persisted_schedule_on_startup()
    assert app.schedule_shifts[2].upload_at=='05/10/2026 13:30'
    assert app.schedule_shifts[2].upload_claimed_for==''
    assert not (app.workspace.state_dir/'pending-public-attempt.json').exists()
    assert app.schedule_active_stage=='render'
    app._start_deferred_public_promotion.assert_not_called()


@pytest.mark.parametrize('changed',[{'identity':'other'},{'youtube_url':'https://youtu.be/WrongVideo1'},
    {'attempt_id':'other'},{'profile_id':'other'},{'occurrence_id':'other'},{'scheduled_at':'2026-10-04T13:30:00+07:00'}])
def test_restart_never_replays_other_video_or_expired_or_changed_claim(app,changed):
    occurrence=ScheduleEvaluation('due',at(13,30),'public-1330')
    with patch('docurandom.ui.vietnam_now',return_value=at(14)):
        app._remember_deferred_public_schedule_slot(occurrence,shift_id='13')
        app._claim_deferred_public_schedule_slot(occurrence,shift_id='13')
    path=app.workspace.state_dir/'pending-public-attempt.json';value=json.loads(path.read_text());value.update(changed)
    path.write_text(json.dumps(value));app._deferred_public_schedule_context=None
    assert not app._restore_pending_public_attempt_on_startup(at(14))
    assert app.schedule_shifts[2].upload_at=='06/10/2026 13:30'
    app._start_deferred_public_promotion.assert_not_called()


def test_public_lane_cancels_same_profile_monitor_but_waits_for_release():
    import threading
    from docurandom.youtube_upload import ProfileUploadCoordinator
    coordinator=ProfileUploadCoordinator();monitor_cancel=threading.Event()
    lease=coordinator.try_acquire('profile',monitor_cancel);assert lease is not None
    cancel=threading.Event();acquired=threading.Event()
    def public():
        with coordinator.acquire('profile',cancel,job_kind='metadata-recovery',priority=300,preempt_lower=True):acquired.set()
    worker=threading.Thread(target=public);worker.start()
    try:
        assert monitor_cancel.wait(1)
        assert not acquired.is_set()
        lease.__exit__(None,None,None)
        assert acquired.wait(1)
    finally:cancel.set();worker.join(2)


def test_public_lane_never_cancels_live_file_transfer():
    import threading
    from docurandom.youtube_upload import ProfileUploadCoordinator
    coordinator=ProfileUploadCoordinator();upload_cancel=threading.Event()
    lease=coordinator.acquire('profile',upload_cancel)
    cancel=threading.Event();acquired=threading.Event()
    def public():
        with coordinator.acquire('profile',cancel,job_kind='metadata-recovery',priority=300,preempt_lower=True):acquired.set()
    worker=threading.Thread(target=public);worker.start()
    try:
        assert not acquired.wait(.1);assert not upload_cancel.is_set()
        lease.__exit__(None,None,None);assert acquired.wait(1)
    finally:cancel.set();worker.join(2)
