from __future__ import annotations

import base64
import json
import ctypes
import hashlib
import http.client
import io
import math
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

from .bridge_integrity import (
    CHAT_BRIDGE_APP_ASAR_SHA256,
    CHAT_BRIDGE_EXECUTABLE_SHA256,
)
from .content_history import (
    find_duplicate_title,
    format_title_history_for_prompt,
)
from .content_quality import narration_problem, minimum_narration_characters, artifact_field_problems
from .renderer import normalize_output_name
from .storage import (
    app_data_dir,
    atomic_write_json,
    atomic_write_text,
    legacy_app_data_dir,
)


CODEX_CLI_INSTALL_URL = "https://learn.chatgpt.com/docs/codex/cli"
CODEX_NPM_PACKAGE = "@openai/codex@latest"

CHATGPT_WEB_BRIDGE_VERSION = "1.0.158"
CHATGPT_WEB_ELECTRON_VERSION = "43.4.0"
_LEGACY_FINAL_EXPORT_REQUEST = """Export cho tôi 1 file tổng hợp:

Tiêu đề

Mô tả

Prompt Thumbnail

Thoại VOICEVOX liền mạch. Bỏ các \"Phần\", thêm chữ A: vào đầu các câu, ví dụ: \"A: abcdef\".

Prompt hình ảnh, các prompt cách nhau một khoảng trắng xuống dòng, ví dụ:

Image 1. Prompt

Image 2. Prompt

Chỉ trả nội dung tổng hợp, không giải thích gì thêm."""
_PREVIOUS_FINAL_EXPORT_REQUEST = """Export cho tôi 1 file tổng hợp:

Tiêu đề

Mô tả

SEO tags

Prompt Thumbnail

Thoại VOICEVOX liền mạch. Bỏ các \"Phần\", thêm chữ A: vào đầu các câu, ví dụ: \"A: abcdef\".

Prompt hình ảnh, các prompt cách nhau một khoảng trắng xuống dòng, ví dụ:

Image 1. Prompt

Image 2. Prompt

Chỉ trả nội dung tổng hợp, không giải thích gì thêm."""
FINAL_EXPORT_REQUEST = """Hãy trình bày trực tiếp trong thân câu trả lời ChatGPT bản tổng hợp hoàn chỉnh dưới dạng văn bản thuần. Không tạo hoặc đính kèm tệp, tài liệu, Canvas hay artifact.

Tiêu đề

Mô tả

SEO tags

Prompt Thumbnail

Thoại VOICEVOX liền mạch. Bỏ các "Phần", thêm chữ A: vào đầu các câu, ví dụ: "A: abcdef".

Prompt hình ảnh, mỗi prompt trên một dòng riêng, ví dụ:

Image 1. Prompt

Image 2. Prompt

Chỉ trả nội dung theo các mục trên, không giải thích gì thêm."""
_SUPPORTED_FINAL_EXPORT_REQUESTS = (
    FINAL_EXPORT_REQUEST,
    _PREVIOUS_FINAL_EXPORT_REQUEST,
    _LEGACY_FINAL_EXPORT_REQUEST,
)
IMAGE_COMPLETION_QUESTION = (
    'Đã hoàn tất promt ảnh chưa? Nếu rồi thì chỉ trả lời "ĐÃ XONG" '
    'và không nói gì thêm. Nếu chưa hoàn tất thì chỉ cần trả lời '
    '"CHƯA XONG" và không giải thích hay nói gì thêm.'
)
CONTENT_REPAIR_PREFIX = "KHÔI PHỤC PHẦN NỘI DUNG CÒN THIẾU — DOCURANDOM"
MAX_CONTENT_REPAIR_ATTEMPTS = 3
MAX_NARRATION_DURATION_REPAIR_ATTEMPTS = 2


def _content_end_signal(value: str) -> bool:
    """Recognize exhaustion without treating it as proof of complete data."""
    lowered = str(value or "").strip().casefold()
    return bool(
        re.search(r"\[\[\s*done\s*\]\]\s*$", lowered)
        or "đã hết nội dung của lượt hiện tại" in lowered
        or "không còn marker [[continue]]" in lowered
    )


def _missing_content_fields(artifacts: ContentArtifacts, expected_images: int, minimum_characters: int = 0) -> list[str]:
    fields = [
        label for label, value in (
            ("tiêu đề đã chọn", artifacts.title),
            ("mô tả", artifacts.description),
            ("prompt thumbnail", artifacts.thumbnail_prompt),
            ("thoại VOICEVOX", artifacts.voicevox_script),
        ) if not value
    ]
    problem = narration_problem(artifacts.voicevox_script, minimum_characters)
    if artifacts.voicevox_script and problem:
        fields.append("thoại VOICEVOX: " + problem)
    if len(artifacts.image_prompts) < expected_images:
        fields.append(f"prompt ảnh ({len(artifacts.image_prompts)}/{expected_images})")
    names={'thumbnail':'prompt thumbnail','description':'mô tả','voice':'thoại VOICEVOX','images':'prompt ảnh'}
    fields.extend(names[key]+': '+reason for key,reason in _artifact_problems(artifacts).items())
    return fields


def _artifact_problems(artifacts: ContentArtifacts) -> dict[str,str]:
    return artifact_field_problems(artifacts.title,artifacts.description,artifacts.voicevox_script,
                                  artifacts.thumbnail_prompt,artifacts.image_prompts)


def content_package_problems(path: Path) -> dict[str,str]:
    text=path.read_text(encoding='utf-8-sig')
    sections=_export_sections(text)
    return artifact_field_problems(sections.get('title',''),sections.get('description',''),
        extract_voicevox_script_from_package(text),sections.get('thumbnail',''),
        _parse_numbered_image_prompts(sections.get('images','')))


def _content_repair_request(artifacts: ContentArtifacts, expected_images: int, minimum_characters: int = 0,
                            target_minutes: float = 0) -> str:
    problems=_artifact_problems(artifacts)
    blocks = [
        instruction for value, instruction in (
            (artifacts.title,
             "[[tieu-de-duoc-chon]]tiêu đề đã chọn[[end-tieu-de-duoc-chon]]"),
            (artifacts.description and 'description' not in problems, "[[mo-ta]]mô tả đầy đủ[[end-mo-ta]]"),
            (artifacts.thumbnail_prompt and 'thumbnail' not in problems,
             "[[prompt-thumbnail]]prompt thumbnail[[end-prompt-thumbnail]]"),
            (not narration_problem(artifacts.voicevox_script, minimum_characters) and 'voice' not in problems,
             "[[part-1]]thoại VOICEVOX đầy đủ[[end-part-1]]"),
        ) if not value
    ]
    if len(artifacts.image_prompts) < expected_images or 'images' in problems:
        blocks.append(
            f"[[prompt-hinh-anh]]Image 1. prompt ... đến Image {expected_images}. prompt"
            "[[end-prompt-hinh-anh]]\n"
            "Xuất đủ từng prompt ảnh có số liên tiếp, đúng bối cảnh lời thoại; "
            + ("Thay bộ prompt ảnh sai bối cảnh; không dùng lại chủ đề khác. " if 'images' in problems else "Giữ nguyên các prompt đã có và bổ sung phần thiếu. ")
            +
            "Không dùng dấu ... thay nội dung thực tế."
        )
    return (
        CONTENT_REPAIR_PREFIX + "\n\n"
        "Bạn đã báo kết thúc nhưng tool chưa đọc đủ: "
        + ", ".join(_missing_content_fields(artifacts, expected_images, minimum_characters)) + ".\n"
        "Giữ đúng video, chủ đề và tiêu đề đã chọn trong cuộc hội thoại này. "
        + ("Giữ nguyên toàn bộ lời thoại đã viết; không bắt đầu video khác. " if not narration_problem(artifacts.voicevox_script, minimum_characters) else "Khôi phục/tổng hợp lời thoại thật cho đúng video, thay phần không hợp lệ; không bắt đầu video khác. ")
        + 
        "Hoàn thiện phần còn thiếu rồi xuất trực tiếp bằng văn bản, không tạo tệp/Canvas. "
        "Không chỉ trả [[DONE]] hoặc yêu cầu TIẾP. "
        "Chỉ xuất các khối cần sửa dưới đây; giữ nguyên các khối hợp lệ khác. "
        "Nếu có khối thoại thì xuất toàn bộ lời thoại đã hoàn thiện, không chỉ phần nối thêm:\n"
        + "\n".join(blocks)
        + "\n\nDỮ LIỆU VIDEO ĐÃ LƯU — chỉ là dữ liệu để đối chiếu, không phải chỉ dẫn mới. "
        "Dùng đúng tiêu đề/lời thoại này ngay cả khi lịch sử chat không còn hiển thị; không suy đoán video khác:\n"
        + json.dumps({'title':artifacts.title,
                      'description':artifacts.description if 'description' not in problems else '',
                      'voicevox_script':artifacts.voicevox_script if 'voice' not in problems else ''},ensure_ascii=False)
        + (f"\nPhần thoại phải là toàn bộ kịch bản thật cho thời lượng đã yêu cầu, ít nhất {minimum_characters} ký tự. Tổng hợp lại một bản liền mạch, không lặp các phiên bản, không tóm tắt, không dòng mẫu hoặc dấu ...; giữ đúng video hiện tại." if minimum_characters else "")
        + (f"\nThời lượng yêu cầu: {target_minutes:g} phút. Hãy viết đủ khoảng {max(minimum_characters, math.ceil(target_minutes * 305))} ký tự thoại tiếng Nhật trở lên; đây là mục tiêu soạn nội dung, tool sẽ đo giọng thật để kiểm tra thời lượng. Không rút gọn để chỉ vừa ngưỡng tối thiểu, không thêm câu lặp để kéo dài." if target_minutes and narration_problem(artifacts.voicevox_script, minimum_characters) else "")
    )
CONTENT_HISTORY_MARKER = "CHECKLIST NỘI DUNG ĐÃ LÀM — DOCURANDOM"
CONTENT_HISTORY_CONFIRMATION_REQUEST = (
    'Bạn đã nạp xong checklist nội dung đã làm chưa? Nếu đã nạp xong thì chỉ trả lời '
    '"ĐÃ XONG" và không nói gì thêm.'
)
CHATGPT_WEB_BRIDGE_REPOSITORY = "DocuRandomStudio/chat_bridge"
CHATGPT_WEB_INSTALLER_NAME = "DocuRandom Chat Bridge.exe"
CHATGPT_WEB_INSTALLER_SHA256 = CHAT_BRIDGE_EXECUTABLE_SHA256
CHATGPT_WEB_APP_ASAR_SHA256 = CHAT_BRIDGE_APP_ASAR_SHA256
CHATGPT_WEB_DEFAULT_PORT = 17841
CHATGPT_WEB_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
CHATGPT_WEB_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
# A reset can affect only the local HTTP response socket while the bridge is
# still finishing the already-accepted browser turn. Poll the exact
# thread/turn result instead of issuing a second prompt.
CHATGPT_WEB_DISCONNECTED_RECOVERY_SECONDS = 10 * 60
CHATGPT_WEB_DISCONNECTED_POLL_SECONDS = 0.5
# This is a transport liveness lease, not a limit on how long ChatGPT may
# think or how long an answer may be.  The bridge owns a 180-second stale-turn
# watchdog and can recover its exact conversation before releasing the turn.
# Keep this client lease longer than the bridge lease so this caller does not
# cancel that recovery early; a still-slower dead transport remains bounded.
CHATGPT_WEB_ACTIVITY_POLL_SECONDS = 0.35
CHATGPT_WEB_HEARTBEAT_STALE_SECONDS = 210.0
CHATGPT_WEB_INPUT_PROGRESS_STALE_SECONDS = 180.0
CHATGPT_WEB_HEARTBEAT_CANCEL_TIMEOUT_SECONDS = 4.0
CHATGPT_WEB_CONNECT_TIMEOUT_SECONDS = 15.0
# This lease covers only the local CLI/bridge handshake before a real ChatGPT
# thread exists.  It is deliberately separate from generation time: once a
# thread exists, the heartbeat lease above is the only liveness guard.
CHATGPT_WEB_STARTUP_STALE_SECONDS = 180.0
# A bridge can have a real server-side generation with no CLI thread event yet.
# Only the explicit no-response DOM state gets this longer fail-closed window;
# a live generation marker is allowed to run without a guessed answer deadline.
CHATGPT_WEB_STARTUP_NO_RESPONSE_STALE_SECONDS = 600.0
# Once a real thread exists, an explicit empty-DOM/no-response state gets the
# same fail-closed window as the pre-thread handshake.  The bridge itself can
# perform one exact-conversation recovery inside this window; the Python
# watchdog must not mistake its per-second status clock for progress.
CHATGPT_WEB_NO_RESPONSE_STALE_SECONDS = 600.0
DEFAULT_IMAGE_PROMPT_INTERVAL_SECONDS = 60.0
MAX_EXPECTED_IMAGE_PROMPTS = 500
DEFAULT_ALLOWED_MISSING_IMAGE_PROMPTS = 3
CHATGPT_WEB_AUTO_MODEL = "chatgpt-web/auto"
CHATGPT_WEB_SMART_HIGH_MODEL = "chatgpt-web/high"
CODEX_CHATGPT_WEB_PROFILE = "docurandom-chatgpt-web"
CODEX_CHATGPT_WEB_PROVIDER = "docurandom_chatgpt_web"
CODEX_CHATGPT_WEB_TOKEN_ENV = "DOCURANDOM_CHAT_BRIDGE_TOKEN"
CHATGPT_WEB_MODEL_LABELS: dict[str, str] = {
    "ChatGPT Web · Theo model đang chọn trên Web": CHATGPT_WEB_AUTO_MODEL,
}
CHATGPT_WEB_DEFAULT_MODEL_LABEL = next(iter(CHATGPT_WEB_MODEL_LABELS))
_CHATGPT_WEB_MODEL_PRIORITY = (
    "chatgpt-web/auto",
    "chatgpt-web/pro",
    "chatgpt-web/extra-high",
    "chatgpt-web/high",
    "chatgpt-web/medium",
    "chatgpt-web/light",
    "chatgpt-web/luna",
)


class ContentAICancelled(Exception):
    pass


class ContentAIError(RuntimeError):
    pass


class ChatGPTPreSubmitRejected(ContentAIError):
    """Bridge refused this request before staging attachments or dispatching."""


class ContentAISubmissionUncertain(ContentAIError):
    """The prompt may have reached ChatGPT, but no recoverable thread exists."""

    pass


def _codex_event_error_message(event: dict[str, object]) -> str:
    """Return the actionable error carried by Codex JSONL events."""

    if event.get("type") not in {"error", "turn.failed"}:
        return ""
    candidates: list[object] = [event.get("message")]
    nested = event.get("error")
    if isinstance(nested, dict):
        candidates.extend((nested.get("message"), nested.get("detail")))
    else:
        candidates.append(nested)
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            message = candidate.strip()
            # Codex wraps an already-actionable bridge failure in this generic
            # transport prefix. Removing only that fixed wrapper keeps the UI
            # concise without hiding the real ChatGPT/bridge diagnosis.
            message = re.sub(
                r"^stream disconnected before completion:\s*",
                "",
                message,
                flags=re.IGNORECASE,
            )
            lowered = message.casefold()
            if "stopped making progress before the inactivity timeout" in lowered:
                return (
                    "ChatGPT đã ngừng cập nhật phản hồi quá lâu. Tool đã dừng an toàn và "
                    "giữ checkpoint; bộ hẹn giờ hoặc Tự sửa sẽ tự nối lại đúng lượt, không gửi trùng."
                )
            if "exceeded the absolute safety limit" in lowered:
                return (
                    "Phản hồi ChatGPT đã vượt giới hạn an toàn tuyệt đối. Tool đã giữ "
                    "checkpoint; bộ hẹn giờ hoặc Tự sửa sẽ tự recheck và nối lại."
                )
            if "active chatgpt web slots are busy" in lowered:
                return (
                    "Các phiên ChatGPT Web đang dùng hết số luồng phù hợp với máy. "
                    "Workspace này được xếp hàng để tự chạy khi có lượt trống."
                )
            return message
    return ""


def _codex_stderr_error_detail(lines: Iterable[str]) -> str:
    """Hide routine Codex warnings when no structured failure was emitted."""

    actionable = [
        line.strip()
        for line in lines
        if line.strip() and not re.search(r"\bWARN\b", line, flags=re.IGNORECASE)
    ]
    return "\n".join(actionable[-8:]).strip()


@dataclass(frozen=True)
class ChatAttachment:
    name: str
    data: bytes
    mime_type: str = "text/plain"


def _prepare_direct_text_prompt(prompt: str) -> tuple[str, tuple[ChatAttachment, ...]]:
    """Match the existing hash-named master-file transport without a CLI."""
    length = len(prompt.encode("utf-16-le")) // 2
    data = prompt.encode("utf-8")
    if length < 64_000 or not data or len(data) > CHATGPT_WEB_MAX_ATTACHMENT_BYTES:
        return prompt, ()
    digest = hashlib.sha256(data).hexdigest()
    name = f"DocuRandom-Master-Prompt-{digest[:12]}.txt"
    wire = "\n".join((
        "Toàn bộ nội dung yêu cầu của lượt này nằm nguyên vẹn trong tệp UTF-8 đính kèm.",
        f"Tệp: {name}",
        f"Độ dài: {length:,} ký tự; SHA-256: {digest}.",
        "Hãy đọc toàn bộ tệp từ ký tự đầu tiên đến ký tự cuối cùng và xử lý như thể toàn bộ nội dung tệp được dán trực tiếp vào tin nhắn này.",
        "Không tóm tắt, không bỏ qua, không thay đổi thứ tự và tuân thủ đúng mọi chỉ dẫn nằm trong tệp.",
    ))
    return wire, (ChatAttachment(name, data, "text/plain; charset=utf-8"),)


@dataclass(frozen=True)
class ChatGPTGeneratedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    assistant_text: str
    response_id: str


@dataclass(frozen=True)
class CodexCliStatus:
    path: Path | None
    version: str = ""
    logged_in: bool = False
    login_method: str = ""
    detail: str = ""

    @property
    def ready(self) -> bool:
        # Codex is used only as a local transport for the app-owned provider.
        # It does not need or inspect the user's Codex/OpenAI authentication.
        return self.path is not None and bool(self.version)


@dataclass(frozen=True)
class ChatGPTWebStatus:
    launcher_path: Path | None
    installed: bool = False
    launcher_running: bool = False
    setup_complete: bool = False
    authenticated: bool = False
    runtime_ready: bool = False
    session_busy: bool = False
    endpoint: str = ""
    bridge_version: str = ""
    available_models: tuple[str, ...] = ()
    detail: str = ""
    access_token: str = field(default="", repr=False)
    codex_path: Path | None = None
    codex_version: str = ""
    codex_profile_path: Path | None = None
    codex_ready: bool | None = None
    account_capabilities: dict[str, object] = field(default_factory=dict, repr=False)

    @property
    def operational(self) -> bool:
        """The shared bridge is healthy even while another workspace uses it."""

        return bool(
            self.installed
            and self.launcher_running
            and self.setup_complete
            and self.authenticated
            and self.runtime_ready
            and self.endpoint
            and self.available_models
            and self.access_token
            and self.codex_ready is not False
        )

    @property
    def ready(self) -> bool:
        return self.operational and not self.session_busy

    # Compatibility properties keep the UI state machine small while the
    # content backend moves from Codex CLI to the local ChatGPT Web bridge.
    @property
    def path(self) -> Path | None:
        return self.launcher_path

    @property
    def version(self) -> str:
        return (
            f"ChatGPT Web Bridge {self.bridge_version}"
            if self.bridge_version
            else "ChatGPT Web Bridge"
        )

    @property
    def logged_in(self) -> bool:
        return self.authenticated

    @property
    def login_method(self) -> str:
        return "ChatGPT Web" if self.authenticated else ""


def _chatgpt_web_root() -> Path:
    return app_data_dir() / "chat-bridge"


def _chatgpt_web_core_home() -> Path:
    return _chatgpt_web_root()


def _chatgpt_web_launcher_data() -> Path:
    return _chatgpt_web_root()


def _chatgpt_web_config_path() -> Path:
    return _chatgpt_web_core_home() / "config.json"


def _chatgpt_web_descriptor_path() -> Path:
    return _chatgpt_web_core_home() / "runtime" / "bridge.json"


def _chatgpt_web_launcher_state_path() -> Path:
    return _chatgpt_web_launcher_data() / "bridge-state.json"


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _chatgpt_web_environment() -> dict[str, str]:
    root = _chatgpt_web_root()
    root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DOCURANDOM_CHAT_BRIDGE_HOME"] = str(_chatgpt_web_core_home())
    env["DOCURANDOM_CHAT_BRIDGE_PROFILE"] = str(root / "profile")
    return env


def harden_chatgpt_web_profile() -> None:
    """Create the isolated clean-room bridge profile and its local policy."""
    root = _chatgpt_web_root()
    (root / "profile").mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        root / "bridge-policy.json",
        {
            "version": 1,
            "loopback_only": True,
            "external_cookie_import": False,
            "codex_integration": True,
            "telemetry": False,
        },
    )


def _candidate_chatgpt_web_launcher_paths() -> Iterable[Path]:
    configured = os.environ.get("DOCURANDOM_CHATGPT_WEB_LAUNCHER", "").strip().strip('"')
    if configured:
        yield Path(configured).expanduser()
    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        yield Path(bundle_root) / "chat-bridge" / CHATGPT_WEB_INSTALLER_NAME
    yield Path(sys.executable).resolve().parent / "chat-bridge" / CHATGPT_WEB_INSTALLER_NAME
    yield (
        Path(__file__).resolve().parent.parent
        / "third_party"
        / "docurandom-chat-bridge"
        / CHATGPT_WEB_INSTALLER_NAME
    )


@lru_cache(maxsize=16)
def _sha256_for_file_identity(path: str, size: int, modified_ns: int) -> str:
    del size, modified_ns
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().casefold()


def _sha256_file(path: Path) -> str:
    metadata = path.stat()
    return _sha256_for_file_identity(
        str(path.resolve()),
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _chatgpt_web_launcher_is_supported(launcher: Path) -> bool:
    try:
        return bool(
            _sha256_file(launcher) == CHATGPT_WEB_INSTALLER_SHA256
            and _sha256_file(launcher.parent / "resources" / "app.asar")
            == CHATGPT_WEB_APP_ASAR_SHA256
        )
    except OSError:
        return False


def discover_chatgpt_web_launcher() -> Path | None:
    for candidate in _candidate_chatgpt_web_launcher_paths():
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and _chatgpt_web_launcher_is_supported(resolved):
            return resolved
    return None


def _candidate_bundled_chatgpt_web_installers() -> Iterable[Path]:
    configured = os.environ.get("DOCURANDOM_CHATGPT_WEB_LAUNCHER", "").strip().strip('"')
    if configured:
        yield Path(configured).expanduser()
    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        yield Path(bundle_root) / "chat-bridge" / CHATGPT_WEB_INSTALLER_NAME
    yield Path(sys.executable).resolve().parent / "chat-bridge" / CHATGPT_WEB_INSTALLER_NAME
    yield (
        Path(__file__).resolve().parent.parent
        / "third_party"
        / "docurandom-chat-bridge"
        / CHATGPT_WEB_INSTALLER_NAME
    )


def _bundled_chatgpt_web_installer() -> Path | None:
    seen: set[str] = set()
    for candidate in _candidate_bundled_chatgpt_web_installers():
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_file():
            return resolved
    return None


def install_chatgpt_web_launcher(
    *,
    timeout: float = 1200.0,
    on_progress: Callable[[float, str], None] | None = None,
) -> Path:
    """Verify and return DocuRandom's bundled portable clean-room bridge."""
    existing = discover_chatgpt_web_launcher()
    if existing is not None:
        harden_chatgpt_web_profile()
        return existing
    if os.name != "nt":
        raise ContentAIError("Tích hợp ChatGPT Web hiện chỉ hỗ trợ bản Windows của tool.")

    def progress(value: float, message: str) -> None:
        if on_progress is not None:
            on_progress(max(0.0, min(1.0, value)), message)

    bridge = _bundled_chatgpt_web_installer()
    if bridge is None:
        raise ContentAIError(
            "Thiếu DocuRandom Chat Bridge. Hãy giải nén/cài lại đầy đủ "
            "DocuRandomStudio; tool không tự tải một bản không được kiểm chứng."
        )
    progress(
        0.15,
        f"Đang xác minh bridge {CHATGPT_WEB_BRIDGE_VERSION} · Electron "
        f"{CHATGPT_WEB_ELECTRON_VERSION}…",
    )
    try:
        installer_hash = _sha256_file(bridge)
    except OSError as exc:
        raise ContentAIError(f"Không thể đọc DocuRandom Chat Bridge: {exc}") from exc
    if installer_hash != CHATGPT_WEB_INSTALLER_SHA256:
        raise ContentAIError(
            "DocuRandom Chat Bridge không khớp SHA-256 đã khóa; đã từ chối chạy."
        )
    harden_chatgpt_web_profile()
    progress(1.0, "DocuRandom Chat Bridge portable đã sẵn sàng · không cần cài đặt.")
    return bridge


def start_chatgpt_web_launcher(*, show: bool) -> Path:
    launcher = discover_chatgpt_web_launcher()
    if launcher is None:
        raise ContentAIError("Thiếu DocuRandom Chat Bridge trong bản portable.")
    harden_chatgpt_web_profile()
    command = [str(launcher)]
    if not show:
        command.append("--hidden")
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_chatgpt_web_environment(),
            creationflags=_creation_flags(),
        )
    except OSError as exc:
        raise ContentAIError(f"Không thể khởi động ChatGPT Web Bridge: {exc}") from exc
    return launcher


def _pid_is_running(pid: object) -> bool:
    if not isinstance(pid, int) or pid < 1:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_process_executable(pid: int) -> Path | None:
    if os.name != "nt" or pid < 1:
        return None
    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value).resolve()
    finally:
        kernel32.CloseHandle(handle)


def _bridge_descriptor_path(root: Path) -> Path:
    return root / "runtime" / "bridge.json"


def _stop_chatgpt_web_bridge_at(root: Path) -> bool:
    """Stop only a verified DocuRandom bridge process described by ``root``."""
    if os.name != "nt":
        return False
    descriptor = _read_json_object(_bridge_descriptor_path(root))
    if descriptor.get("version") != 1 or descriptor.get("kind") != "docurandom-chat-bridge":
        return False
    pid = descriptor.get("pid")
    if not isinstance(pid, int) or not _pid_is_running(pid):
        return False
    executable = _windows_process_executable(pid)
    if (
        executable is None
        or executable.name.casefold() != CHATGPT_WEB_INSTALLER_NAME.casefold()
        or executable.parent.name.casefold() != "chat-bridge"
        or not (executable.parent / "resources" / "app.asar").is_file()
    ):
        return False

    process_terminate = 0x0001
    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(process_terminate | synchronize, False, pid)
    if not handle:
        return False
    try:
        if not kernel32.TerminateProcess(handle, 0):
            return False
        kernel32.WaitForSingleObject(handle, 5000)
    finally:
        kernel32.CloseHandle(handle)
    deadline = time.monotonic() + 5.0
    while _pid_is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _pid_is_running(pid)


def _stop_outdated_chatgpt_web_bridge() -> bool:
    return _stop_chatgpt_web_bridge_at(_chatgpt_web_root())


def _request_chatgpt_web_bridge_shutdown_at(
    root: Path,
    timeout: float = 8.0,
) -> bool:
    """Ask an authenticated idle bridge to exit without trusting its install path."""
    descriptor = _read_json_object(_bridge_descriptor_path(root))
    if descriptor.get("version") != 1 or descriptor.get("kind") != "docurandom-chat-bridge":
        return False
    pid = descriptor.get("pid")
    control = descriptor.get("control")
    if not isinstance(pid, int) or not _pid_is_running(pid) or not isinstance(control, dict):
        return False
    try:
        endpoint = _validated_loopback_origin(
            control.get("endpoint"), "Kênh điều khiển ChatGPT Web"
        )
    except ContentAIError:
        return False
    token = control.get("token")
    if not isinstance(token, str) or len(token) < 32:
        return False
    request = urllib.request.Request(
        endpoint + "/v1/session/shutdown",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=min(3.0, timeout)) as response:
            if getattr(response, "status", 0) != 202:
                return False
            _read_limited_json_response(response, 16 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise ContentAIError(
                "ChatGPT Web đang tạo nội dung; hãy đợi tác vụ AI hoàn tất rồi cập nhật."
            ) from exc
        return False
    except (OSError, urllib.error.URLError, ContentAIError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(0.05)
    return not _pid_is_running(pid)


def _request_chatgpt_web_bridge_shutdown(timeout: float = 8.0) -> bool:
    return _request_chatgpt_web_bridge_shutdown_at(
        _chatgpt_web_root(),
        timeout,
    )


def stop_chatgpt_web_bridge_for_update() -> bool:
    """Stop the bundled bridge only when it is idle so its files can be replaced."""
    running, _authenticated, _token, busy = _chatgpt_web_session_status(3.0)
    if not running:
        return True
    if busy:
        raise ContentAIError(
            "ChatGPT Web đang tạo nội dung; hãy đợi tác vụ AI hoàn tất rồi cập nhật."
        )
    if not (
        _request_chatgpt_web_bridge_shutdown()
        or _stop_outdated_chatgpt_web_bridge()
    ):
        raise ContentAIError("Chưa thể đóng ChatGPT Web Bridge an toàn để cập nhật.")
    return True


def _chatgpt_web_session_status_at(
    root: Path,
    timeout: float,
) -> tuple[bool, bool, str, bool]:
    descriptor = _read_json_object(_bridge_descriptor_path(root))
    if descriptor.get("version") != 1 or descriptor.get("kind") != "docurandom-chat-bridge":
        return False, False, "", False
    if not _pid_is_running(descriptor.get("pid")):
        return False, False, "", False
    control = descriptor.get("control")
    if not isinstance(control, dict):
        return True, False, "", False
    endpoint = _validated_loopback_origin(
        control.get("endpoint"), "Kênh điều khiển ChatGPT Web"
    )
    token = control.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,}", token):
        return True, False, "", False
    request = urllib.request.Request(
        f"{endpoint}/v1/session/status",
        headers={
            "Authorization": f"Bearer {token}",
            "Cache-Control": "no-store",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = _read_limited_json_response(response)
    except (OSError, urllib.error.URLError, ContentAIError):
        return True, False, "", False
    authenticated = value.get("authenticated") is True
    busy = value.get("busy") is True
    return True, authenticated, token if authenticated else "", busy


def _known_chatgpt_web_roots() -> tuple[Path, ...]:
    roots = (_chatgpt_web_root(), legacy_app_data_dir() / "chat-bridge")
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = os.path.normcase(str(root.resolve(strict=False)))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return tuple(unique)


def stop_all_chatgpt_web_bridges(*, force: bool = False) -> bool:
    """Close every known local bridge while preserving its browser profile."""

    all_stopped = True
    for root in _known_chatgpt_web_roots():
        running, _authenticated, _token, busy = _chatgpt_web_session_status_at(
            root,
            2.0,
        )
        if not running:
            continue
        if busy and not force:
            all_stopped = False
            continue
        stopped = False
        if not busy:
            try:
                stopped = _request_chatgpt_web_bridge_shutdown_at(root, 5.0)
            except ContentAIError:
                stopped = False
        if not stopped and force:
            stopped = _stop_chatgpt_web_bridge_at(root)
        all_stopped = all_stopped and stopped
    return all_stopped


def _clear_chatgpt_web_transient_cache() -> None:
    """Remove only disposable Chromium caches, never login/session storage."""

    profile = (_chatgpt_web_root() / "profile").resolve(strict=False)
    relative_paths = (
        "Cache",
        "Code Cache",
        "GPUCache",
        "DawnCache",
        "GrShaderCache",
        "GraphiteDawnCache",
        "Partitions/docurandom-chatgpt/Cache",
        "Partitions/docurandom-chatgpt/Code Cache",
        "Partitions/docurandom-chatgpt/GPUCache",
        "Partitions/docurandom-chatgpt/DawnCache",
        "Partitions/docurandom-chatgpt/GrShaderCache",
        "Partitions/docurandom-chatgpt/GraphiteDawnCache",
    )
    for relative in relative_paths:
        candidate = (profile / relative).resolve(strict=False)
        if profile not in candidate.parents:
            continue
        try:
            if candidate.is_dir():
                shutil.rmtree(candidate)
            elif candidate.is_file():
                candidate.unlink()
        except OSError:
            # Chromium can briefly retain a cache handle after shutdown. The
            # in-process clearCache path already covered it when available.
            continue


def _request_chatgpt_web_bridge_repair(
    *,
    timeout: float = 120.0,
) -> dict[str, object] | None:
    descriptor = _read_json_object(_chatgpt_web_descriptor_path())
    if descriptor.get("version") != 1 or descriptor.get("kind") != "docurandom-chat-bridge":
        return None
    pid = descriptor.get("pid")
    control = descriptor.get("control")
    if not isinstance(pid, int) or not _pid_is_running(pid) or not isinstance(control, dict):
        return None
    endpoint = _validated_loopback_origin(
        control.get("endpoint"), "Kênh điều khiển ChatGPT Web"
    )
    token = control.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,}", token):
        return None
    request = urllib.request.Request(
        endpoint + "/v1/session/repair",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _read_limited_json_response(response, 64 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise ContentAIError(
                "ChatGPT Web đang làm nội dung. Hãy đợi tác vụ hiện tại hoàn tất rồi bấm Sửa lại."
            ) from exc
        return None
    except (OSError, urllib.error.URLError, ContentAIError):
        return None


def _validated_chatgpt_conversation_url(value: object) -> str:
    """Accept only one real ChatGPT conversation URL, never the home shell."""

    if not isinstance(value, str):
        raise ContentAIError("URL ChatGPT phải là một chuỗi hợp lệ.")
    parsed = urllib.parse.urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname != "chatgpt.com"
        or parsed.username
        or parsed.password
        or not re.fullmatch(r"/c/[^/?#]+", parsed.path or "")
        or urllib.parse.unquote(parsed.path).casefold().startswith("/c/local-chatgpt:")
    ):
        raise ContentAIError(
            "URL phải có dạng https://chatgpt.com/c/<conversation-id>; "
            "không nhận trang ChatGPT home."
        )
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
    )


def _chatgpt_web_bridge_control(
    *,
    timeout: float = 3.0,
) -> tuple[str, str] | None:
    descriptor = _read_json_object(_chatgpt_web_descriptor_path())
    if descriptor.get("version") != 1 or descriptor.get("kind") != "docurandom-chat-bridge":
        return None
    control = descriptor.get("control")
    pid = descriptor.get("pid")
    if not isinstance(pid, int) or not _pid_is_running(pid) or not isinstance(control, dict):
        return None
    endpoint = _validated_loopback_origin(
        control.get("endpoint"), "Kênh điều khiển ChatGPT Web"
    )
    token = control.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,}", token):
        return None
    return endpoint, token


def get_chatgpt_web_conversation_url(
    thread_id: str,
    *,
    timeout: float = 8.0,
) -> str:
    """Resolve the exact saved /c/... URL belonging to one workspace thread."""

    normalized = str(thread_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", normalized):
        raise ContentAIError("Workspace chưa có ChatGPT thread hợp lệ để lấy URL.")
    fallback = ""
    state = _read_json_object(_chatgpt_web_launcher_state_path())
    saved_turns = state.get("savedTurns")
    if isinstance(saved_turns, dict):
        saved = saved_turns.get(f"thread:{normalized}")
        if isinstance(saved, dict):
            try:
                fallback = _validated_chatgpt_conversation_url(saved.get("chatUrl", ""))
            except ContentAIError:
                fallback = ""
    control = _chatgpt_web_bridge_control(timeout=timeout)
    if not fallback:
        from .conversation_links import saved_conversation_url
        fallback=saved_conversation_url(_chatgpt_web_launcher_state_path(),normalized)
    if control is None:
        if fallback:
            return fallback
        raise ContentAIError("ChatGPT Web Bridge chưa chạy; không lấy được URL conversation.")
    endpoint, token = control
    body = json.dumps(
        {"thread_id": normalized, "include_dom": False},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint + "/v1/session/diagnostic",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = _read_limited_json_response(response, 256 * 1024)
        for candidate in (value.get("saved_url"), value.get("current_url"), fallback):
            try:
                return _validated_chatgpt_conversation_url(candidate)
            except ContentAIError:
                continue
    except urllib.error.HTTPError as exc:
        if fallback and exc.code in (404, 409, 503):
            return fallback
    except (OSError, urllib.error.URLError, ContentAIError, ValueError, TypeError):
        if fallback:
            return fallback
    if fallback:
        return fallback
    raise ContentAIError("Bridge chưa có URL /c/... đã lưu cho workspace này.")


def open_chatgpt_web_conversation(
    url: str,
    *,
    thread_id: str = "",
    timeout: float = 15.0,
) -> str:
    """Show one exact conversation in the integrated ChatGPT Web window."""

    conversation_url = _validated_chatgpt_conversation_url(url)
    control = _chatgpt_web_bridge_control(timeout=timeout)
    if control is None:
        raise ContentAIError("ChatGPT Web Bridge chưa chạy; hãy bấm Kết nối ChatGPT trước.")
    endpoint, token = control
    body = json.dumps(
        {
            "thread_id": str(thread_id or "").strip(),
            "url": conversation_url,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint + "/v1/session/open-conversation",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = _read_limited_json_response(response, 64 * 1024)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = _read_limited_json_response(exc, 64 * 1024)
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message", "")).strip()
        except Exception:
            detail = ""
        raise ContentAIError(detail or "Không thể mở đúng conversation ChatGPT.") from exc
    except (OSError, urllib.error.URLError, ContentAIError) as exc:
        raise ContentAIError(f"Không thể mở đúng conversation ChatGPT: {exc}") from exc
    opened = value.get("url") if isinstance(value, dict) else ""
    return _validated_chatgpt_conversation_url(opened or conversation_url)


def _chatgpt_web_has_active_turns(timeout: float = 3.0) -> bool:
    """Read real activity, not the separate 'all slots full' capacity flag."""

    descriptor = _read_json_object(_chatgpt_web_descriptor_path())
    control = descriptor.get("control")
    if isinstance(control, dict):
        try:
            endpoint = _validated_loopback_origin(
                control.get("endpoint"), "Kênh điều khiển ChatGPT Web"
            )
            request = urllib.request.Request(
                endpoint + "/healthz",
                headers={"Cache-Control": "no-store"},
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                value = _read_limited_json_response(response, 64 * 1024)
            return int(value.get("active_turns", 0) or 0) > 0
        except (OSError, ValueError, TypeError, urllib.error.URLError, ContentAIError):
            pass
    state = _read_json_object(_chatgpt_web_launcher_state_path())
    return state.get("busy") is True


def repair_chatgpt_web_bridge(
    *,
    show: bool = True,
    timeout: float = 150.0,
    on_progress: Callable[[float, str], None] | None = None,
) -> ChatGPTWebStatus:
    """Repair a stuck embedded ChatGPT renderer without removing login data."""

    def progress(value: float, message: str) -> None:
        if on_progress is not None:
            on_progress(max(0.0, min(1.0, value)), message)

    install_chatgpt_web_launcher(
        on_progress=lambda value, message: progress(0.1 * value, message)
    )
    running, _authenticated, _token, busy = _chatgpt_web_session_status(3.0)
    if busy:
        raise ContentAIError(
            "ChatGPT Web đang làm nội dung. Hãy đợi tác vụ hiện tại hoàn tất rồi bấm Sửa lại."
        )
    if running:
        progress(0.2, "Đang làm mới renderer và cache tạm của ChatGPT Web…")
        repaired = _request_chatgpt_web_bridge_repair(
            timeout=min(120.0, max(15.0, timeout))
        )
        if repaired is not None and repaired.get("ready") is True:
            status = check_chatgpt_web_bridge(auto_start=False, timeout=12.0)
            progress(1.0, "ChatGPT Web đã được làm mới; đăng nhập và checkpoint được giữ nguyên.")
            return status

    # An old bridge does not expose the repair route, while a badly wedged
    # renderer may not answer it. Restart the verified local process and clear
    # only cache folders; cookies, Local Storage and saved turn bindings stay.
    if _chatgpt_web_has_active_turns():
        raise ContentAIError(
            "ChatGPT Web đang làm nội dung. Tool giữ nguyên lượt đang dở; hãy đợi xong rồi bấm Sửa lại."
        )
    progress(0.45, "Bridge không phản hồi đầy đủ · đang khởi động lại an toàn…")
    if not stop_all_chatgpt_web_bridges(force=True):
        raise ContentAIError(
            "Không thể đóng ChatGPT Web Bridge đang treo. Hãy đóng cửa sổ Bridge rồi thử lại."
        )
    _clear_chatgpt_web_transient_cache()
    progress(0.65, "Đã dọn cache tạm · đang mở lại ChatGPT Web…")
    start_chatgpt_web_launcher(show=show)
    deadline = time.monotonic() + max(15.0, timeout)
    last = ChatGPTWebStatus(
        launcher_path=discover_chatgpt_web_launcher(),
        installed=True,
        detail="Bridge đang khởi động lại.",
    )
    while time.monotonic() < deadline:
        time.sleep(0.5)
        last = check_chatgpt_web_bridge(
            auto_start=False,
            passive_session=True,
            timeout=5.0,
        )
        if last.operational:
            progress(1.0, "ChatGPT Web đã hoạt động lại; đăng nhập và checkpoint được giữ nguyên.")
            return last
        if last.launcher_running and not last.authenticated and time.monotonic() + 5.0 >= deadline:
            break
    if last.launcher_running and not last.authenticated:
        progress(1.0, "Bridge đã sửa xong; cửa sổ đăng nhập đã được mở để xác minh lại tài khoản.")
        return last
    raise ContentAIError(
        "ChatGPT Web chưa hoạt động lại sau khi sửa. Hãy cài lại bản DocuRandom đầy đủ."
    )


def _validated_loopback_origin(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ContentAIError(f"{label} không hợp lệ.")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not parsed.port
    ):
        raise ContentAIError(f"{label} phải là địa chỉ 127.0.0.1 cục bộ.")
    return f"http://127.0.0.1:{parsed.port}"


def _read_limited_json_response(response: object, limit: int = 1024 * 1024) -> dict[str, object]:
    reader = getattr(response, "read")
    raw = reader(limit + 1)
    if len(raw) > limit:
        raise ContentAIError("Phản hồi trạng thái ChatGPT Web lớn bất thường.")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContentAIError("ChatGPT Web Bridge trả về trạng thái không hợp lệ.") from exc
    if not isinstance(value, dict):
        raise ContentAIError("ChatGPT Web Bridge trả về trạng thái không hợp lệ.")
    return value


def _chatgpt_web_session_status(timeout: float) -> tuple[bool, bool, str, bool]:
    return _chatgpt_web_session_status_at(_chatgpt_web_root(), timeout)


def _chatgpt_web_account_capabilities(
    endpoint: str, access_token: str, *, timeout: float,
) -> dict[str, object]:
    """Read account notices without changing a conversation or its model."""

    endpoint = _validated_loopback_origin(endpoint, "Kênh điều khiển ChatGPT Web")
    request = urllib.request.Request(
        f"{endpoint}/v1/session/capabilities",
        headers={"Authorization": f"Bearer {access_token}", "Cache-Control": "no-store"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _read_limited_json_response(response)
    except (OSError, urllib.error.URLError, ContentAIError):
        return {}


def _inspect_chatgpt_web_session(timeout: float) -> tuple[bool, bool, str, bool]:
    descriptor = _read_json_object(_chatgpt_web_descriptor_path())
    if descriptor.get("version") != 1 or descriptor.get("kind") != "docurandom-chat-bridge":
        return False, False, "", False
    if not _pid_is_running(descriptor.get("pid")):
        return False, False, "", False
    control = descriptor.get("control")
    if not isinstance(control, dict):
        return True, False, "", False
    endpoint = _validated_loopback_origin(
        control.get("endpoint"), "Kênh điều khiển ChatGPT Web"
    )
    token = control.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,}", token):
        return True, False, "", False
    request = urllib.request.Request(
        f"{endpoint}/v1/session/inspect",
        data=b'{"detectCapabilities":false}',
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = _read_limited_json_response(response)
    except (OSError, urllib.error.URLError, ContentAIError):
        # A setup/smoke operation owns the browser exclusively. Do not turn
        # that transient state into a false "session expired" result.
        passive = _chatgpt_web_session_status(min(timeout, 3.0))
        if passive[3]:
            return passive
        return passive[0], False, "", False
    authenticated = (
        value.get("authenticated") is True
        and value.get("saved") is True
        and value.get("temporary") is False
    )
    return True, authenticated, token if authenticated else "", False


def _chatgpt_web_runtime_status(timeout: float) -> tuple[bool, str, str, tuple[str, ...]]:
    config = _read_json_object(_chatgpt_web_config_path())
    if not config:
        return False, "", "", ()
    if config.get("host") != "127.0.0.1":
        raise ContentAIError("ChatGPT Web Bridge không khóa vào 127.0.0.1; đã từ chối kết nối.")
    port = config.get("port", CHATGPT_WEB_DEFAULT_PORT)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ContentAIError("Cổng ChatGPT Web Bridge không hợp lệ.")
    endpoint = f"http://127.0.0.1:{port}"
    request = urllib.request.Request(
        f"{endpoint}/healthz",
        headers={"Cache-Control": "no-store"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            health = _read_limited_json_response(response)
    except (OSError, urllib.error.URLError, ContentAIError):
        return False, endpoint, str(config.get("releaseVersion", "")), ()
    # ``accepting_turns`` is capacity, not health. With several workspaces the
    # Bridge can have all adaptive slots occupied while remaining authenticated
    # and fully operational. Treating that moment as runtime failure made other
    # workspaces show the misleading "Cần thiết lập ChatGPT Web" state instead
    # of waiting for their own slot.
    ready = bool(
        health.get("service") == "docurandom-chat-bridge"
        and health.get("status") == "ok"
    )
    configured_models = config.get("availableModels")
    if isinstance(configured_models, list):
        models = [
            str(model)
            for model in configured_models
            if isinstance(model, str) and model.startswith("chatgpt-web/")
        ]
    elif config.get("solAvailable") is True:
        models = [
            "chatgpt-web/light",
            "chatgpt-web/medium",
            "chatgpt-web/high",
        ]
        if config.get("proAvailable") is True:
            models.extend(("chatgpt-web/extra-high", "chatgpt-web/pro"))
    else:
        models = ["chatgpt-web/luna"]
    health_version = health.get("version")
    version = (
        health_version
        if isinstance(health_version, str) and health_version.strip()
        else str(config.get("releaseVersion", ""))
    )
    return ready, endpoint, version, tuple(models)


def check_chatgpt_web_bridge(
    *,
    auto_start: bool = False,
    passive_session: bool = False,
    timeout: float = 8.0,
) -> ChatGPTWebStatus:
    launcher = discover_chatgpt_web_launcher()
    if launcher is None:
        return ChatGPTWebStatus(
            launcher_path=None,
            detail=(
                f"Chưa cài bản bridge tương thích {CHATGPT_WEB_BRIDGE_VERSION} · "
                f"Electron {CHATGPT_WEB_ELECTRON_VERSION} đã khóa phiên bản."
            ),
        )
    state = _read_json_object(_chatgpt_web_launcher_state_path())
    setup_complete = state.get("coreSetupComplete") is True
    session_check = (
        _chatgpt_web_session_status
        if passive_session
        else _inspect_chatgpt_web_session
    )
    used_live_status_fallback = False
    running, authenticated, access_token, session_busy = session_check(
        min(timeout, 12.0)
    )
    # DOM inspection is intentionally strict so Temporary Chat is rejected,
    # but ChatGPT can change or delay its visible controls independently of
    # the authenticated browser session. If the live, token-protected control
    # endpoint confirms authentication and the bridge itself records normal
    # saved-chat mode, do not report a false expired session to the user.
    if (
        not passive_session
        and running
        and not authenticated
        and not session_busy
        and state.get("authenticated") is True
        and state.get("chatMode") == "saved"
    ):
        passive = _chatgpt_web_session_status(min(timeout, 5.0))
        if passive[0] and passive[1] and not passive[3]:
            running, authenticated, access_token, session_busy = passive
            used_live_status_fallback = True
    if auto_start and not running:
        start_chatgpt_web_launcher(show=False)
        deadline = time.monotonic() + min(15.0, max(3.0, timeout * 2.0))
        while time.monotonic() < deadline:
            time.sleep(0.25)
            running, authenticated, access_token, session_busy = (
                _chatgpt_web_session_status(min(timeout, 5.0))
            )
            runtime_probe, _endpoint, _version, _models = (
                _chatgpt_web_runtime_status(min(timeout, 3.0))
            )
            # Electron exposes its PID/descriptor before ChatGPT has restored
            # cookies and before the HTTP runtime begins accepting turns. Do
            # not turn that normal startup gap into a false expired session.
            if running and runtime_probe and (authenticated or session_busy):
                break
        state = _read_json_object(_chatgpt_web_launcher_state_path())
        setup_complete = state.get("coreSetupComplete") is True
        if not passive_session and authenticated and not session_busy:
            strict = _inspect_chatgpt_web_session(min(timeout, 12.0))
            if strict[1]:
                running, authenticated, access_token, session_busy = strict
            elif state.get("chatMode") == "saved":
                used_live_status_fallback = True
            else:
                running, authenticated, access_token, session_busy = strict
    runtime_ready, endpoint, version, models = _chatgpt_web_runtime_status(
        min(timeout, 5.0)
    )
    outdated = bool(
        running
        and runtime_ready
        and version
        and version != CHATGPT_WEB_BRIDGE_VERSION
    )
    if auto_start and outdated and not session_busy:
        if _stop_outdated_chatgpt_web_bridge():
            start_chatgpt_web_launcher(show=False)
            deadline = time.monotonic() + min(15.0, max(3.0, timeout * 2.0))
            while time.monotonic() < deadline:
                time.sleep(0.25)
                running, authenticated, access_token, session_busy = (
                    _chatgpt_web_session_status(min(timeout, 5.0))
                )
                runtime_ready, endpoint, version, models = _chatgpt_web_runtime_status(
                    min(timeout, 5.0)
                )
                outdated = bool(
                    running
                    and runtime_ready
                    and version
                    and version != CHATGPT_WEB_BRIDGE_VERSION
                )
                if (
                    running
                    and runtime_ready
                    and not outdated
                    and (authenticated or session_busy)
                ):
                    break
            state = _read_json_object(_chatgpt_web_launcher_state_path())
            setup_complete = state.get("coreSetupComplete") is True
            if not passive_session and authenticated and not session_busy:
                strict = _inspect_chatgpt_web_session(min(timeout, 12.0))
                if strict[1]:
                    running, authenticated, access_token, session_busy = strict
                elif state.get("chatMode") == "saved":
                    used_live_status_fallback = True
                else:
                    running, authenticated, access_token, session_busy = strict
    if outdated:
        runtime_ready = False
    if runtime_ready and authenticated and not setup_complete:
        setup_complete = True
    if not running:
        detail = "Bridge đã có sẵn nhưng chưa chạy; bấm Kết nối ChatGPT."
    elif session_busy:
        detail = (
            "Bridge đang hoàn tất một tác vụ nội bộ · tool sẽ tự chờ, "
            "không refresh hoặc báo hết phiên giả."
        )
    elif outdated:
        detail = (
            f"Bridge nền {version} thuộc bản cũ; tool sẽ tự thay bằng "
            f"{CHATGPT_WEB_BRIDGE_VERSION} khi bridge rảnh."
        )
    elif not authenticated:
        detail = "Bridge đang chạy nhưng chưa xác minh được phiên đăng nhập ChatGPT Web."
    elif not setup_complete or not runtime_ready:
        detail = "Đã đăng nhập; bridge đang tự hoàn tất kiểm tra cục bộ."
    else:
        detail = (
            "Chat thường có lưu đã sẵn sàng · bridge nội bộ có mã truy cập · "
            "không dùng API key hoặc hạn mức Codex Work."
        )
        if used_live_status_fallback:
            detail += " · Đã đối chiếu phiên trực tiếp để tránh báo chưa đăng nhập giả."
    account_capabilities: dict[str, object] = {}
    saved_cooldown = _read_json_object(app_data_dir() / "runtime" / "chatgpt-account-cooldown.json")
    try:
        legacy_long_pause = (
            saved_cooldown.get("version") == 1
            and float(saved_cooldown.get("expires_at_epoch", 0)) - time.time() > 5 * 60
        )
    except (TypeError, ValueError):
        legacy_long_pause = False
    if legacy_long_pause and runtime_ready and authenticated and not outdated:
        account_capabilities = _chatgpt_web_account_capabilities(
            endpoint, access_token, timeout=min(timeout, 5.0),
        )
    return ChatGPTWebStatus(
        launcher_path=launcher,
        installed=True,
        launcher_running=running,
        setup_complete=setup_complete,
        authenticated=authenticated,
        runtime_ready=runtime_ready,
        session_busy=session_busy,
        endpoint=endpoint,
        bridge_version=version or CHATGPT_WEB_BRIDGE_VERSION,
        available_models=models,
        detail=detail,
        access_token=access_token,
        account_capabilities=account_capabilities,
    )


def chatgpt_web_model_labels(
    available_models: Iterable[str],
) -> tuple[str, ...]:
    available = set(available_models)
    labels = [
        label
        for label, model in CHATGPT_WEB_MODEL_LABELS.items()
        if model in available
    ]
    return tuple(labels or [CHATGPT_WEB_DEFAULT_MODEL_LABEL])


def resolve_chatgpt_web_model(
    preference: str,
    available_models: Iterable[str],
) -> str:
    available = set(available_models)
    requested = CHATGPT_WEB_MODEL_LABELS.get(preference, preference)
    if preference == "ChatGPT Web · GPT-5.6 Sol · High":
        requested = CHATGPT_WEB_AUTO_MODEL  # Migrate the old fixed UI label.
    if not requested or requested in ("auto", CHATGPT_WEB_AUTO_MODEL):
        if CHATGPT_WEB_AUTO_MODEL in available:
            return CHATGPT_WEB_AUTO_MODEL
        raise ContentAIError("Bridge chưa hỗ trợ giữ model đang chọn trên Web; hãy cập nhật bridge.")
    if requested not in available:
        raise ContentAIError(
            "Tài khoản/phiên ChatGPT hiện tại không cung cấp tuyến model đã yêu cầu; "
            "tool đã dừng để không chạy nhầm model."
        )
    return requested


@dataclass(frozen=True)
class ChatTurn:
    role: str
    text: str


@dataclass(frozen=True)
class ContentArtifacts:
    title: str
    description: str
    thumbnail_prompt: str
    voicevox_script: str
    voicevox_export_script: str
    image_prompts: tuple[str, ...]
    selected_topic: str = ""
    seo_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContentRunResult:
    artifacts: ContentArtifacts
    package_path: Path
    transcript_path: Path
    checkpoint_path: Path
    thread_id: str
    turns: tuple[ChatTurn, ...]


TurnCallback = Callable[[str, str], None]
ProgressCallback = Callable[[float, str], None]
PartialCallback = Callable[[str], None]
StatusCallback = Callable[[str, str], None]


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _candidate_codex_paths() -> Iterable[Path]:
    configured = os.environ.get("DOCURANDOM_CODEX_PATH", "").strip().strip('"')
    if configured:
        yield Path(configured).expanduser()

    # Release builds carry the verified native Codex CLI beside the app. A
    # clean Windows machine therefore does not need a global Node/npm install.
    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        yield Path(bundle_root) / "codex-cli" / "codex.exe"
    yield Path(sys.executable).resolve().parent / "codex-cli" / "codex.exe"
    yield (
        Path(__file__).resolve().parent.parent
        / "third_party"
        / "docurandom-codex"
        / "codex.exe"
    )

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        try:
            candidates = sorted(
                root.glob("*/codex.exe"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            yield from candidates
        except OSError:
            pass

    home = Path.home()
    yield home / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe"
    app_data = os.environ.get("APPDATA")
    if app_data:
        yield Path(app_data) / "com.koh.jpstudio" / "tools" / "codex" / "codex.exe"
        npm_root = Path(app_data) / "npm" / "node_modules" / "@openai" / "codex"
        yield from _native_codex_paths(npm_root)

    yield from _native_codex_paths(app_data_dir() / "codex-cli" / "node_modules" / "@openai" / "codex")

    # Windows App Execution Aliases may resolve to a protected WindowsApps
    # binary that exists but returns WinError 5 to child processes. Prefer the
    # executable copied into Codex Desktop's user-local bin directory.
    discovered = shutil.which("codex")
    if discovered:
        yield Path(discovered)


def _native_codex_paths(package_root: Path) -> Iterable[Path]:
    """Yield native executables installed by the official npm package."""
    patterns = (
        "vendor/*/codex/codex.exe",
        "node_modules/@openai/codex-win32-*/vendor/*/codex/codex.exe",
    )
    for pattern in patterns:
        try:
            yield from package_root.glob(pattern)
        except OSError:
            pass


def _candidate_npm_paths() -> Iterable[Path]:
    for name in ("npm.cmd", "npm"):
        discovered = shutil.which(name)
        if discovered:
            yield Path(discovered)
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if not base:
            continue
        yield Path(base) / "nodejs" / "npm.cmd"
        yield Path(base) / "Programs" / "nodejs" / "npm.cmd"


def _discover_npm() -> Path | None:
    for candidate in _candidate_npm_paths():
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _run_installer(command: list[str], timeout: float) -> str:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContentAIError(f"Không thể chạy trình cài đặt: {exc}") from exc
    output = completed.stdout.strip()
    if completed.returncode != 0:
        raise ContentAIError(output or "Trình cài đặt kết thúc với lỗi.")
    return output


def install_codex_cli(*, timeout: float = 900.0) -> Path:
    """Install the official Codex npm package into DocuRandom's app data."""
    existing = discover_codex_cli()
    if existing is not None:
        return existing

    npm = _discover_npm()
    if npm is None:
        winget = shutil.which("winget")
        if not winget:
            raise ContentAIError(
                "Máy chưa có Node.js và không tìm thấy Windows App Installer (winget). "
                f"Có thể cài thủ công theo {CODEX_CLI_INSTALL_URL}"
            )
        _run_installer(
            [
                winget,
                "install",
                "--exact",
                "--id",
                "OpenJS.NodeJS.LTS",
                "--source",
                "winget",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--silent",
                "--disable-interactivity",
            ],
            timeout,
        )
        npm = _discover_npm()
    if npm is None:
        raise ContentAIError(
            "Đã chuẩn bị Node.js nhưng chưa tìm thấy npm. Hãy khởi động lại tool rồi thử lại."
        )

    install_root = app_data_dir() / "codex-cli"
    install_root.mkdir(parents=True, exist_ok=True)
    _run_installer(
        [
            str(npm),
            "install",
            "--prefix",
            str(install_root),
            "--no-audit",
            "--no-fund",
            CODEX_NPM_PACKAGE,
        ],
        timeout,
    )
    installed = discover_codex_cli()
    if installed is None:
        raise ContentAIError(
            "Codex đã được tải nhưng tool chưa tìm thấy codex.exe. "
            "Hãy khởi động lại tool rồi bấm Kiểm tra lại."
        )
    return installed


def discover_codex_cli() -> Path | None:
    seen: set[str] = set()
    for candidate in _candidate_codex_paths():
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_file():
            return resolved
    return None


def _run_cli_probe(path: Path, arguments: list[str], timeout: float) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            [str(path), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContentAIError(f"Không thể chạy Codex CLI: {exc}") from exc
    return completed.returncode, completed.stdout.strip()


def check_codex_cli(path: Path | None = None, *, timeout: float = 12.0) -> CodexCliStatus:
    resolved = path or discover_codex_cli()
    if resolved is None:
        return CodexCliStatus(
            path=None,
            detail="Chưa tìm thấy Codex CLI trên máy.",
        )

    version_code, version_output = _run_cli_probe(resolved, ["--version"], timeout)
    if version_code != 0:
        return CodexCliStatus(
            path=resolved,
            detail=version_output or "Codex CLI không khởi động được.",
        )

    return CodexCliStatus(
        path=resolved,
        version=version_output,
        logged_in=False,
        login_method="Không yêu cầu",
        detail=(
            f"{version_output} sẵn sàng làm cầu nối local; "
            "không đọc đăng nhập hay token Codex."
        ),
    )


def _codex_home() -> Path:
    # Never inherit ~/.codex or an ambient CODEX_HOME from Codex Desktop. This
    # prevents global auth, MCP, skills and task history from entering an AI
    # editing run that should only relay text to the loopback Web bridge.
    return app_data_dir() / "chatgpt-web" / "docurandom-codex-home"


def ensure_chatgpt_web_codex_profile(
    endpoint: str,
    access_token: str,
    *,
    codex_home: Path | None = None,
) -> Path:
    """Atomically install the app-owned Codex profile for the loopback bridge.

    The user's base config.toml is never changed. The rotating bridge token is
    deliberately not written here; Codex receives it only in its child-process
    environment for the duration of a turn.
    """
    origin = _validated_loopback_origin(endpoint, "ChatGPT Web Bridge")
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,}", access_token):
        raise ContentAIError("Khóa truy cập cục bộ ChatGPT Web không hợp lệ.")
    home = (codex_home or _codex_home()).expanduser().resolve()
    profile_path = home / f"{CODEX_CHATGPT_WEB_PROFILE}.config.toml"
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    disabled_mcp_sections: list[str] = []
    base_config = home / "config.toml"
    try:
        base_value = tomllib.loads(base_config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        base_value = {}
    configured_mcp = base_value.get("mcp_servers")
    if isinstance(configured_mcp, dict):
        for server_name in sorted(
            name for name in configured_mcp if isinstance(name, str) and name.strip()
        ):
            disabled_mcp_sections.append(
                f"[mcp_servers.{quote(server_name)}]\n"
                "enabled = false\n"
            )
    content = (
        "# Managed by DocuRandom Studio. This file is app-scoped.\n"
        "# Global MCP servers are disabled here because AI editing only relays text to ChatGPT Web.\n"
        f"model = {quote(CHATGPT_WEB_AUTO_MODEL)}\n"
        f"model_provider = {quote(CODEX_CHATGPT_WEB_PROVIDER)}\n"
        'model_reasoning_effort = "none"\n'
        'model_reasoning_summary = "none"\n'
        "model_context_window = 1000000\n"
        "model_auto_compact_token_limit = 900000\n"
        "\n"
        f"[model_providers.{CODEX_CHATGPT_WEB_PROVIDER}]\n"
        'name = "DocuRandom ChatGPT Web"\n'
        f"base_url = {quote(origin + '/v1')}\n"
        'wire_api = "responses"\n'
        "requires_openai_auth = false\n"
        "supports_websockets = false\n"
        "stream_idle_timeout_ms = 86400000\n"
        "stream_max_retries = 0\n"
        f"env_key = {quote(CODEX_CHATGPT_WEB_TOKEN_ENV)}\n"
        "\n"
        "[features]\n"
        "apps = false\n"
        "plugins = false\n"
        "remote_plugin = false\n"
        "recommended_plugins = false\n"
        + ("\n" + "\n".join(disabled_mcp_sections) if disabled_mcp_sections else "")
    )
    atomic_write_text(profile_path, content, encoding="utf-8")
    try:
        profile_path.chmod(0o600)
    except OSError:
        pass
    return profile_path


def read_master_prompt(path: Path) -> str:
    raw = path.expanduser().read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1258", "cp1252"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if text.strip():
            return text
    raise ContentAIError("Không thể đọc master prompt bằng định dạng văn bản được hỗ trợ.")


def is_master_prompt_loaded_confirmation(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)
    return normalized == "đãnạp"


def is_master_prompt_attachment_failure(value: str) -> bool:
    """Recognize a Master Prompt turn that did not receive its attached file.

    ChatGPT does not emit one stable refusal. Depending on the model and the
    file-tool state it may say that the attachment is missing, report an empty
    mounted-file list, or finish after only listing/searching files. The
    repair path must catch all of those forms, but must not arm itself merely
    because an answer happens to mention a file. Therefore every match still
    needs attachment/file evidence plus either an explicit absence marker or
    one of the bounded, no-file probe transcripts observed in the old
    checkpoints.
    """

    normalized = " ".join(
        unicodedata.normalize("NFKC", str(value or "")).casefold().split()
    )
    ascii_normalized = (
        unicodedata.normalize("NFKD", normalized)
        .encode("ascii", "ignore")
        .decode("ascii")
    )

    has_attachment = any(
        marker in normalized or marker in ascii_normalized
        for marker in (
            "tệp",
            "tep",
            "file",
            "attachment",
            "attached",
            "uploaded",
            "mounted data",
            "conversation files",
            "available files",
        )
    )
    has_missing = any(
        marker in normalized or marker in ascii_normalized
        for marker in (
            "không xuất hiện",
            "chưa xuất hiện",
            "chưa nhận được",
            "không khả dụng",
            "không thực sự khả dụng",
            "không hiện diện",
            "chưa hiển thị",
            "không tìm thấy",
            "không thấy",
            "không thể đọc",
            "không có trong dữ liệu tệp",
            "không có trong vùng tệp",
            "không có trong vùng làm việc",
            "không có trong dữ liệu",
            "không có tệp",
            "không có file",
            "không có bản sao",
            "không được đính kèm",
            "không được gắn",
            "không có kết quả",
            "đang trống",
            "danh sách ... trống",
            "trả về 0 tệp",
            "0 tệp",
            "0 file",
            "not available",
            "not found",
            "does not appear",
            "did not appear",
            "no files",
            "empty",
            "missing",
            "not present",
            "couldn't access",
            "cannot access",
            "unable to access",
        )
    )

    # A few old turns ended immediately after the file tools were called. They
    # contain no explicit refusal, but they also contain no file contents or
    # attachment confirmation. Keep these markers exact so ordinary prose
    # such as “I will read the attached file” is not treated as a failure.
    unresolved_file_probe = any(
        marker in normalized
        for marker in (
            "listed files for complete sequential reading",
            "listed available conversation files",
            "listing mounted data files",
            "inspected mounted data files",
            "searched for the referenced master prompt file",
            "searching for the missing attachment file",
        )
    )
    return has_attachment and (has_missing or unresolved_file_probe)


def master_prompt_attachment_failure_in_turns(
    turns: Iterable[ChatTurn],
    master_prompt: str,
) -> bool:
    """Return true only when the latest Master Prompt got a missing-file reply."""

    values = list(turns)
    prompt = str(master_prompt or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    master_indices = [
        index
        for index, turn in enumerate(values)
        if turn.role.casefold() == "user"
        and turn.text.replace("\r\n", "\n").replace("\r", "\n").strip() == prompt
    ]
    if not master_indices:
        return False
    latest_master_index = master_indices[-1]
    for turn in values[latest_master_index + 1 :]:
        if turn.role.casefold() == "assistant":
            return is_master_prompt_attachment_failure(turn.text)
    return False


def master_prompt_attachment_retry_pending_in_turns(
    turns: Iterable[ChatTurn],
    master_prompt: str,
) -> bool:
    """Recognize the old safe attachment-repair retry before its response.

    Older builds checkpointed the replacement Master Prompt immediately after
    the first conversation answered that its attachment was missing.  The
    replacement user turn therefore has no assistant reply yet.  A generic
    pending-turn recovery must not treat that turn as an already-submitted
    prompt: it is the known pre-submit attachment repair, and can be moved to
    a fresh local conversation exactly once.
    """

    values = list(turns)
    prompt = str(master_prompt or "").strip()
    if not prompt or not values:
        return False
    if (
        values[-1].role.casefold() != "user"
        or values[-1].text.strip() != prompt
    ):
        return False
    master_indices = [
        index
        for index, turn in enumerate(values)
        if turn.role.casefold() == "user" and turn.text.strip() == prompt
    ]
    if len(master_indices) < 2:
        return False
    previous_index = master_indices[-2]
    between = values[previous_index + 1 : -1]
    # A retry is safe only when the prior Master Prompt has an explicit
    # missing-file response and no other user turn is interleaved.  This keeps
    # ordinary pending turns fail-closed even if an old answer mentions files.
    if any(turn.role.casefold() == "user" for turn in between):
        return False
    return any(
        turn.role.casefold() == "assistant"
        and is_master_prompt_attachment_failure(turn.text)
        for turn in between
    )


def is_content_history_loaded_confirmation(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)
    return normalized == "đãxong"


def is_image_prompt_done_confirmation(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)
    return normalized in {
        "đãxong",
        "đãhoàntất",
        "đãhoànthành",
        "đãhoàntấttoànbộ",
        "đãhoànthànhtoànbộ",
        "đãhoàntấttoànbộworkflow",
        "đãhoànthànhtoànbộworkflow",
    }


def is_image_prompt_not_done_confirmation(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)
    return normalized == "chưaxong"


def _assistant_replies_to_exact_prompt(
    turns: Iterable[ChatTurn],
    prompt: str,
) -> tuple[str, ...]:
    replies: list[str] = []
    awaiting_reply = False
    expected = prompt.strip()
    for turn in turns:
        role = turn.role.casefold()
        if role == "user":
            awaiting_reply = turn.text.strip() == expected
        elif role == "assistant" and awaiting_reply:
            replies.append(turn.text)
            awaiting_reply = False
    return tuple(replies)


def image_completion_confirmed_in_turns(turns: Iterable[ChatTurn]) -> bool:
    return any(
        is_image_prompt_done_confirmation(reply)
        for reply in _assistant_replies_to_exact_prompt(
            turns,
            IMAGE_COMPLETION_QUESTION,
        )
    )


def content_history_loaded_in_turns(turns: Iterable[ChatTurn]) -> bool:
    return any(
        is_content_history_loaded_confirmation(reply)
        for reply in _assistant_replies_to_exact_prompt(
            turns,
            CONTENT_HISTORY_CONFIRMATION_REQUEST,
        )
    )


def final_export_reply_in_turns(turns: Iterable[ChatTurn]) -> str:
    replies: list[str] = []
    awaiting_reply = False
    supported = {prompt.strip() for prompt in _SUPPORTED_FINAL_EXPORT_REQUESTS}
    for turn in turns:
        role = turn.role.casefold()
        if role == "user":
            awaiting_reply = turn.text.strip() in supported
        elif role == "assistant" and awaiting_reply:
            replies.append(turn.text)
            awaiting_reply = False
    return replies[-1] if replies else ""


def image_prompt_phase_started(turns: Iterable[ChatTurn]) -> bool:
    assistant_text = "\n\n".join(
        turn.text for turn in turns if turn.role.casefold() == "assistant"
    )
    return bool(
        re.search(
            r"\[\[\s*prompt-(?:hinh-anh|image-\d+)\s*\]\]",
            assistant_text,
            re.IGNORECASE,
        )
        or re.search(
            r"(?im)^\s*(?:image|ảnh)\s*\d+\s*[.:\-–—]",
            assistant_text,
        )
    )


def _extract_blocks(text: str, name: str) -> list[str]:
    opening=rf"\[\[{re.escape(name)}\]\]"
    pattern = re.compile(
        # A prose mention in an earlier reply must not swallow a real block
        # beginning later. Prefer the innermost complete same-name block.
        rf"{opening}\s*((?:(?!{opening}).)*?)\s*\[\[end-{re.escape(name)}\]\]",
        flags=re.DOTALL | re.IGNORECASE,
    )
    return [match.group(1).strip() for match in pattern.finditer(text) if match.group(1).strip()]


def _extract_part_blocks(text: str) -> list[tuple[int, str]]:
    pattern = re.compile(
        r"\[\[part-(\d+)\]\]\s*(.*?)\s*\[\[end-part-\1\]\]",
        flags=re.DOTALL | re.IGNORECASE,
    )
    parts: dict[int, str] = {}
    for match in pattern.finditer(text):
        value = match.group(2).strip()
        if value:
            # A later manual correction for the same PART replaces the older
            # block while preserving the numeric order of the finished script.
            parts[int(match.group(1))] = value
    return sorted(parts.items())


def _clean_voice_lines(parts: Iterable[tuple[int, str]]) -> list[str]:
    output: list[str] = []
    heading = re.compile(
        r"^\s*(?:phần|part)\s*(?:\d+|[ivxlcdm]+)?\s*[:.\-–—]*\s*$",
        flags=re.IGNORECASE,
    )
    for _number, block in parts:
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or heading.fullmatch(line):
                continue
            line = re.sub(r"^A\s*:\s*", "", line, flags=re.IGNORECASE).strip()
            if line:
                output.append(line)
    return output


def _extract_image_prompts(text: str) -> tuple[str, ...]:
    # MASTER PROMPT variants use both one final [[prompt-hinh-anh]] block and
    # paged blocks such as [[prompt-image-1]], [[prompt-image-2]], ... . Treat
    # them as one numbered sequence; otherwise a complete 1..15 result can look
    # like only 13..15 and trigger an endless ĐÃ XONG/TIẾP confirmation loop.
    block_pattern = re.compile(
        r"\[\[(prompt-hinh-anh|prompt-image-\d+)\]\]\s*((?:(?!\[\[\1\]\]).)*?)\s*\[\[end-\1\]\]",
        flags=re.DOTALL | re.IGNORECASE,
    )
    blocks = [
        (match.group(1).casefold(),match.group(2).strip())
        for match in block_pattern.finditer(text)
        if match.group(2).strip()
    ]
    values: dict[int, str] = {}
    pattern = re.compile(
        r"(?ims)^\s*(?:image|ảnh)\s*(\d+)\s*[.:\-–—]\s*(.*?)"
        r"(?=^\s*(?:image|ảnh)\s*\d+\s*[.:\-–—]|\Z)"
    )
    for name,block in blocks:
        first=pattern.search(block)
        if name=='prompt-hinh-anh'and first and int(first.group(1))==1:
            values.clear()
        for match in pattern.finditer(block):
            number = int(match.group(1))
            prompt = " ".join(
                line.strip() for line in match.group(2).splitlines() if line.strip()
            )
            if prompt:
                values[number] = prompt
    # Only expose the contiguous sequence from IMAGE 1. A response containing
    # IMAGE 1 and IMAGE 3 is not complete merely because it has two prompts;
    # the next automatic TIẾP must collect the missing IMAGE 2 first.
    contiguous: list[str] = []
    number = 1
    while number in values:
        contiguous.append(values[number])
        number += 1
    return tuple(contiguous)


def _parse_numbered_image_prompts(text: str) -> tuple[str, ...]:
    values: dict[int, str] = {}
    pattern = re.compile(
        r"(?ims)^\s*(?:image|ảnh)\s*(\d+)\s*[.\-:–—]\s*(.*?)"
        r"(?=^\s*(?:image|ảnh)\s*\d+\s*[.\-:–—]|\Z)"
    )
    for match in pattern.finditer(text):
        prompt = " ".join(
            line.strip() for line in match.group(2).splitlines() if line.strip()
        )
        if prompt:
            values[int(match.group(1))] = prompt
    output: list[str] = []
    number = 1
    while number in values:
        output.append(values[number])
        number += 1
    return tuple(output)


def _export_sections(text: str) -> dict[str, str]:
    text = re.sub(r"(?m)^\s*(?:\*\*|__)([^\n]+?)(?:\*\*|__)\s*:?[ \t]*$", r"\1", text)
    heading = re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:[■●◆]\s*)?"
        r"(TIÊU\s*ĐỀ|MÔ\s*TẢ|(?:【\s*)?SEO\s*(?:TAGS?|タグ|キーワード)(?:\s*】)?|PROM(?:P|)T\s*THUMBNAIL|"
        r"(?:THOẠI\s*)?VOICEVOX(?:\s*LIỀN\s*MẠCH)?|A\s*:\s*VOICEVOX|PROM(?:P|)T\s*HÌNH\s*ẢNH)"
        r"\s*:?[ \t]*$"
    )
    aliases = {
        "tiêuđề": "title",
        "môtả": "description",
        "seotag": "seo_tags",
        "seotags": "seo_tags",
        "seoタグ": "seo_tags",
        "seoキーワード": "seo_tags",
        "promtthumbnail": "thumbnail",
        "promptthumbnail": "thumbnail",
        "thoạivoicevox": "voice",
        "thoạivoicevoxliềnmạch": "voice",
        "voicevox": "voice",
        "voicevoxliềnmạch": "voice",
        "avoicevox": "voice",
        "promthìnhảnh": "images",
        "prompthìnhảnh": "images",
    }
    # Marked image/narration blocks also terminate plain metadata sections.
    # Otherwise a final SEO TAGS heading can swallow the following numbered
    # image prompts when the response switches back to [[...]] markers.
    boundaries = [(match.start(), match.end(), match.group(1))
                  for match in heading.finditer(text)]
    boundaries.extend((match.start(), match.end(), None) for match in re.finditer(
        r"(?im)^\s*\[\[(?:end-)?[a-z][a-z0-9-]*\]\][ \t]*$", text
    ))
    boundaries.sort(key=lambda item: item[0])
    sections: dict[str, str] = {}
    for index, (start, content_start, label) in enumerate(boundaries):
        if label is None:
            continue
        normalized = re.sub(
            r"[\W_]+",
            "",
            unicodedata.normalize("NFKC", label).casefold(),
            flags=re.UNICODE,
        )
        key = aliases.get(normalized)
        if key is None:
            continue
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
        value = text[content_start:end].strip()
        if value:
            sections[key] = value
    return sections


def _parse_seo_tags(value: str) -> tuple[str, ...]:
    value = "\n".join(line for line in value.splitlines()
                      if not re.fullmatch(r"[\s─━═\-_*]+", line))
    return tuple(
        dict.fromkeys(
            item.strip().lstrip("#")
            for item in re.split(r"[,;、，；\n]+|(?<!\S)#", value)
            if item.strip().lstrip("#")
        )
    )


def extract_voicevox_script_from_package(exported_text: str) -> str:
    """Extract normalized VOICEVOX narration from one aggregate package."""

    voice_lines: list[str] = []
    for raw_line in _export_sections(exported_text).get("voice", "").splitlines():
        line = raw_line.strip()
        if not line or re.fullmatch(
            r"(?:phần|part)\s*(?:\d+|[ivxlcdm]+)?\s*[:.\-–—]*",
            line,
            flags=re.IGNORECASE,
        ):
            continue
        line = re.sub(r"^A\s*:\s*", "", line, flags=re.IGNORECASE).strip()
        if line:
            voice_lines.append(line)
    return "\n".join(voice_lines)


def detect_image_prompt_interval_seconds(master_prompt: str) -> float:
    """Read the image cadence promised by the user's Master Prompt.

    Older builds assumed one image per minute.  Real Master Prompts can specify
    a different cadence (the current Japanese workflow says one image every
    15 seconds), so the prompt itself is the source of truth.
    """

    normalized = unicodedata.normalize("NFKC", master_prompt).casefold()
    decimal = r"(\d+(?:[.,]\d+)?)"
    interval_patterns = (
        rf"(?:mỗi|every)\s*{decimal}\s*(?:giây|seconds?|secs?)",
        rf"(?:one|một)\s+(?:image|ảnh).{{0,24}}?(?:mỗi|every)\s*{decimal}\s*(?:giây|seconds?|secs?)",
        rf"(?:tổng\s+số\s+giây|total\s+seconds?).{{0,80}}?[/÷]\s*{decimal}",
    )
    for pattern in interval_patterns:
        match = re.search(pattern, normalized, flags=re.DOTALL)
        if match:
            try:
                value = float(match.group(1).replace(",", "."))
            except (TypeError, ValueError):
                continue
            if 1.0 <= value <= 3600.0:
                return value

    # Some prompts express the same rule as ``minutes × 4``.  Restrict this
    # fallback to a nearby image/prompt context so unrelated arithmetic in a
    # large Master Prompt cannot change the render contract.
    multiplier = re.search(
        rf"(?:prompt\s*(?:hình|ảnh)|image).{{0,160}}?"
        rf"(?:số\s*phút|minutes?).{{0,50}}?[x×*]\s*{decimal}",
        normalized,
        flags=re.DOTALL,
    )
    if multiplier:
        try:
            per_minute = float(multiplier.group(1).replace(",", "."))
        except (TypeError, ValueError):
            per_minute = 0.0
        if 0.05 <= per_minute <= 60.0:
            return 60.0 / per_minute
    return DEFAULT_IMAGE_PROMPT_INTERVAL_SECONDS


def expected_image_prompt_count(master_prompt: str, minutes: float) -> int:
    interval = detect_image_prompt_interval_seconds(master_prompt)
    expected = math.ceil(float(minutes) * 60.0 / interval)
    return max(1, min(MAX_EXPECTED_IMAGE_PROMPTS, expected))


def merge_exported_artifacts(
    exported_text: str,
    fallback: ContentArtifacts,
    minutes: float,
    *,
    expected_images: int | None = None,
    allowed_missing_images: int = 0,
) -> ContentArtifacts:
    """Normalize ChatGPT's final human-readable export without losing markers."""

    sections = _export_sections(exported_text)
    images = _parse_numbered_image_prompts(sections.get("images", ""))
    expected_images = expected_images or math.ceil(minutes)
    minimum_images = max(1, expected_images - max(0, int(allowed_missing_images)))
    if (
        not minimum_images <= len(images) <= MAX_EXPECTED_IMAGE_PROMPTS
        or len(images) < len(fallback.image_prompts)
    ):
        images = fallback.image_prompts
    exported_voice = extract_voicevox_script_from_package(exported_text)
    voice_script = exported_voice or fallback.voicevox_script
    if (not narration_problem(fallback.voicevox_script)
        and (narration_problem(exported_voice)
             or (len(fallback.voicevox_script) >= 300
                 and len(exported_voice) < len(fallback.voicevox_script) * 0.80))):
        voice_script = fallback.voicevox_script
    return ContentArtifacts(
        title=sections.get("title", "").strip() or fallback.title,
        description=sections.get("description", "").strip() or fallback.description,
        thumbnail_prompt=sections.get("thumbnail", "").strip() or fallback.thumbnail_prompt,
        voicevox_script=voice_script,
        voicevox_export_script="\n".join(
            f"A: {line}" for line in voice_script.splitlines() if line.strip()
        ),
        image_prompts=images,
        selected_topic=fallback.selected_topic,
        seo_tags=_parse_seo_tags(sections.get("seo_tags", "")) or fallback.seo_tags,
    )


def _marked_content_artifacts(assistant_text: str) -> ContentArtifacts:
    titles = _extract_blocks(assistant_text, "tieu-de-duoc-chon")
    descriptions = _extract_blocks(assistant_text, "mo-ta")
    thumbnails = _extract_blocks(assistant_text, "prompt-thumbnail")
    topics = _extract_blocks(assistant_text, "chu-de-con-duoc-chon")
    voice_lines = _clean_voice_lines(_extract_part_blocks(assistant_text))
    return ContentArtifacts(
        title=titles[-1] if titles else "",
        description=descriptions[-1] if descriptions else "",
        thumbnail_prompt=thumbnails[-1] if thumbnails else "",
        voicevox_script="\n".join(voice_lines),
        voicevox_export_script="\n".join(f"A: {line}" for line in voice_lines),
        image_prompts=_extract_image_prompts(assistant_text),
        selected_topic=topics[-1] if topics else "",
        seo_tags=(),
    )


def extract_content_artifacts(turns: Iterable[ChatTurn], minimum_characters: int = 0) -> ContentArtifacts:
    saved = list(turns)
    # A setup reply can echo every example in the master prompt. None of it
    # belongs to the video created after the BẮT ĐẦU command.
    start = next((i for i, turn in enumerate(saved) if turn.role.casefold() == "user"
                  and turn.text.strip().startswith("BẮT ĐẦU")), 0)
    normal_texts = []
    repair_texts = []
    awaiting_repair = False
    for turn in saved[start:]:
        if turn.role.casefold() == "user":
            awaiting_repair = turn.text.startswith(CONTENT_REPAIR_PREFIX)
        elif turn.role.casefold() == "assistant" and awaiting_repair:
            repair_texts.append(turn.text)
            awaiting_repair = False
        elif turn.role.casefold() == "assistant":
            normal_texts.append(turn.text)
    artifacts = _marked_content_artifacts("\n\n".join(normal_texts))
    for text in normal_texts:
        sections = _export_sections(text)
        if sections.get("voice"):
            artifacts = merge_exported_artifacts(text, artifacts, 1, expected_images=1)
    repaired = _marked_content_artifacts("\n\n".join(repair_texts))
    for text in repair_texts:
        repaired = merge_exported_artifacts(text, repaired, 1, expected_images=1)
    # Repairs only fill missing fields. A later canonical/manual correction
    # takes precedence, and a re-exported whole script must not overwrite PART
    # 1 and append the old PART 2..N a second time.
    return _fill_missing_content_artifacts(artifacts, repaired, minimum_characters)


def _content_continue_count(turns: Iterable[ChatTurn]) -> int:
    """The ordinary continuation budget belongs to the saved video, not a retry."""
    return sum(t.role.casefold() == "user" and t.text.strip().casefold() == "tiếp"
               for t in turns)


def _content_continuations_stalled(turns: Iterable[ChatTurn]) -> bool:
    """Detect three rewrites of existing fields/PARTs, including after restart."""
    saved = list(turns)
    replies = [i for i, t in enumerate(saved) if t.role.casefold() == "assistant"][-3:]
    if len(replies) != 3 or any(
        i == 0 or saved[i - 1].role.casefold() != "user"
        or saved[i - 1].text.strip().casefold() != "tiếp" for i in replies
    ):
        return False

    def coverage(end: int) -> tuple[object, ...]:
        prefix = saved[:end]
        artifacts = extract_content_artifacts(prefix)
        parts = frozenset(
            int(n) for t in prefix if t.role.casefold() == "assistant"
            for n in re.findall(r"\[\[\s*part-(\d+)\s*\]\]", t.text, re.IGNORECASE)
        )
        # Rewording or lengthening PART 1 cannot advance to missing metadata
        # or images. New PART numbers, fields and image counts can.
        return (bool(artifacts.title), bool(artifacts.description),
                bool(artifacts.thumbnail_prompt), bool(artifacts.voicevox_script),
                parts, len(artifacts.image_prompts))

    before = coverage(replies[0] - 1)
    for index in replies:
        after = coverage(index + 1)
        if after != before:
            return False
        before = after
    return True


def _fill_missing_content_artifacts(
    artifacts: ContentArtifacts,
    repaired: ContentArtifacts,
    minimum_characters: int = 0,
) -> ContentArtifacts:
    """Keep validated content; recovery may only fill gaps or extend images."""
    problems=_artifact_problems(artifacts)
    voice = (artifacts.voicevox_script if not narration_problem(artifacts.voicevox_script, minimum_characters) and 'voice' not in problems else repaired.voicevox_script)
    return ContentArtifacts(
        title=artifacts.title or repaired.title,
        description=(artifacts.description if artifacts.description and 'description' not in problems else repaired.description),
        thumbnail_prompt=(artifacts.thumbnail_prompt if artifacts.thumbnail_prompt and 'thumbnail' not in problems else repaired.thumbnail_prompt),
        voicevox_script=voice,
        voicevox_export_script="\n".join(f"A: {line}" for line in voice.splitlines()),
        image_prompts=(repaired.image_prompts
                       if len(repaired.image_prompts) > len(artifacts.image_prompts) or 'images' in problems
                       else artifacts.image_prompts),
        selected_topic=artifacts.selected_topic or repaired.selected_topic,
        seo_tags=artifacts.seo_tags or repaired.seo_tags,
    )


def artifacts_complete(
    artifacts: ContentArtifacts,
    minutes: float,
    *,
    expected_images: int | None = None,
    allowed_missing_images: int = 0,
    minimum_characters: int = 0,
) -> bool:
    expected_images = expected_images or math.ceil(minutes)
    minimum_images = max(1, expected_images - max(0, int(allowed_missing_images)))
    return bool(
        artifacts.title
        and artifacts.description
        and artifacts.thumbnail_prompt
        and artifacts.voicevox_script
        and not narration_problem(artifacts.voicevox_script, minimum_characters)
        and not _artifact_problems(artifacts)
        and minimum_images <= len(artifacts.image_prompts) <= MAX_EXPECTED_IMAGE_PROMPTS
    )


def format_content_package(artifacts: ContentArtifacts) -> str:
    image_text = "\n\n".join(
        f"Image {index}. {prompt}"
        for index, prompt in enumerate(artifacts.image_prompts, 1)
    )
    return (
        "TIÊU ĐỀ\n\n"
        f"{artifacts.title.strip()}\n\n"
        "MÔ TẢ\n\n"
        f"{artifacts.description.strip()}\n\n"
        "SEO TAGS\n\n"
        f"{', '.join(artifacts.seo_tags)}\n\n"
        "PROMPT THUMBNAIL\n\n"
        f"{artifacts.thumbnail_prompt.strip()}\n\n"
        "THOẠI VOICEVOX\n\n"
        f"{artifacts.voicevox_export_script.strip()}\n\n"
        "PROMPT HÌNH ẢNH\n\n"
        f"{image_text.strip()}\n"
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(text, encoding="utf-8-sig")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _master_message_matches(first_user: str, master_prompt: str) -> bool:
    """A saved analytics data suffix must not make Resume send the Master again."""
    first = first_user.strip()
    master = master_prompt.strip()
    if first == master:
        return True
    if not first.startswith(master):
        return False
    suffix = first[len(master):]
    suffix = suffix.strip()
    opening = '[DOCURANDOM_CHANNEL_CONTEXT_DATA]\n'
    closing = '\n[/DOCURANDOM_CHANNEL_CONTEXT_DATA]'
    return bool(suffix.startswith(opening) and suffix.endswith(closing)
                and suffix.count('[DOCURANDOM_CHANNEL_CONTEXT_DATA]') == 1
                and suffix.count('[/DOCURANDOM_CHANNEL_CONTEXT_DATA]') == 1)


def load_completed_content_checkpoint(
    *,
    output_dir: Path,
    project_name: str,
    minutes: float,
    master_prompt: str,
    minimum_characters: int = 0,
) -> ContentRunResult | None:
    """Restore a completed AI package without starting Codex or ChatGPT.

    This is used by the one-click pipeline after a downstream interruption
    (for example a Flow CAPTCHA).  The checkpoint is accepted only when its
    project, duration and complete Master Prompt match the current workspace.
    """

    output_dir = output_dir.expanduser().resolve(strict=False)
    normalized_name = normalize_output_name(project_name)
    checkpoint_path = output_dir / f"{normalized_name}_AI_CHECKPOINT.json"
    transcript_path = output_dir / f"{normalized_name}_CHAT.txt"
    package_path = output_dir / f"{normalized_name}.txt"
    try:
        raw = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        if str(raw.get("project_name", "")) != normalized_name:
            return None
        if abs(float(raw.get("minutes", 0.0)) - float(minutes)) > 1e-9:
            return None
        current_hash = hashlib.sha256(master_prompt.strip().encode("utf-8")).hexdigest()
        stored_hash = str(raw.get("master_prompt_sha256", "")).strip()
        if stored_hash and stored_hash != current_hash:
            return None
        raw_turns = raw.get("turns")
        if not isinstance(raw_turns, list):
            return None
        turns = tuple(
            ChatTurn(str(item["role"]), str(item["text"]))
            for item in raw_turns
            if isinstance(item, dict) and "role" in item and "text" in item
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    first_user = next(
        (turn.text for turn in turns if turn.role.casefold() == "user"),
        "",
    )
    if not turns or not _master_message_matches(first_user, master_prompt):
        return None
    minimum_characters = max(minimum_characters, int(raw.get("minimum_narration_characters", 0) or 0))
    artifacts = extract_content_artifacts(turns, minimum_characters)
    confirmed = raw.get("content_locally_validated") is True or any(
        turn.role.casefold() == "assistant"
        and is_image_prompt_done_confirmation(turn.text)
        for turn in reversed(turns)
    )
    expected_images = expected_image_prompt_count(master_prompt, float(minutes))
    try:
        package_text = package_path.read_text(encoding="utf-8-sig")
    except OSError:
        package_text = ""
    package_title = _export_sections(package_text).get("title", "").strip()
    if package_title and artifacts.title and package_title != artifacts.title.strip():
        package_text = ""
    if package_text:
        artifacts = merge_exported_artifacts(
            package_text,
            artifacts,
            float(minutes),
            expected_images=expected_images,
            allowed_missing_images=DEFAULT_ALLOWED_MISSING_IMAGE_PROMPTS,
        )
    if not confirmed or not artifacts_complete(
        artifacts,
        float(minutes),
        expected_images=expected_images,
        allowed_missing_images=DEFAULT_ALLOWED_MISSING_IMAGE_PROMPTS,
        minimum_characters=minimum_characters,
    ):
        return None
    from . import output_contract
    if output_contract.enabled(master_prompt):
        try:
            expected = output_contract.identity(master_prompt, str(raw.get('output_contract_id', '')), expected_images)
        except ValueError:
            return None
        if (hashlib.sha256(package_path.read_bytes()).hexdigest() != raw.get('output_contract_package_sha256')
                or output_contract.problems(expected, artifacts, turns)):
            return None
    _atomic_write_text(package_path, format_content_package(artifacts))
    return ContentRunResult(
        artifacts=artifacts,
        package_path=package_path,
        transcript_path=transcript_path,
        checkpoint_path=checkpoint_path,
        thread_id=str(raw.get("thread_id", "")).strip(),
        turns=turns,
    )


def _format_transcript(turns: Iterable[ChatTurn]) -> str:
    labels = {"user": "BẠN", "assistant": "TRỢ LÝ", "system": "HỆ THỐNG"}
    chunks = []
    for turn in turns:
        label = labels.get(turn.role.casefold(), turn.role.upper())
        chunks.append(f"===== {label} =====\n{turn.text}")
    return "\n\n".join(chunks)


class CodexCliChat:
    """A cancellable multi-turn wrapper over the signed-in Codex CLI."""

    def __init__(
        self,
        executable: Path,
        working_dir: Path,
        *,
        access_token: str,
        endpoint: str,
        codex_home: Path | None = None,
        profile: str = CODEX_CHATGPT_WEB_PROFILE,
        model: str = CHATGPT_WEB_AUTO_MODEL,
    ) -> None:
        self.executable = executable.expanduser().resolve()
        self.working_dir = working_dir.expanduser().resolve()
        self.profile = profile.strip()
        self.model = model.strip()
        if not self.profile or not self.model.startswith("chatgpt-web/"):
            raise ContentAIError("Profile/model Codex ChatGPT Web không hợp lệ.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,}", access_token):
            raise ContentAIError("Khóa truy cập cục bộ ChatGPT Web không hợp lệ.")
        self._access_token = access_token
        self.endpoint = _validated_loopback_origin(endpoint, "ChatGPT Web Bridge")
        self.codex_home = (codex_home or _codex_home()).expanduser().resolve()
        self._bridge_port = urllib.parse.urlsplit(self.endpoint).port
        self.thread_id = ""
        self._process_lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._recovery_connection: http.client.HTTPConnection | None = None

    def _poll_bridge_activity(
        self,
        stop_event: threading.Event,
        *,
        on_partial: Callable[[str], None] | None,
        on_status: Callable[[str, str], None] | None,
        on_stalled: Callable[[str, str, str], None] | None = None,
        on_startup_stalled: Callable[[str], None] | None = None,
        startup_started_at: float | None = None,
        startup_last_activity: list[float] | None = None,
        startup_stale_after: float = CHATGPT_WEB_STARTUP_STALE_SECONDS,
        poll_interval: float = CHATGPT_WEB_ACTIVITY_POLL_SECONDS,
        stale_after: float = CHATGPT_WEB_HEARTBEAT_STALE_SECONDS,
        no_response_stale_after: float = CHATGPT_WEB_NO_RESPONSE_STALE_SECONDS,
        input_progress_stale_after: float = CHATGPT_WEB_INPUT_PROGRESS_STALE_SECONDS,
    ) -> None:
        last_text = ""
        last_status: tuple[str, str] | None = None
        observed_thread_id = ""
        last_heartbeat_at: float | None = None
        last_updated_at = 0
        last_turn_id = ""
        last_bridge_activity_updated_at = 0
        bridge_no_response_evidence = False
        last_no_response_evidence = False
        input_phase = ""
        input_progress_at: float | None = None
        input_progress_clock = 0
        # The health probe is part of the watchdog loop.  Do not let a
        # connection attempt outlive the loop's polling cadence: on Windows a
        # filtered loopback port can take the full socket timeout even when no
        # bridge is listening, which would otherwise defeat the startup stall
        # detector and leave a workspace occupied indefinitely.
        bridge_probe_timeout = min(1.5, max(0.1, float(poll_interval)))
        while not stop_event.wait(max(0.05, float(poll_interval))):
            thread_id = self.thread_id
            if not thread_id:
                # The old watchdog started only after thread.started.  If the
                # CLI/bridge stalls before that event, the worker and the
                # shared ChatGPT slot could remain occupied forever.  Watch
                # this pre-thread phase separately, but never impose a limit
                # on a real ChatGPT generation.
                if startup_started_at is not None and self._bridge_port:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", self._bridge_port, timeout=bridge_probe_timeout
                    )
                    try:
                        connection.request(
                            "GET",
                            "/v1/session/status",
                            headers={
                                "Accept": "application/json",
                                "Authorization": f"Bearer {self._access_token}",
                            },
                        )
                        response = connection.getresponse()
                        raw = response.read(64 * 1024)
                        if response.status == 200:
                            value = json.loads(raw.decode("utf-8"))
                            active_turns = int(value.get("active_turns") or 0)
                            activity_updated_at = int(
                                value.get("activity_updated_at") or 0
                            )
                            heartbeat_updated_at = int(
                                value.get("updated_at") or activity_updated_at or 0
                            )
                            bridge_no_response_evidence = bool(
                                value.get("no_response_evidence") is True
                            )
                            if (
                                active_turns > 0
                                and heartbeat_updated_at > last_bridge_activity_updated_at
                            ):
                                last_bridge_activity_updated_at = heartbeat_updated_at
                                if startup_last_activity is not None:
                                    startup_last_activity[0] = time.monotonic()
                            # ``active_turns`` alone is not a heartbeat.  The
                            # previous fallback refreshed this watchdog on every
                            # poll while the bridge's timestamps were frozen, so
                            # a lost CLI/thread event could occupy the shared slot
                            # forever.  Only a newer bridge timestamp is evidence
                            # that the turn is still being serviced.
                    except (
                        OSError,
                        ValueError,
                        TypeError,
                        json.JSONDecodeError,
                        http.client.HTTPException,
                    ):
                        pass
                    finally:
                        connection.close()
                if startup_started_at is not None:
                    last_activity = (
                        startup_last_activity[0]
                        if startup_last_activity
                        else startup_started_at
                    )
                    configured_limit = (
                        CHATGPT_WEB_STARTUP_NO_RESPONSE_STALE_SECONDS
                        if bridge_no_response_evidence
                        else startup_stale_after
                    )
                    startup_limit = max(0.05, float(configured_limit))
                    if time.monotonic() - last_activity >= startup_limit:
                        message = (
                            "ChatGPT Web chưa cung cấp thread có thể phục hồi hoặc bridge "
                            "không còn heartbeat trong "
                            f"{int(startup_limit)} giây. Bridge/CLI có thể đã treo; "
                            "chưa xác nhận prompt đã được gửi hoặc thu hồi. Checkpoint được giữ ở "
                            "trạng thái chưa chắc chắn và tool sẽ không tự gửi lại prompt."
                        )
                        if on_startup_stalled is not None:
                            on_startup_stalled(message)
                        return
                continue
            if not self._bridge_port:
                continue
            now = time.monotonic()
            if thread_id != observed_thread_id:
                observed_thread_id = thread_id
                last_heartbeat_at = now
                last_updated_at = 0
                last_turn_id = ""
            # `updated_at` is a UI/status heartbeat and changes while the
            # page merely says "đang suy nghĩ".  Only the bridge's
            # `activity_updated_at` is evidence of actual response progress.
            # An explicit empty-DOM state gets a longer, bounded recovery
            # window so the bridge can reload the exact conversation once.
            stale_limit = max(
                0.05,
                float(
                    no_response_stale_after
                    if last_no_response_evidence
                    else stale_after
                ),
            )
            if (
                last_heartbeat_at is not None
                and now - last_heartbeat_at >= stale_limit
            ):
                message = (
                    "Phản hồi ChatGPT đã vượt giới hạn an toàn do ChatGPT Web Bridge "
                    f"mất heartbeat trong {int(stale_limit)} giây; "
                    "đây không phải giới hạn thời lượng suy nghĩ. Checkpoint vẫn được giữ, "
                    "không gửi lại prompt."
                )
                if on_stalled is not None:
                    on_stalled(message, thread_id, last_turn_id)
                return
            connection = http.client.HTTPConnection(
                "127.0.0.1", self._bridge_port, timeout=1.5
            )
            try:
                query = urllib.parse.urlencode({"thread_id": thread_id})
                connection.request(
                    "GET",
                    f"/v1/turns/status?{query}",
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self._access_token}",
                    },
                )
                response = connection.getresponse()
                raw = response.read(CHATGPT_WEB_MAX_RESPONSE_BYTES + 1)
                if response.status != 200 or len(raw) > CHATGPT_WEB_MAX_RESPONSE_BYTES:
                    continue
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict) or value.get("active") is not True:
                    continue
                now = time.monotonic()
                activity_updated_at = int(value.get("activity_updated_at") or 0)
                heartbeat_updated_at = int(
                    value.get("updated_at") or activity_updated_at or 0
                )
                last_no_response_evidence = (
                    value.get("no_response_evidence") is True
                )
                current_turn_id = str(value.get("turn_id") or "").strip()
                if current_turn_id:
                    last_turn_id = current_turn_id
                if (
                    last_heartbeat_at is None
                    or heartbeat_updated_at > last_updated_at
                ):
                    last_heartbeat_at = now
                    last_updated_at = heartbeat_updated_at
                stage = str(value.get("stage", "")).strip()
                message = str(value.get("message", "")).strip()
                phase = stage.split(":", 1)[0]
                # Elapsed-time status messages prove IPC liveness, not actual
                # input progress. Bound only these pre-submit operations;
                # long model thinking remains governed by heartbeat liveness.
                if phase in {"inline-import", "inline-import-progress", "trusted-input-queued"}:
                    phase = "inline-import" if phase.startswith("inline-import") else phase
                    if phase != input_phase or activity_updated_at > input_progress_clock:
                        input_progress_at = now
                        input_progress_clock = activity_updated_at
                    input_phase = phase
                    if (input_progress_at is not None and
                            now - input_progress_at >= max(0.05, float(input_progress_stale_after))):
                        if on_stalled is not None:
                            on_stalled(
                                "CHATGPT_INPUT_PROGRESS_STALLED: Thao tác nhập ChatGPT không có tiến triển; "
                                "giải phóng lượt, giữ checkpoint và kiểm tra trạng thái gửi trước khi nối lại.",
                                thread_id, last_turn_id,
                            )
                        return
                else:
                    input_phase = ""
                    input_progress_at = None
                current_status = (stage, message)
                if on_status is not None and message and current_status != last_status:
                    on_status(stage or "chatgpt-running", message)
                    last_status = current_status
                text = str(value.get("text", ""))
                if on_partial is not None and text and text != last_text:
                    on_partial(text)
                    last_text = text
            except (OSError, ValueError, json.JSONDecodeError, http.client.HTTPException):
                pass
            finally:
                connection.close()

    def _cancel_remote_turn(
        self,
        thread_id: str,
        turn_id: str,
        reason: str,
    ) -> bool:
        """Cancel one exact remote turn before releasing the local queue slot."""

        if not thread_id or not turn_id or not self._bridge_port:
            return False
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self._bridge_port,
            timeout=CHATGPT_WEB_HEARTBEAT_CANCEL_TIMEOUT_SECONDS,
        )
        body = json.dumps(
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "reason": reason,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            connection.request(
                "POST",
                "/v1/turns/cancel",
                body=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-store",
                    "Authorization": f"Bearer {self._access_token}",
                },
            )
            response = connection.getresponse()
            response.read(64 * 1024)
            return response.status in (200, 202)
        except (OSError, http.client.HTTPException, ValueError, TypeError):
            return False
        finally:
            connection.close()

    def cancel(self) -> None:
        with self._process_lock:
            process = self._process
            recovery_connection = self._recovery_connection
        if recovery_connection is not None:
            try:
                recovery_connection.close()
            except OSError:
                pass
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def recover_pending(
        self,
        *,
        cancel_event: threading.Event | None = None,
        on_partial: Callable[[str], None] | None = None,
        on_status: Callable[[str, str], None] | None = None,
    ) -> str | None:
        """Recover a submitted-but-uncheckpointed answer from its exact DOM."""

        if not self.thread_id or not self._bridge_port:
            return None
        if cancel_event is not None and cancel_event.is_set():
            raise ContentAICancelled("Đã hủy tác vụ viết nội dung.")
        activity_stop = threading.Event()
        activity_thread = threading.Thread(
            target=self._poll_bridge_activity,
            kwargs={
                "stop_event": activity_stop,
                "on_partial": on_partial,
                "on_status": on_status,
            },
            daemon=True,
            name="CodexChatGPTRecoveryActivity",
        )
        # ``/v1/turns/recover`` is a long-poll owned by the local bridge.  The
        # bridge may need to wait for a slow ChatGPT turn and then reload the
        # exact conversation once when the DOM has no response evidence.  A
        # normal 15-second socket timeout aborts that recovery before the
        # bridge can perform its evidence-based reload and leaves the
        # workspace stuck in ``Request cancelled``.  Keep this connection
        # open until the bridge returns; ``cancel()`` closes the stored
        # connection explicitly when the user cancels the workspace.
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self._bridge_port,
            timeout=None,
        )
        with self._process_lock:
            self._recovery_connection = connection
        activity_thread.start()
        try:
            if on_status is not None:
                on_status(
                    "recovering-dom",
                    "Đang kiểm tra đúng DOM của phiên ChatGPT trước khi gửi thêm tin nhắn…",
                )
            payload = json.dumps({"thread_id": self.thread_id}).encode("utf-8")
            connection.request(
                "POST",
                "/v1/turns/recover",
                body=payload,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
            )
            response = connection.getresponse()
            raw = response.read(CHATGPT_WEB_MAX_RESPONSE_BYTES + 1)
            if cancel_event is not None and cancel_event.is_set():
                raise ContentAICancelled("Đã hủy tác vụ viết nội dung.")
            if len(raw) > CHATGPT_WEB_MAX_RESPONSE_BYTES:
                raise ContentAIError("Phản hồi DOM phục hồi vượt giới hạn an toàn.")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ContentAIError("Bridge trả dữ liệu phục hồi DOM không hợp lệ.") from exc
            if response.status != 200:
                message = ""
                if isinstance(value, dict):
                    error = value.get("error")
                    if isinstance(error, dict):
                        message = str(error.get("message", "")).strip()
                raise ContentAIError(
                    message or f"Không thể phục hồi DOM ChatGPT (HTTP {response.status})."
                )
            if isinstance(value, dict) and value.get("status") == "not-submitted":
                return None
            raw_text = value.get("output_text", "") if isinstance(value, dict) else ""
            text = raw_text if isinstance(raw_text, str) else str(raw_text or "")
            if not text.strip():
                raise ContentAIError("DOM ChatGPT đã phục hồi nhưng phản hồi đang trống.")
            return text
        except (OSError, http.client.HTTPException) as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise ContentAICancelled("Đã hủy tác vụ viết nội dung.") from exc
            raise ContentAIError(
                "Mất kết nối khi đang thu hồi phản hồi từ DOM ChatGPT; checkpoint vẫn được giữ."
            ) from exc
        finally:
            activity_stop.set()
            activity_thread.join(timeout=2)
            connection.close()
            with self._process_lock:
                if self._recovery_connection is connection:
                    self._recovery_connection = None

    @staticmethod
    def _is_recoverable_submitted_error(message: str) -> bool:
        lowered = str(message or "").casefold()
        if "chatgpt_account_load_failed" in lowered:
            return False
        return any(
            marker in lowered
            for marker in (
                "checkpoint vẫn được giữ",
                "checkpoint đã được giữ",
                "không gửi lại prompt",
                "đã chuyển khỏi đúng cuộc hội thoại",
                "không tạo dom phản hồi",
                "không có bằng chứng phản hồi",
                "the prompt was accepted",
            )
        )

    def _command(self, output_path: Path) -> list[str]:
        configured = [
            str(self.executable),
            "--profile",
            self.profile,
            "--model",
            self.model,
        ]
        shared = [
            "--json",
            "--skip-git-repo-check",
            "--ignore-rules",
            "-o",
            str(output_path),
        ]
        if self.thread_id:
            return [
                *configured,
                "exec",
                "resume",
                self.thread_id,
                "-",
                *shared,
            ]
        return [
            *configured,
            "exec",
            "-",
            "--sandbox",
            "read-only",
            "-C",
            str(self.working_dir),
            *shared,
        ]

    def send(
        self,
        prompt: str,
        *,
        cancel_event: threading.Event | None = None,
        on_partial: Callable[[str], None] | None = None,
        on_status: Callable[[str, str], None] | None = None,
        attachments: Iterable[ChatAttachment] = (),
    ) -> str:
        if not prompt.strip():
            raise ContentAIError("Tin nhắn gửi cho AI đang trống.")
        output_handle = tempfile.NamedTemporaryFile(
            prefix="DocuRandom_AI_", suffix=".txt", delete=False
        )
        output_path = Path(output_handle.name)
        output_handle.close()
        stderr_lines: list[str] = []
        structured_errors: list[str] = []
        messages: list[str] = []
        message_snapshots: dict[str, str] = {}
        watchdog_error: list[str] = []
        watchdog_lock = threading.Lock()
        process: subprocess.Popen[str] | None = None
        activity_stop = threading.Event()
        activity_thread: threading.Thread | None = None
        startup_started_at: float | None = None
        startup_last_activity: list[float] | None = None
        try:
            if on_status is not None:
                on_status("codex-starting", "Đang mở Codex CLI với model ChatGPT Web riêng…")
            process_environment = os.environ.copy()
            process_environment[CODEX_CHATGPT_WEB_TOKEN_ENV] = self._access_token
            process_environment["CODEX_HOME"] = str(self.codex_home)
            # The bridge reports turn failures through JSONL stdout. Routine
            # fallback-model and PowerShell snapshot warnings are not task
            # failures and should not replace that actionable message in UI.
            process_environment["RUST_LOG"] = "error"
            process = subprocess.Popen(
                self._command(output_path),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_creation_flags(),
                env=process_environment,
            )
            with self._process_lock:
                self._process = process
            if not self.thread_id:
                startup_started_at = time.monotonic()
                startup_last_activity = [startup_started_at]

            def set_watchdog_error(message: str) -> bool:
                with watchdog_lock:
                    if watchdog_error:
                        return False
                    watchdog_error.append(message)
                    return True

            def on_stalled(message: str, thread_id: str, turn_id: str) -> None:
                if not set_watchdog_error(message):
                    return
                self._cancel_remote_turn(thread_id, turn_id, message)
                self.cancel()

            def on_startup_stalled(message: str) -> None:
                # There is no exact remote thread to cancel yet.  Closing the
                # CLI response lets the bridge's pre-submission grace timer
                # release the browser turn without guessing or resending.
                if set_watchdog_error(message):
                    self.cancel()

            activity_thread = threading.Thread(
                target=self._poll_bridge_activity,
                kwargs={
                    "stop_event": activity_stop,
                    "on_partial": on_partial,
                    "on_status": on_status,
                    "on_stalled": on_stalled,
                    "on_startup_stalled": on_startup_stalled,
                    "startup_started_at": startup_started_at,
                    "startup_last_activity": startup_last_activity,
                },
                daemon=True,
                name="CodexChatGPTActivity",
            )
            activity_thread.start()

            def drain_stderr() -> None:
                if process is None or process.stderr is None:
                    return
                for line in process.stderr:
                    stderr_lines.append(line.rstrip())

            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stderr_thread.start()
            if process.stdin is None or process.stdout is None:
                raise ContentAIError("Không thể mở kênh giao tiếp với Codex CLI.")
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                raise ContentAIError("Codex CLI đóng trước khi nhận master prompt.") from exc

            for line in process.stdout:
                if cancel_event is not None and cancel_event.is_set():
                    self.cancel()
                    raise ContentAICancelled("Đã hủy tác vụ viết nội dung.")
                line = line.strip()
                if not line:
                    continue
                if startup_last_activity is not None:
                    startup_last_activity[0] = time.monotonic()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                structured_error = _codex_event_error_message(event)
                if structured_error and (
                    not structured_errors or structured_errors[-1] != structured_error
                ):
                    structured_errors.append(structured_error)
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    self.thread_id = str(event["thread_id"])
                    if on_status is not None:
                        on_status("codex-thread", "Codex đã tạo phiên và đang chờ ChatGPT phản hồi…")
                item = event.get("item")
                if (
                    event.get("type") in {"item.started", "item.updated", "item.completed"}
                    and isinstance(item, dict)
                    and item.get("type") == "agent_message"
                    and item.get("text")
                ):
                    item_id = str(item.get("id") or "assistant")
                    message_snapshots[item_id] = str(item["text"])
                    if on_partial is not None:
                        on_partial("\n\n".join(message_snapshots.values()).strip())
                if event.get("type") == "item.completed" and isinstance(item, dict):
                    if item.get("type") == "agent_message" and item.get("text"):
                        text_value = str(item["text"])
                        if text_value not in messages:
                            messages.append(text_value)
                if event.get("type") == "turn.completed" and on_status is not None:
                    on_status("codex-completed", "Codex đã nhận tín hiệu hoàn tất từ ChatGPT Web.")

            return_code = process.wait()
            stderr_thread.join(timeout=1)
            if watchdog_error:
                # The watchdog already cancelled the exact remote turn and
                # interrupted this local CLI process. Never submit the prompt
                # again after this fail-closed path.
                if not self.thread_id:
                    raise ContentAISubmissionUncertain(watchdog_error[0])
                raise ContentAIError(watchdog_error[0])
            if cancel_event is not None and cancel_event.is_set():
                raise ContentAICancelled("Đã hủy tác vụ viết nội dung.")
            if return_code != 0:
                detail = (
                    structured_errors[-1]
                    if structured_errors
                    else _codex_stderr_error_detail(stderr_lines)
                )
                if self.thread_id and self._is_recoverable_submitted_error(detail):
                    if on_status is not None:
                        on_status(
                            "recovering-dom",
                            "Codex đã nhận lượt nhưng chưa nhận được output; đang thu hồi từ đúng cuộc hội thoại, không gửi lại prompt…",
                        )
                    try:
                        recovered = self.recover_pending(
                            cancel_event=cancel_event,
                            on_partial=on_partial,
                            on_status=on_status,
                        )
                    except ContentAICancelled:
                        raise
                    except ContentAIError:
                        recovered = None
                    if recovered:
                        return recovered
                if not self.thread_id and any(
                    marker in detail.casefold()
                    for marker in (
                        "không tạo dom phản hồi",
                        "không có bằng chứng phản hồi",
                        "không gửi lại prompt",
                        "checkpoint vẫn được giữ",
                    )
                ):
                    raise ContentAISubmissionUncertain(
                        "ChatGPT Web đã nhận hoặc có thể đã nhận prompt nhưng CLI chưa tạo "
                        "thread có thể phục hồi: " + (detail or "Bridge không xác nhận được lượt.")
                    )
                raise ContentAIError(
                    detail or f"Codex CLI kết thúc với mã lỗi {return_code}."
                )
            if messages:
                return "\n\n".join(messages).strip()
            try:
                fallback = output_path.read_text(encoding="utf-8-sig").strip()
            except OSError:
                fallback = ""
            if not fallback:
                if not self.thread_id:
                    raise ContentAISubmissionUncertain(
                        "Codex CLI kết thúc mà ChatGPT Web chưa xác nhận prompt và không tạo "
                        "thread có thể phục hồi. Checkpoint được giữ; không gửi lại prompt tự động."
                    )
                raise ContentAIError("Codex CLI không trả về nội dung.")
            return fallback
        except FileNotFoundError as exc:
            raise ContentAIError("Codex CLI không còn tồn tại ở đường dẫn đã kiểm tra.") from exc
        finally:
            activity_stop.set()
            if activity_thread is not None:
                activity_thread.join(timeout=2)
            with self._process_lock:
                if self._process is process:
                    self._process = None
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                # Windows may keep the Codex output handle briefly after the
                # child exits. The OS temp cleaner can safely remove it later.
                pass


@dataclass
class _ChatGPTWebRequest:
    connection: http.client.HTTPConnection
    cancelled: threading.Event = field(default_factory=threading.Event)
    transport: socket.socket | None = None


class _ChatGPTWebSocketReader(io.RawIOBase):
    """A cancellable raw reader; no buffered read is closed by another thread."""

    def __init__(self, transport: socket.socket, check_cancel: Callable[[], None]) -> None:
        super().__init__()
        self._transport = transport
        # Retain the normal socket makefile reference count, including when
        # HTTPConnection detaches a Connection: close response.
        self._raw = transport.makefile("rb", buffering=0)
        self._check_cancel = check_cancel

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if not len(buffer):
            return 0
        while True:
            self._check_cancel()
            try:
                ready, _, _ = select.select([self._transport], [], [], 0.1)
                if not ready:
                    continue
                size = self._raw.readinto(buffer)
            except OSError:
                self._check_cancel()
                raise
            self._check_cancel()
            if size is not None:
                return size

    def close(self) -> None:
        try:
            raw = getattr(self, "_raw", None)
            if raw is not None:
                raw.close()
        finally:
            super().close()


class _ChatGPTWebResponseSocket:
    """Adapt only HTTPResponse.makefile(); HTTP framing stays in the stdlib."""

    def __init__(self, transport: socket.socket, check_cancel: Callable[[], None]) -> None:
        self._transport = transport
        self._check_cancel = check_cancel

    def makefile(self, mode: str):
        if mode != "rb":
            raise ValueError("ChatGPT response transport is read-only")
        return io.BufferedReader(_ChatGPTWebSocketReader(self._transport, self._check_cancel))


class ChatGPTWebChat:
    """Cancellable multi-turn chat over the pinned local Web bridge.

    The bridge owns the browser profile and ChatGPT session. This client only
    talks to its validated loopback endpoint and never reads cookies or browser
    storage.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        working_dir: Path,
        access_token: str,
        *,
        thread_id: str | None = None,
        response_id: str = "",
    ) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ContentAIError(
                "ChatGPT Web Bridge phải dùng địa chỉ cục bộ http://127.0.0.1:PORT."
            )
        if not model.startswith("chatgpt-web/"):
            raise ContentAIError("Model ChatGPT Web không hợp lệ.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,}", access_token):
            raise ContentAIError("Khóa truy cập cục bộ ChatGPT Web không hợp lệ.")
        self.endpoint = f"http://127.0.0.1:{parsed.port}"
        self.port = parsed.port
        # Recovery requests must use the same validated loopback port after
        # the streaming response socket is interrupted.
        self._bridge_port = parsed.port
        self.model = model
        self._access_token = access_token
        self.working_dir = working_dir.expanduser().resolve()
        supplied_thread_id = str(thread_id or "").strip()
        self.thread_id = supplied_thread_id or f"thread_docurandom_{uuid.uuid4().hex}"
        self.response_id = str(response_id or "").strip()
        self._connection_lock = threading.RLock()
        self._active_request: _ChatGPTWebRequest | None = None
        self._active_turn_id = ""
        self._remote_cancelled_turn_id = ""
        self._recovery_connection: http.client.HTTPConnection | None = None

    def cancel(self) -> None:
        with self._connection_lock:
            request = self._active_request
            recovery_connection = self._recovery_connection
        if request is not None:
            self._cancel_request(request)
        if recovery_connection is not None:
            try:
                recovery_connection.close()
            except OSError:
                pass

    def cancel_remote_turn(
        self,
        *,
        reason: str = "Đã hủy lượt ChatGPT Web.",
    ) -> bool:
        """Cancel the exact accepted bridge turn, when a caller opts in.

        Ordinary ``cancel()`` intentionally remains a local transport
        interrupt for compatibility with existing callers and tests.  A
        watchdog that owns the decision to abandon a browser turn can call
        this explicit method as well, so a disconnected turn is not left in
        the bridge's recovery window.
        """

        with self._connection_lock:
            turn_id = self._active_turn_id
        if turn_id:
            return self._cancel_remote_turn(turn_id, reason)
        return False

    def _cancel_request(self, request: _ChatGPTWebRequest) -> None:
        with self._connection_lock:
            # A watcher belongs to one request, never to the next turn on this
            # chat. Only the reading thread may close its HTTPResponse.
            if self._active_request is not request:
                return
            request.cancelled.set()
            transport = request.transport or getattr(request.connection, "sock", None)
            if transport is None:
                return
            try:
                # close() clears response.fp while readline()/peek() can still
                # be using it. shutdown() interrupts transport without mutating
                # fp; the cancellable reader also polls for Windows compatibility.
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _cancel_remote_turn(self, turn_id: str, reason: str) -> bool:
        """Cancel the exact accepted bridge turn before releasing its slot.

        Closing the local response socket is not sufficient: the bridge keeps
        a disconnected browser turn alive for recovery for a short grace
        period.  A bounded, exact cancellation prevents an audit watchdog
        from leaving the shared ChatGPT capacity occupied.
        """

        with self._connection_lock:
            if self._active_turn_id != turn_id:
                return False
            if self._remote_cancelled_turn_id == turn_id:
                return True
            self._remote_cancelled_turn_id = turn_id
            thread_id = self.thread_id

        if not thread_id or not self._bridge_port:
            return False
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self._bridge_port,
            timeout=CHATGPT_WEB_HEARTBEAT_CANCEL_TIMEOUT_SECONDS,
        )
        body = json.dumps(
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "reason": str(reason or "Đã hủy lượt ChatGPT Web.")[:240],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            connection.request(
                "POST",
                "/v1/turns/cancel",
                body=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-store",
                    "Authorization": f"Bearer {self._access_token}",
                },
            )
            response = connection.getresponse()
            response.read(64 * 1024)
            return response.status in (200, 202)
        except (OSError, http.client.HTTPException, ValueError, TypeError):
            return False
        finally:
            connection.close()

    def recover_pending(
        self,
        *,
        cancel_event: threading.Event | None = None,
        on_partial: Callable[[str], None] | None = None,
        on_status: Callable[[str, str], None] | None = None,
        output_mode: str = "",
        pending_prompt: str = "",
        completed_turn_baseline: dict[str, str] | None = None,
        allow_missing_content_repair: bool = False,
    ) -> str | dict[str, object] | None:
        """Recover a submitted turn, or prove a stale pre-submit lock safe to resend."""

        if not self.thread_id or not self._bridge_port:
            return None
        if cancel_event is not None and cancel_event.is_set():
            raise ContentAICancelled("Đã hủy tác vụ viết nội dung.")
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self._bridge_port,
            timeout=None,
        )
        with self._connection_lock:
            self._recovery_connection = connection
        try:
            if on_status is not None:
                on_status(
                    "recovering-dom",
                    "Đang thu hồi phản hồi đã được bridge nhận từ đúng cuộc hội thoại; không gửi lại prompt…",
                )
            payload_value: dict[str, object] = {"thread_id": self.thread_id}
            if pending_prompt:
                _wire, pending_files = _prepare_direct_text_prompt(pending_prompt)
                normalized_wire = " ".join(unicodedata.normalize("NFC", _wire).split())
                payload_value["pending_prompt_sha256"] = hashlib.sha256(normalized_wire.encode("utf-8")).hexdigest()
                if len(pending_files) == 1:
                    file = pending_files[0]
                    payload_value["pending_attachment"] = {
                        "name": file.name, "size": len(file.data),
                        "sha256": hashlib.sha256(file.data).hexdigest(),
                    }
            if str(output_mode or "").strip() == "generated_image":
                payload_value["output_mode"] = "generated_image"
            if completed_turn_baseline and pending_prompt:
                def turn_hash(text: str) -> str:
                    return hashlib.sha256(" ".join(unicodedata.normalize("NFC",text).split()).encode("utf-8")).hexdigest()
                payload_value["completed_turn_baseline"] = {
                    "user_sha256": turn_hash(completed_turn_baseline["user"]),
                    "assistant_sha256": turn_hash(completed_turn_baseline["assistant"]),
                }
                baseline_user = completed_turn_baseline["user"]
                _baseline_wire, baseline_files = _prepare_direct_text_prompt(baseline_user)
                if len(baseline_files) == 1:
                    payload_value["completed_turn_baseline"]["user_attachment"] = {
                        "sha256": hashlib.sha256(baseline_files[0].data).hexdigest(),
                        "char_length": len(baseline_user.encode("utf-16-le")) // 2,
                    }
                if allow_missing_content_repair:
                    payload_value["allow_missing_content_repair"] = True
            payload = json.dumps(payload_value).encode("utf-8")
            connection.request(
                "POST",
                "/v1/turns/recover",
                body=payload,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
            )
            response = connection.getresponse()
            raw = response.read(CHATGPT_WEB_MAX_RESPONSE_BYTES + 1)
            if cancel_event is not None and cancel_event.is_set():
                raise ContentAICancelled("Đã hủy tác vụ viết nội dung.")
            if len(raw) > CHATGPT_WEB_MAX_RESPONSE_BYTES:
                raise ContentAIError("Phản hồi DOM phục hồi vượt giới hạn an toàn.")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ContentAIError("Bridge trả dữ liệu phục hồi DOM không hợp lệ.") from exc
            if response.status != 200:
                message = ""
                if isinstance(value, dict):
                    error = value.get("error")
                    if isinstance(error, dict):
                        message = str(error.get("message", "")).strip()
                raise ContentAIError(
                    message or f"Không thể phục hồi DOM ChatGPT (HTTP {response.status})."
                )
            if isinstance(value, dict) and value.get("status") == "not-submitted":
                expected_file = payload_value.get("pending_attachment")
                if (expected_file and value.get("safe_to_resend") is True
                    and value.get("attachment_sha256") != expected_file["sha256"]):
                    raise ContentAIError("Bridge chưa xác minh đúng tệp Master Prompt đang chờ; giữ checkpoint.")
                if value.get('proof') == 'empty-pre-submit-attachment':
                    if not expected_file or value.get('safe_to_resend') is not True or value.get('attachment_sha256') != expected_file['sha256']:
                        raise ContentAIError('Chưa xác minh được lần nạp lại tệp chưa gửi; giữ checkpoint.')
                    value['attachment_recovery_verified'] = True
                if value.get('proof') == 'failed-before-master-input':
                    if (not expected_file or value.get('safe_to_resend') is not True
                        or value.get('attachment_sha256') != expected_file['sha256']
                        or value.get('prompt_sha256') != payload_value.get('pending_prompt_sha256')):
                        raise ContentAIError('Chưa xác minh đúng Master lỗi trước khi nhập; giữ checkpoint.')
                    value['early_master_recovery_verified'] = True
                return value
            if isinstance(value, dict) and value.get("status") == "recovery-baseline":
                return value
            if (
                isinstance(value, dict)
                and str(output_mode or "").strip() == "generated_image"
                and (
                    isinstance(value.get("generated_image"), dict)
                    or bool(str(value.get("recovery_error") or "").strip())
                )
            ):
                # A finished negative image turn is a structured outcome too.
                # Returning only output_text drops the Bridge's terminal proof,
                # so generate_image mislabels it as an uncertain submitted turn
                # and delays the existing bounded retry path indefinitely.
                return value
            raw_text = value.get("output_text", "") if isinstance(value, dict) else ""
            text = raw_text if isinstance(raw_text, str) else str(raw_text or "")
            if not text.strip():
                raise ContentAIError("DOM ChatGPT đã phục hồi nhưng phản hồi đang trống.")
            if on_partial is not None:
                on_partial(text)
            return text
        except (OSError, http.client.HTTPException) as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise ContentAICancelled("Đã hủy tác vụ viết nội dung.") from exc
            raise ContentAIError(
                "Mất kết nối khi đang thu hồi phản hồi từ DOM ChatGPT; checkpoint vẫn được giữ."
            ) from exc
        finally:
            connection.close()
            with self._connection_lock:
                if self._recovery_connection is connection:
                    self._recovery_connection = None

    def repair_stalled_image_turn(self, expected_prompt: str, created_at: object,
                                  *, cancel_event: threading.Event | None = None) -> dict[str, object]:
        """Recover only this aged image receipt; the Bridge may stop its proved stalled turn."""
        if cancel_event is not None and cancel_event.is_set():
            raise ContentAICancelled("Đã hủy phục hồi thumbnail.")
        digest = hashlib.sha256(" ".join(unicodedata.normalize("NFC", expected_prompt).split()).encode()).hexdigest()
        draft = expected_prompt.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
        payload = json.dumps({"thread_id": self.thread_id, "created_at": created_at,
                              "prompt_sha256": digest, "draft_sha256": hashlib.sha256(draft.encode()).hexdigest()}).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self._bridge_port, timeout=30)
        with self._connection_lock:
            self._recovery_connection = connection
        try:
            connection.request("POST", "/v1/session/repair-stalled-image-turn", body=payload,
                headers={"Authorization": f"Bearer {self._access_token}", "Content-Type": "application/json"})
            response = connection.getresponse()
            value = json.loads(response.read(128 * 1024).decode())
            if response.status != 200:
                raise ContentAIError(str((value.get("error") or {}).get("message") or "CHATGPT_IMAGE_RECOVERY_PENDING: Chưa xác minh được lượt ảnh cũ."))
            if not isinstance(value, dict) or value.get("receipt_preserved") is not True:
                raise ContentAIError("CHATGPT_IMAGE_RECOVERY_PENDING: Bridge chưa giữ biên nhận ảnh cũ.")
            if cancel_event is not None and cancel_event.is_set():
                raise ContentAICancelled("Đã hủy trước khi thử lại thumbnail.")
            return value
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise ContentAIError("CHATGPT_IMAGE_RECOVERY_PENDING: Chưa nhận xác nhận phục hồi; giữ checkpoint.") from exc
        finally:
            connection.close()
            with self._connection_lock:
                if self._recovery_connection is connection:
                    self._recovery_connection = None

    def recover_image_with_repair(self, expected_prompt: str, created_at: object,
                                  *, cancel_event: threading.Event | None = None,
                                  on_status: object = None) -> object:
        """Read first; authorize one replacement only after exact receipt reconciliation."""
        if not isinstance(created_at, (int, float)) or created_at <= 0:
            return self.recover_pending(cancel_event=cancel_event, on_status=on_status, output_mode="generated_image")
        first = self.repair_stalled_image_turn(expected_prompt, created_at, cancel_event=cancel_event)
        if first.get("safe_to_retry") is True:
            return first
        try:
            return self.recover_pending(cancel_event=cancel_event, on_status=on_status, output_mode="generated_image")
        except ContentAIError as exc:
            # A regular two-minute read lease supplies the observation interval
            # for a genuinely unchanged Stop state. Never turn 429 into a stop.
            if "429" in str(exc) or "CHATGPT_RATE_LIMIT" in str(exc):
                raise
            second = self.repair_stalled_image_turn(expected_prompt, created_at, cancel_event=cancel_event)
            if second.get("safe_to_retry") is True:
                return second
            raise ContentAIError("CHATGPT_IMAGE_RECOVERY_PENDING: Lượt ảnh chưa đủ bằng chứng để thay thế; "
                                 f"giữ checkpoint. {exc}") from exc

    def retire_idle_image_turn(self, expected_prompt: str, created_at: object,
                               *, cancel_event: threading.Event | None = None) -> None:
        """Archive a proved stale image receipt before its bounded same-chat retry."""
        if cancel_event is not None and cancel_event.is_set():
            raise ContentAICancelled("Đã hủy lượt thử lại thumbnail.")
        digest = hashlib.sha256(" ".join(unicodedata.normalize("NFC", expected_prompt).split()).encode("utf-8")).hexdigest()
        payload = json.dumps({"thread_id": self.thread_id, "created_at": created_at,
                              "prompt_sha256": digest}).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self._bridge_port, timeout=30)
        with self._connection_lock:
            self._recovery_connection = connection
        try:
            connection.request("POST", "/v1/session/retire-idle-image-turn", body=payload,
                headers={"Authorization": f"Bearer {self._access_token}", "Content-Type": "application/json"})
            response = connection.getresponse()
            value = json.loads(response.read(128 * 1024).decode("utf-8"))
            if response.status != 200 or value.get("status") != "retired-for-one-image-retry":
                raise ContentAIError("CHATGPT_PENDING_SUBMISSION_LOCKED: " + str((value.get("error") or {}).get("message") or "Bridge chưa xác minh lượt ảnh cũ đã rảnh."))
            if cancel_event is not None and cancel_event.is_set():
                raise ContentAICancelled("Đã hủy trước khi gửi lại thumbnail.")
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise ContentAIError(
                "CHATGPT_PENDING_SUBMISSION_LOCKED: Chưa nhận xác nhận đối soát lượt ảnh cũ; giữ checkpoint."
            ) from exc
        finally:
            connection.close()
            with self._connection_lock:
                if self._recovery_connection is connection:
                    self._recovery_connection = None

    def diagnose_session(self, *, timeout: float = 15.0) -> dict[str, object]:
        """Read bounded status for this exact local ChatGPT conversation."""

        if not self.thread_id or not self._bridge_port:
            raise ContentAIError("Không có thread ChatGPT để tự kiểm tra.")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self._bridge_port, timeout=max(3.0, float(timeout))
        )
        payload = json.dumps({"thread_id": self.thread_id}).encode("utf-8")
        with self._connection_lock:
            self._recovery_connection = connection
        try:
            connection.request(
                "POST",
                "/v1/session/diagnostic",
                body=payload,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                    "Cache-Control": "no-store",
                },
            )
            response = connection.getresponse()
            raw = response.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise ContentAIError("Chẩn đoán ChatGPT vượt giới hạn an toàn.")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ContentAIError("Bridge trả chẩn đoán ChatGPT không hợp lệ.") from exc
            if response.status != 200:
                message = ""
                if isinstance(value, dict):
                    error = value.get("error")
                    if isinstance(error, dict):
                        message = str(error.get("message") or "").strip()
                raise ContentAIError(
                    message or f"Không thể kiểm tra hội thoại ChatGPT (HTTP {response.status})."
                )
            if not isinstance(value, dict):
                raise ContentAIError("Bridge trả chẩn đoán ChatGPT không hợp lệ.")
            return value
        except (OSError, http.client.HTTPException) as exc:
            raise ContentAIError(
                "Không kết nối được ChatGPT Bridge để kiểm tra lượt đã gửi."
            ) from exc
        finally:
            connection.close()
            with self._connection_lock:
                if self._recovery_connection is connection:
                    self._recovery_connection = None

    def _recover_disconnected_response(
        self,
        turn_id: str,
        *,
        cancel_event: threading.Event | None,
        on_partial: Callable[[str], None] | None,
        on_status: Callable[[str, str], None] | None,
    ) -> dict[str, object] | None:
        """Collect a completed result after only the local response socket died.

        The bridge keys this endpoint by the same stable prompt-cache/thread
        identity and per-request turn id that were sent with the original
        request.  A missing result is not retried with the prompt: the caller
        falls back to its existing checkpoint/recovery path.
        """
        if not self.thread_id or not self._bridge_port or not turn_id:
            return None
        deadline = time.monotonic() + CHATGPT_WEB_DISCONNECTED_RECOVERY_SECONDS
        last_status: tuple[str, str] | None = None
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise ContentAICancelled("Đã hủy tác vụ viết nội dung.")
            connection = http.client.HTTPConnection(
                "127.0.0.1", self._bridge_port, timeout=3.0
            )
            try:
                query = urllib.parse.urlencode(
                    {"thread_id": self.thread_id, "turn_id": turn_id}
                )
                connection.request(
                    "GET",
                    f"/v1/turns/result?{query}",
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self._access_token}",
                    },
                )
                response = connection.getresponse()
                raw = response.read(CHATGPT_WEB_MAX_RESPONSE_BYTES + 1)
                if response.status in (404, 405, 501):
                    # An older bridge does not expose the result endpoint.
                    return None
                if response.status != 200 or len(raw) > CHATGPT_WEB_MAX_RESPONSE_BYTES:
                    return None
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None
                if not isinstance(payload, dict):
                    return None
                if payload.get("status") == "completed":
                    value = payload.get("response")
                    if isinstance(value, dict):
                        text = self._extract_response_text(value)
                        if text and on_partial is not None:
                            on_partial(text)
                        return value
                stage = str(payload.get("stage") or payload.get("status") or "").strip()
                message = str(payload.get("message") or "").strip()
                if on_status is not None and (stage, message) != last_status:
                    on_status(
                        stage or "bridge-recovering",
                        message or "Đang thu hồi kết quả ChatGPT đã được chấp nhận; không gửi lại prompt.",
                    )
                    last_status = (stage, message)
            except (OSError, http.client.HTTPException, ValueError, TypeError):
                pass
            finally:
                connection.close()
            if cancel_event is not None and cancel_event.wait(CHATGPT_WEB_DISCONNECTED_POLL_SECONDS):
                raise ContentAICancelled("Đã hủy tác vụ viết nội dung.")
            time.sleep(CHATGPT_WEB_DISCONNECTED_POLL_SECONDS)
        return None

    @staticmethod
    def _is_recoverable_submitted_error(message: str) -> bool:
        lowered = str(message or "").casefold()
        if "chatgpt_account_load_failed" in lowered:
            return False
        return any(
            marker in lowered
            for marker in (
                "checkpoint vẫn được giữ",
                "checkpoint đã được giữ",
                "không gửi lại prompt",
                "đã chuyển khỏi đúng cuộc hội thoại",
                "không tạo dom phản hồi",
                "không có bằng chứng phản hồi",
                "lượt chưa thu hồi",
                "interrupted chatgpt turn",
                "recover its dom",
                "the prompt was accepted",
            )
        )

    @staticmethod
    def _error_message(status: int, raw: bytes) -> str:
        message = ""
        try:
            value = json.loads(raw.decode("utf-8"))
            if isinstance(value, dict):
                error = value.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message", "")).strip()
                elif isinstance(error, str):
                    message = error.strip()
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        lowered = message.casefold()
        if "active chatgpt web slots are busy" in lowered or "conversation slots are busy" in lowered:
            return (
                "Các phiên ChatGPT Web đang bận. Workspace này phải chờ lượt; "
                "phiên đăng nhập vẫn hoạt động và không cần đăng nhập lại."
            )
        if "stopped making progress before the inactivity timeout" in lowered:
            return (
                "ChatGPT đã ngừng cập nhật phản hồi quá lâu. Checkpoint đã được giữ; "
                "bộ hẹn giờ hoặc Tự sửa sẽ tự nối lại đúng lượt, không gửi trùng."
            )
        if "exceeded the absolute safety limit" in lowered:
            return (
                "Phản hồi ChatGPT đã vượt giới hạn an toàn tuyệt đối. Checkpoint đã được giữ; "
                "bộ hẹn giờ hoặc Tự sửa sẽ tự recheck và nối lại."
            )
        if (
            status == 429
            or "chatgpt_rate_limited" in lowered
            or "too many requests" in lowered
            or "making requests too quickly" in lowered
            or "temporarily limited access" in lowered
            or "rate limit" in lowered
            or "usage limit" in lowered
        ):
            return (
                "CHATGPT_RATE_LIMITED: ChatGPT đang tạm giới hạn do có quá nhiều yêu cầu. Tool đã giữ checkpoint, "
                "tạm dừng các lượt ChatGPT mới và tự kiểm tra lại theo thời hạn Retry-After đã lưu; giữ nguyên tài khoản/model."
                + (" · " + message[:600] if message else "")
            )
        if "network error" in lowered or "connection was interrupted" in lowered:
            return (
                "ChatGPT không xử lý được tin nhắn Master Prompt này. Nội dung có thể vượt giới hạn "
                "của model/phiên ChatGPT hiện tại hoặc kết nối vừa bị gián đoạn. Tool đã dừng trước "
                "bước BẮT ĐẦU và không gửi nội dung thiếu."
            )
        if (
            (status in (401, 403) or "session" in lowered or "sign in" in lowered)
            and "composer was not available" not in lowered
            and "file import failed" not in lowered
        ):
            return "Phiên ChatGPT Web đã hết hạn; hãy mở bridge và đăng nhập lại."
        if "not available for this account" in lowered:
            return (
                "Model đang chọn không khả dụng trong phiên ChatGPT hiện tại; "
                "tool đã dừng để không chạy nhầm model."
            )
        if (
            "interrupted chatgpt turn" in lowered
            or "recover its dom" in lowered
            or "recovery-required" in lowered
            or "lượt chưa thu hồi" in lowered
        ):
            return (
                "ChatGPT đang giữ một lượt chưa thu hồi. Tool sẽ đọc lại đúng cuộc hội thoại; "
                "nếu DOM xác nhận lượt đó chưa từng được gửi, tool chỉ gửi lại đúng một lần."
            )
        if (
            "browser" in lowered
            or "selector" in lowered
            or "dom" in lowered
            or "composer was not available" in lowered
            or "file import failed" in lowered
        ):
            # Keep the actionable, bounded renderer facts from the bridge.
            # The previous mapping discarded the exact URL/title/composer
            # counts, so every workspace looked identical and the operator
            # could not tell a slow SPA shell from a signed-out page or an
            # attachment/import failure. The bridge never puts prompt text,
            # cookies, headers, or page HTML in this diagnostic.
            detail = re.sub(r"\s+", " ", message).strip()[:700]
            suffix = f" Chi tiết: {detail}" if detail else ""
            return (
                "Bridge không đọc được giao diện ChatGPT hiện tại. "
                "Checkpoint được giữ để kiểm tra/nối lại đúng hội thoại; "
                "không gửi lại lượt chưa rõ kết quả."
                + suffix
            )
        return message or f"ChatGPT Web Bridge trả về lỗi HTTP {status}."

    @staticmethod
    def _extract_response_text(value: dict[str, object]) -> str:
        direct = value.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct
        chunks: list[str] = []
        output = value.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                if item.get("role") not in (None, "assistant"):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "output_text"
                        and isinstance(part.get("text"), str)
                        and str(part["text"]).strip()
                    ):
                        chunks.append(str(part["text"]))
        return "\n\n".join(chunks)

    def _send_response(
        self,
        prompt: str,
        *,
        cancel_event: threading.Event | None = None,
        on_partial: Callable[[str], None] | None = None,
        on_status: Callable[[str, str], None] | None = None,
        attachments: Iterable[ChatAttachment] = (),
        output_mode: str = "",
        expected_exact: str = "",
        _recovery_resend_used: bool = False,
    ) -> dict[str, object]:
        if not prompt.strip():
            raise ContentAIError("Tin nhắn gửi cho AI đang trống.")
        if cancel_event is not None and cancel_event.is_set():
            raise ContentAICancelled("Đã hủy tác vụ viết nội dung.")
        attachments = tuple(attachments)
        if not attachments and output_mode in ("", "text"):
            wire_prompt, wire_attachments = _prepare_direct_text_prompt(prompt)
        else:
            wire_prompt, wire_attachments = prompt, attachments
        if not self.thread_id:
            self.thread_id = f"thread_docurandom_{uuid.uuid4().hex}"
            self.response_id = ""
        turn_id = f"turn_docurandom_{uuid.uuid4().hex}"
        streaming = on_partial is not None or on_status is not None
        body: dict[str, object] = {
            'docurandom_prompt_sha256':hashlib.sha256(' '.join(unicodedata.normalize('NFC',prompt).split()).encode()).hexdigest(),
            "model": self.model,
            "stream": streaming,
            "input": wire_prompt,
            "prompt_cache_key": self.thread_id,
            "metadata": {
                "thread_id": self.thread_id,
                "turn_id": turn_id,
                "client": "DocuRandomStudio",
            },
            "client_metadata": {
                "x-codex-turn-metadata": json.dumps(
                    {"thread_id": self.thread_id, "turn_id": turn_id},
                    separators=(",", ":"),
                )
            },
        }
        if output_mode:
            body["docurandom_output"] = output_mode
        if expected_exact:
            if expected_exact != "ĐÃ TẠO":
                raise ContentAIError("Xác nhận ChatGPT Web không thuộc danh sách an toàn.")
            body["docurandom_expected_exact"] = expected_exact
        attachment_payloads: list[dict[str, str]] = []
        for attachment in wire_attachments:
            safe_name = Path(str(attachment.name)).name
            suffix = Path(safe_name).suffix.casefold()
            image_types = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
            is_image = output_mode in {'generated_image', 'thumbnail_confirmation', 'verified_json'} and suffix in image_types
            if not safe_name or (suffix not in (".txt", ".md") and not is_image):
                raise ContentAIError("File Master Prompt phải có định dạng .txt hoặc .md.")
            if not attachment.data or len(attachment.data) > CHATGPT_WEB_MAX_ATTACHMENT_BYTES:
                raise ContentAIError("File Master Prompt vượt giới hạn an toàn 8 MB.")
            if is_image:
                from .thumbnail_manager import inspect_thumbnail_bytes, ThumbnailError
                try:
                    inspection = inspect_thumbnail_bytes(attachment.data)
                except ThumbnailError as exc:
                    raise ContentAIError('Ảnh đính kèm thumbnail không hợp lệ: ' + str(exc)) from exc
                expected_suffix = '.jpg' if suffix == '.jpeg' else suffix
                if inspection.extension != expected_suffix or attachment.mime_type != image_types[suffix]:
                    raise ContentAIError('Định dạng hoặc MIME của ảnh đính kèm thumbnail không khớp bytes.')
            attachment_payloads.append(
                {
                    "name": safe_name,
                    "mime_type": attachment.mime_type,
                    "content_base64": base64.b64encode(attachment.data).decode("ascii"),
                }
            )
        if len(attachment_payloads) > 1:
            raise ContentAIError("Mỗi lượt chỉ hỗ trợ một file Master Prompt.")
        if attachment_payloads:
            body["attachments"] = attachment_payloads
        if self.response_id:
            body["previous_response_id"] = self.response_id
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.port,
            timeout=CHATGPT_WEB_CONNECT_TIMEOUT_SECONDS,
        )
        active_request = _ChatGPTWebRequest(connection)
        with self._connection_lock:
            if self._active_request is not None:
                raise ContentAIError("Hội thoại ChatGPT này đang xử lý một lượt khác.")
            self._active_request = active_request
            self._active_turn_id = turn_id
            self._remote_cancelled_turn_id = ""
        finished = threading.Event()

        def raise_if_cancelled() -> None:
            if active_request.cancelled.is_set() or (
                cancel_event is not None and cancel_event.is_set()
            ):
                raise ContentAICancelled("Đã hủy tác vụ viết nội dung.")

        def watch_cancel() -> None:
            while not finished.wait(0.1):
                if active_request.cancelled.is_set() or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    # Retry only the interrupt if cancellation raced with TCP
                    # connection setup. This never retries the HTTP request.
                    self._cancel_request(active_request)

        cancel_watcher = threading.Thread(
            target=watch_cancel,
            daemon=True,
            name="ChatGPTWebCancelWatch",
        )
        raw = b""
        value: dict[str, object] | None = None
        recoverable_submission_error: ContentAIError | None = None
        response: http.client.HTTPResponse | None = None
        try:
            cancel_watcher.start()
            raise_if_cancelled()
            if on_status is not None and output_mode in ("", "text"):
                on_status("transport-starting", "Bắt đầu truyền đúng lượt ChatGPT đã xếp hàng.")
            connection.request(
                "POST",
                "/v1/responses",
                body=encoded,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(encoded)),
                    "Cache-Control": "no-store",
                    "User-Agent": "DocuRandomStudio/1.0",
                    "Authorization": f"Bearer {self._access_token}",
                },
            )
            with self._connection_lock:
                # getresponse() may detach connection.sock for Connection:
                # close responses, while the response is still being read.
                active_request.transport = getattr(connection, "sock", None)
            raise_if_cancelled()
            if active_request.transport is not None:
                # Windows shutdown() does not always interrupt a pending recv.
                # Nonblocking reads + select let every wait observe cancellation
                # without timing out/poisoning an HTTP buffered file mid-chunk.
                active_request.transport.setblocking(False)

                def response_factory(transport, **kwargs):
                    return http.client.HTTPResponse(
                        _ChatGPTWebResponseSocket(transport, raise_if_cancelled),
                        **kwargs,
                    )

                connection.response_class = response_factory
            response = connection.getresponse()
            raise_if_cancelled()
            if response.status >= 400:
                raw = response.read(CHATGPT_WEB_MAX_RESPONSE_BYTES + 1)
                try:
                    bridge_message = json.loads(raw).get('error', {}).get('message')
                except (ValueError, AttributeError):
                    bridge_message = None
                if response.status == 409 and bridge_message in {
                    'This ChatGPT conversation is already running',
                    'This workspace has an interrupted ChatGPT turn; recover its DOM before sending another prompt',
                    'Previous ChatGPT attachment is unresolved in its exact conversation; no new file or prompt was sent',
                }:
                    raise ChatGPTPreSubmitRejected(bridge_message)
                raise ContentAIError(self._error_message(response.status, raw))
            if not streaming:
                content_length = int(response.getheader("Content-Length", "0") or 0)
                if content_length > CHATGPT_WEB_MAX_RESPONSE_BYTES:
                    raise ContentAIError("Phản hồi ChatGPT Web lớn bất thường; đã dừng an toàn.")
                raw = response.read(CHATGPT_WEB_MAX_RESPONSE_BYTES + 1)
                if len(raw) > CHATGPT_WEB_MAX_RESPONSE_BYTES:
                    raise ContentAIError("Phản hồi ChatGPT Web vượt giới hạn an toàn.")
            else:
                current_text = ""
                total_bytes = 0
                while True:
                    raise_if_cancelled()
                    line = response.readline(CHATGPT_WEB_MAX_RESPONSE_BYTES + 1)
                    raise_if_cancelled()
                    if not line:
                        break
                    total_bytes += len(line)
                    if total_bytes > CHATGPT_WEB_MAX_RESPONSE_BYTES:
                        raise ContentAIError("Luồng ChatGPT Web vượt giới hạn an toàn.")
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ContentAIError("ChatGPT Web Bridge trả về luồng dữ liệu không hợp lệ.") from exc
                    if not isinstance(event, dict):
                        continue
                    event_type = str(event.get("type", ""))
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            current_text += delta
                            if on_partial is not None:
                                on_partial(current_text)
                    elif event_type == "response.output_text.snapshot":
                        snapshot = event.get("text")
                        if isinstance(snapshot, str):
                            current_text = snapshot
                            if on_partial is not None:
                                on_partial(current_text)
                    elif event_type == "response.status":
                        stage = str(event.get("stage", "")).strip()
                        message = str(event.get("message", "")).strip()
                        if on_status is not None and message:
                            on_status(stage, message)
                    elif event_type == "response.completed":
                        completed = event.get("response")
                        if isinstance(completed, dict):
                            value = completed
                            break
                    elif event_type == "error":
                        error_raw = json.dumps(
                            {"error": event.get("error", {})},
                            ensure_ascii=False,
                        ).encode("utf-8")
                        raise ContentAIError(self._error_message(500, error_raw))
                if value is None:
                    raise ContentAIError("ChatGPT Web Bridge kết thúc luồng trước khi hoàn tất.")
        except ContentAIError as exc:
            raise_if_cancelled()
            if isinstance(exc, ChatGPTPreSubmitRejected):
                raise
            # ChatGPT can accept a prompt and then lose the streaming socket
            # while moving from the home shell to the newly-created /c/<id>
            # conversation. The bridge keeps that exact turn checkpointed;
            # recover it after closing this response instead of reporting a
            # false failure or ever submitting the prompt twice.
            if self._is_recoverable_submitted_error(str(exc)):
                recoverable_submission_error = exc
            else:
                raise
        except (OSError, http.client.HTTPException) as exc:
            if active_request.cancelled.is_set() or (
                cancel_event is not None and cancel_event.is_set()
            ):
                raise ContentAICancelled("Đã hủy tác vụ viết nội dung.") from exc
            recovered = self._recover_disconnected_response(
                turn_id,
                cancel_event=cancel_event,
                on_partial=on_partial,
                on_status=on_status,
            )
            if recovered is not None:
                value = recovered
            else:
                raise ContentAIError(
                    "Mất kết nối với ChatGPT Web Bridge. Checkpoint đã được giữ; "
                    "tool không gửi lại prompt để tránh tạo trùng."
                ) from exc
        finally:
            finished.set()
            with self._connection_lock:
                if self._active_request is active_request:
                    self._active_request = None
                if self._active_turn_id == turn_id:
                    self._active_turn_id = ""
            try:
                close_response = getattr(response, "close", None)
                if close_response is not None:
                    close_response()
            finally:
                connection.close()
        raise_if_cancelled()
        if recoverable_submission_error is not None:
            if on_status is not None:
                on_status(
                    "recovering-dom",
                    "Bridge đã nhận lượt nhưng luồng trả lời bị ngắt; đang thu hồi output từ đúng cuộc hội thoại, không gửi lại prompt…",
                )
            try:
                recovered = self.recover_pending(
                    cancel_event=cancel_event,
                    on_partial=on_partial,
                    on_status=on_status,
                    output_mode=output_mode,
                    pending_prompt=prompt,
                )
            except ContentAIError as recovery_error:
                if any(marker in str(recovery_error).casefold() for marker in (
                    "chatgpt_rate_limited", "too many requests", "tạm giới hạn do có quá nhiều yêu cầu",
                    "we're doing a quick check to keep chatgpt reliable",
                    "we’re doing a quick check to keep chatgpt reliable",
                    "chatgpt_submission_transport_uncertain",
                )):
                    # Preserve the provider's current hold/reset time, rather
                    # than replacing it with the earlier generic 409 lock.
                    raise recovery_error from recoverable_submission_error
                raise recoverable_submission_error
            if isinstance(recovered, dict) and recovered.get("status") == "not-submitted":
                if recovered.get("safe_to_resend") is True and not _recovery_resend_used:
                    if on_status is not None:
                        on_status(
                            "resending-unsubmitted",
                            "Đã xác minh lượt cũ chưa từng được gửi; đang gửi lại đúng prompt một lần, không lặp tiếp…",
                        )
                    return self._send_response(
                        prompt,
                        cancel_event=cancel_event,
                        on_partial=on_partial,
                        on_status=on_status,
                        attachments=attachments,
                        output_mode=output_mode,
                        expected_exact=expected_exact,
                        _recovery_resend_used=True,
                    )
                if recovered.get("safe_to_resend") is True and output_mode == "generated_image":
                    # Keep the native absence proof when the one transport
                    # retry also failed. The UI must not retain a false
                    # submitted lock or infer safety from a missing receipt.
                    recoverable_submission_error.unsubmitted_image_proof = {
                        "safe_to_resend": True,
                        "thread_id": self.thread_id,
                        "prompt_sha256": hashlib.sha256(
                            " ".join(unicodedata.normalize("NFC", prompt).split()).encode("utf-8")
                        ).hexdigest(),
                    }
                raise recoverable_submission_error
            if isinstance(recovered, dict):
                value = recovered
            elif recovered:
                value = {"output_text": recovered}
            else:
                raise recoverable_submission_error
        if value is None:
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ContentAIError("ChatGPT Web Bridge trả về dữ liệu không hợp lệ.") from exc
            value = decoded if isinstance(decoded, dict) else None
        if not isinstance(value, dict):
            raise ContentAIError("ChatGPT Web Bridge trả về dữ liệu không hợp lệ.")
        response_id = value.get("id")
        if isinstance(response_id, str) and response_id.strip():
            self.response_id = response_id.strip()
        return value

    def send(
        self,
        prompt: str,
        *,
        cancel_event: threading.Event | None = None,
        on_partial: Callable[[str], None] | None = None,
        on_status: Callable[[str, str], None] | None = None,
        attachments: Iterable[ChatAttachment] = (),
        expected_exact: str = "",
        output_mode: str = "",
    ) -> str:
        value = self._send_response(
            prompt,
            cancel_event=cancel_event,
            on_partial=on_partial,
            on_status=on_status,
            attachments=attachments,
            output_mode=output_mode,
            expected_exact=expected_exact,
        )
        text = self._extract_response_text(value)
        if not text:
            raise ContentAIError("ChatGPT Web không trả về nội dung văn bản.")
        return text

    def read_existing_generated_image(self, *, timeout: float = 45.0, expected_prompt: str = "", source_only: bool = False) -> ChatGPTGeneratedImage | None:
        """Collect a manual retry on the exact saved chat without submitting."""
        request = urllib.request.Request(
            f"http://127.0.0.1:{self._bridge_port}/v1/session/existing-image",
            data=json.dumps({"thread_id": self.thread_id, **({'source_only':True}if source_only else {}), **({"image_prompt_sha256": hashlib.sha256(" ".join(unicodedata.normalize("NFC", expected_prompt).split()).encode()).hexdigest()} if expected_prompt else {})}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._access_token}",
                     "Content-Type": "application/json", "Cache-Control": "no-store"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                value = _read_limited_json_response(response, CHATGPT_WEB_MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            # Preserve the bridge's actual failure (busy, wrong chat, loading)
            # instead of repeatedly reporting an opaque HTTP 500.
            raw = exc.read(CHATGPT_WEB_MAX_RESPONSE_BYTES)
            raise ContentAIError(self._error_message(exc.code, raw)) from exc
        self._last_existing_image_evidence = {
            key: value.get(key) for key in (
                "status", "assistant_count", "user_count", "image_ready", "image_busy",
                "candidate_count", "after_latest_user", "latest_assistant_after_user",
                "history_scan",
                "source_proof",
            )
        }
        if value.get("status") == "ready":
            return self.parse_generated_image_response(value)
        if value.get("status") == "no-image":
            return None
        error = ContentAIError("Lượt tạo ảnh trong đúng hội thoại vẫn chưa xác nhận hoàn tất; chờ thu hồi, không gửi lại.")
        # A finished VOICEVOX audit may follow an explicit no-image result.
        # Carry read-only evidence; only the terminal-recovery UI can use it,
        # after two independent exact-thread idle snapshots.
        error.image_recovery_evidence = {key: value.get(key) for key in (
            "status", "assistant_count", "user_count", "image_ready", "image_busy",
            "candidate_count", "after_latest_user", "latest_assistant_after_user",
        )}
        raise error

    def generate_image(
        self,
        prompt: str,
        *,
        cancel_event: threading.Event | None = None,
        on_status: Callable[[str, str], None] | None = None,
        attachments: tuple[ChatAttachment, ...] = (),
    ) -> ChatGPTGeneratedImage:
        value = self._send_response(
            prompt,
            cancel_event=cancel_event,
            on_status=on_status,
            output_mode="generated_image",
            **({'attachments': attachments} if attachments else {}),
        )
        return self.parse_generated_image_response(value)

    def parse_generated_image_response(
        self,
        value: dict[str, object],
    ) -> ChatGPTGeneratedImage:
        text = self._extract_response_text(value)
        image = value.get("generated_image")
        if not isinstance(image, dict):
            terminal_error = str(value.get("recovery_error") or "").strip()
            if terminal_error:
                raise ContentAIError(
                    "CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: " + terminal_error
                )
            raise ContentAIError(
                "ChatGPT không trả về dữ liệu ảnh thumbnail."
                + (f" Phản hồi văn bản: {' '.join(text.split())[:240]}" if text else "")
            )
        encoded = image.get("data_base64")
        if not isinstance(encoded, str) or not encoded:
            raise ContentAIError("Bridge nhận được thumbnail nhưng dữ liệu ảnh đang trống.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ContentAIError("Bridge trả về thumbnail sai định dạng dữ liệu.") from exc
        if not data or len(data) > 20 * 1024 * 1024:
            raise ContentAIError("Thumbnail ChatGPT vượt giới hạn an toàn 20 MB.")
        try:
            width = int(image.get("width", 0) or 0)
            height = int(image.get("height", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ContentAIError("Bridge trả về kích thước thumbnail không hợp lệ.") from exc
        if width < 1 or height < 1:
            raise ContentAIError("Bridge không xác định được kích thước thumbnail.")
        return ChatGPTGeneratedImage(
            data=data,
            mime_type=str(image.get("mime_type") or "application/octet-stream"),
            width=width,
            height=height,
            assistant_text=text,
            response_id=self.response_id,
        )


class MasterPromptRunner:
    def __init__(
        self,
        chat: CodexCliChat,
        *,
        output_dir: Path,
        project_name: str,
        minutes: float,
        max_turns: int = 48,
        title_history: Iterable[str] | None = None,
        title_history_total: int | None = None,
        recover_submitted_only: bool = False,
        minimum_characters: int = 0,
        production_context: str = "",
    ) -> None:
        self.chat = chat
        self.recover_submitted_only = bool(recover_submitted_only)
        self.output_dir = output_dir.expanduser().resolve()
        self.project_name = normalize_output_name(project_name)
        self.minutes = float(minutes)
        self.minimum_characters = max(0, int(minimum_characters))
        self.production_context = production_context
        self.content_locally_validated = False
        self.max_turns = max(4, int(max_turns))
        self.title_history_enabled = title_history is not None
        self.title_history = tuple(
            str(title).strip() for title in (title_history or ()) if str(title).strip()
        )
        self.title_history_total = max(
            len(self.title_history),
            int(title_history_total or len(self.title_history)),
        )
        self.turns: list[ChatTurn] = []
        self.expected_images = math.ceil(self.minutes)
        self.master_prompt_sha256 = ""
        self.output_contract_id = ""
        self.output_contract_package_sha256 = ""
        # One automatic repair is enough to recover a Master Prompt that the
        # old bridge accepted before ChatGPT materialized its attachment card.
        # Persist the counter so Continue/restart cannot resubmit the same
        # giant prompt indefinitely if the account/UI still rejects files.
        self.master_attachment_repair_attempts = 0
        self.content_repair_attempts = 0
        self.narration_duration_repair_attempts = 0
        self.narration_repair_reason = ""
        # A bridge restart can orphan the saved conversation after an older
        # build has already checkpointed its one safe attachment-repair retry.
        # Persist a separate, bounded recovery lease so that this exact
        # pre-submit shape can be moved to one fresh conversation without
        # weakening the fail-closed rule for ordinary pending turns.
        self.conversation_recovery_attempts = 0
        self.conversation_recovery_pending = False
        self.submission_uncertain = False
        self.submission_uncertain_reason = ""
        self.pending_turn_transport_started: bool | None = None
        self.stalled_content_repair: dict[str, object] = {}
        if not self.project_name:
            raise ContentAIError("Hãy nhập tên dự án/tên video.")
        if not 1.0 <= self.minutes <= 180.0:
            raise ContentAIError("Thời lượng nội dung phải từ 1 đến 180 phút.")

    @property
    def package_path(self) -> Path:
        return self.output_dir / f"{self.project_name}.txt"

    @property
    def transcript_path(self) -> Path:
        return self.output_dir / f"{self.project_name}_CHAT.txt"

    @property
    def checkpoint_path(self) -> Path:
        return self.output_dir / f"{self.project_name}_AI_CHECKPOINT.json"

    def cancel(self) -> None:
        self.chat.cancel()

    def _append_turn(self, role: str, text: str, callback: TurnCallback | None) -> None:
        # Preserve the exact renderer string in the checkpoint and transcript.
        # Normalization belongs in artifact parsers, not in the raw conversation
        # record that is used to audit what ChatGPT actually returned.
        turn = ChatTurn(role=role, text=text)
        if role.casefold() == "assistant":
            self.pending_turn_transport_started = None
        self.turns.append(turn)
        if callback is not None:
            callback(turn.role, turn.text)
        self._save_checkpoint()

    def _save_checkpoint(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.transcript_path, _format_transcript(self.turns))
        payload: dict[str, object] = {
            "version": 2,
            "project_name": self.project_name,
            "minutes": self.minutes,
            "thread_id": self.chat.thread_id,
            "master_prompt_sha256": self.master_prompt_sha256,
            "master_attachment_repair_attempts": self.master_attachment_repair_attempts,
            "content_repair_attempts": self.content_repair_attempts,
            "narration_duration_repair_attempts": self.narration_duration_repair_attempts,
            "narration_repair_reason": self.narration_repair_reason,
            "minimum_narration_characters": self.minimum_characters,
            "content_locally_validated": self.content_locally_validated,
            "conversation_recovery_attempts": self.conversation_recovery_attempts,
            "expected_images": self.expected_images,
            "stalled_content_repair": self.stalled_content_repair,
            "turns": [
                {"role": turn.role, "text": turn.text} for turn in self.turns
            ],
        }
        if self.output_contract_id:
            payload['output_contract_id'] = self.output_contract_id
            payload['output_contract_package_sha256'] = self.output_contract_package_sha256
        if self.conversation_recovery_pending:
            payload["conversation_recovery_pending"] = True
        if self.submission_uncertain:
            payload["submission_uncertain"] = True
            payload["submission_uncertain_reason"] = self.submission_uncertain_reason
        if self.pending_turn_transport_started is not None and self.turns and self.turns[-1].role.casefold()=="user":
            payload["pending_turn_transport_started"] = self.pending_turn_transport_started
            payload["pending_turn_queue_hash"] = hashlib.sha256((self.chat.thread_id+"\0"+self.turns[-1].text).encode("utf-8")).hexdigest()
        atomic_write_json(self.checkpoint_path, payload)

    def _load_checkpoint(
        self,
        master_prompt: str,
    ) -> tuple[list[ChatTurn], ContentArtifacts, str, bool] | None:
        """Load an identical complete or partial run without replaying work."""

        try:
            raw = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            if str(raw.get("project_name", "")) != self.project_name:
                return None
            if abs(float(raw.get("minutes", 0.0)) - self.minutes) > 1e-9:
                return None
            stored_hash = str(raw.get("master_prompt_sha256", "")).strip()
            current_hash = hashlib.sha256(master_prompt.strip().encode("utf-8")).hexdigest()
            if stored_hash and stored_hash != current_hash:
                raise ContentAIError(
                    'Master đã đổi so với checkpoint; giữ hội thoại cũ, không tự nạp lại. '
                    'Hãy khôi phục Master của lượt cũ hoặc bắt đầu lượt mới qua quy trình lưu trữ phiên.'
                )
            from . import output_contract
            if output_contract.enabled(master_prompt):
                self.output_contract_id = str(raw.get('output_contract_id', ''))
                self.output_contract_package_sha256 = str(raw.get('output_contract_package_sha256', ''))
                try:
                    output_contract.identity(master_prompt, self.output_contract_id, self.expected_images)
                except ValueError as exc:
                    raise ContentAIError('CONTENT_OUTPUT_CONTRACT_FAILED: Mã lượt không hợp lệ; giữ checkpoint, không gửi lại.') from exc
            if raw.get("submission_uncertain") is True:
                reason = str(raw.get("submission_uncertain_reason", "")).strip()
                raise ContentAIError(
                    "Checkpoint đang ở trạng thái chưa xác nhận: ChatGPT có thể đã nhận "
                    "prompt nhưng không còn thread để thu hồi. Tool dừng và không tự gửi lại "
                    "để tránh tạo nội dung trùng. Hãy kiểm tra đúng phiên ChatGPT/bridge rồi "
                    "xử lý checkpoint này thủ công."
                    + (f" Chi tiết: {reason}" if reason else "")
                )
            if raw.get("conversation_recovery_pending") is True:
                raise ContentAISubmissionUncertain(
                    "Checkpoint đang dở lúc chuyển sang conversation ChatGPT mới; "
                    "chưa thể xác nhận lượt Master Prompt có được gửi hay chưa. "
                    "Tool giữ nguyên checkpoint và không tự gửi lại để tránh tạo trùng. "
                    "Hãy kiểm tra đúng conversation/bridge rồi xử lý checkpoint thủ công."
                )
            try:
                self.master_attachment_repair_attempts = max(
                    0,
                    min(1, int(raw.get("master_attachment_repair_attempts", 0) or 0)),
                )
            except (TypeError, ValueError):
                self.master_attachment_repair_attempts = 0
            try:
                self.content_repair_attempts = max(
                    0, min(MAX_CONTENT_REPAIR_ATTEMPTS+1,
                           int(raw.get("content_repair_attempts", 0) or 0)),
                )
            except (TypeError, ValueError):
                self.content_repair_attempts = 0
            self.narration_duration_repair_attempts = max(0, min(MAX_NARRATION_DURATION_REPAIR_ATTEMPTS,
                int(raw.get("narration_duration_repair_attempts", 0) or 0)))
            self.narration_repair_reason = str(raw.get("narration_repair_reason") or "")
            try:
                self.conversation_recovery_attempts = max(
                    0,
                    min(1, int(raw.get("conversation_recovery_attempts", 0) or 0)),
                )
            except (TypeError, ValueError):
                self.conversation_recovery_attempts = 0
            self.minimum_characters = max(self.minimum_characters, int(raw.get("minimum_narration_characters", 0) or 0))
            self.content_locally_validated = raw.get("content_locally_validated") is True
            repair = raw.get("stalled_content_repair", {})
            if not isinstance(repair, dict):
                raise ContentAIError("Checkpoint tự sửa không hợp lệ; giữ dữ liệu, không gửi thêm.")
            self.stalled_content_repair = repair
            raw_turns = raw.get("turns")
            if not isinstance(raw_turns, list):
                return None
            turns = [
                ChatTurn(str(item["role"]), str(item["text"]))
                for item in raw_turns
                if isinstance(item, dict) and "role" in item and "text" in item
            ]
            queued_flag=raw.get("pending_turn_transport_started")
            queued_hash=hashlib.sha256((str(raw.get("thread_id") or "")+"\0"+turns[-1].text).encode("utf-8")).hexdigest() if turns else ""
            self.pending_turn_transport_started = queued_flag if (isinstance(queued_flag,bool)
                and turns and turns[-1].role.casefold()=="user" and raw.get("pending_turn_queue_hash")==queued_hash) else None
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        if not turns:
            return None
        first_user = next(
            (turn.text for turn in turns if turn.role.casefold() == "user"),
            "",
        )
        if not _master_message_matches(first_user, master_prompt):
            if stored_hash:
                raise ContentAIError('Checkpoint Master không khớp tin đã gửi; giữ hội thoại, không gửi lại prompt.')
            return None
        artifacts = extract_content_artifacts(turns, self.minimum_characters)
        confirmed = image_completion_confirmed_in_turns(turns)
        exported_reply = final_export_reply_in_turns(turns)
        if exported_reply:
            artifacts = merge_exported_artifacts(
                exported_reply,
                artifacts,
                self.minutes,
                expected_images=self.expected_images,
                allowed_missing_images=DEFAULT_ALLOWED_MISSING_IMAGE_PROMPTS,
            )
        from . import output_contract
        if output_contract.enabled(master_prompt):
            accepted_package = False
            try:
                package = self.package_path.read_bytes()
                if self.output_contract_package_sha256 and hashlib.sha256(package).hexdigest() == self.output_contract_package_sha256:
                    accepted_package = True
                    package_text = package.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
                    artifacts = merge_exported_artifacts(package_text, artifacts, self.minutes,
                        expected_images=self.expected_images, allowed_missing_images=0)
            except OSError:
                pass
        data_complete = artifacts_complete(
            artifacts,
            self.minutes,
            expected_images=self.expected_images,
            allowed_missing_images=DEFAULT_ALLOWED_MISSING_IMAGE_PROMPTS,
        minimum_characters=self.minimum_characters,
                        )
        legacy_confirmed = any(
            turn.role.casefold() == "assistant"
            and is_image_prompt_done_confirmation(turn.text)
            for turn in turns
        )
        legacy_complete = bool(
            str(raw.get("version", 1) or 1).strip() in {"", "1"}
            and legacy_confirmed
            and data_complete
        )
        complete = legacy_complete or bool(
            exported_reply and (confirmed or self.content_locally_validated) and data_complete
        )
        if output_contract.enabled(master_prompt):
            expected = output_contract.identity(master_prompt, self.output_contract_id, self.expected_images)
            complete = complete and accepted_package and not output_contract.problems(expected, artifacts, turns)
        thread_id = str(raw.get("thread_id", "")).strip()
        if not complete and not thread_id:
            # A partial transcript without the exact ChatGPT thread cannot be
            # resumed safely; starting fresh is less likely to mix workflows.
            return None
        return turns, artifacts, thread_id, complete

    def _send(
        self,
        text: str,
        *,
        cancel_event: threading.Event,
        on_turn: TurnCallback | None,
        on_partial: PartialCallback | None,
        on_status: StatusCallback | None = None,
        send_text: str | None = None,
        attachments: Iterable[ChatAttachment] = (),
    ) -> str:
        if cancel_event.is_set():
            raise ContentAICancelled("Đã hủy tác vụ viết nội dung.")
        self.output_contract_package_sha256 = ''
        if isinstance(self.chat,ChatGPTWebChat):
            if not self.chat.thread_id:
                self.chat.thread_id=f"thread_docurandom_{uuid.uuid4().hex}"
            self.pending_turn_transport_started=False
        self._append_turn("user", text, on_turn)
        def persisted_status(stage: str, message: str) -> None:
            if stage=="transport-starting":
                self.pending_turn_transport_started=True
                self._save_checkpoint()
            if on_status is not None:
                on_status(stage,message)
        try:
            response = self.chat.send(
                send_text if send_text is not None else text,
                cancel_event=cancel_event,
                on_partial=on_partial,
                on_status=persisted_status if isinstance(self.chat,ChatGPTWebChat) else on_status,
                attachments=attachments,
            )
        except Exception as exc:
            # CodexCliChat learns the real bridge thread only after the CLI
            # emits thread.started. Persist it even when DOM evidence fails so
            # the next run can recover the exact pending turn instead of
            # creating a fresh conversation on every automatic retry.
            if isinstance(exc, ContentAISubmissionUncertain):
                self.submission_uncertain = True
                self.submission_uncertain_reason = str(exc)
            try:
                self._save_checkpoint()
            except (OSError, TypeError, ValueError):
                pass
            raise
        self._append_turn("assistant", response, on_turn)
        return response

    def _progress(self, callback: ProgressCallback | None, value: float, message: str) -> None:
        if callback is not None:
            callback(max(0.0, min(1.0, value)), message)

    def _request_content_repair(
        self,
        artifacts: ContentArtifacts,
        *,
        cancel_event: threading.Event,
        on_turn: TurnCallback | None,
        on_partial: PartialCallback | None,
        on_status: StatusCallback | None,
    ) -> str:
        measured_duration_repair = bool(
            self.narration_repair_reason.startswith("NARRATION_CONTENT_INCOMPLETE:")
            and narration_problem(artifacts.voicevox_script, self.minimum_characters)
            and self.minimum_characters > minimum_narration_characters(self.minutes)
        )
        quality_migration_repair=bool(_artifact_problems(artifacts) and self.content_repair_attempts==MAX_CONTENT_REPAIR_ATTEMPTS)
        if self.content_repair_attempts >= MAX_CONTENT_REPAIR_ATTEMPTS and not quality_migration_repair and not (
            measured_duration_repair
            and self.narration_duration_repair_attempts < MAX_NARRATION_DURATION_REPAIR_ATTEMPTS
        ):
            raise ContentAIError(
                "ChatGPT đã hết số lần khôi phục phần nội dung thiếu. "
                "Checkpoint được giữ; không gửi TIẾP lặp lại hoặc render thiếu dữ liệu."
            )
        if quality_migration_repair:
            self.content_repair_attempts+=1
            repair_label='đối soát dữ liệu sai bối cảnh 1/1'
        elif self.content_repair_attempts >= MAX_CONTENT_REPAIR_ATTEMPTS:
            self.narration_duration_repair_attempts += 1
            repair_label = (f"theo thời lượng giọng đã đo {self.narration_duration_repair_attempts}/"
                            f"{MAX_NARRATION_DURATION_REPAIR_ATTEMPTS}")
        else:
            self.content_repair_attempts += 1
            repair_label = f"{self.content_repair_attempts}/{MAX_CONTENT_REPAIR_ATTEMPTS}"
        self._save_checkpoint()
        if on_status is not None:
            on_status(
                "content-repair",
                f"Đang bổ sung nội dung thiếu {repair_label}: "
                + ", ".join(_missing_content_fields(artifacts, self.expected_images, self.minimum_characters)),
            )
        return self._send(
            _content_repair_request(artifacts, self.expected_images, self.minimum_characters,
                                    target_minutes=self.minutes),
            cancel_event=cancel_event,
            on_turn=on_turn,
            on_partial=on_partial,
            on_status=on_status,
        )

    def _can_request_stalled_content_repair(self, artifacts: ContentArtifacts) -> bool:
        return bool(
            not self.stalled_content_repair
            and self.content_repair_attempts < MAX_CONTENT_REPAIR_ATTEMPTS
            and not narration_problem(artifacts.voicevox_script, self.minimum_characters)
            and _missing_content_fields(artifacts, self.expected_images, self.minimum_characters)
            and len(self.turns) >= 3
            and self.turns[-1].role.casefold() == "user"
            and self.turns[-1].text.strip().casefold() == "tiếp"
            and self.turns[-2].role.casefold() == "assistant"
            and self.turns[-3].role.casefold() == "user"
            and self.turns[-3].text.strip().casefold() == "tiếp"
        )

    def _stalled_repair_transcript_hash(self) -> str:
        return hashlib.sha256(json.dumps(
            [{"role": t.role, "text": t.text} for t in self.turns],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def _reserve_stalled_content_repair(self, artifacts: ContentArtifacts,
                                       proof: dict[str, object]) -> None:
        def normalized_hash(text: str) -> str:
            return hashlib.sha256(" ".join(unicodedata.normalize("NFC", text).split()).encode("utf-8")).hexdigest()
        if not self._can_request_stalled_content_repair(artifacts) or not (
            proof.get("status") == "recovery-baseline"
            and proof.get("proof") == "stable-completed-baseline"
            and proof.get("safe_to_resend") is False
            and proof.get("safe_to_request_missing") is True
            and proof.get("user_sha256") == normalized_hash(self.turns[-3].text)
            and proof.get("assistant_sha256") == normalized_hash(self.turns[-2].text)
            and proof.get("pending_prompt_sha256") == normalized_hash(self.turns[-1].text)
            and re.fullmatch(r"[a-f0-9]{64}", str(proof.get("evidence_sha256") or ""))
        ):
            raise ContentAIError("Chưa đủ bằng chứng để tự bổ sung nội dung; giữ checkpoint, không gửi lại prompt.")
        request = _content_repair_request(artifacts, self.expected_images,
            self.minimum_characters, target_minutes=self.minutes)
        request = request.replace("Bạn đã báo kết thúc nhưng tool chưa đọc đủ:",
            "Tool đã giữ lời thoại của video hiện tại, nhưng còn thiếu:")
        request += ("\nMã lượt phục hồi: " + uuid.uuid4().hex
            + ". Đây là yêu cầu bổ sung phần thiếu, không phải gửi lại TIẾP. "
              "Không viết lại lời thoại, không đổi chủ đề, không tạo video mới.")
        self.stalled_content_repair = {
            "status": "reserved", "thread_id": self.chat.thread_id,
            "transcript_sha256": self._stalled_repair_transcript_hash(),
            "evidence_sha256": proof["evidence_sha256"],
            "prompt": request,
            "voice_sha256": hashlib.sha256(artifacts.voicevox_script.encode("utf-8")).hexdigest(),
            "title": artifacts.title,
        }
        self.content_repair_attempts += 1
        # Keep the ambiguous original TIẾP in the transcript. Reservation and
        # its exact repair prompt survive a crash before the FIFO enqueue.
        self._save_checkpoint()

    def _validate_stalled_content_repair(self) -> None:
        record = self.stalled_content_repair
        if record.get("status") != "enqueued" or not self.turns or self.turns[-1].role.casefold() != "assistant":
            return
        artifacts = extract_content_artifacts(self.turns, self.minimum_characters)
        latest = extract_content_artifacts([self.turns[-1]], self.minimum_characters)
        if ((latest.voicevox_script and hashlib.sha256(latest.voicevox_script.encode("utf-8")).hexdigest() != record.get("voice_sha256"))
            or (latest.title and record.get("title") and latest.title != record["title"])
            or hashlib.sha256(artifacts.voicevox_script.encode("utf-8")).hexdigest() != record.get("voice_sha256")
            or (record.get("title") and artifacts.title != record["title"])):
            raise ContentAIError("Lượt bổ sung đã thay đổi kịch bản/tiêu đề cũ; giữ checkpoint để kiểm tra, không render hoặc upload.")
        record["status"] = "completed"
        self._save_checkpoint()

    def _resume_stalled_content_repair(self, *, cancel_event: threading.Event,
        on_turn: TurnCallback | None, on_partial: PartialCallback | None,
        on_status: StatusCallback | None) -> str:
        record = self.stalled_content_repair
        if (record.get("status") != "reserved" or record.get("thread_id") != self.chat.thread_id
            or record.get("transcript_sha256") != self._stalled_repair_transcript_hash()
            or not str(record.get("prompt") or "").startswith(CONTENT_REPAIR_PREFIX)):
            raise ContentAIError("Checkpoint lượt bổ sung không khớp đúng hội thoại; giữ dữ liệu, không gửi thêm.")
        if on_status is not None:
            on_status("content-repair", "Đã xác minh kịch bản có sẵn và chat rảnh; tự yêu cầu phần thiếu một lần trong đúng chat.")
        record["status"] = "enqueued"
        # _send writes the enqueued state and the new user turn atomically,
        # before HTTP. A cancelled FIFO wait uses the existing unsent marker.
        result = self._send(str(record["prompt"]), cancel_event=cancel_event,
            on_turn=on_turn, on_partial=on_partial, on_status=on_status)
        self._validate_stalled_content_repair()
        return result

    def _artifact_progress(self, artifacts: ContentArtifacts) -> float:
        progress = 0.12
        if artifacts.title:
            progress = 0.25
        if artifacts.voicevox_script:
            target = max(1, round(self.minutes * 305))
            progress = max(
                progress,
                0.30 + 0.35 * min(1.0, len(artifacts.voicevox_script) / target),
            )
        if artifacts.description:
            progress = max(progress, 0.74)
        if artifacts.thumbnail_prompt:
            progress = max(progress, 0.84)
        if artifacts.image_prompts:
            progress = max(
                progress,
                0.85
                + 0.12
                * min(1.0, len(artifacts.image_prompts) / self.expected_images),
            )
        return progress

    def run(
        self,
        master_prompt: str,
        *,
        cancel_event: threading.Event | None = None,
        on_turn: TurnCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_partial: PartialCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> ContentRunResult:
        cancel = cancel_event or threading.Event()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.master_prompt_sha256 = hashlib.sha256(
            master_prompt.strip().encode("utf-8")
        ).hexdigest()
        self.expected_images = expected_image_prompt_count(master_prompt, self.minutes)
        recovered = self._load_checkpoint(master_prompt)
        from . import output_contract
        if output_contract.enabled(master_prompt) and not self.output_contract_id:
            self.output_contract_id = uuid.uuid4().hex
        if self.recover_submitted_only and (
            recovered is None or not recovered[0]
            or recovered[0][-1].role.casefold() != "user"
        ):
            raise ContentAIError(
                "Không có bằng chứng phản hồi của đúng lượt đang chờ; giữ checkpoint, không gửi lại prompt."
            )
        resumed_partial = False
        if recovered is not None:
            turns, artifacts, thread_id, complete = recovered
            self.turns = turns
            self.chat.thread_id = thread_id
            self._validate_stalled_content_repair()
            if complete:
                _atomic_write_text(
                    self.package_path,
                    format_content_package(artifacts),
                )
                self._progress(
                    on_progress,
                    1.0,
                    "Checkpoint đã đủ dữ liệu · bỏ qua ChatGPT và tiếp tục tạo ảnh/VOICEVOX/render…",
                )
                if on_status is not None:
                    on_status(
                        "workflow-recovered",
                        f"Đã phục hồi {len(artifacts.image_prompts)}/{self.expected_images} prompt ảnh từ checkpoint.",
                    )
                return ContentRunResult(
                    artifacts=artifacts,
                    package_path=self.package_path,
                    transcript_path=self.transcript_path,
                    checkpoint_path=self.checkpoint_path,
                    thread_id=thread_id,
                    turns=tuple(self.turns),
                )
            resumed_partial = True
            self._progress(
                on_progress,
                self._artifact_progress(artifacts),
                (
                    f"Đã phục hồi checkpoint dở dang · tiếp tục từ "
                    f"{len(artifacts.image_prompts)}/{self.expected_images} prompt ảnh…"
                ),
            )
            if on_status is not None:
                on_status(
                    "workflow-resuming",
                    "Đã đọc checkpoint dở dang; đang xác minh conversation ChatGPT trước khi gửi thêm lượt nào.",
                )

            if self.stalled_content_repair.get("status") == "reserved":
                self._resume_stalled_content_repair(cancel_event=cancel,
                    on_turn=on_turn, on_partial=on_partial, on_status=on_status)
                artifacts = extract_content_artifacts(self.turns, self.minimum_characters)

            # A previous build could checkpoint the replacement Master Prompt
            if self.pending_turn_transport_started is False and self.turns[-1].role.casefold()=="user":
                queued_prompt=self.turns.pop().text
                self.pending_turn_transport_started=None
                self._save_checkpoint()
                if on_status is not None:
                    on_status("workflow-resuming-queued", "Lượt chỉ nằm trong hàng chờ, chưa bắt đầu truyền; nối lại đúng lượt một lần.")
                self._send(queued_prompt,cancel_event=cancel,on_turn=on_turn,on_partial=on_partial,on_status=on_status)
                self._validate_stalled_content_repair()
                artifacts=extract_content_artifacts(self.turns,self.minimum_characters)

            # A previous build could checkpoint the replacement Master Prompt
            # after the first conversation reported a missing attachment but
            # before that replacement turn received any response.  The bridge
            # may then be restarted, orphaning the old thread ID.  This exact
            # shape is safe to move to a fresh conversation once: the latest
            # user turn is known to be the old repair, while no answer can
            # belong to it yet.  Do not use this branch for an ordinary
            # trailing user turn; those remain fail-closed below.
            safe_master_retry_pending = master_prompt_attachment_retry_pending_in_turns(
                self.turns,
                master_prompt,
            )
            if safe_master_retry_pending:
                if self.conversation_recovery_attempts >= 1:
                    raise ContentAIError(
                        "Conversation ChatGPT cũ đã mất và lượt Master Prompt sạch đã "
                        "được thử một lần nhưng chưa hoàn tất. Checkpoint được giữ; "
                        "tool không tự gửi lại lần nữa để tránh tạo nội dung trùng."
                    )
                # Drop only the locally checkpointed, pre-submit retry.  Keep
                # the earlier refusal in the transcript for auditability, but
                # clear the transport identity so the next _send creates a
                # new saved ChatGPT conversation instead of resuming HTTP 410.
                self.turns.pop()
                self.chat.thread_id = ""
                self.conversation_recovery_attempts = 1
                self.conversation_recovery_pending = True
                self._save_checkpoint()
                # The marker above is intentionally durable until this point.
                # Clearing it before _send is safe: _send checkpoints the new
                # user turn (and the new bridge thread) before any answer is
                # accepted.  If cancellation/crash happens before _send, the
                # marker remains on disk and _load_checkpoint fails closed.
                self.conversation_recovery_pending = False
                resumed_partial = False
                if on_status is not None:
                    on_status(
                        "chatgpt-conversation-recovery",
                        "Conversation ChatGPT cũ không còn trong bridge; checkpoint xác nhận "
                        "lượt sửa tệp chưa có phản hồi. Đang tạo conversation sạch và gửi "
                        "lại đúng Master Prompt kèm tệp đúng một lần…",
                    )
            else:
                # Older builds could checkpoint a normal assistant refusal saying
                # that the large Master Prompt file was not visible, then leave a
                # locally saved confirmation request as the trailing user turn.
                # Sending that confirmation again cannot repair the missing file.
                # Prove the trailing request is not already in the exact DOM, drop
                # only that unsent local turn, and resend the Master Prompt once so
                # the bridge's strict attachment/UI verification gets a chance.
                attachment_failure = master_prompt_attachment_failure_in_turns(
                    self.turns,
                    master_prompt,
                )
                if attachment_failure:
                    if self.master_attachment_repair_attempts >= 1:
                        raise ContentAIError(
                            "ChatGPT vẫn không nhìn thấy tệp Master Prompt sau một lần tự sửa. "
                            "Checkpoint được giữ; hãy kiểm tra đúng conversation và trạng thái "
                            "đính kèm trước khi bấm Tiếp tục lại. Tool không gửi lặp thêm để tránh trùng."
                        )
                    recover_pending = getattr(self.chat, "recover_pending", None)
                    if not callable(recover_pending):
                        raise ContentAIError(
                            "Checkpoint có phản hồi thiếu tệp Master Prompt nhưng bridge hiện tại "
                            "không hỗ trợ xác minh lượt cuối; tool dừng để không gửi trùng."
                        )
                    trailing_is_confirmation = bool(
                        self.turns
                        and self.turns[-1].role.casefold() == "user"
                        and "đọc toàn bộ master prompt" in self.turns[-1].text.casefold()
                        and "đã nạp" in self.turns[-1].text.casefold()
                    )
                    if trailing_is_confirmation:
                        if on_status is not None:
                            on_status(
                                "master-attachment-repair-check",
                                "Đang xác minh yêu cầu xác nhận cũ chưa gửi trước khi sửa lượt đính kèm…",
                            )
                        recovered_confirmation = recover_pending(
                            cancel_event=cancel,
                            on_partial=on_partial,
                            on_status=on_status,
                        )
                        if recovered_confirmation:
                            self._append_turn(
                                "assistant",
                                str(recovered_confirmation),
                                on_turn,
                            )
                        else:
                            # This user turn was checkpointed before trusted Send;
                            # it is safe to remove only after the exact bridge DOM
                            # has proved that no answer belongs to it.
                            self.turns.pop()
                            self._save_checkpoint()
                    self.master_attachment_repair_attempts += 1
                    self._save_checkpoint()
                    resumed_partial = False
                    if on_status is not None:
                        on_status(
                            "master-attachment-repair",
                            "Phản hồi cũ xác nhận tệp chưa tới ChatGPT · đang gửi lại đúng Master Prompt một lần với kiểm tra tệp chặt hơn…",
                        )

            # ``_send`` checkpoints the user turn before waiting for the web
            # answer.  A timeout therefore leaves one trailing user turn.  Do
            # not guess the next prompt or send TIẾP again: first recover the
            # answer that already belongs to that turn from the exact saved
            # ChatGPT DOM.
            if (
                not safe_master_retry_pending
                and self.turns
                and self.turns[-1].role.casefold() == "user"
            ):
                recover_pending = getattr(self.chat, "recover_pending", None)
                if not callable(recover_pending):
                    raise ContentAIError(
                        "Checkpoint đang chờ một phản hồi ChatGPT nhưng bridge hiện tại "
                        "không hỗ trợ phục hồi DOM. Tool đã dừng để không gửi trùng."
                    )
                self._progress(
                    on_progress,
                    self._artifact_progress(artifacts),
                    "Đang nối lại đúng DOM ChatGPT và thu hồi phản hồi còn dang dở…",
                )
                recovery_options = {}
                if isinstance(self.chat, ChatGPTWebChat):
                    recovery_options["pending_prompt"] = self.turns[-1].text
                    if len(self.turns)>=3 and self.turns[-2].role.casefold()=="assistant" and self.turns[-3].role.casefold()=="user":
                        recovery_options["completed_turn_baseline"]={"user":self.turns[-3].text,"assistant":self.turns[-2].text}
                        recovery_options["allow_missing_content_repair"] = self._can_request_stalled_content_repair(artifacts)
                recovered_response = recover_pending(
                    cancel_event=cancel,
                    on_partial=on_partial,
                    on_status=on_status,
                    **recovery_options,
                )
                queued_unsent_proved=False
                already_checkpointed=False
                if isinstance(recovered_response, dict) and recovered_response.get("status") == "recovery-baseline":
                    self._reserve_stalled_content_repair(artifacts, recovered_response)
                    recovered_response = self._resume_stalled_content_repair(cancel_event=cancel,
                        on_turn=on_turn, on_partial=on_partial, on_status=on_status)
                    already_checkpointed = True
                if isinstance(recovered_response, dict):
                    queued_unsent_proved=(recovered_response.get("proof")=="queued-after-completed-turn"
                        and recovered_response.get("safe_to_resend") is True)
                    queued_unsent_proved = queued_unsent_proved or (
                        recovered_response.get('proof') == 'empty-pre-submit-attachment'
                        and recovered_response.get('attachment_recovery_verified') is True
                        and recovered_response.get('safe_to_resend') is True)
                    queued_unsent_proved = queued_unsent_proved or (
                        recovered_response.get('proof') == 'failed-before-master-input'
                        and recovered_response.get('early_master_recovery_verified') is True
                        and recovered_response.get('safe_to_resend') is True)
                    if (
                        recovered_response.get("status") != "not-submitted"
                        or recovered_response.get("safe_to_resend") is not True
                        or (self.recover_submitted_only and not queued_unsent_proved)
                    ):
                        raise ContentAIError(
                            "Không có bằng chứng phản hồi của đúng lượt đã gửi; giữ checkpoint, không gửi lại prompt."
                        )
                    recovered_response = None
                if not recovered_response:
                    if self.recover_submitted_only and not queued_unsent_proved:
                        raise ContentAIError(
                            "Không có bằng chứng phản hồi của đúng lượt đã gửi; giữ checkpoint, không gửi lại prompt."
                        )
                    pending_prompt = self.turns.pop().text
                    self.pending_turn_transport_started=None
                    self._save_checkpoint()
                    if on_status is not None:
                        on_status(
                            "workflow-resending-unsubmitted",
                            "Tin nhắn trước chưa từng được gửi lên ChatGPT; đang gửi lại đúng tin đó một lần.",
                        )
                    recovered_response = self._send(
                        pending_prompt,
                        cancel_event=cancel,
                        on_turn=on_turn,
                        on_partial=on_partial,
                        on_status=on_status,
                    )
                    already_checkpointed=True
                if not already_checkpointed:
                    self._append_turn("assistant", recovered_response, on_turn)
                self._validate_stalled_content_repair()
                artifacts = extract_content_artifacts(self.turns, self.minimum_characters)
                if on_status is not None:
                    on_status(
                        "workflow-dom-recovered",
                        "Đã thu hồi phản hồi từ đúng DOM và ghi vào checkpoint; giờ mới tiếp tục workflow.",
                    )
                exported_reply = final_export_reply_in_turns(self.turns)
                confirmed = image_completion_confirmed_in_turns(self.turns)
                if exported_reply and confirmed:
                    artifacts = merge_exported_artifacts(
                        exported_reply,
                        artifacts,
                        self.minutes,
                        expected_images=self.expected_images,
                        allowed_missing_images=DEFAULT_ALLOWED_MISSING_IMAGE_PROMPTS,
                    )
                    if artifacts_complete(
                        artifacts,
                        self.minutes,
                        expected_images=self.expected_images,
                        allowed_missing_images=DEFAULT_ALLOWED_MISSING_IMAGE_PROMPTS,
                    minimum_characters=self.minimum_characters,
                       ):
                        artifacts = self._enforce_output_contract(artifacts, master_prompt, cancel,
                            on_turn=on_turn, on_partial=on_partial, on_status=on_status)
                        _atomic_write_text(
                            self.package_path,
                            format_content_package(artifacts),
                        )
                        self._record_output_contract_package(master_prompt)
                        self._save_checkpoint()
                        self._progress(
                            on_progress,
                            1.0,
                            "Đã thu hồi file tổng hợp cuối từ DOM · tiếp tục công đoạn sau ChatGPT…",
                        )
                        return ContentRunResult(
                            artifacts=artifacts,
                            package_path=self.package_path,
                            transcript_path=self.transcript_path,
                            checkpoint_path=self.checkpoint_path,
                            thread_id=self.chat.thread_id,
                            turns=tuple(self.turns),
                        )

        confirmation_request = (
            'Nếu đã đọc toàn bộ Master Prompt thì hãy trả lời "ĐÃ NẠP" '
            "và không nói gì thêm."
        )
        master_loaded = any(
            turn.role.casefold() == "assistant"
            and is_master_prompt_loaded_confirmation(turn.text)
            for turn in self.turns
        )
        if not resumed_partial:
            complete_master_message = master_prompt + self.production_context
            self._progress(on_progress, 0.02, "Đang nạp toàn bộ nội dung Master Prompt vào ChatGPT…")
            self._send(
                complete_master_message,
                cancel_event=cancel,
                on_turn=on_turn,
                on_partial=on_partial,
                on_status=on_status,
                send_text=complete_master_message,
            )
        if not master_loaded:
            self._progress(on_progress, 0.04, "Đang yêu cầu ChatGPT xác nhận đã nạp Master Prompt…")
            confirmation_response = self._send(
                confirmation_request,
                cancel_event=cancel,
                on_turn=on_turn,
                on_partial=on_partial,
                on_status=on_status,
            )
            if not is_master_prompt_loaded_confirmation(confirmation_response):
                raise ContentAIError(
                    'ChatGPT chưa trả đúng "ĐÃ NẠP"; tool đã dừng trước bước BẮT ĐẦU.'
                )

        history_context = (
            format_title_history_for_prompt(
                self.title_history,
                total_count=self.title_history_total,
            )
            if self.title_history_enabled
            else ""
        )
        if self.title_history_enabled and not history_context:
            history_context = (
                "CHỐNG TRÙNG NỘI DUNG — ĐANG BẬT\n"
                "Sổ lịch sử chưa có tiêu đề cũ. Hãy tạo một chủ đề mới và tiêu đề cụ thể."
            )
        workflow_started = any(
            turn.role.casefold() == "user" and turn.text.lstrip().startswith("BẮT ĐẦU")
            for turn in self.turns
        )
        if self.title_history_enabled and not workflow_started:
            history_message = (
                f"{CONTENT_HISTORY_MARKER}\n\n"
                f"{history_context}\n\n"
                "Hãy đọc và ghi nhớ checklist này để nội dung sắp tạo không lặp lại. "
                "Chưa bắt đầu viết nội dung ở bước này."
            )
            history_sent = any(
                turn.role.casefold() == "user"
                and turn.text.lstrip().startswith(CONTENT_HISTORY_MARKER)
                for turn in self.turns
            )
            if not history_sent:
                self._progress(
                    on_progress,
                    0.06,
                    f"Đang nạp checklist {self.title_history_total} nội dung đã làm của workspace…",
                )
                self._send(
                    history_message,
                    cancel_event=cancel,
                    on_turn=on_turn,
                    on_partial=on_partial,
                    on_status=on_status,
                )
            history_loaded = content_history_loaded_in_turns(self.turns)
            if not history_loaded:
                history_confirmation = self._send(
                    CONTENT_HISTORY_CONFIRMATION_REQUEST,
                    cancel_event=cancel,
                    on_turn=on_turn,
                    on_partial=on_partial,
                    on_status=on_status,
                )
                if not is_content_history_loaded_confirmation(history_confirmation):
                    raise ContentAIError(
                        'ChatGPT chưa trả đúng "ĐÃ XONG" sau khi nạp checklist. '
                        "Tool đã dừng trước bước BẮT ĐẦU để không tạo nội dung trùng."
                    )
            if on_status is not None:
                on_status(
                    "content-history-loaded",
                    f"ChatGPT đã xác nhận ĐÃ XONG checklist {self.title_history_total} nội dung · tiếp tục workflow.",
                )
        start_message = (
            "BẮT ĐẦU\n\n"
            f"Thời lượng yêu cầu: {self.minutes:g} phút.\n"
            "Hãy tự thực hiện toàn bộ các bước theo master prompt. Khi một phản hồi đã đầy, "
            "dừng đúng marker để tôi gửi TIẾP."
        )
        start_message += output_contract.instruction(output_contract.identity(
            master_prompt, self.output_contract_id, self.expected_images))
        if not workflow_started:
            self._progress(on_progress, 0.08, "Master prompt đã nạp · bắt đầu tuyển chọn và viết…")
            response = self._send(
                start_message,
                cancel_event=cancel,
                on_turn=on_turn,
                on_partial=on_partial,
                on_status=on_status,
            )
        else:
            response = next(
                (
                    turn.text
                    for turn in reversed(self.turns)
                    if turn.role.casefold() == "assistant"
                ),
                "",
            )
        runtime_sent_again = any(
            turn.role.casefold() == "user"
            and re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*phút\s*", turn.text, re.IGNORECASE)
            for turn in self.turns
        )
        image_completion_question = IMAGE_COMPLETION_QUESTION
        image_completion_confirmed = image_completion_confirmed_in_turns(self.turns)
        image_continue_pending_check = bool(
            resumed_partial
            and image_prompt_phase_started(self.turns)
            and response
            and not is_image_prompt_done_confirmation(response)
            and not is_image_prompt_not_done_confirmation(response)
        )
        title_dedup_corrections = 0

        # Keep a small separate repair/confirmation allowance. Retrying the
        # pipeline must never grant another batch of ordinary TIẾP requests.
        for content_turn in range(1, self.max_turns + MAX_CONTENT_REPAIR_ATTEMPTS + 2):
            # A resumed checkpoint may already end immediately after the exact
            # image-completion question. Never send TIẾP after ĐÃ XONG; the
            # final-package check below requests any missing fields instead.
            if image_completion_confirmed:
                break
            artifacts = extract_content_artifacts(self.turns, self.minimum_characters)
            # Completion markers in newer Master Prompts do not prove that
            # the package has its required fields. Ask for the missing data,
            # never send another blind TIẾP after the same exhausted answer.
            assistant_tail = [t.text for t in self.turns[-6:]
                              if t.role.casefold() == "assistant"]
            repeated = (len(assistant_tail) == 3
                        and len(set(assistant_tail)) == 1)
            last_request = next((t.text for t in reversed(self.turns)
                                 if t.role.casefold() == "user"), "")
            budget_exhausted = _content_continue_count(self.turns) >= self.max_turns - 1
            stalled = _content_continuations_stalled(self.turns)
            if (_content_end_signal(response) or repeated or stalled or budget_exhausted
                or last_request.startswith(CONTENT_REPAIR_PREFIX)) and _missing_content_fields(
                    artifacts, self.expected_images, self.minimum_characters
                ):
                if on_status is not None and (stalled or budget_exhausted):
                    on_status("content-stalled", "Các lượt TIẾP không bổ sung phần còn thiếu; "
                              "chuyển sang khôi phục có giới hạn trong đúng hội thoại.")
                response = self._request_content_repair(
                    artifacts,
                    cancel_event=cancel, on_turn=on_turn, on_partial=on_partial,
                    on_status=on_status,
                )
                image_continue_pending_check = False
                continue
            if (repeated or stalled or budget_exhausted) and last_request == image_completion_question and artifacts_complete(
                artifacts, self.minutes, expected_images=self.expected_images,
                minimum_characters=self.minimum_characters,
            ):
                self.content_locally_validated = True
                self._save_checkpoint()
                if on_status is not None:
                    on_status("content-validated", "Đã kiểm tra đủ nội dung và prompt ảnh liên tiếp; kết thúc vòng xác nhận không tiến triển.")
                break
            duplicate = (
                find_duplicate_title(artifacts.title, self.title_history)
                if self.title_history_enabled and artifacts.title
                else None
            )
            if duplicate is not None:
                title_dedup_corrections += 1
                if title_dedup_corrections > 3:
                    raise ContentAIError(
                        "ChatGPT đã lặp lại tiêu đề/nội dung cũ quá 3 lần. Tool giữ "
                        "checkpoint và dừng trước Flow/VOICEVOX/render để không tạo video trùng."
                    )
                previous_title, score = duplicate
                if on_status is not None:
                    on_status(
                        "title-duplicate",
                        f"Phát hiện tiêu đề trùng {score:.0%} với ‘{previous_title}’ · đang yêu cầu đổi chủ đề.",
                    )
                response = self._send(
                    (
                        "CHỐNG TRÙNG — TIÊU ĐỀ VỪA CHỌN KHÔNG ĐƯỢC DÙNG.\n\n"
                        f"Tiêu đề vừa chọn: {artifacts.title}\n"
                        f"Nội dung cũ bị trùng/gần trùng: {previous_title}\n\n"
                        "Hãy hủy lựa chọn hiện tại, chọn một chủ đề khác rõ rệt về sự kiện, "
                        "luận điểm và nội dung; sau đó làm lại toàn bộ từ marker chủ đề/tiêu đề. "
                        "Không chỉ đổi vài từ trong tiêu đề."
                    ),
                    cancel_event=cancel,
                    on_turn=on_turn,
                    on_partial=on_partial,
                    on_status=on_status,
                )
                image_completion_confirmed = False
                image_continue_pending_check = False
                continue
            expected_images = self.expected_images
            in_image_phase = image_prompt_phase_started(self.turns)
            progress = self._artifact_progress(artifacts)
            self._progress(
                on_progress,
                progress,
                (
                    f"Đã nhận thoại và {len(artifacts.image_prompts)}/"
                    f"{expected_images} prompt ảnh…"
                    if artifacts.voicevox_script
                    else "Đang nhận chủ đề, tiêu đề và các PART kịch bản…"
                ),
            )
            if len(artifacts.image_prompts) > MAX_EXPECTED_IMAGE_PROMPTS:
                raise ContentAIError(
                    f"ChatGPT đã xuất hơn giới hạn an toàn {MAX_EXPECTED_IMAGE_PROMPTS} "
                    "prompt ảnh cho một video. Tool giữ checkpoint và dừng trước khi gửi "
                    "một lô quá lớn sang Flow."
                )
            if in_image_phase and (
                image_continue_pending_check
                or artifacts_complete(
                    artifacts,
                    self.minutes,
                    expected_images=expected_images,
                    allowed_missing_images=0,
                minimum_characters=self.minimum_characters,
                   )
            ):
                self._progress(
                    on_progress,
                    progress,
                    "Đang hỏi ChatGPT đã hoàn tất phần prompt hình ảnh chưa…",
                )
                completion_response = self._send(
                    image_completion_question,
                    cancel_event=cancel,
                    on_turn=on_turn,
                    on_partial=on_partial,
                    on_status=on_status,
                )
                image_continue_pending_check = False
                if is_image_prompt_done_confirmation(completion_response):
                    image_completion_confirmed = True
                    if on_status is not None:
                        on_status(
                            "workflow-complete",
                            (
                                'ChatGPT đã xác nhận "ĐÃ XONG" · dừng gửi TIẾP ngay'
                                f" · hiện có {len(artifacts.image_prompts)}/{expected_images} prompt ảnh"
                                " · chuyển sang xuất file tổng hợp"
                                f" · lượt nội dung {content_turn}/{self.max_turns}"
                            ),
                        )
                    break
                elif _content_continue_count(self.turns) >= self.max_turns - 1 and artifacts_complete(
                    artifacts, self.minutes, expected_images=expected_images,
                    minimum_characters=self.minimum_characters,
                ):
                    self.content_locally_validated = True
                    self._save_checkpoint()
                    if on_status is not None:
                        on_status("content-validated", "Đã kiểm tra đủ nội dung thực tế; dừng vòng xác nhận ảnh lặp.")
                    break
                elif on_status is not None:
                    on_status(
                        "image-confirmation-pending",
                        (
                            'ChatGPT xác nhận "CHƯA XONG" · tool sẽ gửi TIẾP'
                            if is_image_prompt_not_done_confirmation(completion_response)
                            else "Phản hồi xác nhận ảnh chưa đúng mẫu · tool coi là chưa xong và gửi TIẾP"
                        ),
                    )
            if _content_continue_count(self.turns) >= self.max_turns - 1:
                if on_status is not None:
                    on_status(
                        "workflow-limit",
                        f"Đã chạm giới hạn an toàn {self.max_turns} lượt · dừng ChatGPT.",
                    )
                raise ContentAIError(
                    "Đã đạt giới hạn lượt tự động nhưng master prompt chưa xuất đủ nội dung. "
                    "Bạn có thể dùng khung chat để yêu cầu AI tiếp tục."
                )
            lowered = response.casefold()
            asks_runtime = (
                not runtime_sent_again
                and not artifacts.voicevox_script
                and ("bao nhiêu phút" in lowered or "số phút" in lowered or "thời lượng" in lowered)
            )
            next_message = f"{self.minutes:g} phút" if asks_runtime else "TIẾP"
            runtime_sent_again = runtime_sent_again or asks_runtime
            response = self._send(
                next_message,
                cancel_event=cancel,
                on_turn=on_turn,
                on_partial=on_partial,
                on_status=on_status,
            )
            if next_message == "TIẾP" and image_prompt_phase_started(self.turns):
                image_continue_pending_check = True

        artifacts = extract_content_artifacts(self.turns, self.minimum_characters)
        if not image_completion_confirmed and not self.content_locally_validated:
            raise ContentAIError(
                "AI kết thúc nhưng chưa xác nhận ĐÃ XONG cho phần prompt ảnh."
            )
        # Reuse the export already saved by an older build. Repeating export
        # cannot repair the same missing fields and spends another Web turn.
        exported_response = final_export_reply_in_turns(self.turns)
        if not exported_response:
            self._progress(
                on_progress,
                0.985,
                "ChatGPT đã xác nhận ĐÃ XONG · đang yêu cầu in văn bản tổng hợp cuối…",
            )
            exported_response = self._send(
                FINAL_EXPORT_REQUEST,
                cancel_event=cancel,
                on_turn=on_turn,
                on_partial=on_partial,
                on_status=on_status,
            )
        artifacts = merge_exported_artifacts(
            exported_response,
            artifacts,
            self.minutes,
            expected_images=self.expected_images,
            allowed_missing_images=DEFAULT_ALLOWED_MISSING_IMAGE_PROMPTS,
        )
        while not artifacts_complete(
            artifacts,
            self.minutes,
            expected_images=self.expected_images,
            allowed_missing_images=DEFAULT_ALLOWED_MISSING_IMAGE_PROMPTS,
        minimum_characters=self.minimum_characters,
                  ):
            self._request_content_repair(
                artifacts,
                cancel_event=cancel,
                on_turn=on_turn,
                on_partial=on_partial,
                on_status=on_status,
            )
            artifacts = _fill_missing_content_artifacts(
                artifacts, extract_content_artifacts(self.turns, self.minimum_characters), self.minimum_characters
            )
        if self.content_repair_attempts and on_status is not None:
            on_status(
                "content-repair-complete",
                "Đã kiểm tra đủ các trường nội dung sau bổ sung · tiếp tục workflow.",
            )
        if self.title_history_enabled:
            duplicate = find_duplicate_title(artifacts.title, self.title_history)
            repair_attempt = 0
            while duplicate is not None and repair_attempt < 2:
                repair_attempt += 1
                previous_title, score = duplicate
                if on_status is not None:
                    on_status(
                        "title-export-duplicate",
                        f"File tổng hợp đổi về tiêu đề trùng {score:.0%} · đang sửa trước khi lưu.",
                    )
                repaired_response = self._send(
                    (
                        "FILE TỔNG HỢP VỪA XUẤT BỊ TRÙNG NỘI DUNG CŨ VÀ KHÔNG ĐƯỢC CHẤP NHẬN.\n\n"
                        f"Tiêu đề bị trùng: {artifacts.title}\n"
                        f"Trùng/gần trùng với: {previous_title}\n\n"
                        "Hãy chọn một chủ đề khác rõ rệt rồi trình bày lại TOÀN BỘ nội dung ngay "
                        "trong thân câu trả lời dạng văn bản thuần: Tiêu đề, Mô tả, SEO tags, "
                        "Prompt Thumbnail, toàn bộ Thoại VOICEVOX và toàn bộ Prompt hình ảnh. "
                        "Không tạo hoặc đính kèm tệp, tài liệu, Canvas hay artifact. "
                        "Không giải thích gì thêm."
                    ),
                    cancel_event=cancel,
                    on_turn=on_turn,
                    on_partial=on_partial,
                    on_status=on_status,
                )
                blank = ContentArtifacts("", "", "", "", "", ())
                repaired = merge_exported_artifacts(
                    repaired_response,
                    blank,
                    self.minutes,
                    expected_images=self.expected_images,
                    allowed_missing_images=DEFAULT_ALLOWED_MISSING_IMAGE_PROMPTS,
                )
                if artifacts_complete(
                    repaired,
                    self.minutes,
                    expected_images=self.expected_images,
                    allowed_missing_images=DEFAULT_ALLOWED_MISSING_IMAGE_PROMPTS,
                minimum_characters=self.minimum_characters,
                   ):
                    artifacts = repaired
                duplicate = find_duplicate_title(artifacts.title, self.title_history)
            if duplicate is not None:
                raise ContentAIError(
                    "File tổng hợp vẫn trùng tiêu đề/nội dung cũ sau các lần sửa an toàn. "
                    "Tool giữ checkpoint và dừng trước Flow/VOICEVOX/render."
                )
        if not artifacts_complete(
            artifacts,
            self.minutes,
            expected_images=self.expected_images,
            allowed_missing_images=DEFAULT_ALLOWED_MISSING_IMAGE_PROMPTS,
        minimum_characters=self.minimum_characters,
               ):
            raise ContentAIError(
                'ChatGPT đã xác nhận "ĐÃ XONG" nhưng file tổng hợp cuối vẫn thiếu '
                "tiêu đề, thoại, mô tả, thumbnail hoặc prompt ảnh. Checkpoint đã được giữ; "
                "tool không gửi TIẾP để tránh lặp vô hạn."
            )
        artifacts = self._enforce_output_contract(artifacts, master_prompt, cancel,
            on_turn=on_turn, on_partial=on_partial, on_status=on_status)
        _atomic_write_text(
            self.package_path,
            format_content_package(artifacts),
        )
        self._record_output_contract_package(master_prompt)
        self._save_checkpoint()
        self._progress(on_progress, 1.0, f"Đã xuất {self.package_path.name}")
        return ContentRunResult(
            artifacts=artifacts,
            package_path=self.package_path,
            transcript_path=self.transcript_path,
            checkpoint_path=self.checkpoint_path,
            thread_id=self.chat.thread_id,
            turns=tuple(self.turns),
        )

    def _record_output_contract_package(self, master_prompt):
        from .output_contract import enabled
        if enabled(master_prompt):
            self.output_contract_package_sha256 = hashlib.sha256(self.package_path.read_bytes()).hexdigest()

    def _enforce_output_contract(self, artifacts, master_prompt, cancel, *, on_turn, on_partial, on_status):
        from . import output_contract
        from dataclasses import replace
        expected = output_contract.identity(master_prompt, self.output_contract_id, self.expected_images)
        if expected is None:
            return artifacts
        baseline = artifacts
        def adopt(reply):
            candidate = merge_exported_artifacts(reply, ContentArtifacts('', '', '', '', '', ()), self.minutes,
                expected_images=self.expected_images, allowed_missing_images=0)
            failed = output_contract.problems(expected, baseline, self.turns)
            fields = {'title':'title', 'description':'description', 'thumbnail':'thumbnail_prompt',
                      'voice':'voicevox_script', 'images':'image_prompts', 'seo_tags':'seo_tags'}
            keep = {attribute:getattr(baseline, attribute) for key, attribute in fields.items() if key not in failed}
            # A formatting repair may move line breaks, never rewrite the words.
            strip = lambda text: re.sub(r'\s+', '', text)
            if strip(candidate.voicevox_script) != strip(baseline.voicevox_script):
                keep['voicevox_script'] = baseline.voicevox_script
            candidate = replace(candidate, **keep, selected_topic=baseline.selected_topic)
            return replace(candidate, voicevox_export_script='\n'.join('A: '+line for line in candidate.voicevox_script.splitlines()))
        previous = output_contract.latest_repair_reply(self.turns)
        if previous:
            artifacts = adopt(previous)
        errors = output_contract.problems(expected, artifacts, self.turns)
        while errors:
            if self.content_repair_attempts >= MAX_CONTENT_REPAIR_ATTEMPTS:
                raise ContentAIError('CONTENT_OUTPUT_CONTRACT_FAILED: hết ngân sách sửa cấu trúc; giữ checkpoint. '
                                     + json.dumps(errors, ensure_ascii=False))
            self.content_repair_attempts += 1
            self._save_checkpoint()  # Reserve before sending; restart cannot renew the budget.
            if on_status:
                on_status('output-contract-repair', 'Output chưa đúng hợp đồng; sửa đúng gói, không tạo video khác.')
            reply = self._send(output_contract.repair_request(CONTENT_REPAIR_PREFIX, expected, errors, artifacts,
                              winner=output_contract.selected_title(self.turns)),
                cancel_event=cancel, on_turn=on_turn, on_partial=on_partial, on_status=on_status)
            candidate = adopt(reply)
            errors = output_contract.problems(expected, candidate, self.turns)
            if not artifacts_complete(candidate, self.minutes, expected_images=self.expected_images,
                                      minimum_characters=self.minimum_characters):
                errors['package'] = 'Gói sửa vẫn thiếu nguyên liệu hoặc chứa dữ liệu chưa hợp lệ.'
            artifacts = candidate
        return artifacts

    def send_manual(
        self,
        text: str,
        *,
        cancel_event: threading.Event | None = None,
        on_turn: TurnCallback | None = None,
        on_partial: PartialCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> str:
        cancel = cancel_event or threading.Event()
        response = self._send(
            text,
            cancel_event=cancel,
            on_turn=on_turn,
            on_partial=on_partial,
            on_status=on_status,
        )
        artifacts = extract_content_artifacts(self.turns, self.minimum_characters)
        if artifacts_complete(
            artifacts,
            self.minutes,
            expected_images=self.expected_images,
            allowed_missing_images=DEFAULT_ALLOWED_MISSING_IMAGE_PROMPTS,
        minimum_characters=self.minimum_characters,
           ):
            _atomic_write_text(
                self.package_path,
                format_content_package(artifacts),
            )
        return response
