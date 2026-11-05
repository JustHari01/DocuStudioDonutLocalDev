from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import dataclass, field
import atexit
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from .storage import app_data_dir, atomic_write_json


logger = logging.getLogger(__name__)


MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_CLIENT_NAME = "DocuRandom Studio"
MCP_SECRET_FILENAME = "donut-mcp-endpoint.dat"
DOCURANDOM_LOCAL_SERVER_NAME = "donut-browser-local"
DOCURANDOM_LOCAL_REQUIRED_TOOLS = frozenset(
    {
        "list_profiles",
        "run_profile",
        "navigate",
        "evaluate_javascript",
        "get_debug_connection",
        "cdp_command",
    }
)
PROFILE_LAUNCH_TIMEOUT_SECONDS = 120.0
PROFILE_LAUNCH_STALE_RETRY_SECONDS = max(
    PROFILE_LAUNCH_TIMEOUT_SECONDS * 2.0,
    300.0,
)
SHARED_TOOL_TIMEOUT_SECONDS = 120.0

# Donut Local can service short metadata calls concurrently, but starting a
# Wayfern profile is a process/proxy operation and is not safe to fan out from
# every workspace at once.  Keep one launch request in flight per Studio
# process; callers queue behind it instead of making Donut's MCP loop stall.
_PROFILE_LAUNCH_LOCK = threading.Lock()
_PROFILE_LAUNCH_STATE_LOCK = threading.RLock()
_PROFILE_LAUNCH_STATE_FILENAME = "donut-profile-launch-state.json"


@contextmanager
def _exclusive_runtime_file_lock(filename: str, *, timeout: float = 10.0):
    """An OS-owned lock, released even when a Studio process crashes."""
    path = app_data_dir() / "runtime" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise DonutMcpLaunchUncertain(
                        "Một tiến trình DocuRandom khác đang điều phối Donut; "
                        "không gửi thêm lệnh mở profile."
                    ) from exc
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class DonutMcpError(RuntimeError):
    pass


class DonutMcpTimeoutError(DonutMcpError):
    """The local MCP endpoint answered too slowly, but may still be alive.

    A timeout is deliberately distinct from a refused connection. Donut keeps
    a long-running profile operation on the server while the HTTP caller may
    stop waiting; treating that situation as a dead bridge causes needless
    reconnects and, for launch operations, duplicate work.
    """


class DonutMcpLaunchUncertain(DonutMcpTimeoutError):
    """A prior profile launch may still be executing in Donut."""


class DonutMcpPriorLaunchUnresolved(DonutMcpLaunchUncertain):
    """This call sent no launch because an older attempt is still unresolved."""


class DonutMcpEntitlementError(DonutMcpError):
    """Donut explicitly refused an operation because of plan/entitlement."""


class DonutMcpCapacityError(DonutMcpError):
    """Wayfern refused a launch because all plan instance slots are occupied."""


class DonutMcpAuthenticationError(DonutMcpError):
    """The local MCP endpoint explicitly rejected its authentication key."""


def _is_entitlement_error(message: str) -> bool:
    normalized = str(message or "").casefold()
    return any(
        marker in normalized
        for marker in (
            "custom fingerprints require a donut browser plan",
            "paid donut browser plan",
            "plan that includes them",
            "entitlement required",
            "not entitled",
        )
    )


def _is_capacity_error(message: str) -> bool:
    normalized = " ".join(str(message or "").casefold().split())
    return any(
        marker in normalized
        for marker in (
            "wayfern_instance_limit_reached",
            "instance_limit_reached",
            "instance limit reached",
            "browsers are already running on this plan",
        )
    )


def _capacity_error_message(detail: str) -> str:
    detail = str(detail or "").strip()
    message = (
        "Donut đã đạt giới hạn số phiên trình duyệt chạy đồng thời; profile mới chưa được mở. "
        "Hàng đợi và checkpoint vẫn được giữ nguyên."
    )
    return f"{message} Chi tiết Donut: {detail[:500]}" if detail else message


def _is_authentication_error(message: str) -> bool:
    normalized = str(message or "").casefold()
    return any(
        marker in normalized
        for marker in (
            "invalid mcp key",
            "mcp key is invalid",
            "authentication required",
            "unauthorized",
            "khóa mcp donut đã đổi",
            "khóa mcp không hợp lệ",
        )
    )


def _profile_launch_state_path() -> Path:
    return app_data_dir() / "runtime" / _PROFILE_LAUNCH_STATE_FILENAME


def _profile_launch_state_key(endpoint: DonutMcpEndpoint, profile_id: str) -> str:
    identity = f"{endpoint.origin}\0{str(profile_id).strip()}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _load_profile_launch_states() -> dict[str, dict[str, Any]]:
    path = _profile_launch_state_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.error("donut_profile_launch_state_unreadable path=%s", path)
        raise DonutMcpError(
            "Không đọc được trạng thái an toàn của lệnh mở profile Donut; "
            "đã dừng để tránh mở trùng."
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("entries"), dict)
    ):
        logger.error("donut_profile_launch_state_invalid path=%s", path)
        raise DonutMcpError(
            "Trạng thái an toàn của lệnh mở profile Donut không hợp lệ; "
            "đã dừng để tránh mở trùng."
        )
    return {
        str(key): value
        for key, value in payload["entries"].items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _get_profile_launch_state(
    endpoint: DonutMcpEndpoint,
    profile_id: str,
) -> dict[str, Any] | None:
    key = _profile_launch_state_key(endpoint, profile_id)
    with _PROFILE_LAUNCH_STATE_LOCK:
        with _exclusive_runtime_file_lock("donut-profile-launch-journal.lock"):
            return _load_profile_launch_states().get(key)


def _set_profile_launch_state(
    endpoint: DonutMcpEndpoint,
    profile_id: str,
    state: str,
) -> None:
    key = _profile_launch_state_key(endpoint, profile_id)
    with _PROFILE_LAUNCH_STATE_LOCK:
        with _exclusive_runtime_file_lock("donut-profile-launch-journal.lock"):
            entries = _load_profile_launch_states()
            entries[key] = {
                "profile_id": str(profile_id),
                "origin": endpoint.origin,
                "state": str(state),
                "updated_at": int(time.time()),
            }
            atomic_write_json(
                _profile_launch_state_path(),
                {"version": 1, "entries": entries},
            )


def _clear_profile_launch_state(
    endpoint: DonutMcpEndpoint,
    profile_id: str,
) -> None:
    key = _profile_launch_state_key(endpoint, profile_id)
    with _PROFILE_LAUNCH_STATE_LOCK:
        with _exclusive_runtime_file_lock("donut-profile-launch-journal.lock"):
            entries = _load_profile_launch_states()
            if key not in entries:
                return
            del entries[key]
            atomic_write_json(
                _profile_launch_state_path(),
                {"version": 1, "entries": entries},
            )


def _is_timeout_transport_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
        return True
    text = str(exc).casefold()
    return "timed out" in text or "timeout" in text


def _profile_process_alive(process_id: int | None) -> bool:
    if not process_id or process_id <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(process_id, 0)
            return True
        except OSError:
            return False
    kernel = ctypes.windll.kernel32
    open_process = kernel.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    handle = open_process(0x1000, False, process_id)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
    finally:
        kernel.CloseHandle(handle)


def _local_donut_restart_proof(
    endpoint: DonutMcpEndpoint,
    profile_id: str,
) -> tuple[int, int] | None:
    """Return the MCP owner's start time and live browser count for a profile.

    An old launch request cannot still be running in a Donut process that
    started after the request was recorded. The process and browser checks are
    read-only; any missing evidence keeps the launch guard in place.
    """

    if os.name != "nt" or not profile_id:
        return None
    try:
        port = urllib.parse.urlsplit(endpoint.origin).port
    except ValueError:
        return None
    if not port:
        return None
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    script = (
        "$ErrorActionPreference='Stop';"
        "$port=[int]$env:DOCURANDOM_MCP_PROBE_PORT;"
        "$profileId=$env:DOCURANDOM_MCP_PROBE_PROFILE_ID;"
        "$listener=Get-NetTCPConnection -LocalAddress '127.0.0.1' "
        "-LocalPort $port -State Listen | Select-Object -First 1;"
        "if(-not $listener){exit 3};"
        "$server=Get-CimInstance Win32_Process "
        "-Filter \"ProcessId=$($listener.OwningProcess)\";"
        "if(-not $server -or $server.Name -ine 'donutbrowser.exe'){exit 4};"
        "$browsers=@(Get-CimInstance Win32_Process "
        "-Filter \"Name='chrome.exe' OR Name='wayfern.exe' OR "
        "Name='chromium.exe'\" | Where-Object { $_.CommandLine -and "
        "$_.CommandLine.Contains($profileId) });"
        "[pscustomobject]@{"
        "started_at=([DateTimeOffset]$server.CreationDate).ToUnixTimeSeconds();"
        "profile_processes=$browsers.Count"
        "} | ConvertTo-Json -Compress"
    )
    environment = os.environ.copy()
    environment["DOCURANDOM_MCP_PROBE_PORT"] = str(port)
    environment["DOCURANDOM_MCP_PROBE_PROFILE_ID"] = profile_id
    try:
        result = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return None
        payload = json.loads(result.stdout)
        started_at = int(payload["started_at"])
        profile_processes = int(payload["profile_processes"])
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None
    if started_at <= 0 or profile_processes < 0:
        return None
    return started_at, profile_processes


def _debug_target_reachable(targets_url: str) -> bool:
    try:
        with urllib.request.urlopen(targets_url, timeout=3.0) as response:
            if response.status != 200:
                return False
            targets = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return isinstance(targets, list) and any(
        isinstance(target, dict)
        and target.get("type") == "page"
        and bool(target.get("id"))
        and str(target.get("webSocketDebuggerUrl") or "").startswith("ws://")
        for target in targets
    )


@dataclass(frozen=True)
class DonutMcpEndpoint:
    value: str
    origin: str
    token_hint: str

    @classmethod
    def parse(cls, raw: str) -> "DonutMcpEndpoint":
        value = str(raw or "").strip().strip('"')
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError as exc:
            raise DonutMcpError("URL MCP Donut không hợp lệ.") from exc
        if parsed.scheme.casefold() != "http":
            raise DonutMcpError("MCP Donut local phải dùng địa chỉ http://127.0.0.1.")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise DonutMcpError("Từ chối MCP không nằm trên chính máy này.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise DonutMcpError("URL MCP Donut chứa thành phần không được hỗ trợ.")
        match = re.fullmatch(r"/mcp/([A-Za-z0-9_-]{16,})/?", parsed.path)
        if match is None:
            raise DonutMcpError("URL MCP Donut phải có dạng /mcp/<khóa-cục-bộ>.")
        try:
            port = parsed.port
        except ValueError as exc:
            raise DonutMcpError("Cổng MCP Donut không hợp lệ.") from exc
        if port is None:
            raise DonutMcpError("URL MCP Donut chưa có cổng local.")
        host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
        origin = f"http://{host}:{port}"
        token = match.group(1)
        normalized = f"{origin}/mcp/{token}"
        return cls(normalized, origin, f"…{token[-4:]}")

    @property
    def safe_label(self) -> str:
        return f"{self.origin}/mcp/<đã che {self.token_hint}>"


@dataclass
class _SharedMcpSession:
    """One local MCP session shared by all worker threads in this process.

    Donut's local MCP server is a single loopback service. Creating a fresh
    Streamable-HTTP session for every workspace made concurrent startup and
    realtime monitoring contend on the server's initialize path. The session
    itself is safe to share as long as request ids are unique and the
    handshake/reconnect state is guarded separately from normal requests.
    """

    endpoint: DonutMcpEndpoint
    handshake_lock: threading.RLock = field(default_factory=threading.RLock)
    id_lock: threading.Lock = field(default_factory=threading.Lock)
    session_id: str = ""
    server_info: dict[str, Any] = field(default_factory=dict)
    tools: frozenset[str] | None = None
    next_id: int = 1
    active_clients: int = 0
    last_used_at: float = 0.0

    def next_request_id(self) -> int:
        with self.id_lock:
            request_id = self.next_id
            self.next_id += 1
            return request_id


_SHARED_SESSION_IDLE_SECONDS = 60.0
_SHARED_SESSION_REUSE_PROBE_SECONDS = 5.0
_SHARED_SESSIONS_LOCK = threading.RLock()
_SHARED_SESSIONS: dict[str, _SharedMcpSession] = {}


def _shared_session_for(endpoint: DonutMcpEndpoint) -> _SharedMcpSession:
    with _SHARED_SESSIONS_LOCK:
        state = _SHARED_SESSIONS.get(endpoint.value)
        if state is None:
            state = _SharedMcpSession(endpoint)
            _SHARED_SESSIONS[endpoint.value] = state
            return state

        # Do not keep a stale session alive forever. The next caller will
        # perform one clean initialize, while short-lived client contexts
        # within the same automation cycle reuse the existing handshake.
        if (
            state.session_id
            and state.active_clients == 0
            and state.last_used_at > 0
            and time.monotonic() - state.last_used_at
            > _SHARED_SESSION_IDLE_SECONDS
        ):
            with state.handshake_lock:
                if (
                    state.active_clients == 0
                    and state.last_used_at > 0
                    and time.monotonic() - state.last_used_at
                    > _SHARED_SESSION_IDLE_SECONDS
                ):
                    state.session_id = ""
                    state.server_info = {}
                    state.tools = None
                    state.next_id = 1
        return state


def _invalidate_shared_session(state: _SharedMcpSession, session_id: str) -> None:
    """Forget a session after a transport/server restart, without retrying a tool call."""

    with state.handshake_lock:
        if not session_id or state.session_id != session_id:
            return
        state.session_id = ""
        state.server_info = {}
        state.tools = None
        state.next_id = 1


def close_shared_donut_mcp_sessions() -> None:
    """Best-effort cleanup for process shutdown; never touches workspace data."""

    with _SHARED_SESSIONS_LOCK:
        states = tuple(_SHARED_SESSIONS.values())
    for state in states:
        with state.handshake_lock:
            session_id = state.session_id
            state.session_id = ""
            state.server_info = {}
            state.tools = None
            state.next_id = 1
            state.active_clients = 0
        if not session_id:
            continue
        request = urllib.request.Request(
            state.endpoint.value,
            headers={
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": session_id,
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            },
            method="DELETE",
        )
        try:
            urllib.request.urlopen(request, timeout=1.0).close()
        except (urllib.error.URLError, OSError):
            pass


atexit.register(close_shared_donut_mcp_sessions)


@dataclass(frozen=True)
class DonutGroup:
    group_id: str
    name: str


@dataclass(frozen=True)
class DonutProfile:
    profile_id: str
    name: str
    browser: str
    group_id: str = ""
    proxy_id: str = ""
    process_id: int | None = None
    extension_group_id: str = ""

    @property
    def running(self) -> bool:
        return bool(self.process_id)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _secret_path(path: Path | None = None) -> Path:
    return path or app_data_dir() / "secrets" / MCP_SECRET_FILENAME


def _protect_bytes(value: bytes) -> bytes:
    if os.name != "nt":
        raise DonutMcpError("Kho khóa MCP hiện chỉ hỗ trợ Windows.")
    buffer = ctypes.create_string_buffer(value)
    source = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "DocuRandom Donut MCP",
        None,
        None,
        None,
        0x01,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _unprotect_bytes(value: bytes) -> bytes:
    if os.name != "nt":
        raise DonutMcpError("Kho khóa MCP hiện chỉ hỗ trợ Windows.")
    buffer = ctypes.create_string_buffer(value)
    source = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0x01,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def save_donut_mcp_endpoint(raw: str, path: Path | None = None) -> DonutMcpEndpoint:
    endpoint = DonutMcpEndpoint.parse(raw)
    target = _secret_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(_protect_bytes(endpoint.value.encode("utf-8")))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return endpoint


def load_donut_mcp_endpoint(path: Path | None = None) -> DonutMcpEndpoint | None:
    target = _secret_path(path)
    try:
        encrypted = target.read_bytes()
    except OSError:
        return None
    try:
        raw = _unprotect_bytes(encrypted).decode("utf-8")
        return DonutMcpEndpoint.parse(raw)
    except (OSError, UnicodeError, ValueError, DonutMcpError):
        return None


def clear_donut_mcp_endpoint(path: Path | None = None) -> None:
    try:
        _secret_path(path).unlink(missing_ok=True)
    except OSError as exc:
        raise DonutMcpError(f"Không thể xóa khóa MCP Donut đã lưu: {exc}") from exc


class DonutMcpClient:
    """Small Streamable-HTTP MCP client restricted to Donut on loopback."""

    def __init__(
        self,
        endpoint: DonutMcpEndpoint | str,
        *,
        timeout: float = 30.0,
        shared_session: bool = True,
    ) -> None:
        self.endpoint = (
            endpoint if isinstance(endpoint, DonutMcpEndpoint) else DonutMcpEndpoint.parse(endpoint)
        )
        self.timeout = max(2.0, float(timeout))
        self.session_id = ""
        self.server_info: dict[str, Any] = {}
        self._next_id = 1
        self._lock = threading.RLock()
        # Short, independent sessions are intentionally available for
        # operations that may block inside Donut (notably run_profile).  A
        # long-running tool call must not head-of-line-block the global
        # connection check or another workspace's read-only metadata calls.
        self._use_shared_session = bool(shared_session)
        self._shared_state = _shared_session_for(self.endpoint)
        self._shared_lease = False
        self._shared_connecting = False

    def _has_live_session(self) -> bool:
        if not self.session_id:
            return False
        if not self._shared_lease:
            return True
        return self._shared_state.session_id == self.session_id

    def _next_request_id(self) -> int:
        if self._shared_lease or self._shared_connecting:
            return self._shared_state.next_request_id()
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _should_invalidate_after_transport_error(self, method: str) -> bool:
        if method != "tools/call" or not self._shared_lease:
            return True
        with self._shared_state.handshake_lock:
            # A slow tool can occupy Donut's request path while another
            # short read waits behind it. Do not tear down the shared session
            # because only that waiting request reached its own timeout.
            return self._shared_state.active_clients <= 1

    def _decode_response(self, raw: bytes, content_type: str) -> dict[str, Any]:
        if not raw.strip():
            return {}
        text = raw.decode("utf-8", errors="replace")
        if "text/event-stream" in content_type.casefold() or text.lstrip().startswith("event:"):
            data_lines = [
                line[5:].strip()
                for line in text.splitlines()
                if line.startswith("data:") and line[5:].strip()
            ]
            if not data_lines:
                raise DonutMcpError("MCP Donut trả luồng rỗng.")
            text = data_lines[-1]
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DonutMcpError("MCP Donut trả dữ liệu không hợp lệ.") from exc
        if not isinstance(value, dict):
            raise DonutMcpError("MCP Donut trả sai định dạng JSON-RPC.")
        error = value.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "").strip()
            message = str(error.get("message") or "").strip()
            classification_detail = " ".join(part for part in (code, message) if part)
            message = message or code or "MCP Donut từ chối yêu cầu."
            # Donut can report a tool failure as a JSON-RPC error instead of
            # a CallToolResult with isError=true. Both envelopes must receive
            # the same terminal-access classification before run_profile can
            # mistake a definitive denial for an uncertain launch.
            if _is_entitlement_error(classification_detail):
                raise DonutMcpEntitlementError(message)
            if _is_capacity_error(classification_detail):
                raise DonutMcpCapacityError(_capacity_error_message(message))
            if _is_authentication_error(classification_detail):
                raise DonutMcpAuthenticationError(message)
            raise DonutMcpError(message)
        return value

    def _request(
        self,
        payload: dict[str, Any],
        *,
        expect_response: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        method = str(payload.get("method") or "unknown")
        shared = self._shared_lease or self._shared_connecting
        shared_session_id = self._shared_state.session_id if shared else self.session_id
        request_session_id = shared_session_id
        logger.debug(
            "donut_mcp_request method=%s session=%s",
            method,
            bool(request_session_id),
        )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if request_session_id:
            headers["Mcp-Session-Id"] = request_session_id
        request = urllib.request.Request(
            self.endpoint.value,
            data=body,
            headers=headers,
            method="POST",
        )
        request_timeout = self.timeout if timeout is None else max(2.0, float(timeout))
        started_at = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                response_session_id = str(response.headers.get("Mcp-Session-Id") or "")
                if response_session_id:
                    if shared:
                        with self._shared_state.handshake_lock:
                            self._shared_state.session_id = response_session_id
                            self.session_id = response_session_id
                    else:
                        self.session_id = response_session_id
                raw = response.read()
                content_type = str(response.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            if exc.code == 401:
                detail = "Khóa MCP Donut đã đổi hoặc không còn hợp lệ."
            logger.warning("donut_mcp_http_error status=%s", exc.code)
            if (
                shared
                and exc.code in {400, 401, 404, 410, 502, 503, 504}
                and self._should_invalidate_after_transport_error(method)
            ):
                _invalidate_shared_session(self._shared_state, request_session_id)
            elif shared and exc.code in {502, 503, 504}:
                logger.warning(
                    "donut_mcp_session_invalidation_deferred method=%s active_clients=%s",
                    method,
                    self._shared_state.active_clients,
                )
            if exc.code == 401:
                raise DonutMcpAuthenticationError(detail) from exc
            raise DonutMcpError(detail or f"MCP Donut trả HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            timed_out = _is_timeout_transport_error(exc)
            logger.warning(
                "donut_mcp_%s method=%s error=%s",
                "timeout" if timed_out else "transport_error",
                method,
                type(exc).__name__,
            )
            # A request timeout does not prove the server/session is dead.
            # In particular, run_profile can still be launching a browser on
            # the Donut side after this HTTP wait expires. Keep the session
            # usable so the next status call can observe that progress.
            if (
                not timed_out
                and shared
                and self._should_invalidate_after_transport_error(method)
            ):
                _invalidate_shared_session(self._shared_state, request_session_id)
            elif shared and not timed_out:
                logger.warning(
                    "donut_mcp_session_invalidation_deferred method=%s active_clients=%s",
                    method,
                    self._shared_state.active_clients,
                )
            if timed_out:
                raise DonutMcpTimeoutError(
                    f"MCP Donut không phản hồi trong {request_timeout:g}s cho {method}; "
                    "lệnh có thể vẫn đang chạy, hãy kiểm tra lại trạng thái thay vì mở lại."
                ) from exc
            raise DonutMcpError(
                "Không kết nối được MCP Donut local. Hãy mở Donut Browser và bật MCP."
            ) from exc
        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        if method == "initialize":
            logger.info(
                "donut_mcp_initialize_done elapsed_ms=%s shared=%s",
                elapsed_ms,
                shared,
            )
        elif elapsed_ms >= 5000:
            logger.warning(
                "donut_mcp_slow_request method=%s elapsed_ms=%s",
                method,
                elapsed_ms,
            )
        if not expect_response:
            return {}
        return self._decode_response(raw, content_type)

    def connect(self) -> dict[str, Any]:
        with self._lock:
            if not self._use_shared_session:
                if self.session_id and self.server_info:
                    return self.server_info
                logger.info(
                    "donut_mcp_initialize origin=%s shared=False",
                    self.endpoint.origin,
                )
                request_id = self._next_request_id()
                response = self._request(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": MCP_PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": MCP_CLIENT_NAME, "version": "1.0"},
                        },
                    }
                )
                self._request(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    },
                    expect_response=False,
                )
                result = response.get("result")
                self.server_info = result if isinstance(result, dict) else {}
                if not self.session_id:
                    raise DonutMcpError(
                        "MCP Donut không trả mã phiên sau bước initialize."
                    )
                return self.server_info

            if self._shared_lease and self._has_live_session():
                self._shared_state.last_used_at = time.monotonic()
                return self.server_info

            state = self._shared_state
            with state.handshake_lock:
                if state.session_id and state.server_info:
                    idle_seconds = (
                        time.monotonic() - state.last_used_at
                        if state.last_used_at > 0
                        else 0.0
                    )
                    if state.active_clients == 0 and idle_seconds > _SHARED_SESSION_REUSE_PROBE_SECONDS:
                        # Donut clears its in-memory sessions when it restarts.
                        # A cheap ping avoids handing a dead cached session to
                        # the first workspace after that restart.
                        self._shared_connecting = True
                        try:
                            self._request(
                                {
                                    "jsonrpc": "2.0",
                                    "id": self._next_request_id(),
                                    "method": "ping",
                                    "params": {},
                                }
                            )
                        except DonutMcpError as exc:
                            logger.warning(
                                "donut_mcp_session_probe_failed error=%s",
                                type(exc).__name__,
                            )
                            state.session_id = ""
                            state.server_info = {}
                            state.tools = None
                            state.next_id = 1
                        finally:
                            self._shared_connecting = False

                if state.session_id and state.server_info:
                    state.active_clients += 1
                    state.last_used_at = time.monotonic()
                    self.session_id = state.session_id
                    self.server_info = dict(state.server_info)
                    self._shared_lease = True
                    logger.info(
                        "donut_mcp_session_reused origin=%s active_clients=%s",
                        self.endpoint.origin,
                        state.active_clients,
                    )
                    return self.server_info

                logger.info("donut_mcp_initialize origin=%s", self.endpoint.origin)
                self._shared_connecting = True
                try:
                    request_id = self._next_request_id()
                    response = self._request(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": MCP_PROTOCOL_VERSION,
                                "capabilities": {},
                                "clientInfo": {"name": MCP_CLIENT_NAME, "version": "1.0"},
                            },
                        }
                    )
                    self._request(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/initialized",
                            "params": {},
                        },
                        expect_response=False,
                    )
                    result = response.get("result")
                    state.server_info = result if isinstance(result, dict) else {}
                    if not state.session_id:
                        raise DonutMcpError(
                            "MCP Donut không trả mã phiên sau bước initialize."
                        )
                    state.tools = None
                    state.last_used_at = time.monotonic()
                    state.active_clients += 1
                    self.session_id = state.session_id
                    self.server_info = dict(state.server_info)
                    self._shared_lease = True
                    return self.server_info
                except Exception:
                    state.session_id = ""
                    state.server_info = {}
                    state.tools = None
                    state.next_id = 1
                    raise
                finally:
                    self._shared_connecting = False

    def require_docurandom_local(self) -> dict[str, Any]:
        """Reject an official/stale Donut endpoint before any browser action."""

        if self.server_info and (not self._shared_lease or self._has_live_session()):
            info = self.server_info
        else:
            info = self.connect()
        server = info.get("serverInfo")
        name = str(server.get("name") or "").strip() if isinstance(server, dict) else ""
        if name != DOCURANDOM_LOCAL_SERVER_NAME:
            if name:
                raise DonutMcpError(
                    "Cổng MCP đang thuộc Donut chính thức, không phải DocuRandom-DN. "
                    "Hãy đóng Donut chính thức và các profile của nó, mở DocuRandom-DN "
                    "rồi bấm Tiếp tục; checkpoint vẫn được giữ nguyên."
                )
            raise DonutMcpError(
                "MCP không xác nhận được danh tính DocuRandom-DN; checkpoint vẫn được giữ nguyên."
            )
        missing = sorted(DOCURANDOM_LOCAL_REQUIRED_TOOLS - self.list_tool_names())
        if missing:
            raise DonutMcpError(
                "DocuRandom-DN đang chạy nhưng thiếu công cụ local bắt buộc: "
                + ", ".join(missing)
                + ". Hãy cập nhật/cài lại đúng installer rồi bấm Tiếp tục."
            )
        return info

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        result = self.call_tool_value(name, arguments, timeout=timeout)
        if not isinstance(result, dict):
            raise DonutMcpError(f"MCP Donut không trả kết quả cho {name}.")
        return result

    def call_tool_value(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Return either an MCP CallToolResult or Donut's local structured value.

        Older Donut tools use the standard ``content`` wrapper, while the
        extension-management tools intentionally return their JSON value
        directly.  Keeping both shapes here lets callers use one safe client
        without relying on the REST token or touching profile files.
        """

        with self._lock:
            logger.debug("donut_mcp_tool name=%s", name)
            if not self._has_live_session():
                self.connect()
            request_id = self._next_request_id()
            request_timeout = timeout
            if request_timeout is None and self._shared_lease:
                request_timeout = max(self.timeout, SHARED_TOOL_TIMEOUT_SECONDS)
            response = self._request(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                },
                timeout=request_timeout,
            )
            result = response.get("result")
            if result is None:
                raise DonutMcpError(f"MCP Donut không trả kết quả cho {name}.")
            if isinstance(result, dict) and result.get("isError"):
                detail = self._result_text(result) or f"Donut báo lỗi {name}."
                if _is_entitlement_error(detail):
                    raise DonutMcpEntitlementError(
                        "Donut từ chối thao tác do quyền/gói tính năng hiện tại: "
                        + detail[:500]
                    )
                if _is_capacity_error(detail):
                    raise DonutMcpCapacityError(_capacity_error_message(detail))
                if _is_authentication_error(detail):
                    raise DonutMcpAuthenticationError(
                        "MCP Donut từ chối xác thực: " + detail[:500]
                    )
                raise DonutMcpError(detail)
            return result

    def list_tool_names(self) -> frozenset[str]:
        with self._lock:
            if not self._has_live_session():
                self.connect()
            state = self._shared_state
            if self._shared_lease:
                with state.handshake_lock:
                    if state.tools is not None and state.session_id == self.session_id:
                        return state.tools
                    request_id = self._next_request_id()
                    response = self._request(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "tools/list",
                            "params": {},
                        }
                    )
                    result = response.get("result")
                    tools = result.get("tools") if isinstance(result, dict) else None
                    if not isinstance(tools, list):
                        raise DonutMcpError("MCP Donut không trả danh sách công cụ.")
                    state.tools = frozenset(
                        str(item.get("name") or "").strip()
                        for item in tools
                        if isinstance(item, dict) and str(item.get("name") or "").strip()
                    )
                    return state.tools

            request_id = self._next_request_id()
            response = self._request(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/list",
                    "params": {},
                }
            )
            result = response.get("result")
            tools = result.get("tools") if isinstance(result, dict) else None
            if not isinstance(tools, list):
                raise DonutMcpError("MCP Donut không trả danh sách công cụ.")
            return frozenset(
                str(item.get("name") or "").strip()
                for item in tools
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            )

    @staticmethod
    def _result_text(result: dict[str, Any]) -> str:
        content = result.get("content")
        if not isinstance(content, list):
            return ""
        return "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()

    def call_tool_json(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = self.call_tool_value(name, arguments)
        if not isinstance(result, dict) or "content" not in result:
            return result
        text = self._result_text(result)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DonutMcpError(f"Donut trả dữ liệu không đọc được cho {name}.") from exc

    def list_profiles(self) -> tuple[DonutProfile, ...]:
        raw = self.call_tool_json("list_profiles")
        if not isinstance(raw, list):
            raise DonutMcpError("Donut không trả danh sách profile.")
        profiles: list[DonutProfile] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            profile_id = str(item.get("id") or "").strip()
            if not profile_id:
                continue
            try:
                process_id = int(item["process_id"]) if item.get("process_id") else None
            except (TypeError, ValueError):
                process_id = None
            profiles.append(
                DonutProfile(
                    profile_id=profile_id,
                    name=str(item.get("name") or profile_id),
                    browser=str(item.get("browser") or ""),
                    group_id=str(item.get("group_id") or ""),
                    proxy_id=str(item.get("proxy_id") or ""),
                    process_id=process_id,
                    extension_group_id=str(item.get("extension_group_id") or ""),
                )
            )
        return tuple(profiles)

    def list_groups(self) -> tuple[DonutGroup, ...]:
        raw = self.call_tool_json("list_groups")
        if not isinstance(raw, list):
            raise DonutMcpError("Donut không trả danh sách group.")
        output: list[DonutGroup] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            group_id = str(item.get("id") or item.get("group_id") or "").strip()
            if group_id:
                output.append(DonutGroup(group_id, str(item.get("name") or group_id)))
        return tuple(output)

    def get_profile(self, profile_id: str) -> DonutProfile:
        raw = self.call_tool_json("get_profile", {"profile_id": profile_id})
        if not isinstance(raw, dict):
            raise DonutMcpError("Donut không trả thông tin profile đã chọn.")
        try:
            process_id = int(raw["process_id"]) if raw.get("process_id") else None
        except (TypeError, ValueError):
            process_id = None
        return DonutProfile(
            profile_id=str(raw.get("id") or profile_id),
            name=str(raw.get("name") or profile_id),
            browser=str(raw.get("browser") or ""),
            group_id=str(raw.get("group_id") or ""),
            proxy_id=str(raw.get("proxy_id") or ""),
            process_id=process_id,
            extension_group_id=str(raw.get("extension_group_id") or ""),
        )

    def run_profile(
        self,
        profile_id: str,
        url: str,
        *,
        retry_entitlement_after_user_action: bool = False,
    ) -> str:
        # Donut may keep this request open while it starts a profile.  Keep
        # that wait out of the process-wide shared session so a connection
        # test or another workspace can still issue a short metadata call.
        acquired = _PROFILE_LAUNCH_LOCK.acquire(
            timeout=PROFILE_LAUNCH_TIMEOUT_SECONDS + 5.0
        )
        if not acquired:
            raise DonutMcpLaunchUncertain(
                "Donut đang mở profile khác quá lâu; lệnh mở profile mới đã được "
                "dừng an toàn thay vì tạo thêm tiến trình trùng."
            )
        try:
            with _exclusive_runtime_file_lock(
                "donut-profile-launch.lock",
                timeout=PROFILE_LAUNCH_TIMEOUT_SECONDS + 5.0,
            ):
                unresolved = _get_profile_launch_state(self.endpoint, profile_id)
                if unresolved is not None:
                    resolved = self._resolve_uncertain_profile_launch(
                        profile_id,
                        unresolved,
                        retry_entitlement_after_user_action=(
                            retry_entitlement_after_user_action
                        ),
                    )
                    if resolved is not None:
                        return resolved

                if self._use_shared_session:
                    isolated = DonutMcpClient(
                        self.endpoint,
                        timeout=PROFILE_LAUNCH_TIMEOUT_SECONDS,
                        shared_session=False,
                    )
                    with isolated:
                        _set_profile_launch_state(self.endpoint, profile_id, "launching")
                        return self._launch_profile_once(isolated, profile_id, url)
                if not self._has_live_session():
                    self.connect()
                _set_profile_launch_state(self.endpoint, profile_id, "launching")
                return self._launch_profile_once(self, profile_id, url)
        finally:
            _PROFILE_LAUNCH_LOCK.release()

    def _resolve_uncertain_profile_launch(
        self,
        profile_id: str,
        launch_state: dict[str, Any],
        *,
        retry_entitlement_after_user_action: bool = False,
    ) -> str | None:
        """Resolve an old launch, or release a guard from a dead Donut process."""

        prior_state = str(launch_state.get("state") or "unknown")
        if prior_state in {"terminal_entitlement", "terminal_authentication"}:
            # A definitive denial must never become a fresh launch on the next
            # schedule tick. A user can still manually open the same profile
            # after correcting access; live process + CDP proof then releases
            # this guard without touching profile data.
            profile_checked = False
            try:
                profile = self.get_profile(profile_id)
                profile_checked = True
                if self.profile_debug_ready(profile_id, profile=profile):
                    return "already-running"
            except DonutMcpError:
                pass
            if prior_state == "terminal_entitlement":
                if retry_entitlement_after_user_action and profile_checked:
                    # The Upload button is an explicit request to try again
                    # after the user corrects Donut access. Only that action
                    # releases this marker; periodic checkview/scheduled work
                    # still cannot loop on a paid-plan denial. Donut remains
                    # the authority and may reject the same fingerprint again.
                    _clear_profile_launch_state(self.endpoint, profile_id)
                    logger.info(
                        "donut_profile_entitlement_manual_retry "
                        "profile_hash=%s",
                        _profile_launch_state_key(self.endpoint, profile_id)[:12],
                    )
                    return None
                raise DonutMcpEntitlementError(
                    "Donut đã từ chối mở profile vì gói/quyền fingerprint hiện tại. "
                    "Tool không gửi lại lệnh mở; checkpoint vẫn được giữ."
                )
            raise DonutMcpAuthenticationError(
                "Donut đã từ chối xác thực MCP. Tool không gửi lại lệnh mở; "
                "checkpoint vẫn được giữ."
            )

        try:
            profile = self.get_profile(profile_id)
            if not self.profile_debug_ready(profile_id, profile=profile):
                if (
                    prior_state in {"launching", "uncertain", "accepted"}
                    and not _profile_process_alive(profile.process_id)
                ):
                    try:
                        recorded_at = int(launch_state.get("updated_at") or 0)
                    except (TypeError, ValueError):
                        recorded_at = 0
                    proof = _local_donut_restart_proof(self.endpoint, profile_id)
                    if proof is not None and recorded_at > 0:
                        server_started_at, profile_processes = proof
                        restart_proven = server_started_at > recorded_at + 1
                        age_seconds = max(0.0, time.time() - recorded_at)
                        launch_expired = (
                            age_seconds >= PROFILE_LAUNCH_STALE_RETRY_SECONDS
                        )
                        if profile_processes == 0 and (
                            restart_proven or launch_expired
                        ):
                            _clear_profile_launch_state(self.endpoint, profile_id)
                            logger.info(
                                "donut_profile_launch_guard_released "
                                "profile_hash=%s prior_state=%s reason=%s age_seconds=%d",
                                _profile_launch_state_key(self.endpoint, profile_id)[:12],
                                prior_state,
                                "after_restart" if restart_proven else "stale_absence",
                                int(age_seconds),
                            )
                            return None
                raise DonutMcpError("Profile hoặc debug target chưa phản hồi.")
            _clear_profile_launch_state(self.endpoint, profile_id)
        except Exception as exc:
            logger.warning(
                "donut_profile_launch_unresolved profile_hash=%s prior_state=%s error=%s",
                _profile_launch_state_key(self.endpoint, profile_id)[:12],
                prior_state,
                type(exc).__name__,
            )
            raise DonutMcpPriorLaunchUnresolved(
                "Lệnh mở profile Donut trước chưa có kết quả xác nhận. Tool chỉ kiểm tra "
                "trạng thái đọc; không gửi lệnh mở lần nữa. Nếu profile cần mở, hãy mở "
                "đúng profile thủ công trong Donut rồi tiếp tục."
            ) from exc

        logger.info(
            "donut_profile_launch_resolved profile_hash=%s process_running=%s",
            _profile_launch_state_key(self.endpoint, profile_id)[:12],
            profile.running,
        )
        return "already-running"

    @staticmethod
    def _launch_profile_once(
        client: "DonutMcpClient",
        profile_id: str,
        url: str,
    ) -> str:
        try:
            result = client._run_profile_in_current_session(profile_id, url)
        except DonutMcpCapacityError:
            # Wayfern's stderr confirms the child exited before CDP became
            # ready, so this is a definitive temporary refusal, not an
            # uncertain launch. Clear the durable guard so a later user or
            # queued retry can launch after another instance slot is freed.
            _clear_profile_launch_state(client.endpoint, profile_id)
            logger.info(
                "donut_profile_launch_capacity_exhausted profile_hash=%s",
                _profile_launch_state_key(client.endpoint, profile_id)[:12],
            )
            raise
        except (DonutMcpEntitlementError, DonutMcpAuthenticationError) as exc:
            _set_profile_launch_state(
                client.endpoint,
                profile_id,
                "terminal_entitlement"
                if isinstance(exc, DonutMcpEntitlementError)
                else "terminal_authentication",
            )
            logger.warning(
                "donut_profile_launch_refused terminal_access profile_hash=%s",
                _profile_launch_state_key(client.endpoint, profile_id)[:12],
            )
            raise
        except Exception as exc:
            _set_profile_launch_state(client.endpoint, profile_id, "uncertain")
            logger.warning(
                "donut_profile_launch_uncertain profile_hash=%s error=%s",
                _profile_launch_state_key(client.endpoint, profile_id)[:12],
                type(exc).__name__,
            )
            raise DonutMcpLaunchUncertain(
                "Donut chưa xác nhận kết quả lệnh mở profile. Checkpoint được giữ; "
                "tool sẽ chỉ dò trạng thái và không mở profile lần thứ hai."
            ) from exc
        # Keep an accepted marker until a later read-only get_debug_connection
        # confirms this profile has a target endpoint. If Donut acknowledges
        # early or the app restarts during startup, another workspace must not
        # issue a second launch while the first startup is still pending.
        _set_profile_launch_state(client.endpoint, profile_id, "accepted")
        return result

    def _run_profile_in_current_session(self, profile_id: str, url: str) -> str:
        return self._result_text(
            self.call_tool(
                "run_profile",
                {"profile_id": profile_id, "url": url, "headless": False},
                timeout=PROFILE_LAUNCH_TIMEOUT_SECONDS,
            )
        )

    def get_debug_connection(self, profile_id: str) -> dict[str, Any]:
        raw = self.call_tool_json(
            "get_debug_connection",
            {"profile_id": profile_id},
        )
        if not isinstance(raw, dict):
            raise DonutMcpError("Donut không trả kết nối debug cho profile đã chọn.")
        targets_url = str(raw.get("targets_url") or "").strip()
        if not targets_url:
            raise DonutMcpError("Donut chưa cung cấp danh sách tab CDP cho profile đã chọn.")
        try:
            parsed = urllib.parse.urlsplit(targets_url)
        except ValueError as exc:
            raise DonutMcpError("Địa chỉ debug Donut không hợp lệ.") from exc
        if parsed.scheme.casefold() != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise DonutMcpError("Từ chối kết nối debug Donut không nằm trên máy này.")
        # Upload/recovery paths consume this connection directly instead of
        # calling profile_debug_ready(). Once the accepted launch has a live
        # process and a reachable page target, it is no longer uncertain.
        launch_state = _get_profile_launch_state(self.endpoint, profile_id)
        if launch_state and launch_state.get("state") == "accepted":
            profile = self.get_profile(profile_id)
            if (
                profile.running
                and _profile_process_alive(profile.process_id)
                and _debug_target_reachable(targets_url)
            ):
                _clear_profile_launch_state(self.endpoint, profile_id)
                logger.info(
                    "donut_profile_launch_confirmed profile_hash=%s",
                    _profile_launch_state_key(self.endpoint, profile_id)[:12],
                )
        return raw

    def confirm_profile_stopped(
        self, profile_id: str, *, profile: DonutProfile | None = None
    ) -> bool:
        """Release a launch marker only after Donut confirms an explicit stop."""

        profile = profile or self.get_profile(profile_id)
        if profile.profile_id != profile_id or profile.process_id:
            return False
        _clear_profile_launch_state(self.endpoint, profile_id)
        return True

    def profile_debug_ready(
        self, profile_id: str, *, profile: DonutProfile | None = None
    ) -> bool:
        """Require both a live profile process and a reachable page target."""
        profile = profile or self.get_profile(profile_id)
        if not profile.running or not _profile_process_alive(profile.process_id):
            return False
        debug = self.get_debug_connection(profile_id)
        if not _debug_target_reachable(str(debug["targets_url"])):
            return False
        # A successful launch can be acknowledged before Wayfern exposes its
        # first page. Release the durable launch guard only after both signals
        # are confirmed; otherwise a later restart would treat this completed
        # launch as indefinitely uncertain.
        _clear_profile_launch_state(self.endpoint, profile_id)
        return True

    def cdp_command(
        self,
        profile_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw = self.call_tool_json(
            "cdp_command",
            {
                "profile_id": profile_id,
                "method": method,
                "params": params or {},
            },
        )
        if not isinstance(raw, dict):
            raise DonutMcpError(f"Donut không trả kết quả CDP cho {method}.")
        return raw

    def navigate(self, profile_id: str, url: str) -> str:
        return self._result_text(
            self.call_tool("navigate", {"profile_id": profile_id, "url": url})
        )

    def evaluate_javascript(
        self,
        profile_id: str,
        expression: str,
        *,
        await_promise: bool = False,
    ) -> Any:
        raw = self.call_tool_json(
            "evaluate_javascript",
            {
                "profile_id": profile_id,
                "expression": expression,
                "await_promise": await_promise,
                "wait_for_load": False,
            },
        )
        if isinstance(raw, dict) and raw.get("error"):
            raise DonutMcpError(str(raw["error"]))
        return raw.get("value") if isinstance(raw, dict) else raw

    def close(self) -> None:
        if self._shared_lease:
            state = self._shared_state
            with state.handshake_lock:
                if state.active_clients > 0:
                    state.active_clients -= 1
                state.last_used_at = time.monotonic()
            self._shared_lease = False
            self.session_id = ""
            self.server_info = {}
            return
        if not self.session_id:
            return
        request = urllib.request.Request(
            self.endpoint.value,
            headers={
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": self.session_id,
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            },
            method="DELETE",
        )
        try:
            urllib.request.urlopen(request, timeout=3.0).close()
        except (urllib.error.URLError, OSError):
            pass
        self.session_id = ""
        self.server_info = {}

    def __enter__(self) -> "DonutMcpClient":
        self.connect()
        return self

    def __exit__(self, _type: object, _value: object, _tb: object) -> None:
        self.close()


_PROFILE_METADATA_TTL_SECONDS = 20.0
_PROFILE_METADATA_CONDITION = threading.Condition()
_PROFILE_METADATA_CACHE: dict[
    str, tuple[float, tuple[DonutGroup, ...], tuple[DonutProfile, ...]]
] = {}
_PROFILE_METADATA_IN_FLIGHT: set[str] = set()


def load_donut_profile_snapshot(
    endpoint: DonutMcpEndpoint,
    *,
    timeout: float = 35.0,
    max_age: float = _PROFILE_METADATA_TTL_SECONDS,
) -> tuple[tuple[DonutGroup, ...], tuple[DonutProfile, ...]]:
    """Read shared Donut metadata once for all workspace windows.

    Every workspace owns a UI object, but groups/profiles belong to the same
    local Donut installation.  A startup fan-out of twelve identical MCP
    handshakes can starve ``run_profile``.  One caller performs the read and
    the rest consume the short-lived immutable snapshot.
    """

    key = endpoint.value
    deadline = time.monotonic() + max(45.0, float(timeout) + 10.0)
    with _PROFILE_METADATA_CONDITION:
        while True:
            cached = _PROFILE_METADATA_CACHE.get(key)
            if cached is not None and time.monotonic() - cached[0] <= max_age:
                return cached[1], cached[2]
            if key not in _PROFILE_METADATA_IN_FLIGHT:
                _PROFILE_METADATA_IN_FLIGHT.add(key)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DonutMcpError(
                    "Đang có một lượt đọc danh sách profile Donut bị kẹt quá lâu."
                )
            _PROFILE_METADATA_CONDITION.wait(min(remaining, 0.5))

    try:
        with DonutMcpClient(endpoint, timeout=timeout, shared_session=False) as client:
            client.require_docurandom_local()
            groups = client.list_groups()
            profiles = client.list_profiles()
    except Exception:
        with _PROFILE_METADATA_CONDITION:
            _PROFILE_METADATA_IN_FLIGHT.discard(key)
            _PROFILE_METADATA_CONDITION.notify_all()
        raise

    with _PROFILE_METADATA_CONDITION:
        _PROFILE_METADATA_CACHE[key] = (time.monotonic(), groups, profiles)
        _PROFILE_METADATA_IN_FLIGHT.discard(key)
        _PROFILE_METADATA_CONDITION.notify_all()
    return groups, profiles
