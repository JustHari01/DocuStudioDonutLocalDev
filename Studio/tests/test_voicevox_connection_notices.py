"""Exercise real connection events across automatic retries and explicit starts."""
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from docurandom.ui import DocuRandomApp
from docurandom.voicevox import VoicevoxStyle


def connection_app():
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.closed = app.closing_after_cancel = app.voicevox_busy = False
    app.voicevox_token = app.voicevox_remote_notify_token = 0
    app.voicevox_remote_start_pending = False
    app.voicevox_pending_synthesis = False
    app.content_precheck_pending = False
    app.voicevox_endpoint_var = Mock(get=lambda: "http://127.0.0.1:50021")
    app.voicevox_engine_path_var = Mock(get=lambda: "")
    app.voicevox_use_gpu_var = Mock(get=lambda: False)
    app.voicevox_cancel_event = Mock()
    app.events = Queue()
    app.root = Mock()
    app.voicevox_preferred_style_id = 53
    for name in (
        "_voicevox_search_roots", "_append_log", "_set_voicevox_engine_state",
        "_refresh_voicevox_controls", "_refresh_global_state", "_save_config",
        "_notify_remote", "voicevox_progress_var", "voicevox_percent_var",
        "voicevox_status_var", "voicevox_style_var", "voicevox_style_combo",
    ):
        setattr(app, name, Mock())
    app._voicevox_cpu_threads = Mock(return_value=2)
    return app


def start_check(app, **kwargs):
    # The engine worker is tested separately; here keep it queued so we can
    # deliver the same native completion event that the real worker produces.
    with (
        patch("docurandom.ui.discover_engine_path", return_value=Path("engine.exe")),
        patch("docurandom.ui.threading.Thread"),
    ):
        app._connect_voicevox_async(show_error_dialog=False, **kwargs)


def complete_check(app, *, started=False, token=None):
    app.events.put((
        "voicevox_connected", token or app.voicevox_token, "0.25.2",
        (VoicevoxStyle("Narrator", "speaker-a", "Normal", 53),),
        "engine.exe", started, False,
    ))
    app._process_events(reschedule=False)


def test_seven_background_retry_prechecks_keep_working_without_ready_spam():
    app = connection_app()
    for _ in range(7):
        start_check(app)
        complete_check(app)
        assert not app.voicevox_busy
        app._set_voicevox_engine_state.assert_called_with("ready", "● HOẠT ĐỘNG · 1 GIỌNG")
    app._notify_remote.assert_not_called()
    assert app._save_config.call_count == 7


def test_requested_start_reports_ready_once_and_next_retry_stays_quiet():
    app = connection_app()
    start_check(app, notify_ready=True)
    complete_check(app)
    app._notify_remote.assert_called_once_with(
        "status", "VOICEVOX đã sẵn sàng · engine 0.25.2 · 1 kiểu giọng.",
    )
    start_check(app)
    complete_check(app)
    assert app._notify_remote.call_count == 1


def test_real_engine_start_is_reported_even_for_a_background_check():
    app = connection_app()
    start_check(app)
    complete_check(app, started=True)
    app._notify_remote.assert_called_once()


def test_stale_completion_cannot_report_ready_for_newer_connection():
    app = connection_app()
    app.voicevox_token = 2
    app.voicevox_remote_notify_token = 1
    complete_check(app, token=1, started=True)
    app._notify_remote.assert_not_called()


def test_requested_start_waiting_behind_passive_check_preserves_confirmation():
    app = connection_app()
    app.voicevox_token = 1
    app.voicevox_remote_start_pending = True
    app._connect_voicevox_async = Mock()
    app.root.after_idle.side_effect = lambda callback: callback()
    app.events.put(("voicevox_idle", 1, "not running", False))
    app._process_events(reschedule=False)
    app._connect_voicevox_async.assert_called_once_with(
        auto_start=True, show_error_dialog=False, notify_ready=True,
    )
