from __future__ import annotations

import json
import os
import tempfile
import unittest
import ctypes
from pathlib import Path
from unittest import mock

import updater_app


class _Process:
    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> int | None:
        return None if not self.terminated else 1

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 1

    def kill(self) -> None:
        self.terminated = True


class UpdaterAppTests(unittest.TestCase):
    def test_update_staging_uses_selected_docurandom_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdaterHome_") as value:
            selected = Path(value) / "DocuRandom Studio"
            with mock.patch.dict(
                os.environ,
                {"DOCURANDOM_HOME": str(selected)},
                clear=True,
            ):
                self.assertEqual(
                    updater_app._update_root(),
                    selected.resolve() / "Data" / "updates",
                )

    def _layout(self, root: Path) -> tuple[Path, updater_app.InstallPlan]:
        local = root / "local"
        install = root / "installed" / "DocuRandomStudio"
        stage = local / "DocuRandomStudio" / "updates" / "2.2.0" / "staged" / "DocuRandomStudio"
        health = local / "DocuRandomStudio" / "updates" / "2.2.0" / "health.json"
        install.mkdir(parents=True)
        stage.mkdir(parents=True)
        (install / "DocuRandomStudio.exe").write_bytes(b"old")
        (stage / "DocuRandomStudio.exe").write_bytes(b"new")
        plan = updater_app.InstallPlan(
            version="2.2.0",
            current_pid=123,
            install_dir=install,
            staged_dir=stage,
            health_path=health,
            health_token="x" * 40,
        )
        return local, plan

    def test_cleanup_installer_removes_only_external_release_launcher(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomLauncherCleanup_") as value:
            root = Path(value)
            install = root / "managed" / "App"
            install.mkdir(parents=True)
            source = root / "Desktop" / "DocuRandomStudio.exe"
            source.parent.mkdir()
            source.write_bytes(b"installer")

            self.assertTrue(updater_app.cleanup_installer_launcher(source, install))
            self.assertFalse(source.exists())

    def test_cleanup_installer_never_removes_managed_application(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomLauncherCleanup_") as value:
            install = Path(value) / "managed" / "App"
            install.mkdir(parents=True)
            application = install / "DocuRandomStudio.exe"
            application.write_bytes(b"application")

            self.assertFalse(
                updater_app.cleanup_installer_launcher(application, install)
            )
            self.assertTrue(application.exists())

    def test_cleanup_installer_rejects_renamed_unrelated_executable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomLauncherCleanup_") as value:
            root = Path(value)
            install = root / "managed" / "App"
            install.mkdir(parents=True)
            unrelated = root / "Desktop" / "Other.exe"
            unrelated.parent.mkdir()
            unrelated.write_bytes(b"unrelated")

            self.assertFalse(
                updater_app.cleanup_installer_launcher(unrelated, install)
            )
            self.assertTrue(unrelated.exists())

    def test_load_plan_rejects_path_outside_update_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdaterTest_") as value:
            root = Path(value)
            local, plan = self._layout(root)
            outside = root / "install-plan.json"
            outside.write_text("{}", encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {"LOCALAPPDATA": str(local), "DOCURANDOM_HOME": ""},
            ):
                with self.assertRaises(updater_app.InstallError):
                    updater_app.load_plan(outside)

    def test_load_plan_accepts_valid_bounded_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdaterTest_") as value:
            root = Path(value)
            local, plan = self._layout(root)
            plan_path = plan.health_path.parent / "install-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "version": plan.version,
                        "current_pid": plan.current_pid,
                        "install_dir": str(plan.install_dir),
                        "staged_dir": str(plan.staged_dir),
                        "health_path": str(plan.health_path),
                        "health_token": plan.health_token,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {"LOCALAPPDATA": str(local), "DOCURANDOM_HOME": ""},
            ):
                loaded = updater_app.load_plan(plan_path)
            self.assertEqual(loaded.version, "2.2.0")

    def test_install_removes_verified_rollback_copy_after_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdaterTest_") as value:
            _, plan = self._layout(Path(value))
            process = _Process()
            with (
                mock.patch("updater_app.wait_for_process_exit", return_value=True),
                mock.patch("updater_app._stop_processes_in_tree") as stop_helpers,
                mock.patch("updater_app._close_explorer_windows_in_tree") as close_explorer,
                mock.patch("updater_app._launch", return_value=process),
                mock.patch("updater_app._wait_for_health", return_value=True),
            ):
                updater_app.install_update(plan)
            stop_helpers.assert_called_once_with(plan.install_dir)
            close_explorer.assert_called_once_with(plan.install_dir)
            self.assertEqual((plan.install_dir / "DocuRandomStudio.exe").read_bytes(), b"new")
            self.assertEqual(
                (plan.install_dir / ".docurandom-version").read_text(encoding="ascii"),
                "2.2.0",
            )
            backup = plan.install_dir.parent / ".DocuRandomStudio.rollback"
            self.assertFalse(backup.exists())

    def test_install_restores_previous_build_when_health_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdaterTest_") as value:
            _, plan = self._layout(Path(value))
            process = _Process()
            with (
                mock.patch("updater_app.wait_for_process_exit", return_value=True),
                mock.patch("updater_app._stop_processes_in_tree"),
                mock.patch("updater_app._close_explorer_windows_in_tree"),
                mock.patch("updater_app._launch", return_value=process),
                mock.patch("updater_app._wait_for_health", return_value=False),
            ):
                with self.assertRaises(updater_app.InstallError):
                    updater_app.install_update(plan)
            self.assertEqual((plan.install_dir / "DocuRandomStudio.exe").read_bytes(), b"old")
            self.assertFalse((plan.install_dir.parent / ".DocuRandomStudio.rollback").exists())

    def test_directory_lock_falls_back_to_verified_in_place_install(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdaterFallback_") as value:
            _, plan = self._layout(Path(value))
            process = _Process()
            locked = OSError("directory locked")
            locked.winerror = 5  # type: ignore[attr-defined]

            def replace_with_root_lock(source: Path, destination: Path, **_kwargs) -> None:
                if source == plan.install_dir:
                    raise locked
                os.replace(source, destination)

            with (
                mock.patch("updater_app.wait_for_process_exit", return_value=True),
                mock.patch("updater_app._stop_processes_in_tree"),
                mock.patch("updater_app._close_explorer_windows_in_tree"),
                mock.patch(
                    "updater_app._replace_directory_with_retry",
                    side_effect=replace_with_root_lock,
                ),
                mock.patch("updater_app._launch", return_value=process),
                mock.patch("updater_app._wait_for_health", return_value=True),
            ):
                method = updater_app.install_update(plan)

            self.assertEqual(method, "in-place-fallback")
            self.assertEqual(
                (plan.install_dir / "DocuRandomStudio.exe").read_bytes(),
                b"new",
            )
            self.assertFalse(plan.staged_dir.exists())
            self.assertFalse(
                (plan.install_dir.parent / ".DocuRandomStudio.rollback").exists()
            )

    def test_in_place_health_failure_restores_old_runtime_and_keeps_stage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdaterFallback_") as value:
            _, plan = self._layout(Path(value))
            process = _Process()
            locked = OSError("directory locked")
            locked.winerror = 5  # type: ignore[attr-defined]

            def replace_with_root_lock(source: Path, destination: Path, **_kwargs) -> None:
                if source == plan.install_dir:
                    raise locked
                os.replace(source, destination)

            with (
                mock.patch("updater_app.wait_for_process_exit", return_value=True),
                mock.patch("updater_app._stop_processes_in_tree"),
                mock.patch("updater_app._close_explorer_windows_in_tree"),
                mock.patch(
                    "updater_app._replace_directory_with_retry",
                    side_effect=replace_with_root_lock,
                ),
                mock.patch("updater_app._launch", return_value=process),
                mock.patch("updater_app._wait_for_health", return_value=False),
            ):
                with self.assertRaises(updater_app.InstallError):
                    updater_app.install_update(plan)

            self.assertEqual(
                (plan.install_dir / "DocuRandomStudio.exe").read_bytes(),
                b"old",
            )
            self.assertEqual(
                (plan.staged_dir / "DocuRandomStudio.exe").read_bytes(),
                b"new",
            )
            self.assertFalse(
                (plan.install_dir.parent / ".DocuRandomStudio.rollback").exists()
            )

    def test_stuck_old_app_is_force_closed_only_after_graceful_wait(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdaterForceClose_") as value:
            _, plan = self._layout(Path(value))
            process = _Process()
            with (
                mock.patch(
                    "updater_app.wait_for_process_exit",
                    side_effect=[False, True],
                ) as wait,
                mock.patch(
                    "updater_app._terminate_current_app_if_verified",
                    return_value=True,
                ) as terminate,
                mock.patch("updater_app._stop_processes_in_tree"),
                mock.patch("updater_app._close_explorer_windows_in_tree"),
                mock.patch("updater_app._launch", return_value=process),
                mock.patch("updater_app._wait_for_health", return_value=True),
            ):
                updater_app.install_update(plan)

            self.assertEqual(wait.call_count, 2)
            terminate.assert_called_once_with(plan)

    def test_successful_main_removes_stale_install_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdaterTest_") as value:
            root = Path(value)
            plan_path = root / "install-plan.json"
            error_path = root / "install-error.txt"
            plan_path.write_text("{}", encoding="utf-8")
            error_path.write_text("old failure", encoding="utf-8")
            with (
                mock.patch("updater_app.load_plan", return_value=mock.sentinel.plan),
                mock.patch(
                    "updater_app.install_update",
                    return_value="directory-swap",
                ) as install,
                mock.patch("updater_app._write_install_result") as write_result,
            ):
                result = updater_app.main(["--plan", str(plan_path)])
            self.assertEqual(result, 0)
            install.assert_called_once_with(mock.sentinel.plan)
            self.assertEqual(write_result.call_count, 1)
            self.assertFalse(error_path.exists())

    def test_failed_main_records_error_and_reopens_installed_build(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdaterTest_") as value:
            root = Path(value)
            plan_path = root / "install-plan.json"
            plan_path.write_text("{}", encoding="utf-8")
            _, plan = self._layout(root / "layout")
            with (
                mock.patch("updater_app.load_plan", return_value=plan),
                mock.patch(
                    "updater_app.install_update",
                    side_effect=updater_app.InstallError("locked"),
                ),
                mock.patch("updater_app._relaunch_installed_app") as relaunch,
                mock.patch("updater_app._show_install_failure") as show_failure,
            ):
                result = updater_app.main(["--plan", str(plan_path)])
            self.assertEqual(result, 1)
            friendly = (root / "install-error.txt").read_text(encoding="utf-8")
            self.assertIn("Windows vẫn đang giữ", friendly)
            self.assertNotIn("locked", friendly)
            relaunch.assert_called_once_with(plan)
            show_failure.assert_called_once_with(friendly)

    def test_replace_directory_retries_transient_windows_lock(self) -> None:
        locked = OSError("sharing violation")
        locked.winerror = 32  # type: ignore[attr-defined]
        with (
            mock.patch("updater_app.os.name", "nt"),
            mock.patch("updater_app.os.replace", side_effect=[locked, None]) as replace,
            mock.patch("updater_app.time.sleep") as sleep,
        ):
            updater_app._replace_directory_with_retry(
                Path("source"),
                Path("destination"),
                timeout=30.0,
                retry_interval=0.25,
            )
        self.assertEqual(replace.call_count, 2)
        sleep.assert_called_once_with(0.25)

    def test_replace_directory_does_not_hide_permanent_windows_lock(self) -> None:
        locked = OSError("access denied")
        locked.winerror = 5  # type: ignore[attr-defined]
        with (
            mock.patch("updater_app.os.name", "nt"),
            mock.patch("updater_app.os.replace", side_effect=locked) as replace,
        ):
            with self.assertRaises(OSError):
                updater_app._replace_directory_with_retry(
                    Path("source"),
                    Path("destination"),
                    timeout=0.0,
                )
        replace.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows directory-handle behavior")
    def test_in_place_fallback_works_while_app_directory_itself_is_locked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDirectoryLock_") as value:
            root = Path(value)
            install = root / "App"
            install.mkdir()
            (install / "DocuRandomStudio.exe").write_bytes(b"old")
            (install / "old-runtime.bin").write_bytes(b"old-runtime")

            create_file = ctypes.windll.kernel32.CreateFileW
            create_file.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            ]
            create_file.restype = ctypes.c_void_p
            handle = create_file(
                str(install),
                0x80000000,  # GENERIC_READ
                0x00000001 | 0x00000002,  # share read/write, deliberately deny delete
                None,
                3,
                0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
                None,
            )
            self.assertNotIn(handle, (None, ctypes.c_void_p(-1).value))
            try:
                with self.assertRaises(OSError):
                    updater_app._replace_directory_with_retry(
                        install,
                        root / ".App.rollback",
                        timeout=0.0,
                    )
                updater_app._clear_directory_contents(install, timeout=0.0)
                self.assertTrue(install.is_dir())
                self.assertEqual(list(install.iterdir()), [])
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
