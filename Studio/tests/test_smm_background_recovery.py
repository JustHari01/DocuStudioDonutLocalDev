from types import SimpleNamespace
from unittest.mock import Mock, patch

from docurandom.ui import DocuRandomApp


def session(*, automatic=True, closing=False):
    return SimpleNamespace(
        smm_busy=True,
        closed=False,
        closing_after_cancel=closing,
        root=object(),
        smm_api_status_var=Mock(),
        smm_monitor_status_var=Mock(),
        smm_auto_like_var=SimpleNamespace(get=lambda: automatic),
        _append_log=Mock(),
        _refresh_smm_controls=Mock(),
        _schedule_smm_monitor=Mock(),
    )


def test_background_account_failure_logs_and_retries_without_modal():
    app = session()
    with patch("docurandom.ui.messagebox.showerror") as dialog:
        DocuRandomApp._handle_smm_error(app, "Kết nối 1MM", "timeout", interactive=False)
    dialog.assert_not_called()
    app._schedule_smm_monitor.assert_called_once_with(120_000)
    app._append_log.assert_called_once()
    assert app.smm_busy is False


def test_manual_account_failure_still_reports_to_operator():
    app = session()
    with patch("docurandom.ui.messagebox.showerror") as dialog:
        DocuRandomApp._handle_smm_error(app, "Kết nối 1MM", "timeout")
    dialog.assert_called_once_with("Kết nối 1MM", "timeout", parent=app.root)
    app._schedule_smm_monitor.assert_not_called()


def test_account_failure_during_shutdown_does_not_reopen_dialog_or_monitor():
    app = session(closing=True)
    with patch("docurandom.ui.messagebox.showerror") as dialog:
        DocuRandomApp._handle_smm_error(app, "Kết nối 1MM", "cancelled")
    dialog.assert_not_called()
    app._schedule_smm_monitor.assert_not_called()
    assert app.smm_busy is False


def test_background_failure_respects_disabled_monitor():
    app = session(automatic=False)
    with patch("docurandom.ui.messagebox.showerror") as dialog:
        DocuRandomApp._handle_smm_error(app, "Kết nối 1MM", "timeout", interactive=False)
    dialog.assert_not_called()
    app._schedule_smm_monitor.assert_not_called()
