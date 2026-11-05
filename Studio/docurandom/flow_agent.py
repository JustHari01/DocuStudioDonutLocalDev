from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from PIL import Image, UnidentifiedImageError

from .storage import atomic_write_json


DEFAULT_API_BASE = "http://127.0.0.1:8001"
FLOW_WEB_URL = "https://flow.google.com/"
FLOW_EXTENSION_VERSION = "1.3.52"
FLOW_REQUIRED_IMAGE_MODEL = "harbor_seal"
FLOW_REQUIRED_IMAGE_COUNT = 1
CODEX_INSTALL_URL = "https://developers.openai.com/codex/cli/"
ProgressCallback = Callable[[float, str], None]
_EDGE_LAUNCH_LOCK = threading.Lock()
_LAST_EDGE_LAUNCH_AT = 0.0
_EDGE_LAUNCH_COOLDOWN_SECONDS = 90.0
_FLOW_BACKEND_START_LOCK = threading.Lock()
_FLOW_BACKEND_START_TIMEOUT_SECONDS = 25.0
# Flow has one resident browser client in normal desktop use.  Startup and
# schedule restoration can make many workspace threads call open_flow at the
# same time; serialize that control-plane operation so the extension receives
# one prepare request and the remaining callers wait for the same session.
_FLOW_OPEN_LOCK = threading.Lock()
_FLOW_OPEN_WAIT_SECONDS = 120.0
_FLOW_OPEN_POLL_SECONDS = 0.5
# Every workspace owns a lightweight monitor, but the Flow backend is shared.
# Coalesce passive health probes process-wide so a dozen hidden workspace
# windows cannot turn one slow Windows/HTTP check into a false outage storm.
_FLOW_PASSIVE_HEALTH_CACHE_SECONDS = 4.0
_FLOW_PASSIVE_HEALTH_FAILURE_CACHE_SECONDS = 1.0
_FLOW_PASSIVE_HEALTH_WAIT_SECONDS = 4.0
_FLOW_PASSIVE_HEALTH_CONDITION = threading.Condition()
_FLOW_PASSIVE_HEALTH_CACHE: dict[
    str, tuple[float, dict[str, object] | None, str]
] = {}
_FLOW_PASSIVE_HEALTH_INFLIGHT: set[str] = set()
# A busy response is a transient browser lock, not a reason to keep a
# workspace spinning forever.  The extension has its own stale-job reset;
# this upper bound lets the desktop return to the checkpoint if that reset
# cannot complete (for example when Edge is no longer responding).
FLOW_UI_BUSY_MAX_WAIT_SECONDS = 10 * 60
FLOW_UI_GENERATION_SETTLE_POLL_SECONDS = 8.0
FLOW_UI_MAX_UNSENT_ATTEMPTS = 6
# A disconnected Flow extension can reconnect after Edge recreates its
# renderer/WebSocket.  The old three-attempt cap was shorter than that
# lifecycle and made a safe, pre-dispatch 503 look like a permanent failure.
# Keep the retry bounded so a genuinely closed browser still returns to the
# checkpoint, but give the resident extension enough time to reconnect.
FLOW_CONNECTION_MAX_WAIT_SECONDS = 180.0
_FLOW_PROJECT_ERROR_RE = re.compile(
    r"(?:^|[:;\s])(?:flow[_-]?project(?:[_-]?id)?|project(?:[_-]?id)?)\s*=\s*"
    r"([A-Za-z0-9][A-Za-z0-9_-]{7,})",
    re.IGNORECASE,
)
_FLOW_PHASE_RE = re.compile(r"\bphase\s*=\s*([a-z0-9_-]+)", re.IGNORECASE)
_PRE_SUBMISSION_FLOW_PHASES = frozenset({"project", "configure-ui", "configure"})


def _claim_edge_launch(*, force: bool = False) -> bool:
    """Allow one browser launch across every workspace in this process."""

    global _LAST_EDGE_LAUNCH_AT
    now = time.monotonic()
    with _EDGE_LAUNCH_LOCK:
        if (
            not force
            and _LAST_EDGE_LAUNCH_AT > 0.0
            and now - _LAST_EDGE_LAUNCH_AT < _EDGE_LAUNCH_COOLDOWN_SECONDS
        ):
            return False
        _LAST_EDGE_LAUNCH_AT = now
        return True


class FlowAgentError(RuntimeError):
    pass


def _passive_health_request(api_base: str, *, timeout: float = 2.5) -> dict:
    """Share passive health requests across all workspace monitors.

    The desktop has one Flow backend but many workspace sessions. A monitor
    must never start a second request while the first one is still probing the
    backend; waiters reuse the same result (or the same short-lived failure).
    Active/manual checks continue to use the uncached request path.
    """

    key = api_base.rstrip("/")
    deadline = time.monotonic() + max(
        _FLOW_PASSIVE_HEALTH_WAIT_SECONDS,
        float(timeout) + 1.0,
    )
    while True:
        with _FLOW_PASSIVE_HEALTH_CONDITION:
            now = time.monotonic()
            cached = _FLOW_PASSIVE_HEALTH_CACHE.get(key)
            if cached is not None:
                cached_at, payload, error = cached
                cache_ttl = (
                    _FLOW_PASSIVE_HEALTH_CACHE_SECONDS
                    if payload is not None
                    else _FLOW_PASSIVE_HEALTH_FAILURE_CACHE_SECONDS
                )
                if now - cached_at <= cache_ttl:
                    if payload is None:
                        raise FlowAgentError(
                            error or "Flow health probe failed."
                        )
                    return dict(payload)

            if key not in _FLOW_PASSIVE_HEALTH_INFLIGHT:
                _FLOW_PASSIVE_HEALTH_INFLIGHT.add(key)
                break

            remaining = deadline - now
            if remaining <= 0:
                # A previous healthy result is safer than declaring every
                # workspace offline while the shared probe is still unwinding.
                if cached is not None and cached[1] is not None:
                    return dict(cached[1])
                raise FlowAgentError(
                    "Flow health probe đang chờ một lần kiểm tra dùng chung quá lâu."
                )
            _FLOW_PASSIVE_HEALTH_CONDITION.wait(timeout=min(remaining, 0.25))

    try:
        try:
            result = _json_request(f"{key}/health", timeout=timeout)
        except Exception as exc:
            with _FLOW_PASSIVE_HEALTH_CONDITION:
                _FLOW_PASSIVE_HEALTH_CACHE[key] = (
                    time.monotonic(),
                    None,
                    str(exc),
                )
            raise
        payload = dict(result) if isinstance(result, dict) else {}
        with _FLOW_PASSIVE_HEALTH_CONDITION:
            _FLOW_PASSIVE_HEALTH_CACHE[key] = (
                time.monotonic(),
                payload,
                "",
            )
        return dict(payload)
    finally:
        with _FLOW_PASSIVE_HEALTH_CONDITION:
            _FLOW_PASSIVE_HEALTH_INFLIGHT.discard(key)
            _FLOW_PASSIVE_HEALTH_CONDITION.notify_all()


class FlowAgentQuotaError(FlowAgentError):
    pass


class FlowAgentCaptchaError(FlowAgentError):
    pass


class FlowAgentRateLimitError(FlowAgentError):
    pass


class FlowAgentWindowError(FlowAgentError):
    pass


class FlowAgentConnectionError(FlowAgentWindowError):
    """No request was dispatched; a missing socket does not prove Edge closed."""


class FlowAgentUiError(FlowAgentError):
    pass


class FlowAgentSubmissionUncertain(FlowAgentUiError):
    """Submission could have occurred; automatic resending is unsafe."""


class FlowAgentImageError(FlowAgentError):
    pass


class FlowAgentIncompleteError(FlowAgentError):
    """Raised when a Flow batch has not produced one valid image per prompt."""


class FlowAgentCancelled(Exception):
    pass


T = TypeVar("T")


class FlowGenerationCoordinator:
    """A process-wide, cancellable FIFO for complete workspace image batches."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._waiting: list[tuple[str, str]] = []
        self._active_ticket = ""
        self._quota_blocked = ""

    def clear_quota_block(self) -> None:
        with self._condition:
            self._quota_blocked = ""
            self._condition.notify_all()

    def execute(
        self,
        job_id: str,
        operation: Callable[[], T],
        *,
        cancel_event: threading.Event | None = None,
        on_queue: Callable[[int, str], None] | None = None,
        readiness: Callable[[], FlowAgentStatus] | None = None,
    ) -> T:
        ticket = uuid.uuid4().hex
        clean_job_id = job_id.strip() or ticket
        with self._condition:
            self._waiting.append((ticket, clean_job_id))
            self._condition.notify_all()

        last_notice: tuple[int, str] | None = None
        next_probe_at = 0.0
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise FlowAgentCancelled("Đã hủy khi đang chờ lượt tạo ảnh Flow.")
                probe_quota = False
                with self._condition:
                    position = next(
                        (
                            index
                            for index, (candidate, _job) in enumerate(self._waiting, 1)
                            if candidate == ticket
                        ),
                        0,
                    )
                    if position == 0:
                        raise FlowAgentCancelled("Lượt tạo ảnh không còn trong hàng đợi.")
                    blocked = self._quota_blocked
                    if (
                        position == 1
                        and not self._active_ticket
                        and not blocked
                    ):
                        self._active_ticket = ticket
                        self._waiting.pop(0)
                        break
                    now = time.monotonic()
                    if blocked and readiness is not None and now >= next_probe_at:
                        probe_quota = True
                        next_probe_at = now + 5.0
                    message = (
                        "Hàng đợi tạo ảnh đang tạm dừng vì tài khoản Flow đã hết lượt. "
                        "Hãy đổi tài khoản; tool sẽ tự kiểm tra và chạy tiếp."
                        if blocked
                        else f"Đang chờ lượt tạo ảnh · vị trí {position} trong hàng đợi…"
                    )
                    notice = (position, message)
                    self._condition.wait(timeout=0.25)
                if notice != last_notice and on_queue is not None:
                    on_queue(*notice)
                    last_notice = notice
                if probe_quota and readiness is not None:
                    try:
                        status = readiness()
                    except Exception:
                        status = None
                    if status is not None and status.ready and (status.credits or 0) > 0:
                        self.clear_quota_block()

            if on_queue is not None:
                on_queue(0, "Đã đến lượt workspace này · bắt đầu tạo ảnh…")
            try:
                return operation()
            except FlowAgentQuotaError as exc:
                with self._condition:
                    self._quota_blocked = str(exc)
                    self._condition.notify_all()
                raise
        finally:
            with self._condition:
                self._waiting = [item for item in self._waiting if item[0] != ticket]
                if self._active_ticket == ticket:
                    self._active_ticket = ""
                self._condition.notify_all()


@dataclass(frozen=True)
class FlowAgentStatus:
    edge_path: Path | None
    extension_dir: Path | None
    extension_installed: bool
    backend_running: bool
    extension_connected: bool
    has_flow_key: bool
    credits: int | None = None
    detail: str = ""
    extension_version: str = ""
    extension_compatible: bool = True
    browser_open: bool | None = None
    flow_tab_count: int = 0
    ui_image_capable: bool = False
    connected_client_count: int = 0
    ui_client_count: int = 0
    flow_client_count_with_tabs: int = 0

    @property
    def ready(self) -> bool:
        # Current Flow Agent builds generate through the visible project UI.
        # A cached legacy bearer token is not enough when that UI capability is
        # advertised: the browser must still own a real Flow tab. Otherwise a
        # freshly reopened Edge worker can make precheck green for a few
        # seconds, then every image request fails after ChatGPT has finished.
        usable_session = bool(
            (
                self.ui_image_capable
                and self.browser_open is not False
                and self.flow_tab_count > 0
            )
            or (self.has_flow_key and not self.ui_image_capable)
        )
        return bool(
            self.edge_path
            and self.extension_dir
            and self.backend_running
            and self.extension_connected
            and usable_session
            and self.extension_compatible
        )


@dataclass(frozen=True)
class FlowGeneratedImage:
    prompt_index: int
    prompt: str
    path: Path
    model: str
    media_id: str = ""


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _edge_user_data_dir() -> Path | None:
    configured = os.environ.get("DOCURANDOM_FLOW_EDGE_USER_DATA", "").strip().strip('"')
    if not configured:
        return None
    return Path(configured).expanduser()


def _edge_user_data_arguments() -> list[str]:
    user_data_dir = _edge_user_data_dir()
    if user_data_dir is None:
        return []
    return [f"--user-data-dir={user_data_dir}"]


def discover_edge() -> Path | None:
    configured = os.environ.get("DOCURANDOM_EDGE_PATH", "").strip().strip('"')
    candidates = [Path(configured)] if configured else []
    for env_name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    discovered = shutil.which("msedge")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _resource_roots() -> Iterable[Path]:
    configured = os.environ.get("DOCURANDOM_FLOW_EXTENSION", "").strip().strip('"')
    if configured:
        yield Path(configured)
    frozen_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    yield frozen_root / "flow-extension"
    yield Path(__file__).resolve().parents[1] / "flow-extension"
    yield Path.home() / "Desktop" / "flow-agent-main" / "dist" / "Flow-Agent-Edge-v1.2.5"
    yield Path.home() / "Desktop" / "flow-agent-main" / "flow-extension"


def discover_extension_dir() -> Path | None:
    seen: set[str] = set()
    for candidate in _resource_roots():
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        manifest = resolved / "manifest.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("manifest_version") == 3 and data.get("name") == "Flow Agent":
            return resolved
    return None


def _edge_profiles_root() -> Path | None:
    configured = _edge_user_data_dir()
    if configured is not None:
        return configured
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    return Path(local) / "Microsoft" / "Edge" / "User Data"


@dataclass(frozen=True)
class EdgeFlowExtensionInstallation:
    """One Flow Agent entry as recorded by an Edge profile."""

    extension_id: str
    profile_dir: Path
    path: Path | None
    version: str
    disabled: bool


def _edge_profile_directories() -> tuple[Path, ...]:
    root = _edge_profiles_root()
    if root is None or not root.is_dir():
        return ()
    try:
        profiles = [
            profile
            for profile in root.iterdir()
            if profile.is_dir()
            and (profile.name == "Default" or profile.name.startswith("Profile "))
        ]
    except OSError:
        return ()
    return tuple(sorted(profiles, key=lambda value: value.name.lower()))


def _flow_manifest_at(path_value: object) -> tuple[Path | None, dict[str, object]]:
    raw_path = str(path_value or "").strip()
    if not raw_path:
        return None, {}
    try:
        extension_path = Path(raw_path).expanduser().resolve(strict=False)
        manifest = json.loads(
            (extension_path / "manifest.json").read_text(encoding="utf-8")
        )
    except Exception:
        return None, {}
    return extension_path, manifest if isinstance(manifest, dict) else {}


def edge_flow_extension_installations() -> tuple[EdgeFlowExtensionInstallation, ...]:
    """Read Flow Agent installations without opening or modifying Edge."""

    found: dict[tuple[str, str], EdgeFlowExtensionInstallation] = {}
    for profile in _edge_profile_directories():
        for filename in ("Secure Preferences", "Preferences"):
            preferences_path = profile / filename
            try:
                value = json.loads(preferences_path.read_text(encoding="utf-8"))
                settings = value.get("extensions", {}).get("settings", {})
            except Exception:
                continue
            if not isinstance(settings, dict):
                continue
            for extension_id, item in settings.items():
                if not isinstance(item, dict):
                    continue
                clean_id = str(extension_id).strip().lower()
                if not clean_id:
                    continue
                embedded = item.get("manifest")
                embedded_manifest = embedded if isinstance(embedded, dict) else {}
                unpacked_path, unpacked_manifest = _flow_manifest_at(item.get("path"))
                is_flow_agent = (
                    embedded_manifest.get("name") == "Flow Agent"
                    or (
                        unpacked_manifest.get("manifest_version") == 3
                        and unpacked_manifest.get("name") == "Flow Agent"
                    )
                )
                if not is_flow_agent:
                    continue
                version = str(
                    embedded_manifest.get("version")
                    or unpacked_manifest.get("version")
                    or ""
                ).strip()
                key = (os.path.normcase(str(profile)), clean_id)
                installation = EdgeFlowExtensionInstallation(
                    extension_id=clean_id,
                    profile_dir=profile,
                    path=unpacked_path,
                    version=version,
                    disabled=bool(item.get("disable_reasons")),
                )
                previous = found.get(key)
                # Secure Preferences and Preferences may both contain the
                # same entry; prefer the copy with path/version information.
                if previous is None or (
                    (previous.path is None and installation.path is not None)
                    or (not previous.version and installation.version)
                ):
                    found[key] = installation
    return tuple(found.values())


def detect_edge_extension_installed() -> bool:
    return any(
        not installation.disabled
        for installation in edge_flow_extension_installations()
    )


def edge_flow_extension_ids() -> list[str]:
    """Return active installed Flow Agent IDs without opening Edge settings."""

    return list(dict.fromkeys(
        installation.extension_id
        for installation in edge_flow_extension_installations()
        if (
            not installation.disabled
            and len(installation.extension_id) == 32
            and installation.extension_id.isalpha()
        )
    ))


def _json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    timeout: float = 8.0,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    body = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw).get("detail", raw)
        except Exception:
            detail = raw or str(exc)
        raise FlowAgentError(str(detail)) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise FlowAgentError(str(exc)) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FlowAgentError("Flow Agent trả về dữ liệu không hợp lệ.") from exc
    if not isinstance(value, dict):
        raise FlowAgentError("Flow Agent trả về dữ liệu không hợp lệ.")
    return value


def _friendly_generation_error(error: BaseException) -> FlowAgentError:
    detail = str(error).strip() or error.__class__.__name__
    lowered = unicodedata.normalize("NFKC", detail).casefold()
    # This marker means the extension positively observed the submit action.
    # Never classify it as a retryable UI timeout: retrying could create a
    # duplicate generation and would violate the checkpoint's no-resend rule.
    if "flow_prompt_submitted_no_image" in lowered:
        return FlowAgentError(detail)
    if "flow_ui_login_required" in lowered or (
        "accounts.google.com/" in lowered and "cannot access contents" in lowered
    ):
        return FlowAgentUiError(
            "Phiên Google Flow đang yêu cầu đăng nhập Google trong Edge. Hãy hoàn tất "
            "đăng nhập trên đúng tab rồi bấm Tiếp tục. Prompt chưa được gửi; không "
            "cần cấp quyền tiện ích cho trang đăng nhập. Chi tiết: FLOW_UI_LOGIN_REQUIRED"
        )
    if any(
        marker in lowered
        for marker in (
            "extension manifest must request permission",
            "cannot access contents of url",
        )
    ):
        return FlowAgentUiError(
            "Tiện ích Flow chưa được cấp quyền cho địa chỉ Google Flow hiện tại. "
            "Prompt chưa được gửi và vẫn được giữ nguyên trong checkpoint. Hãy cập "
            "nhật/nạp lại tiện ích rồi bấm Tiếp tục để chỉ gửi prompt còn thiếu. "
            f"Chi tiết: {detail}"
        )
    if any(
        marker in lowered
        for marker in (
            "google flow extension is not connected",
            "flow_ui_extension_not_available",
            "no extensions connected",
            "extension bridge is not initialized",
        )
    ):
        return FlowAgentConnectionError(
            "Flow Agent vừa mất kết nối với Edge/Google Flow. Đã dừng tại prompt "
            "chưa gửi và giữ nguyên checkpoint; không mở thêm tab hoặc tải lại "
            "trang. Kiểm tra tiện ích Flow trong Edge rồi bấm Tiếp tục. "
            f"Chi tiết: {detail}"
        )
    pre_submission_phase = (
        _pre_submission_flow_phase(detail)
        if "flow_ui_submission_uncertain" in lowered
        or "flow_ui_timeout_unsent" in lowered
        else ""
    )
    if pre_submission_phase:
        return FlowAgentUiError(
            "Flow chưa gửi prompt vì lỗi xảy ra trước thao tác gửi; driver sẽ tự thử lại "
            "đúng prompt trên project hiện tại. Chi tiết: "
            f"FLOW_UI_TIMEOUT_UNSENT:phase={pre_submission_phase}"
        )
    if "flow_ui_timeout_unsent" in lowered:
        return FlowAgentUiError(
            "Flow chưa gửi prompt; driver sẽ tự thử lại đúng prompt trên project hiện tại. "
            f"Chi tiết: {detail}"
        )
    if "flow_ui_submission_uncertain" in lowered and "phase=no-progress" in lowered:
        return FlowAgentUiError(
            "Flow chưa bắt đầu xử lý lệnh trên tab nền; driver sẽ được phục hồi "
            "ngay trên tab hiện tại và tự thử lại prompt này. Prompt chưa được "
            "đánh dấu là đã gửi. Chi tiết: FLOW_UI_TIMEOUT_UNSENT:phase=no-progress"
        )
    if "flow_ui_submission_uncertain" in lowered:
        return FlowAgentSubmissionUncertain(
            "Flow chưa xác nhận prompt đã gửi hay chưa; đã giữ checkpoint và khóa "
            "gửi lại prompt này để tránh tạo ảnh trùng. Kiểm tra dự án Flow trước "
            f"khi xử lý tiếp. Chi tiết: {detail}"
        )
    if (
        "flow_ui_command_timeout:workspace-flow-tab" in lowered
        or "flow_workspace_project_reopen_failed" in lowered
    ):
        return FlowAgentWindowError(
            "Flow chưa mở lại được đúng project từ checkpoint trong Edge. Prompt chưa "
            "được gửi và vẫn được giữ nguyên; tool sẽ thử lại đúng project ở lượt kế "
            "tiếp, không tạo project mới và không gửi trùng. Chi tiết: "
            f"{detail}"
        )
    if any(
        marker in lowered
        for marker in (
            "flow_ui_prompt_not_inserted",
            "flow_ui_command_timeout",
            "flow_ui_submit_not_found",
            "flow_ui_submit_not_confirmed",
            "flow_ui_content_timeout",
            "flow_ui_busy",
            "flow_ui_no_valid_images",
            "flow_ui_generation_failed",
            "flow_ui_version_mismatch",
            "flow_ui_required_image_config_not_confirmed",
            "flow_ui_image_count_not_confirmed",
            "flow_ui_image_model_not_confirmed",
            "flow_ui_timeout",
            "flow_ui_timeout:prompt-editor",
            "flow_ui_timeout:submit",
            "flow_ui_element_not_found:prompt-editor",
            "flow_ui_element_not_found:submit",
            "flow_ui_automation_failed",
        )
    ):
        return FlowAgentUiError(
            "Tiện ích Flow không điều khiển được ô nhập hoặc nút gửi của giao diện "
            "Google Flow hiện tại. Prompt vẫn được giữ nguyên trong checkpoint, chưa "
            "bị đánh dấu bỏ qua. Tool không tự tải lại trang Flow. Hãy kiểm tra "
            "tab Flow rồi bấm Tiếp tục để chỉ gửi prompt còn thiếu. "
            f"Chi tiết: {detail}"
        )
    if any(
        marker in lowered
        for marker in (
            "no current window",
            "no flow tab",
            "no_flow_tab",
            "flow_workspace_project_reopen_failed",
        )
    ):
        return FlowAgentWindowError(
            "Edge không còn cửa sổ/tab Google Flow đúng project để tạo ảnh. Tool đã tự "
            "mở lại đúng project theo checkpoint "
            f"và thử lại; nếu lỗi vẫn còn, hãy kiểm tra cửa sổ Edge. Chi tiết: {detail}"
        )
    if any(
        marker in lowered
        for marker in (
            "captcha_failed",
            "captcha_timeout",
            "captcha failed",
            "captcha timeout",
        )
    ):
        return FlowAgentCaptchaError(
            "Flow không lấy được CAPTCHA kịp thời. Tool đã thử lại nhưng phiên/tab Flow "
            "vẫn chưa sẵn sàng; hãy mở hoặc tải lại Flow rồi thử tiếp. "
            f"Chi tiết: {detail}"
        )
    if any(
        marker in lowered
        for marker in ("rate limit", "too many requests", "api_429", "http 429")
    ):
        return FlowAgentRateLimitError(
            "Google Flow đang giới hạn tốc độ tạm thời. Ảnh đã tạo vẫn được giữ; "
            f"hãy chờ rồi tiếp tục. Chi tiết: {detail}"
        )
    if any(
        marker in lowered
        for marker in (
            "insufficient credit",
            "not enough credit",
            "credit limit",
            "credits exhausted",
            "remainingcredits: 0",
            "quota",
            "quota exceeded",
            "daily limit",
            "usage limit",
            "credit exhausted",
            "hết lượt",
            "上限",
            "all_image_models_limited",
            "required_image_model_limited",
        )
    ):
        return FlowAgentQuotaError(
            "Tài khoản Google Flow đã hết lượt/credit tạo ảnh. Các ảnh hoàn tất và "
            "checkpoint đã được giữ; hãy đổi tài khoản để tiếp tục ảnh còn thiếu. "
            f"Chi tiết: {detail}"
        )
    if isinstance(error, FlowAgentError):
        return error
    return FlowAgentError(detail)


def _flow_project_id_from_error(error: BaseException | str) -> str:
    """Extract a project id appended by the extension's durable error path."""
    detail = str(error).strip()
    match = _FLOW_PROJECT_ERROR_RE.search(detail)
    return str(match.group(1)).strip() if match else ""


def _flow_identity_from_failures(
    failures: object,
) -> tuple[str, str]:
    """Recover one unambiguous Flow identity from durable failure records."""

    if not isinstance(failures, dict):
        return "", ""
    project_ids: set[str] = set()
    client_ids: set[str] = set()
    for value in failures.values():
        if not isinstance(value, dict):
            continue
        project_id = str(value.get("flow_project_id") or "").strip()
        if not project_id:
            project_id = _flow_project_id_from_error(value.get("reason", ""))
        if project_id:
            project_ids.add(project_id)
        client_id = str(value.get("flow_client_id") or "").strip()
        if client_id:
            client_ids.add(client_id)
    return (
        next(iter(project_ids)) if len(project_ids) == 1 else "",
        next(iter(client_ids)) if len(client_ids) == 1 else "",
    )


def _legacy_flow_checkpoint_proves_all_prompts_unsent(
    checkpoint: object,
    prompts: list[str],
    workspace_id: str,
) -> bool:
    """Prove a legacy project-less run can safely start in a new Flow project.

    A missing project id alone is never enough. Require a complete checkpoint
    whose every prompt hash matches and whose records all explicitly say the
    prompt was not submitted and was not uncertain.
    """

    if not isinstance(checkpoint, dict) or not prompts or not workspace_id:
        return False
    if str(checkpoint.get("workspace_id") or "").strip() != workspace_id:
        return False
    try:
        if int(checkpoint.get("total", -1)) != len(prompts):
            return False
        if int(checkpoint.get("submitted", -1)) != 0:
            return False
        if int(checkpoint.get("completed", -1)) != 0:
            return False
    except (TypeError, ValueError):
        return False

    entries = checkpoint.get("entries")
    failures = checkpoint.get("failures")
    if not isinstance(entries, dict) or entries:
        return False
    journal = checkpoint.get("dispatch_journal")
    if isinstance(journal, dict) and journal.get("workspace_id") == workspace_id and journal.get("run_id") == checkpoint.get("run_id"):
        records = journal.get("prompts")
        if isinstance(records, dict) and len(records) == len(prompts):
            return all(isinstance(records.get(str(index)), dict)
                       and records[str(index)].get("prompt_sha256") == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                       and records[str(index)].get("status") in {"not-dispatched", "not-submitted"}
                       for index, prompt in enumerate(prompts, 1))
        return False
    if not isinstance(failures, dict) or len(failures) != len(prompts):
        return False

    for index, prompt in enumerate(prompts, 1):
        failure = failures.get(str(index))
        if not isinstance(failure, dict):
            return False
        if str(failure.get("prompt_sha256") or "") != hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest():
            return False
        if (
            failure.get("submitted") is not False
            or failure.get("submission_uncertain") is not False
            or str(failure.get("workspace_id") or "").strip() != workspace_id
        ):
            return False
        if str(failure.get("flow_project_id") or "").strip():
            return False
        if _flow_project_id_from_error(failure.get("reason", "")):
            return False
    return True


def _operator_flow_replacement_valid(checkpoint: dict, checkpoint_path: Path,
                                     prompts: tuple[str, ...], workspace_id: str) -> bool:
    """An explicit one-use replacement grant preserves, rather than rewrites, uncertainty."""
    grant = checkpoint.get("operator_project_replacement")
    if not isinstance(grant, dict) or grant.get("authorization") != "replace-missing-project-once":
        return False
    if (grant.get("consumed_at") or grant.get("workspace_id") != workspace_id
            or grant.get("run_id") != checkpoint.get("run_id")
            or grant.get("signature") != checkpoint.get("signature")
            or checkpoint.get("entries") or checkpoint.get("flow_project_id")):
        return False
    expected = [hashlib.sha256(prompt.encode("utf-8")).hexdigest() for prompt in prompts]
    if grant.get("prompt_sha256s") != expected:
        return False
    receipt = Path(str(grant.get("retired_checkpoint") or "")).resolve()
    journal = (checkpoint_path.parent / "repair-journal").resolve()
    try:
        if receipt.parent != journal or not receipt.is_file():
            return False
        return hashlib.sha256(receipt.read_bytes()).hexdigest() == grant.get("retired_sha256")
    except OSError:
        return False


def _uncertainty_is_safe_to_retry_after_no_match(
    failure: object,
    expected_project_id: str = "",
) -> bool:
    """Whether an authoritative empty project scan proves a retry is safe."""

    if (
        not isinstance(failure, dict)
        or failure.get("submitted") is not False
        or failure.get("submission_uncertain") is not True
    ):
        return False
    reason = str(failure.get("reason") or "")
    lowered = unicodedata.normalize("NFKC", reason).casefold()
    if _pre_submission_flow_phase(reason):
        return True
    if "flow_ui_timeout_unsent" in lowered:
        return True
    # MV3 can close the tabs.sendMessage response port while the content
    # driver is still unwinding. The extension now asks that driver for a
    # cancellation acknowledgement; on Resume, an authoritative project
    # scan with no exact prompt is the remaining proof that this old prompt
    # was not committed to Flow.
    if not (
        "flow_ui_submission_uncertain" in lowered
        and "phase=generate-command" in lowered
    ):
        return False
    expected_project_id = str(expected_project_id or "").strip()
    failure_project_id = str(failure.get("flow_project_id") or "").strip()
    if not failure_project_id:
        failure_project_id = _flow_project_id_from_error(reason)
    if (
        expected_project_id
        and failure_project_id
        and failure_project_id != expected_project_id
    ):
        return False
    if any(
        marker in lowered
        for marker in (
            "message channel closed",
            "message port closed",
            "listener indicated an asynchronous response",
        )
    ):
        return True

    # Older extension builds discarded both the transport error and the
    # cancellation acknowledgement, leaving only this generic phase marker.
    # Recover those checkpoints only when the failure itself is tied to the
    # exact project being scanned; the caller still requires a complete,
    # idle snapshot with no exact prompt match before it clears the failure.
    return bool(expected_project_id and failure_project_id == expected_project_id)


def _pre_submission_flow_phase(reason: object) -> str:
    match = _FLOW_PHASE_RE.search(str(reason or ""))
    if not match:
        return ""
    phase = match.group(1).casefold()
    return phase if phase in _PRE_SUBMISSION_FLOW_PHASES else ""


def _snapshot_is_authoritative_for_unsent(snapshot: object, expected_project_id: str) -> bool:
    if not isinstance(snapshot, dict):
        return False
    observed_project_id = str(snapshot.get("projectId") or "").strip()
    if expected_project_id and observed_project_id != expected_project_id:
        return False
    tiles = snapshot.get("tiles")
    reads = snapshot.get("reusedPromptReads")
    composer = snapshot.get("composer")
    history = snapshot.get("history")
    if not isinstance(tiles, list) or not isinstance(reads, list):
        return False
    try:
        tile_count = int(snapshot.get("tileCount"))
    except (TypeError, ValueError):
        return False
    if tile_count != len(tiles) or len(reads) < tile_count:
        return False
    if not isinstance(history, dict) or history.get("scanComplete") is not True:
        return False
    if tile_count == 0 and history.get("emptyStateVerified") is not True:
        return False
    if not isinstance(composer, dict) or composer.get("stopVisible") is not False:
        return False
    if composer.get("hasPromptEditor") is not True:
        return False
    for read in reads:
        if not isinstance(read, dict):
            return False
        # A tile can legitimately have no reusable prompt control (for
        # example an older Flow tile or a partially rendered virtualized row).
        # The read attempt itself is the completeness proof; requiring every
        # tile to return prompt text made a safe pre-submit recovery look like
        # an uncertain submission forever.
        if not str(read.get("reason") or "").strip():
            return False
    return True


def _flow_snapshot_has_active_generation(snapshot: object) -> bool:
    if not isinstance(snapshot, dict):
        return False
    composer = snapshot.get("composer")
    return isinstance(composer, dict) and composer.get("stopVisible") is True


def _completed_flow_project_owner(
    checkpoint_path: Path,
    workspace_id: str,
    flow_project_id: str,
) -> dict[str, str] | None:
    """Find a different completed workspace that already owns this Flow project.

    Flow projects are workspace-scoped in the desktop app.  A project ID
    copied from another workspace is a stale binding, not permission to keep
    sending prompts into that workspace's project.  Restrict the scan to the
    sibling workspace layout used by durable ``.docurandom/flow_history.json``
    checkpoints; arbitrary standalone checkpoints are never scanned.
    """

    current_workspace_id = str(workspace_id or "").strip()
    target_project_id = str(flow_project_id or "").strip()
    checkpoint = Path(checkpoint_path)
    if (
        not current_workspace_id
        or not target_project_id
        or checkpoint.name.casefold() != "flow_history.json"
        or checkpoint.parent.name.casefold() != ".docurandom"
    ):
        return None

    current_root = checkpoint.parent.parent.resolve(strict=False)
    try:
        siblings = tuple(current_root.parent.iterdir())
    except OSError:
        return None

    for sibling in siblings:
        try:
            if not sibling.is_dir() or sibling.is_symlink():
                continue
            if sibling.resolve(strict=False) == current_root:
                continue
            history_path = sibling / ".docurandom" / "flow_history.json"
            if not history_path.is_file():
                continue
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(history, dict):
            continue
        owner_workspace_id = str(history.get("workspace_id") or "").strip()
        if (
            not owner_workspace_id
            or owner_workspace_id == current_workspace_id
            or str(history.get("flow_project_id") or "").strip()
            != target_project_id
            or str(history.get("status") or "").casefold() != "complete"
        ):
            continue
        try:
            total = int(history.get("total", 0))
            completed = int(history.get("completed", 0))
            failed = int(history.get("failed", 0))
        except (TypeError, ValueError):
            continue
        entries = history.get("entries")
        if (
            total <= 0
            or completed != total
            or failed != 0
            or not isinstance(entries, dict)
            or len(entries) < total
        ):
            continue
        return {
            "workspace_id": owner_workspace_id,
            "workspace_name": sibling.name,
        }
    return None


def _wait_cancellable(seconds: float, cancel_event: threading.Event | None) -> None:
    duration = max(0.0, float(seconds))
    if cancel_event is None:
        time.sleep(duration)
    elif cancel_event.wait(duration):
        raise FlowAgentCancelled("Đã hủy tạo ảnh Flow.")


def _valid_image_file(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 32:
            return False
        with Image.open(path) as opened:
            opened.verify()
        with Image.open(path) as opened:
            return opened.width > 0 and opened.height > 0
    except (OSError, ValueError, UnidentifiedImageError):
        return False


def _load_generation_checkpoint(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_complete_flow_images(
    prompts: Iterable[str], output_dir: Path, *, checkpoint_path: Path | None = None,
    aspect: str = "landscape", workspace_id: str | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[FlowGeneratedImage, ...] | None:
    """Read a fully verified local batch without requiring a live Flow browser.

    Partial, changed or damaged batches still use normal generation/recovery.
    This function does not rewrite a checkpoint or submit anything to Flow.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise FlowAgentCancelled("Đã hủy phục hồi ảnh Flow.")
    clean = tuple(value.strip() for value in prompts if value.strip())
    if not clean:
        return None
    directory = output_dir.expanduser().resolve(strict=False)
    path = checkpoint_path or directory / ".docurandom-flow-checkpoint.json"
    saved = _load_generation_checkpoint(path.expanduser().resolve(strict=False))
    signature = hashlib.sha256(json.dumps(
        {"model": FLOW_REQUIRED_IMAGE_MODEL, "aspect": aspect, "prompts": clean},
        ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()
    if saved.get("signature") != signature or saved.get("status") != "complete":
        return None
    if workspace_id and str(saved.get("workspace_id") or "") != str(workspace_id):
        raise FlowAgentError("Checkpoint ảnh Flow không khớp workspace; giữ dữ liệu, không tạo ảnh mới.")
    if any(saved.get(field) != len(clean) for field in ("total", "completed", "submitted")):
        return None
    entries = saved.get("entries")
    if not isinstance(entries, dict):
        return None
    results = []
    for index, prompt in enumerate(clean, 1):
        entry = entries.get(str(index))
        if not isinstance(entry, dict) or entry.get("prompt_sha256") != hashlib.sha256(prompt.encode("utf-8")).hexdigest():
            return None
        filename = str(entry.get("filename") or "")
        candidate = (directory / filename).resolve(strict=False)
        if not filename or candidate.parent != directory or not _valid_image_file(candidate):
            return None
        results.append(FlowGeneratedImage(
            prompt_index=index, prompt=prompt, path=candidate,
            model=str(entry.get("model") or FLOW_REQUIRED_IMAGE_MODEL),
            media_id=str(entry.get("media_id") or ""),
        ))
    if cancel_event is not None and cancel_event.is_set():
        raise FlowAgentCancelled("Đã hủy phục hồi ảnh Flow.")
    return tuple(results)


def bind_flow_project_checkpoint(
    checkpoint_path: Path,
    workspace_id: str,
    flow_project_id: str,
    flow_client_id: str | None = None,
) -> Path:
    """Bind an existing Flow generation checkpoint to a user-confirmed project.

    This is a local metadata-only operation: it never contacts Flow or submits
    a prompt.  The original checkpoint is copied beside itself before the
    project identity is atomically updated.
    """

    path = Path(checkpoint_path).expanduser().resolve(strict=False)
    workspace = str(workspace_id or "").strip()[:200]
    project = str(flow_project_id or "").strip()[:200]
    client = str(flow_client_id or "").strip()[:200]
    if not workspace:
        raise FlowAgentError("Không thể ghép Flow khi thiếu mã workspace.")
    if not project:
        raise FlowAgentError("Tab Flow đang mở không có mã project hợp lệ.")
    if not path.is_file():
        raise FlowAgentError(
            "Không tìm thấy checkpoint Flow hiện có; không tạo checkpoint mới để tránh ghép nhầm."
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FlowAgentError("Không đọc được checkpoint Flow để ghép project.") from exc
    if not isinstance(value, dict):
        raise FlowAgentError("Checkpoint Flow không có cấu trúc hợp lệ.")
    if not isinstance(value.get("entries", {}), dict) or not isinstance(
        value.get("failures", {}), dict
    ):
        raise FlowAgentError("Checkpoint Flow không có cấu trúc hợp lệ.")

    saved_workspace = str(value.get("workspace_id") or "").strip()
    if saved_workspace and saved_workspace != workspace:
        raise FlowAgentError(
            "Checkpoint này đã thuộc workspace khác; tool không đổi liên kết."
        )
    saved_project = str(value.get("flow_project_id") or "").strip()
    if saved_project and saved_project != project:
        raise FlowAgentError(
            "Checkpoint đã gắn với project Flow khác; tool không thay project cũ."
        )

    updated = dict(value)
    updated["workspace_id"] = workspace
    updated["flow_project_id"] = project
    if client:
        updated["flow_client_id"] = client
    backup = path.with_name(
        f"{path.name}.pre-bind-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    )
    try:
        shutil.copy2(path, backup)
        atomic_write_json(path, updated)
    except OSError as exc:
        raise FlowAgentError(f"Không lưu được liên kết Flow: {exc}") from exc
    return backup


class FlowAgentManager:
    def __init__(self, api_base: str = DEFAULT_API_BASE) -> None:
        self.api_base = api_base.rstrip("/")
        self.process: subprocess.Popen[bytes] | None = None
        self._environment_cache: tuple[float, Path | None, Path | None, bool] | None = None
        self._extension_reload_attempted = False

    def check(
        self,
        *,
        include_credits: bool = True,
        refresh_environment: bool = True,
        passive: bool = False,
    ) -> FlowAgentStatus:
        cached = self._environment_cache
        cache_fresh = cached is not None and time.monotonic() - cached[0] < 30.0
        if not refresh_environment and cache_fresh:
            _, edge, extension_dir, installed = cached
        else:
            edge = discover_edge()
            extension_dir = discover_extension_dir()
            installed = detect_edge_extension_installed()
            self._environment_cache = (time.monotonic(), edge, extension_dir, installed)
        try:
            health = (
                _passive_health_request(self.api_base, timeout=2.5)
                if passive
                else _json_request(f"{self.api_base}/health", timeout=2.5)
            )
        except FlowAgentError as exc:
            backend_running = self._backend_endpoint_open()
            if backend_running:
                # A listening socket is authoritative for process liveness;
                # /health may be temporarily delayed while the bridge is
                # handling browser callbacks or refreshing its OS window
                # probe.  Do not label this as a dead backend and make every
                # workspace try to start another one.
                return FlowAgentStatus(
                    edge_path=edge,
                    extension_dir=extension_dir,
                    extension_installed=installed,
                    backend_running=True,
                    extension_connected=False,
                    has_flow_key=False,
                    detail=(
                        "Flow backend đã mở nhưng health-check đang chậm; "
                        "tool sẽ chờ extension kết nối mà không tạo backend mới. "
                        f"Chi tiết: {exc}"
                    ),
                )
            return FlowAgentStatus(
                edge_path=edge,
                extension_dir=extension_dir,
                extension_installed=installed,
                backend_running=False,
                extension_connected=False,
                has_flow_key=False,
                detail=f"Flow backend chưa chạy: {exc}",
            )
        connected = bool(health.get("extension_connected"))
        has_key = bool(health.get("has_flow_key"))
        browser_open = health.get("browser_open")
        raw_flow_tab_count = health.get("flow_tab_count")
        raw_capabilities = health.get("capabilities")
        clients = health.get("clients")
        connected_client_count = 0
        ui_client_count = 0
        flow_client_count_with_tabs = 0
        if isinstance(clients, list):
            connected_client_count = sum(isinstance(client, dict) for client in clients)
            ui_client_count = sum(
                isinstance(client, dict)
                and "ui_text_to_image" in {
                    str(value).strip()
                    for value in (
                        client.get("capabilities")
                        if isinstance(client.get("capabilities"), list)
                        else []
                    )
                }
                and not bool(client.get("ui_quota_blocked", False))
                for client in clients
            )
            for client in clients:
                if not isinstance(client, dict):
                    continue
                try:
                    client_flow_tabs = max(0, int(client.get("flow_tab_count") or 0))
                except (TypeError, ValueError):
                    client_flow_tabs = 0
                if client_flow_tabs > 0:
                    flow_client_count_with_tabs += 1
            if browser_open is None:
                browser_open = any(
                    bool(client.get("browser_open", True))
                    for client in clients
                    if isinstance(client, dict)
                )
            if raw_flow_tab_count is None:
                raw_flow_tab_count = sum(
                    max(0, int(client.get("flow_tab_count") or 0))
                    for client in clients
                    if isinstance(client, dict)
                )
        try:
            flow_tab_count = max(0, int(raw_flow_tab_count or 0))
        except (TypeError, ValueError):
            flow_tab_count = 0
        if not isinstance(clients, list) and flow_tab_count > 0:
            # Older/single-client health responses do not expose the client
            # breakdown. Report one observed source rather than implying that
            # the aggregate count belongs to each workspace.
            flow_client_count_with_tabs = 1
        capabilities = {
            str(value).strip()
            for value in (raw_capabilities if isinstance(raw_capabilities, list) else [])
            if str(value).strip()
        }
        ui_image_capable = "ui_text_to_image" in capabilities
        if ui_image_capable and ui_client_count <= 0 and connected:
            # Compatibility with an older backend that reports only aggregate
            # capabilities and not per-profile capability arrays.
            ui_client_count = 1
        if connected and connected_client_count <= 0:
            connected_client_count = 1
        extension_version = str(health.get("extension_version") or "").strip()
        # Older resident service workers include the field but leave it blank.
        # When they still expose a valid Flow key, that is a usable legacy API
        # session and must not be mistaken for "waiting for Google Flow".  A
        # non-empty, different version remains a hard mismatch because its
        # UI command contract may be incompatible with this build.
        extension_compatible = bool(
            not extension_version or extension_version == FLOW_EXTENSION_VERSION
        )
        credits: int | None = None
        if include_credits and connected and has_key:
            try:
                credit_data = _json_request(f"{self.api_base}/v1/credits", timeout=8.0)
                raw_credit = credit_data.get("total_credits", credit_data.get("credits"))
                credits = int(raw_credit) if raw_credit is not None else None
            except (FlowAgentError, TypeError, ValueError):
                pass
        usable_session = bool(
            (
                ui_image_capable
                and browser_open is not False
                and flow_tab_count > 0
            )
            or (has_key and not ui_image_capable)
        )
        if connected and usable_session and not extension_compatible:
            detail = (
                "Tiện ích Flow trong Edge vẫn là bản cũ "
                f"({extension_version or 'chưa báo phiên bản'}; cần {FLOW_EXTENSION_VERSION}). "
                "Hãy đóng toàn bộ Edge rồi mở lại một lần; prompt/checkpoint vẫn được giữ."
            )
        elif connected and usable_session:
            if ui_client_count > 1:
                detail = (
                    f"Flow Agent sẵn sàng qua {ui_client_count} profile; "
                    "nếu một tài khoản hết lượt ảnh, tool tự chuyển profile còn lượt."
                )
            else:
                detail = (
                    "Flow Agent sẵn sàng qua giao diện Flow."
                    if ui_image_capable and not has_key
                    else "Flow Agent sẵn sàng."
                )
        elif browser_open is False:
            detail = (
                "Microsoft Edge đã đóng; phiên Flow đã tự ngắt. "
                "Mở lại Edge/Google Flow để tool tự kết nối lại."
            )
        elif connected and flow_tab_count <= 0:
            detail = (
                "Extension đã kết nối nhưng chưa có tab Google Flow. Tool chỉ mở tối đa "
                "một tab và sẽ không tự mở lặp lại."
            )
        elif connected:
            detail = (
                "Tab Flow đang mở nhưng phiên chưa sẵn sàng. Hãy hoàn tất đăng nhập/"
                "xác minh ngay trên tab hiện có; tool không tạo thêm tab."
            )
        else:
            detail = "Backend đã chạy; cần mở Edge/Flow để extension kết nối."
        return FlowAgentStatus(
            edge_path=edge,
            extension_dir=extension_dir,
            extension_installed=installed or connected,
            backend_running=True,
            extension_connected=connected,
            has_flow_key=has_key,
            credits=credits,
            detail=detail,
            extension_version=extension_version,
            extension_compatible=extension_compatible,
            browser_open=(bool(browser_open) if browser_open is not None else None),
            flow_tab_count=flow_tab_count,
            ui_image_capable=ui_image_capable,
            connected_client_count=connected_client_count,
            ui_client_count=ui_client_count,
            flow_client_count_with_tabs=flow_client_count_with_tabs,
        )

    def install_edge(self, timeout: float = 900.0) -> Path:
        existing = discover_edge()
        if existing is not None:
            return existing
        winget = shutil.which("winget")
        if not winget:
            raise FlowAgentError("Không tìm thấy trình cài đặt Windows (winget).")
        completed = subprocess.run(
            [
                winget,
                "install",
                "--exact",
                "--id",
                "Microsoft.Edge",
                "--source",
                "winget",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--silent",
                "--disable-interactivity",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=_creation_flags(),
        )
        edge = discover_edge()
        if completed.returncode != 0 or edge is None:
            raise FlowAgentError(completed.stdout.strip() or "Không thể tự cài Microsoft Edge.")
        return edge

    def open_extension_setup(self, *, force: bool = False) -> Path:
        edge = discover_edge()
        extension_dir = discover_extension_dir()
        if edge is None:
            raise FlowAgentError("Microsoft Edge chưa được cài.")
        if extension_dir is None:
            raise FlowAgentError("Không tìm thấy gói Flow Agent Extension đi kèm tool.")
        if not _claim_edge_launch(force=force):
            return extension_dir
        if os.name == "nt":
            subprocess.Popen(["explorer.exe", str(extension_dir)])
        # One Edge invocation opens one window with two tabs. Two separate
        # launches can be promoted to two foreground windows by Windows,
        # stealing focus and creating a second browser lifecycle during setup.
        subprocess.Popen(
            [str(edge), *_edge_user_data_arguments(), "edge://extensions/", FLOW_WEB_URL],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creation_flags(),
        )
        return extension_dir

    def open_flow(self, *, force: bool = False) -> None:
        edge = discover_edge()
        if edge is None:
            raise FlowAgentError("Microsoft Edge chưa được cài.")

        # Routine Flow work must stay inside the already-running Edge profile.
        # Ask the resident extension to reuse its Flow tab or create a
        # background tab in an existing window before considering native Edge
        # launch. Native launch is kept only as a one-time fallback when the
        # local backend itself is unavailable.
        # All workspaces share one resident Flow client.  The first caller may
        # arrive while Edge/extension is still reconnecting after startup; it
        # waits for that one client instead of failing immediately.  Callers
        # behind it then observe the already prepared tab and return without
        # opening another Edge window.
        with _FLOW_OPEN_LOCK:
            last_error = ""
            try:
                response = _json_request(
                    f"{self.api_base}/v1/prepare-flow-tab",
                    method="POST",
                    timeout=20.0,
                )
                payload = response.get("result") if isinstance(response, dict) else None
                tab_id = (
                    payload.get("tabId")
                    if isinstance(payload, dict)
                    else response.get("tabId")
                )
                if response.get("status", 200) == 200 and tab_id:
                    return
                detail = response.get("error") if isinstance(response, dict) else None
                last_error = str(detail or "Flow Agent chưa trả tab Flow.")
            except FlowAgentError as exc:
                last_error = str(exc)

            if self._backend_endpoint_open():
                deadline = time.monotonic() + _FLOW_OPEN_WAIT_SECONDS
                last_health_error = last_error
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    try:
                        health = _json_request(
                            f"{self.api_base}/health",
                            timeout=min(3.0, max(0.5, remaining)),
                        )
                        if bool(health.get("extension_connected")):
                            try:
                                flow_tabs = int(health.get("flow_tab_count") or 0)
                            except (TypeError, ValueError):
                                flow_tabs = 0
                            if flow_tabs > 0:
                                return
                            break
                    except FlowAgentError as exc:
                        last_health_error = str(exc)
                    time.sleep(min(_FLOW_OPEN_POLL_SECONDS, max(0.05, remaining)))

                # The extension may have connected after the first prepare
                # request timed out.  A second prepare is safe: it reuses the
                # resident Flow tab and never submits a prompt.
                try:
                    response = _json_request(
                        f"{self.api_base}/v1/prepare-flow-tab",
                        method="POST",
                        timeout=20.0,
                    )
                    payload = response.get("result") if isinstance(response, dict) else None
                    tab_id = (
                        payload.get("tabId")
                        if isinstance(payload, dict)
                        else response.get("tabId")
                    )
                    if response.get("status", 200) == 200 and tab_id:
                        return
                    detail = response.get("error") if isinstance(response, dict) else None
                    last_error = str(detail or last_health_error)
                except FlowAgentError as exc:
                    last_error = str(exc)

                raise FlowAgentWindowError(
                    "Flow backend đang chạy nhưng extension chưa chuẩn bị được tab Flow; "
                    "giữ nguyên Edge hiện tại và checkpoint. Tool không tạo backend/tab trùng. "
                    f"Chi tiết: {last_error}"
                )

        if not _claim_edge_launch(force=force):
            return
        # This is only the backend-unavailable fallback. Start Edge minimized
        # so a recovery attempt cannot steal the user's foreground window;
        # normal runs go through the resident extension's dedicated minimized
        # window above.
        subprocess.Popen(
            [str(edge), *_edge_user_data_arguments(), "--start-minimized", FLOW_WEB_URL],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creation_flags(),
        )

    def update_installed_extension(self, *, force: bool = True) -> bool:
        """Reload the exact bundled unpacked extension when Edge already uses it.

        Edge does not expose a supported API for silently changing an unpacked
        extension's source directory. Therefore this method only reloads an
        installation whose recorded path is exactly the current app bundle.
        A missing or different path is reported as False so the UI can open
        the normal one-time Load unpacked flow instead of editing preferences.
        """

        extension_dir = discover_extension_dir()
        if extension_dir is None:
            raise FlowAgentError("Không tìm thấy gói Flow Agent Extension đi kèm tool.")
        try:
            manifest = json.loads(
                (extension_dir / "manifest.json").read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise FlowAgentError(
                "Gói Flow Agent đi kèm tool không đọc được manifest."
            ) from exc
        if (
            manifest.get("manifest_version") != 3
            or manifest.get("name") != "Flow Agent"
            or str(manifest.get("version") or "").strip() != FLOW_EXTENSION_VERSION
        ):
            raise FlowAgentError(
                "Gói Flow Agent không đồng bộ với phiên bản tool "
                f"(cần {FLOW_EXTENSION_VERSION})."
            )

        installations = tuple(
            installation
            for installation in edge_flow_extension_installations()
            if not installation.disabled
        )
        if not installations:
            return False
        bundled_key = os.path.normcase(
            str(extension_dir.expanduser().resolve(strict=False))
        )
        if any(
            installation.path is None
            or os.path.normcase(
                str(installation.path.expanduser().resolve(strict=False))
            ) != bundled_key
            or installation.version != FLOW_EXTENSION_VERSION
            for installation in installations
        ):
            return False
        return self.reload_installed_extension(force=force)

    def reload_installed_extension(self, *, force: bool = False) -> bool:
        """Reload the unpacked MV3 worker in-place without restarting Edge."""
        if self._extension_reload_attempted and not force:
            return False
        extension_ids = edge_flow_extension_ids()
        if not extension_ids:
            return False
        self._extension_reload_attempted = True

        # The old implementation opened popup.html in Edge. On Windows that
        # can activate a new tab and move the user's pointer. A connected
        # extension can reload its own MV3 worker directly, without touching
        # any browser tab.
        if self._backend_endpoint_open():
            try:
                response = _json_request(
                    f"{self.api_base}/v1/reload-extension",
                    method="POST",
                    timeout=10.0,
                )
            except FlowAgentError:
                return False
            payload = response.get("result") if isinstance(response, dict) else None
            return bool(
                response.get("status", 200) == 200
                and isinstance(payload, dict)
                and payload.get("reloaded") is True
            )

        # Backend unavailable: retain the legacy one-time recovery fallback.
        # This path is not used during normal image generation, because the
        # desktop starts the backend before asking the extension to reload.
        edge = discover_edge()
        if edge is None:
            return False
        for extension_id in extension_ids:
            reload_url = (
                f"chrome-extension://{extension_id}/popup.html"
                f"?docurandom_reload={urllib.parse.quote(FLOW_EXTENSION_VERSION)}"
            )
            subprocess.Popen(
                [str(edge), *_edge_user_data_arguments(), reload_url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_creation_flags(),
            )
        return True

    def restart_extension_session(self, *, timeout: float = 45.0) -> FlowAgentStatus:
        """Reload Flow Agent and recover one managed Flow tab without reloading it."""

        self.start_backend()
        if not self.reload_installed_extension(force=True):
            raise FlowAgentError(
                "Không tìm thấy Flow Agent đã cài trong Edge để khởi động lại."
            )

        # The temporary extension page reloads the MV3 worker in-place and
        # closes itself. Wait for the new worker to reconnect before asking it
        # to re-inject the UI driver into its remembered tab. The Flow document
        # itself is deliberately never reloaded.
        deadline = time.monotonic() + max(15.0, float(timeout))
        connected_status: FlowAgentStatus | None = None
        # Give chrome.runtime.reload() enough time to disconnect the previous
        # worker before accepting a matching-version worker as the restarted one.
        time.sleep(3.0)
        while time.monotonic() < deadline:
            connected_status = self.check(
                include_credits=False,
                refresh_environment=False,
            )
            if (
                connected_status.extension_connected
                and connected_status.extension_compatible
            ):
                break
            time.sleep(0.5)
        else:
            detail = connected_status.detail if connected_status is not None else ""
            raise FlowAgentError(
                "Flow Agent chưa kết nối lại sau khi reload. " + detail
            )

        remaining = max(8.0, deadline - time.monotonic())
        refresh_result = _json_request(
            f"{self.api_base}/v1/refresh-tokens?force=true&reload_page=false",
            method="POST",
            timeout=min(remaining, 30.0),
        )
        try:
            nudged = int(refresh_result.get("nudged") or 0)
        except (TypeError, ValueError):
            nudged = 0
        clients = refresh_result.get("clients")
        failed_clients = (
            [item for item in clients if isinstance(item, dict) and not item.get("ok")]
            if isinstance(clients, list)
            else []
        )
        if nudged <= 0 or failed_clients:
            detail = (
                str(failed_clients[0].get("error") or "").strip()
                if failed_clients
                else "extension chưa nhận yêu cầu refresh"
            )
            raise FlowAgentError(f"Không phục hồi được driver Google Flow: {detail}.")
        while time.monotonic() < deadline:
            status = self.check(
                include_credits=False,
                refresh_environment=False,
            )
            if status.ready and status.extension_compatible:
                return status
            connected_status = status
            time.sleep(0.5)
        detail = connected_status.detail if connected_status is not None else ""
        raise FlowAgentError(
            "Flow Agent đã reload driver nhưng phiên Google Flow chưa sẵn sàng. " + detail
        )

    def start_backend(self) -> None:
        # The bootstrap thread and workspace precheck can enter this method at
        # the same time.  A transient /health timeout must not make the second
        # caller spawn another uvicorn process on the same port; that duplicate
        # immediately exits with "address already in use" and masks the
        # healthy backend that was already recovering.
        with _FLOW_BACKEND_START_LOCK:
            deadline = time.monotonic() + _FLOW_BACKEND_START_TIMEOUT_SECONDS
            while True:
                if self.check().backend_running:
                    return

                process = self.process
                if process is not None and process.poll() is None:
                    # The backend is a single process shared by every
                    # workspace.  A slow /health response must not be
                    # interpreted as a missing backend when its listening
                    # socket is already open.  Returning here lets the
                    # caller perform the normal readiness check without
                    # making another workspace wait 25 seconds and report a
                    # misleading "không tạo tiến trình trùng" error.
                    if self._backend_endpoint_open():
                        return
                    if time.monotonic() >= deadline:
                        raise FlowAgentError(
                            "Flow backend process đang chạy nhưng chưa mở endpoint "
                            "trong thời gian chờ; không tạo thêm tiến trình trùng."
                        )
                    time.sleep(0.25)
                    continue

                if process is not None:
                    self.process = None

                # A manager instance can lose its process handle after a
                # restart, while the shared local backend is still listening.
                # Probe the socket before spawning so a slow /health response
                # cannot create a competing server.
                if self._backend_endpoint_open():
                    if time.monotonic() >= deadline:
                        raise FlowAgentError(
                            "Flow backend đang giữ cổng nhưng chưa phản hồi health."
                        )
                    time.sleep(0.25)
                    continue
                break

            if getattr(sys, "frozen", False):
                command = [sys.executable, "--flow-backend"]
                cwd = Path(sys.executable).resolve().parent
            else:
                app_path = Path(__file__).resolve().parents[1] / "app.py"
                command = [sys.executable, str(app_path), "--flow-backend"]
                cwd = app_path.parent
            self.process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_creation_flags(),
            )
            spawned = self.process
            while time.monotonic() < deadline:
                if self.check().backend_running:
                    return
                if spawned.poll() is not None:
                    # The child may have lost a port race with a backend that
                    # became healthy between probes. Wait for that authoritative
                    # process before reporting a startup failure.
                    self.process = None
                    if self._backend_endpoint_open():
                        time.sleep(0.25)
                        continue
                    raise FlowAgentError("Flow backend dừng ngay khi khởi động.")
                time.sleep(0.25)
            raise FlowAgentError("Flow backend khởi động quá thời gian chờ.")

    def _backend_endpoint_open(self) -> bool:
        try:
            parsed = urllib.parse.urlparse(self.api_base)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=0.35):
                return True
        except (OSError, ValueError):
            return False

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=4)

    @staticmethod
    def _download(url: str, destination: Path, timeout: float = 120.0) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response, temporary.open("wb") as handle:
                content_type = response.headers.get_content_type()
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
            extension = mimetypes.guess_extension(content_type, strict=False) or destination.suffix or ".png"
            final = destination.with_suffix(extension)
            os.replace(temporary, final)
            return final
        finally:
            temporary.unlink(missing_ok=True)

    def inspect_project_snapshot(
        self,
        prompts: Iterable[str] = (),
        *,
        client_id: str | None = None,
        workspace_id: str | None = None,
        flow_project_id: str | None = None,
        inspect_tile_prompts: bool = False,
        active_tab_only: bool = False,
        timeout: float = 300.0,
    ) -> dict[str, object]:
        """Read visible Flow project output tiles without generating anything."""
        clean_prompts = tuple(
            str(value).strip()
            for value in prompts
            if str(value).strip()
        )[:8]
        headers = {"X-Client-Id": client_id} if client_id else {}
        response = _json_request(
            f"{self.api_base}/v1/ui/project-snapshot",
            method="POST",
            payload={
                "prompts": list(clean_prompts),
                "workspaceId": str(workspace_id or "").strip(),
                "flowProjectId": str(flow_project_id or "").strip(),
                "inspectTilePrompts": bool(inspect_tile_prompts),
                "activeTabOnly": bool(active_tab_only),
            },
            timeout=timeout,
            headers=headers,
        )
        result = response.get("result") if isinstance(response.get("result"), dict) else response
        if not isinstance(result, dict):
            raise FlowAgentError("Flow Agent trả về project snapshot không hợp lệ.")
        return result

    def _reconcile_legacy_flow_project(
        self,
        prompts: tuple[str, ...],
        entries: dict[str, object],
        failures: dict[str, object],
        *,
        client_id: str | None,
    ) -> tuple[str, str]:
        """Recover a legacy project only from exact visible-history evidence."""

        known_prompts: list[str] = []
        for index, prompt in enumerate(prompts, 1):
            entry = entries.get(str(index))
            failure = failures.get(str(index))
            acknowledged = isinstance(entry, dict) or (
                isinstance(failure, dict)
                and (
                    failure.get("submitted") is True
                    or failure.get("submission_uncertain") is True
                )
            )
            if acknowledged and prompt not in known_prompts:
                known_prompts.append(prompt)
            if len(known_prompts) >= 8:
                break
        if not known_prompts:
            return "", ""

        try:
            snapshot = self.inspect_project_snapshot(
                known_prompts,
                client_id=client_id or None,
                workspace_id=None,
                flow_project_id=None,
                inspect_tile_prompts=True,
                timeout=180.0,
            )
        except Exception:
            return "", ""
        project_id = str(snapshot.get("projectId") or "").strip()
        if not project_id:
            project_url = str(snapshot.get("projectUrl") or "").strip()
            match = re.search(
                r"/(?:projects?)/([^/?#]+)",
                project_url,
                re.IGNORECASE,
            )
            if match:
                project_id = urllib.parse.unquote(match.group(1)).strip()
        if not project_id:
            return "", ""
        raw_matches = snapshot.get("matches")
        matches = (
            raw_matches
            if isinstance(raw_matches, list)
            else []
        )
        exact_match = any(
            isinstance(item, dict)
            and bool(item.get("matched"))
            and str(item.get("promptIndex", "")).isdigit()
            and 1 <= int(item["promptIndex"]) <= len(known_prompts)
            for item in matches
        )
        if not exact_match:
            return "", ""
        observed_client_id = str(
            snapshot.get("clientId")
            or snapshot.get("_client_id")
            or ""
        ).strip()
        return project_id, observed_client_id

    def generate_images(
        self,
        prompts: Iterable[str],
        output_dir: Path,
        *,
        model: str = FLOW_REQUIRED_IMAGE_MODEL,
        aspect: str = "landscape",
        checkpoint_path: Path | None = None,
        inter_prompt_delay: tuple[float, float] = (3.0, 5.0),
        client_id: str | None = None,
        retry_failed: bool = False,
        cancel_event: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
        workspace_id: str | None = None,
        flow_project_id: str | None = None,
        flow_client_id: str | None = None,
    ) -> tuple[FlowGeneratedImage, ...]:
        clean_prompts = tuple(value.strip() for value in prompts if value.strip())
        if not clean_prompts:
            raise FlowAgentError("Không có prompt ảnh để gửi sang Flow.")
        # Flow image generation is deliberately fail-closed.  Old workspace
        # settings, callers and restored checkpoints may still request Pro or
        # multiple variations; the desktop boundary always migrates them to
        # Nano Banana 2 Lite and exactly one image per prompt.
        model = FLOW_REQUIRED_IMAGE_MODEL
        complete = load_complete_flow_images(
            clean_prompts, output_dir, checkpoint_path=checkpoint_path,
            aspect=aspect, workspace_id=workspace_id, cancel_event=cancel_event,
        )
        if complete is not None:
            if on_progress:
                on_progress(1.0, f"Đã xác minh đủ {len(complete)} ảnh từ checkpoint; không gửi prompt Flow.")
            return complete
        status = self.check()
        if not status.ready:
            raise FlowAgentError(status.detail or "Flow Agent/extension chưa sẵn sàng.")
        if (
            status.credits is not None
            and status.credits <= 0
            and not status.ui_image_capable
        ):
            raise FlowAgentQuotaError(
                "Tài khoản Google Flow hiện còn 0 lượt/credit tạo ảnh. "
                "Hãy đổi tài khoản; các ảnh và checkpoint hiện có vẫn được giữ."
            )
        sizes = {
            "landscape": "1792x1024",
            "portrait": "1024x1792",
            "square": "1024x1024",
        }
        output_dir = output_dir.expanduser().resolve(strict=False)
        output_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = (
            checkpoint_path.expanduser().resolve(strict=False)
            if checkpoint_path is not None
            else output_dir / ".docurandom-flow-checkpoint.json"
        )
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        signature = hashlib.sha256(
            json.dumps(
                {
                    "model": model,
                    "aspect": aspect,
                    "prompts": clean_prompts,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        checkpoint = _load_generation_checkpoint(checkpoint_path)
        new_checkpoint = str(checkpoint.get("signature", "")) != signature
        if new_checkpoint:
            checkpoint = {
                "version": 2,
                "signature": signature,
                "run_id": uuid.uuid4().hex,
                "model": model,
                "aspect": aspect,
                "total": len(clean_prompts),
                "prompts": list(clean_prompts),
                "entries": {},
                "failures": {},
                "submitted": 0,
                "completed": 0,
            }
            atomic_write_json(checkpoint_path, checkpoint)
        checkpoint["version"] = 2
        checkpoint["model"] = model
        checkpoint["aspect"] = aspect
        checkpoint["total"] = len(clean_prompts)
        checkpoint["prompts"] = list(clean_prompts)
        run_id = str(checkpoint.get("run_id", "")).strip() or uuid.uuid4().hex
        checkpoint["run_id"] = run_id
        raw_entries = checkpoint.get("entries")
        entries: dict[str, object] = raw_entries if isinstance(raw_entries, dict) else {}
        checkpoint["entries"] = entries
        raw_failures = checkpoint.get("failures")
        failures: dict[str, object] = (
            raw_failures if isinstance(raw_failures, dict) else {}
        )
        checkpoint["failures"] = failures
        workspace_id = str(workspace_id or checkpoint.get("workspace_id") or "").strip()
        flow_project_id = str(
            flow_project_id or checkpoint.get("flow_project_id") or ""
        ).strip()
        flow_client_id = str(
            flow_client_id or checkpoint.get("flow_client_id") or ""
        ).strip()
        if workspace_id:
            checkpoint["workspace_id"] = workspace_id
        if new_checkpoint and workspace_id:
            checkpoint["dispatch_journal"] = {"version": 1, "workspace_id": workspace_id, "run_id": run_id,
                "prompts": {str(index): {"prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "status": "not-dispatched"} for index, prompt in enumerate(clean_prompts, 1)}}
            atomic_write_json(checkpoint_path, checkpoint)
        if flow_project_id:
            checkpoint["flow_project_id"] = flow_project_id
        if flow_client_id:
            checkpoint["flow_client_id"] = flow_client_id

        # Preserve an identity already returned by the Flow bridge even when
        # the desktop request was interrupted before the normal response
        # could update the top-level checkpoint. Only one consistent identity
        # is accepted; conflicting records remain fail-closed.
        if not flow_project_id or not flow_client_id:
            failure_project_id, failure_client_id = _flow_identity_from_failures(
                failures
            )
            if not flow_project_id and failure_project_id:
                flow_project_id = failure_project_id
                checkpoint["flow_project_id"] = flow_project_id
            if not flow_client_id and failure_client_id:
                flow_client_id = failure_client_id
                checkpoint["flow_client_id"] = flow_client_id
        foreign_project_owner = (
            _completed_flow_project_owner(
                checkpoint_path,
                workspace_id,
                flow_project_id,
            )
            if workspace_id and flow_project_id and not entries
            else None
        )
        atomic_write_json(checkpoint_path, checkpoint)
        try:
            dispatch_round = max(0, int(checkpoint.get("dispatch_round", 0))) + 1
        except (TypeError, ValueError):
            dispatch_round = 1
        checkpoint["dispatch_round"] = dispatch_round
        checkpoint["status"] = "running"
        checkpoint["completed"] = 0
        checkpoint["failed"] = len(failures)
        checkpoint["incomplete_indices"] = list(range(1, len(clean_prompts) + 1))
        # Every Resume gets a fresh dispatch generation. The local Flow server
        # may cache an error by Idempotency-Key; reusing the old key would make
        # Resume replay that failure without ever touching the browser again.
        atomic_write_json(checkpoint_path, checkpoint)
        results_by_index: dict[int, FlowGeneratedImage] = {}
        pending_indices: set[int] = set()
        submitted_without_image_indices: set[int] = set()
        recoverable_failures: list[tuple[int, str, dict[str, object]]] = []
        total = len(clean_prompts)

        def record_failure(
            index: int,
            prompt: str,
            reason: str,
            *,
            submitted: bool = False,
            submission_uncertain: bool = False,
        ) -> None:
            failures[str(index)] = {
                "prompt": prompt,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "reason": reason,
                "submitted": submitted,
                "submission_uncertain": submission_uncertain,
                "workspace_id": workspace_id or None,
                "flow_project_id": flow_project_id or None,
                "flow_client_id": flow_client_id or None,
            }
            checkpoint["failed"] = len(failures)
            checkpoint["status"] = "incomplete"
            journal = checkpoint.get("dispatch_journal", {}).get("prompts", {})
            if str(index) in journal:
                journal[str(index)]["status"] = ("submitted" if submitted else "not-submitted"
                    if not submission_uncertain and (_pre_submission_flow_phase(reason) or "FLOW_UI_TIMEOUT_UNSENT" in reason)
                    else "uncertain")
            atomic_write_json(checkpoint_path, checkpoint)
            if submitted:
                submitted_without_image_indices.add(index)
                pending_indices.discard(index)
            else:
                pending_indices.add(index)
            if on_progress:
                on_progress(
                    index / total,
                    (
                        f"Prompt {index}/{total} đã gửi; Flow không trả ảnh: {reason}"
                        if submitted
                        else f"Prompt {index}/{total} cần kiểm tra: {reason}"
                    ),
                )

        for index, prompt in enumerate(clean_prompts, 1):
            entry = entries.get(str(index))
            prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            if isinstance(entry, dict):
                filename = Path(str(entry.get("filename", ""))).name
                candidate = output_dir / filename if filename else Path()
                if (
                    str(entry.get("prompt_sha256", "")) == prompt_sha256
                    and candidate.parent == output_dir
                    and _valid_image_file(candidate)
                ):
                    results_by_index[index] = FlowGeneratedImage(
                        prompt_index=index,
                        prompt=prompt,
                        path=candidate,
                        model=str(entry.get("model", model)),
                        media_id=str(entry.get("media_id", "")),
                    )
                    failures.pop(str(index), None)
                    continue
            failure = failures.get(str(index))
            if isinstance(failure, dict):
                if str(failure.get("prompt_sha256", "")) != prompt_sha256:
                    failures.pop(str(index), None)
                elif failure.get("submission_uncertain") is True:
                    reason = str(failure.get("reason") or "")
                    # Version 1.3.23 could persist an unrecoverable uncertainty
                    # even though the driver had emitted no lifecycle event at
                    # all. Every path to prompt insertion/submission emits an
                    # earlier project/configure event, so no-progress proves the
                    # prompt was never reached. Clear only that exact legacy
                    # marker; every uncertainty after a real UI phase remains
                    # fail-closed against duplicate image generation.
                    if "PHASE=NO-PROGRESS" in reason.upper():
                        failures.pop(str(index), None)
                        atomic_write_json(checkpoint_path, checkpoint)
                    else:
                        recoverable_failures.append((index, prompt, failure))
                elif failure.get("submitted") is True:
                    submitted_without_image_indices.add(index)
                    recoverable_failures.append((index, prompt, failure))
                elif not retry_failed:
                    pending_indices.add(index)

        # A pre-1.3.30 workspace may have durable images but no project id.
        # Rebind it only when a read-only visible-history scan finds an exact
        # prompt match; otherwise keep the existing fail-closed guard below.
        if (
            workspace_id
            and not flow_project_id
            and (entries or failures)
            and len(results_by_index) < total
            and (
                bool(results_by_index)
                or any(
                    isinstance(failure, dict)
                    and (
                        failure.get("submitted") is True
                        or failure.get("submission_uncertain") is True
                    )
                    for failure in failures.values()
                )
            )
        ):
            reconciled_project_id, reconciled_client_id = (
                self._reconcile_legacy_flow_project(
                    clean_prompts,
                    entries,
                    failures,
                    client_id=client_id or flow_client_id or None,
                )
            )
            if reconciled_project_id:
                flow_project_id = reconciled_project_id
                checkpoint["flow_project_id"] = flow_project_id
                if reconciled_client_id and not flow_client_id:
                    flow_client_id = reconciled_client_id
                    checkpoint["flow_client_id"] = flow_client_id
                atomic_write_json(checkpoint_path, checkpoint)

        # A legacy run without a project id can create a new project only when
        # its complete checkpoint proves every exact prompt was unsent and no
        # image/result exists. Never infer a project from the selected tab.
        authorized_replacement = bool(
            workspace_id and not flow_project_id and not entries and not results_by_index
            and _operator_flow_replacement_valid(checkpoint, checkpoint_path, clean_prompts, str(workspace_id))
            and not any(candidate.is_file() and _valid_image_file(candidate) for candidate in output_dir.glob("*"))
        )
        if authorized_replacement:
            checkpoint["operator_project_replacement"]["consumed_at"] = time.time()
            checkpoint["operator_project_replacement"]["prior_uncertainty_preserved"] = True
            checkpoint["run_id"] = run_id = uuid.uuid4().hex
            checkpoint["dispatch_journal"] = {"version": 1, "workspace_id": workspace_id, "run_id": run_id,
                "prompts": {str(index): {"prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "status": "not-dispatched"} for index, prompt in enumerate(clean_prompts, 1)}}
            # Commit the one-use grant before any generation can leave this process.
            atomic_write_json(checkpoint_path, checkpoint)
        safe_to_create_new_project = bool(authorized_replacement or (
            workspace_id
            and not flow_project_id
            and not entries
            and not results_by_index
            and _legacy_flow_checkpoint_proves_all_prompts_unsent(
                checkpoint,
                list(clean_prompts),
                str(workspace_id),
            )
            and not any(
                candidate.is_file() and _valid_image_file(candidate)
                for candidate in output_dir.glob("*")
            )
        ))
        if safe_to_create_new_project:
            failures.clear()
            checkpoint["failures"] = {}
            checkpoint["failed"] = 0
            checkpoint["incomplete_indices"] = list(range(1, total + 1))
            checkpoint["missing_image_indices"] = list(range(1, total + 1))
            atomic_write_json(checkpoint_path, checkpoint)
            if on_progress:
                on_progress(
                    0.0,
                    ("Đã giữ biên nhận cũ và nhận quyền thay project đúng một lần; "
                     if authorized_replacement else "Checkpoint xác nhận chưa gửi prompt nào và chưa có ảnh; ") +
                    "đang tự tạo project Flow mới để tiếp tục an toàn.",
                )

        # If any prior prompt was submitted, uncertain, or produced an image,
        # retain the existing fail-closed rebind path to prevent duplicates.
        if (
            workspace_id
            and not flow_project_id
            and (entries or failures)
            and len(results_by_index) < total
        ):
            checkpoint["status"] = "incomplete"
            checkpoint["completed"] = len(results_by_index)
            checkpoint["failed"] = len(failures)
            checkpoint["incomplete_indices"] = [
                index for index in range(1, total + 1)
                if index not in results_by_index
            ]
            checkpoint["missing_image_indices"] = list(
                checkpoint["incomplete_indices"]
            )
            atomic_write_json(checkpoint_path, checkpoint)
            raise FlowAgentSubmissionUncertain(
                "Workspace Flow cũ đã có checkpoint nhưng chưa lưu flow_project_id; "
                "tool không tự chọn tab hiện tại hoặc tạo project mới để tránh "
                "gửi trùng. Hãy ghép workspace này với đúng project Flow một lần, "
                "sau đó bấm Tiếp tục. Chi tiết: FLOW_WORKSPACE_PROJECT_ID_REQUIRED"
            )

        if recoverable_failures:
            # A prompt can be acknowledged by Flow while its response/download
            # is lost. Read the currently visible project before deciding to
            # resend anything. This is deliberately best-effort for already
            # submitted prompts, but an uncertain prompt remains fail-closed
            # unless the project contains an exact prompt match.
            snapshot: dict[str, object] | None = None
            snapshot_error = ""
            snapshot_wait_started = time.monotonic()
            try:
                snapshot = self.inspect_project_snapshot(
                    [prompt for _index, prompt, _failure in recoverable_failures],
                    client_id=client_id or flow_client_id or None,
                    workspace_id=workspace_id or None,
                    flow_project_id=flow_project_id or None,
                    # Flow exposes only a shortened tile title in the project
                    # grid. The exact prompt is available through that tile's
                    # own "reuse prompt" control; read it without submitting.
                    inspect_tile_prompts=True,
                    # The extension may inject the current UI bundle and
                    # wake a minimized renderer before it can answer. The
                    # bridge still serializes this read with the workspace
                    # queue and keeps the operation fail-closed on timeout.
                    timeout=300.0,
                )
            except Exception as exc:
                snapshot_error = str(exc).strip() or exc.__class__.__name__

            # A visible Flow Stop control means the previous accepted request
            # may still be generating. Keep reading the exact saved project
            # until that generation settles; a one-shot snapshot here used to
            # leave every uncertain checkpoint permanently locked.
            while _flow_snapshot_has_active_generation(snapshot):
                elapsed = time.monotonic() - snapshot_wait_started
                remaining = FLOW_UI_BUSY_MAX_WAIT_SECONDS - elapsed
                if remaining <= 0:
                    snapshot_error = (
                        "Flow vẫn đang tạo ảnh; đã hết thời gian chờ an toàn, "
                        "checkpoint được giữ và sẽ tự đối chiếu lại."
                    )
                    break
                if on_progress:
                    on_progress(
                        0.0,
                        "Flow vẫn đang xử lý prompt cũ · đang chờ hoàn tất để đối chiếu "
                        "đúng project trước khi quyết định có gửi lại hay không…",
                    )
                _wait_cancellable(
                    min(FLOW_UI_GENERATION_SETTLE_POLL_SECONDS, remaining),
                    cancel_event,
                )
                try:
                    snapshot = self.inspect_project_snapshot(
                        [prompt for _index, prompt, _failure in recoverable_failures],
                        client_id=client_id or flow_client_id or None,
                        workspace_id=workspace_id or None,
                        flow_project_id=flow_project_id or None,
                        inspect_tile_prompts=True,
                        timeout=300.0,
                    )
                    snapshot_error = ""
                except Exception as exc:
                    snapshot = None
                    snapshot_error = str(exc).strip() or exc.__class__.__name__
                    break

            raw_matches = snapshot.get("matches") if isinstance(snapshot, dict) else []
            if not isinstance(raw_matches, list):
                raw_matches = []
            verified_empty_snapshot = bool(
                not snapshot_error and isinstance(snapshot, dict)
                and _snapshot_is_authoritative_for_unsent(snapshot, flow_project_id)
                and snapshot.get("tileCount") == 0
            )
            if verified_empty_snapshot and submitted_without_image_indices:
                checkpoint["status"] = "incomplete"
                checkpoint["last_reconciliation"] = {
                    "status": "submitted-history-missing", "flow_project_id": flow_project_id,
                    "at_unix": time.time(), "new_generation_dispatched": False,
                }
                atomic_write_json(checkpoint_path, checkpoint)
                raise FlowAgentSubmissionUncertain(
                    "Project Flow đã xác minh trống nhưng checkpoint ghi prompt đã gửi; "
                    "giữ lượt cũ và dừng tạo ảnh mới để tránh gửi trùng. "
                    "Chi tiết: FLOW_SUBMITTED_HISTORY_MISSING;project-snapshot=verified-empty"
                )
            # A legacy shared tab may have supplied its empty project ID to
            # several workspaces. Once the exact saved project proves empty
            # and every prior failure proves no submit, use a fresh dedicated
            # project rather than putting those workspaces into the same grid.
            verified_empty_rebind = bool(
                not snapshot_error and flow_project_id and workspace_id
                and not entries and not results_by_index
                and not submitted_without_image_indices and failures
                and verified_empty_snapshot
                and len(raw_matches) == len(recoverable_failures)
                and all(isinstance(item, dict) and item.get("matched") is False
                        for item in raw_matches)
                and all(isinstance(failure, dict)
                        and _uncertainty_is_safe_to_retry_after_no_match(failure, flow_project_id)
                        for failure in failures.values())
                and not any(_valid_image_file(path) for path in output_dir.glob("Image_*.png"))
            )
            if verified_empty_rebind:
                audit = checkpoint.get("flow_project_rebinds")
                audit = audit if isinstance(audit, list) else []
                audit.append({"from_project_id": flow_project_id,
                    "reason": "verified-empty-project-with-no-prior-submit",
                    "at_unix": time.time(), "prompt_indices": [i for i, _, _ in recoverable_failures]})
                checkpoint["flow_project_rebinds"] = audit[-20:]
                failures.clear()
                recoverable_failures.clear()
                pending_indices.clear()
                flow_project_id = ""
                flow_client_id = ""
                checkpoint.pop("flow_project_id", None)
                checkpoint.pop("flow_client_id", None)
                checkpoint["failed"] = 0
                checkpoint["status"] = "running"
                atomic_write_json(checkpoint_path, checkpoint)
                if on_progress:
                    on_progress(0.0, "Đã xác minh project cũ trống và chưa gửi prompt; đang tạo project riêng cho workspace.")
            matches = {
                int(item.get("promptIndex")): item
                for item in raw_matches
                if isinstance(item, dict)
                and str(item.get("promptIndex", "")).isdigit()
            }
            unresolved_uncertain: list[tuple[int, str, dict[str, object]]] = []
            for relative_index, (index, prompt, failure) in enumerate(recoverable_failures, 1):
                match = matches.get(relative_index) or {}
                if not bool(match.get("matched")):
                    if failure.get("submission_uncertain") is True:
                        if (
                            _uncertainty_is_safe_to_retry_after_no_match(
                                failure,
                                flow_project_id,
                            )
                            and isinstance(match, dict)
                            and match.get("matched") is False
                            and _snapshot_is_authoritative_for_unsent(
                                snapshot,
                                flow_project_id,
                            )
                            # Keep the known completed-project collision on
                            # the dedicated rebind path below; otherwise the
                            # stale project ID could receive a new workspace's
                            # prompt even though its history scan was empty.
                            and not foreign_project_owner
                        ):
                            failures.pop(str(index), None)
                            pending_indices.discard(index)
                            checkpoint["failed"] = len(failures)
                            atomic_write_json(checkpoint_path, checkpoint)
                        else:
                            unresolved_uncertain.append((index, prompt, failure))
                    continue

                raw_image = match.get("image")
                image = raw_image if isinstance(raw_image, dict) else {}
                image_url = str(
                    image.get("url")
                    or image.get("imageUrl")
                    or ""
                ).strip()
                if image_url.lower().startswith(("http://", "https://")):
                    destination = output_dir / f"Image_{index:04d}.png"
                    try:
                        recovered_path = self._download(image_url, destination)
                        if not _valid_image_file(recovered_path):
                            recovered_path.unlink(missing_ok=True)
                            raise FlowAgentImageError("tệp Flow tải về không hợp lệ")
                        recovered = FlowGeneratedImage(
                            prompt_index=index,
                            prompt=prompt,
                            path=recovered_path,
                            model=str(failure.get("model") or model),
                            media_id=str(
                                image.get("mediaId")
                                or image.get("media_id")
                                or image.get("tileId")
                                or match.get("tileId")
                                or ""
                            ),
                        )
                        results_by_index[index] = recovered
                        entries[str(index)] = {
                            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                            "filename": recovered_path.name,
                            "media_id": recovered.media_id,
                            "model": recovered.model,
                            "source": "flow_project_snapshot",
                        }
                        failures.pop(str(index), None)
                        submitted_without_image_indices.discard(index)
                        pending_indices.discard(index)
                        checkpoint["completed"] = len(results_by_index)
                        atomic_write_json(checkpoint_path, checkpoint)
                        if on_progress:
                            on_progress(
                                index / total,
                                f"Đã khôi phục ảnh {index}/{total} từ lịch sử dự án Flow.",
                            )
                        continue
                    except Exception as exc:
                        failure["reason"] = (
                            "FLOW_PROJECT_MATCHED_IMAGE_DOWNLOAD_FAILED: "
                            f"{str(exc).strip() or exc.__class__.__name__}"
                        )

                # An exact prompt match proves that the submit reached Flow,
                # even if the image is still processing or cannot be downloaded.
                # Convert uncertainty to submitted-no-image so a retry can
                # never create a duplicate generation.
                failure["submitted"] = True
                failure["submission_uncertain"] = False
                failure["reason"] = (
                    "FLOW_PROJECT_MATCHED_NO_IMAGE: "
                    f"tile={str(match.get('tileId') or 'unknown')}"
                )
                submitted_without_image_indices.add(index)
                pending_indices.discard(index)

            # Older extension builds could persist the ID of whichever shared
            # Flow tab was open when a new workspace lost its response channel.
            # If that exact ID is now owned by a different, fully completed
            # workspace, this workspace has no saved image entries, and an
            # authoritative scan of the foreign project contains none of its
            # uncertain prompts, rebind it to a fresh project.  Otherwise keep
            # the existing fail-closed duplicate protection.
            foreign_binding_is_proven_stale = bool(
                unresolved_uncertain
                and foreign_project_owner
                and not entries
                and not results_by_index
                and not submitted_without_image_indices
                and len(unresolved_uncertain) == len(recoverable_failures)
                and all(
                    failure.get("submission_uncertain") is True
                    and failure.get("submitted") is not True
                    and str(failure.get("flow_project_id") or flow_project_id)
                    == flow_project_id
                    for failure in failures.values()
                    if isinstance(failure, dict)
                )
                and all(isinstance(failure, dict) for failure in failures.values())
                and snapshot_error == ""
                and _snapshot_is_authoritative_for_unsent(
                    snapshot,
                    flow_project_id,
                )
                and all(
                    isinstance(matches.get(relative_index), dict)
                    and matches[relative_index].get("matched") is False
                    for relative_index in range(1, len(recoverable_failures) + 1)
                )
            )
            if foreign_binding_is_proven_stale:
                old_project_id = flow_project_id
                rebound_indices = [index for index, _prompt, _failure in unresolved_uncertain]
                audit = checkpoint.get("flow_project_rebinds")
                if not isinstance(audit, list):
                    audit = []
                audit.append(
                    {
                        "from_project_id": old_project_id,
                        "owner_workspace_id": foreign_project_owner["workspace_id"],
                        "owner_workspace_name": foreign_project_owner["workspace_name"],
                        "reason": "completed-workspace-collision-with-authoritative-no-prompt-match",
                        "prompt_indices": rebound_indices,
                        "at_unix": time.time(),
                    }
                )
                checkpoint["flow_project_rebinds"] = audit[-20:]
                for index in rebound_indices:
                    failures.pop(str(index), None)
                flow_project_id = ""
                flow_client_id = ""
                checkpoint.pop("flow_project_id", None)
                checkpoint.pop("flow_client_id", None)
                checkpoint["status"] = "running"
                checkpoint["failed"] = len(failures)
                checkpoint["incomplete_indices"] = [
                    index
                    for index in range(1, total + 1)
                    if index not in results_by_index
                ]
                checkpoint["missing_image_indices"] = list(
                    checkpoint["incomplete_indices"]
                )
                atomic_write_json(checkpoint_path, checkpoint)
                if on_progress:
                    on_progress(
                        0.0,
                        "Project Flow cũ đã được nhận diện là của workspace "
                        f"{foreign_project_owner['workspace_name']}; đã chuyển "
                        "workspace này sang project mới sau khi xác nhận prompt "
                        "không có trong project cũ.",
                    )
                unresolved_uncertain.clear()

            if unresolved_uncertain:
                index, _prompt, failure = unresolved_uncertain[0]
                reason = str(failure.get("reason") or "FLOW_UI_SUBMISSION_UNCERTAIN")
                scan_verified = bool(
                    not snapshot_error
                    and _snapshot_is_authoritative_for_unsent(snapshot, flow_project_id)
                )
                if snapshot_error:
                    reason += f"; snapshot-error={snapshot_error}"
                snapshot_status = (
                    "unavailable" if snapshot_error else
                    "no-exact-prompt-match" if scan_verified else "unverified"
                )
                reason += f"; project-snapshot={snapshot_status}"
                checkpoint["last_reconciliation"] = {
                    "status": snapshot_status,
                    "flow_project_id": flow_project_id,
                    "at_unix": time.time(),
                    "snapshot_error": snapshot_error[:2000],
                    "prompt_index": index,
                    "new_generation_dispatched": False,
                }
                checkpoint["status"] = "incomplete"
                checkpoint["failed"] = len(failures)
                checkpoint["incomplete_indices"] = [index]
                atomic_write_json(checkpoint_path, checkpoint)
                raise FlowAgentSubmissionUncertain(reason)
            if recoverable_failures:
                atomic_write_json(checkpoint_path, checkpoint)

        sent_in_this_run = 0
        for index, prompt in enumerate(clean_prompts, 1):
            if cancel_event is not None and cancel_event.is_set():
                raise FlowAgentCancelled("Đã hủy tạo ảnh Flow.")
            if index in results_by_index:
                if on_progress:
                    on_progress(
                        index / total,
                        f"Đã phục hồi ảnh {index}/{total} từ checkpoint workspace.",
                    )
                continue
            if index in submitted_without_image_indices:
                if on_progress:
                    on_progress(
                        index / total,
                        f"Prompt {index}/{total} đã được gửi ở lần trước; không gửi trùng.",
                    )
                continue
            if index in pending_indices:
                if on_progress:
                    on_progress(
                        index / total,
                        f"Prompt {index}/{total} chưa gửi · đang chờ lượt Tiếp tục.",
                    )
                continue
            if sent_in_this_run:
                low, high = sorted((float(inter_prompt_delay[0]), float(inter_prompt_delay[1])))
                delay = random.uniform(max(0.0, low), max(0.0, high))
                if on_progress:
                    on_progress(
                        (index - 1) / total,
                        f"Chờ an toàn {delay:.1f} giây trước ảnh {index}/{total}…",
                    )
                _wait_cancellable(delay, cancel_event)
            if on_progress:
                on_progress((index - 1) / total, f"Flow đang tạo ảnh {index}/{total}…")
            key = hashlib.sha256(f"{model}\0{aspect}\0{prompt}".encode("utf-8")).hexdigest()
            response: dict[str, object] | None = None
            attempt = 0
            ui_recovery_started_at = time.monotonic()
            connection_recovery_started_at: float | None = None
            ui_unsent_attempts = 0
            while True:
                attempt += 1
                if cancel_event is not None and cancel_event.is_set():
                    raise FlowAgentCancelled("Đã hủy tạo ảnh Flow.")
                try:
                    route_client_id = client_id or flow_client_id or None
                    journal = checkpoint.get("dispatch_journal", {}).get("prompts", {})
                    if str(index) in journal:
                        journal[str(index)].update(status="attempting", attempt=attempt, dispatch_round=dispatch_round)
                        atomic_write_json(checkpoint_path, checkpoint)
                    response = _json_request(
                        f"{self.api_base}/v1/images/generations",
                        method="POST",
                        payload={
                            "prompt": prompt,
                            "model": model,
                            "size": sizes.get(aspect, sizes["landscape"]),
                            "n": FLOW_REQUIRED_IMAGE_COUNT,
                            "workspace_id": workspace_id or None,
                            "flow_project_id": flow_project_id or None,
                            "flow_client_id": route_client_id,
                            "new_flow_project": bool(workspace_id and not flow_project_id),
                        },
                        # Longer than the visible-UI watchdog (4 minutes), the
                        # extension transport deadline and bridge cancellation
                        # reconciliation. A shorter
                        # desktop timeout used to leave the Edge job alive, so
                        # every later prompt immediately failed FLOW_UI_BUSY.
                        timeout=390.0,
                        headers={
                            **({"X-Client-Id": route_client_id} if route_client_id else {}),
                            "Idempotency-Key": (
                                f"docurandom-{run_id[:16]}-r{dispatch_round:04d}-"
                                f"{index:04d}-{key[:20]}-a{attempt}"
                            )
                        },
                    )
                    observed_project_id = str(response.get("projectId") or "").strip()
                    if str(index) in journal:
                        journal[str(index)]["status"] = "submitted"
                        atomic_write_json(checkpoint_path, checkpoint)
                    observed_client_id = str(response.get("clientId") or "").strip()
                    for response_item in response.get("data") or []:
                        if isinstance(response_item, dict):
                            observed_project_id = str(
                                response_item.get("project_id") or observed_project_id
                            ).strip()
                            observed_client_id = str(
                                response_item.get("client_id") or observed_client_id
                            ).strip()
                            if observed_project_id:
                                break
                    if observed_project_id and observed_project_id != flow_project_id:
                        flow_project_id = observed_project_id
                        checkpoint["flow_project_id"] = flow_project_id
                    if observed_client_id and observed_client_id != flow_client_id:
                        flow_client_id = observed_client_id
                        checkpoint["flow_client_id"] = flow_client_id
                    if observed_project_id or observed_client_id:
                        atomic_write_json(checkpoint_path, checkpoint)
                    break
                except Exception as exc:
                    # A timeout/uncertain response can arrive after Flow has
                    # already created the project. Preserve that identity
                    # before recording the failure; otherwise Resume would
                    # create a second project and could duplicate the prompt.
                    observed_error_project = _flow_project_id_from_error(exc)
                    if observed_error_project and observed_error_project != flow_project_id:
                        flow_project_id = observed_error_project
                        checkpoint["flow_project_id"] = flow_project_id
                    if route_client_id and route_client_id != flow_client_id:
                        flow_client_id = route_client_id
                        checkpoint["flow_client_id"] = flow_client_id
                    if observed_error_project or route_client_id:
                        atomic_write_json(checkpoint_path, checkpoint)
                    classified = _friendly_generation_error(exc)
                    if not observed_error_project:
                        observed_error_project = _flow_project_id_from_error(classified)
                        if observed_error_project and observed_error_project != flow_project_id:
                            flow_project_id = observed_error_project
                            checkpoint["flow_project_id"] = flow_project_id
                            atomic_write_json(checkpoint_path, checkpoint)
                    if isinstance(classified, FlowAgentSubmissionUncertain):
                        record_failure(index, prompt, str(classified), submission_uncertain=True)
                        raise classified from exc
                    retryable = isinstance(
                        classified,
                        (
                            FlowAgentCaptchaError,
                            FlowAgentRateLimitError,
                            FlowAgentWindowError,
                            FlowAgentUiError,
                        ),
                    )
                    connection_loss = isinstance(classified, FlowAgentConnectionError)
                    if connection_loss:
                        if connection_recovery_started_at is None:
                            connection_recovery_started_at = time.monotonic()
                    ui_busy = (
                        isinstance(classified, FlowAgentUiError)
                        and "FLOW_UI_BUSY" in str(classified).upper()
                        and "FLOW_UI_BUSY_TIMEOUT" not in str(classified).upper()
                    )
                    error_upper = str(classified).upper()
                    ui_unsent = (
                        isinstance(classified, FlowAgentUiError)
                        and any(
                            marker in error_upper
                            for marker in (
                                "FLOW_UI_TIMEOUT_UNSENT",
                                "FLOW_UI_CONTENT_TIMEOUT",
                            )
                        )
                    )
                    if ui_busy or ui_unsent:
                        ui_unsent_attempts += int(ui_unsent)
                        if (time.monotonic() - ui_recovery_started_at >= FLOW_UI_BUSY_MAX_WAIT_SECONDS
                                or ui_unsent_attempts >= FLOW_UI_MAX_UNSENT_ATTEMPTS):
                            raise FlowAgentUiError(
                                "Flow vẫn khóa tác vụ giao diện quá lâu; đã dừng an toàn "
                                "và giữ prompt trong checkpoint. Không tự tải lại hoặc mở "
                                "thêm tab Flow. Chi tiết: FLOW_UI_BUSY_TIMEOUT"
                            ) from exc
                    if not retryable or (
                        not ui_busy
                        and not ui_unsent
                        and not connection_loss
                        and attempt >= 3
                    ):
                        if isinstance(
                            classified,
                            (
                                FlowAgentQuotaError,
                                FlowAgentCaptchaError,
                                FlowAgentRateLimitError,
                                FlowAgentWindowError,
                                FlowAgentUiError,
                            ),
                        ):
                            raise classified from exc
                        reason = str(classified)
                        record_failure(
                            index,
                            prompt,
                            reason,
                            submitted="flow_prompt_submitted_no_image" in reason.casefold(),
                        )
                        response = None
                        break
                    if connection_loss:
                        # The backend rejected before dispatching the prompt.
                        # Let its live connection gate retry this same prompt,
                        # without inferring that a missing socket means a closed
                        # window. Never walk through the rest of the batch.
                        if (
                            connection_recovery_started_at is not None
                            and time.monotonic() - connection_recovery_started_at
                            >= FLOW_CONNECTION_MAX_WAIT_SECONDS
                        ):
                            raise FlowAgentConnectionError(
                                "Flow extension không kết nối lại trong "
                                f"{int(FLOW_CONNECTION_MAX_WAIT_SECONDS)} giây; "
                                "checkpoint vẫn được giữ, chưa gửi lại prompt."
                            ) from exc
                        retry_delay = random.uniform(2.0, 4.0)
                    elif isinstance(classified, FlowAgentWindowError):
                        self.open_flow()
                        retry_delay = random.uniform(4.0, 7.0)
                    elif ui_busy:
                        retry_delay = random.uniform(8.0, 12.0)
                    elif ui_unsent:
                        # The extension positively reports that the prompt has
                        # not been submitted. Retrying this exact prompt is safe,
                        # and keeping the loop here preserves the coordinator's
                        # FIFO lease so another workspace cannot jump ahead.
                        retry_delay = random.uniform(2.0, 4.0)
                    elif isinstance(classified, FlowAgentUiError):
                        retry_delay = random.uniform(2.0, 4.0)
                    elif isinstance(classified, FlowAgentCaptchaError):
                        retry_delay = random.uniform(7.0, 12.0)
                    else:
                        retry_delay = random.uniform(20.0, 30.0)
                    if on_progress:
                        if isinstance(classified, FlowAgentConnectionError):
                            reason = "Flow mất kết nối · giữ lượt và thử lại đúng prompt, không mở thêm tab"
                        elif isinstance(classified, FlowAgentWindowError):
                            reason = "Cửa sổ Flow vừa bị đóng · đã tự mở lại"
                        elif ui_busy:
                            reason = "Flow còn tác vụ giao diện trước chưa giải phóng"
                        elif ui_unsent:
                            reason = (
                                "Workspace này vẫn giữ lượt Flow · driver đang tự phục hồi, "
                                "không chuyển sang workspace khác"
                            )
                        elif isinstance(classified, FlowAgentUiError):
                            reason = "Giao diện Flow chưa nhận được prompt"
                        elif isinstance(classified, FlowAgentCaptchaError):
                            reason = "CAPTCHA chưa sẵn sàng"
                        else:
                            reason = "Flow đang giới hạn tốc độ"
                        on_progress(
                            (index - 1) / total,
                            (
                                f"{reason} · tự kiểm tra lại lần {attempt} sau "
                                f"{retry_delay:.0f} giây…"
                                if ui_busy or ui_unsent or connection_loss
                                else f"{reason} · tự thử lại {attempt}/2 sau {retry_delay:.0f} giây…"
                            ),
                        )
                    _wait_cancellable(retry_delay, cancel_event)
            sent_in_this_run += 1
            if response is None:
                continue
            items = response.get("data")
            if not isinstance(items, list) or not items or not isinstance(items[0], dict):
                record_failure(
                    index,
                    prompt,
                    "Flow không trả dữ liệu ảnh hợp lệ.",
                    submitted=True,
                )
                continue
            item = items[0]
            raw_url = str(item.get("url", "")).strip()
            if not raw_url:
                record_failure(index, prompt, "Flow không trả URL ảnh.", submitted=True)
                continue
            url = urllib.parse.urljoin(f"{self.api_base}/", raw_url)
            destination = output_dir / f"Image_{index:04d}.png"
            try:
                path = self._download(url, destination)
            except Exception as exc:
                record_failure(
                    index,
                    prompt,
                    f"Tải ảnh thất bại: {exc}",
                    submitted=True,
                )
                continue
            if cancel_event is not None and cancel_event.is_set():
                path.unlink(missing_ok=True)
                raise FlowAgentCancelled("Đã hủy tạo ảnh Flow.")
            if not _valid_image_file(path):
                path.unlink(missing_ok=True)
                record_failure(
                    index,
                    prompt,
                    "Tệp tải về bị rỗng hoặc không phải ảnh hợp lệ.",
                    submitted=True,
                )
                continue
            generated = FlowGeneratedImage(
                prompt_index=index,
                prompt=prompt,
                path=path,
                model=str(item.get("model") or model),
                media_id=str(item.get("media_id", "")),
            )
            results_by_index[index] = generated
            failures.pop(str(index), None)
            entries[str(index)] = {
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "filename": path.name,
                "media_id": generated.media_id,
                "model": generated.model,
            }
            checkpoint["completed"] = len(results_by_index)
            atomic_write_json(checkpoint_path, checkpoint)
            if on_progress:
                on_progress(index / total, f"Đã kiểm tra và lưu ảnh {index}/{total}.")
        results = tuple(results_by_index[index] for index in sorted(results_by_index))
        unsubmitted_indices = [
            index
            for index in range(1, total + 1)
            if index not in results_by_index
            and index not in submitted_without_image_indices
        ]
        missing_image_indices = [
            index for index in range(1, total + 1) if index not in results_by_index
        ]
        if unsubmitted_indices:
            reason_counts: dict[str, int] = {}
            for value in failures.values():
                if not isinstance(value, dict):
                    continue
                reason = str(value.get("reason", "")).strip()
                if reason:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
            reason_summary = "; ".join(
                f"{count} ảnh: {reason}"
                for reason, count in sorted(
                    reason_counts.items(), key=lambda item: (-item[1], item[0])
                )[:3]
            )
            checkpoint["status"] = "incomplete"
            checkpoint["completed"] = len(results)
            checkpoint["failed"] = len(failures)
            checkpoint["submitted"] = total - len(unsubmitted_indices)
            checkpoint["incomplete_indices"] = unsubmitted_indices
            checkpoint["missing_image_indices"] = missing_image_indices
            atomic_write_json(checkpoint_path, checkpoint)
            missing_text = ", ".join(str(index) for index in unsubmitted_indices[:12])
            if len(unsubmitted_indices) > 12:
                missing_text += ", …"
            raise FlowAgentIncompleteError(
                f"Tool có {total} prompt nhưng mới xác nhận đã gửi "
                f"{total - len(unsubmitted_indices)}/{total}. "
                f"Prompt chưa gửi được: {missing_text}; hàng đợi dừng đúng tại phần chưa gửi. "
                "Ảnh đã tạo và toàn bộ prompt vẫn được giữ trong checkpoint. "
                "Bấm Tiếp tục để chỉ gửi phần còn thiếu."
                + (f" Nguyên nhân: {reason_summary}" if reason_summary else "")
            )
        checkpoint["status"] = (
            "complete_with_image_failures" if missing_image_indices else "complete"
        )
        checkpoint["completed"] = len(results)
        checkpoint["submitted"] = total
        checkpoint["failed"] = len(missing_image_indices)
        checkpoint["incomplete_indices"] = []
        checkpoint["missing_image_indices"] = missing_image_indices
        atomic_write_json(checkpoint_path, checkpoint)
        if not results:
            raise FlowAgentError(
                f"Đã gửi đủ {total}/{total} prompt sang Flow nhưng không có ảnh hợp lệ để dựng video. "
                "Tool không gửi lại các prompt này nhằm tránh tạo trùng; hãy kiểm tra dự án Flow."
            )
        if on_progress:
            on_progress(
                1.0,
                f"Đã gửi đủ {total}/{total} prompt sang Flow và lưu {len(results)} ảnh hợp lệ"
                + (
                    f"; {len(missing_image_indices)} prompt không có ảnh."
                    if missing_image_indices
                    else "."
                ),
            )
        return results
