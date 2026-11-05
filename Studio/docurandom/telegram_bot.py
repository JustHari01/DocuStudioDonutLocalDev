from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass, replace
import html
import json
import logging
import os
from pathlib import Path
import queue
import re
import secrets
import threading
import time
from typing import Callable
from urllib import error, parse, request
import uuid

from .storage import app_data_dir


_TOKEN_PATTERN = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$")
_SECRET_FILENAME = "telegram-bot.bin"
_MAX_MESSAGE_LENGTH = 3900
MAX_TELEGRAM_DOWNLOAD_BYTES = 20 * 1024 * 1024
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_LOGGER = logging.getLogger(__name__)


BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("menu", "Mở các nút điều khiển nhanh"),
    ("hide_menu", "Ẩn các nút điều khiển nhanh"),
    ("status", "Xem trạng thái theo số/tên workspace"),
    ("workspaces", "Xem danh sách workspace"),
    ("run", "Chạy toàn bộ workflow"),
    ("continue", "Tiếp tục từ checkpoint"),
    ("pause", "Tạm dừng workflow an toàn"),
    ("cancel", "Hủy tác vụ đang chạy"),
    ("checkview", "Bật/tắt theo dõi view theo workspace"),
    ("flow_start", "Khởi động và kiểm tra Flow"),
    ("flow_restart", "Khởi động lại tiện ích Flow"),
    ("flow_status", "Xem trạng thái Flow"),
    ("voicevox_start", "Khởi động VOICEVOX"),
    ("voicevox_status", "Xem trạng thái VOICEVOX"),
    ("schedule", "Xem lịch của workspace"),
    ("schedule_workflow", "Đặt giờ workflow theo giờ Việt Nam"),
    ("schedule_upload", "Đặt giờ upload theo giờ Việt Nam"),
    ("schedule_start", "Bắt đầu canh lịch đã đặt"),
    ("schedule_cancel", "Hủy canh lịch, giữ nguyên giờ"),
    ("thumbnail", "Hướng dẫn gửi ảnh thumbnail"),
    ("thumbnail_prompt", "Lấy prompt thumbnail trong file Tổng hợp"),
    ("thumbnail_current", "Xem thumbnail đang chọn"),
    ("thumbnail_delete", "Bỏ thumbnail hiện tại có xác nhận"),
    ("logs", "Xem 25 dòng log gần nhất"),
    ("errors", "Xem lỗi gần nhất của workspace"),
    ("upload", "Yêu cầu upload YouTube có xác nhận"),
    ("help", "Xem hướng dẫn đầy đủ"),
)


class TelegramBotError(RuntimeError):
    pass


class TelegramPollingConflict(TelegramBotError):
    """Another process is already receiving commands for the same bot."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


@dataclass(frozen=True)
class TelegramBotSettings:
    enabled: bool = False
    token: str = ""
    chat_id: int = 0
    bot_username: str = ""
    notify_prompts: bool = False
    notify_errors: bool = True
    notify_status: bool = True


@dataclass(frozen=True)
class TelegramBotState:
    status: str = "disabled"
    detail: str = "Telegram chưa bật."
    bot_username: str = ""
    paired_chat_id: int = 0


@dataclass(frozen=True)
class TelegramCommand:
    chat_id: int
    name: str
    args: str
    raw_text: str
    attachment: TelegramAttachment | None = None
    expected_thread_id: str = ""


@dataclass(frozen=True)
class TelegramAttachment:
    filename: str
    mime_type: str
    data: bytes
    file_unique_id: str = ""


@dataclass(frozen=True)
class _OutboundPhoto:
    data: bytes
    filename: str
    caption: str


def validate_bot_token(value: str) -> str:
    token = str(value or "").strip()
    if not _TOKEN_PATTERN.fullmatch(token):
        raise TelegramBotError("Token bot Telegram chưa đúng định dạng BotFather.")
    return token


def _secret_path(path: Path | None = None) -> Path:
    return path or app_data_dir() / "secrets" / _SECRET_FILENAME


def _protect_bytes(value: bytes) -> bytes:
    if os.name != "nt":
        raise TelegramBotError("Kho token Telegram hiện chỉ hỗ trợ Windows.")
    buffer = ctypes.create_string_buffer(value)
    source = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "DocuRandom Telegram Bot",
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
        raise TelegramBotError("Kho token Telegram hiện chỉ hỗ trợ Windows.")
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


def save_telegram_settings(
    settings: TelegramBotSettings,
    path: Path | None = None,
) -> None:
    token = validate_bot_token(settings.token) if settings.token else ""
    normalized = replace(
        settings,
        token=token,
        chat_id=max(0, int(settings.chat_id)),
        bot_username=str(settings.bot_username or "").lstrip("@")[:64],
    )
    target = _secret_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        payload = json.dumps(asdict(normalized), ensure_ascii=False).encode("utf-8")
        temporary.write_bytes(_protect_bytes(payload))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def load_telegram_settings(path: Path | None = None) -> TelegramBotSettings:
    target = _secret_path(path)
    try:
        encrypted = target.read_bytes()
        payload = json.loads(_unprotect_bytes(encrypted).decode("utf-8"))
        settings = TelegramBotSettings(
            enabled=bool(payload.get("enabled", False)),
            token=str(payload.get("token", "")),
            chat_id=max(0, int(payload.get("chat_id", 0))),
            bot_username=str(payload.get("bot_username", "")).lstrip("@")[:64],
            notify_prompts=False,
            notify_errors=bool(payload.get("notify_errors", True)),
            notify_status=bool(payload.get("notify_status", True)),
        )
        if settings.token:
            validate_bot_token(settings.token)
        return settings
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError, TelegramBotError) as exc:
        _LOGGER.warning(
            "Telegram settings unavailable path=%s error_type=%s",
            target,
            type(exc).__name__,
        )
        return TelegramBotSettings()


def clear_telegram_settings(path: Path | None = None) -> None:
    try:
        _secret_path(path).unlink(missing_ok=True)
    except OSError as exc:
        raise TelegramBotError(f"Không xóa được token Telegram đã lưu: {exc}") from exc


class TelegramBotClient:
    def __init__(self, token: str, *, timeout: float = 12.0) -> None:
        self.token = validate_bot_token(token)
        self.timeout = max(2.0, float(timeout))

    def call(self, method: str, payload: dict[str, object] | None = None) -> object:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        api_request = request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with request.urlopen(api_request, timeout=self.timeout) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("description", "")
            except Exception:
                detail = ""
            clean = str(detail or f"Telegram HTTP {exc.code}").replace(
                self.token,
                "<token>",
            )
            if method == "getUpdates" and exc.code == 409:
                raise TelegramPollingConflict(clean) from None
            raise TelegramBotError(clean) from None
        except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            message = str(exc).replace(self.token, "<token>")
            raise TelegramBotError(f"Không gọi được Telegram: {message}") from None
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            detail = str(parsed.get("description", "Telegram trả về lỗi.")) if isinstance(parsed, dict) else "Telegram trả về dữ liệu không hợp lệ."
            raise TelegramBotError(detail.replace(self.token, "<token>"))
        return parsed.get("result")

    def get_me(self) -> dict[str, object]:
        result = self.call("getMe")
        if not isinstance(result, dict):
            raise TelegramBotError("Telegram không trả về thông tin bot hợp lệ.")
        return result

    def set_my_commands(self) -> None:
        self.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": command, "description": description}
                    for command, description in BOT_COMMANDS
                ],
                "scope": {"type": "all_private_chats"},
            },
        )

    def get_updates(
        self,
        *,
        offset: int | None,
        timeout: int,
    ) -> list[dict[str, object]]:
        payload: dict[str, object] = {
            "timeout": max(0, int(timeout)),
            "limit": 50,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = int(offset)
        result = self.call("getUpdates", payload)
        if not isinstance(result, list):
            raise TelegramBotError("Telegram không trả về danh sách lệnh hợp lệ.")
        return [item for item in result if isinstance(item, dict)]

    def get_file(self, file_id: str) -> dict[str, object]:
        result = self.call("getFile", {"file_id": str(file_id)})
        if not isinstance(result, dict) or not str(result.get("file_path", "")).strip():
            raise TelegramBotError("Telegram không trả về đường dẫn ảnh hợp lệ.")
        return result

    def download_file(
        self,
        file_id: str,
        *,
        limit: int = MAX_TELEGRAM_DOWNLOAD_BYTES,
    ) -> bytes:
        metadata = self.get_file(file_id)
        try:
            declared_size = int(metadata.get("file_size", 0) or 0)
        except (TypeError, ValueError):
            declared_size = 0
        if declared_size > limit:
            raise TelegramBotError("Ảnh Telegram vượt giới hạn tải 20 MB.")
        file_path = str(metadata.get("file_path", "")).strip().lstrip("/")
        safe_path = "/".join(parse.quote(part, safe="") for part in file_path.split("/"))
        file_request = request.Request(
            f"https://api.telegram.org/file/bot{self.token}/{safe_path}",
            method="GET",
        )
        try:
            with request.urlopen(file_request, timeout=self.timeout) as response:
                payload = response.read(limit + 1)
        except (error.HTTPError, error.URLError, TimeoutError, OSError) as exc:
            clean = str(exc).replace(self.token, "<token>")
            raise TelegramBotError(f"Không tải được ảnh từ Telegram: {clean}") from None
        if len(payload) > limit:
            raise TelegramBotError("Ảnh Telegram vượt giới hạn tải 20 MB.")
        return payload

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str = "",
    ) -> None:
        chunks = split_telegram_message(text)
        for index, chunk in enumerate(chunks):
            payload: dict[str, object] = {
                "chat_id": int(chat_id),
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = str(parse_mode)
            if index == len(chunks) - 1 and reply_markup is not None:
                payload["reply_markup"] = reply_markup
            self.call(
                "sendMessage",
                payload,
            )

    def send_photo(
        self,
        chat_id: int,
        data: bytes,
        *,
        filename: str = "thumbnail-preview.jpg",
        caption: str = "",
    ) -> None:
        payload = bytes(data)
        if not payload:
            raise TelegramBotError("Ảnh xem trước Telegram đang trống.")
        if len(payload) > MAX_TELEGRAM_DOWNLOAD_BYTES:
            raise TelegramBotError("Ảnh xem trước Telegram vượt giới hạn an toàn 20 MB.")
        safe_name = Path(str(filename)).name or "thumbnail-preview.jpg"
        boundary = f"DocuRandom-{uuid.uuid4().hex}"
        chunks: list[bytes] = []

        def add_field(name: str, value: str) -> None:
            chunks.extend(
                (
                    f"--{boundary}\r\n".encode("ascii"),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                    value.encode("utf-8"),
                    b"\r\n",
                )
            )

        add_field("chat_id", str(int(chat_id)))
        if caption.strip():
            add_field("caption", caption.strip()[:1000])
        chunks.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                (
                    'Content-Disposition: form-data; name="photo"; '
                    f'filename="{safe_name.replace(chr(34), "")}"\r\n'
                ).encode("utf-8"),
                b"Content-Type: image/jpeg\r\n\r\n",
                payload,
                b"\r\n",
                f"--{boundary}--\r\n".encode("ascii"),
            )
        )
        body = b"".join(chunks)
        api_request = request.Request(
            f"https://api.telegram.org/bot{self.token}/sendPhoto",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        try:
            with request.urlopen(api_request, timeout=max(self.timeout, 30.0)) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("description", "")
            except Exception:
                detail = ""
            raise TelegramBotError(
                str(detail or f"Telegram HTTP {exc.code}").replace(self.token, "<token>")
            ) from None
        except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise TelegramBotError(
                f"Không gửi được ảnh xem trước Telegram: {str(exc).replace(self.token, '<token>')}"
            ) from None
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            detail = str(parsed.get("description", "Telegram trả về lỗi.")) if isinstance(parsed, dict) else "Telegram trả về dữ liệu không hợp lệ."
            raise TelegramBotError(detail.replace(self.token, "<token>"))


def telegram_main_keyboard() -> dict[str, object]:
    return {
        "keyboard": [
            [{"text": "/status"}, {"text": "/workspaces"}],
            [{"text": "/run"}, {"text": "/continue"}],
            [{"text": "/pause"}, {"text": "/cancel"}],
            [{"text": "/flow_start"}, {"text": "/flow_restart"}],
            [{"text": "/flow_status"}, {"text": "/voicevox_status"}],
            [{"text": "/schedule"}, {"text": "/thumbnail"}],
            [{"text": "/thumbnail_prompt"}, {"text": "/errors"}],
            [{"text": "/logs"}],
            [{"text": "/help"}, {"text": "/hide_menu"}],
        ],
        "resize_keyboard": True,
        "is_persistent": False,
        "input_field_placeholder": "Chọn lệnh DocuRandom…",
    }


def telegram_remove_keyboard() -> dict[str, object]:
    """Tell Telegram clients to remove the custom reply keyboard for this chat."""

    return {"remove_keyboard": True}


def split_telegram_message(text: str) -> list[str]:
    remaining = str(text or "").strip()
    if not remaining:
        return []
    chunks: list[str] = []
    while len(remaining) > _MAX_MESSAGE_LENGTH:
        cut = remaining.rfind("\n", 0, _MAX_MESSAGE_LENGTH)
        if cut < _MAX_MESSAGE_LENGTH // 2:
            cut = _MAX_MESSAGE_LENGTH
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def format_telegram_notification(category: str, message: str) -> str:
    """Render one compact, safely escaped HTML notification."""

    raw = str(message or "").strip()[:3200]
    workspace = "DocuRandom"
    match = re.match(r"^\[([^\]\r\n]{1,160})\]\s*(.*)$", raw, flags=re.DOTALL)
    if match is not None:
        workspace = match.group(1).strip() or workspace
        raw = match.group(2).strip()
    normalized = raw.casefold()
    if category == "error":
        icon, heading = "⚠️", "CẢNH BÁO"
    elif category == "test":
        icon, heading = "🧪", "KẾT NỐI TELEGRAM"
    elif re.search(r"^công đoạn:\s*(?:tạo\s+)?thumbnail\b", normalized, flags=re.MULTILINE):
        icon, heading = "🖼️", "THUMBNAIL"
    elif "youtube like" in normalized or " like" in normalized:
        icon, heading = "👍", "YOUTUBE LIKE"
    elif "upload" in normalized or "youtube" in normalized:
        icon, heading = "📤", "YOUTUBE"
    elif "workflow" in normalized:
        icon, heading = "⚙️", "WORKFLOW"
    elif "voicevox" in normalized:
        icon, heading = "🎤", "VOICEVOX"
    elif "flow" in normalized or "tạo ảnh" in normalized:
        icon, heading = "🖼️", "FLOW"
    elif "hẹn giờ" in normalized or "lịch" in normalized:
        icon, heading = "⏰", "LỊCH TỰ ĐỘNG"
    else:
        icon, heading = "ℹ️", "TRẠNG THÁI"

    labels = {
        "công đoạn",
        "ca",
        "chi tiết",
        "xử lý",
        "video",
        "kênh",
        "link",
        "đơn",
        "gói",
        "lượt xem 60 phút",
        "view trong 60 phút",
        "tỷ lệ like",
        "like đã đặt",
        "số lượng",
        "trạng thái",
        "kết quả",
        "gói rẻ nhất",
        "mã đơn",
    }
    rendered: list[str] = []
    for source_line in raw.splitlines():
        line = source_line.strip()
        if not line:
            if rendered and rendered[-1] != "":
                rendered.append("")
            continue
        if line.startswith("🔗 "):
            rendered.append("<b>🔗 Link:</b>")
            rendered.append(f"<pre>{html.escape(line[2:].strip())}</pre>")
            continue
        key, separator, value = line.partition(":")
        normalized_key = re.sub(r"^[^\w]+", "", key.strip()).strip().casefold()
        recognized_label = normalized_key in labels or normalized_key.startswith(
            "video "
        )
        if separator and recognized_label:
            escaped_key = html.escape(key.strip())
            escaped_value = html.escape(value.strip())
            if normalized_key == "chi tiết":
                rendered.append(f"<b>{escaped_key}:</b>")
                rendered.append(f"<blockquote>{escaped_value}</blockquote>")
            elif normalized_key in {"link", "kết quả"}:
                rendered.append(f"<b>{escaped_key}:</b>")
                rendered.append(f"<pre>{escaped_value}</pre>")
            elif normalized_key in {
                "ca",
                "đơn",
                "mã đơn",
                "gói",
                "gói rẻ nhất",
                "số lượng",
            }:
                rendered.append(f"<b>{escaped_key}:</b> <code>{escaped_value}</code>")
            else:
                rendered.append(f"<b>{escaped_key}:</b> {escaped_value}")
        else:
            rendered.append(html.escape(line))
    body = "\n".join(rendered).strip() or "Không có chi tiết."
    result = (
        f"{icon} <b>{heading}</b>\n"
        f"🏷 <b>Workspace:</b> <code>{html.escape(workspace)}</code>\n"
        "━━━━━━━━━━━━\n"
        f"{body}"
    )
    if len(result) <= _MAX_MESSAGE_LENGTH:
        return result
    # Keep tags intact: Telegram HTML cannot be split halfway through an
    # entity or formatting block.
    compact = html.escape(raw[:500]) or "Không có chi tiết."
    return (
        f"{icon} <b>{heading}</b>\n"
        f"🏷 <b>Workspace:</b> <code>{html.escape(workspace[:160])}</code>\n"
        "━━━━━━━━━━━━\n"
        f"<blockquote>{compact}</blockquote>"
    )


class TelegramBotService:
    def __init__(
        self,
        command_handler: Callable[[TelegramCommand], str],
        *,
        settings_path: Path | None = None,
    ) -> None:
        self.command_handler = command_handler
        self.settings_path = settings_path
        self._lock = threading.RLock()
        self._settings = load_telegram_settings(settings_path)
        self._state = TelegramBotState(
            status="starting" if self._settings.enabled and self._settings.token else "disabled",
            detail=(
                "Đang kết nối Telegram…"
                if self._settings.enabled and self._settings.token
                else "Telegram chưa bật."
            ),
            bot_username=self._settings.bot_username,
            paired_chat_id=self._settings.chat_id,
        )
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._outbound: queue.Queue[tuple[str, str] | _OutboundPhoto] = queue.Queue()
        self._pairing_code = ""
        self._pairing_expires_at = 0.0
        self._last_notifications: dict[str, float] = {}
        self._announced_connection: tuple[str, int, str] | None = None
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="DocuRandom-Telegram",
        )
        self._thread.start()

    def settings(self) -> TelegramBotSettings:
        with self._lock:
            return self._settings

    def state(self) -> TelegramBotState:
        with self._lock:
            return self._state

    def _set_state(self, status: str, detail: str) -> None:
        with self._lock:
            previous = self._state
            self._state = TelegramBotState(
                status=status,
                detail=str(detail),
                bot_username=self._settings.bot_username,
                paired_chat_id=self._settings.chat_id,
            )
            current = self._state
        if current == previous:
            return
        log = _LOGGER.warning if status in {"error", "send_only"} else _LOGGER.info
        log(
            "Telegram state status=%s paired=%s detail=%s",
            status,
            bool(current.paired_chat_id),
            detail,
        )

    def configure(
        self,
        *,
        token: str,
        enabled: bool,
        notify_prompts: bool,
        notify_errors: bool,
        notify_status: bool,
    ) -> TelegramBotSettings:
        normalized_token = validate_bot_token(token)
        client = TelegramBotClient(normalized_token)
        identity = client.get_me()
        username = str(identity.get("username", "")).lstrip("@")
        if not username:
            raise TelegramBotError("Bot Telegram không có username hợp lệ.")
        with self._lock:
            chat_id = self._settings.chat_id if normalized_token == self._settings.token else 0
            self._settings = TelegramBotSettings(
                enabled=bool(enabled),
                token=normalized_token,
                chat_id=chat_id,
                bot_username=username,
                notify_prompts=False,
                notify_errors=bool(notify_errors),
                notify_status=bool(notify_status),
            )
            save_telegram_settings(self._settings, self.settings_path)
        self._set_state("starting" if enabled else "disabled", "Đang kết nối Telegram…" if enabled else "Telegram đã lưu nhưng đang tắt.")
        self._wake_event.set()
        return self.settings()

    def begin_pairing(self, *, ttl_seconds: int = 300) -> str:
        with self._lock:
            if not self._settings.enabled or not self._settings.token:
                raise TelegramBotError("Hãy lưu và bật bot trước khi ghép cặp.")
            self._pairing_code = f"{secrets.randbelow(1_000_000):06d}"
            self._pairing_expires_at = time.monotonic() + max(60, int(ttl_seconds))
            return self._pairing_code

    def notify(self, category: str, message: str) -> None:
        if str(category).casefold() == "prompt":
            return
        clean = str(message or "").strip()
        if not clean:
            return
        settings = self.settings()
        allowed = {
            "prompt": settings.notify_prompts,
            "error": settings.notify_errors,
            "status": settings.notify_status,
        }.get(str(category), True)
        if not settings.enabled or not settings.chat_id or not allowed:
            return
        signature = f"{category}:{clean}"
        now = time.monotonic()
        with self._lock:
            previous = self._last_notifications.get(signature, 0.0)
            if now - previous < 30.0:
                return
            self._last_notifications[signature] = now
            if len(self._last_notifications) > 200:
                threshold = now - 300.0
                self._last_notifications = {
                    key: value
                    for key, value in self._last_notifications.items()
                    if value >= threshold
                }
        self._outbound.put((str(category), clean))
        self._wake_event.set()

    def send_test(self) -> None:
        settings = self.settings()
        if not settings.chat_id:
            raise TelegramBotError("Bot chưa ghép cặp với chat riêng.")
        self._outbound.put(("test", "DocuRandom Studio · kết nối Telegram hoạt động."))
        self._wake_event.set()

    def send_photo(self, data: bytes, *, caption: str, filename: str = "thumbnail-preview.jpg") -> None:
        settings = self.settings()
        if not settings.enabled or not settings.chat_id:
            return
        payload = bytes(data)
        if not payload:
            return
        self._outbound.put(
            _OutboundPhoto(
                data=payload,
                filename=Path(str(filename)).name or "thumbnail-preview.jpg",
                caption=str(caption or "").strip(),
            )
        )
        self._wake_event.set()

    def disable(self, *, clear_token: bool) -> None:
        with self._lock:
            if clear_token:
                self._settings = TelegramBotSettings()
                clear_telegram_settings(self.settings_path)
            else:
                self._settings = replace(self._settings, enabled=False)
                save_telegram_settings(self._settings, self.settings_path)
        self._set_state("disabled", "Telegram đã tắt.")
        self._wake_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def _announce_connection(
        self,
        client: TelegramBotClient,
        *,
        send_only: bool,
    ) -> None:
        current = self.settings()
        mode = "send-only" if send_only else "full"
        announcement_key = (current.bot_username, current.chat_id, mode)
        if (
            not current.chat_id
            or not current.notify_status
            or self._announced_connection == announcement_key
        ):
            return
        detail = (
            "DocuRandom Studio đã kết nối Telegram ở chế độ chỉ gửi thông báo; "
            "một máy khác đang nhận lệnh của bot."
            if send_only
            else "DocuRandom Studio đã kết nối Telegram."
        )
        client.send_message(
            current.chat_id,
            format_telegram_notification("status", f"[Toàn tool] {detail}"),
            parse_mode="HTML",
        )
        self._announced_connection = announcement_key

    def _run(self) -> None:
        active_token = ""
        offset: int | None = None
        client: TelegramBotClient | None = None
        while not self._stop_event.is_set():
            settings = self.settings()
            if not settings.enabled or not settings.token:
                active_token = ""
                offset = None
                client = None
                self._set_state("disabled", "Telegram chưa bật.")
                self._wake_event.wait(1.0)
                self._wake_event.clear()
                continue
            try:
                if client is None or active_token != settings.token:
                    client = TelegramBotClient(settings.token, timeout=12.0)
                    identity = client.get_me()
                    client.set_my_commands()
                    username = str(identity.get("username", "")).lstrip("@")
                    with self._lock:
                        self._settings = replace(self._settings, bot_username=username)
                        save_telegram_settings(self._settings, self.settings_path)
                    # Discard commands left before this local session. This prevents
                    # a stale /run or /upload from being replayed after restart.
                    try:
                        pending = client.get_updates(offset=-1, timeout=0)
                    except TelegramPollingConflict:
                        # Telegram permits only one getUpdates consumer per bot.
                        # Keep this machine useful: outbound status/error/photo
                        # notifications can still be sent while another machine
                        # owns remote commands.
                        pending = []
                        self._set_state(
                            "send_only",
                            "Bot đang được một DocuRandom khác nhận lệnh; "
                            "máy này vẫn gửi thông báo Telegram.",
                        )
                        self._announce_connection(client, send_only=True)
                    offset = (
                        max(int(item.get("update_id", 0)) for item in pending) + 1
                        if pending
                        else None
                    )
                    active_token = settings.token
                self._drain_outbound(client)
                try:
                    updates = client.get_updates(offset=offset, timeout=5)
                except TelegramPollingConflict:
                    self._set_state(
                        "send_only",
                        "Bot đang được một DocuRandom khác nhận lệnh; "
                        "máy này vẫn gửi thông báo Telegram.",
                    )
                    self._announce_connection(client, send_only=True)
                    self._wake_event.wait(2.0)
                    self._wake_event.clear()
                    continue
                if self.settings().token != active_token:
                    client = None
                    active_token = ""
                    offset = None
                    continue
                for update in updates:
                    update_id = int(update.get("update_id", 0))
                    offset = max(offset or 0, update_id + 1)
                    self._handle_update(client, update)
                current = self.settings()
                paired = f" · chat {current.chat_id}" if current.chat_id else " · chưa ghép cặp"
                self._set_state(
                    "connected",
                    f"@{current.bot_username} đang hoạt động{paired}.",
                )
                self._announce_connection(client, send_only=False)
            except Exception as exc:
                detail = str(exc).replace(settings.token, "<token>")
                self._set_state("error", detail)
                self._stop_event.wait(2.0)

    def _drain_outbound(self, client: TelegramBotClient) -> None:
        settings = self.settings()
        if not settings.chat_id:
            return
        for _index in range(10):
            try:
                item = self._outbound.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _OutboundPhoto):
                client.send_photo(
                    settings.chat_id,
                    item.data,
                    filename=item.filename,
                    caption=item.caption,
                )
                time.sleep(0.08)
                continue
            category, message = item
            client.send_message(
                settings.chat_id,
                format_telegram_notification(category, message),
                parse_mode="HTML",
            )
            time.sleep(0.08)

    def _handle_update(
        self,
        client: TelegramBotClient,
        update: dict[str, object],
    ) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("type", "")) != "private":
            return
        try:
            chat_id = int(chat.get("id", 0))
        except (TypeError, ValueError):
            return
        text = str(message.get("text") or message.get("caption") or "").strip()
        command_part, _, args = text.partition(" ") if text.startswith("/") else ("", "", "")
        name = command_part[1:].split("@", 1)[0].casefold() if command_part else ""
        if name == "pair":
            self._handle_pair(client, chat_id, args.strip())
            return
        settings = self.settings()
        if not settings.chat_id or chat_id != settings.chat_id:
            client.send_message(chat_id, "Chat này chưa được DocuRandom cho phép. Hãy mở tool và tạo mã ghép cặp mới.")
            return
        try:
            attachment = self._image_attachment(client, message)
        except TelegramBotError as exc:
            client.send_message(chat_id, f"Không nhận được thumbnail: {exc}")
            return
        if attachment is not None:
            if name not in {"", "thumbnail"}:
                client.send_message(
                    chat_id,
                    "Ảnh chưa được nhận vì caption chứa lệnh khác. Hãy gửi lại ảnh không caption hoặc dùng /thumbnail.",
                )
                return
            response = self.command_handler(
                TelegramCommand(
                    chat_id=chat_id,
                    name="thumbnail_receive",
                    args=args.strip(),
                    raw_text=text,
                    attachment=attachment,
                )
            )
            if response:
                client.send_message(chat_id, response)
            return
        if not name:
            return
        response = self.command_handler(
            TelegramCommand(
                chat_id=chat_id,
                name=name,
                args=args.strip(),
                raw_text=text,
            )
        )
        if response:
            client.send_message(
                chat_id,
                response,
                reply_markup=(
                    telegram_main_keyboard()
                    if name in {"start", "menu", "help"}
                    else telegram_remove_keyboard()
                    if name == "hide_menu"
                    else None
                ),
            )

    @staticmethod
    def _attachment_candidate(
        message: dict[str, object],
    ) -> tuple[str, str, str, int, str] | None:
        photos = message.get("photo")
        if isinstance(photos, list):
            candidates = [item for item in photos if isinstance(item, dict)]
            if candidates:
                selected = max(
                    candidates,
                    key=lambda item: (
                        int(item.get("file_size", 0) or 0),
                        int(item.get("width", 0) or 0) * int(item.get("height", 0) or 0),
                    ),
                )
                return (
                    str(selected.get("file_id", "")),
                    str(selected.get("file_unique_id", "")),
                    "thumbnail.jpg",
                    int(selected.get("file_size", 0) or 0),
                    "image/jpeg",
                )
        document = message.get("document")
        if not isinstance(document, dict):
            return None
        filename = Path(str(document.get("file_name", "thumbnail"))).name
        mime_type = str(document.get("mime_type", "")).casefold()
        suffix = Path(filename).suffix.casefold()
        if not mime_type.startswith("image/") and suffix not in _IMAGE_EXTENSIONS:
            return None
        return (
            str(document.get("file_id", "")),
            str(document.get("file_unique_id", "")),
            filename,
            int(document.get("file_size", 0) or 0),
            mime_type or "application/octet-stream",
        )

    def _image_attachment(
        self,
        client: TelegramBotClient,
        message: dict[str, object],
    ) -> TelegramAttachment | None:
        candidate = self._attachment_candidate(message)
        if candidate is None:
            return None
        file_id, unique_id, filename, declared_size, mime_type = candidate
        if not file_id:
            raise TelegramBotError("Telegram không cung cấp mã tệp ảnh.")
        if declared_size > MAX_TELEGRAM_DOWNLOAD_BYTES:
            raise TelegramBotError("Ảnh Telegram vượt giới hạn tải 20 MB.")
        return TelegramAttachment(
            filename=filename,
            mime_type=mime_type,
            data=client.download_file(file_id),
            file_unique_id=unique_id,
        )

    def _handle_pair(self, client: TelegramBotClient, chat_id: int, code: str) -> None:
        with self._lock:
            valid = (
                bool(self._pairing_code)
                and secrets.compare_digest(code, self._pairing_code)
                and time.monotonic() <= self._pairing_expires_at
            )
            if valid:
                self._pairing_code = ""
                self._pairing_expires_at = 0.0
                self._settings = replace(self._settings, chat_id=chat_id)
                save_telegram_settings(self._settings, self.settings_path)
        if valid:
            client.send_message(
                chat_id,
                "Đã ghép cặp DocuRandom thành công. Bấm Menu hoặc chọn nút bên dưới; không cần nhớ lệnh.",
                reply_markup=telegram_main_keyboard(),
            )
            self._set_state("connected", f"Đã ghép cặp chat {chat_id}.")
        else:
            client.send_message(chat_id, "Mã ghép cặp sai hoặc đã hết hạn. Hãy tạo mã mới trong DocuRandom.")
