from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


APP_EXECUTABLE = "DocuRandomStudio.exe"
VERSION_MARKER = ".docurandom-version"
PLAN_SCHEMA = 1
PROCESS_WAIT_SECONDS = 90.0
GRACEFUL_PROCESS_WAIT_SECONDS = 15.0
FORCED_PROCESS_WAIT_SECONDS = 15.0
HEALTH_WAIT_SECONDS = 60.0
FILE_UNLOCK_WAIT_SECONDS = 8.0
FILE_UNLOCK_RETRY_INTERVAL = 0.25
LAUNCHER_CLEANUP_WAIT_SECONDS = 30.0
WINDOWS_RETRYABLE_FILE_ERRORS = frozenset({5, 32, 33})
INSTALL_RESULT_NAME = "install-result.json"


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallPlan:
    version: str
    current_pid: int
    install_dir: Path
    staged_dir: Path
    health_path: Path
    health_token: str
    source_version: str = ""


def _update_root() -> Path:
    managed_home = os.environ.get("DOCURANDOM_HOME", "").strip()
    if managed_home:
        home = Path(managed_home).expanduser().resolve(strict=False)
        if home.is_absolute() and home != Path(home.anchor):
            return (home / "Data" / "updates").resolve()
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return (Path(base) / "DocuRandomStudio" / "updates").resolve()


def _is_within(path: Path, parent: Path) -> bool:
    resolved = path.resolve(strict=False)
    root = parent.resolve(strict=False)
    return resolved == root or root in resolved.parents


def load_plan(path: Path) -> InstallPlan:
    plan_path = path.resolve(strict=True)
    root = _update_root()
    if not _is_within(plan_path, root) or plan_path.name != "install-plan.json":
        raise InstallError("Kế hoạch cập nhật nằm ngoài vùng dữ liệu an toàn.")
    try:
        value = json.loads(plan_path.read_text(encoding="utf-8"))
        if value.get("schema") != PLAN_SCHEMA:
            raise ValueError("schema")
        plan = InstallPlan(
            version=str(value["version"]),
            current_pid=int(value["current_pid"]),
            install_dir=Path(value["install_dir"]).resolve(strict=True),
            staged_dir=Path(value["staged_dir"]).resolve(strict=True),
            health_path=Path(value["health_path"]).resolve(strict=False),
            health_token=str(value["health_token"]),
            source_version=str(value.get("source_version") or ""),
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InstallError("Kế hoạch cập nhật không hợp lệ.") from exc
    version_root = plan_path.parent.resolve()
    if not _is_within(plan.staged_dir, version_root / "staged"):
        raise InstallError("Thư mục bản mới nằm ngoài staging đã xác minh.")
    if not _is_within(plan.health_path, version_root):
        raise InstallError("Tệp kiểm tra sức khỏe nằm ngoài vùng cập nhật.")
    if plan.health_path.name != "health.json" or len(plan.health_token) < 32:
        raise InstallError("Mã kiểm tra sức khỏe không hợp lệ.")
    if plan.current_pid <= 0 or not (plan.staged_dir / APP_EXECUTABLE).is_file():
        raise InstallError("Bản cập nhật staging không đầy đủ.")
    if plan.install_dir.parent == plan.install_dir or not (plan.install_dir / APP_EXECUTABLE).is_file():
        raise InstallError("Không xác định được thư mục cài đặt hiện tại.")
    if _is_within(plan.install_dir, root) or _is_within(root, plan.install_dir):
        raise InstallError("Thư mục cài đặt chồng lên vùng cập nhật.")
    return plan


def wait_for_process_exit(pid: int, timeout: float = PROCESS_WAIT_SECONDS) -> bool:
    if os.name != "nt":
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.2)
        return False
    synchronize = 0x00100000
    process = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
    if not process:
        return True
    try:
        result = ctypes.windll.kernel32.WaitForSingleObject(
            process, max(0, int(timeout * 1000))
        )
        return result == 0
    finally:
        ctypes.windll.kernel32.CloseHandle(process)


def _terminate_current_app_if_verified(plan: InstallPlan) -> bool:
    """Force-close only the exact packaged app recorded in the update plan.

    A frozen Tk process can remain alive after its last window disappears when
    a third-party worker is stuck.  The old updater waited silently and then
    reopened the old build.  After a graceful wait we verify the PID's
    executable path before terminating that one process tree.
    """

    if os.name != "nt":
        return False
    script = r"""
$ErrorActionPreference = 'Stop'
$expected = [System.IO.Path]::GetFullPath($env:DOCURANDOM_UPDATE_CURRENT_EXE)
$pidValue = [int]$env:DOCURANDOM_UPDATE_CURRENT_PID
$process = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $pidValue) -ErrorAction SilentlyContinue
if ($null -eq $process) { exit 0 }
$actual = [System.IO.Path]::GetFullPath([string]$process.ExecutablePath)
if (-not $actual.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)) { exit 7 }
& taskkill.exe /PID $pidValue /T /F | Out-Null
if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 128) { exit $LASTEXITCODE }
exit 0
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    environment = os.environ.copy()
    environment["DOCURANDOM_UPDATE_CURRENT_EXE"] = str(
        (plan.install_dir / APP_EXECUTABLE).resolve(strict=True)
    )
    environment["DOCURANDOM_UPDATE_CURRENT_PID"] = str(plan.current_pid)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=15.0,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _close_explorer_windows_in_tree(install_dir: Path) -> None:
    """Close only File Explorer windows browsing inside the install tree.

    Explorer keeps directory handles open while showing a folder. Windows then
    refuses the atomic install-directory rename even though the app itself has
    exited. The path is passed through a child-only environment variable rather
    than interpolated into PowerShell source.
    """

    if os.name != "nt":
        return
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$root = [System.IO.Path]::GetFullPath($env:DOCURANDOM_UPDATE_INSTALL_ROOT).TrimEnd('\')
$prefix = $root + '\'
$shellApp = New-Object -ComObject Shell.Application
foreach ($window in @($shellApp.Windows())) {
    try {
        $url = [string]$window.LocationURL
        if (-not $url -or -not $url.StartsWith('file:', [System.StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        $path = [System.Uri]::UnescapeDataString(([System.Uri]$url).LocalPath).Replace('/', '\').TrimEnd('\')
        if ($path.Equals($root, [System.StringComparison]::OrdinalIgnoreCase) -or
            $path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $window.Quit()
        }
    } catch {}
}
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    environment = os.environ.copy()
    environment["DOCURANDOM_UPDATE_INSTALL_ROOT"] = str(install_dir.resolve(strict=True))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=10.0,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        # Retry below still handles short-lived locks if PowerShell is missing
        # or Explorer cannot be automated in the current Windows session.
        pass


def _stop_processes_in_tree(install_dir: Path) -> None:
    """Stop every surviving process whose executable belongs to this app.

    Packaged background modes reuse ``DocuRandomStudio.exe`` (for example the
    local Flow backend), while Electron and media helpers use their own names.
    Matching a short allow-list therefore left legitimate child processes
    alive and Windows refused to rename the application directory.  Basing the
    boundary on the resolved executable path catches every packaged helper but
    cannot terminate programs installed anywhere else on the computer.
    """

    if os.name != "nt":
        return
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$root = [System.IO.Path]::GetFullPath($env:DOCURANDOM_UPDATE_INSTALL_ROOT).TrimEnd('\')
$prefix = $root + '\'
$updaterPid = [int]$env:DOCURANDOM_UPDATE_UPDATER_PID
$stopped = New-Object System.Collections.Generic.List[int]
foreach ($process in @(Get-CimInstance Win32_Process)) {
    try {
        $pidValue = [int]$process.ProcessId
        if ($pidValue -eq $updaterPid) { continue }
        $path = [System.IO.Path]::GetFullPath([string]$process.ExecutablePath)
        if ($path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
            $stopped.Add($pidValue)
        }
    } catch {}
}
if ($stopped.Count -gt 0) {
    Wait-Process -Id $stopped.ToArray() -Timeout 12 -ErrorAction SilentlyContinue
}
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    environment = os.environ.copy()
    environment["DOCURANDOM_UPDATE_INSTALL_ROOT"] = str(install_dir.resolve(strict=True))
    environment["DOCURANDOM_UPDATE_UPDATER_PID"] = str(os.getpid())
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=15.0,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        pass


# Compatibility for update plans/tests created by builds before 2.3.13.  The
# implementation is intentionally the new path-bounded process sweep.
_stop_packaged_helpers_in_tree = _stop_processes_in_tree


def _replace_directory_with_retry(
    source: Path,
    destination: Path,
    *,
    timeout: float = FILE_UNLOCK_WAIT_SECONDS,
    retry_interval: float = FILE_UNLOCK_RETRY_INTERVAL,
) -> None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            retryable = os.name == "nt" and getattr(exc, "winerror", None) in WINDOWS_RETRYABLE_FILE_ERRORS
            if not retryable or time.monotonic() >= deadline:
                raise
            time.sleep(max(0.0, retry_interval))


def _safe_remove_tree(path: Path, *, expected_parent: Path, marker_required: bool) -> None:
    resolved = path.resolve(strict=True)
    parent = expected_parent.resolve(strict=True)
    if resolved.parent != parent or resolved == parent:
        raise InstallError("Từ chối xóa thư mục ngoài vùng cập nhật dự kiến.")
    if marker_required and not (resolved / ".docurandom-update-backup.json").is_file():
        raise InstallError("Bản sao lưu cũ không có dấu xác minh.")
    shutil.rmtree(resolved)


def _move_directory(source: Path, destination: Path) -> None:
    try:
        os.replace(source, destination)
    except OSError:
        shutil.copytree(source, destination)
        _safe_remove_tree(source, expected_parent=source.parent, marker_required=False)


def _clear_directory_contents(
    directory: Path,
    *,
    timeout: float = FILE_UNLOCK_WAIT_SECONDS,
    require_executable: bool = True,
) -> None:
    """Clear children while retaining a Windows directory that may be open.

    File Explorer and antivirus software can keep a handle to the ``App``
    directory itself.  Windows then rejects a directory rename even though all
    application files are replaceable.  Retaining the root and removing its
    children provides a reliable bounded fallback.
    """

    resolved = directory.resolve(strict=True)
    if resolved == Path(resolved.anchor) or (
        require_executable and not (resolved / APP_EXECUTABLE).is_file()
    ):
        raise InstallError("Từ chối làm sạch thư mục không phải runtime DocuRandom Studio.")
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        failures: list[OSError] = []
        for child in tuple(resolved.iterdir()):
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
            except OSError as exc:
                failures.append(exc)
        if not failures:
            return
        if time.monotonic() >= deadline:
            raise failures[-1]
        time.sleep(FILE_UNLOCK_RETRY_INTERVAL)


def _copy_runtime(source: Path, destination: Path) -> None:
    source_root = source.resolve(strict=True)
    if not (source_root / APP_EXECUTABLE).is_file():
        raise InstallError("Runtime nguồn không đầy đủ.")
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, destination, dirs_exist_ok=True)


def _create_verified_backup_copy(source: Path, backup: Path, version: str) -> None:
    copying = backup.with_name(f"{backup.name}.copying")
    if copying.exists():
        _safe_remove_tree(
            copying,
            expected_parent=backup.parent,
            marker_required=False,
        )
    try:
        shutil.copytree(source, copying)
        if not (copying / APP_EXECUTABLE).is_file():
            raise InstallError("Bản sao lưu runtime cũ không đầy đủ.")
        (copying / ".docurandom-update-backup.json").write_text(
            json.dumps({"version": version, "created_at": time.time()}),
            encoding="utf-8",
        )
        _replace_directory_with_retry(copying, backup)
    except Exception:
        if copying.exists():
            try:
                _safe_remove_tree(
                    copying,
                    expected_parent=backup.parent,
                    marker_required=False,
                )
            except (InstallError, OSError):
                pass
        raise


def _write_install_result(
    plan: InstallPlan,
    *,
    status: str,
    detail: str,
    method: str = "",
) -> None:
    result_path = plan.health_path.parent / INSTALL_RESULT_NAME
    temporary = result_path.with_name(f".{INSTALL_RESULT_NAME}.{os.getpid()}.tmp")
    payload = {
        "schema": 1,
        "status": status,
        "target_version": plan.version,
        "detail": str(detail)[:4000],
        "method": method,
        "install_dir": str(plan.install_dir),
        "updated_at": time.time(),
    }
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, result_path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _friendly_install_error(detail: str) -> str:
    value = str(detail).strip()
    normalized = value.casefold()
    if any(
        marker in normalized
        for marker in (
            "locked",
            "access is denied",
            "being used by another process",
            "winerror 5",
            "winerror 32",
            "winerror 33",
        )
    ):
        return (
            "Windows vẫn đang giữ một file của DocuRandom Studio. Tool đã giữ "
            "nguyên bản cũ và sẽ dùng lại gói cập nhật ở lần thử tiếp theo."
        )
    return value or "Windows chưa cho phép thay thế runtime DocuRandom Studio."


def _show_install_failure(detail: str) -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            "Tool đã tự mở lại bản cũ an toàn vì chưa thể cài bản mới.\n\n"
            + str(detail)[:3000]
            + "\n\nGói cập nhật vẫn được giữ; hãy bấm Cập nhật lại, không phải tải lại từ đầu.",
            "Cập nhật chưa hoàn tất",
            0x10,
        )
    except Exception:
        pass


def _launch(executable: Path, args: list[str]) -> subprocess.Popen[bytes]:
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [str(executable), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


def _wait_for_health(plan: InstallPlan, process: subprocess.Popen[bytes]) -> bool:
    deadline = time.monotonic() + HEALTH_WAIT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            value = json.loads(plan.health_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.25)
            continue
        return bool(
            value.get("ok") is True
            and value.get("token") == plan.health_token
            and value.get("version") == plan.version
        )
    return False


def _cleanup_successful_install(plan: InstallPlan, backup: Path) -> None:
    """Best-effort cleanup after the new build has proved it can start.

    Cleanup must never turn an already healthy update into a rollback.  Large
    files are removed first; the running updater executable may remain until a
    later startup because Windows does not allow a process to delete itself.
    """

    try:
        if backup.exists():
            _safe_remove_tree(
                backup,
                expected_parent=plan.install_dir.parent,
                marker_required=True,
            )
    except (InstallError, OSError):
        pass
    update_version_root = plan.health_path.parent
    for name in (
        "DocuRandomStudio-portable.zip",
        "install-error.txt",
        "install-plan.json",
    ):
        try:
            (update_version_root / name).unlink(missing_ok=True)
        except OSError:
            pass
    stage_parent = update_version_root / "staged"
    try:
        if stage_parent.exists():
            _safe_remove_tree(
                stage_parent,
                expected_parent=update_version_root,
                marker_required=False,
            )
    except (InstallError, OSError):
        pass


def install_update(plan: InstallPlan) -> str:
    if not wait_for_process_exit(
        plan.current_pid,
        timeout=GRACEFUL_PROCESS_WAIT_SECONDS,
    ):
        if not _terminate_current_app_if_verified(plan) or not wait_for_process_exit(
            plan.current_pid,
            timeout=FORCED_PROCESS_WAIT_SECONDS,
        ):
            raise InstallError(
                "Ứng dụng cũ chưa đóng được an toàn. Hãy đóng tool rồi bấm cập nhật lại."
            )
    try:
        plan.health_path.unlink(missing_ok=True)
    except OSError as exc:
        raise InstallError("Không thể làm sạch kiểm tra sức khỏe cũ.") from exc

    install_parent = plan.install_dir.parent
    backup = install_parent / f".{plan.install_dir.name}.rollback"
    if backup.exists():
        _safe_remove_tree(backup, expected_parent=install_parent, marker_required=True)

    _stop_processes_in_tree(plan.install_dir)
    _close_explorer_windows_in_tree(plan.install_dir)
    install_method = "directory-swap"
    try:
        _replace_directory_with_retry(plan.install_dir, backup)
    except OSError as exc:
        # The common WinError 5/32 case is a handle to the App directory rather
        # than a locked executable. Keep the root, make a verified rollback
        # copy, then replace its contents in place.
        install_method = "in-place-fallback"
        try:
            _create_verified_backup_copy(plan.install_dir, backup, plan.version)
            _clear_directory_contents(plan.install_dir)
            _copy_runtime(plan.staged_dir, plan.install_dir)
        except Exception as fallback_exc:
            if backup.exists():
                try:
                    if plan.install_dir.exists():
                        _clear_directory_contents(
                            plan.install_dir,
                            require_executable=False,
                        )
                    else:
                        plan.install_dir.mkdir(parents=True)
                    _copy_runtime(backup, plan.install_dir)
                    (plan.install_dir / ".docurandom-update-backup.json").unlink(
                        missing_ok=True
                    )
                    _safe_remove_tree(
                        backup,
                        expected_parent=install_parent,
                        marker_required=True,
                    )
                except Exception:
                    pass
            raise InstallError(
                "Windows vẫn đang khóa các file của tool nên chưa thể cài đè. "
                "Bản cũ đã được giữ nguyên."
            ) from fallback_exc
    else:
        (backup / ".docurandom-update-backup.json").write_text(
            json.dumps({"version": plan.version, "created_at": time.time()}),
            encoding="utf-8",
        )
    installed_new = False
    new_process: subprocess.Popen[bytes] | None = None
    try:
        if install_method == "directory-swap":
            plan.install_dir.mkdir(parents=True)
            installed_new = True
            _copy_runtime(plan.staged_dir, plan.install_dir)
        else:
            installed_new = True
        (plan.install_dir / VERSION_MARKER).write_text(plan.version, encoding="ascii")
        executable = plan.install_dir / APP_EXECUTABLE
        new_process = _launch(
            executable,
            ["--update-health", str(plan.health_path), plan.health_token],
        )
        if not _wait_for_health(plan, new_process):
            raise InstallError("Bản mới không khởi động ổn định trong thời gian cho phép.")
        _write_install_result(
            plan,
            status="success",
            detail=f"Đã cài và xác minh DocuRandom Studio {plan.version}.",
            method=install_method,
        )
        _cleanup_successful_install(plan, backup)
        return install_method
    except Exception:
        if new_process is not None and new_process.poll() is None:
            try:
                new_process.terminate()
                new_process.wait(timeout=8)
            except Exception:
                try:
                    new_process.kill()
                    new_process.wait(timeout=8)
                except Exception:
                    pass
        _stop_processes_in_tree(plan.install_dir)
        try:
            if installed_new and plan.install_dir.exists():
                if install_method == "directory-swap":
                    _safe_remove_tree(
                        plan.install_dir,
                        expected_parent=install_parent,
                        marker_required=False,
                    )
                elif install_method == "in-place-fallback":
                    _clear_directory_contents(
                        plan.install_dir,
                        require_executable=False,
                    )
            if backup.exists():
                if install_method == "in-place-fallback":
                    plan.install_dir.mkdir(parents=True, exist_ok=True)
                    _copy_runtime(backup, plan.install_dir)
                    _safe_remove_tree(
                        backup,
                        expected_parent=install_parent,
                        marker_required=True,
                    )
                elif not plan.install_dir.exists():
                    _replace_directory_with_retry(backup, plan.install_dir)
                try:
                    (plan.install_dir / ".docurandom-update-backup.json").unlink(
                        missing_ok=True
                    )
                except OSError:
                    pass
        except Exception as rollback_exc:
            raise InstallError(
                "Bản mới không qua kiểm tra và Windows cản trở phục hồi tự động. "
                f"Bản sao an toàn vẫn ở: {backup}"
            ) from rollback_exc
        raise


def _relaunch_installed_app(plan: InstallPlan | None) -> None:
    """Reopen the usable build after any unsuccessful update attempt."""

    if plan is None:
        return
    executable = plan.install_dir / APP_EXECUTABLE
    try:
        if executable.is_file():
            _launch(executable, [])
    except OSError:
        pass


def cleanup_installer_launcher(source: Path, install_dir: Path) -> bool:
    """Remove the one-file installer after it has launched the managed app.

    The installer creates a normal Desktop shortcut.  Keeping the downloaded
    bootstrap EXE beside that shortcut makes low-tech users see two apparently
    identical applications.  Only the exact signed-release filename is
    eligible, and the managed runtime itself can never be selected.
    """

    source = source.expanduser().resolve(strict=False)
    install_dir = install_dir.expanduser().resolve(strict=False)
    if source.name.casefold() != APP_EXECUTABLE.casefold():
        return False
    if source.parent == source or _is_within(source, install_dir):
        return False
    deadline = time.monotonic() + LAUNCHER_CLEANUP_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            source.unlink(missing_ok=True)
            return not source.exists()
        except OSError as exc:
            if not _is_retryable_file_error(exc):
                return False
            time.sleep(FILE_UNLOCK_RETRY_INTERVAL)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", type=Path)
    mode.add_argument("--cleanup-installer", type=Path)
    parser.add_argument("--install-dir", type=Path)
    args = parser.parse_args(argv)
    if args.cleanup_installer is not None:
        if args.install_dir is None:
            return 2
        return 0 if cleanup_installer_launcher(args.cleanup_installer, args.install_dir) else 1
    plan: InstallPlan | None = None
    try:
        plan = load_plan(args.plan)
        _write_install_result(
            plan,
            status="installing",
            detail="Đang đóng bản cũ và áp dụng bản cập nhật.",
        )
        install_update(plan)
        try:
            (args.plan.resolve(strict=False).parent / "install-error.txt").unlink(
                missing_ok=True
            )
        except OSError:
            pass
        return 0
    except Exception as exc:
        detail = _friendly_install_error(str(exc))
        try:
            error_path = args.plan.resolve(strict=False).parent / "install-error.txt"
            error_path.write_text(detail, encoding="utf-8")
        except OSError:
            pass
        if plan is not None:
            _write_install_result(
                plan,
                status="failed",
                detail=detail,
            )
        _relaunch_installed_app(plan)
        if plan is None or not plan.source_version:
            # Plans produced by 2.3.15 and older cannot read the durable result
            # receipt, so the target updater must explain the failure itself.
            _show_install_failure(detail)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
