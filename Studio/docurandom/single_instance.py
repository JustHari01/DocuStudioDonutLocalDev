from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from typing import Callable, Protocol


WINDOW_TITLE = "DocuRandom Studio"
MUTEX_NAME = r"Local\DocuRandomStudio.SingleWindow.v2"
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9


class _User32(Protocol):
    def FindWindowW(self, class_name: object, window_name: str) -> int: ...

    def IsWindowVisible(self, window: int) -> int: ...

    def IsIconic(self, window: int) -> int: ...

    def ShowWindow(self, window: int, command: int) -> int: ...

    def BringWindowToTop(self, window: int) -> int: ...

    def SetForegroundWindow(self, window: int) -> int: ...


def _running_on_windows() -> bool:
    return os.name == "nt"


def _open_windows_mutex() -> tuple[int, bool, Callable[[int], None]]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    ctypes.set_last_error(0)
    raw_handle = create_mutex(None, False, MUTEX_NAME)
    error = ctypes.get_last_error()
    if not raw_handle:
        raise OSError(error, "Không thể tạo khóa một cửa sổ của DocuRandom Studio.")
    handle = int(raw_handle)

    def closer(value: int) -> None:
        close_handle(ctypes.c_void_p(value))

    return handle, error == ERROR_ALREADY_EXISTS, closer


@dataclass
class SingleInstanceGuard:
    """Own the named mutex handle for the lifetime of the main UI process."""

    already_running: bool = False
    _handle: int | None = None
    _closer: Callable[[int], None] | None = None
    acquisition_failed: bool = False

    @classmethod
    def acquire(cls) -> "SingleInstanceGuard":
        if not _running_on_windows():
            return cls()
        try:
            handle, already_running, closer = _open_windows_mutex()
        except (AttributeError, OSError, TypeError, ValueError):
            # The launcher will look for an existing window before deliberately
            # failing open. This covers cross-integrity access-denied cases where
            # the elevated process owns the mutex but a normal process launched
            # by the user cannot open it.
            return cls(acquisition_failed=True)
        return cls(
            already_running=already_running,
            _handle=handle,
            _closer=closer,
        )

    def close(self) -> None:
        handle = self._handle
        closer = self._closer
        self._handle = None
        self._closer = None
        if handle is not None and closer is not None:
            try:
                closer(handle)
            except (OSError, ValueError):
                pass

    def __enter__(self) -> "SingleInstanceGuard":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


def _load_user32() -> _User32:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = ctypes.c_void_p
    for function_name in (
        "IsWindowVisible",
        "IsIconic",
        "ShowWindow",
        "BringWindowToTop",
        "SetForegroundWindow",
    ):
        function = getattr(user32, function_name)
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return user32


@dataclass(frozen=True)
class WindowFocusResult:
    """Observed Win32 activation results for an exact-title top-level window.

    ``handled`` deliberately means that the exact window was found. Windows may
    legally reject a foreground request, but a second launcher must still exit
    instead of creating another UI. Individual fields preserve the API results
    so callers/tests do not confuse "found" with "successfully foregrounded".
    """

    found: bool = False
    visible: bool = False
    minimized: bool = False
    show_window_result: bool | None = None
    bring_to_top_result: bool | None = None
    foreground_result: bool | None = None

    @property
    def handled(self) -> bool:
        return self.found

    @property
    def activated(self) -> bool:
        return bool(self.foreground_result or self.bring_to_top_result)

    def __bool__(self) -> bool:
        return self.handled


def _focus_once(user32: _User32, title: str) -> WindowFocusResult:
    try:
        raw_window = user32.FindWindowW(None, title)
    except (AttributeError, OSError, TypeError, ValueError):
        return WindowFocusResult()
    if not raw_window:
        return WindowFocusResult()

    try:
        window = int(raw_window)
    except (TypeError, ValueError):
        return WindowFocusResult()
    try:
        visible = bool(user32.IsWindowVisible(window))
    except (AttributeError, OSError, TypeError, ValueError):
        visible = False
    if not visible:
        # Finding the exact hidden/startup window is enough to prevent another
        # application window. It is not force-shown because it may be mid-setup.
        return WindowFocusResult(found=True, visible=False)

    try:
        minimized = bool(user32.IsIconic(window))
    except (AttributeError, OSError, TypeError, ValueError):
        minimized = False

    show_result: bool | None = None
    if minimized:
        try:
            # ShowWindow returns the previous visibility state, not a strict
            # success flag; expose the raw boolean semantics under that name.
            show_result = bool(user32.ShowWindow(window, SW_RESTORE))
        except (AttributeError, OSError, TypeError, ValueError):
            show_result = False
    try:
        bring_result: bool | None = bool(user32.BringWindowToTop(window))
    except (AttributeError, OSError, TypeError, ValueError):
        bring_result = False
    try:
        foreground_result: bool | None = bool(user32.SetForegroundWindow(window))
    except (AttributeError, OSError, TypeError, ValueError):
        foreground_result = False
    return WindowFocusResult(
        found=True,
        visible=True,
        minimized=minimized,
        show_window_result=show_result,
        bring_to_top_result=bring_result,
        foreground_result=foreground_result,
    )


def focus_existing_window(
    title: str = WINDOW_TITLE,
    *,
    retry_attempts: int = 0,
    retry_delay: float = 0.075,
) -> WindowFocusResult:
    """Find, restore and foreground an exact-title window with bounded retries."""

    if not _running_on_windows():
        return WindowFocusResult()
    try:
        user32 = _load_user32()
    except (AttributeError, OSError, TypeError, ValueError):
        return WindowFocusResult()

    attempts = max(0, min(30, int(retry_attempts)))
    delay = max(0.0, min(0.25, float(retry_delay)))
    result = WindowFocusResult()
    for attempt in range(attempts + 1):
        result = _focus_once(user32, title)
        if result.handled:
            return result
        if attempt < attempts and delay > 0:
            time.sleep(delay)
    return result
