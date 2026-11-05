from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from queue import Queue
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from docurandom.content_ai import (
    ChatGPTWebStatus,
    ChatTurn,
    ContentAICancelled,
    CodexCliStatus,
    ContentArtifacts,
    ContentRunResult,
)
from docurandom.batch import ProgressAggregator
from docurandom.flow_agent import FlowAgentStatus
from docurandom.models import RenderSettings
from docurandom.runtime_profile import HostResources, RenderRuntimeProfile
from docurandom.voicevox import (
    VoicevoxCancelled,
    VoicevoxStyle,
    VoicevoxSynthesisResult,
    VoicevoxSynthesisSettings,
)
from docurandom.ui import (
    CUSTOM_VOICEVOX_NARRATION_PRESET,
    CUSTOM_VOICEVOX_PRESET_DESCRIPTION,
    DEFAULT_AUDIO_FADE,
    DEFAULT_AUDIO_VOLUME,
    DEFAULT_DURATION_MAX,
    DEFAULT_DURATION_MIN,
    DEFAULT_EFFECT_DENSITY,
    DEFAULT_FPS,
    DEFAULT_FRAMING,
    DEFAULT_INTENSITY,
    DEFAULT_PARALLEL_JOBS,
    DEFAULT_PERFORMANCE_MODE,
    DEFAULT_RANDOMIZE_ORDER,
    DEFAULT_REMOVE_GEMINI_WATERMARKS,
    DEFAULT_RESOLUTION,
    DEFAULT_TRANSITION_PACK,
    DEFAULT_VARIANT_COUNT,
    DEFAULT_VIDEO_MODE,
    DEFAULT_VOICEVOX_INTONATION,
    DEFAULT_VOICEVOX_LINE_PAUSE,
    DEFAULT_VOICEVOX_NARRATION_PRESET,
    DEFAULT_VOICEVOX_PAUSE_SCALE,
    DEFAULT_VOICEVOX_PITCH,
    DEFAULT_VOICEVOX_POST_SILENCE,
    DEFAULT_VOICEVOX_PRE_SILENCE,
    DEFAULT_VOICEVOX_SAMPLE_RATE,
    DEFAULT_VOICEVOX_SAVE_LINES,
    DEFAULT_VOICEVOX_SPEED,
    DEFAULT_VOICEVOX_STEREO,
    DEFAULT_VOICEVOX_USE_GPU,
    DEFAULT_VOICEVOX_VOLUME,
    EFFECT_DENSITY_OPTIONS,
    FRAMING_OPTIONS,
    INTENSITY_OPTIONS,
    PERFORMANCE_MODE_OPTIONS,
    RESOLUTION_OPTIONS,
    SmartVoiceRequest,
    _WorkspaceVoiceGate,
    _AdaptiveResourceGate,
    _SharedChatGPTTurnPool,
    TRANSITION_PACK_OPTIONS,
    VIDEO_MODE_OPTIONS,
    VOICEVOX_CHATGPT_AUDIT_TIMEOUT_SECONDS,
    VOICEVOX_NARRATION_PRESETS,
    VoicevoxAuditTimeout,
    DocuRandomApp,
    _SharedCodexPrecheck,
    _automation_pause_required_after_version_change,
    _is_chatgpt_rate_limit_error,
    _is_terminal_flow_recovery_error,
    _is_transient_voicevox_error,
    _is_expected_donut_unavailable,
    _is_wayfern_fingerprint_plan_denial,
    pipeline_input_lock_state,
)
import docurandom.ui as ui_module
from docurandom.workspace_scheduler import (
    ScheduleEvaluation,
    ScheduleShift,
    VIETNAM_TIMEZONE,
    parse_vietnam_datetime,
)


def test_ui_imports_pipeline_inspector_for_scheduler_and_manual_upload() -> None:
    assert callable(ui_module.inspect_workspace_pipeline)


def test_wayfern_custom_fingerprint_plan_denial_is_identified_as_terminal() -> None:
    exact_launch_error = (
        'Failed to launch browser: {"code":"WAYFERN_FINGERPRINT_APPLY_FAILED",'
        '"params":{"detail":"CDP error: {\\"code\\":-32000,'
        '\\"message\\":\\"Custom fingerprints require a Donut Browser plan '
        'that includes them.\\"}"}}'
    )
    assert _is_wayfern_fingerprint_plan_denial(exact_launch_error)
    assert _is_wayfern_fingerprint_plan_denial(
        "Donut đã từ chối mở profile vì gói/quyền fingerprint hiện tại. "
        "Tool không gửi lại lệnh mở; checkpoint vẫn được giữ."
    )
    assert not _is_wayfern_fingerprint_plan_denial(
        'WAYFERN_FINGERPRINT_APPLY_FAILED: no page target exposed'
    )
    assert not _is_wayfern_fingerprint_plan_denial(
        "Custom fingerprints require a Donut Browser plan"
    )


def test_flow_project_identity_errors_are_not_auto_retried() -> None:
    assert _is_terminal_flow_recovery_error(
        "FLOW_WORKSPACE_PROJECT_ID_REQUIRED: checkpoint cần ghép đúng project"
    )
    assert _is_terminal_flow_recovery_error(
        "FLOW_UI_SUBMISSION_UNCERTAIN: phase=generate-command"
    )
    assert not _is_terminal_flow_recovery_error("Flow backend tạm thời chưa sẵn sàng")


def test_failed_history_scan_does_not_become_an_infinite_flow_retry() -> None:
    original = (
        "FLOW_UI_SUBMISSION_UNCERTAIN:phase=generate-command:project=flow-project-123; "
        "project-snapshot=no-exact-prompt-match"
    )
    assert ui_module._is_auto_recoverable_flow_submission(original)
    for diagnostic in (
        "; snapshot-error=FLOW_UI_PROJECT_NOT_READY: no history",
        "; project-snapshot=unavailable",
        "; project-snapshot=unverified",
    ):
        assert not ui_module._is_auto_recoverable_flow_submission(original + diagnostic)


class _Var:
    def __init__(self, value: object) -> None:
        self.value = value

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


class _TextValue:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self, _start: str, _end: str) -> str:
        return self.value

    def delete(self, _start: str, _end: str) -> None:
        self.value = ""

    def insert(self, _start: str, value: str) -> None:
        self.value = value


class UiHelperTests(unittest.TestCase):
    def test_fingerprint_plan_block_is_persisted_without_dropping_schedule_checkpoint(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.youtube_selected_profile_id = "profile-1"
        app.youtube_percent_var = _Var(0)
        app.youtube_status_var = _Var("")
        app.schedule_active_shift_id = "shift-1"
        app.schedule_active_stage = "upload"
        app.schedule_last_event = ""
        app._save_config = Mock()

        message = app._mark_youtube_profile_launch_blocked()

        self.assertTrue(app.youtube_profile_launch_blocked)
        self.assertEqual(app.schedule_active_shift_id, "shift-1")
        self.assertEqual(app.schedule_active_stage, "error")
        self.assertEqual(app.schedule_last_event, message)
        self.assertEqual(app.youtube_percent_var.get(), "Cần xử lý")
        app._save_config.assert_called_once_with()

    def test_fingerprint_plan_block_cancels_a_previously_queued_recovery(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.youtube_selected_profile_id = "profile-1"
        app.youtube_percent_var = _Var(0)
        app.youtube_status_var = _Var("")
        app.youtube_recovery_after_id = "stale-recovery"
        app.youtube_recovery_count = 3
        app.schedule_active_shift_id = "shift-1"
        app.schedule_active_stage = "upload"
        app.schedule_last_event = ""
        app.root = Mock()
        app._save_config = Mock()

        app._mark_youtube_profile_launch_blocked()

        app.root.after_cancel.assert_called_once_with("stale-recovery")
        self.assertIsNone(app.youtube_recovery_after_id)
        self.assertEqual(app.youtube_recovery_count, 0)
        self.assertEqual(app.schedule_active_stage, "error")

    def test_terminal_fingerprint_block_never_schedules_checkpoint_recovery(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_profile_launch_blocked = True
        app.youtube_recovery_after_id = None
        app.youtube_recovery_count = 2
        app.youtube_status_var = _Var("")
        app.root = Mock()
        app._append_log = Mock()

        app._schedule_youtube_checkpoint_recovery("stale retry")

        app.root.after.assert_not_called()
        self.assertEqual(app.youtube_recovery_count, 0)
        app._append_log.assert_called_once()

    def test_queued_recovery_callback_skips_after_terminal_fingerprint_block(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_profile_launch_blocked = False
        app.youtube_upload_busy = False
        app.youtube_upload_token = 5
        app.youtube_recovery_after_id = None
        app.youtube_recovery_count = 0
        app.youtube_status_var = _Var("")
        app.root = Mock()
        app.root.after.return_value = "recovery"
        app._append_log = Mock()
        app._start_youtube_upload = Mock()

        app._schedule_youtube_checkpoint_recovery("transient MCP startup")
        callback = app.root.after.call_args.args[1]
        app.youtube_profile_launch_blocked = True

        callback()

        app._start_youtube_upload.assert_not_called()
        self.assertEqual(app.youtube_recovery_count, 0)
        self.assertIn("bỏ qua timer cũ", app._append_log.call_args.args[0])

    def test_fingerprint_plan_block_suppresses_automatic_schedule_launches(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_upload_busy = False
        app.youtube_profile_launch_blocked = True
        app.youtube_profile_launch_block_reason = "Wayfern fingerprint authorization is required."
        app.schedule_last_event = ""
        app._start_content_pipeline = Mock()
        app._start_youtube_upload = Mock()
        app._schedule_youtube_checkpoint_recovery = Mock()
        evaluation = ScheduleEvaluation(
            "due",
            scheduled_at=datetime(2026, 9, 23, 8, 30, tzinfo=VIETNAM_TIMEZONE),
            occurrence_id="youtube-upload:shift-1",
        )

        app._trigger_scheduled_workflow(evaluation, shift_id="shift-1")
        app._trigger_scheduled_upload(evaluation, shift_id="shift-1")
        app._resume_active_scheduled_pipeline_if_idle()
        self.assertFalse(app._stage_ready_video_for_schedule_lane())

        app._start_content_pipeline.assert_not_called()
        app._start_youtube_upload.assert_not_called()
        app._schedule_youtube_checkpoint_recovery.assert_not_called()
        self.assertEqual(
            app.schedule_last_event,
            "Wayfern fingerprint authorization is required.",
        )

    def test_voicevox_audit_watchdog_cancels_client_turn(self) -> None:
        receipt_root = tempfile.TemporaryDirectory()
        self.addCleanup(receipt_root.cleanup)
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.content_cli_status = ChatGPTWebStatus(
            launcher_path=None,
            installed=True,
            launcher_running=True,
            setup_complete=True,
            authenticated=True,
            runtime_ready=True,
            endpoint="http://127.0.0.1:17841",
            available_models=("chatgpt-web/high",),
            access_token="t" * 48,
        )
        app.chatgpt_turn_pool = Mock()
        app.chatgpt_turn_pool.acquire.return_value = True

        def wait_until_cancelled(*_args, cancel_event=None, **_kwargs):
            assert cancel_event is not None
            if not cancel_event.wait(1.0):
                raise AssertionError("audit watchdog did not cancel the client turn")
            raise ContentAICancelled("cancelled by test watchdog")

        cancel_event = threading.Event()
        with (
            patch(
                "docurandom.ui.VOICEVOX_CHATGPT_AUDIT_TIMEOUT_SECONDS",
                min(0.05, VOICEVOX_CHATGPT_AUDIT_TIMEOUT_SECONDS),
            ),
            patch("docurandom.ui.resolve_chatgpt_web_model", return_value="chatgpt-web/high"),
            patch("docurandom.ui.build_audit_attachment", return_value=b"local test"),
            patch("docurandom.ui.ChatGPTWebChat") as chat_class,
        ):
            chat_class.return_value.send.side_effect = wait_until_cancelled
            chat_class.return_value.thread_id = 'voice-watchdog'
            chat_class.return_value.response_id = ''
            with self.assertRaises(VoicevoxAuditTimeout):
                app._send_voicevox_audit_to_chatgpt(
                    project_data={},
                    styles=(),
                    working_dir=Path(receipt_root.name),
                    cancel_event=cancel_event,
                    progress=Mock(),
                )
            chat_class.return_value.cancel.assert_called_once()
        app.chatgpt_turn_pool.release.assert_called_once_with()

    def test_voicevox_audit_watchdog_starts_after_queue_slot(self) -> None:
        receipt_root = tempfile.TemporaryDirectory()
        self.addCleanup(receipt_root.cleanup)
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.content_cli_status = ChatGPTWebStatus(
            launcher_path=None,
            installed=True,
            launcher_running=True,
            setup_complete=True,
            authenticated=True,
            runtime_ready=True,
            endpoint="http://127.0.0.1:17841",
            available_models=("chatgpt-web/high",),
            access_token="t" * 48,
        )

        class DelayedPool:
            def __init__(self) -> None:
                self.released = False

            def acquire(self, **_kwargs: object) -> bool:
                # This delay is longer than the injected response watchdog.
                # Queue time must not make the audit fail before send().
                time.sleep(0.08)
                return True

            def release(self) -> None:
                self.released = True

        pool = DelayedPool()
        app.chatgpt_turn_pool = pool
        with (
            patch(
                "docurandom.ui.VOICEVOX_CHATGPT_AUDIT_TIMEOUT_SECONDS",
                0.02,
            ),
            patch("docurandom.ui.resolve_chatgpt_web_model", return_value="chatgpt-web/high"),
            patch("docurandom.ui.build_audit_attachment", return_value=b"local test"),
            patch("docurandom.ui.ChatGPTWebChat") as chat_class,
        ):
            chat_class.return_value.send.return_value = "{}"
            chat_class.return_value.thread_id = 'voice-queue'
            chat_class.return_value.response_id = ''
            response = app._send_voicevox_audit_to_chatgpt(
                project_data={},
                styles=(),
                working_dir=Path(receipt_root.name),
                cancel_event=threading.Event(),
                progress=Mock(),
            )

        self.assertEqual(response, "{}")
        self.assertTrue(pool.released)
        chat_class.return_value.send.assert_called_once()

    def test_voicevox_audit_cancellation_releases_slot_without_retry_or_error(self) -> None:
        receipt_root = tempfile.TemporaryDirectory()
        self.addCleanup(receipt_root.cleanup)
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.content_cli_status = ChatGPTWebStatus(
            launcher_path=None,
            installed=True,
            launcher_running=True,
            setup_complete=True,
            authenticated=True,
            runtime_ready=True,
            endpoint="http://127.0.0.1:17841",
            available_models=("chatgpt-web/high",),
            access_token="t" * 48,
        )
        app.chatgpt_turn_pool = Mock()
        app.chatgpt_turn_pool.acquire.return_value = True
        cancel_event = threading.Event()
        with (
            patch("docurandom.ui.project_boxes", return_value=[]),
            patch("docurandom.ui.resolve_chatgpt_web_model", return_value="chatgpt-web/high"),
            patch("docurandom.ui.build_audit_attachment", return_value=b"local test"),
            patch("docurandom.ui.ChatGPTWebChat") as chat_class,
        ):
            chat_class.return_value.send.side_effect = ContentAICancelled("cancelled")
            chat_class.return_value.thread_id = 'voice-cancel'
            chat_class.return_value.response_id = ''
            with self.assertRaises(VoicevoxCancelled):
                app._audit_voicevox_project_data(
                    project_data={},
                    source_path=Path(receipt_root.name) / "unused-test.vvproj",
                    styles=(),
                    client=Mock(),
                    cancel_event=cancel_event,
                    progress=Mock(),
                )
            chat_class.return_value.send.assert_called_once()
        app.chatgpt_turn_pool.release.assert_called_once_with()

    def test_existing_workspace_pauses_once_after_version_change(self) -> None:
        self.assertTrue(
            _automation_pause_required_after_version_change(
                {"schedule_armed": True},
                "2.3.79",
            )
        )
        self.assertTrue(
            _automation_pause_required_after_version_change(
                {"last_started_app_version": "2.3.78"},
                "2.3.79",
            )
        )
        self.assertFalse(
            _automation_pause_required_after_version_change(
                {"last_started_app_version": "2.3.79"},
                "2.3.79",
            )
        )
        self.assertFalse(_automation_pause_required_after_version_change({}, "2.3.79"))

    def test_pipeline_inputs_lock_only_the_stage_that_owns_them(self) -> None:
        content = pipeline_input_lock_state(content_busy=True)
        self.assertTrue(content.content)
        self.assertTrue(content.identity)
        self.assertTrue(content.flow)
        self.assertTrue(content.voice_script)
        self.assertFalse(content.source)
        self.assertFalse(content.voice)
        self.assertFalse(content.render)
        self.assertFalse(content.youtube)

        parallel = pipeline_input_lock_state(
            flow_generation_busy=True,
            pipeline_voice_busy=True,
        )
        self.assertTrue(parallel.identity)
        self.assertTrue(parallel.flow)
        self.assertTrue(parallel.source)
        self.assertTrue(parallel.voice)
        self.assertTrue(parallel.voice_script)
        self.assertFalse(parallel.content)
        self.assertFalse(parallel.render)
        self.assertFalse(parallel.youtube)
        self.assertIn("Flow + VOICEVOX", parallel.status)

    def test_render_and_youtube_input_locks_are_independent(self) -> None:
        rendering = pipeline_input_lock_state(rendering=True)
        self.assertTrue(rendering.identity)
        self.assertTrue(rendering.source)
        self.assertTrue(rendering.voice)
        self.assertTrue(rendering.render)
        self.assertFalse(rendering.content)
        self.assertFalse(rendering.flow)
        self.assertFalse(rendering.youtube)

        uploading = pipeline_input_lock_state(youtube_upload_busy=True)
        self.assertTrue(uploading.youtube)
        self.assertFalse(uploading.content)
        self.assertFalse(uploading.identity)
        self.assertFalse(uploading.source)
        self.assertFalse(uploading.voice)
        self.assertFalse(uploading.render)

    def test_waiting_schedules_do_not_block_app_update(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.rendering = False
        app.voicevox_busy = False
        app.pipeline_voice_busy = False
        app.content_busy = False
        app.flow_generation_busy = False
        app.youtube_upload_busy = False
        app.scanning = False
        app.audio_preflight_active = False
        app.schedule_workflow_enabled_var = _Var(True)
        app.schedule_upload_enabled_var = _Var(True)

        self.assertTrue(app.has_active_user_tasks())
        self.assertFalse(app.has_update_blocking_tasks())

        app.youtube_upload_busy = True
        self.assertTrue(app.has_update_blocking_tasks())

    def test_managed_shutdown_cancels_active_checkview_worker(self) -> None:
        app = SimpleNamespace(
            closed=False,
            rendering=False,
            voicevox_busy=False,
            content_busy=False,
            flow_generation_busy=False,
            pipeline_voice_busy=False,
            youtube_upload_busy=False,
            content_runner=None,
            status_var=_Var(""),
            scan_token=0,
            audio_probe_token=0,
            preview_token=0,
            _close_overlay_editor=Mock(),
            _append_log=Mock(),
            _cancel_schedule_tick=Mock(),
            _cancel_thumbnail_ai_retry=Mock(),
            _cancel_youtube_checkpoint_recovery=Mock(),
            _cancel_content_precheck_watchdog=Mock(),
        )
        for name in (
            "lifecycle_cancel_event", "scan_cancel_event",
            "audio_preflight_cancel_event", "voicevox_cancel_event",
            "voicevox_audit_cancel_event", "pipeline_voice_cancel_event",
            "flow_cancel_event", "content_cancel_event",
            "youtube_upload_cancel_event", "smm_monitor_cancel_event",
        ):
            setattr(app, name, threading.Event())

        DocuRandomApp.begin_managed_shutdown(app)

        self.assertTrue(app.smm_monitor_cancel_event.is_set())
        self.assertTrue(app.closing_after_cancel)

    def test_update_pause_disarms_every_trigger_and_preserves_checkpoint(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.schedule_armed_var = _Var(True)
        app.smm_auto_like_var = _Var(True)
        app.pipeline_pause_requested = False
        app.pipeline_auto_resume_after_id = "retry-timer"
        app.schedule_active_shift_id = "daily-10-15"
        app.schedule_active_stage = "voicevox"
        app.root = Mock()
        app._save_config = Mock()
        app._refresh_schedule_controls = Mock()
        app._refresh_schedule_status = Mock()
        app._refresh_content_controls = Mock()
        app._stop_smm_monitor = Mock()
        app._append_log = Mock()
        app.smm_monitor_after_id = "like-timer"

        app._pause_automation_for_update("2.3.79")

        self.assertTrue(app.automation_paused_for_update)
        self.assertTrue(app.pipeline_pause_requested)
        self.assertTrue(app.schedule_armed_var.get())
        self.assertTrue(app.schedule_resume_after_manual_continue)
        self.assertTrue(app.smm_auto_like_var.get())
        self.assertIsNone(app.pipeline_auto_resume_after_id)
        self.assertEqual(app.schedule_active_shift_id, "daily-10-15")
        self.assertEqual(app.schedule_active_stage, "voicevox")
        app.root.after_cancel.assert_called_once_with("retry-timer")
        app._save_config.assert_called_once_with()
        app._stop_smm_monitor.assert_called_once()

    def test_cancelling_saved_schedule_clears_future_continue_resume(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.schedule_armed_var = _Var(False)
        app.schedule_resume_after_manual_continue = True
        app._save_config = Mock()
        app._refresh_schedule_controls = Mock()
        app._refresh_schedule_status = Mock()
        app._append_log = Mock()

        app._cancel_schedules()

        self.assertFalse(app.schedule_resume_after_manual_continue)
        app._save_config.assert_called_once_with()

    def test_persisted_schedule_skips_missed_slots_but_keeps_a_useful_due_slot(self) -> None:
        current = datetime(2026, 8, 29, 6, 0, tzinfo=VIETNAM_TIMEZONE)
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.saved_schedule_startup_intent = True
        app.closed = False
        app.closing_after_cancel = False
        app.schedule_shifts = [
            ScheduleShift(
                "missed",
                "28/08/2026 05:30",
                "28/08/2026 10:00",
            ),
            ScheduleShift(
                "useful-now",
                "29/08/2026 05:30",
                "29/08/2026 10:00",
            ),
        ]
        app.schedule_selected_shift_id = "useful-now"
        app.schedule_active_shift_id = ""
        app.schedule_active_occurrence_id = ""
        app.schedule_active_stage = ""
        app.schedule_armed_var = _Var(False)
        app._load_schedule_shift_into_editor = Mock()
        app._save_config = Mock()
        app._refresh_schedule_shift_tree = Mock()
        app._refresh_schedule_controls = Mock()
        app._refresh_schedule_status = Mock()

        with patch("docurandom.ui.vietnam_now", return_value=current):
            app._restore_persisted_schedule_on_startup()

        missed, useful = app.schedule_shifts
        self.assertGreater(parse_vietnam_datetime(missed.workflow_at), current)
        self.assertGreater(parse_vietnam_datetime(missed.upload_at), current)
        self.assertEqual(useful.workflow_at, "29/08/2026 05:30")
        self.assertEqual(useful.upload_at, "29/08/2026 10:00")
        self.assertTrue(app.schedule_armed_var.get())
        self.assertFalse(app.automation_paused_for_update)

    def test_persisted_active_upload_is_carried_after_tool_was_closed(self) -> None:
        current = datetime(2026, 8, 29, 6, 0, tzinfo=VIETNAM_TIMEZONE)
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.saved_schedule_startup_intent = True
        app.closed = False
        app.closing_after_cancel = False
        app.schedule_shifts = [
            ScheduleShift(
                "daily-15-05",
                "28/08/2026 13:30",
                "28/08/2026 05:00",
            )
        ]
        app.schedule_selected_shift_id = "daily-15-05"
        app.schedule_active_shift_id = "daily-15-05"
        app.schedule_active_occurrence_id = "youtube-upload:old"
        app.schedule_active_stage = "youtube-retry"
        app.schedule_deferred_upload = False
        app.schedule_armed_var = _Var(True)
        app._load_schedule_shift_into_editor = Mock()
        app._save_config = Mock()
        app._refresh_schedule_shift_tree = Mock()
        app._refresh_schedule_controls = Mock()
        app._refresh_schedule_status = Mock()
        app._schedule_log_once = Mock()
        app._notify_remote = Mock()

        with patch("docurandom.ui.vietnam_now", return_value=current):
            app._restore_persisted_schedule_on_startup()

        (active,) = app.schedule_shifts
        self.assertGreater(parse_vietnam_datetime(active.upload_at), current)
        self.assertEqual(app.schedule_active_stage, "youtube-retry")
        self.assertFalse(app.schedule_deferred_upload)
        app._notify_remote.assert_called_once()

    def test_update_does_not_move_ready_video_to_skipped(self) -> None:
        current = datetime(2026, 8, 29, 6, 0, tzinfo=VIETNAM_TIMEZONE)
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.saved_schedule_startup_intent = True
        app.automation_version_pause_applied = True
        app.closed = False
        app.closing_after_cancel = False
        app.schedule_shifts = [
            ScheduleShift(
                "daily-05-10",
                "28/08/2026 13:30",
                "28/08/2026 05:00",
                workflow_enabled=True,
                upload_enabled=True,
                workflow_daily=True,
                upload_daily=True,
                upload_claimed_for="old-occurrence",
            )
        ]
        app.schedule_selected_shift_id = "daily-05-10"
        app.schedule_active_shift_id = "daily-05-10"
        app.schedule_active_occurrence_id = "youtube-upload:old"
        app.schedule_active_stage = "ready"
        app.schedule_deferred_upload = False
        app.schedule_deferred_public = False
        app.schedule_armed_var = _Var(True)
        app._load_schedule_shift_into_editor = Mock()
        app._save_config = Mock()
        app._refresh_schedule_shift_tree = Mock()
        app._refresh_schedule_controls = Mock()
        app._refresh_schedule_status = Mock()

        with patch("docurandom.ui.vietnam_now", return_value=current):
            app._restore_persisted_schedule_on_startup()

        (active,) = app.schedule_shifts
        self.assertEqual(app.schedule_active_stage, "ready")
        self.assertEqual(active.upload_claimed_for, "")
        self.assertTrue(active.upload_enabled)
        self.assertNotIn("Skipped", app.schedule_last_event)

    def test_startup_recovers_checkpoint_when_stage_is_upload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUploadRecovery_") as temp:
            state_dir = Path(temp)
            (state_dir / "youtube_upload.json").write_text(
                json.dumps(
                    {
                        "status": "uncertain",
                        "identity": "restart-recovery-video",
                        "profile_id": "restart-recovery-profile",
                        "video_path": str(state_dir / "video.mp4"),
                        "package_path": "",
                        "attempt_id": "restart-recovery-attempt",
                    }
                ),
                encoding="utf-8",
            )
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.closed = False
            app.closing_after_cancel = False
            app.workspace = SimpleNamespace(state_dir=state_dir)
            app.schedule_active_shift_id = "daily-05-10"
            app.schedule_active_stage = "upload"
            app.youtube_upload_busy = False
            app.youtube_recovery_after_id = None
            app._schedules_armed = Mock(return_value=True)
            app._schedule_youtube_checkpoint_recovery = Mock()

            app._resume_active_scheduled_pipeline_if_idle()

        app._schedule_youtube_checkpoint_recovery.assert_called_once_with(
            "khôi phục checkpoint YouTube sau khi mở lại tool"
        )

    def test_startup_pause_keeps_saved_armed_schedule_inert(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.automation_paused_for_update = True
        app.schedule_armed_var = _Var(True)
        app.schedule_shifts = [
            ScheduleShift(
                "daily-05-10",
                "30/08/2026 05:30",
                "30/08/2026 10:00",
            )
        ]

        self.assertFalse(app._schedules_armed())

    def test_remote_checkview_off_stops_monitor_without_network(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.smm_auto_like_var = _Var(True)
        app._stop_smm_monitor = Mock()
        app._save_config = Mock()

        result = app._set_smm_auto_like_from_remote(False)

        self.assertFalse(app.smm_auto_like_var.get())
        self.assertIn("OFF", result)
        app._stop_smm_monitor.assert_called_once()
        app._save_config.assert_called_once()

    def test_remote_checkview_on_enables_only_after_prerequisites(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.smm_auto_like_var = _Var(False)
        app.smm_monitor_status_var = _Var("")
        app.youtube_selected_profile_id = "profile-1"
        app._save_config = Mock()
        app._resume_smm_monitor = Mock()
        app._refresh_smm_controls = Mock()

        with (
            patch("docurandom.ui.DONUT_YOUTUBE_ENABLED", True),
            patch("docurandom.ui.resolve_donut_mcp_endpoint", return_value="http://127.0.0.1"),
            patch("docurandom.ui.load_smm_api_key", return_value="encrypted-key"),
        ):
            result = app._set_smm_auto_like_from_remote(True)

        self.assertTrue(app.smm_auto_like_var.get())
        self.assertIn("ON", result)
        app._resume_smm_monitor.assert_called_once()

    def test_workspace_schedules_are_independent(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.schedule_armed_var = _Var(True)
        app.schedule_workflow_enabled_var = _Var(True)
        app.schedule_workflow_at_var = _Var("28/08/2026 19:00")
        app.schedule_workflow_claimed_for = ""
        app.schedule_upload_enabled_var = _Var(True)
        app.schedule_upload_at_var = _Var("28/08/2026 21:00")
        app.schedule_upload_claimed_for = ""

        workflow, upload = app._schedule_evaluations(
            datetime(2026, 8, 28, 20, 0, tzinfo=VIETNAM_TIMEZONE)
        )

        self.assertEqual(workflow.state, "due")
        self.assertEqual(upload.state, "waiting")

    def test_checked_schedule_waits_until_user_starts_scheduler(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.schedule_armed_var = _Var(False)
        app.schedule_workflow_enabled_var = _Var(True)
        app.schedule_workflow_at_var = _Var("28/08/2026 19:00")
        app.schedule_workflow_claimed_for = ""
        app.schedule_upload_enabled_var = _Var(True)
        app.schedule_upload_at_var = _Var("28/08/2026 19:00")
        app.schedule_upload_claimed_for = ""

        workflow, upload = app._schedule_evaluations(
            datetime(2026, 8, 28, 20, 0, tzinfo=VIETNAM_TIMEZONE)
        )

        self.assertEqual(workflow.state, "off")
        self.assertEqual(upload.state, "off")

    def test_multiple_schedule_shifts_are_evaluated_independently(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.schedule_armed_var = _Var(True)
        app.schedule_shifts = [
            ScheduleShift(
                "shift-1",
                "28/08/2026 04:00",
                "28/08/2026 05:00",
            ),
            ScheduleShift(
                "shift-2",
                "28/08/2026 09:00",
                "28/08/2026 10:00",
            ),
        ]

        evaluations = app._all_schedule_evaluations(
            datetime(2026, 8, 28, 8, 0, tzinfo=VIETNAM_TIMEZONE)
        )

        self.assertEqual(len(evaluations), 2)
        self.assertEqual(evaluations[0][1].state, "due")
        self.assertEqual(evaluations[0][2].state, "due")
        self.assertEqual(evaluations[1][1].state, "waiting")
        self.assertEqual(evaluations[1][2].state, "waiting")

    def test_schedule_tick_handles_due_uploads_before_new_workflows(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.flow_render_pending = False
        app.schedule_last_event = ""
        app._resume_active_scheduled_pipeline_if_idle = Mock()
        app._refresh_schedule_status = Mock()
        first = ScheduleShift("daily-05-10", "29/08/2026 05:00", "29/08/2026 10:00")
        second = ScheduleShift("daily-10-15", "29/08/2026 10:00", "29/08/2026 15:00")
        due_upload = ScheduleEvaluation("due", occurrence_id="upload")
        due_workflow = ScheduleEvaluation("due", occurrence_id="workflow")
        waiting = ScheduleEvaluation("waiting")
        app._all_schedule_evaluations = Mock(
            return_value=[
                (first, waiting, due_upload),
                (second, due_workflow, waiting),
            ]
        )
        ordered = Mock()
        app._trigger_scheduled_upload = ordered.upload
        app._trigger_scheduled_workflow = ordered.workflow

        app._schedule_tick(reschedule=False)

        self.assertEqual(
            ordered.mock_calls,
            [
                unittest.mock.call.upload(due_upload, shift_id="daily-05-10"),
                unittest.mock.call.upload(waiting, shift_id="daily-10-15"),
                unittest.mock.call.workflow(waiting, shift_id="daily-05-10"),
                unittest.mock.call.workflow(due_workflow, shift_id="daily-10-15"),
            ],
        )

    @patch("docurandom.ui.vietnam_now", return_value=datetime(2026, 8, 28, 20, 0, tzinfo=VIETNAM_TIMEZONE))
    def test_scheduled_upload_waits_for_render_instead_of_skipping(self, _now: Mock) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_upload_busy = False
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_voice_busy = False
        app.rendering = True
        app.scanning = False
        app.audio_preflight_active = False
        app.schedule_last_event = ""
        app._schedule_log_once = Mock()
        app._skip_failed_scheduled_session = Mock(return_value=False)
        app._request_scheduled_session_cancellation = Mock()
        app._claim_schedule = Mock()
        app._start_youtube_upload = Mock()
        due = ScheduleEvaluation(
            "due",
            scheduled_at=datetime(2026, 8, 28, 20, 0, tzinfo=VIETNAM_TIMEZONE),
            occurrence_id="youtube-upload:test",
        )

        app._trigger_scheduled_upload(due)

        app._claim_schedule.assert_not_called()
        app._start_youtube_upload.assert_not_called()
        app._request_scheduled_session_cancellation.assert_not_called()
        app._schedule_log_once.assert_called_once()
        self.assertIn("đang render", app.schedule_last_event)

    def test_due_workflow_waits_until_previous_upload_is_confirmed(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.schedule_active_shift_id = "daily-15-05"
        app.schedule_active_stage = "ready"
        app.youtube_upload_busy = False
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_voice_busy = False
        app.thumbnail_ai_busy = False
        app.rendering = False
        app.scanning = False
        app.audio_preflight_active = False
        app.schedule_last_event = ""
        app.schedule_shifts = [
            ScheduleShift(
                "daily-05-10",
                "30/08/2026 05:30",
                "30/08/2026 10:00",
                workflow_enabled=True,
                upload_enabled=True,
                workflow_daily=True,
                upload_daily=True,
            )
        ]
        app._scheduled_video_is_ready = Mock(return_value=True)
        app._discard_previous_session_before_workflow = Mock(
            side_effect=AssertionError("a ready manual video must not be archived")
        )
        app._claim_schedule = Mock()
        app._schedule_log_once = Mock()
        app._save_config = Mock()
        app._refresh_schedule_status = Mock()
        app._append_log = Mock()
        app._start_content_pipeline = Mock()
        due = ScheduleEvaluation(
            "due",
            scheduled_at=datetime(2026, 8, 30, 5, 30, tzinfo=VIETNAM_TIMEZONE),
            occurrence_id="workflow:daily-05-10",
        )

        app._trigger_scheduled_workflow(due, shift_id="daily-05-10")

        app._discard_previous_session_before_workflow.assert_not_called()
        app._start_content_pipeline.assert_not_called()
        app._claim_schedule.assert_not_called()
        app._start_content_pipeline.assert_not_called()
        self.assertEqual(app.schedule_active_stage, "ready")
        self.assertIn("chờ YouTube xác nhận", app.schedule_last_event)

    def test_youtube_uncertain_schedules_exact_checkpoint_recovery(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_upload_busy = False
        app.youtube_upload_token = 7
        app.youtube_recovery_after_id = None
        app.youtube_recovery_count = 0
        app.schedule_active_shift_id = "daily-15-05"
        app.schedule_active_stage = "upload"
        app.youtube_status_var = _Var("")
        app.root = Mock()
        app.root.after.return_value = "youtube-recovery"
        app._save_config = Mock()
        app._append_log = Mock()
        app._start_youtube_upload = Mock()

        app._schedule_youtube_checkpoint_recovery("metadata chưa đủ")

        self.assertEqual(app.schedule_active_stage, "youtube-retry")
        self.assertEqual(app.root.after.call_args.args[0], 120_000)
        self.assertIn("đúng bản nháp", app.youtube_status_var.get())
        callback = app.root.after.call_args.args[1]
        callback()
        app._start_youtube_upload.assert_called_once_with(automatic=True)

    def test_startup_youtube_recovery_runs_after_short_bootstrap_delay(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_recovery_after_id = None
        app.youtube_recovery_count = 0
        app.schedule_active_shift_id = "daily-05-10"
        app.schedule_active_stage = "youtube-retry"
        app.youtube_status_var = _Var("")
        app.root = Mock()
        app.root.after.return_value = "youtube-recovery"
        app._save_config = Mock()
        app._append_log = Mock()

        app._schedule_youtube_checkpoint_recovery(
            "khôi phục checkpoint YouTube sau khi mở lại tool"
        )

        self.assertEqual(app.root.after.call_args.args[0], 5_000)

    def test_youtube_recovery_timer_skips_checkpoint_moved_to_archive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYoutubeRecovery_") as temp:
            state_dir = Path(temp) / "State"
            state_dir.mkdir(parents=True)
            checkpoint = state_dir / "youtube_upload.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "status": "uncertain",
                        "identity": "old-video-identity",
                        "profile_id": "profile-old",
                        "video_path": "old.mp4",
                        "package_path": "old.txt",
                        "attempt_id": "attempt-old",
                    }
                ),
                encoding="utf-8",
            )
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.workspace = SimpleNamespace(state_dir=state_dir)
            app.closed = False
            app.closing_after_cancel = False
            app.youtube_upload_busy = False
            app.youtube_recovery_after_id = None
            app.youtube_recovery_count = 0
            app.schedule_active_shift_id = ""
            app.youtube_status_var = _Var("")
            app.root = Mock()
            app.root.after.return_value = "stale-recovery"
            app._save_config = Mock()
            app._append_log = Mock()
            app._start_youtube_upload = Mock()

            app._schedule_youtube_checkpoint_recovery("phiên cũ")
            callback = app.root.after.call_args.args[1]
            checkpoint.unlink()

            callback()

            app._start_youtube_upload.assert_not_called()
            self.assertGreaterEqual(app._append_log.call_count, 2)
            self.assertIn("bỏ qua callback khôi phục", app._append_log.call_args.args[0])

    def test_youtube_recovery_notice_is_not_repeated_on_each_poll(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.youtube_recovery_notice = ""
        app.youtube_recovery_notice_at = 0.0
        app._notify_remote = Mock()

        app._notify_youtube_recovery_once("YouTube vẫn đang tải đúng tab cũ")
        app._notify_youtube_recovery_once("YouTube vẫn đang tải đúng tab cũ")

        app._notify_remote.assert_called_once()

    def test_youtube_connection_retry_has_a_separate_notice(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.youtube_recovery_notice = ""
        app.youtube_recovery_notice_at = 0.0
        app._notify_remote = Mock()

        app._notify_youtube_connection_retry_once(
            "Profile không tải được YouTube Studio (ERR_PROXY_CONNECTION_FAILED)"
        )

        message = app._notify_remote.call_args.args[1]
        self.assertIn("TRƯỚC KHI GẮN FILE", message)
        self.assertIn("tự mở target mới", message)

    def test_new_workflow_keeps_previous_shift_until_explicit_skip(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.schedule_active_shift_id = "daily-15-05"
        app.schedule_active_stage = "ready"
        app.youtube_upload_busy = False
        app.schedule_shifts = [
            ScheduleShift(
                "daily-15-05",
                "29/08/2026 15:30",
                "30/08/2026 05:00",
            )
        ]
        app._skip_failed_scheduled_session = Mock(return_value=True)
        due = ScheduleEvaluation(
            "due",
            scheduled_at=datetime(2026, 8, 30, 5, 30, tzinfo=VIETNAM_TIMEZONE),
            occurrence_id="workflow:daily-05-10",
        )

        ready = app._discard_previous_session_before_workflow(
            due,
            new_shift_id="daily-05-10",
        )

        self.assertFalse(ready)
        app._skip_failed_scheduled_session.assert_not_called()
        self.assertIn("không tự chuyển sang Skipped", app.schedule_last_event)

    @patch("docurandom.ui.vietnam_now", return_value=datetime(2026, 8, 30, 5, 0, tzinfo=VIETNAM_TIMEZONE))
    def test_failed_schedule_is_kept_at_upload_deadline(self, _now: Mock) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_upload_busy = False
        app.schedule_active_shift_id = "daily-15-05"
        app.schedule_active_stage = "error"
        app.schedule_last_event = "VOICEVOX không tạo được audio."
        app._skip_failed_scheduled_session = Mock(return_value=True)
        app._start_youtube_upload = Mock()
        due = ScheduleEvaluation(
            "due",
            scheduled_at=datetime(2026, 8, 30, 5, 0, tzinfo=VIETNAM_TIMEZONE),
            occurrence_id="youtube-upload:daily-15-05",
        )

        app._trigger_scheduled_upload(due, shift_id="daily-15-05")

        app._skip_failed_scheduled_session.assert_not_called()
        self.assertIn("Checkpoint vẫn được giữ", app.schedule_last_event)
        app._start_youtube_upload.assert_not_called()

    @patch("docurandom.ui.vietnam_now", return_value=datetime(2026, 8, 28, 20, 0, tzinfo=VIETNAM_TIMEZONE))
    @patch("docurandom.ui.resolve_workspace_upload_sources")
    @patch("docurandom.ui.resolve_donut_mcp_endpoint", return_value=Mock())
    def test_ready_scheduled_upload_claims_before_starting_once(
        self,
        _endpoint: Mock,
        resolve_sources: Mock,
        _now: Mock,
    ) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        # This test isolates scheduling after the material preflight passes.
        app._automatic_thumbnail_required_but_missing = Mock(return_value=False)
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_upload_busy = False
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_voice_busy = False
        app.rendering = False
        app.scanning = False
        app.audio_preflight_active = False
        app.workspace = SimpleNamespace(state_dir=Path(tempfile.mkdtemp()))
        app.youtube_selected_profile_id = "profile-1"
        app.last_content_package = None
        app._effective_youtube_thumbnail = Mock(return_value=None)
        app._claim_schedule = Mock()
        app._start_youtube_upload = Mock()
        due = ScheduleEvaluation(
            "due",
            scheduled_at=datetime(2026, 8, 28, 20, 0, tzinfo=VIETNAM_TIMEZONE),
            occurrence_id="youtube-upload:test",
        )

        app._trigger_scheduled_upload(due)

        app._claim_schedule.assert_called_once()
        self.assertIn("Công khai", app._claim_schedule.call_args.args[2])
        app._start_youtube_upload.assert_called_once_with(
            automatic=True,
            defer_public_until_slot=False,
            publish_if_no_older_video=True,
        )
        resolve_sources.assert_called_once()

    def test_chatgpt_pool_runs_multiple_sessions_and_cancels_waiters(self) -> None:
        pool = _SharedChatGPTTurnPool(2, serial=False)
        self.assertTrue(pool.acquire(label="one"))
        self.assertTrue(pool.acquire(label="two"))
        self.assertEqual(pool.active_count, 2)

        cancelled = threading.Event()
        waiting: list[bool] = []
        waiter = threading.Thread(
            target=lambda: waiting.append(
                pool.acquire(cancel_event=cancelled, label="three")
            )
        )
        waiter.start()
        for _ in range(50):
            if pool.pending_count == 1:
                break
            threading.Event().wait(0.01)
        self.assertEqual(pool.pending_count, 1)
        cancelled.set()
        waiter.join(timeout=1.0)
        self.assertEqual(waiting, [False])
        self.assertEqual(pool.active_count, 2)
        pool.release()
        pool.release()

    def test_chatgpt_pool_wait_callback_runs_outside_condition_lock(self) -> None:
        pool = _SharedChatGPTTurnPool(1)
        self.assertTrue(pool.acquire(label="active"))
        cancelled = threading.Event()
        observed: list[dict[str, int | bool]] = []
        result: list[bool] = []

        def report(_position: int, _active: int, _limit: int) -> None:
            # This is the same shape as the launcher refresh path. The
            # callback must be able to query the pool without deadlocking.
            observed.append(pool.queue_snapshot())
            cancelled.set()

        waiter = threading.Thread(
            target=lambda: result.append(
                pool.acquire(
                    cancel_event=cancelled,
                    label="waiting",
                    on_wait=report,
                )
            ),
            daemon=True,
        )
        waiter.start()
        waiter.join(timeout=1.0)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(result, [False])
        self.assertEqual(observed[0]["active"], 1)
        pool.release()

    def test_chatgpt_pool_caps_web_fanout_at_twenty(self) -> None:
        pool = _SharedChatGPTTurnPool(99, serial=False)
        self.assertEqual(pool.limit, 20)
        for index in range(20):
            self.assertTrue(pool.acquire(label=f"workspace-{index}"))
        self.assertEqual(pool.active_count, 20)
        for _ in range(20):
            pool.release()

    def test_chatgpt_pool_does_not_serialize_after_slow_response(self) -> None:
        pool = _SharedChatGPTTurnPool(2, serial=False)
        pool.mark_slow_response(cooldown_seconds=60)
        self.assertEqual(pool.current_limit, 2)
        self.assertTrue(pool.acquire(label="one"))
        self.assertTrue(pool.acquire(label="two"))
        self.assertEqual(pool.active_count, 2)
        pool.release()
        pool.release()

    def test_chatgpt_pool_pauses_then_resumes_serially_after_rate_limit(self) -> None:
        now = [100.0]
        pool = _SharedChatGPTTurnPool(3, monotonic=lambda: now[0])
        pool.mark_rate_limited(cooldown_seconds=60)
        self.assertEqual(pool.current_limit, 0)

        acquired: list[bool] = []
        waiter = threading.Thread(target=lambda: acquired.append(pool.acquire(label="queued")))
        waiter.start()
        for _ in range(50):
            if pool.pending_count == 1:
                break
            threading.Event().wait(0.01)
        self.assertEqual(pool.pending_count, 1)
        now[0] = 161.0
        waiter.join(timeout=1.0)
        self.assertEqual(acquired, [True])
        self.assertEqual(pool.current_limit, 1)
        pool.release()

    def test_chatgpt_pool_keeps_account_deadline_after_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cooldown.json"
            wall = [1000.0]
            first = _SharedChatGPTTurnPool(3, monotonic=lambda: 100.0,
                                          wall_clock=lambda: wall[0], cooldown_path=path)
            first.mark_rate_limited(cooldown_seconds=3600)
            wall[0] = 1600.0
            # A new process has an unrelated monotonic clock.
            monotonic = [1.0]
            second = _SharedChatGPTTurnPool(3, monotonic=lambda: monotonic[0],
                                           wall_clock=lambda: wall[0], cooldown_path=path)
            self.assertEqual(second.current_limit, 0)
            second.mark_rate_limited(cooldown_seconds=300)
            self.assertEqual(json.loads(path.read_text())["expires_at_epoch"], 4600.0)
            monotonic[0] = 3002.0
            self.assertEqual(second.current_limit, 1)

    def test_chatgpt_pool_ignores_expired_or_invalid_disk_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cooldown.json"
            for payload in ({"version": 1, "expires_at_epoch": 999},
                            {"version": 1, "expires_at_epoch": "bad"},
                            {"version": 1, "expires_at_epoch": float("inf")}, []):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload))
                    pool = _SharedChatGPTTurnPool(3, configured_limit=3,
                                                 wall_clock=lambda: 1000, cooldown_path=path)
                    self.assertEqual(pool.current_limit, 3)

    def test_chatgpt_pool_is_serial_by_default_and_can_expand_live(self) -> None:
        pool = _SharedChatGPTTurnPool(3)
        self.assertTrue(pool.serial)
        self.assertEqual(pool.current_limit, 1)
        snapshot = pool.queue_snapshot()
        self.assertTrue(snapshot["serial"])
        self.assertEqual(snapshot["recommended_limit"], 3)
        self.assertFalse(snapshot["rate_limited"])

        self.assertFalse(pool.set_serial(False))
        self.assertEqual(pool.current_limit, 3)
        self.assertTrue(pool.acquire(label="one"))
        self.assertTrue(pool.acquire(label="two"))
        self.assertEqual(pool.active_count, 2)
        self.assertTrue(pool.set_serial(True))
        self.assertEqual(pool.current_limit, 1)
        pool.release()
        pool.release()

    def test_chatgpt_pool_applies_exact_numeric_parallel_limit(self) -> None:
        pool = _SharedChatGPTTurnPool(2)
        self.assertEqual(pool.current_limit, 1)
        self.assertEqual(pool.set_parallel_limit(5), 5)
        self.assertEqual(pool.current_limit, 5)
        snapshot = pool.queue_snapshot()
        self.assertEqual(snapshot["configured_limit"], 5)
        self.assertFalse(snapshot["serial"])
        for index in range(5):
            self.assertTrue(pool.acquire(label=f"numeric-{index}"))
        self.assertEqual(pool.active_count, 5)
        for _ in range(5):
            pool.release()

    def test_chatgpt_rate_limit_classifier_covers_bridge_and_user_messages(self) -> None:
        self.assertTrue(_is_chatgpt_rate_limit_error("CHATGPT_RATE_LIMITED"))
        self.assertTrue(_is_chatgpt_rate_limit_error("Too many requests"))
        self.assertTrue(
            _is_chatgpt_rate_limit_error("ChatGPT đang tạm giới hạn do có quá nhiều yêu cầu")
        )
        self.assertFalse(_is_chatgpt_rate_limit_error("ChatGPT đang viết nội dung bình thường"))

    def test_voice_gate_is_parallel_between_workspaces_but_single_inside_one(self) -> None:
        shared_slots = threading.BoundedSemaphore(2)
        first_workspace = _WorkspaceVoiceGate(shared_slots)
        second_workspace = _WorkspaceVoiceGate(shared_slots)
        waiting_workspace = _WorkspaceVoiceGate(shared_slots)

        self.assertTrue(first_workspace.acquire(timeout=0.05))
        self.assertTrue(second_workspace.acquire(timeout=0.05))
        self.assertFalse(waiting_workspace.acquire(timeout=0.02))

        same_workspace_result: list[bool] = []
        waiter = threading.Thread(
            target=lambda: same_workspace_result.append(
                first_workspace.acquire(timeout=0.02)
            )
        )
        waiter.start()
        waiter.join(timeout=1.0)
        self.assertEqual(same_workspace_result, [False])

        second_workspace.release()
        self.assertTrue(waiting_workspace.acquire(timeout=0.05))
        waiting_workspace.release()
        first_workspace.release()

    def test_adaptive_voice_gate_reacts_to_workload_changes(self) -> None:
        capacity = [2]
        shared_slots = _AdaptiveResourceGate(2, lambda: capacity[0])
        first = _WorkspaceVoiceGate(shared_slots)
        second = _WorkspaceVoiceGate(shared_slots)
        waiting = _WorkspaceVoiceGate(shared_slots)

        self.assertTrue(first.acquire(timeout=0.05))
        self.assertTrue(second.acquire(timeout=0.05))
        capacity[0] = 1
        self.assertFalse(waiting.acquire(timeout=0.02))
        second.release()
        first.release()
        self.assertTrue(waiting.acquire(timeout=0.05))
        waiting.release()

    def test_workspace_change_keeps_active_jobs_running(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.rendering = True
        app.voicevox_busy = True
        app.content_busy = True
        app.flow_generation_busy = True
        app.scanning = True
        app._save_config = Mock()
        app.on_change_workspace = Mock()

        app._request_workspace_change()

        app._save_config.assert_called_once_with()
        app.on_change_workspace.assert_called_once_with()
        self.assertFalse(app.closed)

    def test_workspace_activity_combines_live_render_and_ai_progress(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.job_states = {
            "render-running": "running",
            "render-waiting": "waiting",
        }
        app.progress_aggregator = ProgressAggregator()
        app.progress_aggregator.add("render-running")
        app.progress_aggregator.add("render-waiting")
        app.progress_aggregator.update("render-running", 0.6)
        app.rendering = True
        app.content_busy = True
        app.flow_generation_busy = False
        app.content_progress_var = _Var(40.0)
        app.voicevox_busy = False
        app.voicevox_progress_var = _Var(0.0)
        app.scanning = False
        app.audio_preflight_active = False
        app.last_content_package = None
        app.last_voicevox_path = None

        snapshot = app.workspace_activity()

        self.assertTrue(snapshot["active"])
        # Render is 30% overall and AI is 40%, so the workspace is 35%.
        self.assertEqual(snapshot["progress"], 35.0)
        self.assertIn("Video 1 chạy", snapshot["status"])
        self.assertIn("AI 40%", snapshot["status"])

    def test_workspace_activity_uses_flow_stage_percent_not_ai_overall_percent(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.job_states = {}
        app.progress_aggregator = ProgressAggregator()
        app.rendering = False
        app.content_busy = True
        app.flow_generation_busy = True
        app.content_progress_var = _Var(89.0)
        app.flow_progress_var = _Var(25.0)
        app.voicevox_busy = False
        app.voicevox_progress_var = _Var(0.0)
        app.scanning = False
        app.audio_preflight_active = False
        app.last_content_package = None
        app.last_voicevox_path = None

        snapshot = app.workspace_activity()

        self.assertEqual(snapshot["progress"], 25.0)
        self.assertIn("Tạo ảnh 25%", snapshot["status"])

    def test_cancel_render_only_cancels_jobs_owned_by_workspace(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.rendering = True
        app.cancelling_all = False
        app.job_states = {
            "workspace-a-running": "running",
            "workspace-a-waiting": "waiting",
            "workspace-a-done": "completed",
        }
        app.render_pool = Mock()
        app.render_pool.cancel.return_value = True
        app._cancel_audio_render_preflight = Mock()
        app._update_job_card = Mock()
        app.cancel_button = Mock()
        app.status_var = _Var("")
        app._append_log = Mock()
        app._refresh_global_state = Mock()

        app.cancel_render()

        self.assertEqual(
            [call.args[0] for call in app.render_pool.cancel.call_args_list],
            ["workspace-a-running", "workspace-a-waiting"],
        )
        app.render_pool.cancel_all.assert_not_called()
        self.assertEqual(app.job_states["workspace-a-done"], "completed")

    @staticmethod
    def _nvenc_profile() -> RenderRuntimeProfile:
        return RenderRuntimeProfile(
            host=HostResources(8, 16 * 1024**3, 12 * 1024**3),
            encoder="h264_nvenc",
            hardware_accelerated=True,
            recommended_jobs=2,
            threads_per_job=3,
            encoder_label="NVIDIA NVENC",
        )

    def test_disk_estimate_scales_with_timeline_duration(self) -> None:
        short = DocuRandomApp._estimate_job_bytes(8, 1920, 1080, 30, 60)
        long = DocuRandomApp._estimate_job_bytes(8, 1920, 1080, 30, 3600)
        self.assertGreater(long, short * 20)
        self.assertGreater(long, 8 * 1024**3)

    def test_content_precheck_snapshots_master_project_and_media(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomContentPrecheck_") as temp:
            root = Path(temp)
            source = root / "media"
            output = root / "output"
            source.mkdir()
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.master_prompt_var = _Var("")
            app.master_prompt_text = _TextValue("BẮT ĐẦU — 日本語")
            app.output_var = _Var(str(output))
            app.output_name_var = _Var("Tên video")
            app.content_minutes_var = _Var(12)
            app.scanning = False
            app.source_var = _Var(str(source))
            app.scanned_source = str(source.resolve())
            app.media = [object()]
            app.rights_confirm_var = _Var(True)
            app.editorial_intent_var = _Var("Góc nhìn biên tập nguyên gốc")

            snapshot = app._content_input_snapshot()

            self.assertEqual(snapshot["master_prompt"], "BẮT ĐẦU — 日本語")
            self.assertEqual(snapshot["output_dir"], output.resolve())
            self.assertEqual(snapshot["project_name"], "Tên video")
            self.assertEqual(snapshot["minutes"], 12.0)
            self.assertTrue(output.is_dir())

    def test_content_generation_starts_once_only_after_both_prechecks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomContentGate_") as temp:
            root = Path(temp)
            master = root / "master.txt"
            master.write_text("MASTER", encoding="utf-8")
            output = root / "output"
            output.mkdir()
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.content_token = 4
            app.content_busy = True
            app.content_precheck_pending = True
            app.content_generation_started = False
            app.content_codex_ready = True
            app.content_voice_ready = True
            app.content_cancel_event = threading.Event()
            app.content_cli_status = ChatGPTWebStatus(
                launcher_path=root / "Codex Web GPT.exe",
                installed=True,
                launcher_running=True,
                setup_complete=True,
                authenticated=True,
                runtime_ready=True,
                endpoint="http://127.0.0.1:17841",
                bridge_version="2.1.9",
                available_models=("chatgpt-web/auto", "chatgpt-web/high"),
                access_token="docurandom_bridge_token_0123456789_abcdefghijklmnop",
                codex_path=root / "codex.exe",
                codex_version="codex-cli test",
                codex_profile_path=root / "docurandom-chatgpt-web.config.toml",
                codex_ready=True,
            )
            app.content_snapshot = {
                "master_prompt": "MASTER",
                "output_dir": output,
                "project_name": "project",
                "minutes": 8.0,
                "chatgpt_web_model": "ChatGPT Web · GPT-5.6 Sol · High",
            }
            app.content_runner = None
            app.events = Mock()
            app._set_content_progress = Mock()
            app._append_content_chat = Mock()
            app._finish_content_error = Mock()
            fake_chat = object()
            fake_runner = Mock()
            fake_runner.run.return_value = object()
            started_threads: list[object] = []

            class _ImmediateThread:
                def __init__(self, *, target, **_kwargs) -> None:
                    self.target = target

                def start(self) -> None:
                    started_threads.append(self)
                    self.target()

            with (
                patch("docurandom.ui.QueuedChatGPTWebChat", return_value=fake_chat) as chat,
                patch("docurandom.ui.MasterPromptRunner", return_value=fake_runner) as runner,
                patch("docurandom.ui.threading.Thread", _ImmediateThread),
            ):
                app._maybe_begin_content_generation(4)
                app._maybe_begin_content_generation(4)

            self.assertEqual(len(started_threads), 1)
            self.assertTrue(app.content_generation_started)
            self.assertFalse(app.content_precheck_pending)
            chat.assert_called_once_with(
                "http://127.0.0.1:17841",
                "chatgpt-web/auto",
                output,
                "docurandom_bridge_token_0123456789_abcdefghijklmnop",
                turn_pool=None,
                queue_label="project",
                on_wait=ANY,
                on_acquired=None,
                on_failure=None,
            )
            runner.assert_called_once()
            fake_runner.run.assert_called_once()
            self.assertEqual(app.content_runner, fake_runner)
            app._finish_content_error.assert_not_called()

    def test_content_generation_stays_blocked_until_voicevox_is_ready(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.content_token = 8
        app.content_busy = True
        app.content_precheck_pending = True
        app.content_generation_started = False
        app.content_codex_ready = True
        app.content_voice_ready = False
        app._finish_content_error = Mock()

        with patch("docurandom.ui.threading.Thread") as thread:
            app._maybe_begin_content_generation(8)

        thread.assert_not_called()
        self.assertFalse(app.content_generation_started)
        app._finish_content_error.assert_not_called()

    def test_precheck_watchdog_wakes_pipeline_from_unbound_ready_checks(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.content_token = 21
        app.content_busy = True
        app.content_precheck_pending = True
        app.content_generation_started = False
        app.content_snapshot = {"flow_enabled": True}
        app.content_codex_ready = False
        app.content_voice_ready = False
        app.content_flow_ready = False
        app.codex_checking = False
        app.flow_checking = False
        app.voicevox_busy = False
        app.voicevox_engine_state = "ready"
        app.voicevox_progress_var = _Var(100.0)
        app.content_cli_status = ChatGPTWebStatus(
            launcher_path=Path("C:/ChatGPTWeb/Codex Web GPT.exe"),
            installed=True,
            launcher_running=True,
            setup_complete=True,
            authenticated=True,
            runtime_ready=True,
            endpoint="http://127.0.0.1:17841",
            available_models=("chatgpt-web/high",),
            access_token="docurandom_bridge_token_0123456789_abcdefghijklmnop",
            codex_path=Path("C:/Codex/codex.exe"),
            codex_version="codex-cli test",
            codex_profile_path=Path("C:/Codex/config.toml"),
            codex_ready=True,
        )
        app.flow_status = SimpleNamespace(ready=True)
        app.content_precheck_started_at = 10.0
        app.content_precheck_last_change_at = 10.0
        app.content_precheck_last_signature = ""
        app.content_precheck_watchdog_after_id = None
        app._record_pipeline_stage = Mock()
        app._append_log = Mock()
        app._set_content_progress = Mock()
        app._cancel_content_precheck_watchdog = Mock()
        app._schedule_content_precheck_watchdog = Mock()

        def begin(_token: int) -> None:
            app.content_generation_started = True
            app.content_precheck_pending = False

        app._maybe_begin_content_generation = Mock(side_effect=begin)

        with patch("docurandom.ui.time.monotonic", return_value=12.0):
            app._drive_content_precheck(21)

        self.assertTrue(app.content_codex_ready)
        self.assertTrue(app.content_voice_ready)
        self.assertTrue(app.content_flow_ready)
        app._maybe_begin_content_generation.assert_called_once_with(21)
        app._cancel_content_precheck_watchdog.assert_called_once_with()
        app._schedule_content_precheck_watchdog.assert_not_called()

    def test_pipeline_flow_precheck_never_reopens_setup_for_existing_tab(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.flow_checking = False
        app.flow_check_token = 0
        app.flow_check_started_at = 0.0
        app.content_token = 31
        app.content_precheck_flow_claimed = False
        app._append_log = Mock()
        app._append_exception_log = Mock()
        app._refresh_flow_controls = Mock()
        app.flow_status_var = _Var("")
        app.flow_detail_var = _Var("")
        app.events = SimpleNamespace(put=Mock())
        status = FlowAgentStatus(
            edge_path=Path("edge.exe"),
            extension_dir=Path("extension"),
            extension_installed=True,
            backend_running=True,
            extension_connected=True,
            has_flow_key=False,
            browser_open=True,
            flow_tab_count=1,
            ui_image_capable=False,
        )
        app.flow_manager = SimpleNamespace(
            check=Mock(return_value=status),
            install_edge=Mock(),
            start_backend=Mock(),
            open_extension_setup=Mock(),
            open_flow=Mock(),
        )

        class ImmediateThread:
            def __init__(self, *, target, **_kwargs) -> None:
                self.target = target

            def start(self) -> None:
                self.target()

        with patch("docurandom.ui.threading.Thread", ImmediateThread):
            app._check_flow_async(
                reason="pipeline",
                auto_start=True,
                pipeline_token=31,
                poll_remaining=90,
            )

        app.flow_manager.start_backend.assert_called_once_with()
        app.flow_manager.open_extension_setup.assert_not_called()
        app.flow_manager.open_flow.assert_not_called()
        event = app.events.put.call_args.args[0]
        self.assertEqual(event[0], "flow_checked")

    def test_precheck_watchdog_retries_stuck_check_then_reports_it(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.content_token = 22
        app.content_busy = True
        app.content_precheck_pending = True
        app.content_generation_started = False
        app.content_snapshot = {"flow_enabled": False}
        app.content_codex_ready = False
        app.content_voice_ready = True
        app.content_flow_ready = True
        app.codex_checking = True
        app.codex_check_token = 4
        app.codex_check_started_at = 1.0
        app.flow_checking = False
        app.voicevox_busy = False
        app.voicevox_engine_state = "ready"
        app.voicevox_progress_var = _Var(100.0)
        app.content_cli_status = None
        app.flow_status = None
        app.content_precheck_started_at = 1.0
        app.content_precheck_last_change_at = 1.0
        app.content_precheck_last_signature = (
            "codex:0:1:0|voice:1:0:ready:100|flow:0:1:0"
        )
        app.content_precheck_recovery_attempts = {
            "codex": 0,
            "voicevox": 0,
            "flow": 0,
        }
        app.content_precheck_watchdog_after_id = None
        app._record_pipeline_stage = Mock()
        app._append_log = Mock()
        app._append_content_chat = Mock()
        app._set_content_progress = Mock()
        app._maybe_begin_content_generation = Mock()
        app._schedule_content_precheck_watchdog = Mock()
        app._finish_content_error = Mock()

        with patch("docurandom.ui.time.monotonic", return_value=100.0):
            app._drive_content_precheck(22)

        self.assertFalse(app.codex_checking)
        self.assertEqual(app.codex_check_token, 5)
        self.assertEqual(app.content_precheck_recovery_attempts["codex"], 1)
        app._finish_content_error.assert_not_called()

        app.codex_checking = True
        app.codex_check_started_at = 100.0
        app.content_precheck_last_change_at = 100.0
        app.content_precheck_last_signature = (
            "codex:0:1:0|voice:1:0:ready:100|flow:0:1:0"
        )
        with patch("docurandom.ui.time.monotonic", return_value=180.0):
            app._drive_content_precheck(22)

        app._finish_content_error.assert_called_once()
        self.assertIn("codex", app._finish_content_error.call_args.args[0])

    def test_voicevox_precheck_gets_three_silent_worker_retries(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.content_token = 23
        app.content_busy = True
        app.content_precheck_pending = True
        app.content_generation_started = False
        app.content_snapshot = {"flow_enabled": False}
        app.content_codex_ready = True
        app.content_voice_ready = False
        app.content_flow_ready = True
        app.codex_checking = False
        app.flow_checking = False
        app.voicevox_busy = True
        app.voicevox_token = 7
        app.voicevox_cancel_event = threading.Event()
        app.voicevox_engine_state = "starting"
        app.voicevox_progress_var = _Var(0.0)
        app.voicevox_check_activity_at = 1.0
        app.content_cli_status = None
        app.flow_status = None
        app.content_precheck_started_at = 1.0
        app.content_precheck_last_change_at = 1.0
        app.content_precheck_last_signature = (
            "codex:1:0:0|voice:0:1:starting:0|flow:0:1:0"
        )
        app.content_precheck_recovery_attempts = {
            "codex": 0,
            "voicevox": 2,
            "flow": 0,
        }
        app.content_precheck_watchdog_after_id = None
        app._record_pipeline_stage = Mock()
        app._append_log = Mock()
        app._append_content_chat = Mock()
        app._set_content_progress = Mock()
        app._maybe_begin_content_generation = Mock()
        app._schedule_content_precheck_watchdog = Mock()
        app._finish_content_error = Mock()

        with patch("docurandom.ui.time.monotonic", return_value=130.0):
            app._drive_content_precheck(23)

        self.assertFalse(app.voicevox_busy)
        self.assertEqual(app.voicevox_token, 8)
        self.assertEqual(app.content_precheck_recovery_attempts["voicevox"], 3)
        app._finish_content_error.assert_not_called()

        app.voicevox_busy = True
        app.voicevox_check_activity_at = 130.0
        app.content_precheck_last_change_at = 130.0
        app.content_precheck_last_signature = (
            "codex:1:0:0|voice:0:1:starting:0|flow:0:1:0"
        )
        with patch("docurandom.ui.time.monotonic", return_value=260.0):
            app._drive_content_precheck(23)

        app._finish_content_error.assert_called_once()
        self.assertIn("tự nối lại", app._finish_content_error.call_args.args[0])

    def test_voicevox_transient_error_classification_is_narrow(self) -> None:
        self.assertTrue(
            _is_transient_voicevox_error(
                "VOICEVOX: Không kết nối được VOICEVOX Engine: timed out"
            )
        )
        self.assertTrue(
            _is_transient_voicevox_error(
                "Precheck không nhận được phản hồi từ voicevox sau khi đã tự thử lại."
            )
        )
        self.assertFalse(
            _is_transient_voicevox_error(
                "VOICEVOX endpoint phải là HTTP cục bộ."
            )
        )

    def test_closed_donut_is_expected_startup_state(self) -> None:
        self.assertTrue(
            _is_expected_donut_unavailable(
                "Không kết nối được MCP Donut local. Hãy mở Donut Browser và bật MCP."
            )
        )
        self.assertTrue(
            _is_expected_donut_unavailable(
                "ConnectionRefusedError: [WinError 10061] actively refused"
            )
        )
        self.assertFalse(_is_expected_donut_unavailable("MCP trả JSON không hợp lệ"))

    def test_diagnostic_sessions_keep_only_active_and_latest_logs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomLogs_") as temp:
            log_dir = Path(temp)
            active = log_dir / "diagnostic.log"
            active.write_text("unfinished prior run\n", encoding="utf-8")
            (log_dir / "latest-older.log").write_text("old\n", encoding="utf-8")
            (log_dir / "diagnostic.previous.log").write_text(
                "legacy\n", encoding="utf-8"
            )
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.diagnostic_log_path = active
            app.diagnostic_log_lock = threading.Lock()
            app.diagnostic_session_closed = False

            app._start_diagnostic_session()

            self.assertTrue(active.is_file())
            self.assertEqual(len(list(log_dir.glob("*.log"))), 2)
            app._append_log("Workflow · stage=precheck · status=running")
            app._finish_diagnostic_session("Đóng test")
            self.assertFalse(active.exists())
            self.assertEqual(len(list(log_dir.glob("latest-*.log"))), 1)

            reopened = DocuRandomApp.__new__(DocuRandomApp)
            reopened.diagnostic_log_path = active
            reopened.diagnostic_log_lock = threading.Lock()
            reopened.diagnostic_session_closed = False
            reopened._start_diagnostic_session()

            self.assertTrue(active.is_file())
            self.assertEqual(len(list(log_dir.glob("*.log"))), 2)

    def test_resume_after_flow_captcha_skips_chatgpt_and_applies_saved_content(self) -> None:
        saved = ContentRunResult(
            artifacts=ContentArtifacts(
                title="title",
                description="description",
                thumbnail_prompt="thumbnail",
                voicevox_script="voice",
                voicevox_export_script="A: voice",
                image_prompts=("image one", "image two"),
            ),
            package_path=Path("C:/output/video.txt"),
            transcript_path=Path("C:/output/video_CHAT.txt"),
            checkpoint_path=Path("C:/output/video_AI_CHECKPOINT.json"),
            thread_id="saved-thread",
            turns=(ChatTurn("assistant", "ĐÃ XONG"),),
        )
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.content_token = 9
        app.content_busy = True
        app.content_precheck_pending = True
        app.content_generation_started = False
        app.content_codex_ready = True
        app.content_voice_ready = True
        app.content_flow_ready = True
        app.content_cli_status = None
        app.content_snapshot = {"resume_content_result": saved}
        app._set_content_progress = Mock()
        app._append_content_chat = Mock()
        app._record_pipeline_stage = Mock()
        app._apply_content_result = Mock()

        with patch("docurandom.ui.ChatGPTWebChat") as chat:
            app._maybe_begin_content_generation(9)

        chat.assert_not_called()
        app._apply_content_result.assert_called_once_with(saved)
        self.assertTrue(app.content_generation_started)
        self.assertFalse(app.content_precheck_pending)

    def test_saved_writing_still_requires_web_precheck_for_pending_voice_audit(self) -> None:
        saved = ContentRunResult(
            artifacts=ContentArtifacts(title='title', description='description', thumbnail_prompt='thumbnail', voicevox_script='voice', voicevox_export_script='A: voice', image_prompts=('image',)),
            package_path=Path('video.txt'),
            transcript_path=Path('video_CHAT.txt'),
            checkpoint_path=Path('video_AI_CHECKPOINT.json'),
            thread_id='saved-thread', turns=(),
        )
        app = DocuRandomApp.__new__(DocuRandomApp)
        for stage, audit, required in (
            ('voicevox', True, True),
            ('flow_images', True, True),
            ('render', True, False),
            ('voicevox', False, False),
        ):
            with self.subTest(stage=stage, audit=audit):
                app.content_snapshot = {'resume': True, 'resume_content_result': saved, 'resume_stage': stage}
                app.voicevox_ai_audit_var = _Var(audit)
                self.assertEqual(app._content_chatgpt_required_for_resume(), required)

    def test_thumbnail_text_response_cannot_be_mislabeled_as_watermark_error(self) -> None:
        error = RuntimeError('ChatGPT không trả về dữ liệu ảnh thumbnail. Phản hồi: prompt yêu cầu no watermark, sharp industrial photo')
        self.assertEqual(DocuRandomApp._diagnostic_error_code(error), 'THUMBNAIL_NO_IMAGE')

    def test_new_flow_batch_clears_stale_render_join_before_voice_can_finish(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomFlowJoin_") as temp:
            root = Path(temp)
            result = ContentRunResult(
                artifacts=ContentArtifacts(
                    title="title",
                    description="description",
                    thumbnail_prompt="thumbnail",
                    voicevox_script="voice",
                    voicevox_export_script="A: voice",
                    image_prompts=("image one", "image two"),
                ),
                package_path=root / "video.txt",
                transcript_path=root / "video_CHAT.txt",
                checkpoint_path=root / "video_AI_CHECKPOINT.json",
                thread_id="saved-thread",
                turns=(ChatTurn("assistant", "ĐÃ XONG"),),
            )
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.content_busy = True
            app.content_precheck_pending = True
            app.content_generation_started = True
            app.content_last_result = None
            app.last_content_package = None
            app.content_snapshot = {
                "flow_enabled": True,
                "output_dir": root,
            }
            app.workspace = None
            app.flow_render_pending = True
            app.voicevox_script_text = Mock()
            app.voicevox_backup_dir_var = _Var("")
            app.root = Mock()
            app._cancel_content_precheck_watchdog = Mock()
            app._record_pipeline_stage = Mock()
            app._set_content_progress = Mock()
            app._refresh_voicevox_backup_summary = Mock()
            app._refresh_smart_action = Mock()
            app._save_config = Mock()
            app._append_content_chat = Mock()
            app._refresh_content_controls = Mock()
            app._start_chatgpt_thumbnail_generation = Mock()
            app._start_pipeline_voice_prefetch = Mock(
                side_effect=lambda: self.assertFalse(app.flow_render_pending)
            )
            app._start_flow_image_generation = Mock()

            app._apply_content_result(result)

            self.assertFalse(app.flow_render_pending)
            app._save_config.assert_called()
            app._start_pipeline_voice_prefetch.assert_called_once_with()
            app.root.after_idle.assert_called_once()

    def test_chatgpt_web_ready_status_turns_badge_green(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.content_cli_status = None
        app.content_codex_ready = False
        app.content_codex_status_var = _Var("")
        app.content_codex_detail_var = _Var("")
        app.content_codex_status_label = Mock()
        app._refresh_content_controls = Mock()
        status = ChatGPTWebStatus(
            launcher_path=Path("C:/ChatGPTWeb/Codex Web GPT.exe"),
            installed=True,
            launcher_running=True,
            setup_complete=True,
            authenticated=True,
            runtime_ready=True,
            endpoint="http://127.0.0.1:17841",
            bridge_version="2.1.9",
            available_models=("chatgpt-web/high",),
            detail="Phiên ChatGPT Web riêng đã sẵn sàng.",
            access_token="docurandom_bridge_token_0123456789_abcdefghijklmnop",
            codex_path=Path("C:/Codex/codex.exe"),
            codex_version="codex-cli test",
            codex_profile_path=Path("C:/Users/test/.codex/docurandom-chatgpt-web.config.toml"),
            codex_ready=True,
        )

        app._apply_codex_status_ui(status)

        self.assertIs(app.content_cli_status, status)
        self.assertTrue(app.content_codex_ready)
        self.assertEqual(
            app.content_codex_status_var.get(),
            "● CODEX + CHATGPT WEB SẴN SÀNG",
        )
        self.assertIn("sẵn sàng", app.content_codex_detail_var.get())
        self.assertIn("mức suy luận bạn chọn", app.content_codex_detail_var.get())
        app.content_codex_status_label.configure.assert_called_once_with(
            style="EngineReady.TLabel"
        )

    def test_chatgpt_web_busy_status_stays_green_instead_of_losing_session(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.content_cli_status = None
        app.content_codex_ready = False
        app.content_codex_status_var = _Var("")
        app.content_codex_detail_var = _Var("")
        app.content_codex_status_label = Mock()
        app._refresh_content_controls = Mock()
        status = ChatGPTWebStatus(
            launcher_path=Path("C:/ChatGPTWeb/Codex Web GPT.exe"),
            installed=True,
            launcher_running=True,
            setup_complete=True,
            authenticated=True,
            runtime_ready=True,
            session_busy=True,
            endpoint="http://127.0.0.1:17841",
            available_models=("chatgpt-web/high",),
            access_token="docurandom_bridge_token_0123456789_abcdefghijklmnop",
            codex_path=Path("C:/Codex/codex.exe"),
            codex_version="codex-cli test",
            codex_ready=True,
        )

        app._apply_codex_status_ui(status)

        self.assertFalse(app.content_codex_ready)
        self.assertIn("ĐANG BẬN", app.content_codex_status_var.get())
        app.content_codex_status_label.configure.assert_called_once_with(
            style="EngineReady.TLabel"
        )

    def test_startup_precheck_is_shared_and_cached_between_workspaces(self) -> None:
        coordinator = _SharedCodexPrecheck()

        def make_app() -> DocuRandomApp:
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.closed = False
            app.closing_after_cancel = False
            app.codex_checking = False
            app.codex_installing = False
            app.codex_check_token = 0
            app.codex_precheck = coordinator
            app.events = Mock()
            app._append_log = Mock()
            app._set_codex_checking_ui = Mock()
            return app

        class _ImmediateThread:
            def __init__(self, *, target, **_kwargs) -> None:
                self.target = target

            def start(self) -> None:
                self.target()

        bridge_status = ChatGPTWebStatus(
            launcher_path=Path("C:/ChatGPTWeb/Codex Web GPT.exe"),
            installed=True,
            launcher_running=True,
            setup_complete=True,
            authenticated=True,
            runtime_ready=True,
            endpoint="http://127.0.0.1:17841",
            available_models=("chatgpt-web/high",),
            access_token="docurandom_bridge_token_0123456789_abcdefghijklmnop",
        )
        cli_status = CodexCliStatus(
            path=Path("C:/Codex/codex.exe"),
            version="codex-cli test",
            logged_in=True,
        )
        first = make_app()
        second = make_app()
        with (
            patch("docurandom.ui.threading.Thread", _ImmediateThread),
            patch(
                "docurandom.ui.check_chatgpt_web_bridge",
                return_value=bridge_status,
            ) as check_bridge,
            patch("docurandom.ui.check_codex_cli", return_value=cli_status),
            patch(
                "docurandom.ui.ensure_chatgpt_web_codex_profile",
                return_value=Path("C:/Codex/profile.toml"),
            ),
        ):
            first._check_codex_async(reason="startup")
            second._check_codex_async(reason="startup")

        check_bridge.assert_called_once()
        self.assertEqual(first.events.put.call_count, 1)
        self.assertEqual(second.events.put.call_count, 1)

    def test_login_success_is_detected_automatically_without_running_pipeline(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.codex_check_token = 11
        app.codex_checking = True
        app.codex_login_poll_active = True
        app.closed = False
        app.content_status_var = _Var("")
        app.content_precheck_pending = False
        app.content_busy = False
        app.content_token = 0
        app._apply_codex_status_ui = Mock()
        app._refresh_content_controls = Mock()
        status = CodexCliStatus(
            path=Path("C:/Codex/codex.exe"),
            version="codex-cli test",
            logged_in=True,
            login_method="ChatGPT",
        )

        app._handle_codex_checked(11, "login", None, 60, status)

        self.assertFalse(app.codex_checking)
        self.assertFalse(app.codex_login_poll_active)
        self.assertIn("thành công", app.content_status_var.get())
        app._apply_codex_status_ui.assert_called_once_with(status)

    def test_login_wait_rechecks_after_two_seconds(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.codex_check_token = 5
        app.codex_checking = True
        app.codex_login_poll_active = True
        app.closed = False
        app.content_codex_detail_var = _Var("")
        app.content_precheck_pending = False
        app.content_busy = False
        app.content_token = 0
        app.root = Mock()
        app._apply_codex_status_ui = Mock()
        app._refresh_content_controls = Mock()
        app._check_codex_async = Mock()
        status = CodexCliStatus(
            path=Path("C:/Codex/codex.exe"),
            version="",
            logged_in=False,
            detail="CLI chưa sẵn sàng",
        )

        app._handle_codex_checked(5, "login", None, 3, status)

        app.root.after.assert_called_once()
        delay, callback = app.root.after.call_args.args
        self.assertEqual(delay, 2000)
        callback()
        app._check_codex_async.assert_called_once_with(
            reason="login",
            poll_remaining=2,
        )

    def test_login_poll_uses_passive_status_without_refreshing_chatgpt(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.codex_checking = False
        app.codex_installing = False
        app.codex_check_token = 2
        app.events = Mock()
        app._set_codex_checking_ui = Mock()

        class _ImmediateThread:
            def __init__(self, *, target, **_kwargs) -> None:
                self.target = target

            def start(self) -> None:
                self.target()

        with (
            patch("docurandom.ui.threading.Thread", _ImmediateThread),
            patch("docurandom.ui.check_chatgpt_web_bridge") as check,
        ):
            check.return_value = ChatGPTWebStatus(launcher_path=None)
            app._check_codex_async(reason="login", poll_remaining=3)

        check.assert_called_once_with(
            auto_start=False,
            passive_session=True,
        )

    def test_busy_pipeline_waits_passively_instead_of_reporting_expired(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.codex_check_token = 7
        app.codex_checking = True
        app.codex_login_poll_active = False
        app.closed = False
        app.content_precheck_pending = True
        app.content_busy = True
        app.content_token = 12
        app.root = Mock()
        app._apply_codex_status_ui = Mock()
        app._refresh_content_controls = Mock()
        app._set_content_progress = Mock()
        app._check_codex_async = Mock()
        app._maybe_begin_content_generation = Mock()
        app._finish_content_error = Mock()
        status = ChatGPTWebStatus(
            launcher_path=Path("C:/ChatGPTWeb/Codex Web GPT.exe"),
            installed=True,
            launcher_running=True,
            authenticated=True,
            session_busy=True,
            detail="Launcher busy",
        )

        app._handle_codex_checked(7, "pipeline", 12, 4, status)

        app._finish_content_error.assert_not_called()
        app._maybe_begin_content_generation.assert_not_called()
        delay, callback = app.root.after.call_args.args
        self.assertEqual(delay, 2000)
        callback()
        app._check_codex_async.assert_called_once_with(
            reason="pipeline_wait",
            pipeline_token=12,
            poll_remaining=3,
        )

    def test_pipeline_wait_performs_one_strong_check_after_launcher_is_idle(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.codex_check_token = 9
        app.codex_checking = True
        app.codex_login_poll_active = False
        app.closed = False
        app.content_precheck_pending = True
        app.content_busy = True
        app.content_token = 15
        app.root = Mock()
        app._apply_codex_status_ui = Mock()
        app._refresh_content_controls = Mock()
        app._check_codex_async = Mock()
        app._maybe_begin_content_generation = Mock()
        app._finish_content_error = Mock()
        status = ChatGPTWebStatus(
            launcher_path=Path("C:/ChatGPTWeb/Codex Web GPT.exe"),
            installed=True,
            launcher_running=True,
            setup_complete=True,
            authenticated=True,
            runtime_ready=True,
            endpoint="http://127.0.0.1:17841",
            available_models=("chatgpt-web/high",),
            access_token="docurandom_bridge_token_0123456789_abcdefghijklmnop",
        )

        app._handle_codex_checked(9, "pipeline_wait", 15, 3, status)

        app._maybe_begin_content_generation.assert_not_called()
        callback = app.root.after_idle.call_args.args[0]
        callback()
        app._check_codex_async.assert_called_once_with(
            reason="pipeline",
            pipeline_token=15,
            poll_remaining=3,
        )

    def test_open_codex_login_starts_automatic_status_monitor(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.content_busy = False
        app.closed = False
        app.closing_after_cancel = False
        launcher = Path("C:/ChatGPTWeb/Codex Web GPT.exe")
        app.content_cli_status = ChatGPTWebStatus(
            launcher_path=launcher,
            installed=True,
        )
        app.codex_login_poll_active = False
        app.content_codex_ready = False
        app.content_status_var = _Var("")
        app.content_codex_status_var = _Var("")
        app.content_codex_detail_var = _Var("")
        app.content_codex_status_label = Mock()
        app.root = Mock()
        app._refresh_content_controls = Mock()
        app._check_codex_async = Mock()

        with (
            patch(
                "docurandom.ui.discover_chatgpt_web_launcher",
                return_value=launcher,
            ),
            patch("docurandom.ui.start_chatgpt_web_launcher") as start,
        ):
            app._open_codex_login()

        start.assert_called_once_with(show=True)
        self.assertTrue(app.codex_login_poll_active)
        self.assertIn("chỉ cần đăng nhập", app.content_status_var.get())
        delay, callback = app.root.after.call_args.args
        self.assertEqual(delay, 1200)
        callback()
        app._check_codex_async.assert_called_once_with(
            reason="login",
            poll_remaining=180,
        )

    def test_cancel_content_pipeline_stops_cli_and_voicevox_precheck(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.content_busy = True
        app.content_precheck_pending = True
        app.content_cancel_event = threading.Event()
        app.content_runner = Mock()
        app.voicevox_busy = True
        app.content_status_var = _Var("")
        app.content_cancel_button = Mock()
        app._cancel_voicevox = Mock()

        app._cancel_content_pipeline()

        self.assertTrue(app.content_cancel_event.is_set())
        app.content_runner.cancel.assert_called_once_with()
        app._cancel_voicevox.assert_called_once_with()
        app.content_cancel_button.configure.assert_called_once_with(state="disabled")

    def test_cancel_content_pipeline_remains_available_during_flow(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.content_busy = False
        app.flow_generation_busy = True
        app.pipeline_auto_resume_after_id = None
        app.pipeline_pause_requested = False
        app.flow_cancel_event = threading.Event()
        app.content_cancel_event = threading.Event()
        app.content_runner = None
        app.content_precheck_pending = False
        app.voicevox_busy = False
        app.content_status_var = _Var("")
        app.content_cancel_button = Mock()
        app.content_pause_button = Mock()

        app._cancel_content_pipeline()

        self.assertTrue(app.flow_cancel_event.is_set())
        self.assertIn("hủy tạo ảnh Flow", app.content_status_var.get())
        app.content_cancel_button.configure.assert_called_once_with(state="disabled")
        app.content_pause_button.configure.assert_called_once_with(state="disabled")

    def test_pause_stops_pending_auto_repair_without_losing_checkpoint(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_auto_resume_after_id = "retry-timer"
        app.pipeline_pause_requested = False
        app.flow_cancel_event = threading.Event()
        app.content_cancel_event = threading.Event()
        app.content_runner = None
        app.content_precheck_pending = False
        app.voicevox_busy = False
        app.content_status_var = _Var("")
        app.root = Mock()
        app._append_content_chat = Mock()
        app._refresh_content_controls = Mock()

        app._pause_content_pipeline()

        self.assertTrue(app.pipeline_pause_requested)
        self.assertIsNone(app.pipeline_auto_resume_after_id)
        app.root.after_cancel.assert_called_once_with("retry-timer")
        self.assertIn("Đã tạm dừng", app.content_status_var.get())
        app._append_content_chat.assert_called_once()

    def test_turning_off_auto_repair_cancels_scheduled_retry(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.pipeline_auto_repair_var = _Var(False)
        app.pipeline_auto_resume_after_id = "retry-timer"
        app.content_status_var = _Var("")
        app.root = Mock()
        app._append_content_chat = Mock()
        app._save_config = Mock()
        app._refresh_content_controls = Mock()

        app._pipeline_auto_repair_changed()

        app.root.after_cancel.assert_called_once_with("retry-timer")
        self.assertIsNone(app.pipeline_auto_resume_after_id)
        self.assertIn("Đã tắt tự sửa", app.content_status_var.get())
        app._save_config.assert_called_once_with()

    def test_armed_schedule_auto_recovers_slow_chat_without_manual_checkbox(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.pipeline_auto_repair_var = _Var(False)
        app.schedule_armed_var = _Var(True)
        app.schedule_shifts = [
            ScheduleShift(
                "shift-1",
                "31/08/2026 04:00",
                "31/08/2026 05:00",
            )
        ]
        app.schedule_active_shift_id = "shift-1"
        app.schedule_active_stage = "error"
        app.pipeline_auto_retry_count = 0
        app.pipeline_auto_resume_after_id = None
        app.pipeline_pause_requested = False
        app.closed = False
        app.closing_after_cancel = False
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_voice_busy = False
        app.content_status_var = _Var("")
        app.root = Mock()
        app.root.after.return_value = "retry-timer"
        app._append_content_chat = Mock()
        app._refresh_content_controls = Mock()
        app._save_config = Mock()
        app._start_content_pipeline = Mock()

        with patch(
            "docurandom.ui.vietnam_now",
            return_value=datetime(2026, 8, 31, 4, 0, tzinfo=VIETNAM_TIMEZONE),
        ):
            app._schedule_pipeline_auto_resume(
                "chatgpt",
                "ChatGPT đã ngừng cập nhật phản hồi quá lâu.",
            )

        self.assertEqual(app.schedule_active_stage, "retry")
        self.assertEqual(app.root.after.call_args.args[0], 900_000)
        callback = app.root.after.call_args.args[1]
        callback()
        self.assertEqual(app.schedule_active_stage, "workflow")
        app._start_content_pipeline.assert_called_once_with(resume=True, show_error_dialog=False)

    @patch("docurandom.ui.messagebox.showerror")
    def test_armed_schedule_precheck_error_waits_for_15_minute_retry(
        self,
        showerror: Mock,
    ) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.content_busy = False
        app.pipeline_voice_busy = False
        app.thumbnail_ai_busy = False
        app.closed = False
        app.closing_after_cancel = False
        app.schedule_armed_var = _Var(True)
        app.schedule_shifts = [
            ScheduleShift(
                "shift-1",
                "31/08/2026 04:00",
                "31/08/2026 05:00",
            )
        ]
        app.schedule_active_shift_id = "shift-1"
        app.schedule_active_stage = "workflow"
        app.content_status_var = _Var("")
        app._content_input_snapshot = Mock(side_effect=ValueError("thiếu cấu hình"))
        app._append_exception_log = Mock()
        app._refresh_content_input_status = Mock()
        app._save_config = Mock()
        app._schedule_pipeline_auto_resume = Mock(return_value=True)

        app._start_content_pipeline(resume=True)

        self.assertEqual(app.schedule_active_stage, "error")
        self.assertIn("15 phút", app.schedule_last_event)
        app._schedule_pipeline_auto_resume.assert_called_once_with(
            "precheck đầu vào",
            "thiếu cấu hình",
        )
        showerror.assert_not_called()

    def test_manual_slow_chat_also_recovers_without_resending(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.pipeline_auto_repair_var = _Var(False)
        app.schedule_armed_var = _Var(False)
        app.pipeline_auto_retry_count = 0
        app.pipeline_auto_resume_after_id = None
        app.pipeline_pause_requested = False
        app.closed = False
        app.closing_after_cancel = False
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_voice_busy = False
        app.content_status_var = _Var("")
        app.root = Mock()
        app.root.after.return_value = "retry-timer"
        app._append_content_chat = Mock()
        app._refresh_content_controls = Mock()
        app._start_content_pipeline = Mock()

        app._schedule_pipeline_auto_resume(
            "chatgpt",
            "ChatGPT response stopped making progress before the inactivity timeout",
        )

        self.assertEqual(app.root.after.call_args.args[0], 15_000)
        callback = app.root.after.call_args.args[1]
        callback()
        app._start_content_pipeline.assert_called_once_with(resume=True, show_error_dialog=False)

    def test_terminal_chatgpt_timeout_keeps_checkpoint_without_auto_retry(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.pipeline_auto_repair_var = _Var(True)
        app.schedule_armed_var = _Var(True)
        app.schedule_active_shift_id = "shift-1"
        app.schedule_active_stage = "error"
        app.content_status_var = _Var("")
        app.pipeline_auto_resume_after_id = None
        app.closed = False
        app.closing_after_cancel = False
        app.pipeline_pause_requested = False
        app.root = Mock()
        app._append_content_chat = Mock()
        app._refresh_content_controls = Mock()
        app._save_config = Mock()

        scheduled = app._schedule_pipeline_auto_resume(
            "chatgpt",
            "Phản hồi ChatGPT đã vượt giới hạn an toàn · checkpoint vẫn được giữ.",
        )

        self.assertFalse(scheduled)
        self.assertEqual(app.schedule_active_stage, "error")
        app.root.after.assert_not_called()
        app._save_config.assert_called_once_with()
        self.assertIn("checkpoint", app.content_status_var.get())

    def test_conversation_identity_error_keeps_checkpoint_without_auto_retry(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.pipeline_auto_repair_var = _Var(True)
        app.schedule_armed_var = _Var(True)
        app.schedule_active_shift_id = "shift-1"
        app.schedule_active_stage = "error"
        app.content_status_var = _Var("")
        app.pipeline_auto_resume_after_id = None
        app.closed = False
        app.closing_after_cancel = False
        app.pipeline_pause_requested = False
        app.root = Mock()
        app._append_content_chat = Mock()
        app._refresh_content_controls = Mock()
        app._save_config = Mock()

        scheduled = app._schedule_pipeline_auto_resume(
            "chatgpt",
            "Đọc phản hồi: ChatGPT đã chuyển khỏi đúng cuộc hội thoại đang xử lý; "
            "checkpoint vẫn được giữ, không gửi lại prompt.",
        )

        self.assertFalse(scheduled)
        self.assertEqual(app.schedule_active_stage, "error")
        app.root.after.assert_not_called()
        app._save_config.assert_called_once_with()
        self.assertIn("checkpoint", app.content_status_var.get())

    def test_exhausted_targeted_content_repairs_stop_automatic_retry(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app._require_manual_pipeline_resume = Mock()
        app._append_content_chat = Mock()
        app.root = Mock()
        scheduled = app._schedule_pipeline_auto_resume(
            'chatgpt', 'ChatGPT đã hết số lần khôi phục phần nội dung thiếu; checkpoint giữ nguyên.'
        )
        self.assertFalse(scheduled)
        app._require_manual_pipeline_resume.assert_called_once()
        app.root.after.assert_not_called()

    def test_manual_chatgpt_rate_limit_waits_five_minutes_before_recovery(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.pipeline_auto_repair_var = _Var(False)
        app.schedule_armed_var = _Var(False)
        app.pipeline_auto_retry_count = 0
        app.pipeline_auto_resume_after_id = None
        app.pipeline_pause_requested = False
        app.closed = False
        app.closing_after_cancel = False
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_voice_busy = False
        app.content_status_var = _Var("")
        app.root = Mock()
        app.root.after.return_value = "rate-limit-retry"
        app._append_content_chat = Mock()
        app._refresh_content_controls = Mock()
        app._start_content_pipeline = Mock()

        scheduled = app._schedule_pipeline_auto_resume(
            "chatgpt",
            "ChatGPT đang tạm giới hạn do có quá nhiều yêu cầu.",
        )

        self.assertTrue(scheduled)
        self.assertEqual(app.root.after.call_args.args[0], 300_000)
        self.assertIn("300 giây", app.content_status_var.get())

    def test_manual_transient_voicevox_precheck_recovers_automatically(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.pipeline_auto_repair_var = _Var(False)
        app.schedule_armed_var = _Var(False)
        app.pipeline_auto_retry_count = 0
        app.pipeline_auto_resume_after_id = None
        app.pipeline_pause_requested = False
        app.closed = False
        app.closing_after_cancel = False
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_voice_busy = False
        app.rendering = False
        app.audio_preflight_active = False
        app.content_status_var = _Var("")
        app.root = Mock()
        app.root.after.return_value = "retry-timer"
        app._append_content_chat = Mock()
        app._refresh_content_controls = Mock()
        app._start_content_pipeline = Mock()

        scheduled = app._schedule_pipeline_auto_resume(
            "precheck",
            "Precheck không nhận được phản hồi từ voicevox sau khi đã tự thử lại.",
        )

        self.assertTrue(scheduled)
        self.assertEqual(app.root.after.call_args.args[0], 15_000)
        callback = app.root.after.call_args.args[1]
        callback()
        app._start_content_pipeline.assert_called_once_with(resume=True, show_error_dialog=False)

    def test_auto_resume_waits_for_running_flow_without_consuming_retry(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.pipeline_auto_repair_var = _Var(False)
        app.schedule_armed_var = _Var(False)
        app.pipeline_auto_retry_count = 0
        app.pipeline_auto_resume_after_id = None
        app.pipeline_pause_requested = False
        app.closed = False
        app.closing_after_cancel = False
        app.content_busy = False
        app.flow_generation_busy = True
        app.pipeline_voice_busy = False
        app.rendering = False
        app.audio_preflight_active = False
        app.content_status_var = _Var("")
        app.root = Mock()
        app.root.after.side_effect = ["initial-retry", "busy-poll"]
        app._append_content_chat = Mock()
        app._refresh_content_controls = Mock()
        app._start_content_pipeline = Mock()

        app._schedule_pipeline_auto_resume(
            "VOICEVOX",
            "VOICEVOX: Không kết nối được VOICEVOX Engine: timed out",
        )
        callback = app.root.after.call_args_list[0].args[1]
        callback()

        self.assertEqual(app.pipeline_auto_retry_count, 1)
        self.assertEqual(app.pipeline_auto_resume_after_id, "busy-poll")
        self.assertEqual(app.root.after.call_args_list[1].args[0], 2000)
        app._start_content_pipeline.assert_not_called()

    def test_claimed_schedule_recovers_after_restart_without_resending(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomScheduleRecover_") as temp:
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.schedule_armed_var = _Var(True)
            app.schedule_shifts = [
                ScheduleShift(
                    "shift-1",
                    "28/08/2026 04:00",
                    "28/08/2026 05:00",
                )
            ]
            app.schedule_active_shift_id = "shift-1"
            app.schedule_active_stage = "workflow"
            app.pipeline_auto_resume_after_id = None
            app.content_busy = False
            app.flow_generation_busy = False
            app.pipeline_voice_busy = False
            app.thumbnail_ai_busy = False
            app.rendering = False
            app.scanning = False
            app.audio_preflight_active = False
            app.youtube_upload_busy = False
            app.schedule_last_event = ""
            app._content_input_snapshot = Mock(return_value={"output_dir": Path(temp)})
            checkpoint = Path(temp) / "pipeline.json"
            app._pipeline_checkpoint_path = Mock(return_value=checkpoint)
            app._save_config = Mock()
            app._start_content_pipeline = Mock()

            app._resume_active_scheduled_pipeline_if_idle()

            self.assertEqual(app.schedule_active_stage, "workflow")
            self.assertIn("không gửi trùng", app.schedule_last_event)
            app._start_content_pipeline.assert_called_once_with(resume=False)

    def test_schedule_cutoff_defers_final_afternoon_video_to_next_morning(
        self,
    ) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.workspace = SimpleNamespace(state_dir=Path(tempfile.mkdtemp()))
        app.schedule_shifts = [
            ScheduleShift(
                "shift-1",
                "29/08/2026 13:00",
                "29/08/2026 14:45",
            )
        ]
        app.schedule_active_shift_id = "shift-1"
        app.schedule_active_occurrence_id = "workflow:shift-1"
        app.schedule_active_stage = "retry"
        app.pipeline_auto_resume_after_id = "retry-in-15-minutes"
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_voice_busy = False
        app.thumbnail_ai_busy = False
        app.rendering = False
        app.scanning = False
        app.audio_preflight_active = False
        app.youtube_upload_busy = False
        app.root = Mock()
        app._scheduled_video_is_ready = Mock(return_value=False)
        app._append_log = Mock()
        app._claim_schedule = Mock()
        app._save_config = Mock()
        app._refresh_schedule_status = Mock()
        app._schedule_log_once = Mock()

        cutoff = datetime(2026, 8, 29, 16, 45, tzinfo=VIETNAM_TIMEZONE)
        skipped = app._skip_active_schedule_past_readiness_deadline(cutoff)

        self.assertTrue(skipped)
        self.assertEqual(app.schedule_active_stage, "deferred-upload")
        self.assertTrue(app.schedule_deferred_upload)
        app._claim_schedule.assert_called_once()

    def test_any_missed_upload_slot_waits_for_the_next_slot(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.workspace = Mock()
        app.schedule_shifts = [
            ScheduleShift(
                "shift-morning",
                "11/09/2026 04:30",
                "11/09/2026 08:30",
            )
        ]
        app.schedule_active_shift_id = "shift-morning"
        app.schedule_active_occurrence_id = "workflow:shift-morning"
        app.schedule_active_stage = "render"
        app.schedule_deferred_upload = False
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_voice_busy = False
        app.thumbnail_ai_busy = False
        app.rendering = True
        app.scanning = False
        app.audio_preflight_active = False
        app.youtube_upload_busy = False
        app._scheduled_video_is_ready = Mock(return_value=False)
        app._append_log = Mock()
        app._claim_schedule = Mock()
        app._save_config = Mock()
        app._refresh_schedule_status = Mock()
        app._schedule_log_once = Mock()

        current = datetime(2026, 9, 11, 10, 30, tzinfo=VIETNAM_TIMEZONE)
        skipped = app._skip_active_schedule_past_readiness_deadline(current)

        self.assertTrue(skipped)
        self.assertEqual(app.schedule_active_stage, "deferred-upload")
        self.assertTrue(app.schedule_deferred_upload)
        app._claim_schedule.assert_called_once()

    @patch("docurandom.ui.resolve_workspace_upload_sources")
    @patch("docurandom.ui.resolve_donut_mcp_endpoint", return_value=Mock())
    def test_deferred_final_video_is_uploaded_by_next_morning_slot(
        self,
        _endpoint: Mock,
        resolve_sources: Mock,
    ) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        # This test isolates scheduling after the material preflight passes.
        app._automatic_thumbnail_required_but_missing = Mock(return_value=False)
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_upload_busy = False
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_voice_busy = False
        app.thumbnail_ai_busy = False
        app.rendering = False
        app.scanning = False
        app.audio_preflight_active = False
        app.schedule_active_shift_id = "shift-final"
        app.schedule_active_occurrence_id = "youtube-upload:shift-final:old"
        app.schedule_active_stage = "deferred-upload"
        app.schedule_deferred_upload = True
        app.schedule_last_event = ""
        app.schedule_shifts = [
            ScheduleShift(
                "shift-final",
                "10/09/2026 13:00",
                "11/09/2026 14:45",
            ),
            ScheduleShift(
                "shift-morning",
                "11/09/2026 04:30",
                "11/09/2026 10:00",
            ),
        ]
        app.workspace = Mock()
        app.youtube_selected_profile_id = "profile-1"
        app.last_content_package = None
        app._effective_youtube_thumbnail = Mock(return_value=None)
        app._scheduled_video_is_ready = Mock(return_value=True)
        app._claim_schedule = Mock()
        app._start_youtube_upload = Mock()
        app._schedule_log_once = Mock()

        due = ScheduleEvaluation(
            "due",
            scheduled_at=datetime(2026, 9, 11, 10, 0, tzinfo=VIETNAM_TIMEZONE),
            occurrence_id="youtube-upload:shift-morning:20260911T1000",
        )
        with patch(
            "docurandom.ui.vietnam_now",
            return_value=datetime(2026, 9, 11, 10, 1, tzinfo=VIETNAM_TIMEZONE),
        ):
            app._trigger_scheduled_upload(due, shift_id="shift-morning")

        app._claim_schedule.assert_called_once()
        app._start_youtube_upload.assert_called_once_with(
            automatic=True,
            defer_public_until_slot=True,
            publish_if_no_older_video=False,
        )
        resolve_sources.assert_called_once()

    @patch("docurandom.ui.resolve_workspace_upload_sources")
    @patch("docurandom.ui.resolve_donut_mcp_endpoint", return_value=Mock())
    def test_deferred_public_video_is_promoted_only_by_morning_slot(
        self,
        _endpoint: Mock,
        resolve_sources: Mock,
    ) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_upload_busy = False
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_voice_busy = False
        app.thumbnail_ai_busy = False
        app.rendering = False
        app.scanning = False
        app.audio_preflight_active = False
        app.schedule_active_shift_id = "shift-final"
        app.schedule_active_occurrence_id = "youtube-upload:shift-final:old"
        app.schedule_active_stage = "deferred-public"
        app.schedule_deferred_upload = False
        app.schedule_deferred_public = True
        app.schedule_last_event = ""
        app.schedule_shifts = [
            ScheduleShift(
                "shift-final",
                "10/09/2026 13:00",
                "11/09/2026 14:45",
            ),
            ScheduleShift(
                "shift-morning",
                "11/09/2026 04:30",
                "11/09/2026 10:00",
            ),
        ]
        app.workspace = Mock()
        app.youtube_selected_profile_id = "profile-1"
        app.last_content_package = None
        app._effective_youtube_thumbnail = Mock(return_value=None)
        app._automatic_thumbnail_required_but_missing = Mock(return_value=False)
        app._claim_schedule = Mock()
        app._start_youtube_upload = Mock()
        app._schedule_log_once = Mock()

        due = ScheduleEvaluation(
            "due",
            scheduled_at=datetime(2026, 9, 11, 10, 0, tzinfo=VIETNAM_TIMEZONE),
            occurrence_id="youtube-upload:shift-morning:20260911T1000",
        )
        with patch(
            "docurandom.ui.vietnam_now",
            return_value=datetime(2026, 9, 11, 10, 1, tzinfo=VIETNAM_TIMEZONE),
        ):
            app._trigger_scheduled_upload(due, shift_id="shift-morning")

        self.assertFalse(app.schedule_deferred_public)
        app._claim_schedule.assert_called_once()
        self.assertIn("Công khai", app._claim_schedule.call_args.args[2])
        app._start_youtube_upload.assert_called_once_with(automatic=True)
        resolve_sources.assert_called_once()

    def test_packaged_queue_uses_morning_visibility_job_without_claiming_active_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDeferredQueue_") as temp:
            state_dir = Path(temp) / "State"
            state_dir.mkdir(parents=True)
            item = {
                "identity": "queue-identity",
                "attempt_id": "queue-attempt",
                "workspace_id": "workspace-queue",
                "profile_id": "profile-queue",
                "youtube_url": "https://youtu.be/Queue123",
                "privacy": "UNLISTED",
                "final_privacy": "PUBLIC",
            }
            (state_dir / "deferred_public_queue.json").write_text(
                json.dumps({"version": 1, "items": [item]}),
                encoding="utf-8",
            )
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.workspace = SimpleNamespace(state_dir=state_dir)
            app.closed = False
            app.closing_after_cancel = False
            app.youtube_upload_busy = False
            app.schedule_active_shift_id = "new-production"
            app.schedule_active_stage = "workflow"
            app.schedule_last_event = ""
            app.schedule_shifts = [
                ScheduleShift(
                    "shift-morning",
                    "13/09/2026 04:30",
                    "13/09/2026 08:30",
                )
            ]
            app._start_deferred_public_promotion = Mock(return_value=True)
            app._claim_deferred_public_schedule_slot = Mock()
            app._claim_schedule = Mock()

            due = ScheduleEvaluation(
                "due",
                scheduled_at=datetime(2026, 9, 13, 8, 30, tzinfo=VIETNAM_TIMEZONE),
                occurrence_id="youtube-upload:shift-morning:20260913T0830",
            )
            with patch(
                "docurandom.ui.vietnam_now",
                return_value=datetime(2026, 9, 13, 8, 30, tzinfo=VIETNAM_TIMEZONE),
            ):
                app._trigger_scheduled_upload(due, shift_id="shift-morning")

            app._start_deferred_public_promotion.assert_called_once_with(
                item,
                automatic=True,
            )
            app._claim_deferred_public_schedule_slot.assert_called_once_with(
                due,
                shift_id="shift-morning",
            )
            app._claim_schedule.assert_not_called()
            self.assertEqual(app.schedule_active_shift_id, "new-production")
            self.assertEqual(app.schedule_active_stage, "workflow")

    def test_packaged_queue_is_promoted_at_each_publication_slot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomQueueEverySlot_") as temp:
            state_dir = Path(temp) / "State"
            state_dir.mkdir(parents=True)
            item = {
                "identity": "queue-1330",
                "attempt_id": "attempt-1330",
                "workspace_id": "workspace-queue",
                "profile_id": "profile-queue",
                "youtube_url": "https://youtu.be/Queue1330",
                "privacy": "UNLISTED",
                "final_privacy": "PUBLIC",
            }
            (state_dir / "deferred_public_queue.json").write_text(
                json.dumps({"version": 1, "items": [item]}),
                encoding="utf-8",
            )
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.workspace = SimpleNamespace(state_dir=state_dir)
            app.closed = False
            app.closing_after_cancel = False
            app.youtube_upload_busy = False
            app.schedule_active_shift_id = ""
            app.schedule_active_stage = ""
            app.schedule_last_event = ""
            app.schedule_shifts = [
                ScheduleShift(
                    "daily-13-30",
                    "20/09/2026 08:30",
                    "20/09/2026 13:30",
                )
            ]
            app._start_deferred_public_promotion = Mock(return_value=True)
            app._claim_deferred_public_schedule_slot = Mock()

            due = ScheduleEvaluation(
                "due",
                scheduled_at=datetime(2026, 9, 20, 13, 30, tzinfo=VIETNAM_TIMEZONE),
                occurrence_id="youtube-upload:daily-13-30:20260920T1330",
            )
            with patch(
                "docurandom.ui.vietnam_now",
                return_value=datetime(2026, 9, 20, 13, 30, tzinfo=VIETNAM_TIMEZONE),
            ):
                app._trigger_scheduled_upload(due, shift_id="daily-13-30")

            app._start_deferred_public_promotion.assert_called_once_with(
                item,
                automatic=True,
            )
            app._claim_deferred_public_schedule_slot.assert_called_once_with(
                due,
                shift_id="daily-13-30",
            )

    def test_after_1500_holds_successor_until_prior_video_is_public(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomAfterCutoff_") as temp:
            state_dir = Path(temp) / "State"
            state_dir.mkdir(parents=True)
            item = {
                "identity": "queue-before-cutoff",
                "attempt_id": "attempt-before-cutoff",
                "workspace_id": "workspace-cutoff",
                "profile_id": "profile-cutoff",
                "youtube_url": "https://youtu.be/QueueBeforeCutoff",
                "privacy": "UNLISTED",
                "final_privacy": "PUBLIC",
            }
            (state_dir / "deferred_public_queue.json").write_text(
                json.dumps({"version": 1, "items": [item]}),
                encoding="utf-8",
            )
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.workspace = SimpleNamespace(state_dir=state_dir)
            app.closed = False
            app.closing_after_cancel = False
            app.youtube_upload_busy = False
            app.content_busy = False
            app.flow_generation_busy = False
            app.pipeline_voice_busy = False
            app.thumbnail_ai_busy = False
            app.rendering = False
            app.scanning = False
            app.audio_preflight_active = False
            app.schedule_active_shift_id = "daily-13-30"
            app.schedule_active_occurrence_id = (
                "youtube-upload:daily-13-30:20260922T1330"
            )
            app.schedule_active_stage = "ready"
            app.schedule_stage_after_public = True
            app.schedule_stage_upload_deferred = False
            app.schedule_deferred_upload = False
            app.schedule_deferred_public = False
            app.schedule_armed_var = _Var(True)
            app.schedule_last_event = ""
            app.schedule_shifts = [
                ScheduleShift(
                    "daily-13-30",
                    "22/09/2026 08:30",
                    "22/09/2026 13:30",
                ),
                ScheduleShift(
                    "daily-04-30",
                    "23/09/2026 04:30",
                    "23/09/2026 08:30",
                ),
            ]
            app.youtube_selected_profile_id = "profile-cutoff"
            app.last_content_package = None
            app._effective_youtube_thumbnail = Mock(return_value=None)
            app._scheduled_video_is_ready = Mock(return_value=True)
            app._automatic_thumbnail_required_but_missing = Mock(return_value=False)
            app._claim_schedule = Mock()
            app._start_youtube_upload = Mock()
            app._start_deferred_public_promotion = Mock()
            app._schedule_log_once = Mock()
            app._save_config = Mock()
            app._claim_deferred_public_schedule_slot = Mock()

            due = ScheduleEvaluation(
                "due",
                scheduled_at=datetime(
                    2026, 9, 22, 13, 30, tzinfo=VIETNAM_TIMEZONE
                ),
                occurrence_id="youtube-upload:daily-13-30:20260922T1330",
            )
            with patch(
                "docurandom.ui.vietnam_now",
                return_value=datetime(2026, 9, 22, 15, 48, tzinfo=VIETNAM_TIMEZONE),
            ):
                app._trigger_scheduled_upload(due, shift_id="daily-13-30")

            app._start_deferred_public_promotion.assert_not_called()
            app._claim_schedule.assert_not_called()
            app._start_youtube_upload.assert_not_called()
            app._claim_deferred_public_schedule_slot.assert_called_once_with(
                due, shift_id="daily-13-30", expired=True,
            )
            self.assertEqual(
                json.loads((state_dir / "deferred_public_queue.json").read_text(encoding="utf-8"))["items"],
                [item],
            )


    def test_automatic_public_worker_refuses_to_start_at_1530(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.workspace = SimpleNamespace(state_dir=Path(tempfile.mkdtemp()))
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_upload_busy = False
        app.schedule_last_event = ""
        app.schedule_shifts = [
            ScheduleShift(
                "daily-13-30",
                "22/09/2026 08:30",
                "22/09/2026 13:30",
            )
        ]
        app._schedule_log_once = Mock()

        item = {
            "identity": "worker-cutoff",
            "attempt_id": "worker-attempt",
            "profile_id": "profile-worker",
            "youtube_url": "https://youtu.be/WorkerCutoff",
            "final_privacy": "PUBLIC",
        }
        with patch(
            "docurandom.ui.vietnam_now",
            return_value=datetime(2026, 9, 22, 15, 30, tzinfo=VIETNAM_TIMEZONE),
        ):
            started = app._start_deferred_public_promotion(item, automatic=True)

        self.assertFalse(started)
        self.assertIn("tạm dừng chuyển Công khai", app.schedule_last_event)

    @patch("docurandom.ui.resolve_workspace_upload_sources")
    @patch("docurandom.ui.resolve_donut_mcp_endpoint", return_value=Mock())
    def test_ready_checkpoint_after_1500_is_uploaded_unlisted(
        self,
        _endpoint: Mock,
        resolve_sources: Mock,
    ) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.workspace = SimpleNamespace(state_dir=Path(tempfile.mkdtemp()))
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_upload_busy = False
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_voice_busy = False
        app.thumbnail_ai_busy = False
        app.rendering = False
        app.scanning = False
        app.audio_preflight_active = False
        app.schedule_active_shift_id = "daily-13-30"
        app.schedule_active_occurrence_id = (
            "youtube-upload:daily-13-30:20260922T1330"
        )
        app.schedule_active_stage = "ready"
        app.schedule_stage_after_public = False
        app.schedule_deferred_upload = False
        app.schedule_deferred_public = False
        app.schedule_last_event = ""
        app.schedule_shifts = [
            ScheduleShift(
                "daily-13-30",
                "22/09/2026 08:30",
                "22/09/2026 13:30",
            ),
            ScheduleShift(
                "daily-04-30",
                "23/09/2026 04:30",
                "23/09/2026 08:30",
            ),
        ]
        app.youtube_selected_profile_id = "profile-ready"
        app.last_content_package = None
        app._effective_youtube_thumbnail = Mock(return_value=None)
        app._automatic_thumbnail_required_but_missing = Mock(return_value=False)
        app._claim_schedule = Mock()
        app._claim_deferred_public_schedule_slot = Mock()
        app._start_youtube_upload = Mock()
        app._schedule_log_once = Mock()

        due = ScheduleEvaluation(
            "due",
            scheduled_at=datetime(2026, 9, 22, 13, 30, tzinfo=VIETNAM_TIMEZONE),
            occurrence_id="youtube-upload:daily-13-30:20260922T1330",
        )
        with patch(
            "docurandom.ui.vietnam_now",
            return_value=datetime(2026, 9, 22, 15, 48, tzinfo=VIETNAM_TIMEZONE),
        ):
            app._trigger_scheduled_upload(due, shift_id="daily-13-30")

        app._claim_schedule.assert_not_called()
        app._claim_deferred_public_schedule_slot.assert_called_once_with(
            due, shift_id="daily-13-30", expired=True,
        )
        app._start_youtube_upload.assert_not_called()
        resolve_sources.assert_not_called()

    def test_old_missing_public_queue_releases_the_claimed_slot_for_ready_video(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomStaleQueueUI_") as temp:
            state_dir = Path(temp) / "State"
            state_dir.mkdir(parents=True)
            item = {
                "identity": "stale-ui-identity",
                "attempt_id": "attempt-stale-ui",
                "workspace_id": "workspace-stale-ui",
                "profile_id": "profile-stale-ui",
                "youtube_url": "https://youtu.be/stale-ui-video",
                "privacy": "UNLISTED",
                "final_privacy": "PUBLIC",
                "queued_at": "2026-09-20T08:00:00+07:00",
            }
            (state_dir / "deferred_public_queue.json").write_text(
                json.dumps({"version": 1, "items": [item]}),
                encoding="utf-8",
            )
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.workspace = SimpleNamespace(state_dir=state_dir)
            app.schedule_shifts = [
                ScheduleShift(
                    "daily-08-30",
                    "21/09/2026 04:30",
                    "22/09/2026 08:30",
                )
            ]
            app.schedule_selected_shift_id = ""
            app.schedule_stage_after_public = True
            app.schedule_last_event = ""
            app._deferred_public_schedule_context = (
                ScheduleEvaluation(
                    "due",
                    scheduled_at=datetime(
                        2026,
                        9,
                        21,
                        8,
                        30,
                        tzinfo=VIETNAM_TIMEZONE,
                    ),
                    occurrence_id="youtube-upload:daily-08-30:20260921T0830",
                ),
                "daily-08-30",
            )
            app.youtube_status_var = _Var("")
            app._append_log = Mock()
            app._notify_remote = Mock()
            app._refresh_youtube_controls = Mock()
            app._refresh_schedule_controls = Mock()
            app._refresh_schedule_shift_tree = Mock()
            app._save_config = Mock()
            app._trigger_scheduled_upload = Mock()

            handled = app._handle_stale_deferred_public_promotion(
                item,
                "YouTube chưa thấy đúng video ID stale-ui-video trong Nội dung trước thời hạn",
            )

            self.assertTrue(handled)
            self.assertFalse(app.schedule_stage_after_public)
            app._trigger_scheduled_upload.assert_called_once()
            retry_evaluation = app._trigger_scheduled_upload.call_args.args[0]
            self.assertEqual(
                retry_evaluation.occurrence_id,
                "youtube-upload:daily-08-30:20260921T0830",
            )
            self.assertEqual(
                app._trigger_scheduled_upload.call_args.kwargs["shift_id"],
                "daily-08-30",
            )
            self.assertEqual(
                app.schedule_shifts[0].upload_at,
                "21/09/2026 08:30",
            )
            self.assertEqual(app.schedule_shifts[0].upload_claimed_for, "")

    def test_ready_video_after_public_is_staged_unlisted_with_safety_window(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomStageWindow_") as temp:
            state_dir = Path(temp) / "State"
            state_dir.mkdir(parents=True)
            app = DocuRandomApp.__new__(DocuRandomApp)
            # This test isolates scheduling after the material preflight passes.
            app._automatic_thumbnail_required_but_missing = Mock(return_value=False)
            app.workspace = SimpleNamespace(state_dir=state_dir)
            app.schedule_armed_var = _Var(True)
            app.schedule_shifts = [
                ScheduleShift(
                    "daily-04-30",
                    "20/09/2026 04:30",
                    "20/09/2026 08:30",
                ),
                ScheduleShift(
                    "daily-08-30",
                    "20/09/2026 08:30",
                    "20/09/2026 13:30",
                ),
            ]
            app.schedule_active_shift_id = "daily-04-30"
            app.schedule_active_stage = "ready"
            app.schedule_stage_after_public = True
            app.schedule_stage_upload_deferred = False
            app.youtube_privacy_var = _Var("Công khai")
            app._scheduled_video_is_ready = Mock(return_value=True)
            app._save_config = Mock()

            prepared = app._prepare_immediate_render_upload(
                datetime(2026, 9, 20, 4, 30, tzinfo=VIETNAM_TIMEZONE)
            )

            self.assertTrue(prepared)
            self.assertEqual(app.schedule_active_stage, "upload")
            self.assertTrue(app.schedule_stage_upload_deferred)

    def test_current_ready_video_enters_unlisted_lane_before_next_public_slot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomCurrentLane_") as temp:
            app = DocuRandomApp.__new__(DocuRandomApp)
            # This test isolates scheduling after the material preflight passes.
            app._automatic_thumbnail_required_but_missing = Mock(return_value=False)
            app.closed = False
            app.closing_after_cancel = False
            app.youtube_upload_busy = False
            app.workspace = SimpleNamespace(state_dir=Path(temp))
            app.schedule_armed_var = _Var(True)
            app.schedule_shifts = [
                ScheduleShift(
                    "daily-04-30",
                    "21/09/2026 13:30",
                    "22/09/2026 04:30",
                ),
                ScheduleShift(
                    "daily-08-30",
                    "22/09/2026 04:30",
                    "22/09/2026 08:30",
                ),
            ]
            app.schedule_active_shift_id = ""
            app.schedule_active_stage = ""
            app.schedule_stage_after_public = False
            app.schedule_stage_upload_deferred = False
            app._scheduled_video_is_ready = Mock(return_value=True)
            app._save_config = Mock()
            app._start_youtube_upload = Mock()

            staged = app._stage_ready_video_for_schedule_lane(
                datetime(2026, 9, 22, 0, 50, tzinfo=VIETNAM_TIMEZONE)
            )

            self.assertTrue(staged)
            self.assertEqual(app.schedule_active_stage, "upload")
            self.assertTrue(app.schedule_stage_upload_deferred)
            app._start_youtube_upload.assert_called_once_with(
                automatic=True,
                defer_public_until_slot=True,
            )

    def test_ready_video_before_publication_stages_unlisted_immediately(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomStageGuard_") as temp:
            state_dir = Path(temp) / "State"
            state_dir.mkdir(parents=True)
            app = DocuRandomApp.__new__(DocuRandomApp)
            # This test isolates scheduling after the material preflight passes.
            app._automatic_thumbnail_required_but_missing = Mock(return_value=False)
            app.workspace = SimpleNamespace(state_dir=state_dir)
            app.schedule_armed_var = _Var(True)
            app.schedule_shifts = [
                ScheduleShift(
                    "daily-08-30",
                    "20/09/2026 04:30",
                    "20/09/2026 08:30",
                ),
                ScheduleShift(
                    "daily-13-30",
                    "20/09/2026 08:30",
                    "20/09/2026 13:30",
                ),
            ]
            app.schedule_active_shift_id = "daily-08-30"
            app.schedule_active_stage = "ready"
            app.schedule_stage_after_public = True
            app.schedule_stage_upload_deferred = False
            app.youtube_privacy_var = _Var("Công khai")
            app._scheduled_video_is_ready = Mock(return_value=True)
            app._save_config = Mock()

            prepared = app._prepare_immediate_render_upload(
                datetime(2026, 9, 20, 8, 0, tzinfo=VIETNAM_TIMEZONE)
            )

            self.assertTrue(prepared)
            self.assertEqual(app.schedule_active_stage, "upload")
            self.assertTrue(app.schedule_stage_upload_deferred)

    def test_public_promotion_releases_ready_video_to_unlisted_upload(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_upload_busy = False
        app.schedule_stage_after_public = False
        app.schedule_active_stage = ""
        app._scheduled_video_is_ready = Mock(return_value=True)
        app._prepare_immediate_render_upload = Mock(return_value=True)
        app._start_youtube_upload = Mock()
        app._start_next_continuous_workflow = Mock()
        app._save_config = Mock()

        app.schedule_active_stage = "upload"
        app._continue_after_public_promotion()

        app._prepare_immediate_render_upload.assert_called_once_with(
            allow_pending_public_queue=True,
        )
        app._start_youtube_upload.assert_called_once_with(
            automatic=True,
            defer_public_until_slot=True,
        )
        app._start_next_continuous_workflow.assert_not_called()

    def test_public_slot_holds_ready_successor_behind_remaining_fifo_items(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomPublicFifoSuccessor_") as temp:
            state_dir = Path(temp) / "State"
            state_dir.mkdir(parents=True)
            item = {
                "identity": "older-public-item",
                "attempt_id": "older-attempt",
                "workspace_id": "workspace-fifo",
                "profile_id": "profile-fifo",
                "youtube_url": "https://youtu.be/OlderItem123",
                "privacy": "UNLISTED",
                "final_privacy": "PUBLIC",
            }
            (state_dir / "deferred_public_queue.json").write_text(
                json.dumps({"version": 1, "items": [item]}),
                encoding="utf-8",
            )
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.closed = False
            app.closing_after_cancel = False
            app.youtube_upload_busy = False
            app.workspace = SimpleNamespace(state_dir=state_dir)
            app.schedule_armed_var = _Var(True)
            app.schedule_shifts = [
                ScheduleShift(
                    "daily-08-30",
                    "20/09/2026 04:30",
                    "20/09/2026 08:30",
                ),
                ScheduleShift(
                    "daily-13-30",
                    "20/09/2026 08:30",
                    "20/09/2026 13:30",
                ),
            ]
            app.schedule_active_shift_id = "daily-08-30"
            app.schedule_active_stage = "ready"
            app.schedule_stage_after_public = True
            app.schedule_stage_upload_deferred = False
            app.schedule_deferred_upload = False
            app.schedule_last_event = ""
            app._scheduled_video_is_ready = Mock(return_value=True)
            app._automatic_public_promotion_allowed = Mock(return_value=True)
            app._safe_to_stage_before_next_publication = Mock(return_value=(True, None))
            app._save_config = Mock()

            prepared = app._prepare_immediate_render_upload(
                datetime(2026, 9, 20, 8, 31, tzinfo=VIETNAM_TIMEZONE),
                allow_pending_public_queue=True,
            )

            self.assertTrue(prepared)
            self.assertEqual(app.schedule_active_stage, "ready")
            self.assertFalse(app.schedule_stage_upload_deferred)

    @patch(
        "docurandom.ui.vietnam_now",
        return_value=datetime(2026, 9, 20, 8, 30, tzinfo=VIETNAM_TIMEZONE),
    )
    @patch("docurandom.ui.resolve_workspace_upload_sources")
    @patch("docurandom.ui.resolve_donut_mcp_endpoint", return_value=Mock())
    def test_ready_video_uses_public_fallback_in_slot_when_public_queue_is_empty(
        self,
        _endpoint: Mock,
        resolve_sources: Mock,
        _now: Mock,
    ) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.youtube_upload_busy = False
        app.content_busy = False
        app.flow_generation_busy = False
        app.pipeline_voice_busy = False
        app.thumbnail_ai_busy = False
        app.rendering = False
        app.scanning = False
        app.audio_preflight_active = False
        app.schedule_active_shift_id = "daily-08-30"
        app.schedule_active_occurrence_id = "youtube-upload:daily-08-30:old"
        app.schedule_active_stage = "ready"
        app.schedule_stage_after_public = True
        app.schedule_stage_upload_deferred = False
        app.schedule_deferred_upload = False
        app.schedule_deferred_public = False
        app.schedule_last_event = ""
        app.schedule_shifts = [
            ScheduleShift(
                "daily-08-30",
                "20/09/2026 04:30",
                "20/09/2026 08:30",
            ),
            ScheduleShift(
                "daily-13-30",
                "20/09/2026 08:30",
                "20/09/2026 13:30",
            ),
        ]
        app.schedule_armed_var = _Var(True)
        app.schedule_upload_enabled_var = _Var(True)
        app.workspace = SimpleNamespace(state_dir=Path(tempfile.mkdtemp()))
        app.youtube_selected_profile_id = "profile-1"
        app.last_content_package = None
        app._effective_youtube_thumbnail = Mock(return_value=None)
        app._scheduled_video_is_ready = Mock(return_value=True)
        app._automatic_thumbnail_required_but_missing = Mock(return_value=False)
        app._claim_schedule = Mock()
        app._start_youtube_upload = Mock()
        app._save_config = Mock()
        app._schedule_log_once = Mock()

        due = ScheduleEvaluation(
            "due",
            scheduled_at=datetime(2026, 9, 20, 8, 30, tzinfo=VIETNAM_TIMEZONE),
            occurrence_id="youtube-upload:daily-08-30:20260920T0830",
        )
        app._trigger_scheduled_upload(due, shift_id="daily-08-30")

        app._claim_schedule.assert_called_once()
        app._start_youtube_upload.assert_called_once_with(
            automatic=True,
            defer_public_until_slot=False,
            publish_if_no_older_video=True,
        )

    def test_deferred_upload_holds_public_only_when_evening_slot_is_late(
        self,
    ) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.schedule_armed_var = _Var(True)
        app.schedule_shifts = [
            ScheduleShift(
                "shift-evening",
                "13/09/2026 13:30",
                "13/09/2026 19:00",
            )
        ]
        app.schedule_active_shift_id = "shift-evening"
        app.schedule_active_stage = "deferred-upload"
        app.youtube_privacy_var = _Var("Công khai")

        self.assertTrue(
            app._should_hold_public_until_morning(
                datetime(2026, 9, 13, 8, 30, tzinfo=VIETNAM_TIMEZONE)
            )
        )
        self.assertFalse(
            app._should_hold_public_until_morning(
                datetime(2026, 9, 13, 19, 1, tzinfo=VIETNAM_TIMEZONE)
            )
        )
        self.assertTrue(
            app._should_hold_public_until_morning(
                datetime(2026, 9, 13, 14, 59, tzinfo=VIETNAM_TIMEZONE)
            )
        )
        self.assertTrue(
            app._should_hold_public_until_morning(
                datetime(2026, 9, 13, 15, 1, tzinfo=VIETNAM_TIMEZONE)
            )
        )

    def test_render_completion_after_15_keeps_morning_promotion_slot(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        # This test isolates scheduling after the material preflight passes.
        app._automatic_thumbnail_required_but_missing = Mock(return_value=False)
        app.schedule_armed_var = _Var(True)
        app.schedule_shifts = [
            ScheduleShift(
                "shift-evening",
                "20/09/2026 13:30",
                "20/09/2026 15:00",
            ),
            ScheduleShift(
                "shift-morning",
                "21/09/2026 04:30",
                "21/09/2026 08:30",
            ),
        ]
        app.schedule_active_shift_id = "shift-evening"
        app.schedule_active_stage = "render"
        app.schedule_deferred_upload = False
        app.schedule_last_event = ""
        app._defer_scheduled_upload_to_next_slot = Mock()
        app._save_config = Mock()

        prepared = app._prepare_immediate_render_upload(
            datetime(2026, 9, 20, 23, 0, tzinfo=VIETNAM_TIMEZONE)
        )

        self.assertTrue(prepared)
        app._defer_scheduled_upload_to_next_slot.assert_not_called()
        self.assertEqual(app.schedule_active_stage, "upload")
        self.assertFalse(app.schedule_deferred_upload)
        self.assertIn("Không công khai", app.schedule_last_event)

    def test_render_completion_before_15_stages_unlisted_without_moving_public_clock(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        # This test isolates scheduling after the material preflight passes.
        app._automatic_thumbnail_required_but_missing = Mock(return_value=False)
        app.schedule_armed_var = _Var(True)
        app.schedule_shifts = [
            ScheduleShift(
                "shift-afternoon",
                "20/09/2026 10:00",
                "20/09/2026 15:00",
            )
        ]
        app.schedule_active_shift_id = "shift-afternoon"
        app.schedule_active_stage = "render"
        app.schedule_deferred_upload = False
        app.schedule_last_event = ""
        app._claim_schedule = Mock()
        app._save_config = Mock()

        prepared = app._prepare_immediate_render_upload(
            datetime(2026, 9, 20, 12, 0, tzinfo=VIETNAM_TIMEZONE)
        )

        self.assertTrue(prepared)
        app._claim_schedule.assert_not_called()
        self.assertEqual(app.schedule_active_stage, "upload")
        self.assertTrue(app.schedule_stage_upload_deferred)
        self.assertIn("Không công khai", app.schedule_last_event)

    def test_ready_checkpoint_waits_for_the_upload_clock_after_restart(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.schedule_armed_var = _Var(True)
        app.schedule_shifts = [
            ScheduleShift(
                "shift-ready",
                "20/09/2026 13:30",
                "21/09/2026 04:30",
            )
        ]
        app.schedule_active_shift_id = "shift-ready"
        app.schedule_active_stage = "ready"
        app.youtube_auto_upload_var = _Var(True)
        app.youtube_upload_busy = False
        app._scheduled_video_is_ready = Mock(return_value=True)
        app._prepare_immediate_render_upload = Mock(return_value=True)
        app._start_youtube_upload = Mock()

        # This assertion exercises the due Public-clock barrier before the
        # daily cutoff; it must not change with the actual time of the test run.
        with patch(
            "docurandom.ui.vietnam_now",
            return_value=datetime(2026, 9, 21, 4, 30, tzinfo=VIETNAM_TIMEZONE),
        ):
            app._resume_active_scheduled_pipeline_if_idle()

        app._prepare_immediate_render_upload.assert_not_called()
        app._start_youtube_upload.assert_not_called()

    def test_deferred_ready_checkpoint_is_not_advanced_twice_at_next_slot(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        # This test isolates scheduling after the material preflight passes.
        app._automatic_thumbnail_required_but_missing = Mock(return_value=False)
        app.schedule_armed_var = _Var(True)
        app.schedule_shifts = [
            ScheduleShift(
                "shift-carried",
                "20/09/2026 13:30",
                "21/09/2026 04:30",
            )
        ]
        app.schedule_active_shift_id = "shift-carried"
        app.schedule_active_stage = "deferred-upload"
        app.schedule_deferred_upload = True
        app.schedule_last_event = ""
        app._defer_scheduled_upload_to_next_slot = Mock()
        app._save_config = Mock()

        prepared = app._prepare_immediate_render_upload(
            datetime(2026, 9, 21, 4, 30, tzinfo=VIETNAM_TIMEZONE)
        )

        self.assertTrue(prepared)
        app._defer_scheduled_upload_to_next_slot.assert_not_called()
        self.assertEqual(app.schedule_active_stage, "ready")
        self.assertFalse(app.schedule_stage_upload_deferred)

    def test_smart_voice_style_prefers_saved_then_kigashima_normal(self) -> None:
        styles = [
            VoicevoxStyle("Another", "a", "Normal", 1),
            VoicevoxStyle("麒ヶ島宗麟", "k", "ノーマル", 7),
            VoicevoxStyle("麒ヶ島宗麟", "k", "怒り", 8),
        ]

        saved = DocuRandomApp._select_smart_voicevox_style(styles, 8)
        first_run = DocuRandomApp._select_smart_voicevox_style(styles, -1)

        self.assertEqual(saved.style_id, 8)
        self.assertEqual(first_run.style_id, 7)

    def test_telegram_suppresses_image_prompts_but_keeps_status(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.workspace = Mock(name="workspace")
        app.workspace.name = "Kênh Nhật"
        app.notification_sink = Mock()

        app._notify_remote("prompt", "Flow 1/30\nA very long image prompt")
        app._notify_remote("status", "Workflow đã hoàn tất")

        app.notification_sink.assert_called_once_with(
            "status",
            "[Kênh Nhật] Workflow đã hoàn tất",
        )

    def test_voicevox_precheck_ready_hides_start_button(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.voicevox_engine_state = "ready"
        app.voicevox_busy = False
        app.closed = False
        app.closing_after_cancel = False
        app.voicevox_precheck_label = Mock()
        app.voicevox_check_button = Mock()
        app.voicevox_start_button = Mock()
        app.voicevox_start_button.winfo_manager.return_value = "pack"

        app._refresh_voicevox_precheck_ui()

        app.voicevox_precheck_label.configure.assert_called_once_with(
            style="EngineReady.TLabel"
        )
        app.voicevox_check_button.configure.assert_called_once_with(state="normal")
        app.voicevox_start_button.pack_forget.assert_called_once_with()

    def test_voicevox_precheck_stopped_shows_start_button(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.voicevox_engine_state = "stopped"
        app.voicevox_busy = False
        app.closed = False
        app.closing_after_cancel = False
        app.voicevox_precheck_label = Mock()
        app.voicevox_check_button = Mock()
        app.voicevox_start_button = Mock()
        app.voicevox_start_button.winfo_manager.return_value = ""

        app._refresh_voicevox_precheck_ui()

        app.voicevox_precheck_label.configure.assert_called_once_with(
            style="EngineWaiting.TLabel"
        )
        app.voicevox_start_button.pack.assert_called_once()
        app.voicevox_start_button.configure.assert_called_once_with(
            state="normal",
            text="Khởi động VOICEVOX",
        )

    def test_smart_voice_duration_hint_grows_with_script_and_pauses(self) -> None:
        short = DocuRandomApp._estimate_voice_duration("短い文です。", 0.1)
        long = DocuRandomApp._estimate_voice_duration(
            "これは長いニュース原稿です。" * 20 + "\n二行目です。",
            1.0,
        )
        self.assertGreaterEqual(short, 10.0)
        self.assertGreater(long, short)

    def test_smart_voice_request_recovers_blank_editor_from_exact_package(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomVoicePackage_") as temp:
            package_path = Path(temp) / "video.txt"
            package_path.write_text(
                """TIÊU ĐỀ

title

THOẠI VOICEVOX

A: 一行目です。
A: 二行目です。

PROMPT HÌNH ẢNH

Image 1. prompt
""",
                encoding="utf-8-sig",
            )
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.voicevox_script_text = _TextValue("")
            app.last_content_package = package_path
            app.content_last_result = None
            app.content_runner = None
            app.voicevox_styles = {}
            app.voicevox_style_var = _Var("Normal")
            app.voicevox_preferred_style_id = 53
            app.voicevox_endpoint_var = _Var("http://127.0.0.1:50021")
            app.voicevox_engine_path_var = _Var("")
            app.voicevox_use_gpu_var = _Var(False)
            app.output_name_var = _Var("video")
            app._collect_voicevox_settings = Mock(
                return_value=VoicevoxSynthesisSettings()
            )
            app._effective_voicevox_backup_dir = Mock(return_value=None)
            app._append_log = Mock()

            request = app._collect_smart_voice_request()

            self.assertIsNotNone(request)
            assert request is not None
            self.assertEqual(request.script, "一行目です。\n二行目です。")
            self.assertEqual(app.voicevox_script_text.value, request.script)
            app._append_log.assert_called_once()

    def test_smart_job_uses_generated_voice_duration_for_render(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomSmartVoice_") as temp:
            root = Path(temp)
            audio = root / "voice.wav"
            audio.write_bytes(b"RIFF-generated")
            subtitle = root / "voice.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n一行目。\n", encoding="utf-8")
            metadata = root / "voice.voicevox.json"
            metadata.write_text("{}", encoding="utf-8")
            voice = VoicevoxSynthesisResult(
                audio_path=audio,
                subtitle_path=subtitle,
                metadata_path=metadata,
                line_files_dir=None,
                line_count=2,
                duration_seconds=18.75,
                engine_version="test",
                style=VoicevoxStyle("麒ヶ島宗麟", "uuid", "ノーマル", 7),
            )
            request = SmartVoiceRequest(
                batch_key="batch",
                script="一行目。\n二行目。",
                settings=VoicevoxSynthesisSettings(),
                preferred_style_id=7,
                endpoint="http://127.0.0.1:50021",
                engine_path="",
                use_gpu=True,
            )
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.audio_cache_lock = threading.RLock()
            app.audio_duration_cache = {}
            app._create_smart_voice_for_job = Mock(return_value=voice)
            app._render_one_job = Mock(return_value=("result", "style", "id"))
            settings = RenderSettings(source_dir=root, output_dir=root)

            value = app._run_smart_render_job(
                "job-1",
                "label",
                tuple(),
                settings,
                threading.Event(),
                request,
            )

            self.assertEqual(value, ("result", "style", "id"))
            rendered_settings = app._render_one_job.call_args.args[3]
            self.assertEqual(rendered_settings.audio_path, audio)
            self.assertEqual(rendered_settings.audio_duration, 18.75)
            self.assertTrue(rendered_settings.audio_identity)

    def test_pipeline_voice_prefetch_starts_without_waiting_for_flow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomVoicePrefetch_") as temp:
            root = Path(temp)
            audio = root / "project.wav"
            subtitle = root / "project.srt"
            metadata = root / "project.voicevox.json"
            audio.write_bytes(b"RIFF-generated")
            subtitle.write_text("", encoding="utf-8")
            metadata.write_text("{}", encoding="utf-8")
            voice = VoicevoxSynthesisResult(
                audio_path=audio,
                subtitle_path=subtitle,
                metadata_path=metadata,
                line_files_dir=None,
                line_count=1,
                duration_seconds=12.5,
                engine_version="test",
                style=VoicevoxStyle("麒ヶ島宗麟", "uuid", "ノーマル", 53),
            )
            request = SmartVoiceRequest(
                batch_key="prefetch",
                script="一行目です。",
                settings=VoicevoxSynthesisSettings(),
                preferred_style_id=53,
                endpoint="http://127.0.0.1:50021",
                engine_path="",
                use_gpu=False,
                backup_dir=root,
                output_stem="project",
            )
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.closed = False
            app.closing_after_cancel = False
            app.pipeline_voice_busy = False
            app.pipeline_voice_token = 0
            app.pipeline_voice_cancel_event = threading.Event()
            app.pipeline_voice_result = None
            app.content_snapshot = {
                "output_dir": root,
                "project_name": "project",
            }
            app.workspace = None
            app.output_var = _Var(str(root))
            app.output_name_var = _Var("project")
            app.source_var = _Var(str(root))
            app.voicevox_progress_var = _Var(0.0)
            app.voicevox_percent_var = _Var("0%")
            app.voicevox_status_var = _Var("")
            app.events = Queue()
            app._collect_smart_voice_request = Mock(return_value=request)
            app._record_pipeline_stage = Mock()
            app._append_content_chat = Mock()
            app._refresh_content_controls = Mock()
            app._create_smart_voice_for_job = Mock(return_value=voice)

            app._start_pipeline_voice_prefetch()
            event = app.events.get(timeout=2.0)

            self.assertTrue(app.pipeline_voice_busy)
            self.assertEqual(event, ("pipeline_voice_done", 1, voice))
            call = app._create_smart_voice_for_job.call_args
            self.assertEqual(call.args[4], app.pipeline_voice_cancel_event)
            self.assertTrue(call.kwargs["progress_callback"])

    def test_pipeline_render_waits_for_voice_checkpoint_then_joins(self) -> None:
        voice = Mock()
        voice.duration_seconds = 42.0
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.flow_render_pending = True
        app.closed = False
        app.closing_after_cancel = False
        app.pipeline_voice_busy = True
        app.pipeline_voice_result = None
        app.media = [Mock()]
        app.content_status_var = _Var("")
        app.root = Mock()
        app.main_notebook = Mock()
        app.render_tab = Mock()
        app.rendering = False
        app.audio_preflight_active = False

        def start_render() -> None:
            app.rendering = True

        app.start_render = Mock(side_effect=start_render)
        app._save_config = Mock()

        app._start_pipeline_render_if_ready()

        self.assertTrue(app.flow_render_pending)
        self.assertIn("chờ VOICEVOX", app.content_status_var.get())
        app.root.after_idle.assert_not_called()

        app.pipeline_voice_busy = False
        app.pipeline_voice_result = voice
        app._start_pipeline_render_if_ready()

        self.assertFalse(app.flow_render_pending)
        self.assertIn("0:42", app.content_status_var.get())
        app.main_notebook.select.assert_called_once_with(app.render_tab)
        launch_render = app.root.after_idle.call_args.args[0]
        launch_render()
        app.start_render.assert_called_once_with()
        self.assertFalse(app.flow_render_pending)

    def test_start_render_ignores_duplicate_workspace_render(self) -> None:
        for rendering, states in (
            (True, {}),
            (False, {"job-1": "running"}),
            (False, {"job-1": "queued", "job-2": "waiting"}),
        ):
            with self.subTest(rendering=rendering, states=states):
                app = DocuRandomApp.__new__(DocuRandomApp)
                app.audio_preflight_active = False
                app.audio_preflight_resume_token = None
                app.rendering = rendering
                app.job_states = states
                app._append_log = Mock()
                app.status_var = _Var("")

                app.start_render()

                app._append_log.assert_called_once()
                self.assertIn("render trùng", app._append_log.call_args.args[0])
                self.assertIn("đã bỏ qua", app.status_var.get())

    def test_render_join_does_not_consume_stale_media_during_new_pipeline(self) -> None:
        for busy_flag in ("content_busy", "flow_generation_busy", "scanning"):
            with self.subTest(busy_flag=busy_flag):
                app = DocuRandomApp.__new__(DocuRandomApp)
                app.flow_render_pending = True
                app.closed = False
                app.closing_after_cancel = False
                app.pipeline_voice_busy = False
                app.pipeline_voice_result = Mock(duration_seconds=42.0)
                app.media = [Mock()]
                setattr(app, busy_flag, True)
                app.root = Mock()

                app._start_pipeline_render_if_ready()

                self.assertTrue(app.flow_render_pending)
                app.root.after_idle.assert_not_called()

    def test_flow_monitor_logs_connection_changes_but_not_identical_polls(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.flow_check_token = 1
        app.flow_status = None
        app.content_precheck_pending = False
        app._append_log = Mock()
        app._schedule_flow_monitor = Mock()

        def apply(status):
            app.flow_status = status

        app._apply_flow_status_ui = Mock(side_effect=apply)
        disconnected = FlowAgentStatus(
            Path("edge.exe"), Path("extension"), True, True, False, False
        )
        ready = FlowAgentStatus(
            Path("edge.exe"), Path("extension"), True, True, True, False,
            browser_open=True, flow_tab_count=1, ui_image_capable=True,
            flow_client_count_with_tabs=1,
        )
        for status in (disconnected, disconnected, ready, ready, disconnected):
            app._handle_flow_checked(1, "monitor", None, 0, status)

        self.assertEqual(app._append_log.call_count, 3)
        connected_log = app._append_log.call_args_list[1].args[0]
        self.assertIn("ready=True", connected_log)
        self.assertIn("flow_tabs=1", connected_log)
        self.assertIn("across 1 connected client(s) with Flow tabs", connected_log)
        self.assertIn("token_present=False", connected_log)

    def test_flow_monitor_pauses_while_image_generation_owns_backend(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.flow_generation_busy = True
        app.flow_checking = False
        app._schedule_flow_monitor = Mock()
        app.flow_manager = SimpleNamespace(check=Mock())

        app._check_flow_async(reason="monitor")

        app._schedule_flow_monitor.assert_called_once_with(delay_ms=10_000)
        app.flow_manager.check.assert_not_called()

    def test_selector_mousewheel_scrolls_page_and_stops_widget_default(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.settings_canvas = Mock()

        result = app._scroll_settings_from_selector(
            SimpleNamespace(delta=-120, num=0)
        )

        self.assertEqual(result, "break")
        app.settings_canvas.yview_scroll.assert_called_once_with(1, "units")

    def test_editor_mousewheel_scrolls_whole_tab(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.editor_canvas = Mock()

        result = app._scroll_editor_panel(
            SimpleNamespace(delta=-240, num=0)
        )

        self.assertEqual(result, "break")
        app.editor_canvas.yview_scroll.assert_called_once_with(2, "units")

    def test_editor_canvas_tracks_viewport_width_without_resize_storm(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.editor_canvas = Mock()
        app.editor_content_window = "editor-window"
        app.editor_layout_after_id = None
        app.editor_scroll_after_id = "scroll-pending"
        app.editor_pending_width = 0
        app.editor_applied_width = 0
        app.root = Mock()
        app.root.after.return_value = "layout-pending"

        app._schedule_editor_canvas_layout(SimpleNamespace(width=777))

        self.assertEqual(app.editor_pending_width, 777)
        callback = app.root.after.call_args.args[1]
        callback()
        app.editor_canvas.itemconfigure.assert_called_once_with(
            "editor-window",
            width=777,
        )
        self.assertIsNone(app.editor_layout_after_id)

    def test_selector_mousewheel_supports_directions_and_linux_buttons(self) -> None:
        cases = (
            (SimpleNamespace(delta=120, num=0), -1),
            (SimpleNamespace(delta=-240, num=0), 2),
            (SimpleNamespace(delta=0, num=4), -1),
            (SimpleNamespace(delta=0, num=5), 1),
            (SimpleNamespace(delta=0, num=0), 0),
            (SimpleNamespace(delta=-120, num="??"), 1),
            (SimpleNamespace(delta="??", num="??"), 0),
        )
        for event, expected in cases:
            with self.subTest(event=event):
                self.assertEqual(
                    DocuRandomApp._mousewheel_scroll_units(event), expected
                )

    def test_audio_identity_changes_when_same_path_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomAudioIdentity_") as temp:
            path = Path(temp) / "audio.wav"
            path.write_bytes(b"first")
            first = DocuRandomApp._audio_identity(path)
            path.write_bytes(b"replacement-content")
            second = DocuRandomApp._audio_identity(path)
            self.assertNotEqual(first, second)

    def test_gpu_fallback_rejects_disk_error_but_accepts_encoder_error(self) -> None:
        profile = self._nvenc_profile()
        self.assertFalse(
            DocuRandomApp._is_hardware_encoder_failure(
                RuntimeError(
                    "FFmpeg thất bại ở bước timeline. No space left on device "
                    "while writing h264_nvenc output"
                ),
                profile,
            )
        )
        self.assertTrue(
            DocuRandomApp._is_hardware_encoder_failure(
                RuntimeError(
                    "FFmpeg thất bại ở bước media 1. Cannot load nvcuda.dll"
                ),
                profile,
            )
        )

    def test_collect_settings_never_probes_audio_synchronously(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUiCollect_") as temp:
            root = Path(temp)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            audio = root / "audio.wav"
            audio.write_bytes(b"not-probed-by-this-test")

            app = DocuRandomApp.__new__(DocuRandomApp)
            app.source_var = _Var(str(source))
            app.output_var = _Var(str(output))
            app.output_name_var = _Var("Video_1.mp4")
            app.audio_var = _Var(str(audio))
            app.resolution_var = _Var(next(iter(RESOLUTION_OPTIONS)))
            app.fps_var = _Var("24")
            app.duration_min_var = _Var(2.0)
            app.duration_max_var = _Var(3.0)
            app.intensity_var = _Var(next(iter(INTENSITY_OPTIONS)))
            app.framing_var = _Var(next(iter(FRAMING_OPTIONS)))
            app.transition_pack_var = _Var(next(iter(TRANSITION_PACK_OPTIONS)))
            app.effect_density_var = _Var(next(iter(EFFECT_DENSITY_OPTIONS)))
            app.video_mode_var = _Var(next(iter(VIDEO_MODE_OPTIONS)))
            app.audio_volume_var = _Var(1.0)
            app.audio_fade_var = _Var(0.5)
            app.variant_count_var = _Var("1")
            manual_label = next(
                label
                for label, value in PERFORMANCE_MODE_OPTIONS.items()
                if value == "manual"
            )
            app.performance_mode_var = _Var(manual_label)
            app.parallel_jobs_var = _Var("1")
            app.recursive_var = _Var(False)
            app.randomize_order_var = _Var(True)
            app.remove_watermarks_var = _Var(False)
            app.rights_confirm_var = _Var(True)
            app.editorial_intent_var = _Var("Kiểm thử")
            app.audio_duration = 0.0
            app.audio_duration_identity = ""
            app.audio_duration_cache = {}
            app.audio_cache_lock = threading.RLock()
            app.disk_lock = threading.RLock()
            app.job_disk_reservations = {}
            app.media = [object()]
            app.scanned_source = str(source.resolve())

            with (
                patch(
                    "docurandom.ui.probe_audio_duration",
                    side_effect=AssertionError("Tk thread must not probe audio"),
                ) as probe,
                patch(
                    "docurandom.ui.shutil.disk_usage",
                    return_value=SimpleNamespace(free=10**15),
                ),
            ):
                settings = app._collect_settings()

            probe.assert_not_called()
            self.assertEqual(settings.audio_duration, 0.0)
            self.assertTrue(settings.audio_identity)
            self.assertEqual(settings.output_name, "Video_1")

    def test_audio_preflight_resume_is_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUiResume_") as temp:
            audio = Path(temp) / "audio.wav"
            audio.write_bytes(b"audio")
            identity = DocuRandomApp._audio_identity(audio)
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.closed = False
            app.closing_after_cancel = False
            app.cancelling_all = False
            app.audio_preflight_resume_token = 7
            app.audio_preflight_identity = identity
            app.audio_var = _Var(str(audio))
            app.scanning = False
            app.start_render = Mock()
            app._refresh_global_state = Mock()

            app._resume_render_after_audio_preflight(7, identity)
            app._resume_render_after_audio_preflight(7, identity)

            app.start_render.assert_called_once_with()
            self.assertIsNone(app.audio_preflight_resume_token)

    def test_audio_change_cancels_pending_render_preflight(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUiCancel_") as temp:
            old_audio = Path(temp) / "old.wav"
            new_audio = Path(temp) / "new.wav"
            old_audio.write_bytes(b"old")
            new_audio.write_bytes(b"new-audio")
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.audio_preflight_active = True
            app.audio_preflight_resume_token = None
            app.audio_preflight_identity = DocuRandomApp._audio_identity(old_audio)
            app.audio_preflight_cancel_event = threading.Event()
            app.audio_preflight_token = 3
            app.audio_var = _Var(str(new_audio))
            app.rights_confirm_var = _Var(True)
            app.closed = False
            app._refresh_global_state = Mock()
            app._probe_selected_audio_async = Mock()

            app._audio_path_changed()

            self.assertTrue(app.audio_preflight_cancel_event.is_set())
            self.assertFalse(app.audio_preflight_active)
            self.assertEqual(app.audio_preflight_token, 4)
            app._probe_selected_audio_async.assert_called_once_with()

    def test_missing_restored_audio_is_pending_without_error_log(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUiPendingAudio_") as temp:
            missing = Path(temp) / "Voice" / "Video 1.wav"
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.audio_var = _Var(str(missing))
            app.audio_probe_token = 0
            app.audio_duration = 0.0
            app.audio_duration_path = ""
            app.audio_duration_identity = ""
            app.audio_summary_var = _Var("")
            app.events = Queue()
            app.lifecycle_cancel_event = threading.Event()
            app._append_exception_log = Mock()
            app._clear_audio = Mock()

            app._probe_selected_audio_async()
            event = app.events.get(timeout=2.0)

            self.assertEqual(event[0], "audio_probe_pending")
            self.assertEqual(event[1], 1)
            self.assertEqual(Path(event[2]), missing.resolve(strict=False))
            app._append_exception_log.assert_not_called()

    def test_late_progress_cannot_overwrite_terminal_job_card(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.job_states = {"job-1": "completed"}
        app.progress_aggregator = Mock()
        app._update_job_card = Mock()
        app._set_progress = Mock()

        app._apply_job_progress("job-1", "render", 0.5, "Đến muộn")

        app.progress_aggregator.update.assert_not_called()
        app._update_job_card.assert_not_called()
        app._set_progress.assert_not_called()

    def test_continue_failed_render_requeues_same_job_with_checkpoint(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        settings = RenderSettings(
            source_dir=Path("source"),
            output_dir=Path("output"),
            width=1280,
            height=720,
            fps=30,
            duration_max=5.0,
        )
        media = (Mock(), Mock())
        app.closed = False
        app.closing_after_cancel = False
        app.cancelling_all = False
        app.rendering = False
        app.job_states = {"job-1": "error"}
        app.job_requests = {"job-1": ("Video 1", media, settings, None)}
        app.render_pool = Mock()
        app.render_pool.get_handle.return_value = None
        app.disk_lock = threading.RLock()
        app.job_disk_reservations = {}
        app.progress_aggregator = Mock()
        app.job_widgets = {
            "job-1": {
                "stage_var": _Var("Lỗi"),
                "stage_progress_var": _Var(39.0),
                "stage_percent_label": Mock(),
            }
        }
        app._estimate_job_bytes = Mock(return_value=1234)
        app._update_job_card = Mock()
        app._submit_render_job = Mock(return_value=Mock())
        app._append_log = Mock()
        app._refresh_global_state = Mock()

        app.continue_job("job-1")

        self.assertEqual(app.job_states["job-1"], "queued")
        app.progress_aggregator.reset.assert_called_once_with("job-1")
        app._submit_render_job.assert_called_once_with(
            "job-1",
            "Video 1",
            media,
            settings,
            None,
            resume=True,
        )
        self.assertEqual(app.job_disk_reservations["job-1"], (settings.output_dir, 1234))
        self.assertTrue(app.rendering)

    def test_continue_failed_render_waits_for_another_active_job(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.cancelling_all = False
        app.rendering = True
        app.job_states = {"job-1": "error", "job-2": "running"}
        app.job_requests = {}
        app._append_log = Mock()
        app._job_label = Mock(return_value="Video 1")
        app._update_job_card = Mock()
        app._refresh_global_state = Mock()
        app._submit_render_job = Mock()

        app.continue_job("job-1")

        self.assertEqual(app.job_states["job-1"], "error")
        app._submit_render_job.assert_not_called()
        app._update_job_card.assert_called_once()
        self.assertIn("chưa tiếp tục", app._update_job_card.call_args.kwargs["status"])
        app._refresh_global_state.assert_called_once_with()

    def test_restore_defaults_preserves_sources_media_and_running_jobs(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.root = object()
        app.closed = False
        app.closing_after_cancel = False
        app.rendering = True
        app.source_var = _Var("source-kept")
        app.output_var = _Var("output-kept")
        app.audio_var = _Var("audio-kept")
        app.voicevox_backup_dir_var = _Var("voice-backup-kept")
        app.resolution_var = _Var(next(reversed(RESOLUTION_OPTIONS)))
        app.fps_var = _Var("60")
        app.duration_min_var = _Var(9.0)
        app.duration_max_var = _Var(12.0)
        app.intensity_var = _Var(next(reversed(INTENSITY_OPTIONS)))
        app.framing_var = _Var(next(reversed(FRAMING_OPTIONS)))
        app.transition_pack_var = _Var(next(iter(TRANSITION_PACK_OPTIONS)))
        app.effect_density_var = _Var(next(reversed(EFFECT_DENSITY_OPTIONS)))
        app.video_mode_var = _Var(next(reversed(VIDEO_MODE_OPTIONS)))
        app.audio_volume_var = _Var(1.75)
        app.audio_fade_var = _Var(4.0)
        app.variant_count_var = _Var("9")
        app.performance_mode_var = _Var("Thủ công")
        app.parallel_jobs_var = _Var("4")
        app.randomize_order_var = _Var(False)
        app.remove_watermarks_var = _Var(True)
        app.voicevox_preset_var = _Var("Mở bài · gây chú ý")
        app.voicevox_preset_description_var = _Var("custom")
        app.voicevox_speed_var = _Var(1.8)
        app.voicevox_pitch_var = _Var(0.12)
        app.voicevox_intonation_var = _Var(1.8)
        app.voicevox_volume_var = _Var(1.5)
        app.voicevox_pre_silence_var = _Var(0.8)
        app.voicevox_post_silence_var = _Var(0.9)
        app.voicevox_pause_scale_var = _Var(1.7)
        app.voicevox_line_pause_var = _Var(2.0)
        app.voicevox_sample_rate_var = _Var("48000")
        app.voicevox_stereo_var = _Var(True)
        app.voicevox_save_lines_var = _Var(True)
        app.voicevox_use_gpu_var = _Var(False)
        app.voicevox_styles = {}
        app.recursive_var = _Var(True)
        app.editorial_intent_var = _Var("ghi chú phải giữ")
        app.rights_confirm_var = _Var(True)
        app.status_var = _Var("2 đang chạy")
        media = [object(), object()]
        job_states = {"running-job": "running"}
        app.media = media
        app.job_states = job_states
        app._performance_mode_changed = Mock()
        app._update_estimate = Mock()
        app._save_config = Mock()
        app._append_log = Mock()

        with patch("docurandom.ui.messagebox.askyesno", return_value=True) as confirm:
            app._restore_render_defaults()

        confirm.assert_called_once()
        self.assertEqual(app.source_var.get(), "source-kept")
        self.assertEqual(app.output_var.get(), "output-kept")
        self.assertEqual(app.audio_var.get(), "audio-kept")
        self.assertEqual(app.voicevox_backup_dir_var.get(), "voice-backup-kept")
        self.assertIs(app.media, media)
        self.assertIs(app.job_states, job_states)
        self.assertTrue(app.recursive_var.get())
        self.assertEqual(app.editorial_intent_var.get(), "ghi chú phải giữ")
        self.assertTrue(app.rights_confirm_var.get())
        self.assertEqual(app.resolution_var.get(), DEFAULT_RESOLUTION)
        self.assertEqual(app.fps_var.get(), DEFAULT_FPS)
        self.assertEqual(app.duration_min_var.get(), DEFAULT_DURATION_MIN)
        self.assertEqual(app.duration_max_var.get(), DEFAULT_DURATION_MAX)
        self.assertEqual(app.intensity_var.get(), DEFAULT_INTENSITY)
        self.assertEqual(app.framing_var.get(), DEFAULT_FRAMING)
        self.assertEqual(app.transition_pack_var.get(), DEFAULT_TRANSITION_PACK)
        self.assertEqual(app.effect_density_var.get(), DEFAULT_EFFECT_DENSITY)
        self.assertEqual(app.video_mode_var.get(), DEFAULT_VIDEO_MODE)
        self.assertEqual(app.audio_volume_var.get(), DEFAULT_AUDIO_VOLUME)
        self.assertEqual(app.audio_fade_var.get(), DEFAULT_AUDIO_FADE)
        self.assertEqual(app.variant_count_var.get(), DEFAULT_VARIANT_COUNT)
        self.assertEqual(app.performance_mode_var.get(), DEFAULT_PERFORMANCE_MODE)
        self.assertEqual(app.parallel_jobs_var.get(), DEFAULT_PARALLEL_JOBS)
        self.assertEqual(app.randomize_order_var.get(), DEFAULT_RANDOMIZE_ORDER)
        self.assertEqual(
            app.remove_watermarks_var.get(), DEFAULT_REMOVE_GEMINI_WATERMARKS
        )
        self.assertEqual(
            app.voicevox_preset_var.get(), CUSTOM_VOICEVOX_NARRATION_PRESET
        )
        self.assertEqual(
            app.voicevox_preset_description_var.get(),
            CUSTOM_VOICEVOX_PRESET_DESCRIPTION,
        )
        self.assertEqual(app.voicevox_speed_var.get(), DEFAULT_VOICEVOX_SPEED)
        self.assertEqual(app.voicevox_pitch_var.get(), DEFAULT_VOICEVOX_PITCH)
        self.assertEqual(
            app.voicevox_intonation_var.get(), DEFAULT_VOICEVOX_INTONATION
        )
        self.assertEqual(app.voicevox_volume_var.get(), DEFAULT_VOICEVOX_VOLUME)
        self.assertEqual(
            app.voicevox_pre_silence_var.get(), DEFAULT_VOICEVOX_PRE_SILENCE
        )
        self.assertEqual(
            app.voicevox_post_silence_var.get(), DEFAULT_VOICEVOX_POST_SILENCE
        )
        self.assertEqual(
            app.voicevox_pause_scale_var.get(), DEFAULT_VOICEVOX_PAUSE_SCALE
        )
        self.assertEqual(
            app.voicevox_line_pause_var.get(), DEFAULT_VOICEVOX_LINE_PAUSE
        )
        self.assertEqual(
            app.voicevox_sample_rate_var.get(), DEFAULT_VOICEVOX_SAMPLE_RATE
        )
        self.assertEqual(app.voicevox_stereo_var.get(), DEFAULT_VOICEVOX_STEREO)
        self.assertEqual(
            app.voicevox_save_lines_var.get(), DEFAULT_VOICEVOX_SAVE_LINES
        )
        self.assertEqual(app.voicevox_use_gpu_var.get(), DEFAULT_VOICEVOX_USE_GPU)
        app._performance_mode_changed.assert_called_once_with()
        app._update_estimate.assert_called_once_with()
        app._save_config.assert_called_once_with()

    def test_apply_voicevox_narration_preset_sets_every_voice_parameter(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.voicevox_busy = False
        preset_name = "Phê bình · nghiêm và dè dặt"
        preset = VOICEVOX_NARRATION_PRESETS[preset_name]
        app.voicevox_preset_var = _Var(preset_name)
        app.voicevox_preset_description_var = _Var("")
        app.voicevox_speed_var = _Var(1.0)
        app.voicevox_pitch_var = _Var(0.0)
        app.voicevox_intonation_var = _Var(1.0)
        app.voicevox_volume_var = _Var(1.0)
        app.voicevox_pause_scale_var = _Var(1.0)
        app.voicevox_line_pause_var = _Var(0.25)
        app.voicevox_pre_silence_var = _Var(0.1)
        app.voicevox_post_silence_var = _Var(0.1)
        app.voicevox_sample_rate_var = _Var("24000")
        app.voicevox_stereo_var = _Var(True)
        app._save_config = Mock()

        app._apply_voicevox_preset()

        self.assertEqual(app.voicevox_speed_var.get(), preset.speed)
        self.assertEqual(app.voicevox_pitch_var.get(), preset.pitch)
        self.assertEqual(app.voicevox_intonation_var.get(), preset.intonation)
        self.assertEqual(app.voicevox_volume_var.get(), preset.volume)
        self.assertEqual(app.voicevox_pause_scale_var.get(), preset.pause_scale)
        self.assertEqual(app.voicevox_line_pause_var.get(), preset.line_pause)
        self.assertEqual(app.voicevox_pre_silence_var.get(), preset.pre_silence)
        self.assertEqual(app.voicevox_post_silence_var.get(), preset.post_silence)
        self.assertEqual(app.voicevox_sample_rate_var.get(), "48000")
        self.assertFalse(app.voicevox_stereo_var.get())
        self.assertIn(preset.description, app.voicevox_preset_description_var.get())
        app._save_config.assert_called_once_with()

    def test_voicevox_parameters_switch_to_custom_when_they_match_no_preset(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.voicevox_preset_apply_in_progress = False
        app.voicevox_preset_var = _Var(DEFAULT_VOICEVOX_NARRATION_PRESET)
        app.voicevox_preset_description_var = _Var("")
        app.voicevox_speed_var = _Var(1.234)
        app.voicevox_pitch_var = _Var(0.0)
        app.voicevox_intonation_var = _Var(1.0)
        app.voicevox_volume_var = _Var(1.0)
        app.voicevox_pause_scale_var = _Var(1.0)
        app.voicevox_line_pause_var = _Var(0.25)
        app.voicevox_pre_silence_var = _Var(0.1)
        app.voicevox_post_silence_var = _Var(0.1)

        app._sync_voicevox_preset_to_parameters()

        self.assertEqual(
            app.voicevox_preset_var.get(), CUSTOM_VOICEVOX_NARRATION_PRESET
        )
        self.assertEqual(
            app.voicevox_preset_description_var.get(),
            CUSTOM_VOICEVOX_PRESET_DESCRIPTION,
        )

    def test_selecting_custom_and_applying_does_not_modify_parameters(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.voicevox_busy = False
        app.voicevox_preset_var = _Var(CUSTOM_VOICEVOX_NARRATION_PRESET)
        app.voicevox_preset_description_var = _Var("")
        app.voicevox_speed_var = _Var(1.234)

        app._apply_voicevox_preset()

        self.assertEqual(app.voicevox_speed_var.get(), 1.234)
        self.assertEqual(
            app.voicevox_preset_description_var.get(),
            CUSTOM_VOICEVOX_PRESET_DESCRIPTION,
        )

    def test_selecting_id_13_only_recommends_its_voice_specific_news_preset(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.voicevox_busy = False
        style = VoicevoxStyle("青山龍星", "uuid", "ノーマル", 13)
        app.voicevox_styles = {style.label: style}
        app.voicevox_style_var = _Var(style.label)
        app.voicevox_preferred_style_id = 53
        app.voicevox_preset_var = _Var(DEFAULT_VOICEVOX_NARRATION_PRESET)
        app.voicevox_preset_description_var = _Var("")
        app.voicevox_speed_var = _Var(1.0)
        app.voicevox_pitch_var = _Var(0.0)
        app.voicevox_intonation_var = _Var(1.0)
        app.voicevox_volume_var = _Var(1.0)
        app.voicevox_pause_scale_var = _Var(1.0)
        app.voicevox_line_pause_var = _Var(0.25)
        app.voicevox_pre_silence_var = _Var(0.1)
        app.voicevox_post_silence_var = _Var(0.1)
        app.voicevox_sample_rate_var = _Var("24000")
        app.voicevox_stereo_var = _Var(True)
        app._save_config = Mock()

        app._voicevox_style_changed()

        selected = "ID 13 · Tin chính trầm rõ"
        self.assertEqual(app.voicevox_preferred_style_id, 13)
        self.assertEqual(app.voicevox_preset_var.get(), selected)
        self.assertEqual(app.voicevox_speed_var.get(), 1.0)
        self.assertEqual(app.voicevox_intonation_var.get(), 1.0)
        self.assertEqual(app.voicevox_sample_rate_var.get(), "24000")
        self.assertIn("Bấm Áp dụng", app.voicevox_preset_description_var.get())
        app._save_config.assert_called_once_with()

    def test_voicevox_preset_cannot_change_while_synthesis_is_running(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = False
        app.closing_after_cancel = False
        app.voicevox_busy = True
        app.voicevox_preset_var = _Var("Mở bài · gây chú ý")
        app.voicevox_speed_var = _Var(0.77)
        app._save_config = Mock()

        app._apply_voicevox_preset()

        self.assertEqual(app.voicevox_speed_var.get(), 0.77)
        app._save_config.assert_not_called()

    def test_restore_defaults_decline_and_closing_are_noops(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.root = object()
        app.closed = False
        app.closing_after_cancel = False
        app.rendering = False
        app.resolution_var = _Var("custom")
        with patch("docurandom.ui.messagebox.askyesno", return_value=False) as confirm:
            app._restore_render_defaults()
        confirm.assert_called_once()
        self.assertEqual(app.resolution_var.get(), "custom")

        app.closing_after_cancel = True
        with patch("docurandom.ui.messagebox.askyesno") as closing_confirm:
            app._restore_render_defaults()
        closing_confirm.assert_not_called()
        self.assertEqual(app.resolution_var.get(), "custom")

    def test_voicevox_result_is_selected_as_render_audio(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomVoicevoxUi_") as temp:
            audio = Path(temp) / "voice.wav"
            subtitle = Path(temp) / "voice.srt"
            metadata = Path(temp) / "voice.voicevox.json"
            audio.write_bytes(b"RIFF-test")
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nTest\n", encoding="utf-8")
            metadata.write_text("{}", encoding="utf-8")
            result = VoicevoxSynthesisResult(
                audio_path=audio,
                subtitle_path=subtitle,
                metadata_path=metadata,
                line_files_dir=None,
                line_count=3,
                duration_seconds=12.4,
                engine_version="test",
                style=VoicevoxStyle("Test Voice", "uuid", "Normal", 7),
            )
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.voicevox_busy = True
            app.voicevox_pending_synthesis = True
            app.voicevox_progress_var = _Var(0.0)
            app.voicevox_percent_var = _Var("0%")
            app.voicevox_status_var = _Var("")
            app.audio_var = _Var("")
            app.rights_confirm_var = _Var(True)
            app._probe_selected_audio_async = Mock()
            app._refresh_voicevox_controls = Mock()
            app._save_config = Mock()
            app._append_log = Mock()

            app._apply_voicevox_result(result)

            self.assertFalse(app.voicevox_busy)
            self.assertEqual(app.audio_var.get(), str(audio))
            self.assertFalse(app.rights_confirm_var.get())
            self.assertEqual(app.voicevox_progress_var.get(), 100.0)
            self.assertIs(app.last_voicevox_path, audio)
            app._probe_selected_audio_async.assert_called_once_with()

    def test_voicevox_backup_dir_uses_selected_or_output_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomVoicevoxBackup_") as temp:
            root = Path(temp)
            app = DocuRandomApp.__new__(DocuRandomApp)
            app.output_var = _Var(str(root / "video-output"))
            app.voicevox_backup_dir_var = _Var("")

            self.assertEqual(
                app._effective_voicevox_backup_dir(),
                (root / "video-output" / "VOICEVOX").resolve(),
            )

            app.voicevox_backup_dir_var.set(str(root / "safe-backup"))
            self.assertEqual(
                app._effective_voicevox_backup_dir(),
                (root / "safe-backup").resolve(),
            )

    def test_parallel_plus_minus_switches_auto_mode_to_manual(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.performance_mode_var = _Var("Tự động (khuyên dùng)")
        app.parallel_jobs_var = _Var("3")
        app.resource_warning_var = _Var("")
        app.runtime_profile = self._nvenc_profile()
        app.rendering = False
        app.cancelling_all = False
        app.render_pool = Mock()

        app._parallel_jobs_changed()

        self.assertEqual(app.performance_mode_var.get(), "Thủ công")
        self.assertEqual(app.parallel_jobs_var.get(), "2")
        app.render_pool.update_limit.assert_called_once_with(2)

    def test_workspace_manual_refresh_cannot_override_global_render_limit(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.global_render_limit_provider = lambda: 1
        app.global_render_limit_setter = Mock()
        app.performance_mode_var = _Var("Thủ công")
        app.parallel_jobs_var = _Var("4")
        app.resource_warning_var = _Var("")
        app.runtime_profile = self._nvenc_profile()
        app.parallel_spinbox = Mock()

        app._performance_mode_changed()

        self.assertEqual(app._effective_parallel_jobs(), 1)
        self.assertEqual(app.parallel_jobs_var.get(), "1")
        app.global_render_limit_setter.assert_not_called()

    def test_workspace_limit_changes_shared_pool_even_while_rendering(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.global_render_limit_provider = lambda: 4
        app.global_render_limit_setter = Mock(return_value=2)
        app.performance_mode_var = _Var("Thủ công")
        app.parallel_jobs_var = _Var("2")
        app.resource_warning_var = _Var("")
        app.runtime_profile = self._nvenc_profile()
        app.rendering = True

        app._parallel_jobs_changed()

        app.global_render_limit_setter.assert_called_once_with(2)
        self.assertEqual(app.parallel_jobs_var.get(), "2")
        self.assertIn("toàn tool: 2", app.resource_warning_var.get())
        self.assertNotIn("hàng đợi kết thúc", app.resource_warning_var.get())

    def test_failed_global_limit_save_preserves_current_limit(self) -> None:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.global_render_limit_provider = lambda: 1
        app.global_render_limit_setter = Mock(side_effect=OSError("disk read-only"))
        app.performance_mode_var = _Var("Thủ công")
        app.parallel_jobs_var = _Var("4")
        app.resource_warning_var = _Var("")
        app.runtime_profile = self._nvenc_profile()

        with patch("docurandom.ui.record_exception"):
            app._parallel_jobs_changed()

        self.assertEqual(app.parallel_jobs_var.get(), "1")
        self.assertIn("Chưa lưu được", app.resource_warning_var.get())


class FlowErrorEventTests(unittest.TestCase):
    def _app(self, *, scheduled: bool, recovery: bool) -> DocuRandomApp:
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.content_token = 7
        app.content_error_dialog_token = 7
        # Reproduce the unrelated VOICEVOX token collision seen live.
        app.voicevox_error_dialog_token = 7
        app.flow_generation_busy = True
        app.flow_percent_var = _Var("")
        app.content_status_var = _Var("")
        app.closed = False
        app.closing_after_cancel = False
        app.root = Mock()
        app.events = Queue()
        app._schedules_armed = Mock(return_value=scheduled)
        app._append_content_chat = Mock()
        app._record_pipeline_stage = Mock()
        app._refresh_flow_controls = Mock()
        app._refresh_content_controls = Mock()
        app._schedule_pipeline_auto_resume = Mock(return_value=recovery)
        app._append_log = Mock()
        return app

    @patch("docurandom.ui.messagebox.showerror")
    def test_automatic_flow_error_leaves_following_workspace_events_running(self, showerror: Mock) -> None:
        for scheduled, recovery in ((True, True), (True, False), (False, True)):
            with self.subTest(scheduled=scheduled, recovery=recovery):
                app = self._app(scheduled=scheduled, recovery=recovery)
                app.events.put(("flow_generation_error", 7, "Flow disconnected"))
                app.events.put(("log", "following upload checkpoint event"))
                self.assertEqual(app._process_events(reschedule=False), 2)
                self.assertFalse(app.flow_generation_busy)
                app._record_pipeline_stage.assert_called_once_with("flow_images", "error", "Flow disconnected")
                app._schedule_pipeline_auto_resume.assert_called_once_with("Flow", "Flow disconnected")
                app._append_log.assert_called_once_with("following upload checkpoint event")
                showerror.assert_not_called()

    @patch("docurandom.ui.messagebox.showerror")
    def test_manual_terminal_flow_error_remains_visible(self, showerror: Mock) -> None:
        app = self._app(scheduled=False, recovery=False)
        app._handle_flow_generation_error(7, "Bind the saved project")
        showerror.assert_called_once_with("Không thể tạo ảnh bằng Flow", "Bind the saved project", parent=app.root)

    @patch("docurandom.ui.messagebox.showerror")
    def test_voicevox_token_collision_cannot_open_flow_dialog(self, showerror: Mock) -> None:
        app = self._app(scheduled=False, recovery=False)
        app.content_error_dialog_token = 0
        app._handle_flow_generation_error(7, "Flow disconnected")
        showerror.assert_not_called()

    @patch("docurandom.ui.messagebox.showerror")
    def test_old_flow_event_does_not_touch_new_run(self, showerror: Mock) -> None:
        app = self._app(scheduled=True, recovery=True)
        app._handle_flow_generation_error(6, "old run")
        self.assertTrue(app.flow_generation_busy)
        app._record_pipeline_stage.assert_not_called()
        app._schedule_pipeline_auto_resume.assert_not_called()
        showerror.assert_not_called()


if __name__ == "__main__":
    unittest.main()
