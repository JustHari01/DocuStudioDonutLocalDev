from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed

from .browser_extension_bridge import (
    BrowserExtensionBridge,
    BrowserExtensionBridgeError,
)
from .donut_mcp import (
    DonutMcpAuthenticationError,
    DonutMcpCapacityError,
    DonutMcpClient,
    DonutMcpEndpoint,
    DonutMcpEntitlementError,
    DonutMcpError,
    DonutMcpPriorLaunchUnresolved,
    DonutMcpTimeoutError,
)
from .smm_api import (
    SmmApiClient,
    SmmProviderRejectedError,
    SmmService,
)
from .storage import app_data_dir, atomic_write_json
from .youtube_upload import (
    ProfileUploadCoordinator,
    YouTubeUploadCancelled,
    _CdpSession,
    _mcp_close_cdp_page_target,
    _cdp_page_target_id,
    _browser_extension_tab_for_target,
    _ensure_youtube_browser_bridge,
    _mcp_open_minimized_cdp_window_ws_url,
    _mcp_set_cdp_window_state,
    _youtube_json_values,
)


YOUTUBE_STUDIO_URL = "https://studio.youtube.com/"
logger = logging.getLogger(__name__)
_VIDEO_STUDIO_URL = re.compile(
    r"^https://studio\.youtube\.com/video/([A-Za-z0-9_-]{6,20})/"
)
_VIDEO_THUMBNAIL_URL = re.compile(
    r"/vi(?:_webp)?/([A-Za-z0-9_-]{6,20})/",
    re.IGNORECASE,
)
_CHECKVIEW_DOM_STALE_RETRIES = 3
_CHECKVIEW_EXTENSION_PAGE_TIMEOUT = 45.0
_CHECKVIEW_PAGE_STABLE_READS = 2
_CHANNEL_STUDIO_URL = re.compile(
    r"^https://studio\.youtube\.com/channel/([A-Za-z0-9_-]+)"
)
_NUMBER = re.compile(r"\d(?:[\d.,\u00a0\u202f ]*\d)?")
_STATE_LOCK = threading.RLock()
_CHECKVIEW_TARGET_LOCK = threading.RLock()
_CHECKVIEW_TARGETS_PATH = app_data_dir() / "youtube_checkview_targets.json"
_CHECKVIEW_PROFILE_OWNERS_PATH = app_data_dir() / "youtube_checkview_profiles.json"


def _checkview_process_birth(process_id: int) -> int | None:
    """Identify the exact Windows process, including after PID reuse."""
    if os.name != "nt" or not isinstance(process_id, int) or process_id <= 0:
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, process_id)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def _load_checkview_profile_owners() -> dict[str, dict[str, object]]:
    try:
        raw = json.loads(_CHECKVIEW_PROFILE_OWNERS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise YouTubeRealtimeError("Không đọc được sổ phiên Checkview; không nhận quyền đóng profile.") from exc
    profiles = raw.get("profiles") if isinstance(raw, dict) else None
    if not isinstance(profiles, dict):
        raise YouTubeRealtimeError("Sổ phiên Checkview sai cấu trúc; không nhận quyền đóng profile.")
    return {key: value for key, value in profiles.items() if isinstance(key, str) and isinstance(value, dict)}


def _remember_checkview_profile(profile_id: str, process_id: int) -> None:
    birth = _checkview_process_birth(process_id)
    if birth is None:
        return
    with _CHECKVIEW_TARGET_LOCK:
        profiles = _load_checkview_profile_owners()
        profiles[profile_id] = {"process_id": process_id, "process_birth": birth,
                                "opened_at": datetime.now(timezone.utc).isoformat()}
        atomic_write_json(_CHECKVIEW_PROFILE_OWNERS_PATH, {"version": 1, "profiles": profiles})


def _forget_checkview_profile(profile_id: str) -> None:
    with _CHECKVIEW_TARGET_LOCK:
        profiles = _load_checkview_profile_owners()
        if profile_id not in profiles:
            return
        profiles.pop(profile_id)
        atomic_write_json(_CHECKVIEW_PROFILE_OWNERS_PATH, {"version": 1, "profiles": profiles})
_NEXT_PAGE_MARKERS = (
    "next page",
    "go to next page",
    "trang tiep theo",
    "trang ke tiep",
    "chuyen den trang tiep theo",
    "次のページ",
    "次へ",
    "다음 페이지",
    "下一页",
    "下一頁",
)
_SIXTY_MINUTE_PATTERNS = (
    re.compile(r"\b60\s*(?:minutes?|mins?|min)\b", re.IGNORECASE),
    re.compile(r"\b60\s*phut\b", re.IGNORECASE),
    re.compile(r"60\s*(?:分|분|分钟|分鐘)"),
)
_FORTY_EIGHT_HOUR_PATTERNS = (
    re.compile(r"\b48\s*(?:hours?|hrs?|hr)\b", re.IGNORECASE),
    re.compile(r"\b48\s*(?:gio|h)\b", re.IGNORECASE),
    re.compile(r"48\s*(?:時間|시간|小时|小時)"),
)


def _is_retryable_order_record(record: Mapping[str, object]) -> bool:
    """Return whether a previous explicit provider rejection may be retried.

    Older checkpoints recorded every add-order exception as ``uncertain``.
    Only a provider response containing an error is safe to retry: it proves
    that 1MM rejected the request and returned no order id.  A timeout,
    dropped connection, or malformed response can happen after the provider
    accepted the order and must remain fail-closed.
    """

    if str(record.get("status") or "") == "retryable":
        return True
    if str(record.get("status") or "") != "uncertain":
        return False
    detail = " ".join(str(record.get("detail") or "").split()).casefold()
    return any(
        marker in detail
        for marker in (
            "not enough funds",
            "insufficient funds",
            "insufficient balance",
            "not enough balance",
            "insufficient credit",
            "không đủ số dư",
            "khong du so du",
            "số dư không đủ",
            "so du khong du",
        )
    )

# YouTube Studio's range picker is a Polymer component.  Wayfern accepts the
# read-only CDP/Accessibility calls on a parked target, but its native CDP
# Input events do not open this component when the window is not foreground.
# The paired Donut extension can execute the same semantic DOM action in the
# target without activating its browser window, so checkview can stay on the
# user's automation desktop.
_CHECKVIEW_SELECT_60_SCRIPT = r"""
(async () => {
  const normalize = (value) => String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase();
  const roots = [];
  const seenRoots = new Set();
  const visitRoots = (root) => {
    if (!root || !root.querySelectorAll || seenRoots.has(root)) return;
    seenRoots.add(root);
    roots.push(root);
    for (const element of root.querySelectorAll("*")) {
      if (element.shadowRoot) visitRoots(element.shadowRoot);
    }
  };
  const deepAll = (selector) => {
    visitRoots(document);
    const output = [];
    for (const root of roots) {
      for (const element of root.querySelectorAll(selector)) output.push(element);
    }
    return output;
  };
  const usable = (element) => {
    if (!element || !element.isConnected) return false;
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" &&
      element.getAttribute("aria-hidden") !== "true";
  };
  const textOf = (element) => normalize([
    element?.getAttribute?.("aria-label") || "",
    element?.getAttribute?.("title") || "",
    element?.innerText || "",
    element?.textContent || "",
  ].join(" "));
  const is60 = (value) => {
    const text = normalize(value);
    return /\b60\s*(?:minutes?|mins?|min|phut)\b/.test(text) ||
      /60\s*(?:分|분|分钟|分鐘)/.test(text);
  };
  const is48 = (value) => {
    const text = normalize(value);
    return /\b48\s*(?:hours?|hrs?|hr|gio|h)\b/.test(text) ||
      /48\s*(?:時間|시간|小时|小時)/.test(text);
  };
  const controls = () => deepAll(
    "ytcp-dropdown-trigger,button,[role='button'],[role='tab'],[role='radio'],[role='combobox']",
  ).filter(usable);
  const rangeControl = (minutes) => {
    const wanted = minutes === 60 ? is60 : is48;
    return controls()
      .filter((element) => {
        const label = textOf(element);
        return wanted(label) && !(minutes === 60 && is48(label));
      })
      .sort((left, right) => {
        const score = (element) =>
          (element.matches("[role='button'],button") ? 4 : 0) +
          (element.getAttribute("tabindex") !== null ? 1 : 0) +
          (element.tagName !== "YTCP-DROPDOWN-TRIGGER" ? 2 : 0);
        return score(right) - score(left);
      });
  };
  const menuOption = (minutes) => {
    const wanted = minutes === 60 ? is60 : is48;
    return deepAll(
      "tp-yt-paper-item,tp-yt-paper-radio-button,[role='option'],[role='menuitem'],[role='menuitemradio'],[role='radio']",
    ).filter(usable)
      .filter((element) => {
        const label = textOf(element);
        return wanted(label) && !(minutes === 60 && is48(label));
      })
      .sort((left, right) => textOf(left).length - textOf(right).length);
  };
  const snapshot = () => {
    const selected60 = rangeControl(60)[0] || null;
    const selected48 = rangeControl(48)[0] || null;
    const report = deepAll("*")
      .filter(usable)
      .map(textOf)
      .find((value) => value.includes("hoat dong gan day nhat") &&
        (is60(value) || is48(value))) || "";
    return {
      control60: Boolean(selected60),
      control48: Boolean(selected48),
      controlLabel: textOf(selected60 || selected48),
      reportLabel: report,
    };
  };
  const pause = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
  let state = snapshot();
  if (state.control60) return { ok: true, changed: false, ...state };

  let opened = false;
  for (const trigger of rangeControl(48)) {
    trigger.click();
    await pause(180);
    if (menuOption(60).length) {
      opened = true;
      break;
    }
  }
  if (!opened) {
    return { ok: false, error: "CHECKVIEW_RANGE_MENU_NOT_OPENED", ...snapshot() };
  }
  const option = menuOption(60)[0];
  option.click();
  const deadline = Date.now() + 6000;
  do {
    await pause(250);
    state = snapshot();
    if (state.control60) return { ok: true, changed: true, ...state };
  } while (Date.now() < deadline);
  return { ok: false, error: "CHECKVIEW_60_RANGE_NOT_CONFIRMED", ...state };
})()
"""


def _load_checkview_target_records() -> dict[str, dict[str, object]]:
    try:
        raw = json.loads(_CHECKVIEW_TARGETS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    profiles = raw.get("profiles") if isinstance(raw, Mapping) else None
    if not isinstance(profiles, Mapping):
        return {}
    records: dict[str, dict[str, object]] = {}
    for profile_id, item in profiles.items():
        if not isinstance(profile_id, str) or not isinstance(item, Mapping):
            continue
        target_id = str(item.get("target_id") or "").strip()
        window_id = item.get("window_id")
        tab_id = item.get("tab_id")
        if not target_id or not isinstance(window_id, int) or not isinstance(tab_id, int):
            continue
        records[profile_id] = {
            "target_id": target_id,
            "window_id": window_id,
            "tab_id": tab_id,
        }
    return records


_CHECKVIEW_TARGET_RECORDS = _load_checkview_target_records()
_CHECKVIEW_WINDOW_STATE = "minimized"


def _save_checkview_target_record(
    profile_id: str,
    *,
    target_id: str,
    window_id: int,
    tab_id: int,
) -> None:
    with _CHECKVIEW_TARGET_LOCK:
        _CHECKVIEW_TARGET_RECORDS[profile_id] = {
            "target_id": str(target_id),
            "window_id": int(window_id),
            "tab_id": int(tab_id),
        }
        atomic_write_json(
            _CHECKVIEW_TARGETS_PATH,
            {"version": 1, "profiles": _CHECKVIEW_TARGET_RECORDS},
        )


def _forget_checkview_target_record(
    profile_id: str,
) -> dict[str, object] | None:
    """Drop one persisted checkview target after its transport is dead.

    The record is only a reuse hint.  Keeping it after a closed CDP socket
    makes the next scan repeatedly reconnect to the same dead target, so a
    transport recovery must remove the hint before creating a replacement.
    """

    normalized_profile_id = str(profile_id or "").strip()
    if not normalized_profile_id:
        return None
    with _CHECKVIEW_TARGET_LOCK:
        saved = _CHECKVIEW_TARGET_RECORDS.pop(normalized_profile_id, None)
        if not isinstance(saved, Mapping):
            return None
        atomic_write_json(
            _CHECKVIEW_TARGETS_PATH,
            {"version": 1, "profiles": _CHECKVIEW_TARGET_RECORDS},
        )
        return dict(saved)


def _read_debug_targets(debug: Mapping[str, object]) -> list[Mapping[str, object]]:
    targets_url = str(debug.get("targets_url") or "").strip()
    try:
        parsed = urlsplit(targets_url)
    except ValueError as exc:
        raise YouTubeRealtimeError("Donut trả danh sách tab CDP không hợp lệ.") from exc
    if parsed.scheme.casefold() != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise YouTubeRealtimeError("Từ chối danh sách tab CDP không nằm trên máy này.")
    try:
        with urllib.request.urlopen(targets_url, timeout=3.0) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise YouTubeRealtimeError("Không đọc được danh sách target checkview của Donut.") from exc
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _debug_target(
    debug: Mapping[str, object],
    target_id: str,
) -> Mapping[str, object] | None:
    expected = str(target_id or "").strip()
    if not expected:
        return None
    for item in _read_debug_targets(debug):
        if str(item.get("id") or "") != expected or item.get("type") != "page":
            continue
        websocket_url = str(item.get("webSocketDebuggerUrl") or "").strip()
        if not websocket_url:
            continue
        try:
            if _cdp_page_target_id(websocket_url) != expected:
                continue
        except Exception:
            continue
        return item
    return None


def _target_window_id(
    client: DonutMcpClient,
    profile_id: str,
    target_id: str,
) -> int:
    try:
        window = client.cdp_command(
            profile_id,
            "Browser.getWindowForTarget",
            {"targetId": str(target_id)},
        )
    except Exception as exc:
        raise YouTubeRealtimeError(
            "Donut không xác định được cửa sổ checkview đã ghim."
        ) from exc
    window_id = window.get("windowId") if isinstance(window, Mapping) else None
    if not isinstance(window_id, int):
        raise YouTubeRealtimeError(
            "Donut không trả đúng windowId cho tab checkview đã ghim."
        )
    return window_id


def _extension_tab_for_window(
    bridge: BrowserExtensionBridge,
    client: DonutMcpClient,
    profile_id: str,
    target_id: str,
    window_id: int,
    stored_tab_id: int,
) -> int:
    """Resolve the exact hidden tab without selecting a user-facing tab."""

    try:
        tabs = bridge.command(profile_id, "tabs_list", timeout=5)
    except BrowserExtensionBridgeError as exc:
        raise YouTubeRealtimeError(
            "DocuRandom Donut Bridge chưa đọc được tab checkview nền."
        ) from exc
    items = tabs if isinstance(tabs, list) else []
    candidates = [
        item
        for item in items
        if isinstance(item, Mapping)
        and item.get("windowId") == window_id
        and isinstance(item.get("id"), int)
    ]
    if isinstance(stored_tab_id, int):
        for item in candidates:
            if item.get("id") == stored_tab_id:
                return stored_tab_id
    if len(candidates) == 1:
        return int(candidates[0]["id"])
    try:
        _mapped_bridge, tab_id = _browser_extension_tab_for_target(
            client,
            profile_id,
            target_id,
            bridge=bridge,
            timeout=8,
        )
        return tab_id
    except Exception as exc:
        raise YouTubeRealtimeError(
            "Không ánh xạ chắc chắn được tab checkview nền; tool không chọn đại tab."
        ) from exc


class YouTubeRealtimeError(RuntimeError):
    pass


class YouTubeRealtimeDeferred(YouTubeRealtimeError):
    """A periodic scan yielded the profile to a higher-priority upload."""


class _CheckviewScanLane:
    """Fair, bounded access to the background YouTube Studio scan lanes.

    Every armed workspace can wake at the same scheduler tick. A plain
    ``threading.Lock`` lets a worker that just finished compete with older
    waiters, which can make one profile repeatedly win while another profile
    never reaches its Content/Analytics scan. This FIFO gate keeps browser
    load bounded while guaranteeing that an already queued profile gets a
    turn.

    Uploads still have priority through ``ProfileUploadCoordinator``. The
    lane only governs the read-only checkview part and is intentionally
    limited to two concurrent background scans, matching the coordinator's
    monitor ceiling.
    """

    def __init__(self, *, capacity: int = 2) -> None:
        self.capacity = max(1, int(capacity))
        self._condition = threading.Condition()
        self._pending: deque[int] = deque()
        self._next_ticket = 0
        self._active = 0

    def acquire(
        self,
        cancel_event: threading.Event,
        report: Callable[[str], None],
        *,
        profile_id: str = "",
    ) -> None:
        with self._condition:
            self._next_ticket += 1
            ticket = self._next_ticket
            self._pending.append(ticket)
            last_report_at = 0.0
            try:
                while True:
                    if cancel_event.is_set():
                        raise YouTubeUploadCancelled(
                            "Đã dừng chờ lượt checkview."
                        )
                    position = self._pending.index(ticket) + 1
                    if (
                        self._active < self.capacity
                        and self._pending
                        and self._pending[0] == ticket
                    ):
                        self._pending.popleft()
                        self._active += 1
                        return
                    now = time.monotonic()
                    if now - last_report_at >= 10.0:
                        label = str(profile_id or "profile hiện tại").strip()
                        report(
                            "Đang chờ lượt checkview nền · "
                            f"{label} · vị trí {position} · "
                            f"đang chạy {self._active}/{self.capacity}…"
                        )
                        last_report_at = now
                    self._condition.wait(timeout=0.5)
            except BaseException:
                try:
                    self._pending.remove(ticket)
                except ValueError:
                    # The ticket has already been promoted to an active lane.
                    pass
                self._condition.notify_all()
                raise

    def release(self) -> None:
        with self._condition:
            if self._active > 0:
                self._active -= 1
            self._condition.notify_all()

    def snapshot(self) -> tuple[int, int, int]:
        """Return ``(active, pending, capacity)`` for diagnostics/tests."""

        with self._condition:
            return self._active, len(self._pending), self.capacity


# Keep one scan/launch lane for the local anti-detect browser.  Donut can read
# existing tabs quickly, but concurrent Wayfern starts compete for the same
# proxy/profile manager and can leave every MCP call waiting.  A single fair
# lane still lets the next workspace continue immediately after the current
# scan releases the profile.
_CHECKVIEW_SCAN_LANE = _CheckviewScanLane(capacity=1)


def _is_stale_dom_node_error(error: BaseException) -> bool:
    """Return whether a CDP DOM node ID was invalidated by a re-render.

    YouTube Studio virtualizes its content rows. A node ID returned by one
    ``DOM.getDocument`` snapshot can therefore disappear before the next
    ``DOM.querySelectorAll``/``DOM.describeNode`` call. That is a recoverable
    page-lifecycle event, not evidence that the channel has no videos.
    """

    message = " ".join(str(error or "").casefold().split())
    return "could not find node with given id" in message


def _is_incomplete_checkview_scan_error(error: BaseException) -> bool:
    """Return whether the content list ended before a full page was proven."""

    message = " ".join(str(error or "").casefold().split())
    return "danh sách video youtube chưa tải đầy đủ" in message


def _is_video_analytics_error(error: BaseException) -> bool:
    """Return whether one video's Studio Analytics page failed semantically.

    This is different from a broken checkview transport or an incomplete
    Content pagination result.  Recreating the whole profile for this error
    only repeats the same bad Analytics document and can make a scheduled
    scan look like it is opening and closing profiles in a loop.
    """

    message = " ".join(str(error or "").casefold().split())
    return (
        "youtube studio báo lỗi khi tải analytics của video" in message
        or "youtube studio bao loi khi tai analytics cua video" in message
    )


def _is_transient_checkview_transport_error(error: BaseException) -> bool:
    """Return whether a read-only scan can safely retry its CDP transport.

    A complete scan does not place any order until it has returned all video
    observations, so recreating the parked target is safe here. A stale
    Studio DOM snapshot is also recoverable: the DOM reader retries from a
    fresh document before this outer recovery recreates the target. Other
    semantic page errors, authentication failures and entitlement errors must
    still fail closed instead of being retried as if the socket were broken.
    """

    if isinstance(error, YouTubeUploadCancelled):
        return False
    if isinstance(error, ConnectionClosed):
        return True
    if _is_video_analytics_error(error):
        return False
    if _is_stale_dom_node_error(error):
        return True
    if _is_incomplete_checkview_scan_error(error):
        return True
    message = " ".join(str(error or "").casefold().split())
    return any(
        marker in message
        for marker in (
            "no close frame",
            "connection closed",
            "websocket",
            "broken pipe",
            "connection reset",
            "forcibly closed",
            "remote protocol error",
            "end of file",
        )
    )


def _acquire_checkview_scan_lane(
    cancel_event: threading.Event,
    report: Callable[[str], None],
    *,
    profile_id: str = "",
) -> None:
    """Admit a scan through the fair bounded background queue."""

    _CHECKVIEW_SCAN_LANE.acquire(
        cancel_event,
        report,
        profile_id=profile_id,
    )


def _get_or_create_checkview_target(
    client: DonutMcpClient,
    profile_id: str,
    debug: Mapping[str, object],
    bridge: BrowserExtensionBridge,
    report: Callable[[str], None],
) -> tuple[str, str, int]:
    """Return one persistent, minimized Studio target for a profile.

    The target identity is persisted only as local bookkeeping. Every reuse
    still verifies the live Donut target catalog and remaps the extension tab
    by window, so a closed tab is recreated safely instead of being mistaken
    for another profile's page.
    """

    with _CHECKVIEW_TARGET_LOCK:
        saved = _CHECKVIEW_TARGET_RECORDS.get(profile_id)
        if isinstance(saved, Mapping):
            saved_target_id = str(saved.get("target_id") or "").strip()
            live_target = _debug_target(debug, saved_target_id)
            if live_target is not None:
                websocket_url = str(
                    live_target.get("webSocketDebuggerUrl") or ""
                ).strip()
                window_id = _target_window_id(
                    client,
                    profile_id,
                    saved_target_id,
                )
                _mcp_set_cdp_window_state(
                    client,
                    profile_id,
                    saved_target_id,
                    _CHECKVIEW_WINDOW_STATE,
                )
                tab_id = _extension_tab_for_window(
                    bridge,
                    client,
                    profile_id,
                    saved_target_id,
                    window_id,
                    int(saved.get("tab_id") or 0),
                )
                _save_checkview_target_record(
                    profile_id,
                    target_id=saved_target_id,
                    window_id=window_id,
                    tab_id=tab_id,
                )
                report("Dùng lại tab checkview nền đã ghim cho profile này.")
                return websocket_url, saved_target_id, tab_id

        report("Đang tạo tab checkview nền cố định cho profile này…")
        websocket_url = _mcp_open_minimized_cdp_window_ws_url(
            client,
            profile_id,
            dict(debug),
            "about:blank",
        )
        target_id = _cdp_page_target_id(websocket_url)
        try:
            window_id = _target_window_id(client, profile_id, target_id)
            _mcp_set_cdp_window_state(
                client,
                profile_id,
                target_id,
                _CHECKVIEW_WINDOW_STATE,
            )
            tab_id = _extension_tab_for_window(
                bridge,
                client,
                profile_id,
                target_id,
                window_id,
                0,
            )
            _save_checkview_target_record(
                profile_id,
                target_id=target_id,
                window_id=window_id,
                tab_id=tab_id,
            )
        except Exception:
            _mcp_close_cdp_page_target(
                client,
                profile_id,
                dict(debug),
                target_id,
            )
            raise
        report("Đã tạo và giữ tab checkview nền để các lượt sau tái sử dụng.")
        return websocket_url, target_id, tab_id


@dataclass(frozen=True)
class RealtimeYouTubeVideo:
    profile_id: str
    video_id: str
    title: str
    realtime_views: int
    visibility: str = "PUBLIC"

    @property
    def identity(self) -> str:
        return f"{self.profile_id}:{self.video_id}"

    @property
    def youtube_url(self) -> str:
        return f"https://youtu.be/{self.video_id}"


@dataclass(frozen=True)
class YouTubeLikePolicy:
    realtime_window_minutes: int = 60
    cooldown_minutes: int = 30
    minimum_likes: int = 10
    minimum_basis_points: int = 500
    maximum_basis_points: int = 1000

    def __post_init__(self) -> None:
        if self.realtime_window_minutes != 60:
            raise ValueError("Nguồn YouTube Studio hiện phải là realtime 60 phút.")
        if self.cooldown_minutes <= 0:
            raise ValueError("Khoảng nghỉ giữa hai đơn phải lớn hơn 0 phút.")
        if self.minimum_likes <= 0:
            raise ValueError("Ngưỡng Like phải lớn hơn 0.")
        if not (
            0 < self.minimum_basis_points <= self.maximum_basis_points <= 10_000
        ):
            raise ValueError("Tỷ lệ Like tự động không hợp lệ.")

    def basis_points_for(self, identity: str) -> int:
        width = self.maximum_basis_points - self.minimum_basis_points + 1
        digest = hashlib.sha256(str(identity).encode("utf-8")).digest()
        return self.minimum_basis_points + int.from_bytes(digest[:4], "big") % width

    def quantity_for(self, identity: str, realtime_views: int) -> tuple[int, int]:
        basis_points = self.basis_points_for(identity)
        quantity = max(
            0,
            (max(0, int(realtime_views)) * basis_points + 5_000) // 10_000,
        )
        return quantity, basis_points


@dataclass(frozen=True)
class YouTubeLikeMonitorResult:
    identity: str
    youtube_url: str
    realtime_views: int | None
    state: str
    message: str
    video_title: str = ""
    quantity: int | None = None
    ratio_basis_points: int | None = None
    service_id: str = ""
    order_id: str = ""


def _ax_value(node: Mapping[str, object], field: str) -> str:
    value = node.get(field)
    return str(value.get("value") or "") if isinstance(value, Mapping) else ""


def _ax_property(node: Mapping[str, object], name: str) -> object:
    properties = node.get("properties")
    if not isinstance(properties, list):
        return None
    for item in properties:
        if not isinstance(item, Mapping) or item.get("name") != name:
            continue
        value = item.get("value")
        return value.get("value") if isinstance(value, Mapping) else None
    return None


def _root_frame(result: Mapping[str, object]) -> Mapping[str, object]:
    frame_tree = result.get("frameTree")
    frame = frame_tree.get("frame") if isinstance(frame_tree, Mapping) else None
    return frame if isinstance(frame, Mapping) else {}


def _fold_ax_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join(
        "".join(char for char in normalized if not unicodedata.combining(char)).split()
    )


def _is_60_minute_label(value: str) -> bool:
    folded = _fold_ax_label(value)
    return any(pattern.search(folded) for pattern in _SIXTY_MINUTE_PATTERNS)


def _is_48_hour_label(value: str) -> bool:
    folded = _fold_ax_label(value)
    return any(pattern.search(folded) for pattern in _FORTY_EIGHT_HOUR_PATTERNS)


def _parse_realtime_views(nodes: list[Mapping[str, object]]) -> int | None:
    """Read Studio's localized 60-minute activity Accessibility label.

    Confirmed live English example: ``Latest activity, Views: Last 60
    minutes, 0.`` Localized variants retain 60 and expose the current count as
    the final numeric token in the same semantic label.
    """

    candidates: list[tuple[int, int]] = []
    for node in nodes:
        if node.get("ignored") is True:
            continue
        name = " ".join(_ax_value(node, "name").split())
        if not _is_60_minute_label(name) or _is_48_hour_label(name):
            continue
        numbers = [
            int(digits)
            for token in _NUMBER.findall(name)
            if (digits := re.sub(r"\D", "", token))
        ]
        if len(numbers) < 2 or 60 not in numbers[:-1]:
            continue
        role = _ax_value(node, "role")
        score = (3 if role == "StaticText" else 1) + min(3, len(name) // 40)
        candidates.append((score, numbers[-1]))
    return max(candidates, default=(0, -1))[1] if candidates else None


class DonutYouTubeRealtimeSource:
    """Semantic CDP reader for video IDs and Studio realtime 60-minute views."""

    def __init__(
        self,
        endpoint: DonutMcpEndpoint | str,
        *,
        coordinator: ProfileUploadCoordinator | None = None,
        max_videos: int | None = None,
        navigation_timeout: float = 30.0,
    ) -> None:
        self.endpoint = endpoint
        self.coordinator = coordinator or ProfileUploadCoordinator()
        self.max_videos = None if max_videos is None else max(1, int(max_videos))
        self.navigation_timeout = max(5.0, float(navigation_timeout))

    def _limit_reached(self, count: int) -> bool:
        return self.max_videos is not None and count >= self.max_videos

    @staticmethod
    def _public_visibility(video_id: str, *, timeout: float = 12.0) -> str:
        """Return PUBLIC only when the anonymous watch page proves it.

        A private video has no usable public videoDetails, while an unlisted
        video explicitly exposes ``isUnlisted: true``. Network/processing
        ambiguity remains UNKNOWN so automatic 1MM ordering fails closed.
        """

        identifier = str(video_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", identifier):
            return "UNKNOWN"
        request = urllib.request.Request(
            "https://www.youtube.com/watch?v="
            + urllib.parse.quote(identifier),
            headers={
                "Accept-Language": "en-US,en;q=0.8",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
                ),
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=max(3.0, float(timeout)),
            ) as response:
                html = response.read(4 * 1024 * 1024).decode(
                    "utf-8",
                    errors="replace",
                )
        except (OSError, TimeoutError, urllib.error.URLError):
            return "UNKNOWN"
        video_ids = {
            value
            for value in _youtube_json_values(html, "videoId")
            if isinstance(value, str)
        }
        states = [
            value
            for value in _youtube_json_values(html, "isUnlisted")
            if isinstance(value, bool)
        ]
        if identifier not in video_ids or not states:
            return "PRIVATE_OR_UNAVAILABLE"
        if True in states:
            return "UNLISTED"
        return "PUBLIC" if False in states else "UNKNOWN"

    def _limited_links(
        self,
        links: Mapping[str, str],
    ) -> tuple[tuple[str, str], ...]:
        items = tuple(links.items())
        return items if self.max_videos is None else items[: self.max_videos]

    @staticmethod
    def _cancelled(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise YouTubeUploadCancelled("Đã dừng theo dõi realtime YouTube.")

    def _wait_for_profile(
        self,
        client: DonutMcpClient,
        profile_id: str,
        cancel_event: threading.Event,
        report: Callable[[str], None] | None = None,
    ) -> bool:
        profile = client.get_profile(profile_id)
        if profile.browser.casefold() not in {"wayfern", "camoufox"}:
            raise YouTubeRealtimeError("Profile không hỗ trợ bộ đọc YouTube local.")
        was_running_before_scan = bool(profile.process_id)
        if profile.process_id:
            # Donut can retain a stale process_id after a profile has crashed
            # or been closed outside the MCP.  A PID-looking value is not a
            # live CDP endpoint; probe the actual endpoint before treating
            # the profile as running.
            try:
                if client.profile_debug_ready(profile_id, profile=profile):
                    return False
            except (DonutMcpEntitlementError, DonutMcpAuthenticationError):
                raise
            except DonutMcpError:
                if report is not None:
                    report("Phát hiện PID cũ nhưng profile đã mất CDP; đang mở lại…")
        try:
            launch_result = client.run_profile(profile_id, YOUTUBE_STUDIO_URL)
        except DonutMcpPriorLaunchUnresolved:
            # No launch was sent in this call. Waiting another two minutes
            # cannot make that old request complete, and would hold the one
            # checkview lane while other live profiles wait to be scanned.
            raise
        except DonutMcpCapacityError as exc:
            raise YouTubeRealtimeDeferred(
                "Wayfern đang đủ 4 phiên chạy. Checkview được hoãn để không gửi "
                "lặp lệnh mở; đóng một profile hoặc chờ một tác vụ kết thúc rồi thử lại."
            ) from exc
        except DonutMcpTimeoutError:
            # Donut may have accepted the launch and still be starting the
            # browser after the HTTP wait expires. Do not submit a second
            # launch: poll the authoritative profile/CDP state instead.
            if report is not None:
                report("Donut đang mở profile; tiếp tục kiểm tra trạng thái, không mở lại lệnh.")
            launch_result = ""
        opened_by_this_scan = (
            not was_running_before_scan
            and str(launch_result or "").strip().casefold() != "already-running"
        )
        try:
            deadline = time.monotonic() + 120.0
            while time.monotonic() < deadline:
                self._cancelled(cancel_event)
                current = client.get_profile(profile_id)
                if current.process_id:
                    try:
                        if client.profile_debug_ready(profile_id, profile=current):
                            if opened_by_this_scan:
                                _remember_checkview_profile(profile_id, current.process_id)
                            return opened_by_this_scan
                    except (DonutMcpEntitlementError, DonutMcpAuthenticationError):
                        raise
                    except DonutMcpError:
                        # The process may still be starting.  Keep polling the
                        # live CDP registration instead of accepting the stale
                        # process_id as proof that the profile is usable.
                        pass
                time.sleep(0.75)
            raise YouTubeRealtimeError("Donut không mở được profile để đọc YouTube Studio.")
        except Exception:
            if opened_by_this_scan:
                self._close_profile_opened_for_scan(
                    client,
                    profile_id,
                    report or (lambda _message: None),
                )
            raise

    def _invalidate_checkview_target(
        self,
        client: DonutMcpClient,
        profile_id: str,
        report: Callable[[str], None],
    ) -> None:
        """Forget and best-effort close a target that cannot be trusted."""

        saved = _forget_checkview_target_record(profile_id)
        if not saved:
            return
        target_id = str(saved.get("target_id") or "").strip()
        if target_id:
            try:
                debug = client.get_debug_connection(profile_id)
                _mcp_close_cdp_page_target(
                    client,
                    profile_id,
                    dict(debug),
                    target_id,
                    timeout=3.0,
                )
            except Exception:
                # The profile or relay may already be gone.  The persisted
                # record was removed above, which is the important recovery
                # step; the next attempt will allocate a fresh target.
                pass
        report("Đã bỏ target checkview không còn dùng được; sẽ tạo lại tab nền…")

    def _navigate(
        self,
        cdp: _CdpSession,
        url: str,
        cancel_event: threading.Event,
    ) -> None:
        self._cancelled(cancel_event)
        previous_frame = _root_frame(cdp.command("Page.getFrameTree"))
        previous_loader = str(previous_frame.get("loaderId") or "").strip()
        previous_url = str(previous_frame.get("url") or "")
        result = cdp.command("Page.navigate", {"url": url})
        error = str(result.get("errorText") or "").strip()
        if error:
            raise YouTubeRealtimeError(f"YouTube Studio không tải được trang ({error}).")
        expected_loader = str(result.get("loaderId") or "").strip()
        navigation_seen = False
        deadline = time.monotonic() + self.navigation_timeout
        while time.monotonic() < deadline:
            self._cancelled(cancel_event)
            frame = _root_frame(cdp.command("Page.getFrameTree"))
            current_loader = str(frame.get("loaderId") or "").strip()
            current_url = str(frame.get("url") or "")
            if expected_loader and current_loader == expected_loader:
                navigation_seen = True
            elif previous_loader and current_loader and current_loader != previous_loader:
                navigation_seen = True
            elif not previous_loader and current_url and current_url != previous_url:
                navigation_seen = True
            if not navigation_seen:
                time.sleep(0.25)
                continue
            try:
                host = (urlsplit(current_url).hostname or "").casefold()
            except ValueError:
                host = ""
            if host == "studio.youtube.com":
                return
            if host in {"accounts.google.com", "consent.youtube.com"}:
                raise YouTubeRealtimeError("Profile đã mất đăng nhập YouTube Studio.")
            time.sleep(0.25)
        raise YouTubeRealtimeError("YouTube Studio tải trang quá thời hạn.")

    @staticmethod
    def _ax_nodes(
        cdp: _CdpSession,
    ) -> list[Mapping[str, object]]:
        result = cdp.command(
            "Accessibility.getFullAXTree",
            {"depth": 100},
        )
        nodes = result.get("nodes")
        return (
            [node for node in nodes if isinstance(node, Mapping)]
            if isinstance(nodes, list)
            else []
        )

    def _channel_id(
        self,
        cdp: _CdpSession,
        cancel_event: threading.Event,
    ) -> str:
        self._navigate(cdp, YOUTUBE_STUDIO_URL, cancel_event)
        deadline = time.monotonic() + self.navigation_timeout
        while time.monotonic() < deadline:
            self._cancelled(cancel_event)
            frame = _root_frame(cdp.command("Page.getFrameTree"))
            match = _CHANNEL_STUDIO_URL.match(str(frame.get("url") or ""))
            if match:
                return match.group(1)
            time.sleep(0.5)
        raise YouTubeRealtimeError("Không xác định được kênh của profile YouTube.")

    @staticmethod
    def _video_links_from_ax(
        nodes: list[Mapping[str, object]],
    ) -> tuple[tuple[str, str], ...]:
        found: dict[str, str] = {}
        for node in nodes:
            if _ax_value(node, "role") != "link":
                continue
            target = str(_ax_property(node, "url") or "")
            match = _VIDEO_STUDIO_URL.match(target)
            if match is None:
                continue
            video_id = match.group(1)
            title = " ".join(_ax_value(node, "name").split())
            if title.casefold().startswith("video thumbnail:"):
                title = title.split(":", 1)[1].strip()
            current = found.get(video_id, "")
            if not current or len(title) > len(current):
                found[video_id] = title
        return tuple(found.items())

    @staticmethod
    def _merge_video_links(
        *groups: tuple[tuple[str, str], ...],
    ) -> tuple[tuple[str, str], ...]:
        """Union semantic links and DOM-row links without losing titles."""

        found: dict[str, str] = {}
        for group in groups:
            for video_id, title in group:
                current = found.get(video_id, "")
                if not current or len(title) > len(current):
                    found[video_id] = title
        return tuple(found.items())

    @staticmethod
    def _video_links_from_extension_page(
        result: object,
    ) -> tuple[tuple[str, str], ...]:
        """Validate one complete Content-page response from the extension.

        The extension is the only reader that can see the live composed DOM
        while the checkview window is minimized.  Do not accept a response
        merely because it contains one row: ``complete`` is an explicit
        contract and the page signature is used by the caller to detect a
        stale/sample page or a failed Next transition.
        """

        if not isinstance(result, Mapping) or result.get("complete") is not True:
            detail = (
                str(result.get("error") or "kết quả extension chưa hoàn tất")
                if isinstance(result, Mapping)
                else "kết quả extension không hợp lệ"
            )
            raise YouTubeRealtimeError(
                "Danh sách video YouTube chưa tải đầy đủ qua extension checkview "
                f"({detail})."
            )
        raw_rows = result.get("rows")
        if not isinstance(raw_rows, list):
            raise YouTubeRealtimeError(
                "Danh sách video YouTube chưa tải đầy đủ qua extension checkview "
                "(thiếu rows)."
            )
        found: dict[str, str] = {}
        for item in raw_rows:
            if not isinstance(item, Mapping):
                continue
            video_id = str(item.get("videoId") or item.get("video_id") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
                continue
            title = " ".join(
                str(item.get("title") or item.get("name") or "").split()
            )[:500]
            current = found.get(video_id, "")
            if not current or len(title) > len(current):
                found[video_id] = title
        if not found:
            raise YouTubeRealtimeError(
                "Danh sách video YouTube chưa tải đầy đủ qua extension checkview "
                "(không có video hợp lệ)."
            )
        reported_count = result.get("videoCount")
        if isinstance(reported_count, int) and reported_count != len(found):
            raise YouTubeRealtimeError(
                "Danh sách video YouTube chưa tải đầy đủ qua extension checkview "
                f"(reported={reported_count}, parsed={len(found)})."
            )
        return tuple(found.items())

    @staticmethod
    def _dom_attributes(node: Mapping[str, object]) -> dict[str, str]:
        raw = node.get("attributes")
        if not isinstance(raw, list):
            return {}
        values: dict[str, str] = {}
        for index in range(0, len(raw) - 1, 2):
            name = str(raw[index] or "").strip().casefold()
            if name:
                values[name] = html.unescape(str(raw[index + 1] or ""))
        return values

    @classmethod
    def _video_links_from_dom_snapshot(
        cls,
        cdp: _CdpSession,
    ) -> tuple[tuple[str, str], ...]:
        """Read current Studio video rows when title controls have no href.

        Recent Studio builds render a video title as ``a#video-title`` with
        ``role=button`` and omit the old ``/video/<id>`` href.  The thumbnail
        URL still carries the stable video ID.  This is a read-only DOM
        protocol path and does not evaluate page JavaScript.
        """

        document = cdp.command(
            "DOM.getDocument",
            {"depth": 1, "pierce": True},
        )
        root = document.get("root") if isinstance(document, Mapping) else None
        root_id = root.get("nodeId") if isinstance(root, Mapping) else None
        if not isinstance(root_id, int):
            return ()
        rows = cdp.command(
            "DOM.querySelectorAll",
            {"nodeId": root_id, "selector": "ytcp-video-row"},
        ).get("nodeIds")
        if not isinstance(rows, list):
            return ()

        found: dict[str, str] = {}
        for row_id in rows:
            if not isinstance(row_id, int):
                continue
            thumbnail_nodes = cdp.command(
                "DOM.querySelectorAll",
                {"nodeId": row_id, "selector": "img"},
            ).get("nodeIds")
            video_id = ""
            if isinstance(thumbnail_nodes, list):
                for thumbnail_id in thumbnail_nodes:
                    if not isinstance(thumbnail_id, int):
                        continue
                    described = cdp.command(
                        "DOM.describeNode",
                        {"nodeId": thumbnail_id, "depth": 0},
                    )
                    node = described.get("node") if isinstance(described, Mapping) else None
                    attributes = (
                        cls._dom_attributes(node)
                        if isinstance(node, Mapping)
                        else {}
                    )
                    for value in (
                        attributes.get("src", ""),
                        attributes.get("data-src", ""),
                        attributes.get("srcset", ""),
                    ):
                        match = _VIDEO_THUMBNAIL_URL.search(value)
                        if match:
                            video_id = match.group(1)
                            break
                    if video_id:
                        break

            title = ""
            title_nodes = cdp.command(
                "DOM.querySelectorAll",
                {"nodeId": row_id, "selector": "a#video-title"},
            ).get("nodeIds")
            if isinstance(title_nodes, list):
                for title_id in title_nodes:
                    if not isinstance(title_id, int):
                        continue
                    described = cdp.command(
                        "DOM.describeNode",
                        {"nodeId": title_id, "depth": 1},
                    )
                    node = described.get("node") if isinstance(described, Mapping) else None
                    if not isinstance(node, Mapping):
                        continue
                    attributes = cls._dom_attributes(node)
                    title = " ".join(
                        (
                            attributes.get("aria-label", "")
                            or attributes.get("title", "")
                            or str(node.get("nodeValue") or "")
                        ).split()
                    )
                    if title:
                        break

            if not video_id:
                # A lazy thumbnail may not expose its src attribute yet. The
                # row's serialized DOM remains bounded to one visible row and
                # can still provide the /vi/<id>/ token.
                try:
                    outer = str(
                        cdp.command("DOM.getOuterHTML", {"nodeId": row_id}).get(
                            "outerHTML", ""
                        )
                    )
                except Exception as exc:
                    if _is_stale_dom_node_error(exc):
                        # Do not turn an invalidated row into a silent missing
                        # video. The outer reader must restart the complete
                        # snapshot so pagination cannot succeed partially.
                        raise
                    outer = ""
                match = _VIDEO_THUMBNAIL_URL.search(html.unescape(outer))
                if match:
                    video_id = match.group(1)

            if video_id:
                current = found.get(video_id, "")
                if not current or len(title) > len(current):
                    found[video_id] = title
        return tuple(found.items())

    @classmethod
    def _video_links_from_dom(
        cls,
        cdp: _CdpSession,
    ) -> tuple[tuple[str, str], ...]:
        """Read rows and recover if Studio invalidates the DOM snapshot.

        ``DOM.querySelectorAll`` returns node IDs tied to the current
        document snapshot. Studio can replace its virtualized rows between
        two commands, which makes those IDs stale. Re-running the complete
        snapshot is important: retrying only the failed row would mix node IDs
        from different renders and could silently omit videos.
        """

        for attempt in range(_CHECKVIEW_DOM_STALE_RETRIES):
            try:
                return cls._video_links_from_dom_snapshot(cdp)
            except Exception as exc:
                if (
                    not _is_stale_dom_node_error(exc)
                    or attempt + 1 >= _CHECKVIEW_DOM_STALE_RETRIES
                ):
                    raise
                # Let Polymer finish the current row replacement before
                # requesting another document snapshot. This wait is bounded
                # and does not activate the parked checkview window.
                time.sleep(0.15 * (attempt + 1))
        return ()

    @staticmethod
    def _fold_label(value: str) -> str:
        return _fold_ax_label(value)

    @staticmethod
    def _realtime_60_control_from_ax(
        nodes: list[Mapping[str, object]],
    ) -> Mapping[str, object] | None:
        """Find the visible Studio range control when it says 60 minutes."""

        matches: list[tuple[int, Mapping[str, object]]] = []
        for node in nodes:
            if node.get("ignored") is True:
                continue
            role = _ax_value(node, "role")
            if role not in {"button", "tab", "radio", "combobox"}:
                continue
            label = " ".join(
                f"{_ax_value(node, 'name')} {_ax_value(node, 'description')}".split()
            )
            if not _is_60_minute_label(label) or _is_48_hour_label(label):
                continue
            score = 2 if _ax_property(node, "focusable") is True else 0
            score += 1 if isinstance(node.get("backendDOMNodeId"), int) else 0
            matches.append((score, node))
        return max(matches, key=lambda item: item[0])[1] if matches else None

    @staticmethod
    def _realtime_48_control_from_ax(
        nodes: list[Mapping[str, object]],
    ) -> Mapping[str, object] | None:
        """Find the current 48-hour range control so its menu can be opened."""

        matches: list[tuple[int, Mapping[str, object]]] = []
        for node in nodes:
            if node.get("ignored") is True:
                continue
            role = _ax_value(node, "role")
            if role not in {"button", "tab", "radio", "combobox"}:
                continue
            label = " ".join(
                f"{_ax_value(node, 'name')} {_ax_value(node, 'description')}".split()
            )
            if not _is_48_hour_label(label) or _is_60_minute_label(label):
                continue
            score = 2 if _ax_property(node, "focusable") is True else 0
            score += 1 if isinstance(node.get("backendDOMNodeId"), int) else 0
            matches.append((score, node))
        return max(matches, key=lambda item: item[0])[1] if matches else None

    @staticmethod
    def _realtime_range_option_from_ax(
        nodes: list[Mapping[str, object]],
        *,
        minutes: int,
    ) -> Mapping[str, object] | None:
        """Find a visible item inside Studio's open realtime range menu."""

        matches: list[tuple[int, Mapping[str, object]]] = []
        for node in nodes:
            if node.get("ignored") is True:
                continue
            role = _ax_value(node, "role")
            if role not in {"option", "menuitem", "menuitemradio", "radio"}:
                continue
            label = " ".join(
                f"{_ax_value(node, 'name')} {_ax_value(node, 'description')}".split()
            )
            is_target = (
                _is_60_minute_label(label)
                if minutes == 60
                else _is_48_hour_label(label)
            )
            if not is_target or (
                minutes == 60 and _is_48_hour_label(label)
            ):
                continue
            score = 2 if _ax_property(node, "focusable") is True else 0
            score += 1 if isinstance(node.get("backendDOMNodeId"), int) else 0
            matches.append((score, node))
        return max(matches, key=lambda item: item[0])[1] if matches else None

    @classmethod
    def _realtime_range_menu_is_open(
        cls,
        nodes: list[Mapping[str, object]],
    ) -> bool:
        return (
            cls._realtime_range_option_from_ax(nodes, minutes=48) is not None
            and cls._realtime_range_option_from_ax(nodes, minutes=60) is not None
        )

    @classmethod
    def _verified_60_minute_views_from_ax(
        cls,
        nodes: list[Mapping[str, object]],
    ) -> int | None:
        """Return views only when both report and visible range say 60 minutes."""

        views = _parse_realtime_views(nodes)
        if (
            views is None
            or cls._realtime_60_control_from_ax(nodes) is None
            or cls._realtime_range_menu_is_open(nodes)
        ):
            return None
        return views

    @classmethod
    def _next_page_button_from_ax(
        cls,
        nodes: list[Mapping[str, object]],
    ) -> Mapping[str, object] | None:
        button, enabled = cls._next_page_control_from_ax(nodes)
        return button if enabled else None

    @classmethod
    def _next_page_control_from_ax(
        cls,
        nodes: list[Mapping[str, object]],
    ) -> tuple[Mapping[str, object] | None, bool]:
        """Return the semantic Next control and whether it can be activated."""

        disabled_match: Mapping[str, object] | None = None
        for node in nodes:
            if (
                node.get("ignored") is True
                or _ax_value(node, "role") != "button"
                or not isinstance(node.get("backendDOMNodeId"), int)
            ):
                continue
            label = cls._fold_label(
                f"{_ax_value(node, 'name')} {_ax_value(node, 'description')}"
            )
            if any(cls._fold_label(marker) in label for marker in _NEXT_PAGE_MARKERS):
                disabled = _ax_property(node, "disabled")
                enabled = not (
                    disabled is True or str(disabled or "").casefold() == "true"
                )
                if enabled:
                    return node, True
                disabled_match = node
        return disabled_match, False

    @staticmethod
    def _activate_ax_button(
        cdp: _CdpSession,
        node: Mapping[str, object],
        activate: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> None:
        backend_node_id = int(node["backendDOMNodeId"])
        with activate() if activate is not None else nullcontext():
            cdp.command("DOM.focus", {"backendNodeId": backend_node_id})
            key_event = {
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            }
            cdp.command("Input.dispatchKeyEvent", {"type": "keyDown", **key_event})
            cdp.command("Input.dispatchKeyEvent", {"type": "keyUp", **key_event})

    @staticmethod
    def _click_ax_control(
        cdp: _CdpSession,
        node: Mapping[str, object],
    ) -> None:
        """Click the live DOM box without fixed screen coordinates or OS mouse use."""

        backend_node_id = int(node["backendDOMNodeId"])
        cdp.command(
            "DOM.scrollIntoViewIfNeeded",
            {"backendNodeId": backend_node_id},
        )
        model = cdp.command(
            "DOM.getBoxModel",
            {"backendNodeId": backend_node_id},
        ).get("model")
        if not isinstance(model, Mapping):
            raise YouTubeRealtimeError("Nút chọn khung realtime chưa có vị trí DOM.")
        quad = model.get("border") or model.get("content")
        if not isinstance(quad, list) or len(quad) < 8:
            raise YouTubeRealtimeError("Nút chọn khung realtime chưa hiển thị ổn định.")
        try:
            x = sum(float(quad[index]) for index in (0, 2, 4, 6)) / 4
            y = sum(float(quad[index]) for index in (1, 3, 5, 7)) / 4
        except (TypeError, ValueError) as exc:
            raise YouTubeRealtimeError(
                "Nút chọn khung realtime trả vị trí DOM không hợp lệ."
            ) from exc
        cdp.command("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        cdp.command(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            },
        )
        cdp.command(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            },
        )

    @staticmethod
    def _select_60_via_extension(
        bridge: BrowserExtensionBridge,
        profile_id: str,
        tab_id: int,
    ) -> Mapping[str, object]:
        """Select Studio's 60-minute range through the paired extension DOM."""

        if not isinstance(tab_id, int) or tab_id <= 0:
            raise YouTubeRealtimeError(
                "Tab extension của checkview không có ID hợp lệ."
            )
        try:
            result = bridge.command(
                profile_id,
                "youtube_checkview_select_60",
                {
                    "tab_id": tab_id,
                },
                timeout=15,
            )
        except BrowserExtensionBridgeError as exc:
            raise YouTubeRealtimeError(
                "DocuRandom Donut Bridge không thực hiện được thao tác chọn 60 phút."
            ) from exc
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            detail = (
                str(result.get("error") or "không rõ lỗi")
                if isinstance(result, Mapping)
                else "kết quả extension không hợp lệ"
            )
            raise YouTubeRealtimeError(
                "Extension không xác nhận được bộ lọc realtime 60 phút "
                f"({detail})."
            )
        return result

    @staticmethod
    def _expose_pagination_controls(
        cdp: _CdpSession,
        activate: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> None:
        # Input events require an active Chromium target. Production owns an
        # active target inside a separate minimized window, so this never has
        # to activate or replace the user's current tab.
        key = {
            "key": "End",
            "code": "End",
            "windowsVirtualKeyCode": 35,
            "nativeVirtualKeyCode": 35,
        }
        with activate() if activate is not None else nullcontext():
            for _ in range(3):
                cdp.command("Input.dispatchKeyEvent", {"type": "keyDown", **key})
                cdp.command("Input.dispatchKeyEvent", {"type": "keyUp", **key})

    def _paginated_video_links(
        self,
        cdp: _CdpSession,
        cancel_event: threading.Event,
        activate: Callable[[], AbstractContextManager[None]] | None = None,
        report: Callable[[str], None] | None = None,
        *,
        extension_bridge: BrowserExtensionBridge | None = None,
        extension_tab_id: int = 0,
        profile_id: str = "",
    ) -> tuple[tuple[str, str], ...]:
        if extension_bridge is not None and extension_tab_id > 0 and profile_id:
            return self._paginated_video_links_via_extension(
                extension_bridge,
                profile_id,
                extension_tab_id,
                cancel_event,
                report,
            )
        merged: dict[str, str] = {}
        seen_pages: set[tuple[str, ...]] = set()
        page_number = 0
        while True:
            deadline = time.monotonic() + self.navigation_timeout
            nodes: list[Mapping[str, object]] = []
            page_links: tuple[tuple[str, str], ...] = ()
            previous_page: tuple[str, ...] = ()
            stable_reads = 0
            while time.monotonic() < deadline:
                self._cancelled(cancel_event)
                nodes = self._ax_nodes(cdp)
                # AX and DOM are complementary on current Studio builds:
                # title controls may expose a stable href only through AX,
                # while virtualized rows may expose the stable ID only in a
                # thumbnail URL. Union both sources so a single "sample" row
                # cannot be mistaken for the complete page.
                page_links = self._merge_video_links(
                    self._video_links_from_ax(nodes),
                    self._video_links_from_dom(cdp),
                )
                current_page = tuple(video_id for video_id, _title in page_links)
                if page_links and current_page == previous_page:
                    stable_reads += 1
                elif page_links:
                    previous_page = current_page
                    stable_reads = 1
                else:
                    previous_page = ()
                    stable_reads = 0
                if (
                    page_links
                    and current_page not in seen_pages
                    and stable_reads >= _CHECKVIEW_PAGE_STABLE_READS
                ):
                    break
                time.sleep(0.25)
            if not page_links:
                # Returning ``merged`` here used to turn a missing/unfinished
                # next page into a successful partial scan. That is exactly
                # how a first/sample video could be mistaken for the complete
                # channel. Fail closed and let scan_profile recreate the
                # owned target, then retry from a fresh Studio document.
                raise YouTubeRealtimeError(
                    "Danh sách video YouTube chưa tải đầy đủ; "
                    f"đã đọc {len(merged)} video nhưng trang tiếp theo chưa được "
                    "xác nhận."
                )

            page_fingerprint = tuple(video_id for video_id, _title in page_links)
            if page_fingerprint in seen_pages:
                # The page did not change after an attempted Next activation.
                # Treat this as the safe terminal condition instead of looping.
                return self._limited_links(merged)
            seen_pages.add(page_fingerprint)

            for video_id, title in page_links:
                current = merged.get(video_id, "")
                if not current or len(title) > len(current):
                    merged[video_id] = title
            page_number += 1
            if report is not None:
                report(
                    f"Checkview đã đọc trang {page_number} · "
                    f"đã thu thập {len(merged)} video; đang xác nhận phân trang…"
                )
            if self._limit_reached(len(merged)):
                return self._limited_links(merged)

            next_button, next_enabled = self._next_page_control_from_ax(nodes)
            if next_button is None:
                self._expose_pagination_controls(cdp, activate)
                time.sleep(0.35)
                self._cancelled(cancel_event)
                nodes = self._ax_nodes(cdp)
                refreshed_links = self._merge_video_links(
                    self._video_links_from_ax(nodes),
                    self._video_links_from_dom(cdp),
                )
                for video_id, title in refreshed_links:
                    current = merged.get(video_id, "")
                    if not current or len(title) > len(current):
                        merged[video_id] = title
                if self._limit_reached(len(merged)):
                    return self._limited_links(merged)
                next_button, next_enabled = self._next_page_control_from_ax(nodes)
            if next_button is None or not next_enabled:
                return self._limited_links(merged)
            self._activate_ax_button(cdp, next_button, activate)

    def _paginated_video_links_via_extension(
        self,
        bridge: BrowserExtensionBridge,
        profile_id: str,
        tab_id: int,
        cancel_event: threading.Event,
        report: Callable[[str], None] | None = None,
    ) -> tuple[tuple[str, str], ...]:
        """Read every Content page through the paired extension DOM reader."""

        merged: dict[str, str] = {}
        seen_signatures: set[str] = set()
        action = "snapshot"
        before_signature = ""
        page_number = 0
        while True:
            self._cancelled(cancel_event)
            result: Mapping[str, object] | None = None
            last_error: Exception | None = None
            # Snapshot only reads the page and can safely be retried. "next"
            # clicks a control: a missing response may mean the click already
            # happened, so issue it exactly once and then observe the page.
            for attempt in range(3 if action == "snapshot" else 1):
                self._cancelled(cancel_event)
                try:
                    candidate = bridge.command(
                        profile_id,
                        "youtube_checkview_content_page",
                        {
                            "tab_id": tab_id,
                            "action": action,
                            "before_signature": before_signature,
                        },
                        timeout=_CHECKVIEW_EXTENSION_PAGE_TIMEOUT,
                    )
                    self._video_links_from_extension_page(candidate)
                    if action == "next" and candidate.get("transitioned") is not True:
                        raise YouTubeRealtimeError("Next chưa được xác nhận")
                    result = candidate
                    break
                except (BrowserExtensionBridgeError, YouTubeRealtimeError) as exc:
                    last_error = exc
                if attempt < 2 and action == "snapshot":
                    time.sleep(0.7)
            if result is None and action == "next":
                result = self._observe_extension_next_transition(
                    bridge, profile_id, tab_id, before_signature, cancel_event
                )
            if result is None:
                raise YouTubeRealtimeError(
                    "Danh sách video YouTube chưa tải đầy đủ qua extension checkview "
                    "(snapshot chưa trả về trang hợp lệ)."
                ) from last_error
            links = self._video_links_from_extension_page(result)
            signature = (
                str(result.get("pageSignature") or "").strip()
                if isinstance(result, Mapping)
                else ""
            ) or "\x1f".join(video_id for video_id, _title in links)
            if not signature or signature in seen_signatures:
                raise YouTubeRealtimeError(
                    "Danh sách video YouTube chưa tải đầy đủ qua extension checkview "
                    "(trang bị lặp hoặc chưa đổi sau Next)."
                )
            if action == "next" and (
                not isinstance(result, Mapping)
                or result.get("transitioned") is not True
            ):
                raise YouTubeRealtimeError(
                    "Danh sách video YouTube chưa tải đầy đủ qua extension checkview "
                    "(Next chưa được xác nhận)."
                )
            seen_signatures.add(signature)
            for video_id, title in links:
                current = merged.get(video_id, "")
                if not current or len(title) > len(current):
                    merged[video_id] = title
            page_number += 1
            if report is not None:
                next_state = (
                    "còn trang tiếp theo"
                    if isinstance(result, Mapping) and result.get("nextEnabled")
                    else "đã ở trang cuối"
                )
                report(
                    f"Checkview extension đã đọc trang {page_number} · "
                    f"đã thu thập {len(merged)} video · {next_state}."
                )
            if self._limit_reached(len(merged)):
                return self._limited_links(merged)
            next_enabled = (
                bool(result.get("nextEnabled"))
                if isinstance(result, Mapping)
                else False
            )
            if not next_enabled:
                return self._limited_links(merged)
            before_signature = signature
            action = "next"

    def _observe_extension_next_transition(
        self,
        bridge: BrowserExtensionBridge,
        profile_id: str,
        tab_id: int,
        before_signature: str,
        cancel_event: threading.Event,
    ) -> Mapping[str, object]:
        """Resolve an uncertain Next by reading the page, never clicking again."""

        last_error: Exception | None = None
        for attempt in range(3):
            self._cancelled(cancel_event)
            try:
                observed = bridge.command(
                    profile_id,
                    "youtube_checkview_content_page",
                    {"tab_id": tab_id, "action": "snapshot"},
                    timeout=_CHECKVIEW_EXTENSION_PAGE_TIMEOUT,
                )
                links = self._video_links_from_extension_page(observed)
                signature = str(observed.get("pageSignature") or "").strip()
                signature = signature or "\x1f".join(video_id for video_id, _ in links)
                if signature != before_signature:
                    return {**observed, "transitioned": True}
            except (BrowserExtensionBridgeError, YouTubeRealtimeError) as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(0.7)
        raise YouTubeRealtimeError(
            "Danh sách video YouTube chưa tải đầy đủ qua extension checkview "
            "(Next chưa được xác nhận; không bấm lại để tránh bỏ sót trang)."
        ) from last_error

    def _latest_activity_video_links(
        self,
        cdp: _CdpSession,
        channel_id: str,
        cancel_event: threading.Event,
        activate: Callable[[], AbstractContextManager[None]] | None = None,
        report: Callable[[str], None] | None = None,
    ) -> tuple[tuple[str, str], ...]:
        self._navigate(
            cdp,
            (
                f"https://studio.youtube.com/channel/{channel_id}/analytics/"
                "tab-overview/period-default/explore?theme=dark&entity_type=CHANNEL"
                f"&entity_id={channel_id}&time_period=4_weeks"
                "&explore_type=LATEST_ACTIVITY"
            ),
            cancel_event,
        )
        deadline = time.monotonic() + self.navigation_timeout
        while time.monotonic() < deadline:
            self._cancelled(cancel_event)
            nodes = self._ax_nodes(cdp)
            button = next(
                (
                    node
                    for node in nodes
                    if _ax_value(node, "role") == "button"
                    and "60" in _ax_value(node, "name")
                    and "48" not in _ax_value(node, "name")
                    and isinstance(node.get("backendDOMNodeId"), int)
                ),
                None,
            )
            if button is None:
                time.sleep(0.5)
                continue
            self._activate_ax_button(cdp, button, activate)
            # The report is sorted by the selected 60-minute window. Its rows
            # expose semantic video links when the channel has active content.
            wait_until = time.monotonic() + 1.0
            while time.monotonic() < wait_until:
                self._cancelled(cancel_event)
                time.sleep(0.25)
            return self._paginated_video_links(cdp, cancel_event, activate, report)
        return ()

    def _recent_video_links(
        self,
        cdp: _CdpSession,
        channel_id: str,
        cancel_event: threading.Event,
        activate: Callable[[], AbstractContextManager[None]] | None = None,
        report: Callable[[str], None] | None = None,
        *,
        extension_bridge: BrowserExtensionBridge | None = None,
        extension_tab_id: int = 0,
        profile_id: str = "",
    ) -> tuple[tuple[str, str], ...]:
        self._navigate(
            cdp,
            f"https://studio.youtube.com/channel/{channel_id}/videos/upload?theme=dark",
            cancel_event,
        )
        return self._paginated_video_links(
            cdp,
            cancel_event,
            activate,
            report,
            extension_bridge=extension_bridge,
            extension_tab_id=extension_tab_id,
            profile_id=profile_id,
        )

    def _video_links(
        self,
        cdp: _CdpSession,
        channel_id: str,
        cancel_event: threading.Event,
        activate: Callable[[], AbstractContextManager[None]] | None = None,
        report: Callable[[str], None] | None = None,
        *,
        extension_bridge: BrowserExtensionBridge | None = None,
        extension_tab_id: int = 0,
        profile_id: str = "",
    ) -> tuple[tuple[str, str], ...]:
        # Studio defaults the channel-level Latest activity screen to a
        # 48-hour graph on some accounts. The paginated Content list gives us
        # every video without opening that report; each video is measured on
        # its own explicitly verified 60-minute report below.
        return self._recent_video_links(
            cdp,
            channel_id,
            cancel_event,
            activate,
            report,
            extension_bridge=extension_bridge,
            extension_tab_id=extension_tab_id,
            profile_id=profile_id,
        )

    def _video_realtime_views(
        self,
        cdp: _CdpSession,
        video_id: str,
        cancel_event: threading.Event,
        *,
        extension_bridge: BrowserExtensionBridge | None = None,
        extension_tab_id: int = 0,
        profile_id: str = "",
    ) -> int:
        analytics_url = (
            f"https://studio.youtube.com/video/{video_id}/analytics/"
            "tab-overview/period-default?theme=dark"
        )
        self._navigate(cdp, analytics_url, cancel_event)
        deadline = time.monotonic() + self.navigation_timeout
        extension_selection_sent = False
        native_control_click_sent = False
        native_menu_click_sent = False
        analytics_retry_started_at = 0.0
        analytics_error_cleared_after_retry = False
        analytics_page_retry_count = 0
        while time.monotonic() < deadline:
            self._cancelled(cancel_event)
            nodes = self._ax_nodes(cdp)
            views = self._verified_60_minute_views_from_ax(nodes)
            if views is not None:
                return views
            # Studio can render a valid video shell while its Analytics card
            # reports an upstream error. That page has no 60-minute control:
            # waiting for the range then reports the wrong cause. Retry the
            # page's own read-only request once and retain the real failure if
            # it comes back.
            analytics_error = any(
                "xay ra loi" in _fold_ax_label(_ax_value(node, "name"))
                or "something went wrong" in _fold_ax_label(
                    _ax_value(node, "name")
                )
                for node in nodes
                if node.get("ignored") is not True
            )
            if analytics_error:
                retry_button = next(
                    (
                        node
                        for node in nodes
                        if node.get("ignored") is not True
                        and _ax_value(node, "role") == "button"
                        and _fold_ax_label(_ax_value(node, "name"))
                        in {"thu lai", "try again", "retry"}
                        and isinstance(node.get("backendDOMNodeId"), int)
                    ),
                    None,
                )
                if analytics_retry_started_at == 0.0 and retry_button is not None:
                    self._click_ax_control(cdp, retry_button)
                    analytics_retry_started_at = time.monotonic()
                elif (
                    retry_button is None
                    and analytics_page_retry_count < 2
                ):
                    # Some Studio error shells do not expose a Retry button.
                    # A fresh navigation is still a read-only recovery and is
                    # safer than immediately aborting the whole channel scan.
                    analytics_page_retry_count += 1
                    self._navigate(cdp, analytics_url, cancel_event)
                    deadline = time.monotonic() + self.navigation_timeout
                    extension_selection_sent = False
                    native_control_click_sent = False
                    native_menu_click_sent = False
                    analytics_retry_started_at = 0.0
                    analytics_error_cleared_after_retry = False
                elif (
                    retry_button is None
                    or analytics_error_cleared_after_retry
                    or time.monotonic() - analytics_retry_started_at >= 12.0
                ):
                    retry_detail = (
                        "đã thử tải lại một lần nhưng trang vẫn lỗi"
                        if analytics_retry_started_at
                        else "trang không có nút thử lại"
                    )
                    raise YouTubeRealtimeError(
                        "YouTube Studio báo lỗi khi tải Analytics của video "
                        f"{video_id}; {retry_detail}."
                    )
                time.sleep(0.75)
                continue
            if analytics_retry_started_at:
                analytics_error_cleared_after_retry = True
            menu_60 = self._realtime_range_option_from_ax(nodes, minutes=60)
            if menu_60 is not None and isinstance(
                menu_60.get("backendDOMNodeId"), int
            ):
                if (
                    not extension_selection_sent
                    and extension_bridge is not None
                    and extension_tab_id > 0
                    and profile_id
                ):
                    self._select_60_via_extension(
                        extension_bridge,
                        profile_id,
                        extension_tab_id,
                    )
                    extension_selection_sent = True
                elif not native_menu_click_sent:
                    self._click_ax_control(cdp, menu_60)
                    native_menu_click_sent = True
                time.sleep(0.75)
                continue
            # Studio normally opens this card on "Last 48 hours". First open
            # that live range control, then select the 60-minute item revealed
            # by its menu. Never accept the 48-hour number while transitioning.
            current_48 = self._realtime_48_control_from_ax(nodes)
            if current_48 is not None and isinstance(
                current_48.get("backendDOMNodeId"), int
            ):
                if (
                    not extension_selection_sent
                    and extension_bridge is not None
                    and extension_tab_id > 0
                    and profile_id
                ):
                    self._select_60_via_extension(
                        extension_bridge,
                        profile_id,
                        extension_tab_id,
                    )
                    extension_selection_sent = True
                elif not native_control_click_sent:
                    self._click_ax_control(cdp, current_48)
                    native_control_click_sent = True
                time.sleep(0.75)
                continue
            # Studio remembers the last selected range. When the current
            # control already says 60 minutes, wait for its report to finish
            # loading. If the menu is open, the menu_60 branch above closes it
            # by selecting the real option instead of the similarly named
            # collapsed control.
            if self._realtime_60_control_from_ax(nodes) is not None:
                time.sleep(0.25)
                continue
            time.sleep(0.75)
        raise YouTubeRealtimeError(
            "Không xác minh được bộ lọc realtime 60 phút cho video "
            f"{video_id}; tool không dùng nhầm số 48 giờ."
        )

    def _scan_profile_once(
        self,
        client: DonutMcpClient,
        profile_id: str,
        *,
        cancel: threading.Event,
        report: Callable[[str], None],
    ) -> tuple[RealtimeYouTubeVideo, ...]:
        opened_by_this_scan = self._wait_for_profile(
            client,
            profile_id,
            cancel,
            report,
        )
        try:
            return self._scan_open_profile_once(
                client,
                profile_id,
                cancel=cancel,
                report=report,
            )
        finally:
            if opened_by_this_scan or self._retained_scan_profile_can_close(client, profile_id):
                self._close_profile_opened_for_scan(client, profile_id, report)

    @staticmethod
    def _retained_scan_profile_can_close(client: DonutMcpClient, profile_id: str) -> bool:
        """Recover ownership lost when Studio stopped during a previous scan.

        A saved target alone never owns a browser. Require the persisted PID
        and process creation time, and leave any upload/editor or user tab
        alone. The caller still holds this profile's coordinator lease.
        """
        try:
            with _CHECKVIEW_TARGET_LOCK:
                owner = _load_checkview_profile_owners().get(profile_id)
                target = dict(_CHECKVIEW_TARGET_RECORDS.get(profile_id) or {})
            if not owner:
                return False
            if (type(owner.get("process_id")) is not int or owner["process_id"] <= 0
                    or type(owner.get("process_birth")) is not int or owner["process_birth"] <= 0):
                return False
            profile = client.get_profile(profile_id)
            if not profile.process_id:
                _forget_checkview_profile(profile_id)
                return False
            if (owner.get("process_id") != profile.process_id
                    or owner.get("process_birth") != _checkview_process_birth(profile.process_id)):
                _forget_checkview_profile(profile_id)
                return False
            debug = client.get_debug_connection(profile_id)
            pages = [tab for tab in _read_debug_targets(debug) if tab.get("type") == "page"]
            if not pages:
                return False
            for tab in pages:
                url = str(tab.get("url") or "")
                if url == "about:blank":
                    continue
                parsed = urlsplit(url)
                if parsed.scheme != "https" or parsed.hostname != "studio.youtube.com":
                    return False
                if re.fullmatch(r"/channel/UC[A-Za-z0-9_-]+/?", parsed.path):
                    continue
                if (tab.get("id") == target.get("target_id")
                        and re.fullmatch(r"/video/[A-Za-z0-9_-]{6,20}/analytics/.*", parsed.path)):
                    continue
                return False
            current = client.get_profile(profile_id)
            return (current.process_id == owner["process_id"]
                    and _checkview_process_birth(current.process_id) == owner["process_birth"])
        except Exception as exc:
            logger.warning("checkview_retained_profile_ownership_unavailable profile_suffix=%s error=%s",
                           profile_id[-6:], type(exc).__name__)
            return False

    @staticmethod
    def _close_profile_opened_for_scan(
        client: DonutMcpClient,
        profile_id: str,
        report: Callable[[str], None],
    ) -> None:
        """Release only a browser session this Checkview scan started itself."""

        try:
            client.call_tool(
                "batch_stop_profiles",
                {"profile_ids": [profile_id]},
                timeout=20,
            )
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                profile = client.get_profile(profile_id)
                if not profile.process_id:
                    client.confirm_profile_stopped(profile_id, profile=profile)
                    _forget_checkview_profile(profile_id)
                    report("Đã đóng profile Checkview do tool tự mở để nhường slot Wayfern.")
                    return
                time.sleep(0.25)
            report("Donut chưa xác nhận profile Checkview đã đóng; slot có thể vẫn đang bận.")
        except Exception as exc:
            logger.warning(
                "checkview_owned_profile_close_failed profile_suffix=%s error=%s",
                str(profile_id)[-6:],
                type(exc).__name__,
            )
            report("Không đóng được profile Checkview do tool tự mở; cần giải phóng slot thủ công.")

    def _scan_open_profile_once(
        self,
        client: DonutMcpClient,
        profile_id: str,
        *,
        cancel: threading.Event,
        report: Callable[[str], None],
    ) -> tuple[RealtimeYouTubeVideo, ...]:
        debug = client.get_debug_connection(profile_id)
        bridge = _ensure_youtube_browser_bridge(
            client,
            profile_id,
            required_capabilities={
                "youtube-checkview-range-v1",
                "youtube-checkview-content-v1",
            },
        )
        ws_url, target_id, extension_tab_id = _get_or_create_checkview_target(
            client,
            profile_id,
            debug,
            bridge,
            report,
        )
        report("Đang đọc Analytics trong tab checkview nền…")
        with _CdpSession(ws_url) as cdp:
            cdp.command("Page.enable")
            cdp.command("DOM.enable")
            cdp.command("Accessibility.enable")
            cdp.command(
                "Emulation.setFocusEmulationEnabled",
                {"enabled": True},
            )
            channel_id = self._channel_id(cdp, cancel)
            links = self._video_links(
                cdp,
                channel_id,
                cancel,
                report=report,
                extension_bridge=bridge,
                extension_tab_id=extension_tab_id,
                profile_id=profile_id,
            )
            report(
                f"Đã tìm thấy {len(links)} video · "
                "đang khóa và xác minh realtime 60 phút…"
            )
            videos: list[RealtimeYouTubeVideo] = []
            for index, (video_id, title) in enumerate(links, start=1):
                self._cancelled(cancel)
                visibility = self._public_visibility(video_id)
                if visibility == "UNKNOWN":
                    report(
                        f"Chưa xác minh được quyền riêng tư của {video_id}; "
                        "không được coi là scan hoàn tất."
                    )
                    raise YouTubeRealtimeError(
                        "Danh sách video YouTube chưa tải đầy đủ: "
                        f"chưa xác minh được quyền riêng tư của {video_id}."
                    )
                if visibility != "PUBLIC":
                    label = {
                        "UNLISTED": "Không công khai",
                        "PRIVATE_OR_UNAVAILABLE": "Riêng tư/chưa phát hành",
                    }.get(visibility, "chưa xác minh được quyền riêng tư")
                    report(
                        f"Bỏ qua {video_id}: {label}; không đọc view hoặc "
                        "gửi đơn 1MM"
                    )
                    continue
                video_attempts = 2
                views: int | None = None
                last_error: YouTubeRealtimeError | None = None
                for video_attempt in range(video_attempts):
                    try:
                        views = self._video_realtime_views(
                            cdp,
                            video_id,
                            cancel,
                            extension_bridge=bridge,
                            extension_tab_id=extension_tab_id,
                            profile_id=profile_id,
                        )
                        break
                    except YouTubeRealtimeError as exc:
                        last_error = exc
                        if (
                            video_attempt + 1 >= video_attempts
                            or not _is_video_analytics_error(exc)
                        ):
                            break
                        report(
                            f"Analytics của {video_id} trả lỗi tạm thời; "
                            "đang tải lại đúng video một lần…"
                        )
                if views is None:
                    exc = last_error or YouTubeRealtimeError(
                        f"Không đọc được realtime 60 phút của {video_id}."
                    )
                    report(
                        f"Chưa đọc được realtime 60 phút của {video_id}: {exc}; "
                        "không dùng dữ liệu dở dang để đặt Like."
                    )
                    raise YouTubeRealtimeError(
                        "Danh sách video YouTube chưa tải đầy đủ: "
                        f"chưa đọc được realtime 60 phút của {video_id} ({exc})."
                    ) from exc
                videos.append(
                    RealtimeYouTubeVideo(
                        profile_id=profile_id,
                        video_id=video_id,
                        title=title,
                        realtime_views=views,
                        visibility="PUBLIC",
                    )
                )
                report(
                    f"Video {index}/{len(links)} · "
                    f"{views:,} view trong 60 phút · đã xác minh"
                )
            report("Đã lưu target Checkview để tái tạo an toàn ở lượt sau.")
            return tuple(videos)

    def scan_profile(
        self,
        profile_id: str,
        *,
        cancel_event: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[RealtimeYouTubeVideo, ...]:
        cancel = cancel_event or threading.Event()
        report = progress or (lambda _message: None)
        lane_acquired = False
        try:
            _acquire_checkview_scan_lane(
                cancel,
                report,
                profile_id=profile_id,
            )
            lane_acquired = True
            lease = self.coordinator.try_acquire(
                profile_id,
                cancel,
                job_kind="monitor",
                priority=0,
            )
            if lease is None:
                raise YouTubeRealtimeDeferred(
                    "Profile đang dành cho upload; lượt kiểm tra view này được hoãn."
                )
            with lease:
                max_transport_attempts = 3
                for attempt in range(max_transport_attempts):
                    try:
                        # Keep a monitor scan on its own MCP session. Starting
                        # a profile can be a long Donut operation and must
                        # not block the global connection check or another
                        # workspace's short metadata reads.
                        with DonutMcpClient(
                            self.endpoint,
                            timeout=40,
                            shared_session=False,
                        ) as client:
                            client.require_docurandom_local()
                            try:
                                return self._scan_profile_once(
                                    client,
                                    profile_id,
                                    cancel=cancel,
                                    report=report,
                                )
                            except Exception as exc:
                                if not _is_transient_checkview_transport_error(exc):
                                    raise
                                self._invalidate_checkview_target(
                                    client,
                                    profile_id,
                                    report,
                                )
                                raise
                    except YouTubeUploadCancelled:
                        raise
                    except Exception as exc:
                        if (
                            attempt + 1 >= max_transport_attempts
                            or not _is_transient_checkview_transport_error(exc)
                        ):
                            raise
                        if _is_stale_dom_node_error(exc):
                            report(
                                "Studio vừa thay đổi DOM trong lúc đọc; "
                                "đang tạo lại target checkview và quét lại từ đầu "
                                f"({attempt + 2}/{max_transport_attempts})…"
                            )
                        elif _is_incomplete_checkview_scan_error(exc):
                            report(
                                "Danh sách video chưa tải đầy đủ; "
                                "đang tạo lại target checkview và quét lại từ đầu "
                                f"({attempt + 2}/{max_transport_attempts})…"
                            )
                        else:
                            report(
                                "Kết nối checkview bị đóng; "
                                f"đang tự mở lại ({attempt + 2}/{max_transport_attempts})…"
                            )
                        if cancel.wait(1.0 + attempt):
                            raise YouTubeUploadCancelled(
                                "Đã dừng chờ khôi phục checkview."
                            )
        finally:
            if lane_acquired:
                _CHECKVIEW_SCAN_LANE.release()


class YouTubeLikeMonitor:
    """Checkpointed auto-order policy over Studio's rolling 60-minute views."""

    def __init__(
        self,
        *,
        state_path: Path,
        smm_client: SmmApiClient,
        realtime_source: DonutYouTubeRealtimeSource,
        policy: YouTubeLikePolicy | None = None,
    ) -> None:
        self.state_path = state_path
        self.smm_client = smm_client
        self.realtime_source = realtime_source
        self.policy = policy or YouTubeLikePolicy()

    def _load_state(self) -> dict[str, object]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "videos": {}}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise YouTubeRealtimeError(
                "Checkpoint tự động Like không đọc được; từ chối đặt đơn để tránh trùng."
            ) from exc
        videos = payload.get("videos") if isinstance(payload, Mapping) else None
        if not isinstance(videos, Mapping):
            raise YouTubeRealtimeError(
                "Checkpoint tự động Like sai cấu trúc; từ chối đặt đơn để tránh trùng."
            )
        return {"version": 1, "videos": dict(videos)}

    def _claim(
        self,
        video: RealtimeYouTubeVideo,
        *,
        quantity: int,
        basis_points: int,
        service: SmmService,
        now: datetime,
    ) -> bool:
        with _STATE_LOCK:
            state = self._load_state()
            videos = state["videos"]
            assert isinstance(videos, dict)
            existing = videos.get(video.identity)
            if isinstance(existing, Mapping) and (
                str(existing.get("status") or "") == "ordering"
                or (
                    str(existing.get("status") or "") == "uncertain"
                    and not _is_retryable_order_record(existing)
                )
            ):
                return False
            record = dict(existing) if isinstance(existing, Mapping) else {}
            record.update(
                {
                    "status": "ordering",
                    "profile_id": video.profile_id,
                    "video_id": video.video_id,
                    "title": video.title,
                    "youtube_url": video.youtube_url,
                    "ratio_basis_points": basis_points,
                    "updated_at": now.astimezone(timezone.utc).isoformat(),
                    "pending": {
                        "realtime_views": video.realtime_views,
                        "quantity": quantity,
                        "service_id": service.service_id,
                    },
                }
            )
            videos[video.identity] = record
            atomic_write_json(self.state_path, state)
            return True

    def _finish(
        self,
        identity: str,
        *,
        status: str,
        now: datetime,
        order_id: str = "",
        detail: str = "",
    ) -> None:
        with _STATE_LOCK:
            state = self._load_state()
            videos = state["videos"]
            assert isinstance(videos, dict)
            existing = videos.get(identity)
            if not isinstance(existing, Mapping):
                return
            updated = dict(existing)
            pending = updated.pop("pending", None)
            timestamp = now.astimezone(timezone.utc).isoformat()
            updated.update(
                {"status": status, "detail": detail, "updated_at": timestamp}
            )
            if status == "active" and isinstance(pending, Mapping):
                orders = updated.get("orders")
                order_history = list(orders) if isinstance(orders, list) else []
                order_history.append(
                    {
                        "order_id": order_id,
                        "quantity": int(pending.get("quantity") or 0),
                        "realtime_views": int(pending.get("realtime_views") or 0),
                        "service_id": str(pending.get("service_id") or ""),
                        "ordered_at": timestamp,
                    }
                )
                updated.update(
                    {
                        "orders": order_history,
                        "last_order_id": order_id,
                        "last_ordered_at": timestamp,
                    }
                )
            videos[identity] = updated
            atomic_write_json(self.state_path, state)

    @staticmethod
    def _cheapest_eligible_service(
        services: tuple[SmmService, ...], quantity: int
    ) -> SmmService | None:
        eligible = [
            service
            for service in services
            if service.minimum <= quantity <= service.maximum
        ]
        return min(
            eligible,
            key=lambda service: (
                service.rate,
                service.minimum,
                service.name.casefold(),
                service.service_id,
            ),
            default=None,
        )

    def scan_once(
        self,
        *,
        profile_id: str,
        services: tuple[SmmService, ...],
        service_loader: Callable[[], tuple[SmmService, ...]] | None = None,
        cancel_event: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
        now: datetime | None = None,
        allow_orders: bool = True,
    ) -> tuple[YouTubeLikeMonitorResult, ...]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cancel = cancel_event or threading.Event()
        videos = self.realtime_source.scan_profile(
            profile_id,
            cancel_event=cancel,
            progress=progress,
        )
        if cancel.is_set():
            raise YouTubeUploadCancelled("Đã dừng theo dõi realtime YouTube.")
        # Automatic callers can reload the provider catalog here, after the
        # slower Studio scan and immediately before price/min/max comparison.
        # This prevents a cached rate from selecting yesterday's cheapest
        # package when the SMM provider changes its catalog.
        if service_loader is not None:
            services = tuple(service_loader())
        state = self._load_state()
        recorded = state.get("videos")
        recorded = recorded if isinstance(recorded, Mapping) else {}
        results: list[YouTubeLikeMonitorResult] = []
        for video in sorted(videos, key=lambda item: item.realtime_views, reverse=True):
            if cancel.is_set():
                raise YouTubeUploadCancelled("Đã dừng theo dõi realtime YouTube.")
            prior = recorded.get(video.identity)
            if isinstance(prior, Mapping) and (
                str(prior.get("status") or "") == "ordering"
                or (
                    str(prior.get("status") or "") == "uncertain"
                    and not _is_retryable_order_record(prior)
                )
            ):
                continue
            last_ordered_at = (
                _parse_datetime(prior.get("last_ordered_at"))
                if isinstance(prior, Mapping)
                else None
            )
            if last_ordered_at is not None:
                next_allowed = last_ordered_at + timedelta(
                    minutes=self.policy.cooldown_minutes
                )
                if current < next_allowed:
                    remaining = max(
                        1,
                        int((next_allowed - current).total_seconds() // 60) + 1,
                    )
                    results.append(
                        YouTubeLikeMonitorResult(
                            identity=video.identity,
                            youtube_url=video.youtube_url,
                            realtime_views=video.realtime_views,
                            state="cooldown",
                            message=f"Đã đặt Like · xét lại sau {remaining} phút",
                            video_title=video.title,
                        )
                    )
                    continue
            quantity, basis_points = self.policy.quantity_for(
                video.identity, video.realtime_views
            )
            if quantity < self.policy.minimum_likes:
                results.append(
                    YouTubeLikeMonitorResult(
                        identity=video.identity,
                        youtube_url=video.youtube_url,
                        realtime_views=video.realtime_views,
                        state="waiting_minimum",
                        video_title=video.title,
                        quantity=quantity,
                        ratio_basis_points=basis_points,
                        message=(
                            f"{video.realtime_views:,} view/60 phút → {quantity:,} Like "
                            f"({basis_points / 100:.2f}%) · chờ tối thiểu "
                            f"{self.policy.minimum_likes:,} Like"
                        ),
                    )
                )
                continue
            service = self._cheapest_eligible_service(services, quantity)
            if service is None:
                results.append(
                    YouTubeLikeMonitorResult(
                        identity=video.identity,
                        youtube_url=video.youtube_url,
                        realtime_views=video.realtime_views,
                        state="no_eligible_service",
                        video_title=video.title,
                        quantity=quantity,
                        ratio_basis_points=basis_points,
                        message=(
                            f"Cần {quantity:,} Like nhưng chưa có gói YouTube Like nhận "
                            "đúng số lượng này."
                        ),
                    )
                )
                continue
            if not allow_orders:
                results.append(
                    YouTubeLikeMonitorResult(
                        identity=video.identity,
                        youtube_url=video.youtube_url,
                        realtime_views=video.realtime_views,
                        state="dry_run_eligible",
                        video_title=video.title,
                        quantity=quantity,
                        ratio_basis_points=basis_points,
                        service_id=service.service_id,
                        message=(
                            f"Đủ tiêu chí: {video.realtime_views:,} view/60 phút → "
                            f"{quantity:,} Like · gói rẻ nhất #{service.service_id}; "
                            "chế độ chỉ đọc nên không đặt đơn."
                        ),
                    )
                )
                continue
            if cancel.is_set():
                raise YouTubeUploadCancelled("Đã dừng trước khi gửi đơn 1MM.")
            if not self._claim(
                video,
                quantity=quantity,
                basis_points=basis_points,
                service=service,
                now=current,
            ):
                continue
            try:
                order_id = self.smm_client.add_order(
                    service.service_id,
                    video.youtube_url,
                    quantity=quantity,
                )
            except Exception as exc:
                detail = " ".join(str(exc).split())[:500]
                if isinstance(exc, SmmProviderRejectedError):
                    self._finish(
                        video.identity,
                        status="retryable",
                        now=current,
                        detail=detail,
                    )
                    results.append(
                        YouTubeLikeMonitorResult(
                            identity=video.identity,
                            youtube_url=video.youtube_url,
                            realtime_views=video.realtime_views,
                            state="retryable",
                            video_title=video.title,
                            quantity=quantity,
                            ratio_basis_points=basis_points,
                            service_id=service.service_id,
                            message=(
                                "1MM đã từ chối đơn và không tạo mã đơn; "
                                "sẽ bỏ qua lượt này và thử lại ở vòng quét sau: "
                                + detail
                            ),
                        )
                    )
                    continue
                self._finish(
                    video.identity,
                    status="uncertain",
                    now=current,
                    detail=detail,
                )
                results.append(
                    YouTubeLikeMonitorResult(
                        identity=video.identity,
                        youtube_url=video.youtube_url,
                        realtime_views=video.realtime_views,
                        state="uncertain",
                        video_title=video.title,
                        quantity=quantity,
                        ratio_basis_points=basis_points,
                        service_id=service.service_id,
                        message=(
                            "Đã khóa chống trùng nhưng chưa xác nhận được mã đơn 1MM: "
                            + detail
                        ),
                    )
                )
                break
            self._finish(
                video.identity,
                status="active",
                now=current,
                order_id=order_id,
            )
            results.append(
                YouTubeLikeMonitorResult(
                    identity=video.identity,
                    youtube_url=video.youtube_url,
                    realtime_views=video.realtime_views,
                    state="ordered",
                    video_title=video.title,
                    quantity=quantity,
                    ratio_basis_points=basis_points,
                    service_id=service.service_id,
                    order_id=order_id,
                    message=(
                        f"{video.realtime_views:,} view/60 phút → đã đặt {quantity:,} Like "
                        f"({basis_points / 100:.2f}%) · gói #{service.service_id} · "
                        f"đơn #{order_id}"
                    ),
                )
            )
        return tuple(results)


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
