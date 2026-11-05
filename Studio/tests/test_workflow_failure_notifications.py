from unittest.mock import Mock, patch

from docurandom.content_ai import ContentAIError
from docurandom.ui import DocuRandomApp


def app_for_notifications():
    app = DocuRandomApp.__new__(DocuRandomApp)
    app._notify_remote = Mock()
    app.content_snapshot = {"project_name": "Video B", "output_dir": "output"}
    app.video_chat_thread_id = "exact-chat"
    return app


def test_identical_probe_alert_waits_but_changed_error_and_new_video_are_reported():
    app = app_for_notifications()
    with patch("docurandom.ui.time.monotonic", return_value=1000):
        for _ in range(3):
            app._notify_workflow_failure("chatgpt", ContentAIError("load failed"), recovery_scheduled=False)
        assert app._notify_remote.call_count == 1
        app._notify_workflow_failure("chatgpt", ContentAIError("login required"), recovery_scheduled=False)
        assert app._notify_remote.call_count == 2
        app.video_chat_thread_id = "new-chat"
        app._notify_workflow_failure("chatgpt", ContentAIError("load failed"), recovery_scheduled=False)
        assert app._notify_remote.call_count == 3
    with patch("docurandom.ui.time.monotonic", return_value=1901):
        app._notify_workflow_failure("chatgpt", ContentAIError("load failed"), recovery_scheduled=False)
    assert app._notify_remote.call_count == 4


def test_content_error_handler_sends_one_alert_with_details_after_recovery_decision():
    app = app_for_notifications()
    for name in ("_cancel_content_precheck_watchdog", "_append_content_chat", "_record_pipeline_stage", "_refresh_content_controls"):
        setattr(app, name, Mock())
    app.content_status_var = Mock()
    app.content_generation_started = True
    app._schedule_pipeline_auto_resume = Mock(return_value=False)
    app._finish_content_error("CHATGPT_CONVERSATION_LOAD_FAILED: preserve checkpoint")
    app._notify_remote.assert_called_once()
    category, message = app._notify_remote.call_args.args
    assert category == "error"
    assert "Công đoạn: ChatGPT Web" in message
    assert "Mã lỗi: CONTENTAIERROR" in message
    assert "CHATGPT_CONVERSATION_LOAD_FAILED" in message
    app._record_pipeline_stage.assert_called_once_with("chatgpt", "error", "CHATGPT_CONVERSATION_LOAD_FAILED: preserve checkpoint")


def test_recovery_alert_is_a_status_and_distinct_from_a_manual_failure():
    app = app_for_notifications()
    app._notify_workflow_failure("chatgpt", ContentAIError("load failed"), recovery_scheduled=True)
    assert app._notify_remote.call_args.args[0] == "status"
    assert "tự kiểm tra/nối lại đúng checkpoint" in app._notify_remote.call_args.args[1]
    app._notify_workflow_failure("chatgpt", ContentAIError("load failed"), recovery_scheduled=False)
    assert app._notify_remote.call_count == 2
    assert app._notify_remote.call_args.args[0] == "error"
