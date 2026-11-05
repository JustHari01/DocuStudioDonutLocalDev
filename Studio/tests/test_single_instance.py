from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from docurandom.single_instance import (
    SW_RESTORE,
    WINDOW_TITLE,
    SingleInstanceGuard,
    WindowFocusResult,
    focus_existing_window,
)


class _FakeUser32:
    def __init__(
        self,
        *,
        visible: bool = True,
        iconic: bool = True,
        windows: list[int] | None = None,
        bring_result: int = 1,
        foreground_result: int = 1,
    ) -> None:
        self.visible = visible
        self.iconic = iconic
        self.windows = list(windows or [731])
        self.bring_result = bring_result
        self.foreground_result = foreground_result
        self.find_calls: list[tuple[object, str]] = []
        self.show_calls: list[tuple[int, int]] = []
        self.bring_calls: list[int] = []
        self.foreground_calls: list[int] = []

    def FindWindowW(self, class_name: object, window_name: str) -> int:
        self.find_calls.append((class_name, window_name))
        if len(self.windows) > 1:
            return self.windows.pop(0)
        return self.windows[0]

    def IsWindowVisible(self, _window: int) -> int:
        return int(self.visible)

    def IsIconic(self, _window: int) -> int:
        return int(self.iconic)

    def ShowWindow(self, window: int, command: int) -> int:
        self.show_calls.append((window, command))
        return 1

    def BringWindowToTop(self, window: int) -> int:
        self.bring_calls.append(window)
        return self.bring_result

    def SetForegroundWindow(self, window: int) -> int:
        self.foreground_calls.append(window)
        return self.foreground_result


class SingleInstanceTests(unittest.TestCase):
    def test_primary_guard_holds_and_closes_mutex_once(self) -> None:
        closer = Mock()
        with (
            patch("docurandom.single_instance._running_on_windows", return_value=True),
            patch(
                "docurandom.single_instance._open_windows_mutex",
                return_value=(41, False, closer),
            ),
        ):
            guard = SingleInstanceGuard.acquire()
        self.assertFalse(guard.already_running)
        guard.close()
        guard.close()
        closer.assert_called_once_with(41)

    def test_second_instance_is_identified(self) -> None:
        closer = Mock()
        with (
            patch("docurandom.single_instance._running_on_windows", return_value=True),
            patch(
                "docurandom.single_instance._open_windows_mutex",
                return_value=(52, True, closer),
            ),
        ):
            guard = SingleInstanceGuard.acquire()
        self.assertTrue(guard.already_running)
        guard.close()
        closer.assert_called_once_with(52)

    def test_existing_visible_window_is_restored_and_foregrounded(self) -> None:
        user32 = _FakeUser32(visible=True, iconic=True)
        with (
            patch("docurandom.single_instance._running_on_windows", return_value=True),
            patch("docurandom.single_instance._load_user32", return_value=user32),
        ):
            result = focus_existing_window()
        self.assertTrue(result.handled)
        self.assertTrue(result.activated)
        self.assertTrue(result.minimized)
        self.assertTrue(result.foreground_result)
        self.assertEqual(user32.find_calls, [(None, WINDOW_TITLE)])
        self.assertEqual(user32.show_calls, [(731, SW_RESTORE)])
        self.assertEqual(user32.bring_calls, [731])
        self.assertEqual(user32.foreground_calls, [731])

    def test_hidden_window_is_not_activated(self) -> None:
        user32 = _FakeUser32(visible=False, iconic=False)
        with (
            patch("docurandom.single_instance._running_on_windows", return_value=True),
            patch("docurandom.single_instance._load_user32", return_value=user32),
        ):
            result = focus_existing_window()
        self.assertTrue(result.handled)
        self.assertFalse(result.visible)
        self.assertFalse(result.activated)
        self.assertEqual(user32.show_calls, [])
        self.assertEqual(user32.foreground_calls, [])

    def test_non_windows_is_graceful(self) -> None:
        with patch("docurandom.single_instance._running_on_windows", return_value=False):
            guard = SingleInstanceGuard.acquire()
            self.assertFalse(guard.already_running)
            self.assertFalse(focus_existing_window())
            guard.close()

    def test_mutex_failure_is_reported_to_launcher(self) -> None:
        with (
            patch("docurandom.single_instance._running_on_windows", return_value=True),
            patch(
                "docurandom.single_instance._open_windows_mutex",
                side_effect=OSError("access denied"),
            ),
        ):
            guard = SingleInstanceGuard.acquire()
        self.assertFalse(guard.already_running)
        self.assertTrue(guard.acquisition_failed)

    def test_focus_retries_short_startup_race(self) -> None:
        user32 = _FakeUser32(windows=[0, 0, 731], iconic=False)
        with (
            patch("docurandom.single_instance._running_on_windows", return_value=True),
            patch("docurandom.single_instance._load_user32", return_value=user32),
            patch("docurandom.single_instance.time.sleep") as sleep_mock,
        ):
            result = focus_existing_window(retry_attempts=4, retry_delay=0.01)
        self.assertTrue(result.handled)
        self.assertEqual(len(user32.find_calls), 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_found_window_is_handled_even_if_foreground_is_denied(self) -> None:
        user32 = _FakeUser32(
            iconic=False,
            bring_result=0,
            foreground_result=0,
        )
        with (
            patch("docurandom.single_instance._running_on_windows", return_value=True),
            patch("docurandom.single_instance._load_user32", return_value=user32),
        ):
            result = focus_existing_window()
        self.assertTrue(result.handled)
        self.assertFalse(result.activated)
        self.assertFalse(result.foreground_result)


class AppLauncherTests(unittest.TestCase):
    def test_second_instance_focuses_and_never_runs_ui(self) -> None:
        import app

        guard = SingleInstanceGuard(already_running=True)
        guard.close = Mock()  # type: ignore[method-assign]
        with (
            patch.object(app.SingleInstanceGuard, "acquire", return_value=guard),
            patch.object(
                app,
                "focus_existing_window",
                return_value=WindowFocusResult(found=False),
            ) as focus_mock,
            patch.object(app, "_run_ui") as run_mock,
        ):
            app.main()
        focus_mock.assert_called_once()
        run_mock.assert_not_called()
        guard.close.assert_called_once()  # type: ignore[attr-defined]

    def test_mutex_failure_with_existing_window_does_not_run_ui(self) -> None:
        import app

        guard = SingleInstanceGuard(acquisition_failed=True)
        guard.close = Mock()  # type: ignore[method-assign]
        with (
            patch.object(app.SingleInstanceGuard, "acquire", return_value=guard),
            patch.object(
                app,
                "focus_existing_window",
                return_value=WindowFocusResult(found=True, visible=True),
            ),
            patch.object(app, "_run_ui") as run_mock,
        ):
            app.main()
        run_mock.assert_not_called()
        guard.close.assert_called_once()  # type: ignore[attr-defined]

    def test_mutex_failure_without_window_fails_open_and_always_closes(self) -> None:
        import app

        guard = SingleInstanceGuard(acquisition_failed=True)
        guard.close = Mock()  # type: ignore[method-assign]
        with (
            patch.object(app.SingleInstanceGuard, "acquire", return_value=guard),
            patch.object(
                app,
                "focus_existing_window",
                return_value=WindowFocusResult(),
            ),
            patch.object(app, "_run_ui", side_effect=RuntimeError("startup failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                app.main()
        guard.close.assert_called_once()  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
