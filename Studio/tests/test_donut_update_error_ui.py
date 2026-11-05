from types import SimpleNamespace
import threading
from unittest import mock

from docurandom.workspace_ui import WorkspaceLauncher


def launcher_state():
    return SimpleNamespace(
        _launcher_closed=False,
        donut_update_check_cancel_event=threading.Event(),
        donut_update_check_busy=True,
        donut_status_var=mock.Mock(),
        donut_test_detail_var=mock.Mock(),
    )


def test_failed_check_reports_auth_problem_and_preserves_running_browser():
    launcher = launcher_state()
    with mock.patch("docurandom.workspace_ui.messagebox.askyesno") as confirm:
        WorkspaceLauncher._finish_donut_update_check(
            launcher, None, error="Đăng nhập GitHub đã hết hạn hoặc chưa đủ quyền.",
        )
    assert launcher.donut_update_check_busy is False
    message = launcher.donut_test_detail_var.set.call_args.args[0]
    assert "Chưa kiểm tra được" in message
    assert "hết hạn" in message
    launcher.donut_status_var.set.assert_not_called()
    confirm.assert_not_called()


def test_completed_check_with_no_release_does_not_report_an_auth_failure():
    launcher = launcher_state()
    WorkspaceLauncher._finish_donut_update_check(launcher, None)
    assert launcher.donut_update_check_busy is False
    launcher.donut_test_detail_var.set.assert_not_called()


def test_late_failed_check_cannot_update_a_closed_launcher():
    launcher = launcher_state()
    launcher.donut_update_check_cancel_event.set()
    WorkspaceLauncher._finish_donut_update_check(launcher, None, error="expired")
    launcher.donut_test_detail_var.set.assert_not_called()
