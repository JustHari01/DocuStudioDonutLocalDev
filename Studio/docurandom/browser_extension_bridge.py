from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
from pathlib import Path
import re
import secrets
import sys
import threading
import time
import uuid
from typing import Any
from urllib.parse import quote, urlparse

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import Server, ServerConnection, serve

from .storage import app_data_dir, atomic_write_json


logger = logging.getLogger(__name__)


BROWSER_BRIDGE_HOST = "127.0.0.1"
BROWSER_BRIDGE_PORT = 17842
BROWSER_FILE_PORT = 17843
BROWSER_BRIDGE_VERSION = "1.1.0"
BROWSER_EXTENSION_ID = "ghicnfkalhhpecnjhjlmklhlbbohlbeg"
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,96}")
_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")


def bundled_browser_extension_dir() -> Path | None:
    """Return the packaged unpacked-extension folder, if available."""

    roots: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(Path(str(frozen_root)))
    roots.append(Path(__file__).resolve().parents[1])
    for root in roots:
        candidate = root / "donut-extension"
        if (candidate / "manifest.json").is_file():
            return candidate.resolve(strict=False)
    return None


class BrowserExtensionBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserExtensionStatus:
    profile_id: str
    connected: bool
    client_id: str = ""
    extension_version: str = ""
    connected_at: float = 0.0


@dataclass
class _PendingResult:
    event: threading.Event
    value: Any = None
    error: str = ""


@dataclass
class _Connection:
    profile_id: str
    client_id: str
    extension_version: str
    socket: ServerConnection
    connected_at: float


@dataclass(frozen=True)
class _SharedFile:
    path: Path
    expires_at: float


class BrowserExtensionBridge:
    """Authenticated loopback bridge for the DocuRandom Donut extension.

    Pairing is a short, explicit action. The extension receives a random
    per-install secret and stores it inside that Donut profile. The bridge only
    stores a SHA-256 digest, so copying the DocuRandom data file doesn't reveal
    a usable browser-control credential.
    """

    def __init__(
        self,
        *,
        host: str = BROWSER_BRIDGE_HOST,
        port: int = BROWSER_BRIDGE_PORT,
        file_port: int = BROWSER_FILE_PORT,
        pairs_path: Path | None = None,
    ) -> None:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise BrowserExtensionBridgeError("Bridge trình duyệt chỉ được nghe trên máy local.")
        self.host = host
        self.port = int(port)
        self.file_port = int(file_port)
        self.pairs_path = pairs_path or app_data_dir() / "browser-bridge" / "pairs.json"
        self._lock = threading.RLock()
        self._connections: dict[str, _Connection] = {}
        self._pending: dict[str, _PendingResult] = {}
        self._pairs = self._load_pairs()
        self._pair_profile_id = ""
        self._pair_expires_at = 0.0
        self._server: Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._file_server: ThreadingHTTPServer | None = None
        self._file_thread: threading.Thread | None = None
        self._file_ready = threading.Event()
        self._shared_files: dict[str, _SharedFile] = {}

    @staticmethod
    def _digest(secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    def _load_pairs(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self.pairs_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        pairs = raw.get("pairs") if isinstance(raw, dict) else None
        if not isinstance(pairs, dict):
            return {}
        output: dict[str, dict[str, str]] = {}
        for client_id, item in pairs.items():
            if not isinstance(client_id, str) or not isinstance(item, dict):
                continue
            profile_id = str(item.get("profile_id") or "")
            secret_hash = str(item.get("secret_hash") or "")
            if _CLIENT_ID_RE.fullmatch(client_id) and _PROFILE_ID_RE.fullmatch(profile_id):
                if re.fullmatch(r"[a-f0-9]{64}", secret_hash):
                    output[client_id] = {
                        "profile_id": profile_id,
                        "secret_hash": secret_hash,
                        "paired_at": str(item.get("paired_at") or ""),
                    }
        return output

    def _save_pairs(self) -> None:
        atomic_write_json(
            self.pairs_path,
            {"version": 1, "pairs": self._pairs},
        )

    def start(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._serve,
                name="docurandom-browser-bridge",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout):
            raise BrowserExtensionBridgeError("Bridge trình duyệt không khởi động kịp.")

    def _serve(self) -> None:
        try:
            with serve(
                self._handle_socket,
                self.host,
                self.port,
                origins=None,
                compression=None,
                max_size=8 * 1024 * 1024,
                # Chrome extension service workers can pause while a heavy
                # Studio page is materializing. Application-level commands
                # already have bounded timeouts and the extension reconnects,
                # so protocol pings must not tear down a healthy local pair.
                ping_interval=None,
            ) as server:
                with self._lock:
                    self._server = server
                    self.port = int(server.socket.getsockname()[1])
                self._ready.set()
                logger.info("browser_extension_bridge_started port=%s", self.port)
                server.serve_forever()
        except OSError as exc:
            logger.error("browser_extension_bridge_start_failed error=%s", exc)
            self._ready.set()
        finally:
            with self._lock:
                self._server = None
                self._connections.clear()

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            file_server = self._file_server
            file_thread = self._file_thread
        if server is not None:
            server.shutdown()
        if file_server is not None:
            file_server.shutdown()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        if file_thread is not None and file_thread is not threading.current_thread():
            file_thread.join(timeout=3)
        with self._lock:
            self._thread = None
            self._file_thread = None
            self._file_server = None
            self._shared_files.clear()
            for pending in self._pending.values():
                pending.error = "Bridge trình duyệt đã dừng."
                pending.event.set()
            self._pending.clear()

    def _start_file_server(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            if self._file_thread is not None and self._file_thread.is_alive():
                return
            self._file_ready.clear()
            self._file_thread = threading.Thread(
                target=self._serve_files,
                name="docurandom-browser-file-bridge",
                daemon=True,
            )
            self._file_thread.start()
        if not self._file_ready.wait(timeout):
            raise BrowserExtensionBridgeError("Máy chủ file local không khởi động kịp.")
        with self._lock:
            if self._file_server is None:
                raise BrowserExtensionBridgeError("Máy chủ file local không thể khởi động.")

    def _serve_files(self) -> None:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                match = re.fullmatch(r"/file/([A-Za-z0-9_-]{20,160})(?:/.*)?", parsed.path)
                if match is None:
                    self.send_error(404)
                    return
                token = match.group(1)
                with bridge._lock:
                    shared = bridge._shared_files.get(token)
                    if shared is not None and time.monotonic() > shared.expires_at:
                        bridge._shared_files.pop(token, None)
                        shared = None
                if shared is None or not shared.path.is_file():
                    self.send_error(404)
                    return
                try:
                    size = shared.path.stat().st_size
                    mime = mimetypes.guess_type(shared.path.name)[0] or "application/octet-stream"
                    self.send_response(200)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
                    with shared.path.open("rb") as source:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except (OSError, BrokenPipeError, ConnectionResetError):
                    return

        try:
            server = ThreadingHTTPServer((self.host, self.file_port), Handler)
            with self._lock:
                self._file_server = server
                self.file_port = int(server.server_address[1])
            self._file_ready.set()
            logger.info("browser_extension_file_bridge_started port=%s", self.file_port)
            server.serve_forever(poll_interval=0.2)
        except OSError as exc:
            logger.error("browser_extension_file_bridge_start_failed error=%s", exc)
            self._file_ready.set()
        finally:
            with self._lock:
                self._file_server = None

    def share_file(self, path: Path, *, ttl: float = 60 * 60) -> str:
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise BrowserExtensionBridgeError("File cần gửi vào trình duyệt không tồn tại.")
        self.start()
        self._start_file_server()
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._shared_files[token] = _SharedFile(
                path=resolved,
                expires_at=time.monotonic() + max(60.0, min(float(ttl), 6 * 60 * 60)),
            )
        return f"http://127.0.0.1:{self.file_port}/file/{token}/{quote(resolved.name)}"

    def begin_pairing(self, profile_id: str, *, ttl: float = 120.0) -> None:
        profile_id = str(profile_id or "").strip()
        if not _PROFILE_ID_RE.fullmatch(profile_id):
            raise BrowserExtensionBridgeError("Profile ID Donut không hợp lệ.")
        self.start()
        with self._lock:
            self._pair_profile_id = profile_id
            self._pair_expires_at = time.monotonic() + max(15.0, min(float(ttl), 300.0))
        logger.info("browser_extension_pairing_started profile_id=%s", profile_id)

    def cancel_pairing(self) -> None:
        with self._lock:
            self._pair_profile_id = ""
            self._pair_expires_at = 0.0

    def _send_json(self, socket: ServerConnection, payload: dict[str, Any]) -> None:
        socket.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def _read_json(self, raw: str | bytes) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BrowserExtensionBridgeError("Extension gửi JSON không hợp lệ.") from exc
        if not isinstance(value, dict):
            raise BrowserExtensionBridgeError("Extension gửi sai định dạng.")
        return value

    def _pair(self, socket: ServerConnection, hello: dict[str, Any]) -> None:
        """Complete a short pairing handshake without becoming a live client.

        The pairing socket is intentionally separate from the persistent
        authenticated socket opened immediately afterwards by the extension.
        Registering this temporary socket in ``_connections`` made its close
        event evict an otherwise healthy profile connection, which produced a
        false connect/disconnect flap during every re-pair.
        """
        client_id = str(hello.get("client_id") or "")
        if not _CLIENT_ID_RE.fullmatch(client_id):
            raise BrowserExtensionBridgeError("Extension chưa có client ID hợp lệ.")
        with self._lock:
            if not self._pair_profile_id or time.monotonic() > self._pair_expires_at:
                self._pair_profile_id = ""
                self._pair_expires_at = 0.0
                raise BrowserExtensionBridgeError(
                    "Tool chưa bật chế độ ghép profile hoặc phiên ghép đã hết hạn."
                )
            profile_id = self._pair_profile_id
            self._pair_profile_id = ""
            self._pair_expires_at = 0.0
            secret = secrets.token_urlsafe(32)
            self._pairs[client_id] = {
                "profile_id": profile_id,
                "secret_hash": self._digest(secret),
                "paired_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_pairs()
        self._send_json(
            socket,
            {
                "type": "paired",
                "profile_id": profile_id,
                "secret": secret,
                "bridge_version": BROWSER_BRIDGE_VERSION,
            },
        )

    def _authenticate(self, socket: ServerConnection, hello: dict[str, Any]) -> _Connection:
        client_id = str(hello.get("client_id") or "")
        profile_id = str(hello.get("profile_id") or "")
        secret = str(hello.get("secret") or "")
        if not _CLIENT_ID_RE.fullmatch(client_id) or not _PROFILE_ID_RE.fullmatch(profile_id):
            raise BrowserExtensionBridgeError("Danh tính extension không hợp lệ.")
        with self._lock:
            pair = self._pairs.get(client_id)
        if (
            pair is None
            or pair.get("profile_id") != profile_id
            or not secrets.compare_digest(pair.get("secret_hash", ""), self._digest(secret))
        ):
            raise BrowserExtensionBridgeError("Extension chưa được ghép hoặc khóa ghép không đúng.")
        self._send_json(
            socket,
            {
                "type": "hello_ok",
                "profile_id": profile_id,
                "bridge_version": BROWSER_BRIDGE_VERSION,
            },
        )
        return _Connection(
            profile_id=profile_id,
            client_id=client_id,
            extension_version=str(hello.get("extension_version") or ""),
            socket=socket,
            connected_at=time.time(),
        )

    def _handle_result(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("id") or "")
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return
        if bool(message.get("ok")):
            pending.value = message.get("result")
        else:
            pending.error = str(message.get("error") or "Extension từ chối lệnh.")
        pending.event.set()

    def _handle_socket(self, socket: ServerConnection) -> None:
        connection: _Connection | None = None
        try:
            first = self._read_json(socket.recv(timeout=10))
            message_type = str(first.get("type") or "")
            if message_type == "pair":
                # A pair socket only receives the newly minted secret.  The
                # extension closes it and opens a separate authenticated live
                # socket; do not publish this short-lived socket as a client.
                self._pair(socket, first)
                return
            elif message_type == "hello":
                connection = self._authenticate(socket, first)
            else:
                raise BrowserExtensionBridgeError("Extension chưa gửi hello/pair.")
            with self._lock:
                previous = self._connections.get(connection.profile_id)
                self._connections[connection.profile_id] = connection
            if previous is not None and previous.socket is not socket:
                try:
                    previous.socket.close(code=4001, reason="Profile reconnected")
                except (ConnectionClosed, OSError):
                    pass
            logger.info(
                "browser_extension_connected profile_id=%s version=%s",
                connection.profile_id,
                connection.extension_version,
            )
            for raw in socket:
                message = self._read_json(raw)
                if message.get("type") == "result":
                    self._handle_result(message)
        except (BrowserExtensionBridgeError, ConnectionClosed, TimeoutError, OSError) as exc:
            try:
                self._send_json(socket, {"type": "error", "error": str(exc)})
            except (ConnectionClosed, OSError):
                pass
        finally:
            if connection is not None:
                with self._lock:
                    if self._connections.get(connection.profile_id) is connection:
                        self._connections.pop(connection.profile_id, None)
                logger.info("browser_extension_disconnected profile_id=%s", connection.profile_id)

    def status(self, profile_id: str) -> BrowserExtensionStatus:
        with self._lock:
            connection = self._connections.get(profile_id)
        if connection is None:
            return BrowserExtensionStatus(profile_id=profile_id, connected=False)
        return BrowserExtensionStatus(
            profile_id=profile_id,
            connected=True,
            client_id=connection.client_id,
            extension_version=connection.extension_version,
            connected_at=connection.connected_at,
        )

    def wait_for_profile(self, profile_id: str, *, timeout: float = 120.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self.status(profile_id).connected:
                return True
            time.sleep(0.1)
        return self.status(profile_id).connected

    def command(
        self,
        profile_id: str,
        command: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        with self._lock:
            connection = self._connections.get(profile_id)
        if connection is None:
            raise BrowserExtensionBridgeError(
                "Extension chưa kết nối với profile Donut đã chọn. Hãy mở profile rồi ghép extension."
            )
        request_id = uuid.uuid4().hex
        pending = _PendingResult(threading.Event())
        with self._lock:
            self._pending[request_id] = pending
        try:
            self._send_json(
                connection.socket,
                {
                    "type": "command",
                    "id": request_id,
                    "command": str(command),
                    "params": params or {},
                },
            )
            if not pending.event.wait(max(1.0, float(timeout))):
                raise BrowserExtensionBridgeError(
                    f"Extension không trả lời lệnh {command} trước thời hạn."
                )
            if pending.error:
                raise BrowserExtensionBridgeError(pending.error)
            return pending.value
        except OSError as exc:
            raise BrowserExtensionBridgeError("Kết nối extension đã bị ngắt.") from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)


_DEFAULT_BRIDGE: BrowserExtensionBridge | None = None
_DEFAULT_LOCK = threading.Lock()


def get_browser_extension_bridge() -> BrowserExtensionBridge:
    global _DEFAULT_BRIDGE
    with _DEFAULT_LOCK:
        if _DEFAULT_BRIDGE is None:
            _DEFAULT_BRIDGE = BrowserExtensionBridge()
        bridge = _DEFAULT_BRIDGE
    bridge.start()
    return bridge
