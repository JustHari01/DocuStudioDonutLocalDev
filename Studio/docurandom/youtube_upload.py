from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Collection

from PIL import Image, ImageOps, UnidentifiedImageError
from websockets.sync.client import connect as websocket_connect

from .browser_extension_bridge import (
    BROWSER_EXTENSION_ID,
    BrowserExtensionBridge,
    BrowserExtensionBridgeError,
    get_browser_extension_bridge,
)
from .donut_mcp import (
    DonutMcpAuthenticationError,
    DonutMcpClient,
    DonutMcpEndpoint,
    DonutMcpEntitlementError,
    DonutMcpError,
    DonutProfile,
)
from .ffmpeg_manager import find_tools
from .pipeline_checkpoint import inspect_workspace_pipeline, read_pipeline_checkpoint
from .narration_identity import narration_digest, package_narration, validate_project_narration
from .voicevox import VoicevoxError
from .storage import app_data_dir, atomic_write_json
from .workspace import Workspace


logger = logging.getLogger(__name__)


YOUTUBE_STUDIO_URL = "https://studio.youtube.com/"
_CHANNEL_STUDIO_URL = re.compile(
    r"^https://studio\.youtube\.com/channel/([A-Za-z0-9_-]+)"
)
YOUTUBE_PRIVACY_OPTIONS = {
    "Riêng tư": "PRIVATE",
    "Không công khai": "UNLISTED",
    "Công khai": "PUBLIC",
}
_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_YOUTUBE_THUMBNAIL_SAFE_BYTES = 1_900_000
_YOUTUBE_RECOVERY_DELAYS = (5.0, 15.0, 30.0)
_POST_RENDER_UPLOAD_SETTLE_SECONDS = 5 * 60
_UPLOAD_FILE_STABLE_PROBE_SECONDS = 1.0
_UPLOAD_FILE_STABLE_PROBES = 3
# Renderer writes hidden ``.<stem>.part.<ext>`` files and atomically renames
# them only after ffmpeg, hashing, and the final integrity checks complete.
# Uploading one of those files races the renderer: the path can disappear
# while the uploader is hashing it, or YouTube can receive an incomplete
# container.  Keep this rule local to upload source resolution so every
# manual, scheduled, and recovered upload gets the same safety gate.
_STAGED_VIDEO_MARKER = ".part"
# Donut's local browser/session limit is shared by every workspace. Keep all
# Studio uploads and visibility edits in one global lane to avoid competing
# profile launches and ambiguous transfers.
YOUTUBE_UPLOAD_MAX_PARALLEL = 1
# The normal schedule slots are five hours apart.  Keep a 30-minute recovery
# margin so a genuinely slow-but-progressing transfer cannot occupy the upload
# lane into the next slot.  This is only a hard ceiling; a completed transfer
# returns immediately, and the no-progress watchdog exits much earlier.
_YOUTUBE_TRANSFER_HARD_TIMEOUT_SECONDS = 4 * 60 * 60 + 30 * 60
# Ten minutes is the normal handoff grace period after the one irreversible
# upload submit. Longer source videos get 15 minutes (and at most 20 minutes)
# before the same Content-list handoff is attempted. If Studio has exposed the
# exact video in Content by then, the old upload wizard is no longer needed: a
# fresh, ID-scoped edit target can take over without attaching the file again.
# This is deliberately separate from the transfer hard timeout; a slow
# transfer remains recoverable in its original target when the draft cannot yet
# be proven.
_YOUTUBE_DRAFT_HANDOFF_AFTER_SECONDS = 10 * 60
_YOUTUBE_LONG_DRAFT_HANDOFF_AFTER_SECONDS = 15 * 60
_YOUTUBE_MAX_DRAFT_HANDOFF_AFTER_SECONDS = 20 * 60
# A fresh Content target is cheap compared with repeating an upload. Recheck
# it on a 3–4 minute cadence while the original upload target remains owned.
_YOUTUBE_CONTENT_PROBE_INTERVAL_SECONDS = 3.5 * 60
_YOUTUBE_RECOVERY_STALL_SECONDS = 15 * 60
_YOUTUBE_MAX_STALLED_RECOVERY_PROBES = 3


class YouTubeUploadError(RuntimeError):
    pass


class YouTubeUploadCancelled(YouTubeUploadError):
    pass


def _check_publication_deadline(deadline: datetime | None) -> None:
    if deadline is not None and datetime.now(timezone.utc) >= deadline:
        raise YouTubeUploadCancelled(
            "Ca Public đã hết cửa sổ 2 tiếng; giữ đúng video/checkpoint cho ca kế tiếp."
        )


class YouTubeUploadWaitingForPublic(YouTubeUploadError):
    """A fresh automatic upload must wait for its predecessor to be Public."""


def has_pending_prior_public(workspace: Workspace) -> bool:
    """Read the predecessor barrier without quarantining or changing data."""
    path = workspace.state_dir / "deferred_public_queue.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise YouTubeUploadWaitingForPublic(
            "Chưa đọc được hàng đợi Public; giữ video/checkpoint, chưa upload tiếp."
        ) from exc
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise YouTubeUploadWaitingForPublic(
            "Hàng đợi Public chưa hợp lệ; giữ video/checkpoint, chưa upload tiếp."
        )
    # Lazy import because session_archive uses this module's package parser.
    from .session_archive import is_deferred_public_item_structurally_valid
    return any(is_deferred_public_item_structurally_valid(item)
               for item in document["items"] if isinstance(item, dict))


class YouTubeUploadUncertain(YouTubeUploadError):
    """The one allowed final submit may have succeeded; it must not be repeated."""

    pass


class YouTubeUploadRecoveryPaused(YouTubeUploadUncertain):
    """No uniquely identifiable draft remains; automatic reopening is unsafe."""


class YouTubeUploadTransferStalled(YouTubeUploadUncertain):
    """Repeated exact-ID server counters prove a transfer stopped moving."""


def _exact_server_upload_feedback(feedback: dict[str, Any], video_id: str) -> tuple[float | None, str]:
    """Read only this ID's transfer counter or explicit upload SUCCESS.

    The backend replaces transferProgressBar with uploadStatus when transfer
    finishes. Copyright checks and processing percentages are independent.
    Any remaining partial transfer counter overrides a stale SUCCESS marker.
    """
    progress_values: list[tuple[float, str]] = []
    success = False
    for item in feedback.get("continuationContents", []):
        if not isinstance(item, dict):
            continue
        upload = item.get("uploadFeedbackItemContinuation", {})
        if not isinstance(upload, dict) or upload.get("id", {}).get("videoId") != video_id:
            continue
        for content in upload.get("contents", []):
            if not isinstance(content, dict):
                continue
            progress = content.get("transferProgressBar")
            if isinstance(progress, dict):
                try:
                    fraction = float(progress.get("fractionCompleted"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(fraction) and 0 <= fraction <= 1:
                    message = progress.get("remainingTimeMessage") or {}
                    progress_values.append((fraction * 100.0, str(message.get("simpleText") or "")))
            status = content.get("uploadStatus")
            if isinstance(status, dict) and status.get("uploadStatus") == "SUCCESS":
                endpoint = status.get("watchEndpoint", {}).get("watchEndpoint", {})
                if not endpoint.get("videoId") or endpoint.get("videoId") == video_id:
                    success = True
    if progress_values:
        return min(progress_values, key=lambda value: value[0])
    return (100.0, "") if success else (None, "")


def _is_transient_youtube_pre_attach_error(error: BaseException) -> bool:
    """Return True only for failures that happened before the file boundary.

    The caller additionally proves that no attempt id was allocated. At that
    point closing the owned target and opening a fresh one cannot duplicate a
    YouTube draft, so local MCP/CDP and proxy navigation failures may be
    retried safely.
    """

    if isinstance(
        error,
        (YouTubeUploadUncertain, DonutMcpEntitlementError, DonutMcpAuthenticationError),
    ):
        return False
    if isinstance(
        error,
        (DonutMcpError, BrowserExtensionBridgeError, OSError, TimeoutError),
    ):
        return True
    if not isinstance(error, YouTubeUploadError):
        return False
    detail = str(error).casefold()
    return any(
        marker in detail
        for marker in (
            "err_proxy_connection_failed",
            "err_tunnel_connection_failed",
            "err_connection",
            "err_timed_out",
            "err_name_not_resolved",
            "err_internet_disconnected",
            "page.navigate",
            "cdp hết thời gian",
            "không tải được youtube studio",
            "youtube studio tải trang quá thời hạn",
        )
    )


@dataclass(frozen=True)
class YouTubeMetadata:
    title: str
    description: str
    hashtags: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceUploadSources:
    video_path: Path
    package_path: Path
    thumbnail_path: Path | None
    metadata: YouTubeMetadata


class YouTubeUploadAssetError(YouTubeUploadError):
    def __init__(self,message: str,sources: WorkspaceUploadSources):
        super().__init__(message)
        self.sources=sources


def _assert_complete_upload_assets(sources: WorkspaceUploadSources) -> None:
    """Reject incomplete new uploads before acquiring/opening any browser."""
    if (not sources.metadata.title.strip() or not sources.metadata.description.strip()
            or not any(str(tag).strip() for tag in sources.metadata.tags)):
        raise YouTubeUploadAssetError(
            "Thiếu tiêu đề, mô tả hoặc SEO tags; giữ video, chưa upload YouTube.", sources
        )
    thumbnail = sources.thumbnail_path
    from .content_ai import content_package_problems
    if problems:=content_package_problems(sources.package_path):
        raise YouTubeUploadAssetError('CONTENT_PACKAGE_INCONSISTENT: '+ '; '.join(problems.values())+'; giữ video, chưa upload.',sources)
    try:
        if thumbnail is None:
            raise ValueError("missing thumbnail")
        with Image.open(thumbnail) as image:
            if image.width < 320 or image.height < 180:
                raise ValueError("thumbnail too small")
            image.verify()
    except (OSError, ValueError):
        raise YouTubeUploadAssetError(
            "Thiếu thumbnail hợp lệ; phải tạo và xác minh ảnh trước khi upload YouTube.", sources
        ) from None


@dataclass(frozen=True)
class _YouTubeDraftCandidate:
    video_id: str
    title: str
    row_text: str
    position: int


@dataclass(frozen=True)
class YouTubeUploadRequest:
    workspace: Workspace
    endpoint: DonutMcpEndpoint
    profile_id: str
    privacy: str = "PRIVATE"
    contains_synthetic_media: bool = True
    video_path: Path | None = None
    package_path: Path | None = None
    thumbnail_path: Path | None = None
    # Only a direct user Upload/Tiếp tục action may retry a profile launch
    # previously refused for fingerprint entitlement. Donut checks the plan.
    manual_profile_recheck: bool = False
    # A scheduled PUBLIC upload that reaches the end-of-day cutoff is repaired
    # as UNLISTED first.  The same identity/checkpoint is promoted to PUBLIC
    # on the next publication slot; it must never attach the file again.
    hold_public_until_next_morning: bool = False
    # Recheck this after the settle delay so crossing 15:00 cannot publish late.
    automatic_public_cutoff_at: datetime | None = None
    wait_for_prior_public: bool = False


@dataclass(frozen=True)
class YouTubeUploadResult:
    profile_id: str
    profile_name: str
    channel_name: str
    video_path: Path
    package_path: Path
    youtube_url: str = ""
    thumbnail_path: Path | None = None
    deferred_public: bool = False
    privacy_status: str = ""


@dataclass(frozen=True)
class YouTubeVisibilityPromotionResult:
    """Result of promoting an already-uploaded exact video to PUBLIC."""

    profile_id: str
    profile_name: str
    channel_name: str
    youtube_url: str


ProgressCallback = Callable[[float, str], None]
_YOUTUBE_REGISTRY_LOCK = threading.RLock()
# Keep the short authoritative edit phase protected across local app processes
# too. The in-process coordinator serializes full uploads, while this file lock
# also covers an accidentally opened second DocuRandom process.
_YOUTUBE_METADATA_UI_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _confirmed_privacy_from_record(record: dict[str, object]) -> str:
    """Return a completed attempt's saved visibility, or empty if unavailable."""

    privacy = str(record.get("confirmed_privacy") or "").strip().upper()
    if privacy in YOUTUBE_PRIVACY_OPTIONS.values():
        return privacy
    status = str(record.get("status") or "").strip().casefold()
    if status in {"complete", "deferred_public"}:
        privacy = str(record.get("privacy") or "").strip().upper()
        if privacy in YOUTUBE_PRIVACY_OPTIONS.values():
            return privacy
    return ""


@contextmanager
def _youtube_metadata_ui_guard(
    cancel: threading.Event,
    report: ProgressCallback,
):
    """Serialize the final Studio edit phase across local app processes."""

    with _YOUTUBE_METADATA_UI_LOCK:
        lock_path = app_data_dir() / "youtube-metadata-ui.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as stream:
            if os.name != "nt":
                yield
                return
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            next_report = 0.0
            while True:
                YouTubeStudioUploader._cancelled(cancel)
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    now = time.monotonic()
                    if now >= next_report:
                        report(97.7, "Đang chờ lượt ghi metadata của profile khác…")
                        next_report = now + 15.0
                    cancel.wait(0.25)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _youtube_video_id(url: str) -> str:
    """Return the canonical video id from a Studio confirmation URL."""

    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold()
    candidate = ""
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        candidate = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{6,32}", candidate) else ""


def _youtube_json_values(html: str, key: str, *, limit: int = 32) -> list[object]:
    """Decode repeated JSON fields from YouTube's watch bootstrap safely."""

    marker = json.dumps(str(key), ensure_ascii=False) + ":"
    decoder = json.JSONDecoder()
    values: list[object] = []
    cursor = 0
    while len(values) < max(1, int(limit)):
        index = html.find(marker, cursor)
        if index < 0:
            break
        start = index + len(marker)
        try:
            value, consumed = decoder.raw_decode(html[start:])
        except json.JSONDecodeError:
            cursor = start
            continue
        values.append(value)
        cursor = start + max(1, consumed)
    return values


def _youtube_server_submission_matches(
    url: str,
    metadata: "YouTubeMetadata",
    privacy: str,
    *,
    timeout: float = 15.0,
) -> bool:
    """Verify a public/unlisted submission from YouTube's own watch data.

    Studio occasionally leaves its upload modal open and disabled after the
    backend has already committed the video. The modal is therefore not an
    authoritative completion signal. This read-only probe verifies the exact
    title, description, tags and visibility before a checkpoint is completed.
    Private videos intentionally fall back to the signed-in Studio check.
    """

    normalized_privacy = str(privacy or "").strip().upper()
    if normalized_privacy not in {"UNLISTED", "PUBLIC"}:
        return False
    video_id = _youtube_video_id(url)
    if not video_id:
        return False
    request = urllib.request.Request(
        f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}",
        headers={
            "Accept-Language": "en-US,en;q=0.8",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=max(3.0, float(timeout))) as response:
            html = response.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
    except (OSError, TimeoutError, urllib.error.URLError):
        return False

    titles = [value for value in _youtube_json_values(html, "title") if isinstance(value, str)]
    descriptions = [
        value
        for value in _youtube_json_values(html, "shortDescription")
        if isinstance(value, str)
    ]
    keyword_sets = [
        tuple(str(item) for item in value)
        for value in _youtube_json_values(html, "keywords")
        if isinstance(value, list) and all(isinstance(item, str) for item in value)
    ]
    unlisted_values = [
        value for value in _youtube_json_values(html, "isUnlisted") if isinstance(value, bool)
    ]
    expected_unlisted = normalized_privacy == "UNLISTED"
    return bool(
        metadata.title in titles
        and metadata.description in descriptions
        and tuple(metadata.tags) in keyword_sets
        and expected_unlisted in unlisted_values
    )


def _normalized_heading(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip()).casefold()
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.replace("đ", "d")
    return re.sub(r"[^a-z0-9]+", "", normalized)


_PACKAGE_HEADINGS = {
    "tieude": "title",
    "title": "title",
    "mota": "description",
    "description": "description",
    "hashtagseo": "hashtags",
    "hashtagsseo": "hashtags",
    "hastagseo": "hashtags",
    "hastagsseo": "hashtags",
    "hashtag": "hashtags",
    "tukhoaseo": "tags",
    "seotag": "tags",
    "seotags": "tags",
    "tagsseo": "tags",
    "tags": "tags",
    "promptthumbnail": "thumbnail",
    "promtthumbnail": "thumbnail",
    "thoaivoicevox": "voice",
    "thoaivoicevoxlienmach": "voice",
    "prompthinhanh": "images",
    "promthinhanh": "images",
}

# These are useful sections in the human-readable package, but they are not
# upload metadata.  In particular, the title is followed by the dedup
# checklist in exported files.  Treating that heading as an unknown line made
# every checklist item part of the YouTube title and produced false
# "title exceeds 100 characters" errors.
_PACKAGE_IGNORED_HEADINGS = {
    "checklistnoidungdalam",
    "checklistnoidungdalamchongtrung",
}


def _read_content_package_sections(path: Path) -> dict[str, list[str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise YouTubeUploadError(f"Không đọc được file Tổng hợp: {exc}") from exc
    sections: dict[str, list[str]] = {}
    current = ""
    for raw_line in text.splitlines():
        normalized = _normalized_heading(raw_line)
        if normalized in _PACKAGE_IGNORED_HEADINGS:
            current = ""
            continue
        # Japanese exports can put the real keywords under this heading and
        # leave a later English SEO TAGS block empty. Match exact headings,
        # never a generic 'SEO' word inside prose or a thumbnail prompt.
        japanese_heading = re.sub(r"[\s#*_:：■●◆【】\[\]]+", "", unicodedata.normalize("NFKC", raw_line)).casefold()
        heading = ({"seoタグ": "tags", "seoキーワード": "tags"}.get(japanese_heading)
                   or _PACKAGE_HEADINGS.get(normalized))
        if heading:
            current = heading
            sections.setdefault(current, [])
            continue
        if current == "tags" and re.fullmatch(r"[\s─━═\-_*]+", raw_line):
            continue
        if current:
            sections[current].append(raw_line)
    return sections


def parse_thumbnail_prompt(path: Path) -> str:
    sections = _read_content_package_sections(path)
    prompt = "\n".join(sections.get("thumbnail", [])).strip()
    if not prompt:
        raise YouTubeUploadError("File Tổng hợp chưa có phần Prompt Thumbnail.")
    return prompt


def parse_content_package(path: Path) -> YouTubeMetadata:
    sections = _read_content_package_sections(path)

    title = " ".join(line.strip() for line in sections.get("title", []) if line.strip())
    description = "\n".join(sections.get("description", [])).strip()
    hashtag_text = "\n".join(sections.get("hashtags", [])).strip()
    hashtags = tuple(dict.fromkeys(re.findall(r"#[^\s#,;]+", description + "\n" + hashtag_text)))
    if hashtag_text and hashtag_text not in description:
        description = (description + "\n\n" + hashtag_text).strip()
    tag_text = "\n".join(sections.get("tags", []))
    tags = tuple(
        dict.fromkeys(
            item.strip().lstrip("#")
            for item in re.split(r"[,;、，；\n]+|(?<!\S)#", tag_text)
            if item.strip().lstrip("#")
        )
    )
    from .automatic_materials import read_metadata_supplement
    supplement = read_metadata_supplement(path)
    title = title or supplement.get('title', '')
    description = description or supplement.get('description', '')
    tags = tags or tuple(supplement.get('seo_tags', ()))
    tags_value = ", ".join(tags)
    source_sidecar = path.with_suffix(".sources.json")
    if source_sidecar.is_file():
        from .broll_runtime import credit_text
        sources = json.loads(source_sidecar.read_text(encoding="utf-8"))
        if sources.get("package_sha256") == hashlib.sha256(path.read_bytes()).hexdigest():
            description += "\n\n" + credit_text(sources)
    if not title:
        raise YouTubeUploadError("File Tổng hợp chưa có phần Tiêu đề.")
    if not description:
        raise YouTubeUploadError("File Tổng hợp chưa có phần Mô tả.")
    if len(title) > 100:
        raise YouTubeUploadError(
            f"Tiêu đề có {len(title)} ký tự; YouTube chỉ cho tối đa 100. Hãy sửa file Tổng hợp."
        )
    if len(description) > 5000:
        raise YouTubeUploadError(
            f"Mô tả có {len(description)} ký tự; YouTube chỉ cho tối đa 5.000."
        )
    if len(tags_value) > 500:
        raise YouTubeUploadError(
            "Danh sách từ khóa SEO vượt giới hạn 500 ký tự của YouTube."
        )
    return YouTubeMetadata(title, description, hashtags, tags)


def _exact_package_path(workspace: Workspace) -> Path | None:
    checkpoint = read_pipeline_checkpoint(workspace.pipeline_checkpoint_path)
    stages = checkpoint.get("stages") if isinstance(checkpoint, dict) else None
    package = stages.get("package") if isinstance(stages, dict) else None
    artifacts = package.get("artifacts") if isinstance(package, dict) else None
    raw = str(artifacts.get("path") or "").strip().strip('"') if isinstance(artifacts, dict) else ""
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = workspace.output_dir / path
        path = path.resolve(strict=False)
        if path.is_file() and path.suffix.casefold() == ".txt":
            return path
    candidates = [
        path.resolve(strict=False)
        for path in workspace.output_dir.glob("*.txt")
        if path.is_file()
        and not path.name.casefold().endswith("_chat.txt")
        and "master-prompt" not in path.name.casefold()
    ]
    return max(candidates, key=lambda item: item.stat().st_mtime) if len(candidates) == 1 else None


def find_workspace_content_package(workspace: Workspace) -> Path | None:
    """Return the one package owned by a workspace without guessing across workspaces."""

    return _exact_package_path(workspace)


def _is_staged_video_path(path: Path) -> bool:
    """Return whether *path* is a renderer staging file, hidden or otherwise."""

    return (
        path.stem.casefold().endswith(_STAGED_VIDEO_MARKER)
        and path.suffix.casefold() in _VIDEO_EXTENSIONS
    )


def _render_checkpoint_status(workspace: Workspace) -> str:
    """Return the persisted render status, or ``""`` for legacy/manual data."""

    checkpoint = read_pipeline_checkpoint(workspace.pipeline_checkpoint_path)
    stages = checkpoint.get("stages") if isinstance(checkpoint, dict) else None
    render = stages.get("render") if isinstance(stages, dict) else None
    if not isinstance(render, dict):
        return ""
    return str(render.get("status") or "").strip().casefold()


def _render_completion_timestamp(workspace: Workspace) -> float | None:
    """Return the persisted UTC completion time for this render, if available."""

    checkpoint = read_pipeline_checkpoint(workspace.pipeline_checkpoint_path)
    stages = checkpoint.get("stages") if isinstance(checkpoint, dict) else None
    render = stages.get("render") if isinstance(stages, dict) else None
    if not isinstance(render, dict) or str(render.get("status") or "").casefold() != "complete":
        return None
    raw = str(render.get("updated_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _upload_file_signature(path: Path) -> tuple[int, int]:
    try:
        if not path.is_file():
            raise OSError("file missing")
        stat = path.stat()
    except OSError as exc:
        raise YouTubeUploadError(
            "Không đọc được file video cuối để xác nhận trước khi upload."
        ) from exc
    if stat.st_size <= 0:
        raise YouTubeUploadError("File video cuối đang rỗng; chưa được phép upload.")
    return int(stat.st_size), int(stat.st_mtime_ns)


def _post_render_upload_not_before(workspace: Workspace, video_path: Path) -> float | None:
    """Compute the durable five-minute upload boundary for a completed render."""

    if _render_checkpoint_status(workspace) != "complete":
        return None
    try:
        if not video_path.is_file():
            raise OSError("file missing")
        stat = video_path.stat()
    except OSError as exc:
        raise YouTubeUploadError(
            "Không đọc được thời điểm ghi file video cuối trước khi upload."
        ) from exc
    if stat.st_size <= 0:
        raise YouTubeUploadError("File video cuối đang rỗng; chưa được phép upload.")
    file_mtime = stat.st_mtime
    completed_at = _render_completion_timestamp(workspace)
    anchor = max(file_mtime, completed_at or 0.0)
    return anchor + _POST_RENDER_UPLOAD_SETTLE_SECONDS


def _wait_for_render_upload_ready(
    workspace: Workspace,
    video_path: Path,
    cancel_event: threading.Event,
    report: Callable[[float, str], None],
) -> None:
    """Wait out the post-render settle period and prove the final file is stable."""

    if _is_staged_video_path(video_path):
        raise YouTubeUploadError(
            "Từ chối upload file .part: chỉ file video cuối sau khi render hoàn tất mới hợp lệ."
        )

    render_complete = _render_checkpoint_status(workspace) == "complete"
    not_before = _post_render_upload_not_before(workspace, video_path)
    signature = _upload_file_signature(video_path)
    quiet_until = not_before or time.time()
    if render_complete:
        remaining = max(0.0, quiet_until - time.time())
        report(
            0.5,
            "Render đã xong · đang chờ đủ 5 phút và xác nhận file cuối ổn định"
            + (f" · còn khoảng {int(remaining + 0.999)} giây" if remaining else ""),
        )

    last_progress_at = time.monotonic()
    while True:
        YouTubeStudioUploader._cancelled(cancel_event)
        current_signature = _upload_file_signature(video_path)
        now = time.time()
        if current_signature != signature:
            signature = current_signature
            # A file that changes after the renderer checkpoint is treated as
            # newly written, even if the filesystem reports coarse timestamps.
            quiet_until = max(
                quiet_until,
                now + _POST_RENDER_UPLOAD_SETTLE_SECONDS if render_complete else now,
            )
        remaining = max(0.0, quiet_until - now)
        if remaining > 0:
            monotonic_now = time.monotonic()
            if render_complete and monotonic_now - last_progress_at >= 30.0:
                report(
                    0.5,
                    "Render đã xong · chờ file cuối ổn định trước upload"
                    f" · còn khoảng {int(remaining + 0.999)} giây",
                )
                last_progress_at = monotonic_now
            if cancel_event.wait(min(10.0, remaining)):
                YouTubeStudioUploader._cancelled(cancel_event)
            continue

        stable = True
        for _ in range(_UPLOAD_FILE_STABLE_PROBES - 1):
            if cancel_event.wait(_UPLOAD_FILE_STABLE_PROBE_SECONDS):
                YouTubeStudioUploader._cancelled(cancel_event)
            current_signature = _upload_file_signature(video_path)
            if current_signature != signature:
                signature = current_signature
                now = time.time()
                quiet_until = max(
                    quiet_until,
                    now + _POST_RENDER_UPLOAD_SETTLE_SECONDS if render_complete else now,
                )
                stable = False
                break
        if stable:
            break

    if render_complete:
        report(0.5, "Đã chờ đủ 5 phút · file video cuối ổn định, cho phép upload.")


def _prior_uncertain_staged_video_matches(
    workspace: Workspace,
    prior: object,
    current_video_path: Path,
) -> bool:
    """Match an unresolved prior .part attempt to its atomically renamed final."""

    if not isinstance(prior, dict):
        return False
    status = str(prior.get("status") or "").casefold()
    if status not in {"attaching", "uploading", "submitting", "uncertain", "deferred_public"}:
        return False
    raw = str(prior.get("video_path") or "").strip().strip('"')
    if not raw:
        return False
    prior_path = Path(raw).expanduser()
    if not prior_path.is_absolute():
        prior_path = workspace.output_dir / prior_path
    prior_path = prior_path.resolve(strict=False)
    if not _is_staged_video_path(prior_path):
        return False
    final_path = _final_path_for_staged_video(prior_path)
    if final_path is None:
        final_path = _completed_render_video_path(workspace, prior_path)
    return (
        final_path is not None
        and final_path.resolve(strict=False) == current_video_path.resolve(strict=False)
    )


def _apply_upload_visibility_cutoff(
    request: YouTubeUploadRequest,
    *,
    current: datetime | None = None,
) -> YouTubeUploadRequest:
    """Keep an automatic upload UNLISTED if its settle wait crosses cutoff."""

    cutoff = request.automatic_public_cutoff_at
    if (
        cutoff is None
        or request.privacy != "PUBLIC"
        or request.hold_public_until_next_morning
    ):
        return request
    now = current or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return (
        replace(request, hold_public_until_next_morning=True)
        if now >= cutoff
        else request
    )


def _final_path_for_staged_video(path: Path) -> Path | None:
    """Map ``.<stem>.part.mp4`` to its atomic final sibling when it exists."""

    if not _is_staged_video_path(path):
        return None
    name = path.name
    if name.startswith("."):
        name = name[1:]
    marker = f"{_STAGED_VIDEO_MARKER}{path.suffix}"
    if not name.casefold().endswith(marker.casefold()):
        return None
    final_name = name[: -len(marker)] + path.suffix
    candidate = path.with_name(final_name).resolve(strict=False)
    return candidate if candidate.is_file() else None


def _completed_render_video_path(
    workspace: Workspace,
    staged_path: Path | None = None,
) -> Path | None:
    """Recover the final render after an old checkpoint named its ``.part`` file."""

    if _render_checkpoint_status(workspace) != "complete":
        return None

    checkpoint = read_pipeline_checkpoint(workspace.pipeline_checkpoint_path)
    stages = checkpoint.get("stages") if isinstance(checkpoint, dict) else None
    render = stages.get("render") if isinstance(stages, dict) else None
    artifacts = render.get("artifacts") if isinstance(render, dict) else None
    raw = str(artifacts.get("video") or "").strip().strip('"') if isinstance(artifacts, dict) else ""
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = workspace.output_dir / path
        path = path.resolve(strict=False)
        if _is_staged_video_path(path):
            path = _final_path_for_staged_video(path) or path
        if (
            path.is_file()
            and path.suffix.casefold() in _VIDEO_EXTENSIONS
            and not _is_staged_video_path(path)
        ):
            return path

    if staged_path is not None:
        return _final_path_for_staged_video(staged_path)

    outcome = inspect_workspace_pipeline(
        workspace.pipeline_checkpoint_path,
        workspace.history_path,
        workspace.output_dir,
    )
    candidates = tuple(
        path for path in outcome.video_paths
        if path.is_file()
        and path.suffix.casefold() in _VIDEO_EXTENSIONS
        and not _is_staged_video_path(path)
    )
    return candidates[0] if len(candidates) == 1 else None


def _exact_video_path(workspace: Workspace) -> Path | None:
    render_status = _render_checkpoint_status(workspace)
    checkpoint = read_pipeline_checkpoint(workspace.pipeline_checkpoint_path)
    stages = checkpoint.get("stages") if isinstance(checkpoint, dict) else None
    render = stages.get("render") if isinstance(stages, dict) else None
    artifacts = render.get("artifacts") if isinstance(render, dict) else None
    raw = str(artifacts.get("video") or "").strip().strip('"') if isinstance(artifacts, dict) else ""
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = workspace.output_dir / path
        path = path.resolve(strict=False)
        if _is_staged_video_path(path):
            path = _completed_render_video_path(workspace, path) or path
        if (
            path.is_file()
            and path.suffix.casefold() in _VIDEO_EXTENSIONS
            and not _is_staged_video_path(path)
            and (not render_status or render_status == "complete")
        ):
            return path
    # A checkpoint with an active/error render stage is not allowed to fall
    # through to an older final file in Output.  Otherwise the scheduler can
    # upload yesterday's video while the current render is still running.
    if render_status and render_status != "complete":
        return None
    outcome = inspect_workspace_pipeline(
        workspace.pipeline_checkpoint_path,
        workspace.history_path,
        workspace.output_dir,
    )
    outcome_videos = tuple(
        path for path in outcome.video_paths
        if path.is_file() and not _is_staged_video_path(path)
    )
    if len(outcome_videos) == 1:
        return outcome_videos[0]

    # A skipped/uploaded session can be restored by copying its Output and
    # package files back into the workspace without restoring the internal
    # checkpoint.  In that case the checkpoint inspector has no video to
    # report even though there is one unambiguous rendered file.  Recover only
    # an exact single video; never guess when multiple candidates exist.
    candidates = [
        path.resolve(strict=False)
        for path in workspace.output_dir.iterdir()
        if (
            path.is_file()
            and path.suffix.casefold() in _VIDEO_EXTENSIONS
            and not _is_staged_video_path(path)
            and not path.name.startswith(".")
        )
    ] if workspace.output_dir.is_dir() else []
    return candidates[0] if len(candidates) == 1 else None


def _thumbnail_path(workspace: Workspace, video: Path) -> Path | None:
    stems = (f"{video.stem}_thumbnail", "thumbnail", "Thumbnail")
    for directory in (workspace.output_dir, workspace.media_dir):
        for stem in stems:
            for extension in _IMAGE_EXTENSIONS:
                candidate = directory / f"{stem}{extension}"
                if candidate.is_file():
                    return candidate.resolve(strict=False)
    return None


def _youtube_thumbnail_fallback(workspace: Workspace, source: Path) -> Path:
    """Create a YouTube-compatible copy without modifying the selected image."""

    try:
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()[:20]
        with Image.open(io.BytesIO(payload)) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened).convert("RGB")
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise YouTubeUploadError(
            f"Không đọc được thumbnail đã chọn để tạo bản tương thích YouTube: {exc}"
        ) from exc

    # Preserve the full composition. Generated thumbnails are normally 16:9;
    # ``thumbnail`` only scales them down and never crops text at the edges.
    image.thumbnail((1280, 720), Image.Resampling.LANCZOS)
    output = b""
    for quality in (92, 88, 84, 80, 76, 70):
        stream = io.BytesIO()
        image.save(stream, format="JPEG", quality=quality, optimize=True)
        output = stream.getvalue()
        if len(output) <= _YOUTUBE_THUMBNAIL_SAFE_BYTES:
            break
    if not output or len(output) > _YOUTUBE_THUMBNAIL_SAFE_BYTES:
        raise YouTubeUploadError(
            "Không thể tạo bản thumbnail dưới giới hạn an toàn của YouTube."
        )

    directory = workspace.state_dir / "youtube-thumbnails"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"upload-{digest}.jpg"
    if target.is_file() and target.stat().st_size == len(output):
        return target.resolve(strict=False)
    temporary = directory / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_bytes(output)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    logger.info(
        "youtube_thumbnail_fallback source=%s source_bytes=%d target=%s target_bytes=%d",
        source.name,
        len(payload),
        target.name,
        len(output),
    )
    return target.resolve(strict=False)


def resolve_workspace_upload_sources(
    workspace: Workspace,
    *,
    video_path: Path | None = None,
    package_path: Path | None = None,
    thumbnail_path: Path | None = None,
) -> WorkspaceUploadSources:
    video = video_path.expanduser().resolve(strict=False) if video_path is not None else _exact_video_path(workspace)
    if video is None:
        raise YouTubeUploadError(
            "Không xác định được duy nhất video của workspace. Hãy hoàn tất render hoặc mở workspace kiểm tra."
        )
    if _is_staged_video_path(video):
        recovered = _completed_render_video_path(workspace, video)
        if recovered is None:
            raise YouTubeUploadError(
                "Render video chưa hoàn tất; file .part chỉ là file tạm và chưa được phép upload."
            )
        video = recovered
    render_status = _render_checkpoint_status(workspace)
    if render_status and render_status != "complete":
        raise YouTubeUploadError(
            f"Render video chưa hoàn tất (trạng thái: {render_status}); chưa được phép upload."
        )
    if (
        not video.is_file()
        or video.suffix.casefold() not in _VIDEO_EXTENSIONS
        or _is_staged_video_path(video)
    ):
        raise YouTubeUploadError("File video của tác vụ không còn tồn tại hoặc sai định dạng.")
    try:
        video.relative_to(workspace.output_dir.resolve(strict=False))
    except ValueError as exc:
        raise YouTubeUploadError("Từ chối upload video nằm ngoài thư mục Output của workspace.") from exc
    package = (
        package_path.expanduser().resolve(strict=False)
        if package_path is not None
        else _exact_package_path(workspace)
    )
    if package is None:
        raise YouTubeUploadError(
            "Không xác định được duy nhất file Tổng hợp của workspace. Hãy chạy/xuất lại nội dung AI."
        )
    if not package.is_file() or package.suffix.casefold() != ".txt":
        raise YouTubeUploadError("File Tổng hợp của tác vụ không còn tồn tại.")
    checkpoint = read_pipeline_checkpoint(workspace.pipeline_checkpoint_path)
    voice = checkpoint.get("stages", {}).get("voicevox", {})
    artifacts = voice.get("artifacts", {}) if isinstance(voice, dict) else {}
    if isinstance(artifacts, dict):
        project = str(artifacts.get("audited_project") or "").strip()
        digest = str(artifacts.get("script_sha256") or "").strip()
        try:
            if project:
                validate_project_narration(package, Path(project))
            if digest and digest != narration_digest(package_narration(package)):
                raise VoicevoxError("Kịch bản giọng đọc của checkpoint thuộc video khác.")
        except VoicevoxError as exc:
            raise YouTubeUploadError(str(exc)) from exc
    try:
        package.relative_to(workspace.output_dir.resolve(strict=False))
    except ValueError as exc:
        raise YouTubeUploadError("Từ chối dùng file Tổng hợp ngoài workspace.") from exc
    thumbnail = (
        thumbnail_path.expanduser().resolve(strict=False)
        if thumbnail_path is not None
        else _thumbnail_path(workspace, video)
    )
    if thumbnail is not None and (
        not thumbnail.is_file() or thumbnail.suffix.casefold() not in _IMAGE_EXTENSIONS
    ):
        raise YouTubeUploadError(
            "Thumbnail đã chọn không còn tồn tại hoặc không phải PNG/JPG/JPEG/WEBP."
        )
    from .thumbnail_contract import accepted
    if thumbnail is not None and not accepted(workspace, package, thumbnail):
        raise YouTubeUploadError("THUMBNAIL_CONTRACT_PENDING: ảnh chưa qua kiểm tra đúng Master; giữ video, chưa upload.")
    sources = WorkspaceUploadSources(
        video_path=video,
        package_path=package,
        thumbnail_path=thumbnail,
        metadata=parse_content_package(package),
    )
    logger.info(
        "youtube_sources_resolved workspace_id=%s video=%s package=%s thumbnail=%s",
        workspace.workspace_id,
        sources.video_path.name,
        sources.package_path.name,
        sources.thumbnail_path.name if sources.thumbnail_path is not None else "none",
    )
    return sources


class _ProfileLease(AbstractContextManager["_ProfileLease"]):
    def __init__(self, coordinator: "ProfileUploadCoordinator", scope_id: str, ticket: str) -> None:
        self.coordinator = coordinator
        self.scope_id = scope_id
        self.ticket = ticket
        self.released = False

    def __exit__(self, _type: object, _value: object, _tb: object) -> None:
        if not self.released:
            self.released = True
            self.coordinator.release(self.scope_id, self.ticket)


@dataclass(frozen=True)
class _ProfileTicket:
    ticket: str
    profile_id: str
    job_kind: str
    priority: int
    sequence: int
    cancel_event: threading.Event


class ProfileUploadCoordinator:
    """Serialize Donut Studio work across all workspaces.

    File transfers and visibility jobs share one global FIFO. A single
    confirmed-transfer recovery can finish metadata on another profile while
    bytes are still uploading. Monitors yield before an upload starts.
    """

    _GLOBAL_UPLOAD_SCOPE = "__docurandom_youtube_upload_global__"
    _GLOBAL_METADATA_SCOPE = "__docurandom_youtube_metadata_recovery__"

    def __init__(
        self,
        *,
        max_parallel_uploads: int = 1,
        max_parallel_monitors: int = 1,
        upload_start_interval: float = 0.0,
        capacity_provider: Callable[[], int] | None = None,
    ) -> None:
        self._condition = threading.Condition()
        self._queues: dict[str, list[_ProfileTicket]] = {}
        self._active: dict[str, _ProfileTicket] = {}
        self._active_uploads: dict[str, _ProfileTicket] = {}
        # Idle jobs can retain a bounded FIFO position without reserving a profile
        # or blocking uploads. A missed heartbeat expires the reservation.
        self._idle_waiters: dict[str, tuple[_ProfileTicket, float, float]] = {}
        self._sequence = 0
        # This is a hard global ceiling. Keep the parameter for compatibility
        # with existing callers, but never let host sizing or an old setting
        # raise the number of concurrent Studio operations.
        self._max_parallel_uploads = max(
            1, min(YOUTUBE_UPLOAD_MAX_PARALLEL, int(max_parallel_uploads))
        )
        # Optional read-only provider used by the all-in-one app to react when
        # Minecraft/Java starts or stops after the UI has already opened.
        # Standalone callers keep the fixed constructor behaviour.
        self._capacity_provider = capacity_provider
        self._max_parallel_monitors = max(1, min(1, int(max_parallel_monitors)))
        self._upload_start_interval = max(0.0, float(upload_start_interval))
        self._next_upload_at = 0.0

    def _current_parallel_uploads_locked(self) -> int:
        limit = self._max_parallel_uploads
        provider = self._capacity_provider
        if provider is not None:
            try:
                limit = int(provider())
            except (TypeError, ValueError, RuntimeError):
                limit = self._max_parallel_uploads
        return max(1, min(YOUTUBE_UPLOAD_MAX_PARALLEL, limit))

    def _new_ticket(
        self,
        *,
        profile_id: str,
        job_kind: str,
        priority: int,
        cancel_event: threading.Event,
    ) -> _ProfileTicket:
        self._sequence += 1
        return _ProfileTicket(
            ticket=uuid.uuid4().hex,
            profile_id=profile_id,
            job_kind=str(job_kind or "upload"),
            priority=int(priority),
            sequence=self._sequence,
            cancel_event=cancel_event,
        )

    @staticmethod
    def _sort_queue(queue: list[_ProfileTicket]) -> None:
        queue.sort(key=lambda item: (-item.priority, item.sequence))

    def is_busy(self, profile_id: str) -> bool:
        with self._condition:
            return bool(
                self._active_uploads
                or self._queues.get(self._GLOBAL_UPLOAD_SCOPE)
                or self._GLOBAL_METADATA_SCOPE in self._active
                or self._queues.get(self._GLOBAL_METADATA_SCOPE)
                or profile_id in self._active
                or self._queues.get(profile_id)
            )

    def try_acquire(
        self,
        profile_id: str,
        cancel_event: threading.Event,
        *,
        job_kind: str = "monitor",
        priority: int = 0,
        wait_key: str = "",
    ) -> _ProfileLease | None:
        """Acquire idle capacity; optional expiring FIFO prevents polling starvation."""

        with self._condition:
            clock = time.monotonic()
            self._idle_waiters = {
                key: entry for key, entry in self._idle_waiters.items()
                if entry[1] > clock and not entry[0].cancel_event.is_set()
            }
            if cancel_event.is_set():
                if wait_key:self._idle_waiters.pop(wait_key,None)
                raise YouTubeUploadCancelled("Đã hủy trước khi dùng profile Donut.")
            request = None
            if wait_key:
                prior = self._idle_waiters.get(wait_key)
                if prior and prior[0].profile_id == profile_id:
                    request = replace(prior[0], cancel_event=cancel_event)
                else:
                    request = self._new_ticket(profile_id=profile_id,job_kind=job_kind,
                                               priority=priority,cancel_event=cancel_event)
                first_seen=prior[2]if prior and prior[0].profile_id==profile_id else clock
                self._idle_waiters[wait_key] = (request, clock + 30.0, first_seen)
                if clock-first_seen>=120.0:
                    for active in self._active.values():
                        if active.job_kind=='monitor'and not active.cancel_event.is_set():
                            active.cancel_event.set()
                            logger.info('idle_work_requested_monitor_yield job_kind=%s wait_seconds=%d',job_kind,int(clock-first_seen))
            if (
                self._active_uploads
                or self._queues.get(self._GLOBAL_UPLOAD_SCOPE)
            ):
                return None
            if len(self._active) >= self._max_parallel_monitors:
                return None
            if profile_id in self._active or self._queues.get(profile_id):
                return None
            if self._idle_waiters:
                first = min(self._idle_waiters, key=lambda key:self._idle_waiters[key][0].sequence)
                if first != wait_key:return None
            if request is None:request = self._new_ticket(
                profile_id=profile_id,
                job_kind=job_kind,
                priority=priority,
                cancel_event=cancel_event,
            )
            self._queues[profile_id] = [request]
            self._active[profile_id] = request
            if wait_key:self._idle_waiters.pop(wait_key,None)
            return _ProfileLease(self, profile_id, request.ticket)

    def cancel_idle_waiter(self, wait_key: str) -> None:
        with self._condition:
            self._idle_waiters.pop(wait_key, None)
            self._condition.notify_all()

    def acquire(
        self,
        profile_id: str,
        cancel_event: threading.Event,
        progress: ProgressCallback | None = None,
        *,
        job_kind: str = "upload",
        priority: int = 100,
        preempt_lower: bool = True,
    ) -> _ProfileLease:
        with self._condition:
            request = self._new_ticket(
                profile_id=profile_id,
                job_kind=job_kind,
                priority=priority,
                cancel_event=cancel_event,
            )
            is_upload = request.job_kind == "upload"
            is_metadata = request.job_kind == "metadata-recovery"
            scope_id = (self._GLOBAL_UPLOAD_SCOPE if is_upload else
                        self._GLOBAL_METADATA_SCOPE if is_metadata else profile_id)
            queue = self._queues.setdefault(scope_id, [])
            queue.append(request)
            self._sort_queue(queue)
            active = self._active.get(scope_id)
            if preempt_lower:
                candidates = (
                    tuple(self._active.values()) if is_upload else
                    tuple(item for item in self._active.values() if item.profile_id == profile_id)
                    if is_metadata else (active,)
                )
                for active_request in candidates:
                    if (
                        active_request is not None
                        and active_request.job_kind != "metadata-recovery"
                        and active_request.priority < request.priority
                    ):
                        active_request.cancel_event.set()
            while True:
                if cancel_event.is_set():
                    if request in queue:
                        queue.remove(request)
                    if not queue:
                        self._queues.pop(scope_id, None)
                    self._condition.notify_all()
                    raise YouTubeUploadCancelled("Đã hủy khi đang chờ lượt YouTube.")
                position = queue.index(request) + 1
                lower_priority_active = bool(is_upload and self._active)
                cooldown_remaining = (
                    max(0.0, self._next_upload_at - time.monotonic())
                    if is_upload
                    else 0.0
                )
                if is_upload:
                    eligible = next(
                        (
                            item
                            for item in queue
                            if item.profile_id not in self._active_uploads
                            and item.profile_id not in self._active
                        ),
                        None,
                    )
                    if (
                        eligible is request
                        and len(self._active_uploads)
                        < self._current_parallel_uploads_locked()
                        and not lower_priority_active
                        and cooldown_remaining <= 0.0
                    ):
                        self._active_uploads[profile_id] = request
                        self._next_upload_at = (
                            time.monotonic() + self._upload_start_interval
                        )
                        return _ProfileLease(self, scope_id, request.ticket)
                elif (position == 1 and scope_id not in self._active
                      and (not is_metadata or (
                          profile_id not in self._active_uploads
                          and not any(item.profile_id == profile_id for item in self._active.values())
                      ))):
                    self._active[scope_id] = request
                    return _ProfileLease(self, scope_id, request.ticket)
                if progress:
                    if is_upload:
                        if position == 1 and cooldown_remaining > 0.0:
                            progress(
                                1.0,
                                "YouTube/Donut đang giãn thời điểm bắt đầu các video · "
                                f"còn {max(1, int(cooldown_remaining + 0.999))} giây",
                            )
                        else:
                            progress(
                                1.0,
                                f"Đang chờ lượt upload YouTube toàn tool · vị trí {position}",
                            )
                    else:
                        progress(1.0, f"Đang chờ profile Donut · vị trí {position}")
                wait_for = (
                    min(0.5, cooldown_remaining)
                    if cooldown_remaining > 0.0
                    else 0.5
                )
                self._condition.wait(timeout=max(0.01, wait_for))

    def release(self, scope_id: str, ticket: str) -> None:
        with self._condition:
            queue = self._queues.get(scope_id, [])
            queue[:] = [item for item in queue if item.ticket != ticket]
            if not queue:
                self._queues.pop(scope_id, None)
            active = self._active.get(scope_id)
            if active is not None and active.ticket == ticket:
                self._active.pop(scope_id, None)
            if scope_id == self._GLOBAL_UPLOAD_SCOPE:
                for profile_id, upload in tuple(self._active_uploads.items()):
                    if upload.ticket == ticket:
                        self._active_uploads.pop(profile_id, None)
                        break
            self._condition.notify_all()


class _CdpSession:
    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self._ws = None
        self._next_id = 1

    def __enter__(self) -> "_CdpSession":
        # The CDP socket is always loopback. Explicitly disable system proxies
        # so a user's Donut/Windows proxy configuration cannot intercept this
        # local control channel and the portable build needs no SOCKS package.
        try:
            parsed = urllib.parse.urlsplit(self.ws_url)
        except ValueError as exc:
            raise YouTubeUploadError("Donut trả địa chỉ WebSocket CDP không hợp lệ.") from exc
        if parsed.scheme.casefold() not in {"ws", "wss"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise YouTubeUploadError("Từ chối kết nối WebSocket CDP không nằm trên máy này.")
        self._ws = websocket_connect(
            self.ws_url,
            proxy=None,
            open_timeout=10,
            close_timeout=3,
            # YouTube Studio's flattened DOM can exceed any fixed practical
            # threshold. This socket is restricted to loopback above, so keep
            # the protocol from closing a valid local CDP response with 1009.
            max_size=None,
            # A large Studio accessibility/DOM response can keep Wayfern's
            # renderer busy beyond the library's default ping deadline. CDP
            # commands already have their own strict timeouts and this socket
            # is loopback-only, so protocol keepalive must not close a healthy
            # session in the middle of metadata verification.
            ping_interval=None,
        )
        return self

    def __exit__(self, _type: object, _value: object, _tb: object) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def command(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        if self._ws is None:
            raise YouTubeUploadError("Kết nối điều khiển Donut chưa mở.")
        request_id = self._next_id
        self._next_id += 1
        self._ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        # Donut's local CDP relay can take longer than an ordinary Chromium
        # page socket when a Wayfern profile is still bootstrapping or a
        # beforeunload dialog is being materialized.  Navigation is the one
        # command that is allowed this longer, bounded window; the regular
        # 20-second bound keeps DOM controls responsive and fail-closed.
        command_timeout = (
            max(1.0, float(timeout))
            if timeout is not None
            else (60.0 if method == "Page.navigate" else 20.0)
        )
        deadline = time.monotonic() + command_timeout
        while time.monotonic() < deadline:
            try:
                message = self._ws.recv(timeout=max(0.1, deadline - time.monotonic()))
            except TimeoutError as exc:
                raise YouTubeUploadError(f"Donut CDP hết thời gian ở lệnh {method}.") from exc
            try:
                value = json.loads(message)
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                value.get("method") == "Page.javascriptDialogOpening"
                and (value.get("params") or {}).get("type") == "beforeunload"
            ):
                # A Close button or stale status must never authorize leaving
                # an upload. Cancel this navigation immediately, keeping the
                # transfer and unsaved metadata alive in the owned target.
                # Do not recurse through command(): the nested receive loop can
                # consume the response that belongs to this read-only request,
                # leaving the outer call waiting forever. Send a fire-and-
                # continue cancellation instead and let this request receive
                # its own response. Only an actual Page.navigate attempt is an
                # uncertain workflow boundary.
                dialog_request_id = self._next_id
                self._next_id += 1
                self._ws.send(
                    json.dumps(
                        {
                            "id": dialog_request_id,
                            "method": "Page.handleJavaScriptDialog",
                            "params": {"accept": False},
                        }
                    )
                )
                if method == "Page.navigate":
                    raise YouTubeUploadUncertain(
                        "YouTube còn dữ liệu chưa lưu/truyền xong. Tool đã hủy rời trang "
                        "và giữ nguyên tab; không upload lại tệp."
                    )
                logger.info(
                    "youtube_cancelled_incidental_beforeunload while_command=%s",
                    method,
                )
                continue
            if value.get("id") != request_id:
                continue
            if value.get("error"):
                error = value["error"]
                message = (
                    str(error.get("message") or "")
                    if isinstance(error, dict)
                    else str(error)
                )
                if "requires a paid donut browser plan" in message.casefold():
                    raise YouTubeUploadError(
                        "Donut Browser đã từ chối điều khiển tự động vì tài khoản hiện tại "
                        "không có quyền browser automation. Không có file nào được gửi lên "
                        "YouTube; checkpoint đã được giữ để tiếp tục sau khi quyền hợp lệ."
                    )
                raise YouTubeUploadError(f"Donut CDP lỗi {method}: {error}")
            result = value.get("result")
            return result if isinstance(result, dict) else {}
        raise YouTubeUploadError(f"Donut CDP không trả lời lệnh {method}.")

    def upload_feedback(self, video_id: str, *, timeout: float = 5.0) -> tuple[float | None, str]:
        """Read YouTube's own feedback for this exact allocated video only."""
        if self._ws is None or not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
            return None, ""
        pending: set[str] = set()
        try:
            self.command("Network.enable")
            deadline = time.monotonic() + max(0.1, timeout)
            while time.monotonic() < deadline:
                try:
                    event = json.loads(self._ws.recv(timeout=max(0.1, deadline - time.monotonic())))
                except TimeoutError:
                    break
                if not isinstance(event, dict):
                    continue
                params = event.get("params") or {}
                if event.get("method") == "Network.responseReceived":
                    response = params.get("response") or {}
                    url = urllib.parse.urlsplit(str(response.get("url") or ""))
                    if (
                        url.scheme == "https" and url.hostname == "studio.youtube.com"
                        and url.path == "/youtubei/v1/upload/feedback"
                        and response.get("status") == 200 and params.get("requestId")
                    ):
                        pending.add(str(params["requestId"]))
                elif event.get("method") == "Network.loadingFinished" and str(params.get("requestId") or "") in pending:
                    body = self.command("Network.getResponseBody", {"requestId": params["requestId"]})
                    if body.get("base64Encoded"):
                        continue
                    raw = str(body.get("body") or "")
                    if len(raw) > 512 * 1024:
                        continue
                    feedback = json.loads(raw)
                    percent, remaining = _exact_server_upload_feedback(feedback, video_id)
                    if percent is not None:
                        return percent, remaining
        except (OSError, ValueError, TypeError, AttributeError, YouTubeUploadError):
            logger.debug("youtube_server_transfer_feedback_unavailable video_id=%s", video_id)
        finally:
            try:
                self.command("Network.disable")
            except YouTubeUploadError:
                pass
        return None, ""

    def evaluate(self, expression: str, *, return_by_value: bool = True) -> object:
        result = self.command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": return_by_value,
                "userGesture": True,
            },
        )
        exception = result.get("exceptionDetails")
        if isinstance(exception, dict):
            description = str(exception.get("text") or "JavaScript exception")
            raise YouTubeUploadError(f"Lỗi điều khiển trang YouTube: {description}")
        remote = result.get("result")
        if not isinstance(remote, dict):
            return None
        if return_by_value:
            return remote.get("value")
        return remote


def _profile_candidate_ports(process_id: int) -> tuple[int, ...]:
    script = (
        f"$all=Get-CimInstance Win32_Process; $ids=@({int(process_id)}); "
        "for($i=0;$i -lt 8;$i++){"
        "$children=@($all|Where-Object{$ids -contains $_.ParentProcessId}|Select-Object -Expand ProcessId);"
        "if(-not $children){break}; $ids+=@($children)};"
        "$cmd=@($all|Where-Object{$ids -contains $_.ProcessId}|Select-Object -Expand CommandLine);"
        "$ports=@(); foreach($line in $cmd){if($line -match '--remote-debugging-port=(\\d+)'){$ports+=$Matches[1]}};"
        "if(-not $ports){$ports=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue|"
        "Where-Object{$ids -contains $_.OwningProcess -and $_.LocalAddress -in @('127.0.0.1','::1')}|"
        "Select-Object -Expand LocalPort)}; $ports|Sort-Object -Unique"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise YouTubeUploadError("Không đọc được cổng điều khiển profile Donut.") from exc
    ports: list[int] = []
    for token in re.findall(r"(?m)^\s*(\d+)\s*$", completed.stdout):
        value = int(token)
        if 1 <= value <= 65535:
            ports.append(value)
    return tuple(dict.fromkeys(ports))


def _cdp_page_ws_url(process_id: int, *, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    last_ports: tuple[int, ...] = ()
    while time.monotonic() < deadline:
        last_ports = _profile_candidate_ports(process_id)
        for port in last_ports:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=1.5
                ) as response:
                    targets = json.loads(response.read().decode("utf-8"))
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                continue
            if not isinstance(targets, list):
                continue
            pages = [item for item in targets if isinstance(item, dict) and item.get("type") == "page"]
            pages.sort(key=lambda item: "studio.youtube.com" not in str(item.get("url", "")))
            for page in pages:
                ws_url = str(page.get("webSocketDebuggerUrl") or "")
                if ws_url:
                    return ws_url
        time.sleep(0.5)
    raise YouTubeUploadError(
        "Profile Donut đã mở nhưng không tìm thấy cổng CDP để chọn file video. "
        f"Cổng đã dò: {', '.join(map(str, last_ports)) or 'không có'}."
    )


def _mcp_cdp_page_ws_urls(
    debug: dict[str, Any],
    *,
    prefer_url: str = "studio.youtube.com",
    timeout: float = 45.0,
    matching_only: bool = False,
) -> tuple[str, ...]:
    """Resolve all page sockets from Donut MCP's loopback debug contract."""

    targets_url = str(debug.get("targets_url") or "").strip()
    if not targets_url:
        browser_url = str(debug.get("browser_url") or "").strip().rstrip("/")
        targets_url = f"{browser_url}/json" if browser_url else ""
    try:
        parsed = urllib.parse.urlsplit(targets_url)
    except ValueError as exc:
        raise YouTubeUploadError("Donut trả địa chỉ CDP không hợp lệ.") from exc
    if parsed.scheme.casefold() != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise YouTubeUploadError("Từ chối kết nối CDP không nằm trên máy này.")

    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(targets_url, timeout=2.0) as response:
                targets = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.5)
            continue
        if isinstance(targets, list):
            pages = [
                item
                for item in targets
                if isinstance(item, dict) and item.get("type") == "page"
            ]
            if matching_only:
                pages = [
                    item
                    for item in pages
                    if prefer_url in str(item.get("url", "")).casefold()
                ]
            pages.sort(key=lambda item: prefer_url not in str(item.get("url", "")).casefold())
            sockets: list[str] = []
            for page in pages:
                ws_url = str(page.get("webSocketDebuggerUrl") or "").strip()
                try:
                    ws = urllib.parse.urlsplit(ws_url)
                except ValueError:
                    continue
                if ws.scheme.casefold() in {"ws", "wss"} and ws.hostname in {
                    "127.0.0.1",
                    "localhost",
                    "::1",
                }:
                    sockets.append(ws_url)
            if sockets:
                return tuple(dict.fromkeys(sockets))
        time.sleep(0.5)
    raise YouTubeUploadError("Donut MCP đã mở profile nhưng chưa trả tab CDP dùng được.")


def _mcp_open_cdp_page_ws_url(
    debug: dict[str, Any],
    url: str,
    *,
    timeout: float = 10.0,
) -> str:
    """Open one dedicated page in the exact Donut profile via loopback CDP."""

    browser_url = str(debug.get("browser_url") or "").strip().rstrip("/")
    if not browser_url:
        targets_url = str(debug.get("targets_url") or "").strip()
        try:
            parsed_targets = urllib.parse.urlsplit(targets_url)
        except ValueError as exc:
            raise YouTubeUploadError("Donut trả địa chỉ CDP không hợp lệ.") from exc
        browser_url = urllib.parse.urlunsplit(
            (parsed_targets.scheme, parsed_targets.netloc, "", "", "")
        ).rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(browser_url)
    except ValueError as exc:
        raise YouTubeUploadError("Donut trả địa chỉ CDP không hợp lệ.") from exc
    if parsed.scheme.casefold() != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise YouTubeUploadError("Từ chối mở tab CDP không nằm trên máy này.")
    endpoint = f"{browser_url}/json/new?{urllib.parse.quote(url, safe='')}"
    request = urllib.request.Request(endpoint, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, timeout)) as response:
            target = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise YouTubeUploadError(
            "Donut đang mở nhưng không tạo được tab YouTube Studio riêng."
        ) from exc
    ws_url = str(target.get("webSocketDebuggerUrl") or "") if isinstance(target, dict) else ""
    try:
        ws = urllib.parse.urlsplit(ws_url)
    except ValueError as exc:
        raise YouTubeUploadError("Tab mới của Donut không có kết nối CDP hợp lệ.") from exc
    if ws.scheme.casefold() not in {"ws", "wss"} or ws.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise YouTubeUploadError("Tab mới của Donut không có kết nối CDP local hợp lệ.")
    return ws_url


def _cdp_page_target_id(ws_url: str) -> str:
    """Return the exact page target carried by a loopback DevTools socket."""

    try:
        parsed = urllib.parse.urlsplit(str(ws_url or ""))
    except ValueError as exc:
        raise YouTubeUploadError("Kết nối tab Donut không có targetId hợp lệ.") from exc
    if parsed.scheme.casefold() not in {"ws", "wss"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise YouTubeUploadError("Từ chối targetId của tab không nằm trên máy này.")
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    target_id = parts[-1].strip() if len(parts) >= 3 else ""
    if not target_id or len(target_id) > 256 or any(
        character in target_id for character in "/\\?#"
    ):
        raise YouTubeUploadError("Kết nối tab Donut không có targetId hợp lệ.")
    return target_id


def _mcp_open_background_cdp_page_ws_url(
    client: DonutMcpClient,
    profile_id: str,
    debug: dict[str, Any],
    url: str,
    *,
    timeout: float = 10.0,
) -> str:
    """Create a background page and resolve only that page's CDP socket."""

    targets_url = str(debug.get("targets_url") or "").strip()
    try:
        parsed = urllib.parse.urlsplit(targets_url)
    except ValueError as exc:
        raise YouTubeUploadError("Donut trả danh sách tab CDP không hợp lệ.") from exc
    if parsed.scheme.casefold() != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise YouTubeUploadError("Từ chối danh sách tab CDP không nằm trên máy này.")
    created = client.cdp_command(
        profile_id,
        "Target.createTarget",
        {"url": url, "background": True},
    )
    target_id = str(created.get("targetId") or "").strip()
    if not target_id:
        raise YouTubeUploadError("Donut không tạo được targetId cho tab nền riêng.")
    deadline = time.monotonic() + max(1.0, timeout)
    try:
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(targets_url, timeout=2.0) as response:
                    targets = json.loads(response.read().decode("utf-8"))
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                time.sleep(0.2)
                continue
            if isinstance(targets, list):
                target = next(
                    (
                        item
                        for item in targets
                        if isinstance(item, dict)
                        and str(item.get("id") or "") == target_id
                        and item.get("type") == "page"
                    ),
                    None,
                )
                if target is not None:
                    ws_url = str(target.get("webSocketDebuggerUrl") or "")
                    if _cdp_page_target_id(ws_url) != target_id:
                        raise YouTubeUploadError(
                            "Tab nền Donut trả kết nối sai targetId."
                        )
                    return ws_url
            time.sleep(0.2)
    except Exception:
        try:
            client.cdp_command(
                profile_id,
                "Target.closeTarget",
                {"targetId": target_id},
            )
        except Exception:
            pass
        raise
    try:
        client.cdp_command(
            profile_id,
            "Target.closeTarget",
            {"targetId": target_id},
        )
    except Exception:
        pass
    raise YouTubeUploadError("Donut tạo tab nền nhưng chưa trả kết nối CDP kịp thời.")


def _mcp_open_minimized_cdp_window_ws_url(
    client: DonutMcpClient,
    profile_id: str,
    debug: dict[str, Any],
    url: str,
    *,
    timeout: float = 10.0,
) -> str:
    """Create an owned active tab inside a separate minimized browser window.

    Chromium drops some trusted Input-domain events for an inactive tab. A
    dedicated window gives the automation target its own active-tab slot. It
    starts minimized to avoid a startup flash, then the upload flow restores
    only that owned window before editing metadata; the user's existing tab is
    never activated or replaced.
    """

    targets_url = str(debug.get("targets_url") or "").strip()
    try:
        parsed = urllib.parse.urlsplit(targets_url)
    except ValueError as exc:
        raise YouTubeUploadError("Donut trả danh sách tab CDP không hợp lệ.") from exc
    if parsed.scheme.casefold() != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise YouTubeUploadError("Từ chối danh sách tab CDP không nằm trên máy này.")

    target_id = ""
    try:
        created = client.cdp_command(
            profile_id,
            "Target.createTarget",
            {
                "url": url,
                "background": True,
                "newWindow": True,
            },
        )
        target_id = str(created.get("targetId") or "").strip()
        if not target_id:
            raise YouTubeUploadError(
                "Donut không tạo được cửa sổ automation riêng."
            )

        window = client.cdp_command(
            profile_id,
            "Browser.getWindowForTarget",
            {"targetId": target_id},
        )
        window_id = window.get("windowId")
        if not isinstance(window_id, int):
            raise YouTubeUploadError(
                "Donut không xác định được cửa sổ automation riêng."
            )
        client.cdp_command(
            profile_id,
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {"windowState": "minimized"},
            },
        )

        deadline = time.monotonic() + max(1.0, timeout)
        minimized = False
        while time.monotonic() < deadline:
            bounds = client.cdp_command(
                profile_id,
                "Browser.getWindowBounds",
                {"windowId": window_id},
            )
            state = str(
                (bounds.get("bounds") or {}).get("windowState")
                if isinstance(bounds.get("bounds"), dict)
                else ""
            ).casefold()
            if state == "minimized":
                minimized = True
                break
            time.sleep(0.1)
        if not minimized:
            raise YouTubeUploadError(
                "Cửa sổ automation không thu nhỏ được; đã dừng để không chiếm tab của bạn."
            )

        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(targets_url, timeout=2.0) as response:
                    targets = json.loads(response.read().decode("utf-8"))
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                time.sleep(0.1)
                continue
            if isinstance(targets, list):
                target = next(
                    (
                        item
                        for item in targets
                        if isinstance(item, dict)
                        and str(item.get("id") or "") == target_id
                        and item.get("type") == "page"
                    ),
                    None,
                )
                if target is not None:
                    ws_url = str(target.get("webSocketDebuggerUrl") or "")
                    if _cdp_page_target_id(ws_url) != target_id:
                        raise YouTubeUploadError(
                            "Cửa sổ automation trả kết nối sai targetId."
                        )
                    return ws_url
            time.sleep(0.1)
        raise YouTubeUploadError(
            "Donut tạo cửa sổ automation nhưng chưa trả kết nối CDP kịp thời."
        )
    except Exception:
        if target_id:
            try:
                client.cdp_command(
                    profile_id,
                    "Target.closeTarget",
                    {"targetId": target_id},
                )
            except Exception:
                pass
        raise


def _mcp_close_cdp_page_target(
    client: DonutMcpClient,
    profile_id: str,
    debug: dict[str, Any],
    target_id: str,
    *,
    timeout: float = 5.0,
) -> bool:
    """Close one owned target and wait until it leaves the target catalog."""

    target = str(target_id or "").strip()
    if not target:
        return True
    targets_url = str(debug.get("targets_url") or "").strip()
    try:
        parsed = urllib.parse.urlsplit(targets_url)
    except ValueError:
        return False
    if parsed.scheme.casefold() != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        return False
    try:
        client.cdp_command(
            profile_id,
            "Target.closeTarget",
            {"targetId": target},
        )
    except Exception:
        # Some Wayfern builds report "target not found" after accepting the
        # asynchronous close. The catalog below is the source of truth.
        pass
    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(targets_url, timeout=2.0) as response:
                targets = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        if not isinstance(targets, list) or not any(
            isinstance(item, dict) and str(item.get("id") or "") == target
            for item in targets
        ):
            return True
        time.sleep(0.1)
    return False


def _mcp_restore_cdp_window(
    client: DonutMcpClient,
    profile_id: str,
    target_id: str,
    *,
    timeout: float = 8.0,
) -> None:
    """Restore an owned window without activating another user tab.

    YouTube removes the upload editor's layout boxes while its whole browser
    window is minimized. Trusted text input cannot focus that hidden editor.
    Restoring only the dedicated automation window keeps the user's existing
    tab untouched while allowing Studio to materialize and persist metadata.
    """

    _mcp_set_cdp_window_state(
        client,
        profile_id,
        target_id,
        "normal",
        timeout=timeout,
    )


def _mcp_set_cdp_window_state(
    client: DonutMcpClient,
    profile_id: str,
    target_id: str,
    state: str,
    *,
    timeout: float = 8.0,
) -> None:
    """Set one automation-owned browser window to a stable state.

    The upload target is always created in its own window. ``minimized`` is
    used while only progress is being observed. ``parked`` keeps Chromium in
    the normal rendering state but places that dedicated window far outside
    the desktop, so Studio retains layout boxes for trusted metadata input
    without covering the user's active window.
    """

    target = str(target_id or "").strip()
    requested = str(state or "").strip().casefold()
    if requested not in {"normal", "minimized", "parked"}:
        raise YouTubeUploadError("Trạng thái cửa sổ automation không hợp lệ.")
    if not target:
        raise YouTubeUploadError("Cửa sổ automation thiếu targetId.")
    window = client.cdp_command(
        profile_id,
        "Browser.getWindowForTarget",
        {"targetId": target},
    )
    window_id = window.get("windowId") if isinstance(window, dict) else None
    if not isinstance(window_id, int):
        raise YouTubeUploadError("Donut không xác định được cửa sổ upload riêng.")
    runtime_state = "normal" if requested == "parked" else requested
    client.cdp_command(
        profile_id,
        "Browser.setWindowBounds",
        {"windowId": window_id, "bounds": {"windowState": runtime_state}},
    )
    if requested == "parked":
        initial_bounds = window.get("bounds") if isinstance(window, dict) else None
        try:
            width = max(1280, int(initial_bounds.get("width") or 0))
            height = max(800, int(initial_bounds.get("height") or 0))
        except (AttributeError, TypeError, ValueError):
            width, height = 1280, 800
        client.cdp_command(
            profile_id,
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {
                    "left": -32000,
                    "top": -32000,
                    "width": width,
                    "height": height,
                },
            },
        )
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        bounds = client.cdp_command(
            profile_id,
            "Browser.getWindowBounds",
            {"windowId": window_id},
        )
        raw_bounds = bounds.get("bounds") if isinstance(bounds, dict) else None
        actual_state = str(
            raw_bounds.get("windowState") if isinstance(raw_bounds, dict) else ""
        ).casefold()
        parked = bool(
            requested == "parked"
            and actual_state == "normal"
            and isinstance(raw_bounds, dict)
            and int(raw_bounds.get("left") or 0) <= -10000
            and int(raw_bounds.get("top") or 0) <= -10000
        )
        if actual_state == requested or parked:
            return
        time.sleep(0.1)
    detail = (
        "khôi phục khỏi trạng thái thu nhỏ"
        if requested == "normal"
        else ("thu nhỏ" if requested == "minimized" else "đưa ra nền")
    )
    raise YouTubeUploadError(f"Cửa sổ upload riêng chưa {detail} trước thời hạn.")


def _mcp_cdp_page_ws_url(
    debug: dict[str, Any],
    *,
    prefer_url: str = "studio.youtube.com",
    timeout: float = 45.0,
) -> str:
    return _mcp_cdp_page_ws_urls(
        debug,
        prefer_url=prefer_url,
        timeout=timeout,
    )[0]


def _ax_value(node: dict[str, Any], field: str) -> str:
    value = node.get(field)
    return str(value.get("value") or "") if isinstance(value, dict) else ""


def _ax_property(node: dict[str, Any], name: str) -> object:
    properties = node.get("properties")
    if not isinstance(properties, list):
        return None
    for item in properties:
        if not isinstance(item, dict) or item.get("name") != name:
            continue
        value = item.get("value")
        return value.get("value") if isinstance(value, dict) else None
    return None


def _require_youtube_browser_bridge(
    profile_id: str,
    *,
    timeout: float = 12.0,
    required_capabilities: Collection[str] | None = None,
) -> BrowserExtensionBridge:
    """Require the paired extension features used for hidden Studio controls."""

    bridge = get_browser_extension_bridge()
    if not bridge.wait_for_profile(profile_id, timeout=timeout):
        raise YouTubeUploadError(
            "DocuRandom Donut Bridge chưa kết nối với profile đã chọn. Tool đã dừng "
            "trước khi gắn video để không chiếm tab của bạn. Hãy mở profile, tải lại "
            "tiện ích DocuRandom Donut Bridge 1.1.38 trở lên rồi bấm Tiếp tục."
        )
    try:
        ping = bridge.command(profile_id, "ping", timeout=8)
    except BrowserExtensionBridgeError as exc:
        raise YouTubeUploadError(
            "Không xác minh được DocuRandom Donut Bridge trong profile đã chọn. "
            "Hãy tải lại tiện ích rồi bấm Tiếp tục."
        ) from exc
    capabilities = (
        {str(item) for item in ping.get("capabilities", [])}
        if isinstance(ping, dict) and isinstance(ping.get("capabilities"), list)
        else set()
    )
    required = {
        "background-tab",
        "youtube-set-text",
        "youtube-native-edit-v2",
        "youtube-tags-v1",
        "youtube-tags-commit-v2",
        "youtube-click",
        "youtube-query",
        "youtube-query-active-v2",
        "youtube-active-controls-v2",
        "youtube-upload-completion-v3",
        "youtube-attached-video",
        "bridge-keepalive",
        "youtube-content-state-v1",
    }
    if required_capabilities:
        required.update(str(item) for item in required_capabilities)
    if not required.issubset(capabilities):
        extension_version = (
            str(ping.get("extensionVersion") or "không rõ")
            if isinstance(ping, dict)
            else "không rõ"
        )
        raise YouTubeUploadError(
            "DocuRandom Donut Bridge đang là bản cũ "
            f"({extension_version}). Hãy tải lại tiện ích 1.1.38 trở lên; checkpoint "
            "được giữ nguyên và tool sẽ không chiếm tab của bạn."
        )
    return bridge


def _ensure_youtube_browser_bridge(
    client: DonutMcpClient,
    profile_id: str,
    *,
    timeout: float = 20.0,
    required_capabilities: Collection[str] | None = None,
) -> BrowserExtensionBridge:
    """Automatically pair the managed extension in the exact Donut profile.

    The pairing page is opened as a background target in this profile only.
    The loopback bridge simultaneously holds a short-lived slot for the same
    Donut profile ID, so another open profile cannot claim the credential.
    """

    bridge = get_browser_extension_bridge()
    if bridge.wait_for_profile(profile_id, timeout=1.5):
        return _require_youtube_browser_bridge(
            profile_id,
            timeout=1.0,
            required_capabilities=required_capabilities,
        )

    pair_target_id = ""
    bridge.begin_pairing(profile_id, ttl=max(20.0, timeout + 5.0))
    try:
        pair_url = (
            f"chrome-extension://{BROWSER_EXTENSION_ID}/popup.html"
            "?docurandom_pair=1"
        )
        created = client.cdp_command(
            profile_id,
            "Target.createTarget",
            {
                "url": pair_url,
                "background": True,
                "newWindow": True,
            },
        )
        pair_target_id = str(created.get("targetId") or "").strip()
        if not pair_target_id:
            raise YouTubeUploadError(
                "Donut không tạo được phiên kích hoạt Browser Bridge."
            )
        try:
            window = client.cdp_command(
                profile_id,
                "Browser.getWindowForTarget",
                {"targetId": pair_target_id},
            )
            window_id = window.get("windowId")
            if isinstance(window_id, int):
                client.cdp_command(
                    profile_id,
                    "Browser.setWindowBounds",
                    {
                        "windowId": window_id,
                        "bounds": {"windowState": "minimized"},
                    },
                )
        except Exception:
            # Pairing remains safe even if this Chromium build refuses to
            # minimize an extension-owned utility target.
            pass
        if not bridge.wait_for_profile(profile_id, timeout=timeout):
            raise YouTubeUploadError(
                "Bridge đã được gán nhưng profile chưa nạp/kích hoạt tiện ích. "
                "Nếu profile đang mở từ trước lúc cập nhật, hãy đóng/mở lại profile "
                "một lần rồi bấm Tiếp tục; không cần cài extension thủ công."
            )
    except BrowserExtensionBridgeError as exc:
        raise YouTubeUploadError(
            "Không tự kích hoạt được DocuRandom Donut Bridge trong profile đã chọn."
        ) from exc
    finally:
        bridge.cancel_pairing()
        if pair_target_id:
            try:
                client.cdp_command(
                    profile_id,
                    "Target.closeTarget",
                    {"targetId": pair_target_id},
                )
            except Exception:
                pass
    return _require_youtube_browser_bridge(
        profile_id,
        timeout=2.0,
        required_capabilities=required_capabilities,
    )


def _browser_extension_tab_for_target(
    client: DonutMcpClient,
    profile_id: str,
    target_id: str,
    *,
    bridge: BrowserExtensionBridge | None = None,
    timeout: float = 12.0,
) -> tuple[BrowserExtensionBridge, int]:
    """Map one exact CDP target to its Chrome extension tab without activation."""

    selected_bridge = bridge or _require_youtube_browser_bridge(
        profile_id,
        timeout=timeout,
    )
    try:
        window = client.cdp_command(
            profile_id,
            "Browser.getWindowForTarget",
            {"targetId": target_id},
        )
    except Exception as exc:
        raise YouTubeUploadError(
            "Không xác định được cửa sổ automation riêng của tab YouTube."
        ) from exc
    window_id = window.get("windowId") if isinstance(window, dict) else None
    if not isinstance(window_id, int):
        raise YouTubeUploadError(
            "Donut không trả đúng cửa sổ automation riêng của tab YouTube."
        )

    deadline = time.monotonic() + max(1.0, timeout)
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            tabs = selected_bridge.command(profile_id, "tabs_list", timeout=5)
            candidates = [
                item
                for item in tabs
                if isinstance(item, dict)
                and item.get("windowId") == window_id
                and isinstance(item.get("id"), int)
            ] if isinstance(tabs, list) else []
            if candidates:
                # A dedicated automation window owns one page. Prefer its active
                # member if Chromium briefly exposes an auxiliary page as well.
                candidates.sort(key=lambda item: not bool(item.get("active")))
                tab_id = int(candidates[0]["id"])
                selected_bridge.command(
                    profile_id,
                    "select_tab",
                    {"tab_id": tab_id},
                    timeout=5,
                )
                return selected_bridge, tab_id
        except BrowserExtensionBridgeError as exc:
            last_error = exc
        time.sleep(0.2)
    raise YouTubeUploadError(
        "Tiện ích chưa nhận đúng tab YouTube thuộc cửa sổ automation riêng. "
        "Tool đã dừng an toàn, không chuyển tab của bạn."
    ) from last_error


class _SemanticCdpController:
    """Coordinate-free YouTube control using CDP DOM and Accessibility domains."""

    def __init__(
        self,
        cdp: _CdpSession,
        *,
        browser_bridge: BrowserExtensionBridge | None = None,
        bridge_profile_id: str = "",
        bridge_tab_id: int = 0,
        automation_target_id: str = "",
        window_state: Callable[[str], None] | None = None,
    ) -> None:
        self.cdp = cdp
        self.browser_bridge = browser_bridge
        self.bridge_profile_id = str(bridge_profile_id or "").strip()
        self.bridge_tab_id = int(bridge_tab_id or 0)
        self.automation_target_id = str(automation_target_id or "").strip()
        self.window_state = window_state
        self.upload_started = False
        self.upload_target_handed_off = False

    @property
    def has_background_bridge(self) -> bool:
        return (
            self.browser_bridge is not None
            and bool(self.bridge_profile_id)
            and self.bridge_tab_id > 0
        )

    def _bridge_command(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 15.0,
    ) -> Any:
        if not self.has_background_bridge or self.browser_bridge is None:
            raise YouTubeUploadError(
                "Tab YouTube chưa được nối với DocuRandom Donut Bridge. Tool dừng "
                "thay vì giành tab đang làm việc của bạn."
            )
        payload = dict(params or {})
        payload["tab_id"] = self.bridge_tab_id
        try:
            return self.browser_bridge.command(
                self.bridge_profile_id,
                command,
                payload,
                timeout=timeout,
            )
        except BrowserExtensionBridgeError as exc:
            raise YouTubeUploadError(
                f"DocuRandom Donut Bridge lỗi ở bước {command}: {exc}"
            ) from exc

    @staticmethod
    def _css_attribute_value(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    def _selector_for_dom_node(self, node: dict[str, Any]) -> str:
        attrs = self._dom_attributes(node)
        if attrs.get("id"):
            return f'[id="{self._css_attribute_value(attrs["id"])}"]'
        if attrs.get("name"):
            return f'[name="{self._css_attribute_value(attrs["name"])}"]'
        return ""

    def enable(self) -> None:
        self.cdp.command("Page.enable")
        self.cdp.command("DOM.enable")
        self.cdp.command("Accessibility.enable")
        if self.automation_target_id:
            # Keep the owned renderer focused without focusing its OS window.
            # This preserves Studio's live editors in the owned off-screen window.
            self.cdp.command("Emulation.setFocusEmulationEnabled", {"enabled": True})

    def prepare_trusted_input(self) -> None:
        """Reassert the owned target's input context without showing its window."""

        if not self.automation_target_id:
            return
        if self.window_state is not None:
            self.window_state("parked")
        # This activates only the tab in the dedicated off-screen window. It
        # never selects or replaces the user's visible Donut tab.
        self.cdp.command(
            "Target.activateTarget",
            {"targetId": self.automation_target_id},
            timeout=10,
        )
        self.cdp.command("Emulation.setFocusEmulationEnabled", {"enabled": True})

    def cancel_pending_leave_site_dialog(self) -> bool:
        """Cancel a stale beforeunload navigation while keeping the upload tab."""

        try:
            self.cdp.command(
                "Page.handleJavaScriptDialog",
                {"accept": False},
            )
        except YouTubeUploadError as exc:
            message = str(exc).casefold()
            if "no dialog is showing" in message or "no javascript dialog" in message:
                return False
            raise
        return True

    def navigate_and_wait(
        self,
        url: str,
        *,
        timeout: float = 90.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Navigate once and wait for a fresh main frame, including redirects."""

        previous_frame = self.page_frame()
        previous_loader = str(previous_frame.get("loaderId") or "").strip()
        previous_url = str(previous_frame.get("url") or "")
        try:
            # _CdpSession gives Page.navigate a longer bounded command window
            # than ordinary DOM commands.  The post-command frame probe below
            # remains authoritative and prevents an unbounded wait.
            navigation = self.cdp.command("Page.navigate", {"url": url})
        except YouTubeUploadError as exc:
            error_message = str(exc).casefold()
            is_navigation_timeout = (
                "page.navigate" in error_message
                and (
                    "hết thời gian" in error_message
                    or "không trả lời" in error_message
                    or "timed out" in error_message
                    or "timeout" in error_message
                )
            )
            if not is_navigation_timeout:
                raise
            # The navigation may already have been accepted by Chromium even
            # when Donut did not return its reply. Inspect the live frame;
            # never accept a beforeunload prompt to force progress.
            self.cancel_pending_leave_site_dialog()
            navigation = {}
        error_text = str(navigation.get("errorText") or "").strip()
        if error_text:
            raise YouTubeUploadError(
                f"Profile không tải được YouTube Studio ({error_text})."
            )
        expected_loader = str(navigation.get("loaderId") or "").strip()
        navigation_seen = False
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if cancel_event is not None:
                YouTubeStudioUploader._cancelled(cancel_event)
            frame = self.page_frame()
            current_loader = str(frame.get("loaderId") or "").strip()
            current_url = str(frame.get("url") or "")
            if expected_loader and current_loader == expected_loader:
                navigation_seen = True
            elif previous_loader and current_loader and current_loader != previous_loader:
                # YouTube can replace the loader returned by Page.navigate
                # during a server redirect or Studio bootstrap.  That is still
                # the new navigation, not the stale DOM we must reject.
                navigation_seen = True
            elif not previous_loader and current_url and current_url != previous_url:
                navigation_seen = True
            if not navigation_seen:
                time.sleep(0.25)
                continue
            try:
                host = (urllib.parse.urlsplit(current_url).hostname or "").casefold()
            except ValueError:
                host = ""
            if host == "studio.youtube.com":
                # The channel-id/file-input waits below are the authoritative
                # DOM-ready probes. Studio can still replace this root route
                # with the signed-in channel route after Page.navigate ends.
                self.raise_for_page_error()
                return
            if host and host != "studio.youtube.com":
                self.raise_for_page_error()
            time.sleep(0.25)
        self.raise_for_page_error()
        raise YouTubeUploadError(
            "YouTube Studio chưa tải xong đúng lần điều hướng trước thời hạn."
        )

    def click(self, node: dict[str, Any]) -> None:
        """Activate a semantic DOM node inside its CDP target without Runtime."""

        backend_id = int(node.get("backendDOMNodeId") or node.get("backendNodeId") or 0)
        if not backend_id:
            raise YouTubeUploadError("Điều khiển YouTube không còn tồn tại trên trang.")
        self.cdp.command("DOM.focus", {"backendNodeId": backend_id})
        key = {
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
        }
        # Chromium's button activation uses keyDown (not rawKeyDown).  The
        # latter was why the first semantic attempt focused the control but did
        # not open YouTube's upload dialog.
        self.cdp.command("Input.dispatchKeyEvent", {"type": "keyDown", **key})
        self.cdp.command("Input.dispatchKeyEvent", {"type": "keyUp", **key})

    def page_url(self) -> str:
        frame = self.page_frame()
        return str(frame.get("url") or "")

    def page_frame(self) -> dict[str, Any]:
        result = self.cdp.command("Page.getFrameTree")
        frame_tree = result.get("frameTree")
        frame = frame_tree.get("frame") if isinstance(frame_tree, dict) else None
        return frame if isinstance(frame, dict) else {}

    def wait_channel_id(
        self,
        *,
        timeout: float = 90.0,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Wait for Studio to resolve its root route to the signed-in channel."""

        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if cancel_event is not None:
                YouTubeStudioUploader._cancelled(cancel_event)
            self.raise_for_page_error()
            match = _CHANNEL_STUDIO_URL.match(self.page_url())
            if match:
                return match.group(1)
            time.sleep(0.5)
        self.raise_for_page_error()
        raise YouTubeUploadError(
            "YouTube Studio chưa xác định được đúng kênh của profile trước thời hạn."
        )

    @staticmethod
    def _safe_page_location(url: str) -> str:
        """Keep useful redirect context without leaking query-string tokens."""

        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return "địa chỉ không hợp lệ"
        if not parsed.scheme:
            return url[:160] or "không rõ địa chỉ"
        host = parsed.hostname or ""
        return f"{parsed.scheme}://{host}{parsed.path}"[:240]

    @staticmethod
    def _is_login_page(url: str) -> bool:
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").casefold()
        path = parsed.path.casefold()
        return (
            host in {"accounts.google.com", "consent.youtube.com", "consent.google.com"}
            or "servicelogin" in path
            or "signin" in path
        )

    def raise_for_page_error(self) -> None:
        frame = self.page_frame()
        url = str(frame.get("url") or "")
        unreachable = str(frame.get("unreachableUrl") or "")
        if self._is_login_page(url):
            raise YouTubeUploadError(
                "Profile Donut đã mất phiên đăng nhập YouTube. Tool đã dừng trước khi "
                "đăng video; hãy đăng nhập lại đúng kênh trong profile rồi bấm Tiếp tục. "
                f"Trang hiện tại: {self._safe_page_location(url)}"
            )
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").casefold()
        except ValueError:
            host = ""
        if (
            host
            and host != "studio.youtube.com"
            and not url.casefold().startswith("chrome-error://")
        ):
            raise YouTubeUploadError(
                "YouTube Studio đã chuyển profile sang một trang ngoài dự kiến. Tool đã "
                "dừng an toàn để không thao tác nhầm. "
                f"Trang hiện tại: {self._safe_page_location(url)}"
            )
        if not unreachable and not url.casefold().startswith("chrome-error://"):
            return
        error_code = ""
        try:
            for node in self.nodes(depth=8):
                combined = f"{_ax_value(node, 'name')} {_ax_value(node, 'value')}"
                match = re.search(r"\bERR_[A-Z0-9_]+\b", combined)
                if match:
                    error_code = match.group(0)
                    break
        except YouTubeUploadError:
            pass
        detail = error_code or "trang lỗi mạng của Chromium"
        target = self._safe_page_location(unreachable or url)
        if "PROXY" in detail or "TUNNEL" in detail:
            raise YouTubeUploadError(
                f"Proxy của profile không tải được YouTube Studio ({detail}). "
                f"Địa chỉ lỗi: {target}"
            )
        raise YouTubeUploadError(
            f"Profile không tải được YouTube Studio do lỗi mạng ({detail}). "
            f"Địa chỉ lỗi: {target}"
        )

    def mark_upload_started(self) -> None:
        self.upload_started = True

    def mark_upload_target_handed_off(self) -> None:
        """Record that the upload target was closed after a proven handoff."""

        self.upload_target_handed_off = True

    def raise_for_upload_error(self) -> None:
        if not self.upload_started:
            return
        messages: list[str] = []
        for node in self.nodes(depth=30):
            if node.get("ignored") is True or _ax_value(node, "role") != "alert":
                continue
            message = (_ax_value(node, "name") or _ax_value(node, "value")).strip()
            if message:
                messages.append(" ".join(message.split())[:400])
        if messages:
            raise YouTubeUploadError(
                "YouTube báo lỗi trong lượt upload duy nhất: " + " · ".join(messages[:2])
            )

    def nodes(self, *, depth: int = 40) -> list[dict[str, Any]]:
        result = self.cdp.command("Accessibility.getFullAXTree", {"depth": depth})
        nodes = result.get("nodes")
        return [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []

    @staticmethod
    def _dom_attributes(node: dict[str, Any]) -> dict[str, str]:
        raw = node.get("attributes")
        if not isinstance(raw, list):
            return {}
        return {
            str(raw[index]): str(raw[index + 1])
            for index in range(0, len(raw) - 1, 2)
        }

    def dom_nodes(self) -> list[dict[str, Any]]:
        result = self.cdp.command(
            "DOM.getFlattenedDocument",
            {"depth": -1, "pierce": True},
        )
        nodes = result.get("nodes")
        return [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []

    def dom_node_has_box(self, node: dict[str, Any]) -> bool:
        backend_id = int(node.get("backendNodeId") or 0)
        if not backend_id:
            return False
        try:
            result = self.cdp.command(
                "DOM.getBoxModel",
                {"backendNodeId": backend_id},
            )
        except YouTubeUploadError:
            return False
        return isinstance(result.get("model"), dict)

    def wait_laid_out_dom(
        self,
        *,
        attributes: dict[str, str] | None = None,
        node_names: tuple[str, ...] = (),
        timeout: float = 45.0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Wait for a matching live element with a real layout box."""

        wanted_names = {name.upper() for name in node_names}
        wanted_attrs = attributes or {}
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if cancel_event is not None:
                YouTubeStudioUploader._cancelled(cancel_event)
            self.raise_for_page_error()
            for node in reversed(self.dom_nodes()):
                if wanted_names and str(node.get("nodeName") or "").upper() not in wanted_names:
                    continue
                attrs = self._dom_attributes(node)
                if any(attrs.get(key) != value for key, value in wanted_attrs.items()):
                    continue
                if self.dom_node_has_box(node):
                    return node
            time.sleep(0.5)
        raise YouTubeUploadError(
            "YouTube Studio mới chỉ tạo DOM ẩn, chưa mở hộp upload thật trước thời hạn."
        )

    def find_dom(
        self,
        nodes: list[dict[str, Any]],
        *,
        attributes: dict[str, str] | None = None,
        node_names: tuple[str, ...] = (),
        ancestor_ids: tuple[str, ...] = (),
        require_laid_out: bool = False,
    ) -> dict[str, Any] | None:
        wanted_names = {name.upper() for name in node_names}
        wanted_attrs = attributes or {}
        by_id = {node.get("nodeId"): node for node in nodes if node.get("nodeId")}
        for node in reversed(nodes):
            if wanted_names and str(node.get("nodeName") or "").upper() not in wanted_names:
                continue
            attrs = self._dom_attributes(node)
            if any(attrs.get(key) != value for key, value in wanted_attrs.items()):
                continue
            if ancestor_ids:
                seen_ids: set[str] = set()
                current = node
                for _ in range(20):
                    current = by_id.get(current.get("parentId"))
                    if current is None:
                        break
                    parent_id = self._dom_attributes(current).get("id")
                    if parent_id:
                        seen_ids.add(parent_id)
                if not all(ancestor_id in seen_ids for ancestor_id in ancestor_ids):
                    continue
            if node.get("backendNodeId") and (
                not require_laid_out or self.dom_node_has_box(node)
            ):
                return node
        return None

    def wait_dom(
        self,
        *,
        attributes: dict[str, str] | None = None,
        node_names: tuple[str, ...] = (),
        ancestor_ids: tuple[str, ...] = (),
        timeout: float = 45.0,
        require_enabled: bool = False,
        require_laid_out: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event is not None:
                YouTubeStudioUploader._cancelled(cancel_event)
            self.raise_for_page_error()
            self.raise_for_upload_error()
            node = self.find_dom(
                self.dom_nodes(),
                attributes=attributes,
                node_names=node_names,
                ancestor_ids=ancestor_ids,
                require_laid_out=require_laid_out,
            )
            attrs = self._dom_attributes(node) if node is not None else {}
            if node is not None and (
                not require_enabled
                or (
                    "disabled" not in attrs
                    and attrs.get("aria-disabled", "false").casefold() != "true"
                )
            ):
                return node
            time.sleep(0.5)
        detail = ", ".join(f"{key}={value}" for key, value in (attributes or {}).items())
        self.raise_for_page_error()
        location = self._safe_page_location(self.page_url())
        raise YouTubeUploadError(
            f"YouTube Studio không hiện điều khiển {detail or node_names} trước thời hạn. "
            "Trang có thể tải thiếu do proxy chậm hoặc YouTube vừa thay đổi giao diện. "
            f"Trang hiện tại: {location}"
        )

    @staticmethod
    def _normalized(value: str) -> str:
        return " ".join(value.casefold().split())

    def find(
        self,
        nodes: list[dict[str, Any]],
        *,
        role: str,
        names: tuple[str, ...],
        contains: bool = False,
    ) -> dict[str, Any] | None:
        wanted = tuple(self._normalized(name) for name in names)
        matches: list[dict[str, Any]] = []
        for node in nodes:
            if node.get("ignored") is True or _ax_value(node, "role") != role:
                continue
            actual = self._normalized(_ax_value(node, "name"))
            matched = any(item in actual for item in wanted) if contains else actual in wanted
            if matched and node.get("backendDOMNodeId"):
                matches.append(node)
        focusable = [node for node in matches if _ax_property(node, "focusable") is True]
        return (focusable or matches)[-1] if matches else None

    def wait_for(
        self,
        *,
        role: str,
        names: tuple[str, ...],
        contains: bool = False,
        timeout: float = 45.0,
        require_enabled: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event is not None:
                YouTubeStudioUploader._cancelled(cancel_event)
            self.raise_for_page_error()
            self.raise_for_upload_error()
            node = self.find(self.nodes(), role=role, names=names, contains=contains)
            if node is not None and (
                not require_enabled or _ax_property(node, "disabled") is not True
            ):
                return node
            time.sleep(0.5)
        joined = " / ".join(names)
        self.raise_for_page_error()
        location = self._safe_page_location(self.page_url())
        raise YouTubeUploadError(
            f"YouTube Studio không hiện {role} “{joined}” trước thời hạn. "
            "Trang có thể tải thiếu do proxy chậm hoặc YouTube vừa thay đổi giao diện. "
            f"Trang hiện tại: {location}"
        )

    def _key(
        self,
        *,
        event_type: str,
        key: str,
        code: str,
        virtual_key: int,
        modifiers: int = 0,
    ) -> None:
        self.cdp.command(
            "Input.dispatchKeyEvent",
            {
                "type": event_type,
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": virtual_key,
                "nativeVirtualKeyCode": virtual_key,
                "modifiers": modifiers,
            },
        )

    def _key_press(
        self,
        key: str,
        code: str,
        virtual_key: int,
        *,
        modifiers: int = 0,
    ) -> None:
        self._key(
            event_type="keyDown",
            key=key,
            code=code,
            virtual_key=virtual_key,
            modifiers=modifiers,
        )
        self._key(
            event_type="keyUp",
            key=key,
            code=code,
            virtual_key=virtual_key,
            modifiers=modifiers,
        )

    def press(self, node: dict[str, Any], *, space: bool = False) -> None:
        # The extension used to call element.click() in the page.  Studio can
        # animate that untrusted click and even move to the next wizard page
        # without committing its Polymer form model.  The resulting draft has
        # an uploaded video but empty metadata.  This controller owns an exact
        # restored CDP target, so always activate controls through Chromium's
        # trusted keyboard input path instead of a page-side synthetic click.
        self.prepare_trusted_input()
        backend_id = int(node.get("backendDOMNodeId") or node.get("backendNodeId"))
        self.cdp.command("DOM.focus", {"backendNodeId": backend_id})
        if space:
            self._key_press(" ", "Space", 32)
        else:
            self._key_press("Enter", "Enter", 13)

    def click_dom(self, node: dict[str, Any]) -> None:
        """Activate the exact laid-out control with trusted pointer input."""
        self.prepare_trusted_input()
        backend_id = int(node.get("backendDOMNodeId") or node.get("backendNodeId") or 0)
        model = self.cdp.command("DOM.getBoxModel", {"backendNodeId": backend_id}).get("model", {})
        quad = model.get("content") or model.get("border")
        if not isinstance(quad, list) or len(quad) != 8:
            raise YouTubeUploadError("Nút YouTube không có vùng bấm được xác minh.")
        x = sum(float(quad[i]) for i in (0, 2, 4, 6)) / 4
        y = sum(float(quad[i]) for i in (1, 3, 5, 7)) / 4
        if not all(math.isfinite(v) for v in (x, y)):
            raise YouTubeUploadError("Vùng bấm YouTube không hợp lệ.")
        # Match Chromium's physical pointer sequence, including hover and
        # the pressed-button mask used by Studio's custom button handlers.
        self.cdp.command("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0,
        })
        for kind, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            self.cdp.command("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y, "button": "left", "buttons": buttons,
                "clickCount": 1,
            })

    def set_text(
        self,
        node: dict[str, Any],
        value: str,
        *,
        verify_timeout: float = 8.0,
    ) -> None:
        self.prepare_trusted_input()
        backend_id = int(node.get("backendDOMNodeId") or node.get("backendNodeId"))
        self.cdp.command("DOM.focus", {"backendNodeId": backend_id})
        self._key_press("a", "KeyA", 65, modifiers=2)
        self._key_press("Backspace", "Backspace", 8)
        self.cdp.command("Input.insertText", {"text": value})
        deadline = time.monotonic() + verify_timeout
        while time.monotonic() < deadline:
            result = self.cdp.command(
                "Accessibility.getPartialAXTree",
                {"backendNodeId": backend_id, "fetchRelatives": False},
            )
            nodes = result.get("nodes")
            actual = ""
            if isinstance(nodes, list) and nodes and isinstance(nodes[0], dict):
                actual = _ax_value(nodes[0], "value")
            if actual.rstrip("\r\n") == value.rstrip("\r\n"):
                # YouTube commits contenteditable fields on focus loss.  A DOM
                # or AX read before blur only proves that glyphs are visible;
                # it does not prove that Studio accepted the value into the
                # draft model.  Tab is a trusted, reversible commit boundary.
                self._key_press("Tab", "Tab", 9)
                time.sleep(0.3)
                return
            time.sleep(0.2)
        raise YouTubeUploadError(
            "YouTube Studio không xác nhận đúng nội dung vừa điền. Tool đã dừng thay vì "
            "tiếp tục với metadata có thể sai."
        )

    def set_named_text(
        self,
        field: str,
        value: str,
        *,
        verify_timeout: float = 10.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Set one stable Studio field, including when its tab is hidden."""

        def wait_field_node(timeout: float) -> dict[str, Any]:
            if field == "title":
                return self.wait_dom(
                    attributes={"id": "textbox"},
                    ancestor_ids=("title-textarea",),
                    timeout=timeout,
                    require_laid_out=True,
                    cancel_event=cancel_event,
                )
            if field == "description":
                return self.wait_dom(
                    attributes={"id": "textbox"},
                    ancestor_ids=("description-textarea",),
                    timeout=timeout,
                    require_laid_out=True,
                    cancel_event=cancel_event,
                )
            if field == "tags":
                return self.wait_dom(
                    node_names=("INPUT",),
                    ancestor_ids=("tags-container",),
                    timeout=timeout,
                    require_laid_out=True,
                    cancel_event=cancel_event,
                )
            raise YouTubeUploadError(f"Trường YouTube không được hỗ trợ: {field}")

        if field in {"title", "description"}:
            # On a freshly loaded /edit page the current value is already an
            # authoritative server round-trip. Do not clear and retype a field
            # that is already exact: background Chromium may reject trusted
            # key input even though the saved Polymer value is correct.
            try:
                existing = self.named_text(
                    field,
                    timeout=min(15.0, max(1.0, verify_timeout)),
                    require_laid_out=True,
                    cancel_event=cancel_event,
                )
            except YouTubeUploadError:
                existing = ""
            if existing.rstrip("\r\n") == value.rstrip("\r\n"):
                return

            # YouTube Studio's Polymer contenteditable can briefly reflect an
            # extension-side execCommand mutation, then restore its internal
            # value (usually the filename). Drive the editor through CDP's
            # trusted Input domain instead. The controller is connected to the
            # exact automation-owned target, so this neither activates nor
            # types into the user's working tab.
            deadline = time.monotonic() + max(1.0, verify_timeout)
            last_error: YouTubeUploadError | None = None
            for attempt in range(3):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    node = wait_field_node(
                        remaining if attempt == 0 else min(12.0, remaining)
                    )
                    self.set_text(
                        node,
                        value,
                        verify_timeout=min(12.0, max(1.0, deadline - time.monotonic())),
                    )

                    # A successful immediate AX read is not enough: Studio can
                    # replace/reset the editor in the next animation frame.
                    # Reacquire the live field and require it to remain correct
                    # for a full second before allowing the upload to continue.
                    stable_since = 0.0
                    stability_deadline = min(deadline, time.monotonic() + 3.0)
                    while time.monotonic() < stability_deadline:
                        if cancel_event is not None:
                            YouTubeStudioUploader._cancelled(cancel_event)
                        current = wait_field_node(
                            min(1.0, max(0.1, stability_deadline - time.monotonic()))
                        )
                        backend_id = int(
                            current.get("backendNodeId")
                            or current.get("backendDOMNodeId")
                            or 0
                        )
                        result = self.cdp.command(
                            "Accessibility.getPartialAXTree",
                            {"backendNodeId": backend_id, "fetchRelatives": False},
                        )
                        nodes = result.get("nodes")
                        actual = ""
                        if isinstance(nodes, list) and nodes and isinstance(nodes[0], dict):
                            actual = _ax_value(nodes[0], "value")
                        if actual.rstrip("\r\n") == value.rstrip("\r\n"):
                            if not stable_since:
                                stable_since = time.monotonic()
                            elif time.monotonic() - stable_since >= 1.0:
                                logger.debug(
                                    "youtube_metadata_field_stable field=%s attempt=%d expected_length=%d",
                                    field,
                                    attempt + 1,
                                    len(value),
                                )
                                return
                        else:
                            last_error = YouTubeUploadError(
                                f"YouTube đã thay lại trường {field} sau khi nhập "
                                f"(cần {len(value)} ký tự, đang có {len(actual)} ký tự)."
                            )
                            break
                        time.sleep(0.2)
                except YouTubeUploadError as exc:
                    last_error = exc
                if time.monotonic() < deadline:
                    time.sleep(0.4)

            if not self.has_background_bridge:
                detail = f" Chi tiết lần cuối: {last_error}" if last_error is not None else ""
                raise YouTubeUploadError(
                    f"YouTube Studio không giữ đúng nội dung vừa điền ở trường {field}. "
                    f"Tool đã dừng trước khi đăng.{detail}"
                )
            logger.warning(
                "youtube_cdp_text_fallback_to_bridge field=%s detail=%s",
                field,
                last_error,
            )

        if not self.has_background_bridge:
            node = wait_field_node(verify_timeout)
            self.set_text(node, value, verify_timeout=verify_timeout)
            return

        deadline = time.monotonic() + max(1.0, verify_timeout)
        expected = value.rstrip("\r\n")
        # Studio may replace its metadata editor once shortly after the file is
        # attached. Wait for the real field, then allow at most three
        # reversible assignments and require the value to remain stable. This
        # avoids both the stale hidden template and an unbounded write loop.
        for _attempt in range(3):
            while time.monotonic() < deadline:
                if cancel_event is not None:
                    YouTubeStudioUploader._cancelled(cancel_event)
                current = self._bridge_command(
                    "youtube_get_text",
                    {"field": field},
                    timeout=5,
                )
                if isinstance(current, dict) and current.get("found"):
                    break
                time.sleep(0.25)
            else:
                break

            self._bridge_command(
                "youtube_set_text",
                {"field": field, "text": value},
                timeout=min(20.0, max(10.0, verify_timeout)),
            )
            attempt_deadline = min(deadline, time.monotonic() + 12.0)
            stable_since = 0.0
            while time.monotonic() < attempt_deadline:
                if cancel_event is not None:
                    YouTubeStudioUploader._cancelled(cancel_event)
                current = self._bridge_command(
                    "youtube_get_text",
                    {"field": field},
                    timeout=5,
                )
                actual = (
                    str(current.get("value") or "")
                    if isinstance(current, dict)
                    else ""
                )
                matches = (
                    isinstance(current, dict)
                    and current.get("found")
                    and actual.rstrip("\r\n") == expected
                )
                if matches:
                    if not stable_since:
                        stable_since = time.monotonic()
                    elif time.monotonic() - stable_since >= 1.0:
                        return
                elif stable_since:
                    # Studio replaced/reset the editor; reacquire it once.
                    break
                time.sleep(0.2)
            time.sleep(0.4)
        raise YouTubeUploadError(
            f"YouTube Studio không giữ đúng nội dung vừa điền ở trường {field}. "
            "Tool đã dừng trước khi đăng."
        )

    def select_radio(
        self,
        *,
        names: tuple[str, ...],
        contains: bool = False,
        timeout: float = 45.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        node = self.wait_for(
            role="radio",
            names=names,
            contains=contains,
            timeout=timeout,
            cancel_event=cancel_event,
        )
        if _ax_property(node, "checked") != "true":
            self.press(node, space=True)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            current = self.find(self.nodes(), role="radio", names=names, contains=contains)
            if current is not None and _ax_property(current, "checked") == "true":
                return
            time.sleep(0.2)
        raise YouTubeUploadError(f"Không chọn được tùy chọn YouTube “{' / '.join(names)}”.")

    def select_dom_radio(
        self,
        name: str,
        *,
        timeout: float = 45.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        node = self.wait_dom(
            attributes={"name": name},
            timeout=timeout,
            require_laid_out=True,
            cancel_event=cancel_event,
        )
        attrs = self._dom_attributes(node)
        if attrs.get("aria-checked", "false").casefold() != "true" and "checked" not in attrs:
            self.press(node, space=True)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            current = self.find_dom(self.dom_nodes(), attributes={"name": name})
            attrs = self._dom_attributes(current) if current is not None else {}
            if attrs.get("aria-checked", "false").casefold() == "true" or "checked" in attrs:
                return
            time.sleep(0.2)
        if self.has_background_bridge:
            selector = f'[name="{self._css_attribute_value(name)}"]'
            self._bridge_command(
                "youtube_click",
                {"selector": selector},
                timeout=10,
            )
            bridge_deadline = time.monotonic() + 8.0
            while time.monotonic() < bridge_deadline:
                if cancel_event is not None:
                    YouTubeStudioUploader._cancelled(cancel_event)
                state = self._bridge_command(
                    "youtube_query",
                    {"selector": selector},
                    timeout=5,
                )
                if isinstance(state, dict) and (
                    bool(state.get("checked"))
                    or str(state.get("ariaChecked") or "").casefold() == "true"
                ):
                    return
                time.sleep(0.2)
        raise YouTubeUploadError(f"Không chọn được tùy chọn YouTube {name}.")

    def named_text(
        self,
        field: str,
        *,
        timeout: float = 30.0,
        require_laid_out: bool = True,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Read the exact live title/description value through AX."""

        ancestor = {
            "title": "title-textarea",
            "description": "description-textarea",
        }.get(field)
        if ancestor is None:
            raise YouTubeUploadError(f"Trường YouTube không được hỗ trợ: {field}")
        node = self.wait_dom(
            attributes={"id": "textbox"},
            ancestor_ids=(ancestor,),
            timeout=timeout,
            require_laid_out=require_laid_out,
            cancel_event=cancel_event,
        )
        backend_id = int(node.get("backendNodeId") or node.get("backendDOMNodeId") or 0)
        result = self.cdp.command(
            "Accessibility.getPartialAXTree",
            {"backendNodeId": backend_id, "fetchRelatives": False},
        )
        nodes = result.get("nodes")
        if not isinstance(nodes, list) or not nodes or not isinstance(nodes[0], dict):
            return ""
        return _ax_value(nodes[0], "value")

    def dom_radio_checked(self, name: str) -> bool:
        node = self.find_dom(self.dom_nodes(), attributes={"name": name})
        attrs = self._dom_attributes(node) if node is not None else {}
        return (
            attrs.get("aria-checked", "false").casefold() == "true"
            or "checked" in attrs
        )

    def tag_labels(self) -> tuple[str, ...]:
        """Return visible Studio tag-chip labels in their saved order."""

        if self.has_background_bridge:
            current = self._bridge_command(
                "youtube_get_text",
                {"field": "tags"},
                timeout=8,
            )
            bridge_tags = current.get("tags") if isinstance(current, dict) else None
            if isinstance(bridge_tags, list):
                labels = tuple(
                    str(value).strip() for value in bridge_tags if str(value).strip()
                )
                if labels:
                    return labels

        nodes = self.dom_nodes()
        by_id = {node.get("nodeId"): node for node in nodes if node.get("nodeId")}
        labels: list[str] = []
        for node in nodes:
            if str(node.get("nodeName") or "").upper() != "YTCP-CHIP":
                continue
            current = node
            inside_tags = False
            for _ in range(24):
                current = by_id.get(current.get("parentId"))
                if current is None:
                    break
                if self._dom_attributes(current).get("id") == "tags-container":
                    inside_tags = True
                    break
            if not inside_tags or not self.dom_node_has_box(node):
                continue
            label = self._dom_attributes(node).get("aria-label", "").strip()
            if label:
                labels.append(label)
        return tuple(labels)

    @staticmethod
    def tag_labels_match(labels: tuple[str, ...], expected: tuple[str, ...]) -> bool:
        if len(labels) != len(expected):
            return False
        return all(
            label == tag
            or label.startswith(tag + ".")
            or label.startswith(tag + "。")
            or label.startswith(tag + " ")
            for label, tag in zip(labels, expected)
        )

    def set_tags(
        self,
        tags: tuple[str, ...],
        *,
        timeout: float = 60.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Replace Studio tags through trusted typing and verify chip creation."""

        expected = tuple(str(tag).strip() for tag in tags if str(tag).strip())
        self.prepare_trusted_input()
        if self.tag_labels_match(self.tag_labels(), expected):
            return

        clear = self.find_dom(
            self.dom_nodes(),
            attributes={"id": "clear-button"},
            require_laid_out=True,
        )
        if clear is not None:
            self.press(clear)
            clear_deadline = time.monotonic() + 8.0
            while time.monotonic() < clear_deadline and self.tag_labels():
                if cancel_event is not None:
                    YouTubeStudioUploader._cancelled(cancel_event)
                time.sleep(0.2)
            if self.tag_labels():
                raise YouTubeUploadError("YouTube không xóa được danh sách SEO tags cũ.")

        deadline = time.monotonic() + max(1.0, timeout)
        for index, tag in enumerate(expected, start=1):
            self.prepare_trusted_input()
            input_node = self.wait_dom(
                attributes={"id": "text-input"},
                node_names=("INPUT",),
                ancestor_ids=("tags-container",),
                timeout=max(1.0, deadline - time.monotonic()),
                require_laid_out=True,
                cancel_event=cancel_event,
            )
            backend_id = int(input_node.get("backendNodeId") or 0)
            self.cdp.command("DOM.focus", {"backendNodeId": backend_id})
            self.cdp.command("Input.insertText", {"text": tag})
            self._key_press("Enter", "Enter", 13)
            chip_deadline = min(deadline, time.monotonic() + 8.0)
            while time.monotonic() < chip_deadline:
                if cancel_event is not None:
                    YouTubeStudioUploader._cancelled(cancel_event)
                labels = self.tag_labels()
                if len(labels) >= index and self.tag_labels_match(
                    labels[:index], expected[:index]
                ):
                    break
                time.sleep(0.2)
            else:
                if not self.has_background_bridge:
                    raise YouTubeUploadError(
                        f"YouTube không commit được SEO tag thứ {index}: {tag}"
                    )
                committed = self._bridge_command(
                    "youtube_set_tags",
                    {"tags": list(expected)},
                    timeout=min(30.0, max(10.0, deadline - time.monotonic())),
                )
                labels = tuple(
                    str(value).strip()
                    for value in (
                        committed.get("tags")
                        if isinstance(committed, dict)
                        and isinstance(committed.get("tags"), list)
                        else []
                    )
                    if str(value).strip()
                )
                if self.tag_labels_match(labels, expected):
                    return
                raise YouTubeUploadError(
                    f"YouTube không commit được SEO tag thứ {index}: {tag}"
                )

        labels = self.tag_labels()
        if not self.tag_labels_match(labels, expected):
            raise YouTubeUploadError(
                "YouTube không giữ đúng toàn bộ SEO tags sau khi nhập."
            )

    def press_escape(self) -> None:
        self._key_press("Escape", "Escape", 27)

    def find_file_input(
        self,
        nodes: list[dict[str, Any]],
        accept_hint: str,
    ) -> dict[str, Any] | None:
        hint = accept_hint.casefold()
        for node in reversed(nodes):
            if str(node.get("nodeName") or "").upper() != "INPUT":
                continue
            attrs = self._dom_attributes(node)
            if attrs.get("type", "").casefold() != "file":
                continue
            accept = attrs.get("accept", "").casefold()
            is_video = "video" in accept or attrs.get("name") == "Filedata"
            is_image = "image" in accept
            if (hint == "video" and is_video) or (hint == "image" and is_image):
                if node.get("backendNodeId"):
                    return node
        return None

    def wait_file_input(
        self,
        accept_hint: str,
        *,
        timeout: float = 45.0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event is not None:
                YouTubeStudioUploader._cancelled(cancel_event)
            self.raise_for_page_error()
            self.raise_for_upload_error()
            node = self.find_file_input(self.dom_nodes(), accept_hint)
            if node is not None:
                return node
            time.sleep(0.5)
        raise YouTubeUploadError(
            f"YouTube Studio không hiện ô chọn file {accept_hint} đúng loại trước thời hạn."
        )

    def set_file(self, path: Path, accept_hint: str) -> None:
        node = self.find_file_input(self.dom_nodes(), accept_hint)
        if node is None or not node.get("backendNodeId"):
            raise YouTubeUploadError(
                f"Không tìm thấy ô chọn file {accept_hint} đúng loại trên YouTube Studio. "
                "Tool từ chối đoán để tránh gắn video nhầm chỗ."
            )
        self.cdp.command(
            "DOM.setFileInputFiles",
            {
                "files": [str(path.resolve())],
                "backendNodeId": int(node["backendNodeId"]),
            },
        )

    def custom_thumbnail_ready(self) -> bool:
        """Return whether Studio exposes a live custom-thumbnail preview."""

        node = self.find_dom(
            self.dom_nodes(),
            attributes={"id": "preview-button"},
            node_names=("BUTTON",),
            ancestor_ids=("custom-still-editor-wrapper",),
            require_laid_out=True,
        )
        return node is not None

    def wait_custom_thumbnail_ready(
        self,
        *,
        timeout: float = 45.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Wait until Studio has accepted the selected custom thumbnail."""

        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if cancel_event is not None:
                YouTubeStudioUploader._cancelled(cancel_event)
            self.raise_for_page_error()
            if self.custom_thumbnail_ready():
                return
            time.sleep(0.5)
        raise YouTubeUploadError(
            "YouTube Studio chưa xác nhận thumbnail tùy chỉnh sau khi đã gắn file."
        )

    def attached_video(self) -> dict[str, object]:
        """Return the one video already attached to the live upload dialog.

        This is a read-only recovery probe. It never assigns to an input and is
        deliberately strict so a failed attempt cannot be mistaken for another
        video that happens to be open in the same profile.
        """

        if not self.has_background_bridge:
            raise YouTubeUploadError(
                "DocuRandom Donut Bridge chưa sẵn sàng để kiểm tra file đã gắn. "
                "Tool không dùng Runtime.evaluate native và không gắn lại file."
            )
        value = self._bridge_command(
            "youtube_attached_video",
            timeout=8,
        )
        return dict(value) if isinstance(value, dict) else {}

    def interrupted_upload_state(
        self,
        expected_file_name: str,
        *,
        resume: bool = False,
        timeout: float = 15.0,
    ) -> dict[str, object]:
        """Probe or resume one exact YouTube "Continue uploading" row.

        YouTube keeps an interrupted upload in the channel-content list and
        asks for the same source file again.  This is a resume operation, not
        a new upload wizard.  The bridge matches the visible row by its exact
        filename and refuses to click when zero or multiple rows match.
        """

        if not self.has_background_bridge:
            return {}
        value = self._bridge_command(
            "youtube_resume_interrupted_upload",
            {
                "file_name": str(expected_file_name),
                "action": "resume" if resume else "probe",
            },
            timeout=timeout,
        )
        return dict(value) if isinstance(value, dict) else {}

    def content_upload_state(
        self,
        video_id: str = "",
        *,
        resume: bool = False,
        timeout: float = 20.0,
    ) -> dict[str, object]:
        """Read one exact Content row through the paired extension.

        YouTube can render a draft row with only a thumbnail ``/vi/<id>/``
        URL and no ``/video/<id>/edit`` anchor.  The extension reader handles
        that current Studio shape and can click Continue only after proving
        that exactly one row owns the requested video ID.
        """

        if not self.has_background_bridge:
            return {}
        value = self._bridge_command(
            "youtube_content_state",
            {
                "video_id": str(video_id or "").strip(),
                "action": "resume" if resume else "probe",
            },
            timeout=timeout,
        )
        return dict(value) if isinstance(value, dict) else {}

    def href(self, node: dict[str, Any]) -> str:
        backend_id = node.get("backendDOMNodeId")
        if not backend_id:
            return ""
        result = self.cdp.command(
            "DOM.describeNode",
            {"backendNodeId": int(backend_id), "depth": 0, "pierce": True},
        )
        dom_node = result.get("node")
        attributes = dom_node.get("attributes") if isinstance(dom_node, dict) else None
        if not isinstance(attributes, list):
            return ""
        attrs = {
            str(attributes[index]): str(attributes[index + 1])
            for index in range(0, len(attributes) - 1, 2)
        }
        return attrs.get("href", "")

    def upload_dialog_url(self) -> str:
        """Read the allocated ID from exactly one visible owned upload dialog."""
        ids = set()
        for node in self.dom_nodes():
            if node.get("nodeName") != "YTCP-UPLOADS-DIALOG":
                continue
            attrs = self._dom_attributes(node)
            video_id = str(attrs.get("video-id") or "")
            if ("hidden" not in attrs and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id)
                    and self.dom_node_has_box(node)):
                ids.add(video_id)
        return f"https://youtu.be/{next(iter(ids))}" if len(ids) == 1 else ""

    def youtube_url(self) -> str:
        # The upload dialog remains in a shadow DOM while Studio shows
        # ``Saving...``.  On Wayfern, a second raw CDP read can be rejected
        # after the extension performed the final click, even though the
        # exact owned background tab is still available to the paired bridge.
        # Read that stable ``#video-link`` first so a successful upload is not
        # left in ``uncertain`` merely because the AX ancestry changed.
        if self.has_background_bridge:
            for selector in (
                "#video-link",
                "a[href*='youtu.be/']",
                "a[href*='youtube.com/watch']",
            ):
                current = self._bridge_command(
                    "youtube_query",
                    {"selector": selector},
                    timeout=8,
                )
                if not isinstance(current, dict) or not current.get("found"):
                    continue
                for candidate in (
                    str(current.get("href") or "").strip(),
                    str(current.get("text") or "").strip(),
                    str(current.get("value") or "").strip(),
                ):
                    if re.fullmatch(
                        r"https?://(?:www\.)?(?:youtu\.be/[A-Za-z0-9_-]+|"
                        r"youtube\.com/watch\?[^\s]+)",
                        candidate,
                    ):
                        return candidate

        dialog_url = self.upload_dialog_url()
        if dialog_url:
            return dialog_url
        dom_nodes = self.dom_nodes()
        # The allocated watch URL remains in the upload dialog DOM even when
        # Studio hides that dialog after Save. Hidden nodes are intentionally
        # absent from the accessibility tree, so read their stable attributes
        # before falling back to AX. This is still bound to the exact owned CDP
        # target and cannot select a different video's link.
        for node in reversed(dom_nodes):
            attrs = self._dom_attributes(node)
            for candidate in (
                attrs.get("href", "").strip(),
                attrs.get("value", "").strip(),
            ):
                if re.fullmatch(
                    r"https?://(?:www\.)?(?:youtu\.be/[A-Za-z0-9_-]+|"
                    r"youtube\.com/watch\?[^\s]+)",
                    candidate,
                ):
                    return candidate
        by_backend = {
            int(node["backendNodeId"]): node
            for node in dom_nodes
            if node.get("backendNodeId")
        }
        by_id = {node.get("nodeId"): node for node in dom_nodes if node.get("nodeId")}
        for node in self.nodes():
            if _ax_value(node, "role") != "link":
                continue
            backend_id = int(node.get("backendDOMNodeId") or 0)
            current = by_backend.get(backend_id)
            belongs_to_upload = False
            for _ in range(40):
                if current is None:
                    break
                node_name = str(current.get("nodeName") or "").upper()
                attrs = self._dom_attributes(current)
                element_id = attrs.get("id", "").casefold()
                if "UPLOAD" in node_name or "VIDEO-INFO" in node_name or element_id in {
                    "upload-dialog",
                    "video-info",
                }:
                    belongs_to_upload = True
                    break
                current = by_id.get(current.get("parentId"))
            if not belongs_to_upload:
                continue
            href = self.href(node)
            if "youtu.be/" in href or "youtube.com/watch" in href:
                return href
        return ""

    def youtube_submission_complete(self) -> bool:
        """Return true only after Studio proves the file transfer is over.

        A video link is allocated before the visibility/save mutation is
        committed and the Close button may remain enabled while the browser is
        still sending bytes. Therefore neither the link nor the button can
        turn an upload into a completed checkpoint. The localized progress
        text is checked first; any value below 100% keeps the tab pinned.
        """

        if self.has_background_bridge:
            # Always inspect the live transfer dialog before any generic
            # overlay. Studio can show "Checks complete" (or even leave a
            # stale completion dialog in the DOM) while the browser is still
            # uploading bytes. An explicit percentage below 100 is therefore
            # authoritative and must win over every other marker.
            dialog = self._bridge_command(
                "youtube_query",
                {"selector": "ytcp-uploads-dialog"},
                timeout=8,
            )
            dialog_found = bool(isinstance(dialog, dict) and dialog.get("found"))
            if not dialog_found:
                # The same dialog is absent on an unfinished /video/id/edit
                # page. Its disappearance is never proof that bytes arrived.
                percent, _remaining = self.server_upload_progress()
                return percent is not None and percent >= 100.0
            dialog_text = " ".join(str(dialog.get("text") or "").split())
            percent, _remaining = self._parse_youtube_upload_progress(dialog_text)
            if percent is not None:
                return percent >= 100.0

            transfer_complete_markers = (
                "video upload complete",
                "upload complete",
                "upload completed",
                "tải video lên hoàn tất",
                "tải lên hoàn tất",
                "đã tải lên xong",
                "動画のアップロードが完了",
                "アップロードが完了",
            )
            # Studio replaces the transfer counter with an explicit upload
            # completion message inside the same wizard (observed on DaiTa12).
            # There need not be a separate processing popup or another network
            # feedback response. Read the live owned wizard before looking for
            # that popup; a partial transfer above still vetoes completion.
            if (
                dialog.get("laidOut") is not False
                and any(
                    marker in unicodedata.normalize("NFKC", dialog_text).casefold()
                    for marker in transfer_complete_markers
                )
            ):
                return True

            # In the owned live wizard Studio replaces the byte counter with
            # this terminal processing-delay message. Its dedicated transfer
            # status element is stronger evidence than a generic Content row
            # or copyright check, which can appear before all bytes arrive.
            # A partial percentage above always vetoes this handoff.
            delayed_markers = ("quá trình xử lý bị chậm trễ", "processing delayed")
            if (dialog.get("laidOut") is not False and any(
                    marker in unicodedata.normalize("NFKC", dialog_text).casefold()
                    for marker in delayed_markers)):
                transfer_status = self._bridge_command(
                    "youtube_query",
                    # The extension visits each shadow root independently;
                    # a descendant selector cannot cross the wizard host.
                    {"selector": "ytcp-video-upload-progress"}, timeout=8)
                if (isinstance(transfer_status, dict) and transfer_status.get("found")
                        and transfer_status.get("laidOut") is not False):
                    status_text = unicodedata.normalize("NFKC", str(transfer_status.get("text") or "")).casefold()
                    status_percent, _ = self._parse_youtube_upload_progress(status_text)
                    if status_percent is None and any(marker in status_text for marker in delayed_markers):
                        return True

            # After Done is pressed, Studio may keep the upload wizard behind
            # a foreground processing dialog. Accept only wording that
            # explicitly confirms the upload itself, never the independent
            # copyright/checks or video-processing stages.
            processing_dialog = self._bridge_command(
                "youtube_query",
                {"selector": "ytcp-dialog, tp-yt-paper-dialog"},
                timeout=8,
            )
            processing_text = ""
            if (
                isinstance(processing_dialog, dict)
                and processing_dialog.get("found")
                and processing_dialog.get("laidOut") is not False
            ):
                processing_text = unicodedata.normalize(
                    "NFKC", str(processing_dialog.get("text") or "")
                ).casefold()
            return bool(
                processing_text
                and any(marker in processing_text for marker in transfer_complete_markers)
            )

        for node in self.dom_nodes():
            if str(node.get("nodeName") or "").upper() != "YTCP-UPLOADS-DIALOG":
                continue
            # Do not treat a temporarily hidden/minimized dialog as complete.
            # Without the paired bridge there is no reliable way to prove that
            # its Close button has become enabled, so fail closed and wait.
            return False
        percent, _remaining = self.server_upload_progress()
        return percent is not None and percent >= 100.0

    def youtube_parked_transfer_status_complete(self, video_id: str) -> bool:
        """Corroborate a fresh exact-ID Content probe on the original target.

        Parked Studio custom elements can have no layout box even when their
        current transfer status has advanced to processing. This DOM state is
        not sufficient alone: the caller must also find this exact uploaded
        draft in a fresh Content target and veto any partial server/DOM counter.
        """
        if not video_id or self.cdp is None:
            return False
        document = self.cdp.command("DOM.getDocument", {"depth": -1, "pierce": True})
        root = document.get("root")
        if not isinstance(root, dict):
            return False

        def walk(node):
            if str(node.get("nodeName") or "").upper() in {"SCRIPT", "STYLE"}:
                return
            yield node
            for key in ("children", "shadowRoots"):
                for child in node.get(key, []):
                    if isinstance(child, dict):
                        yield from walk(child)

        dialogs = [node for node in walk(root)
                   if node.get("nodeName") == "YTCP-UPLOADS-DIALOG"
                   and self._dom_attributes(node).get("video-id") == video_id]
        if len(dialogs) != 1:
            return False
        statuses = [node for node in walk(dialogs[0])
                    if node.get("nodeName") == "YTCP-VIDEO-UPLOAD-PROGRESS"]
        if len(statuses) != 1:
            return False
        status_text = " ".join(str(node.get("nodeValue") or "")
                               for node in walk(statuses[0]) if node.get("nodeName") == "#text")
        status_text = " ".join(unicodedata.normalize("NFKC", status_text).casefold().split())
        percent, _ = self._parse_youtube_upload_progress(status_text)
        return percent is None and status_text.startswith((
            "quá trình xử lý bị chậm trễ", "processing delayed",
            "video đã tải lên xong", "video upload complete", "upload complete",
        ))

    def server_upload_progress(self, video_id: str = "") -> tuple[float | None, str]:
        allocated_id = video_id or _youtube_video_id(self.youtube_url())
        if not allocated_id and self.cdp is not None:
            page = urllib.parse.urlsplit(self.page_url())
            match = re.fullmatch(r"/video/([A-Za-z0-9_-]{6,20})/edit", page.path)
            if page.hostname == "studio.youtube.com" and match:
                allocated_id = match.group(1)
        if not allocated_id or self.cdp is None:
            return None, ""
        return self.cdp.upload_feedback(allocated_id)

    def youtube_upload_progress(self) -> tuple[float | None, str]:
        """Read Studio's informational upload percentage and ETA.

        This text is localized, but a parsed value below ``100%`` is an
        authoritative signal that bytes are still being transmitted. A ready
        Close button is accepted only once the transfer text reaches 100% or
        Studio replaces it with a post-upload processing/completion marker.
        """

        if not self.has_background_bridge:
            return None, ""
        dialog = self._bridge_command(
            "youtube_query",
            {"selector": "ytcp-uploads-dialog"},
            timeout=8,
        )
        if not isinstance(dialog, dict) or not dialog.get("found"):
            return None, ""
        text = " ".join(str(dialog.get("text") or "").split())
        if not text:
            return None, ""

        return self._parse_youtube_upload_progress(text)

    @staticmethod
    def _parse_youtube_upload_progress(text: str) -> tuple[float | None, str]:
        """Parse localized upload percentage/ETA from one Studio status string."""

        percent: float | None = None
        percent_patterns = (
            r"(?:đã\s+tải\s+được|đã\s+tải\s+lên|tải\s+lên)\s*"
            r"(\d{1,3}(?:[.,]\d+)?)\s*%",
            r"(?:đã\s+tải|đang\s+tải|tải\s+được)\s*"
            r"(\d{1,3}(?:[.,]\d+)?)\s*%",
            r"(?:uploaded|uploading)\s*(\d{1,3}(?:[.,]\d+)?)\s*%",
            r"(\d{1,3}(?:[.,]\d+)?)\s*%\s*(?:uploaded|uploading)",
            r"(?:アップロード済み|アップロード中)[^\d]{0,16}"
            r"(\d{1,3}(?:[.,]\d+)?)\s*%",
            r"(\d{1,3}(?:[.,]\d+)?)\s*%[^\d]{0,16}"
            r"(?:アップロード済み|アップロード中)",
            # Some Studio locales put the numeric counter before a generic
            # completion word, or omit the verb entirely while the upload
            # dialog is visible. Restrict this fallback to upload/completion
            # wording so an unrelated percentage in the dialog is not read as
            # transfer progress.
            r"(\d{1,3}(?:[.,]\d+)?)\s*%[^\d]{0,24}"
            r"(?:complete|completed|done|hoàn\s*tất|hoàn\s*thành|xong)",
        )
        for pattern in percent_patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match is None:
                continue
            try:
                candidate = float(match.group(1).replace(",", "."))
            except ValueError:
                continue
            if 0.0 <= candidate <= 100.0:
                percent = candidate
                break

        remaining = ""
        remaining_patterns = (
            r"(?:còn)\s*((?:khoảng\s*)?\d+\s*(?:giây|phút|giờ))",
            r"((?:about\s*)?\d+\s*(?:seconds?|minutes?|hours?))\s*"
            r"(?:left|remaining)",
            r"(?:remaining|left)\s*((?:about\s*)?\d+\s*"
            r"(?:seconds?|minutes?|hours?))",
            r"残り\s*((?:約\s*)?\d+\s*(?:秒|分|時間))",
        )
        for pattern in remaining_patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match is not None:
                remaining = " ".join(match.group(1).split())
                break
        return percent, remaining

    def close_completed_upload_dialog(
        self,
        *,
        timeout: float = 30.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Acknowledge Studio's completed upload before navigating elsewhere."""

        if self.has_background_bridge:
            dialog = self._bridge_command(
                "youtube_query",
                {"selector": "ytcp-uploads-dialog"},
                timeout=8,
            )
            if not isinstance(dialog, dict) or not dialog.get("found"):
                return
        if not self.youtube_submission_complete():
            raise YouTubeUploadError(
                "YouTube chưa tải xong tệp nên nút Close vẫn bị khóa; tool không rời trang."
            )

        processing_was_open = False
        if self.has_background_bridge:
            processing_dialog = self._bridge_command(
                "youtube_query",
                {"selector": "ytcp-dialog, tp-yt-paper-dialog"},
                timeout=8,
            )
            processing_was_open = bool(
                isinstance(processing_dialog, dict)
                and processing_dialog.get("found")
                and processing_dialog.get("laidOut") is not False
            )
            if processing_was_open:
                # The foreground processing dialog's live Close control has
                # no stable id in current Studio builds.  Resolve it by its
                # semantic label/text inside the paired extension; this does
                # not use coordinates or focus the user's visible window.
                self._bridge_command(
                    "youtube_close_completed_upload",
                    timeout=10,
                )
            else:
                self._bridge_command(
                    "youtube_click",
                    {
                        "selector": "#close-button",
                        "names": ["close", "đóng", "閉じる"],
                    },
                    timeout=10,
                )
        else:
            close_button = self.wait_dom(
                attributes={"id": "close-button"},
                timeout=timeout,
                require_laid_out=True,
                require_enabled=True,
                cancel_event=cancel_event,
            )
            self.press(close_button)
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if cancel_event is not None:
                YouTubeStudioUploader._cancelled(cancel_event)
            if self.has_background_bridge:
                selector = (
                    "ytcp-dialog, tp-yt-paper-dialog"
                    if processing_was_open
                    else "ytcp-uploads-dialog"
                )
                dialog = self._bridge_command(
                    "youtube_query", {"selector": selector}, timeout=8
                )
                if (
                    not isinstance(dialog, dict)
                    or not dialog.get("found")
                    or dialog.get("laidOut") is False
                ):
                    return
            else:
                dialog_present = any(
                    str(node.get("nodeName") or "").upper() == "YTCP-UPLOADS-DIALOG"
                    for node in self.dom_nodes()
                )
                if not dialog_present:
                    return
            time.sleep(0.25)
        raise YouTubeUploadError(
            "YouTube đã tải xong nhưng hộp upload chưa đóng; tool không chuyển trang để tránh "
            "hộp Leave site."
        )

    def channel_name(self) -> str:
        nodes = self.nodes()
        labels = {
            "kênh của bạn",
            "your channel",
            "あなたのチャンネル",
        }
        for index, node in enumerate(nodes):
            if self._normalized(_ax_value(node, "name")) not in labels:
                continue
            for candidate in nodes[index + 1 : index + 8]:
                name = _ax_value(candidate, "name").strip()
                if name and self._normalized(name) not in labels and name != "•":
                    return name
        return "Kênh trong profile"


_DEEP_HELPER = r"""
const deepAll=(selector)=>{const out=[];const visit=(root)=>{for(const el of root.querySelectorAll(selector)){out.push(el)};for(const el of root.querySelectorAll('*')){if(el.shadowRoot)visit(el.shadowRoot)}};visit(document);return out};
const visible=(el)=>!!(el&&el.getClientRects().length&&!el.disabled&&el.getAttribute('aria-disabled')!=='true');
"""


def _js_click(selectors: tuple[str, ...], texts: tuple[str, ...] = ()) -> str:
    selector_json = json.dumps(selectors, ensure_ascii=False)
    text_json = json.dumps(tuple(text.casefold() for text in texts), ensure_ascii=False)
    return (
        "(()=>{" + _DEEP_HELPER
        + f"const selectors={selector_json};const texts={text_json};let target=null;"
        "for(const s of selectors){target=deepAll(s).find(visible);if(target)break;}"
        "if(!target&&texts.length){target=deepAll('button,ytcp-button,tp-yt-paper-item,tp-yt-paper-radio-button').find(el=>{const t=(el.innerText||el.textContent||'').trim().toLocaleLowerCase();return visible(el)&&texts.some(x=>t===x||t.includes(x))})}"
        "if(!target)return false;target.scrollIntoView({block:'center'});target.click();return true})()"
    )


def _js_focus_textbox(selectors: tuple[str, ...]) -> str:
    selector_json = json.dumps(selectors, ensure_ascii=False)
    return (
        "(()=>{" + _DEEP_HELPER + f"const selectors={selector_json};let target=null;"
        "for(const s of selectors){target=deepAll(s).find(visible);if(target)break;}"
        "if(!target)return false;target.scrollIntoView({block:'center'});target.focus();"
        "if(target.isContentEditable){document.execCommand('selectAll',false,null);document.execCommand('delete',false,null)}else{target.value='';target.dispatchEvent(new Event('input',{bubbles:true}))}return true})()"
    )


@contextmanager
def _prepared_youtube_upload_tab(
    client: DonutMcpClient,
    profile: DonutProfile,
    debug: dict[str, Any],
    report: ProgressCallback,
    cancel: threading.Event,
):
    """Yield an upload dialog in a new target owned only by automation.

    Fresh uploads never navigate or click an existing user tab. The dedicated
    target is closed after success or any failure before file attachment. Once
    attachment starts, uncertain/cancelled tabs remain available for the
    checkpoint recovery path.  After the exact video is proven in Studio's
    Content list, the final metadata phase may explicitly hand this target off
    to a fresh edit target; that handoff is also safe to recover if metadata
    editing later fails.
    """

    YouTubeStudioUploader._cancelled(cancel)
    report(5, f"Đang tự kích hoạt Browser Bridge trong profile {profile.name}…")
    browser_bridge = _ensure_youtube_browser_bridge(
        client,
        profile.profile_id,
    )
    report(6, f"Đang mở tab upload riêng trong profile {profile.name}…")
    ws_url = _mcp_open_minimized_cdp_window_ws_url(
        client,
        profile.profile_id,
        debug,
        "about:blank",
    )
    target_id = _cdp_page_target_id(ws_url)
    browser_bridge, bridge_tab_id = _browser_extension_tab_for_target(
        client,
        profile.profile_id,
        target_id,
        bridge=browser_bridge,
    )
    session = _CdpSession(ws_url)
    session_open = False
    controller: _SemanticCdpController | None = None
    completed = False
    try:
        cdp = session.__enter__()
        session_open = True
        controller = _SemanticCdpController(
            cdp,
            browser_bridge=browser_bridge,
            bridge_profile_id=profile.profile_id,
            bridge_tab_id=bridge_tab_id,
            automation_target_id=target_id,
            window_state=lambda state: _mcp_set_cdp_window_state(
                client,
                profile.profile_id,
                target_id,
                state,
            ),
        )
        controller.enable()
        report(7, "Đang chuẩn bị đúng tab upload riêng của tool…")
        controller.navigate_and_wait(
            YOUTUBE_STUDIO_URL,
            timeout=90,
            cancel_event=cancel,
        )
        channel_id = controller.wait_channel_id(
            timeout=90,
            cancel_event=cancel,
        )
        channel_name = controller.channel_name() or profile.name
        report(17, "Đang mở luồng tải video nền của đúng kênh…")
        # This page is the active tab of its own minimized window, so Studio
        # materializes the real dialog without touching the user's window.
        controller.navigate_and_wait(
            (
                f"https://studio.youtube.com/channel/{channel_id}/"
                "videos/upload?d=ud"
            ),
            timeout=90,
            cancel_event=cancel,
        )
        controller.wait_file_input(
            "video",
            timeout=60,
            cancel_event=cancel,
        )
        controller.wait_laid_out_dom(
            attributes={"id": "dialog"},
            node_names=("YTCP-DIALOG",),
            timeout=60,
            cancel_event=cancel,
        )
        # A fully minimized Chromium window drops Studio's editor layout after
        # file attachment. Park the dedicated automation window off-screen so
        # title/description/tags stay writable without covering the user.
        _mcp_set_cdp_window_state(
            client,
            profile.profile_id,
            target_id,
            "parked",
        )
        yield controller, channel_name
        completed = True
    finally:
        if (
            controller is not None
            and controller.upload_started
            and not completed
            and not controller.upload_target_handed_off
            and controller.window_state is not None
        ):
            # An uncertain transfer must remain recoverable, but it should not
            # leave a visible YouTube window over the user's work.
            try:
                controller.window_state("parked")
            except Exception:
                logger.warning(
                    "youtube_upload_uncertain_park_failed profile_suffix=%s target_suffix=%s",
                    profile.profile_id[-6:],
                    target_id[-8:],
                )
        if session_open:
            session.__exit__(None, None, None)
        should_close = (
            completed
            or controller is None
            or not controller.upload_started
            or controller.upload_target_handed_off
        )
        if should_close:
            closed_by_bridge = False
            try:
                closed = browser_bridge.command(
                    profile.profile_id,
                    "close_tab",
                    {"tab_id": bridge_tab_id},
                    timeout=8,
                )
                closed_by_bridge = bool(
                    isinstance(closed, dict) and closed.get("closed")
                )
            except Exception:
                # Cleanup must never turn an already confirmed upload into an
                # error. The MCP target close below is the independent fallback.
                pass
            if not closed_by_bridge:
                closed_by_cdp = _mcp_close_cdp_page_target(
                    client,
                    profile.profile_id,
                    debug,
                    target_id,
                )
                if not closed_by_cdp:
                    logger.warning(
                        "youtube_upload_tab_cleanup_not_confirmed "
                        "profile_suffix=%s target_suffix=%s",
                        profile.profile_id[-6:],
                        target_id[-8:],
                    )


class YouTubeStudioUploader:
    def __init__(
        self,
        coordinator: ProfileUploadCoordinator | None = None,
        *,
        registry_path: Path | None = None,
    ) -> None:
        self.coordinator = coordinator or ProfileUploadCoordinator()
        self.registry_path = registry_path or app_data_dir() / "youtube_upload_registry.json"
        self._video_hash_cache: dict[tuple[str, int, int], str] = {}

    @staticmethod
    def _checkpoint_path(workspace: Workspace) -> Path:
        return workspace.state_dir / "youtube_upload.json"

    def _request_identity(
        self, request: YouTubeUploadRequest, sources: WorkspaceUploadSources
    ) -> str:
        try:
            stat = sources.video_path.stat()
        except OSError as exc:
            raise YouTubeUploadError("Không đọc được video để kiểm tra chống đăng trùng.") from exc
        cache_key = (
            str(sources.video_path.resolve(strict=False)),
            int(stat.st_size),
            int(stat.st_mtime_ns ^ stat.st_ctime_ns),
        )
        video_sha256 = self._video_hash_cache.get(cache_key)
        if not video_sha256:
            digest = hashlib.sha256()
            try:
                with sources.video_path.open("rb") as stream:
                    while chunk := stream.read(8 * 1024 * 1024):
                        digest.update(chunk)
            except OSError as exc:
                raise YouTubeUploadError(
                    "Không đọc được toàn bộ video để tạo khóa chống đăng trùng."
                ) from exc
            video_sha256 = digest.hexdigest()
            self._video_hash_cache[cache_key] = video_sha256
        return hashlib.sha256(
            (
                "docurandom-youtube-v2\n"
                + request.profile_id
                + "\n"
                + video_sha256
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _legacy_request_identity(
        request: YouTubeUploadRequest, sources: WorkspaceUploadSources
    ) -> str:
        return hashlib.sha256(
            (
                str(sources.video_path.resolve(strict=False))
                + "\n"
                + str(sources.video_path.stat().st_size)
                + "\n"
                + request.profile_id
                + "\n"
                + sources.metadata.title
            ).encode("utf-8")
        ).hexdigest()

    def _registry_load(self) -> dict[str, object]:
        if not self.registry_path.exists():
            return {"version": 1, "uploads": {}}
        try:
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise YouTubeUploadError(
                "Sổ chống đăng trùng YouTube không đọc được. Tool từ chối upload để bảo "
                "vệ kênh; không xóa hoặc ghi đè sổ này khi chưa kiểm tra."
            ) from exc
        uploads = value.get("uploads") if isinstance(value, dict) else None
        if not isinstance(uploads, dict):
            raise YouTubeUploadError(
                "Sổ chống đăng trùng YouTube sai cấu trúc. Tool từ chối upload để bảo vệ kênh."
            )
        return {"version": 1, "uploads": uploads}

    @contextmanager
    def _registry_guard(self):
        """Serialize the global upload registry across threads and processes."""

        with _YOUTUBE_REGISTRY_LOCK:
            lock_path = self.registry_path.with_suffix(self.registry_path.suffix + ".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as stream:
                if os.name != "nt":
                    yield
                    return
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                deadline = time.monotonic() + 10.0
                while True:
                    try:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if time.monotonic() >= deadline:
                            raise YouTubeUploadError(
                                "Sổ chống đăng trùng YouTube đang bị một tiến trình khác khóa. "
                                "Tool từ chối bắt đầu lượt upload mới."
                            ) from exc
                        time.sleep(0.1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def _registry_get(self, identity: str) -> dict[str, object]:
        with self._registry_guard():
            uploads = self._registry_load().get("uploads")
            item = uploads.get(identity) if isinstance(uploads, dict) else None
            return dict(item) if isinstance(item, dict) else {}

    def authorize_stalled_upload_replacement(
        self, request: YouTubeUploadRequest, sources: WorkspaceUploadSources, *,
        expected_attempt_id: str, probe: dict[str, object], operator_approved: bool = False,
    ) -> dict[str, object]:
        """Reserve one explicitly approved Unlisted replacement of a proven partial draft.

        This method never sends a file. The fresh read-only probe must identify
        the exact old video, prove partial server bytes and no usable Resume
        control, and confirm that its owned browser process has been closed.
        Old checkpoint/claim evidence is archived before changing the lease.
        """
        if operator_approved is not True:
            raise YouTubeUploadUncertain("Chưa có yêu cầu cho phép lượt thay thế; giữ bản nháp cũ.")
        if request.privacy not in {"PUBLIC", "UNLISTED"}:
            raise YouTubeUploadUncertain("Lượt thay thế phải được chọn Unlisted hoặc chờ lịch Public.")
        self._video_hash_cache.clear()
        identity = self._request_identity(request, sources)
        try:
            observed = datetime.fromisoformat(str(probe["observed_at"]))
            age = (datetime.now(timezone.utc) - observed).total_seconds()
            percent = float(probe["server_percent"])
        except (KeyError, TypeError, ValueError) as exc:
            raise YouTubeUploadUncertain("Thiếu bằng chứng mới của bản nháp bị kẹt.") from exc
        if (observed.tzinfo is None or not 0 <= age <= 120 or not math.isfinite(percent)
                or not 0 <= percent < 100 or probe.get("exact_row_found") is not True
                or probe.get("resume_unavailable") is not True
                or probe.get("owned_profile_closed") is not True):
            raise YouTubeUploadUncertain("Bằng chứng chưa đủ để thay bản nháp; không upload lại.")
        with self._registry_guard():
            registry = self._registry_load()
            uploads = registry["uploads"]
            record = uploads.get(identity)
            video_id = _youtube_video_id(str(record.get("youtube_url") or "")) if isinstance(record, dict) else ""
            if (not isinstance(record, dict) or not expected_attempt_id
                    or record.get("attempt_id") != expected_attempt_id
                    or record.get("workspace_id") != request.workspace.workspace_id
                    or record.get("profile_id") != request.profile_id
                    or record.get("transfer_complete") is not False
                    or record.get("transfer_recovery_stalled") is not True
                    or not video_id or probe.get("video_id") != video_id
                    or record.get("replacement_history")
                    or str(record.get("status") or "").casefold() not in {"uploading", "uncertain"}):
                raise YouTubeUploadUncertain("Khóa bản nháp đã đổi hoặc đã dùng lượt thay thế; không tạo thêm lượt.")
            try:
                progressed = datetime.fromisoformat(str(record["transfer_feedback_progress_at"]))
                unchanged = abs(float(record["transfer_feedback_percent"]) - percent) < 0.000001
                stalled_for = (datetime.now(timezone.utc) - progressed).total_seconds()
            except (KeyError, TypeError, ValueError) as exc:
                raise YouTubeUploadUncertain("Không xác minh được lịch sử tiến độ bản nháp.") from exc
            if not unchanged or not _YOUTUBE_RECOVERY_STALL_SECONDS <= stalled_for:
                raise YouTubeUploadUncertain("Video đang tiến triển; giữ lượt đang tải, không thay thế.")
            if record.get("title") and sources.metadata.title != record["title"]:
                raise YouTubeUploadUncertain("Gói nội dung đã đổi sang video khác; giữ bản nháp cũ, không thay thế.")
            checkpoint_path = self._checkpoint_path(request.workspace)
            try:
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise YouTubeUploadUncertain("Checkpoint cũ không đọc được; không thay thế.") from exc
            if (not isinstance(checkpoint, dict) or checkpoint.get("identity") != identity
                    or checkpoint.get("attempt_id") != expected_attempt_id
                    or str(checkpoint.get("status") or "").casefold() not in {"attaching", "uploading", "uncertain"}
                    or checkpoint.get("transfer_complete") is True
                    or _youtube_video_id(str(checkpoint.get("youtube_url") or "")) != video_id):
                raise YouTubeUploadUncertain("Checkpoint không khớp bản nháp bị kẹt; không thay thế.")
            # The attempt is a generated UUID, never a caller-supplied path.
            if not re.fullmatch(r"[a-fA-F0-9]{32}", expected_attempt_id):
                raise YouTubeUploadUncertain("Mã lượt cũ không hợp lệ để lưu bản sao.")
            archive = request.workspace.state_dir / "upload-replacements" / expected_attempt_id
            archive.mkdir(parents=True, exist_ok=False)
            atomic_write_json(archive / "youtube_upload.json", checkpoint)
            atomic_write_json(archive / "registry-claim.json", record)
            atomic_write_json(archive / "read-only-probe.json", probe)
            old = {**record, "public_blocked": True, "superseded_at": _now(),
                   "superseded_reason": "operator-approved stalled incomplete upload"}
            ready = {
                "status": "replacement_ready", "attempt_id": uuid.uuid4().hex,
                "workspace_id": request.workspace.workspace_id, "profile_id": request.profile_id,
                "video_path": str(sources.video_path), "youtube_url": "", "updated_at": _now(),
                "replacement_history": [old], "replacement_budget_used": 1,
                "replacement_checkpoint": str(archive / "youtube_upload.json"),
                "replacement_privacy": "UNLISTED",
                "replacement_final_privacy": request.privacy,
            }
            # Persist the reserved budget first. A crash before the workspace
            # write can repair this checkpoint from the archived exact lease.
            uploads[identity] = ready
            atomic_write_json(self.registry_path, registry)
            self._write_replacement_ready_checkpoint(request, sources, identity, ready)
            return ready

    def _write_replacement_ready_checkpoint(self, request, sources, identity, ready):
        atomic_write_json(self._checkpoint_path(request.workspace), {
            "status": "replacement_ready", "progress": 0, "identity": identity,
            "profile_id": request.profile_id, "workspace_id": request.workspace.workspace_id,
            "video_path": str(sources.video_path), "package_path": str(sources.package_path),
            "attempt_id": ready["attempt_id"], "youtube_url": "", "updated_at": _now(),
            "replacement_ready": True, "transfer_complete": False,
            "detail": "Đã sao lưu bản nháp bị kẹt; cho phép đúng một lượt thay thế Unlisted.",
        })

    def _assert_public_video_not_superseded(self, profile_id: str, video_id: str) -> None:
        with self._registry_guard():
            for record in self._registry_load()["uploads"].values():
                if not isinstance(record, dict) or record.get("profile_id") != profile_id:
                    continue
                for old in record.get("replacement_history", []):
                    if (isinstance(old, dict) and old.get("public_blocked") is True
                            and _youtube_video_id(str(old.get("youtube_url") or "")) == video_id):
                        if (record.get("replacement_resumed_video_id") == video_id
                                and record.get("transfer_complete") is True
                                and str(record.get("status") or "") in {"complete", "deferred_public"}
                                and _confirmed_privacy_from_record(record) in {"UNLISTED", "PUBLIC"}
                                and _youtube_video_id(str(record.get("youtube_url") or "")) == video_id):
                            continue
                        raise YouTubeUploadError("Bản nháp cũ đã được thay thế và bị khóa Public; không đăng trùng.")

    def _wait_for_existing_file_transfers(
        self,
        identity: str,
        cancel: threading.Event,
        report: ProgressCallback,
        *,
        recovery_request: YouTubeUploadRequest | None = None,
        recovery_sources: WorkspaceUploadSources | None = None,
    ) -> None:
        # Browser file tabs outlive Studio. The new coordinator starts empty,
        # so queue priority alone cannot protect a transfer whose recovery
        # worker has not entered the queue yet. Wait outside the lease so that
        # worker can take control of its original tab without a deadlock.
        reported_blockers: tuple[str, ...] | None = None
        next_inspection = 0.0
        while True:
            self._cancelled(cancel)
            if (recovery_request is not None and recovery_sources is not None
                    and time.monotonic() >= next_inspection):
                self._inspect_waiting_recovery_profile(
                    recovery_request, recovery_sources, identity, cancel, report)
                next_inspection = time.monotonic() + 60.0
            with self._registry_guard():
                uploads = self._registry_load()["uploads"]
                blockers = tuple(sorted(
                    key for key, item in uploads.items()
                    if key != identity and isinstance(item, dict)
                    and str(item.get("status") or "").casefold()
                    in {"attaching", "uploading", "submitting"}
                    and bool(str(item.get("attempt_id") or ""))
                    and item.get("transfer_complete") is not True
                    and item.get("transfer_requires_content_resume") is not True
                    and self._upload_claim_matches_current_checkpoint(key, item)
                ))
            if not blockers:
                return
            if blockers != reported_blockers:
                report(0, "Đang chờ nối lượt truyền tệp đã có · không mở lượt mới")
                reported_blockers = blockers
            cancel.wait(1.0)

    def _inspect_waiting_recovery_profile(self, request, sources, identity, cancel, report):
        """Inspect an owned uncertain attempt while another profile transfers bytes.

        This lane reads the exact server counter or target catalog only. It
        never opens/navigates a tab, attaches a file or changes metadata.
        """
        record = self._registry_get(identity)
        attempt_id = str(record.get("attempt_id") or "")
        if (not attempt_id or str(record.get("status") or "").casefold() != "uncertain"
                or record.get("profile_started_by_workflow") is not True
                or record.get("profile_id") != request.profile_id
                or record.get("transfer_complete") is True
                or not self._upload_claim_matches_current_checkpoint(identity, record)):
            return
        with self.coordinator.acquire(request.profile_id, cancel, report,
                                      job_kind="metadata-recovery", priority=50,
                                      preempt_lower=False):
            record = self._registry_get(identity)
            if (str(record.get("attempt_id") or "") != attempt_id
                    or str(record.get("status") or "").casefold() != "uncertain"
                    or record.get("profile_started_by_workflow") is not True
                    or record.get("profile_id") != request.profile_id
                    or record.get("transfer_complete") is True
                    or not self._upload_claim_matches_current_checkpoint(identity, record)):
                return
            failure = None
            try:
                with DonutMcpClient(request.endpoint, timeout=15) as client:
                    profile = client.get_profile(request.profile_id)
                    video_id = _youtube_video_id(str(record.get("youtube_url") or ""))
                    targets = []
                    if profile.process_id:
                        debug = client.get_debug_connection(request.profile_id)
                        with urllib.request.urlopen(str(debug.get("targets_url") or ""), timeout=5) as response:
                            targets = json.load(response)
                    expected_target = str(record.get("target_id") or "")
                    if (not video_id and expected_target and not any(
                            t.get("type") == "page" and t.get("id") == expected_target for t in targets)):
                        self._registry_update_attempt(identity, attempt_id, {"recovery_requires_manual": True})
                        failure = YouTubeUploadRecoveryPaused(
                            "Tab upload cũ đã mất và chưa có mã video để đối chiếu. "
                            "Đã dừng tự mở profile, giữ nguyên lượt upload để kiểm tra bản nháp.")
                    elif video_id and profile.process_id:
                        for target in targets:
                            if (target.get("type") != "page"
                                    or "studio.youtube.com" not in str(target.get("url") or "")
                                    or not target.get("webSocketDebuggerUrl")):
                                continue
                            with _CdpSession(target["webSocketDebuggerUrl"]) as cdp:
                                percent, _ = cdp.upload_feedback(video_id, timeout=5)
                            if percent is not None and math.isfinite(percent) and 0 <= percent < 100:
                                if self._record_partial_transfer_feedback(identity, attempt_id, percent):
                                    failure = YouTubeUploadTransferStalled(
                                        "Tiến độ truyền đúng mã video không tăng trong 15 phút. "
                                        "Tool thu hồi phiên treo, giữ checkpoint để nối đúng bản nháp.")
                                break
            except YouTubeUploadCancelled:
                raise
            except Exception as exc:
                logger.warning("youtube_waiting_recovery_inspection_failed profile_suffix=%s error=%s",
                               request.profile_id[-6:], type(exc).__name__)
                return  # An unavailable diagnostic never authorizes stopping a profile.
            if failure is not None:
                self._park_failed_recovery_profile(request, sources, failure, report)
                raise failure

    def _upload_claim_matches_current_checkpoint(
        self, identity: str, record: dict[str, object]
    ) -> bool:
        # The duplicate registry intentionally keeps historical claims forever.
        # Only a claim still referenced by the workspace's active checkpoint
        # can reserve the transfer lane after a restart.
        root: Path | None = None
        workspace_registry = self.registry_path.parent / "workspaces.json"
        try:
            # Windows can briefly deny a read while the UI atomically replaces
            # its workspace registry. Keep the active-upload guard intact and
            # retry that sharing violation instead of failing the queued job.
            for read_attempt in range(5):
                try:
                    registry_text = workspace_registry.read_text(encoding="utf-8-sig")
                    break
                except OSError as exc:
                    sharing_violation = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
                    if not sharing_violation or read_attempt == 4:
                        raise
                    time.sleep(0.05 * (2 ** read_attempt))
            workspaces = json.loads(registry_text)
        except FileNotFoundError:
            workspaces = {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise YouTubeUploadError("Không đọc được danh sách workspace để nối lượt truyền tệp.") from exc
        for item in workspaces.get("workspaces", ()):
            if str(item.get("workspace_id") or "") == str(record.get("workspace_id") or ""):
                if item.get("root_dir"):
                    root = Path(str(item["root_dir"]))
                break
        if root is None:
            video_path = str(record.get("video_path") or "")
            if not video_path:
                return False
            root = Path(video_path).parent.parent
        try:
            checkpoint_name = (
                "youtube_archived_recovery.json"
                if record.get("archived_upload_recovery") is True
                else "youtube_upload.json"
            )
            checkpoint = json.loads((root / ".docurandom" / checkpoint_name).read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return False
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise YouTubeUploadError("Không đọc được checkpoint để xác minh lượt truyền tệp đang dở.") from exc
        return bool(
            isinstance(checkpoint, dict)
            and checkpoint.get("identity") == identity
            and checkpoint.get("attempt_id") == record.get("attempt_id")
        )

    def latest_workspace_upload_state(
        self,
        workspace_id: str,
    ) -> dict[str, str] | None:
        """Return the newest durable upload claim for one workspace.

        This is a read-only recovery aid for schedule state left behind after
        an upload checkpoint was archived.  The global registry is protected
        by the same cross-process lock used by upload claims.
        """

        normalized_workspace_id = str(workspace_id or "").strip()
        if not normalized_workspace_id:
            return None
        with self._registry_guard():
            uploads = self._registry_load().get("uploads")
            if not isinstance(uploads, dict):
                return None
            matching = [
                (str(identity), record)
                for identity, record in uploads.items()
                if isinstance(record, dict)
                and str(record.get("workspace_id") or "").strip()
                == normalized_workspace_id
            ]
        if not matching:
            return None
        identity, record = max(
            matching,
            key=lambda item: (
                str(item[1].get("updated_at") or ""),
                str(item[1].get("started_at") or ""),
            ),
        )
        return {
            "identity": identity,
            "status": str(record.get("status") or "").strip().casefold(),
            "updated_at": str(record.get("updated_at") or "").strip(),
            "youtube_url": str(record.get("youtube_url") or "").strip(),
        }

    def _registry_set(self, identity: str, value: dict[str, object]) -> None:
        with self._registry_guard():
            registry = self._registry_load()
            uploads = registry.get("uploads")
            if not isinstance(uploads, dict):
                uploads = {}
                registry["uploads"] = uploads
            uploads[identity] = dict(value)
            atomic_write_json(self.registry_path, registry)

    def _registry_claim(
        self,
        identity: str,
        value: dict[str, object],
    ) -> dict[str, object]:
        """Create the irreversible upload claim iff no claim already exists."""

        with self._registry_guard():
            registry = self._registry_load()
            uploads = registry.get("uploads")
            if not isinstance(uploads, dict):
                raise YouTubeUploadError("Sổ chống đăng trùng YouTube sai cấu trúc.")
            existing = uploads.get(identity)
            if isinstance(existing, dict):
                if (existing.get("status") == "replacement_ready"
                        and existing.get("replacement_budget_used") == 1
                        and len(existing.get("replacement_history", [])) == 1
                        and existing.get("workspace_id") == value.get("workspace_id")
                        and existing.get("profile_id") == value.get("profile_id")
                        and str(value.get("privacy") or "").upper() == "UNLISTED"):
                    claimed = {**value, **{key: existing[key] for key in (
                        "replacement_history", "replacement_budget_used", "replacement_checkpoint", "replacement_privacy", "replacement_final_privacy")}}
                    uploads[identity] = claimed
                    atomic_write_json(self.registry_path, registry)
                    return {}
                return dict(existing)
            uploads[identity] = dict(value)
            atomic_write_json(self.registry_path, registry)
            return {}

    def _registry_update_attempt(
        self,
        identity: str,
        attempt_id: str,
        changes: dict[str, object],
    ) -> dict[str, object]:
        with self._registry_guard():
            registry = self._registry_load()
            uploads = registry.get("uploads")
            existing = uploads.get(identity) if isinstance(uploads, dict) else None
            if not isinstance(existing, dict) or str(existing.get("attempt_id") or "") != attempt_id:
                raise YouTubeUploadUncertain(
                    "Khóa chống đăng trùng đã đổi chủ sở hữu. Tool dừng ngay và không thực "
                    "hiện thêm thao tác YouTube."
                )
            updated = dict(existing)
            updated.update(changes)
            video_id = _youtube_video_id(str(updated.get("youtube_url") or ""))
            if (updated.get("transfer_complete") is True and video_id
                    and str(updated.get("status") or "") in {"complete", "deferred_public"}
                    and _confirmed_privacy_from_record(updated) in {"UNLISTED", "PUBLIC"}
                    and any(isinstance(old, dict) and _youtube_video_id(str(old.get("youtube_url") or "")) == video_id
                            for old in updated.get("replacement_history", []))):
                updated["replacement_resumed_video_id"] = video_id
            uploads[identity] = updated
            atomic_write_json(self.registry_path, registry)
            return updated

    def _checkpoint(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        *,
        status: str,
        progress: float,
        detail: str,
        extra: dict[str, object] | None = None,
    ) -> None:
        identity = self._request_identity(request, sources)
        effective_privacy = self._effective_privacy(request)
        value: dict[str, object] = {
            "version": 1,
            "identity": identity,
            "workspace_id": request.workspace.workspace_id,
            "profile_id": request.profile_id,
            "video_path": str(sources.video_path),
            "package_path": str(sources.package_path),
            "thumbnail_path": (
                str(sources.thumbnail_path) if sources.thumbnail_path is not None else ""
            ),
            "title": sources.metadata.title,
            "description": sources.metadata.description,
            "seo_tags": list(sources.metadata.tags),
            "made_for_kids": False,
            "privacy": effective_privacy,
            "final_privacy": request.privacy,
            "deferred_public": bool(
                request.hold_public_until_next_morning
                and request.privacy == "PUBLIC"
            ),
            "contains_synthetic_media": request.contains_synthetic_media,
            "status": status,
            "progress": max(0.0, min(100.0, progress)),
            "detail": detail,
            "updated_at": _now(),
        }
        if extra:
            value.update(extra)
        atomic_write_json(self._checkpoint_path(request.workspace), value)
        logger.debug(
            "youtube_checkpoint workspace_id=%s profile_suffix=%s status=%s progress=%.1f detail=%s",
            request.workspace.workspace_id,
            request.profile_id[-6:],
            status,
            progress,
            detail,
        )

    @staticmethod
    def _cancelled(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise YouTubeUploadCancelled("Đã hủy upload YouTube; checkpoint được giữ.")

    @staticmethod
    def _close_profile_started_by_workflow(
        client: DonutMcpClient,
        profile_id: str,
    ) -> None:
        """Release a Wayfern session only when this workflow started it."""

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
                    return
                time.sleep(0.25)
            logger.warning(
                "youtube_owned_profile_close_not_confirmed profile_suffix=%s",
                str(profile_id)[-6:],
            )
        except Exception as exc:
            logger.warning(
                "youtube_owned_profile_close_failed profile_suffix=%s error=%s",
                str(profile_id)[-6:],
                type(exc).__name__,
            )

    def _close_completed_recovery_profile(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        ownership: dict[str, bool],
    ) -> None:
        """Free a recovery-owned Wayfern slot after the original upload is proven."""

        try:
            identity = self._request_identity(request, sources)
            record = self._registry_get(identity)
            if not ownership.get("opened") and record.get("profile_started_by_workflow") is not True:
                return
            status = str(record.get("status") or "").casefold()
            if status not in {"complete", "deferred_public"}:
                return
            with DonutMcpClient(request.endpoint, timeout=40) as client:
                self._close_profile_started_by_workflow(client, request.profile_id)
        except Exception as exc:
            logger.warning(
                "youtube_recovery_owned_profile_close_failed profile_suffix=%s error=%s",
                request.profile_id[-6:],
                type(exc).__name__,
            )

    def _record_partial_transfer_feedback(self, identity, attempt_id, percent):
        """Persist progress across retries, which otherwise reset the watchdog."""
        record = self._registry_get(identity)
        now = datetime.now(timezone.utc)
        try:
            previous = float(record.get("transfer_feedback_percent"))
            progressed_at = datetime.fromisoformat(str(record["transfer_feedback_progress_at"]))
            observed_at = datetime.fromisoformat(str(record["transfer_feedback_observed_at"]))
            if progressed_at.tzinfo is None or observed_at.tzinfo is None:
                raise ValueError("progress clock lacks timezone")
            stagnant = abs(percent - previous) < 0.000001
        except (KeyError, TypeError, ValueError):
            stagnant = False
            progressed_at = observed_at = now
        if not stagnant or progressed_at > now or observed_at > now:
            progressed_at = now
            stagnant = False
        stalled = bool(stagnant and observed_at <= now
                       and (now - progressed_at).total_seconds() >= _YOUTUBE_RECOVERY_STALL_SECONDS)
        count = int(record.get("transfer_stall_recovery_count") or 0) if stagnant else 0
        if stalled and (now - observed_at).total_seconds() >= 30:
            count += 1
        changes = {
            "transfer_feedback_percent": percent,
            "transfer_feedback_progress_at": progressed_at.isoformat(),
            "transfer_feedback_observed_at": now.isoformat(),
            "transfer_recovery_stalled": stalled,
            "transfer_stall_recovery_count": count,
        }
        if stalled and count >= _YOUTUBE_MAX_STALLED_RECOVERY_PROBES:
            changes.update({"recovery_requires_manual": True,
                            "recovery_stop_reason": "Tiến độ truyền file không đổi qua 3 lượt dò; đã dừng tự mở profile. Giữ bản nháp và file gốc để nối file hoặc xử lý một lượt thay thế có kiểm soát."})
        self._registry_update_attempt(identity, attempt_id, changes)
        return stalled

    def _park_failed_recovery_profile(self, request, sources, error, report):
        """Release only our profile at a proven safe recovery boundary."""
        if not isinstance(error, (YouTubeUploadRecoveryPaused, YouTubeUploadTransferStalled)):
            return
        identity = self._request_identity(request, sources)
        record = self._registry_get(identity)
        if (record.get("profile_started_by_workflow") is not True
                or record.get("profile_id") != request.profile_id
                or not self._upload_claim_matches_current_checkpoint(identity, record)):
            return
        attempt_id = str(record.get("attempt_id") or "")
        if not attempt_id or record.get("transfer_complete") is True:
            return
        try:
            with DonutMcpClient(request.endpoint, timeout=40) as client:
                profile = client.get_profile(request.profile_id)
                if not profile.process_id:
                    return
                if isinstance(error, YouTubeUploadRecoveryPaused):
                    # The old target is gone. Leave any other editor/upload or
                    # user page alone; only an empty/dashboard profile is idle.
                    debug = client.get_debug_connection(request.profile_id)
                    # Use the normal CDP target catalog, independent of bridge
                    # tab labels and without executing page JavaScript.
                    target_url = str(debug.get("targets_url") or "")
                    with urllib.request.urlopen(target_url, timeout=5) as response:
                        targets = json.load(response)
                    allowed = re.compile(r"^https://studio\.youtube\.com/channel/UC[A-Za-z0-9_-]+/?(?:\?.*)?$")
                    if any(t.get("type") == "page" and t.get("url") != "about:blank"
                           and not allowed.fullmatch(str(t.get("url") or "")) for t in targets):
                        return
                else:
                    observed = datetime.fromisoformat(str(record.get("transfer_feedback_observed_at")))
                    age = (datetime.now(timezone.utc) - observed).total_seconds()
                    if (record.get("transfer_recovery_stalled") is not True
                            or not 0 <= age <= 60
                            or not _youtube_video_id(str(record.get("youtube_url") or ""))):
                        return
                # Write the resume boundary BEFORE stopping the browser. The
                # exact identity/attempt/URL survive even if Studio exits here.
                changes = {"status": "uncertain", "updated_at": _now(),
                           "recovery_profile_parked_at": _now()}
                if isinstance(error, YouTubeUploadTransferStalled):
                    changes["transfer_requires_content_resume"] = True
                self._registry_update_attempt(identity, attempt_id, changes)
                current = self._registry_get(identity)
                if (current.get("attempt_id") != attempt_id
                        or current.get("transfer_complete") is True):
                    return
                self._close_profile_started_by_workflow(client, request.profile_id)
                report(96, "Đã giữ checkpoint và nhường phiên Donut bị kẹt; chỉ nối bản nháp cũ, không tạo upload mới")
        except Exception as exc:
            logger.warning("youtube_stalled_profile_release_failed profile_suffix=%s error=%s",
                           request.profile_id[-6:], type(exc).__name__)

    def upload_recovery_requires_manual(self, identity: str, attempt_id: str) -> bool:
        record = self._registry_get(identity)
        return bool(attempt_id and record.get("attempt_id") == attempt_id
                    and record.get("recovery_requires_manual") is True)

    def upload_recovery_stop_reason(self, identity: str, attempt_id: str) -> str:
        record = self._registry_get(identity)
        if record.get("attempt_id") != attempt_id:
            return ""
        return str(record.get("recovery_stop_reason") or "Tab upload đã mất · giữ checkpoint, dừng tự mở profile")

    @staticmethod
    def _wait_js(
        cdp: _CdpSession,
        expression: str,
        cancel_event: threading.Event,
        *,
        timeout: float = 30.0,
    ) -> object:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            YouTubeStudioUploader._cancelled(cancel_event)
            value = cdp.evaluate(expression)
            if value:
                return value
            time.sleep(0.5)
        raise YouTubeUploadError("YouTube Studio không hiện đúng điều khiển cần thiết.")

    @staticmethod
    def _set_file(cdp: _CdpSession, path: Path, accept_hint: str) -> None:
        expression = (
            "(()=>{" + _DEEP_HELPER
            + f"const hint={json.dumps(accept_hint)};const all=deepAll('input[type=file]');"
            "return all.find(el=>(el.accept||'').toLowerCase().includes(hint))||all[0]||null})()"
        )

        result = cdp.command(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": False},
        )
        remote = result.get("result") if isinstance(result, dict) else None
        object_id = str(remote.get("objectId") or "") if isinstance(remote, dict) else ""
        if not object_id:
            raise YouTubeUploadError(f"Không tìm thấy ô chọn file {accept_hint} trên YouTube Studio.")
        cdp.command(
            "DOM.setFileInputFiles",
            {"files": [str(path.resolve())], "objectId": object_id},
        )

    @staticmethod
    def _effective_privacy(request: YouTubeUploadRequest) -> str:
        """Return the visibility that must be written during this attempt.

        The requested visibility remains in ``final_privacy`` in the
        checkpoint.  This is intentionally separate from the actual Studio
        value so a late-day PUBLIC upload can be repaired safely as UNLISTED
        and promoted later without changing its upload identity.
        """

        privacy = (
            request.privacy
            if request.privacy in YOUTUBE_PRIVACY_OPTIONS.values()
            else "PRIVATE"
        )
        if request.hold_public_until_next_morning and privacy == "PUBLIC":
            return "UNLISTED"
        return privacy

    @classmethod
    def _completion_status(cls, request: YouTubeUploadRequest) -> str:
        return (
            "deferred_public"
            if cls._effective_privacy(request) == "UNLISTED"
            and request.hold_public_until_next_morning
            and request.privacy == "PUBLIC"
            else "complete"
        )

    @staticmethod
    def _set_text(
        cdp: _CdpSession,
        selectors: tuple[str, ...],
        value: str,
    ) -> None:
        focused = cdp.evaluate(_js_focus_textbox(selectors))
        if not focused:
            raise YouTubeUploadError("YouTube Studio không tìm thấy ô tiêu đề/mô tả.")
        cdp.command("Input.insertText", {"text": value})

    def _execute_locked_upload(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        identity: str,
        resume_record: dict[str, object] | None,
        reconcile_record: dict[str, object] | None,
        report: ProgressCallback,
        cancel: threading.Event,
    ) -> YouTubeUploadResult:
        registry_item = self._registry_get(identity)
        registry_status = str(registry_item.get("status") or "").casefold()
        if registry_status == "complete":
            report(100, "Video đã hoàn tất ở lượt trước · không đăng lần hai")
            return YouTubeUploadResult(
                profile_id=request.profile_id,
                profile_name=str(registry_item.get("profile_name") or "Profile đã chọn"),
                channel_name=str(registry_item.get("channel_name") or "Kênh đã upload"),
                video_path=sources.video_path,
                package_path=sources.package_path,
                youtube_url=str(registry_item.get("youtube_url") or ""),
                thumbnail_path=sources.thumbnail_path,
                privacy_status=_confirmed_privacy_from_record(registry_item),
            )
        if resume_record is not None:
            expected_attempt = str(resume_record.get("attempt_id") or "")
            if (
                str(registry_item.get("attempt_id") or "") != expected_attempt
                or registry_status not in {"attaching", "uploading", "uncertain"}
            ):
                raise YouTubeUploadUncertain(
                    "Phiên upload đã gắn file không còn khớp khóa chống trùng. Tool "
                    "dừng và không gắn lại video."
                )
            return self._resume_attached_locked(
                request,
                sources,
                registry_item,
                report,
                cancel,
            )
        if reconcile_record is not None:
            expected_attempt = str(reconcile_record.get("attempt_id") or "")
            if (
                str(registry_item.get("attempt_id") or "") != expected_attempt
                or registry_status
                not in {"uploading", "submitting", "uncertain", "deferred_public"}
            ):
                raise YouTubeUploadUncertain(
                    "Lượt Lưu cần xác minh không còn khớp khóa chống trùng. Tool "
                    "dừng và không bấm hoặc upload lại video."
                )
            return self._reconcile_submitted_locked(
                request,
                sources,
                {**dict(reconcile_record), **dict(registry_item)},
                report,
                cancel,
            )
        if registry_status in {"attaching", "uploading", "submitting", "uncertain"}:
            raise YouTubeUploadUncertain(
                "Một lượt của chính video này đã giữ khóa trong khi chờ profile. "
                "Tool từ chối lượt thứ hai để tránh đăng trùng/spam."
            )
        return self._upload_locked(request, sources, report, cancel)

    def _recover_locked_upload(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        identity: str,
        report: ProgressCallback,
        cancel: threading.Event,
        first_error: Exception,
        *,
        metadata_only: bool = False,
    ) -> YouTubeUploadResult:
        """Recover while retaining the one global upload lane."""

        last_error = first_error
        for delay in _YOUTUBE_RECOVERY_DELAYS:
            if isinstance(last_error, (YouTubeUploadRecoveryPaused, YouTubeUploadTransferStalled)):
                break
            self._cancelled(cancel)
            registered = self._registry_get(identity)
            attempt_id = str(registered.get("attempt_id") or "")
            try:
                current = json.loads(
                    self._checkpoint_path(request.workspace).read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError):
                current = {}
            if not isinstance(current, dict):
                current = {}
            current_status = str(current.get("status") or "").casefold()
            irreversible = bool(
                attempt_id
                and current_status
                in {
                    "attaching",
                    "uploading",
                    "submitting",
                    "uncertain",
                    "deferred_public",
                }
            )
            transient_pre_attach = bool(
                not attempt_id
                and _is_transient_youtube_pre_attach_error(last_error)
            )
            if metadata_only and (not irreversible
                                  or registered.get("transfer_complete") is not True
                                  or not _youtube_video_id(str(registered.get("youtube_url") or ""))):
                break  # This lane must never fall back to attaching a file.
            if not irreversible and not transient_pre_attach:
                break

            if irreversible:
                preserved = {**dict(current), **dict(registered)}
                self._registry_update_attempt(
                    identity,
                    attempt_id,
                    {"status": "uncertain", "updated_at": _now()},
                )
                self._checkpoint(
                    request,
                    sources,
                    status="uncertain",
                    progress=float(current.get("progress") or 24),
                    detail=(
                        "Đã giữ đúng lượt upload/bản nháp sau lỗi; tool sẽ tự nối lại "
                        "và không gắn file lần hai"
                    ),
                    extra={
                        key: preserved[key]
                        for key in (
                            "attempt_id",
                            "profile_name",
                            "channel_name",
                            "youtube_url",
                            "bridge_tab_id",
                            "target_id",
                            "transfer_complete",
                        )
                        if key in preserved
                    },
                )
                progress_value = float(current.get("progress") or 24)
                report(
                    progress_value,
                    "YouTube đã giữ đúng bản nháp · tự nối lại để kiểm tra và "
                    f"điền metadata còn thiếu sau {int(delay)} giây",
                )
            else:
                report(
                    1,
                    "YouTube/Donut/proxy chưa tải được trước khi gắn file · đóng tab "
                    "lỗi, mở tab mới và tự thử lại sau "
                    f"{int(delay)} giây",
                )
            if cancel.wait(delay):
                self._cancelled(cancel)

            try:
                if transient_pre_attach:
                    return self._upload_locked(request, sources, report, cancel)
                registered = self._registry_get(identity)
                try:
                    current = json.loads(
                        self._checkpoint_path(request.workspace).read_text(
                            encoding="utf-8"
                        )
                    )
                except (OSError, UnicodeError, json.JSONDecodeError):
                    current = {}
                if not isinstance(current, dict):
                    current = {}
                record = {**dict(current), **dict(registered)}
                if metadata_only:
                    record["metadata_only_recovery"] = True
                progress_value = float(current.get("progress") or 24)
                allocated_url = str(record.get("youtube_url") or "").strip()
                if progress_value >= 94.0 or _youtube_video_id(allocated_url):
                    return self._reconcile_submitted_locked(
                        request,
                        sources,
                        record,
                        report,
                        cancel,
                    )
                return self._resume_attached_locked(
                    request,
                    sources,
                    record,
                    report,
                    cancel,
                )
            except YouTubeUploadCancelled:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning('youtube_checkpoint_recovery_retry workspace_id=%s video_id=%s error=%s',
                    request.workspace.workspace_id,
                    _youtube_video_id(str(record.get('youtube_url') or '')) if 'record' in locals() else '',
                    str(exc)[:700])
                continue
        raise last_error

    @staticmethod
    def _assert_prior_public_completed(request: YouTubeUploadRequest) -> None:
        if request.wait_for_prior_public and has_pending_prior_public(request.workspace):
            raise YouTubeUploadWaitingForPublic(
                "Đang chờ video Unlisted trước chuyển Công khai; "
                "giữ video đã render, chưa upload tiếp."
            )

    def upload(
        self,
        request: YouTubeUploadRequest,
        *,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> YouTubeUploadResult:
        cancel = cancel_event or threading.Event()
        report = progress or (lambda _percent, _message: None)
        sources = resolve_workspace_upload_sources(
            request.workspace,
            video_path=request.video_path,
            package_path=request.package_path,
            thumbnail_path=request.thumbnail_path,
        )
        _wait_for_render_upload_ready(
            request.workspace,
            sources.video_path,
            cancel,
            report,
        )
        request = _apply_upload_visibility_cutoff(request)
        if request.hold_public_until_next_morning and request.privacy == "PUBLIC":
            report(
                0.5,
                "Đã qua mốc công khai · video mới sẽ upload Không công khai để chờ ca kế tiếp.",
            )
        try:
            prior = json.loads(
                self._checkpoint_path(request.workspace).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            prior = {}
        if _prior_uncertain_staged_video_matches(
            request.workspace,
            prior,
            sources.video_path,
        ):
            raise YouTubeUploadUncertain(
                "Checkpoint upload trước vẫn trỏ file .part của đúng video này và lượt đó "
                "chưa được xác nhận. Tool khóa upload mới để tránh tạo bản trùng; cần đối "
                "chiếu bản nháp đã cấp trong YouTube Studio trước."
            )
        report(0.5, "Đang tạo khóa SHA-256 chống đăng trùng video…")
        identity = self._request_identity(request, sources)
        legacy_identity = self._legacy_request_identity(request, sources)
        if (
            isinstance(prior, dict)
            and str(prior.get("identity") or "") in {identity, legacy_identity}
            and str(prior.get("status") or "").casefold() == "complete"
        ):
            report(100, "Video này đã được upload trước đó · không đăng trùng")
            logger.info(
                "youtube_duplicate_prevented workspace_id=%s profile_suffix=%s video=%s",
                request.workspace.workspace_id,
                request.profile_id[-6:],
                sources.video_path.name,
            )
            return YouTubeUploadResult(
                profile_id=request.profile_id,
                profile_name=str(prior.get("profile_name") or "Profile đã chọn"),
                channel_name=str(prior.get("channel_name") or "Kênh đã upload"),
                video_path=sources.video_path,
                package_path=sources.package_path,
                youtube_url=str(prior.get("youtube_url") or ""),
                thumbnail_path=sources.thumbnail_path,
                privacy_status=_confirmed_privacy_from_record(prior),
            )
        registry_item = self._registry_get(identity)
        registry_status = str(registry_item.get("status") or "").casefold()
        if registry_item.get("replacement_privacy") == "UNLISTED":
            final_privacy = str(registry_item.get("replacement_final_privacy") or "UNLISTED")
            request = replace(request, privacy=final_privacy, hold_public_until_next_morning=final_privacy == "PUBLIC")
            if registry_status == "replacement_ready":
                self._write_replacement_ready_checkpoint(request, sources, identity, registry_item)
                prior = json.loads(self._checkpoint_path(request.workspace).read_text(encoding="utf-8"))
        if registry_item.get("recovery_requires_manual") is True:
            if not request.manual_profile_recheck:
                raise YouTubeUploadRecoveryPaused(
                    str(registry_item.get("recovery_stop_reason") or
                        "Tab upload cũ đã mất và chưa xác định được duy nhất bản nháp. Đã dừng tự mở profile; giữ checkpoint để đối chiếu, không upload mới.")
                )
            self._registry_update_attempt(identity, str(registry_item.get("attempt_id") or ""),
                                          {"recovery_requires_manual": False})
        if registry_status == "complete":
            report(100, "Video này đã được upload trước đó · khóa toàn cục chặn đăng trùng")
            return YouTubeUploadResult(
                profile_id=request.profile_id,
                profile_name=str(registry_item.get("profile_name") or "Profile đã chọn"),
                channel_name=str(registry_item.get("channel_name") or "Kênh đã upload"),
                video_path=sources.video_path,
                package_path=sources.package_path,
                youtube_url=str(registry_item.get("youtube_url") or ""),
                thumbnail_path=sources.thumbnail_path,
                privacy_status=_confirmed_privacy_from_record(registry_item),
            )
        prior_matches = (
            isinstance(prior, dict)
            and str(prior.get("identity") or "") in {identity, legacy_identity}
        )
        prior_status = (
            str(prior.get("status") or "").casefold() if isinstance(prior, dict) else ""
        )
        try:
            prior_progress = float(prior.get("progress") or 0) if isinstance(prior, dict) else 0.0
        except (TypeError, ValueError):
            prior_progress = 101.0
        allocated_url = str(
            registry_item.get("youtube_url")
            or (prior.get("youtube_url") if isinstance(prior, dict) else "")
            or ""
        ).strip()
        has_allocated_video = bool(_youtube_video_id(allocated_url))
        resume_record = (
            dict(registry_item)
            if (
                prior_matches
                and prior_status in {"attaching", "uploading", "uncertain"}
                and prior_progress < 94.0
                and registry_status in {"attaching", "uploading", "uncertain"}
                and bool(str(registry_item.get("attempt_id") or ""))
                and not has_allocated_video
            )
            else None
        )
        reconcile_record = (
            {**dict(prior), **dict(registry_item)}
            if (
                prior_matches
                and prior_status
                in {"uploading", "submitting", "uncertain", "deferred_public"}
                and (prior_progress >= 94.0 or has_allocated_video)
                and registry_status
                in {"uploading", "submitting", "uncertain", "deferred_public"}
                and bool(str(registry_item.get("attempt_id") or ""))
            )
            else None
        )
        if (
            prior_matches
            and prior_status
            in {"attaching", "uploading", "submitting", "uncertain", "deferred_public"}
            and resume_record is None
            and reconcile_record is None
        ):
            raise YouTubeUploadUncertain(
                "Video này đã bắt đầu một lượt upload trước đó nhưng YouTube chưa trả xác "
                "nhận cuối cùng. Để tránh tạo bản trùng/spam, tool sẽ không tự upload lại. "
                "Hãy kiểm tra bản nháp/nội dung trong đúng profile YouTube trước."
            )
        if (
            registry_status in {"attaching", "uploading", "submitting", "uncertain"}
            and resume_record is None
            and reconcile_record is None
        ):
            raise YouTubeUploadUncertain(
                "Khóa chống trùng toàn cục cho biết video này đã bắt đầu upload vào profile "
                "đã chọn. Tool từ chối tạo lượt thứ hai; hãy kiểm tra YouTube Studio trước."
            )
        if resume_record is None and reconcile_record is None:
            _assert_complete_upload_assets(sources)
            self._assert_prior_public_completed(request)
            self._checkpoint(
                request,
                sources,
                status="queued",
                progress=0,
                detail="Đang chờ profile Donut",
            )
        elif resume_record is not None:
            report(1, "Đang tiếp tục đúng phiên video đã gắn · không upload lại file…")
        else:
            report(1, "Đang kiểm tra lượt Lưu đã gửi · không bấm Lưu hoặc upload lại…")
        metadata_only = bool(
            reconcile_record is not None
            and registry_item.get("transfer_complete") is True
            and _youtube_video_id(str(registry_item.get("youtube_url") or ""))
        )
        lease_options = (
            {"job_kind": "metadata-recovery", "preempt_lower": False}
            if metadata_only else
            {"priority": (
                200 if registry_status in {"attaching", "uploading", "submitting"}
                and registry_item.get("transfer_requires_content_resume") is not True else 150
            )} if resume_record is not None or reconcile_record is not None else {}
        )
        if metadata_only:
            reconcile_record["metadata_only_recovery"] = True
        try:
            restoring_attached_transfer = (
                (resume_record is not None or reconcile_record is not None)
                and registry_status in {"attaching", "uploading", "submitting"}
                and registry_item.get("transfer_requires_content_resume") is not True
            )
            if not metadata_only and not restoring_attached_transfer:
                self._wait_for_existing_file_transfers(
                    identity, cancel, report,
                    **({"recovery_request": request, "recovery_sources": sources}
                       if resume_record is not None or reconcile_record is not None else {}))
            with self.coordinator.acquire(request.profile_id, cancel, report, **lease_options):
                if resume_record is None and reconcile_record is None:
                    # Recheck after waiting in the global lane: another job may
                    # have confirmed an Unlisted predecessor in the meantime.
                    _assert_complete_upload_assets(sources)
                    self._assert_prior_public_completed(request)
                # A proven transferred video cannot start a second upload.
                # Finish its metadata separately so a slow live transfer does
                # not hold every completed draft and occupied browser slot.
                try:
                    return self._execute_locked_upload(
                        request,
                        sources,
                        identity,
                        resume_record,
                        reconcile_record,
                        report,
                        cancel,
                    )
                except (YouTubeUploadCancelled, YouTubeUploadWaitingForPublic):
                    raise
                except Exception as exc:
                    return self._recover_locked_upload(
                        request,
                        sources,
                        identity,
                        report,
                        cancel,
                        exc,
                        metadata_only=metadata_only,
                    )
        except YouTubeUploadCancelled as exc:
            try:
                current = json.loads(
                    self._checkpoint_path(request.workspace).read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError):
                current = {}
            if not isinstance(current, dict):
                current = {}
            current_status = str(current.get("status") or "").casefold()
            if current_status in {
                "attaching",
                "uploading",
                "submitting",
                "uncertain",
                "deferred_public",
            }:
                detail = (
                    "Đã yêu cầu hủy sau khi lượt upload duy nhất bắt đầu. Tool không tự "
                    "upload lại để tránh bản trùng; hãy kiểm tra YouTube Studio."
                )
                self._checkpoint(
                    request,
                    sources,
                    status="uncertain",
                    progress=float(current.get("progress") or 24),
                    detail=detail,
                    extra={
                        key: current[key]
                        for key in (
                            "attempt_id",
                            "profile_name",
                            "channel_name",
                            "youtube_url",
                            "bridge_tab_id",
                            "target_id",
                            "transfer_complete",
                        )
                        if key in current
                    },
                )
                raise YouTubeUploadUncertain(detail) from exc
            self._checkpoint(request, sources, status="cancelled", progress=0, detail=str(exc))
            raise
        except Exception as exc:
            logger.exception(
                "youtube_upload_failed workspace_id=%s profile_suffix=%s",
                request.workspace.workspace_id,
                request.profile_id[-6:],
            )
            registered = self._registry_get(identity)
            if (
                str(registered.get("status") or "").casefold() == "complete"
                and str(registered.get("youtube_url") or "").strip()
            ):
                # A local socket can fail while contexts are closing after the
                # confirmation URL was already durably committed. Returning the
                # confirmed record is safer than changing it to an error and
                # inviting a retry.
                self._checkpoint(
                    request,
                    sources,
                    status="complete",
                    progress=100,
                    detail="YouTube upload đã được xác nhận trước khi kết nối local đóng",
                    extra={
                        "profile_name": str(
                            registered.get("profile_name") or "Profile đã chọn"
                        ),
                        "channel_name": str(
                            registered.get("channel_name") or "Kênh đã upload"
                        ),
                        "youtube_url": str(registered.get("youtube_url") or ""),
                    },
                )
                return YouTubeUploadResult(
                    profile_id=request.profile_id,
                    profile_name=str(registered.get("profile_name") or "Profile đã chọn"),
                    channel_name=str(registered.get("channel_name") or "Kênh đã upload"),
                    video_path=sources.video_path,
                    package_path=sources.package_path,
                    youtube_url=str(registered.get("youtube_url") or ""),
                    thumbnail_path=sources.thumbnail_path,
                    privacy_status=_confirmed_privacy_from_record(registered),
                )
            try:
                current = json.loads(
                    self._checkpoint_path(request.workspace).read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError):
                current = {}
            if not isinstance(current, dict):
                current = {}
            current_status = str(current.get("status") or "").casefold()
            if current_status in {
                "attaching",
                "uploading",
                "submitting",
                "uncertain",
                "deferred_public",
            }:
                detail = (
                    "Lượt upload duy nhất đã bắt đầu nhưng chưa xác nhận hoàn tất. Tool đã "
                    "khóa mọi lần thử lại để tránh đăng trùng/spam. Hãy kiểm tra đúng profile "
                    f"trên YouTube Studio. Lỗi gốc: {exc}"
                )
                self._checkpoint(
                    request,
                    sources,
                    status="uncertain",
                    progress=float(current.get("progress") or 24),
                    detail=detail,
                    extra={
                        key: current[key]
                        for key in (
                            "attempt_id",
                            "profile_name",
                            "channel_name",
                            "youtube_url",
                            "bridge_tab_id",
                            "target_id",
                            "transfer_complete",
                        )
                        if key in current
                    },
                )
                if isinstance(exc, YouTubeUploadUncertain):
                    raise
                raise YouTubeUploadUncertain(detail) from exc
            self._checkpoint(request, sources, status="error", progress=0, detail=str(exc))
            if isinstance(exc, YouTubeUploadError):
                raise
            raise YouTubeUploadError(str(exc)) from exc

    def promote_deferred_public(
        self,
        *,
        endpoint: DonutMcpEndpoint,
        profile_id: str,
        youtube_url: str,
        identity: str = "",
        attempt_id: str = "",
        report: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        public_deadline_at: datetime | None = None,
        publication_sources: WorkspaceUploadSources | None = None,
        publication_workspace: Workspace | None = None,
        assets_only: bool = False,
    ) -> YouTubeVisibilityPromotionResult:
        """Promote one exact queued video without attaching or uploading a file.

        The queue contains a URL allocated and metadata-verified by an earlier
        upload.  This path deliberately opens only an ID-scoped Studio edit
        target, changes visibility, saves it, reloads the same target, and
        verifies PUBLIC.  It never consults the current workspace output files.
        """

        cancel = cancel_event or threading.Event()
        _check_publication_deadline(public_deadline_at)
        report_callback = report or (lambda _percent, _message: None)
        video_id = _youtube_video_id(youtube_url)
        if not video_id:
            raise YouTubeUploadError(
                "Hàng đợi Công khai thiếu mã video YouTube hợp lệ; không thao tác."
            )
        self._assert_public_video_not_superseded(profile_id, video_id)
        from .publication_assets import valid_thumbnail
        if publication_sources is None or publication_workspace is None:
            raise YouTubeUploadError("Thiếu nguyên liệu đã xác minh; không chuyển Public.")
        if (not publication_sources.metadata.title.strip()
                or not publication_sources.metadata.description.strip()
                or not publication_sources.metadata.tags
                or publication_sources.thumbnail_path is None
                or not valid_thumbnail(publication_sources.thumbnail_path)):
            raise YouTubeUploadError("Thiếu metadata hoặc thumbnail; giữ Không công khai.")
        edit_url = f"https://studio.youtube.com/video/{video_id}/edit"
        with self.coordinator.acquire(profile_id, cancel, report_callback,
                                      job_kind="metadata-recovery", priority=300,
                                      preempt_lower=True):
            self._cancelled(cancel)
            _check_publication_deadline(public_deadline_at)
            with DonutMcpClient(endpoint, timeout=40) as client:
                client.require_docurandom_local()
                profile = client.get_profile(profile_id)
                if profile.browser.casefold() not in {"wayfern", "camoufox"}:
                    raise YouTubeUploadError(
                        "Profile trong hàng đợi không hỗ trợ bộ điều khiển local."
                    )
                opened_by_promotion = False
                try:
                    if not profile.process_id:
                        report_callback(5, f"Đang mở lại profile {profile.name} để chuyển Công khai…")
                        launch_result = client.run_profile(
                            profile.profile_id,
                            YOUTUBE_STUDIO_URL,
                        )
                        opened_by_promotion = (
                            str(launch_result or "").strip().casefold() != "already-running"
                        )
                        deadline = time.monotonic() + 120.0
                        while time.monotonic() < deadline:
                            self._cancelled(cancel)
                            profile = client.get_profile(profile_id)
                            if profile.process_id:
                                break
                            cancel.wait(0.75)
                        if not profile.process_id:
                            raise YouTubeUploadError(
                                "Không mở lại được profile chứa video chờ Công khai."
                            )

                    debug = client.get_debug_connection(profile.profile_id)
                    report_callback(8, f"Đang mở target riêng để kiểm tra video {video_id}…")
                    ws_url = _mcp_open_minimized_cdp_window_ws_url(
                        client,
                        profile.profile_id,
                        debug,
                        "about:blank",
                    )
                    target_id = _cdp_page_target_id(ws_url)
                    try:
                        _mcp_set_cdp_window_state(
                            client,
                            profile.profile_id,
                            target_id,
                            "parked",
                        )
                        browser_bridge = _ensure_youtube_browser_bridge(
                            client,
                            profile.profile_id,
                        )
                        browser_bridge, bridge_tab_id = _browser_extension_tab_for_target(
                            client,
                            profile.profile_id,
                            target_id,
                            bridge=browser_bridge,
                        )
                        with _CdpSession(ws_url) as cdp:
                            controller = _SemanticCdpController(
                                cdp,
                                browser_bridge=browser_bridge,
                                bridge_profile_id=profile.profile_id,
                                bridge_tab_id=bridge_tab_id,
                                automation_target_id=target_id,
                                window_state=lambda state: _mcp_set_cdp_window_state(
                                    client,
                                    profile.profile_id,
                                    target_id,
                                    state,
                                ),
                            )
                            controller.enable()
                            self._prepare_exact_draft_edit_target(
                                controller,
                                youtube_url,
                                cancel,
                                report_callback,
                            )
                            saved_upload = self._registry_get(identity)
                            if (saved_upload.get('attempt_id') == attempt_id
                                    and saved_upload.get('profile_id') == profile_id
                                    and self._commit_transferred_private_draft(
                                        controller, youtube_url, saved_upload, report_callback, cancel)):
                                controller.navigate_and_wait(edit_url, timeout=120, cancel_event=cancel)
                            with _youtube_metadata_ui_guard(cancel, report_callback):
                                _check_publication_deadline(public_deadline_at)
                                # Save/read back all required metadata and the custom image
                                # while still Unlisted, before the Public visibility action.
                                request = YouTubeUploadRequest(
                                    workspace=publication_workspace, endpoint=endpoint,
                                    profile_id=profile_id, privacy="UNLISTED",
                                    contains_synthetic_media=True,
                                )
                                report_callback(90, "Đang bổ sung và xác minh đủ metadata/thumbnail trước Public…")
                                if self._verify_edit_visibility(controller, "PUBLIC", cancel):
                                    request = replace(request, privacy="PUBLIC")
                                    mismatches = self._saved_metadata_mismatches(
                                        request, publication_sources, controller, cancel,
                                    )
                                    if mismatches:
                                        report_callback(
                                            91, "Video đã Public · bổ sung metadata còn thiếu trên đúng mã video, giữ quyền riêng tư…",
                                        )
                                        self._persist_and_verify_saved_metadata(
                                            request, publication_sources, controller, youtube_url,
                                            report_callback, cancel,
                                        )
                                else:
                                    self._persist_and_verify_saved_metadata(
                                        request, publication_sources, controller, youtube_url,
                                        report_callback, cancel,
                                    )
                                if assets_only:
                                    return YouTubeVisibilityPromotionResult(
                                        profile_id=profile.profile_id, profile_name=profile.name,
                                        channel_name="Kênh trong profile", youtube_url=f"https://youtu.be/{video_id}",
                                    )
                                _check_publication_deadline(public_deadline_at)
                                report_callback(98, "Đang chuyển đúng video sang Công khai…")
                                try:
                                    self._set_edit_visibility(
                                        controller, "PUBLIC", cancel,
                                        public_deadline_at=public_deadline_at,
                                    )
                                except YouTubeUploadError as exc:
                                    if not any(label in str(exc) for label in ('Done', 'Xong', 'Hoàn tất', '完了')):
                                        raise
                                    # A disabled popup is not proof of either
                                    # failure or publication. Re-read the same
                                    # server target once; never create an upload.
                                    controller.navigate_and_wait(edit_url, timeout=60, cancel_event=cancel)
                                    if not self._verify_edit_visibility(controller, 'PUBLIC', cancel):
                                        raise exc
                                    return YouTubeVisibilityPromotionResult(
                                        profile_id=profile.profile_id, profile_name=profile.name,
                                        channel_name='Kênh trong profile', youtube_url=f'https://youtu.be/{video_id}',
                                    )
                                save = controller.wait_dom(
                                    attributes={"id": "save"},
                                    timeout=60,
                                    require_laid_out=True,
                                    cancel_event=cancel,
                                )
                                save_attrs = controller._dom_attributes(save)
                                save_enabled = not (
                                    "disabled" in save_attrs
                                    or save_attrs.get("aria-disabled", "false").casefold()
                                    == "true"
                                )
                                if save_enabled:
                                    self._cancelled(cancel)
                                    _check_publication_deadline(public_deadline_at)
                                    controller.press(save)
                                    self._wait_edit_save_complete(controller, cancel)
                                controller.navigate_and_wait(
                                    edit_url,
                                    timeout=120,
                                    cancel_event=cancel,
                                )
                                controller.wait_dom(
                                    attributes={"id": "textbox"},
                                    ancestor_ids=("title-textarea",),
                                    timeout=120,
                                    require_laid_out=True,
                                    cancel_event=cancel,
                                )
                                if not self._verify_edit_visibility(
                                    controller,
                                    "PUBLIC",
                                    cancel,
                                ):
                                    raise YouTubeUploadError(
                                        f"YouTube chưa xác minh video {video_id} đã chuyển PUBLIC."
                                    )
                                report_callback(100, "Đã chuyển đúng video sang Công khai và xác minh lại")
                    finally:
                        _mcp_close_cdp_page_target(
                            client,
                            profile.profile_id,
                            debug,
                            target_id,
                        )

                    if identity and attempt_id:
                        self._registry_update_attempt(
                            identity,
                            attempt_id,
                            {
                                "status": "complete",
                                "youtube_url": youtube_url,
                                "privacy": "PUBLIC",
                                "final_privacy": "PUBLIC",
                                "deferred_public": False,
                                "transfer_complete": True,
                                "updated_at": _now(),
                            },
                        )
                    return YouTubeVisibilityPromotionResult(
                        profile_id=profile.profile_id,
                        profile_name=profile.name,
                        channel_name="Kênh trong profile",
                        youtube_url=f"https://youtu.be/{video_id}",
                    )
                finally:
                    if opened_by_promotion:
                        self._close_profile_started_by_workflow(
                            client,
                            profile.profile_id,
                        )

    @staticmethod
    def _source_video_duration_seconds(path: Path) -> float | None:
        tools = find_tools()
        if tools is None:
            return None
        try:
            completed = subprocess.run(
                [
                    str(tools.ffprobe),
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            value = float(completed.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _draft_handoff_after_seconds(duration: float | None) -> float:
        """Choose the Content handoff grace period from the source duration."""

        if duration is None or duration < 30 * 60:
            return float(_YOUTUBE_DRAFT_HANDOFF_AFTER_SECONDS)
        if duration <= 60 * 60:
            return float(_YOUTUBE_LONG_DRAFT_HANDOFF_AFTER_SECONDS)
        return float(_YOUTUBE_MAX_DRAFT_HANDOFF_AFTER_SECONDS)

    @staticmethod
    def _duration_values_from_studio_row(text: str) -> tuple[float, ...]:
        values: list[float] = []
        for hours, minutes, seconds in re.findall(
            r"(?<!\d)(?:(\d{1,3}):)?([0-5]?\d):([0-5]\d)(?!\d)",
            str(text or ""),
        ):
            values.append(
                float((int(hours) * 3600 if hours else 0) + int(minutes) * 60 + int(seconds))
            )
        return tuple(values)

    @staticmethod
    def _draft_label(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return " ".join(normalized.split())

    @classmethod
    def _studio_date_markers(cls, attempted_at: datetime) -> tuple[str, ...]:
        english_months = (
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        )
        markers: set[str] = set()
        local_day = attempted_at.astimezone().date()
        for offset in (-1, 0, 1):
            day = local_day + timedelta(days=offset)
            month_name = english_months[day.month - 1]
            for value in (
                f"{month_name} {day.day}, {day.year}",
                f"{day.day} {month_name} {day.year}",
                f"{day.day} thg {day.month}, {day.year}",
                f"{day.day} tháng {day.month}, {day.year}",
                f"{day.year}/{day.month}/{day.day}",
                f"{day.year}/{day.month:02}/{day.day:02}",
                f"{day.year}-{day.month:02}-{day.day:02}",
                f"{day.day}/{day.month}/{day.year}",
                f"{day.year}年{day.month}月{day.day}日",
                f"{day.year}년 {day.month}월 {day.day}일",
            ):
                markers.add(cls._draft_label(value))
            if offset == 0:
                markers.update(
                    {
                        "today",
                        "hôm nay",
                        "vừa xong",
                        "mới đây",
                        "just now",
                        "a few seconds ago",
                        "minutes ago",
                        "hours ago",
                        "phút trước",
                        "giờ trước",
                        "今日",
                        "今",
                        "오늘",
                    }
                )
        return tuple(sorted(markers))

    @classmethod
    def _select_unique_recent_draft(
        cls,
        candidates: tuple[_YouTubeDraftCandidate, ...],
        sources: WorkspaceUploadSources,
        *,
        expected_duration: float | None,
        expected_date_markers: tuple[str, ...] = (),
    ) -> _YouTubeDraftCandidate | None:
        """Select only one recent row matching identity and processed duration."""

        aliases = {
            cls._draft_label(sources.video_path.name),
            cls._draft_label(sources.video_path.stem),
            cls._draft_label(sources.metadata.title),
        }
        aliases.discard("")
        blocked_states = (
            "uploading",
            "upload failed",
            "upload aborted",
            "processing abandoned",
            "đang tải lên",
            "tải lên không thành công",
            "đã hủy tải lên",
            "アップロード中",
            "アップロードに失敗",
            "処理を中止",
            "업로드 중",
            "업로드 실패",
        )
        draft_states = (
            "draft",
            "private",
            "bản nháp",
            "riêng tư",
            "下書き",
            "非公開",
            "초안",
            "비공개",
        )
        matches: list[_YouTubeDraftCandidate] = []
        for candidate in candidates:
            if candidate.position >= 10:
                continue
            title = cls._draft_label(candidate.title)
            row = cls._draft_label(candidate.row_text)
            if any(marker in row for marker in blocked_states):
                continue
            if expected_date_markers and not any(
                marker in row for marker in expected_date_markers
            ):
                continue
            identity_matches = any(
                alias == title
                or title.endswith(f": {alias}")
                or (len(alias) >= 4 and alias in row)
                for alias in aliases
            )
            generic_title = (
                not title
                or title in {"draft", "untitled", "video thumbnail"}
                or title.rstrip(":") == "video thumbnail"
            )
            blank_draft_matches = generic_title and any(
                marker in row for marker in draft_states
            )
            if not identity_matches and not blank_draft_matches:
                continue
            durations = cls._duration_values_from_studio_row(candidate.row_text)
            if expected_duration is None or not durations:
                # Time/title alone can collide when a workspace reuses its
                # output filename. Fail closed unless the Studio row also
                # proves the duration of the rendered file.
                continue
            tolerance = max(3.0, expected_duration * 0.01)
            if not any(abs(value - expected_duration) <= tolerance for value in durations):
                continue
            matches.append(candidate)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _studio_draft_candidates(
        controller: _SemanticCdpController,
    ) -> tuple[_YouTubeDraftCandidate, ...]:
        expression = r"""(() => {
          const roots = [];
          const seenRoots = new Set();
          const visitRoots = root => {
            if (!root || !root.querySelectorAll || seenRoots.has(root)) return;
            seenRoots.add(root);
            roots.push(root);
            for (const element of root.querySelectorAll('*')) {
              if (element.shadowRoot) visitRoots(element.shadowRoot);
            }
          };
          const renderedText = root => {
            const chunks = [];
            const visitText = node => {
              if (!node) return;
              if (node.nodeType === Node.TEXT_NODE) {
                const value = String(node.nodeValue || '').trim();
                if (value) chunks.push(value);
                return;
              }
              if (node.nodeType !== Node.ELEMENT_NODE &&
                  node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return;
              if (node.nodeType === Node.ELEMENT_NODE) {
                if (!node.isConnected) return;
                const style = getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden' ||
                    node.getAttribute('aria-hidden') === 'true') return;
              }
              if (node.shadowRoot) visitText(node.shadowRoot);
              for (const child of node.childNodes || []) visitText(child);
            };
            visitText(root);
            return chunks.join(' ').replace(/\s+/g, ' ').trim().slice(0, 2400);
          };
          const rowFor = element => {
            let current = element;
            for (let depth = 0; depth < 32 && current; depth += 1) {
              const tag = String(current.tagName || '').toUpperCase();
              const role = String(current.getAttribute?.('role') || '').toLowerCase();
              const className = String(current.className || '').toLowerCase();
              if (tag === 'YTCP-VIDEO-ROW' || tag.includes('VIDEO-ROW') ||
                  (role === 'row' && className.includes('video-row'))) return current;
              if (current.parentElement) current = current.parentElement;
              else {
                const root = current.getRootNode?.();
                current = root && root.host ? root.host : null;
              }
            }
            return null;
          };
          const idsFor = element => {
            const ids = new Set();
            const values = [
              element.getAttribute?.('href') || '',
              element.getAttribute?.('src') || '',
              element.getAttribute?.('data-video-id') || '',
              element.getAttribute?.('video-id') || '',
              element.getAttribute?.('data-videoid') || '',
              element.getAttribute?.('videoid') || '',
            ];
            for (const value of values) {
              const text = String(value || '');
              const hrefMatch = text.match(/\/video\/([A-Za-z0-9_-]{6,20})\/(?:edit|analytics)(?:[/?#]|$)/i);
              if (hrefMatch) ids.add(hrefMatch[1]);
              const thumbnailMatch = text.match(/\/vi(?:_[A-Za-z0-9]+)?\/([A-Za-z0-9_-]{6,20})\//i);
              if (thumbnailMatch) ids.add(thumbnailMatch[1]);
              if (/^[A-Za-z0-9_-]{6,20}$/.test(text)) ids.add(text);
            }
            return ids;
          };
          visitRoots(document);
          const elements = [];
          for (const root of roots) {
            for (const element of root.querySelectorAll('*')) elements.push(element);
          }
          const rows = new Map();
          let position = 0;
          for (const element of elements) {
            const row = rowFor(element);
            if (!row) continue;
            let item = rows.get(row);
            if (!item) {
              item = { row, elements: [], ids: new Set(), position: position++ };
              rows.set(row, item);
            }
            item.elements.push(element);
            for (const videoId of idsFor(element)) item.ids.add(videoId);
          }
          const output = [];
          for (const item of rows.values()) {
            for (const videoId of item.ids) {
              const titleElement = item.elements.find(
                element => element.getAttribute?.('id') === 'video-title',
              );
              const title = titleElement
                ? String(titleElement.getAttribute?.('aria-label') ||
                    titleElement.getAttribute?.('title') ||
                    renderedText(titleElement) || '').trim()
                : '';
              output.push({
                videoId,
                title,
                rowText: renderedText(item.row),
                position: item.position,
              });
            }
          }
          return output.sort((left, right) => left.position - right.position);
        })()"""
        if not controller.has_background_bridge:
            raise YouTubeUploadError(
                "DocuRandom Donut Bridge chưa sẵn sàng để đọc Nội dung YouTube. "
                "Tool không rơi về Runtime.evaluate native của Wayfern; checkpoint "
                "được giữ và không upload lại."
            )
        state = controller.content_upload_state(timeout=20)
        value = state.get("candidates") if isinstance(state, dict) else None
        if not isinstance(value, list):
            return ()
        output: list[_YouTubeDraftCandidate] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            video_id = str(item.get("videoId") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
                continue
            try:
                position = max(0, int(item.get("position") or 0))
            except (TypeError, ValueError):
                position = len(output)
            output.append(
                _YouTubeDraftCandidate(
                    video_id=video_id,
                    title=str(item.get("title") or "").strip(),
                    row_text=str(item.get("rowText") or "").strip(),
                    position=position,
                )
            )
        return tuple(output)

    @staticmethod
    def _prepare_exact_draft_edit_target(
        controller: _SemanticCdpController,
        youtube_url: str,
        cancel: threading.Event,
        report: ProgressCallback,
        *,
        timeout: float = 120.0,
        on_content_loaded: Callable[[], bool] | None = None,
    ) -> None:
        """Prove one allocated ID in Content, then load its exact edit page.

        The upload dialog can allocate a watch URL before its draft is visible
        to Studio.  This preflight deliberately uses a new target and the
        Content list, but matches only the checkpointed video ID.  A video ID
        cannot be safely replaced by a different workspace upload, so this is
        stronger than picking the newest row by title alone.  The edit page is
        loaded before the old upload target is closed, which makes the handoff
        reversible when Studio is still slow.
        """

        video_id = _youtube_video_id(youtube_url)
        if not video_id:
            raise YouTubeUploadError(
                "YouTube chưa cấp mã video hợp lệ để kiểm tra bản nháp trong Nội dung."
            )
        controller.navigate_and_wait(
            YOUTUBE_STUDIO_URL,
            timeout=timeout,
            cancel_event=cancel,
        )
        channel_id = controller.wait_channel_id(
            timeout=timeout,
            cancel_event=cancel,
        )
        content_url = (
            f"https://studio.youtube.com/channel/{channel_id}/videos/upload"
            "?filter=%5B%5D&sort=%7B%22columnType%22%3A%22date%22%2C%22sortOrder%22%3A%22DESCENDING%22%7D"
        )
        controller.navigate_and_wait(
            content_url,
            timeout=timeout,
            cancel_event=cancel,
        )
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            YouTubeStudioUploader._cancelled(cancel)
            controller.raise_for_page_error()
            candidate = next(
                (
                    item
                    for item in YouTubeStudioUploader._studio_draft_candidates(controller)
                    if item.video_id == video_id
                ),
                None,
            )
            if candidate is not None:
                # Navigation can finish before Polymer has rendered the row
                # and its Resume control. Probe only after the exact ID is
                # present; an earlier one-shot probe silently misses recovery.
                if on_content_loaded is not None and on_content_loaded():
                    return
                active, status_detail = YouTubeStudioUploader._content_row_upload_status(
                    candidate.row_text
                )
                if active:
                    raise YouTubeUploadError(
                        f"Đúng video ID {video_id} đã có trong Nội dung nhưng "
                        f"chưa tải xong ({status_detail}); tool giữ tab upload cũ."
                    )
                report(97.6, f"Đã thấy đúng video ID {video_id} trong Nội dung")
                edit_url = f"https://studio.youtube.com/video/{video_id}/edit"
                YouTubeStudioUploader._load_exact_draft_editor(
                    controller, edit_url, cancel, report, timeout=timeout,
                )
                return
            cancel.wait(1.0)
        raise YouTubeUploadError(
            f"YouTube chưa thấy đúng video ID {video_id} trong Nội dung trước thời hạn; "
            "tool giữ tab upload cũ và không upload lại file."
        )

    @staticmethod
    def _load_exact_draft_editor(
        controller: _SemanticCdpController,
        edit_url: str,
        cancel: threading.Event,
        report: ProgressCallback,
        *,
        timeout: float = 120.0,
    ) -> None:
        """Rebuild an empty editor once, before any metadata has been written.

        Studio sometimes commits the edit URL but fails to mount its Polymer
        editor. A new navigation of that exact allocated ID can recover it.
        Never use this repair on an attached upload, another route, or an
        authentication/network error; those need their existing recovery lane.
        """
        expected = urllib.parse.urlsplit(edit_url)
        probe_timeout = max(1.0, float(timeout) / 2.0)
        for load_attempt in range(2):
            YouTubeStudioUploader._cancelled(cancel)
            controller.navigate_and_wait(
                edit_url, timeout=timeout, cancel_event=cancel,
            )
            try:
                controller.wait_dom(
                    attributes={"id": "textbox"},
                    ancestor_ids=("title-textarea",),
                    timeout=probe_timeout,
                    require_laid_out=True,
                    cancel_event=cancel,
                )
                return
            except YouTubeUploadError as exc:
                YouTubeStudioUploader._cancelled(cancel)
                if (
                    load_attempt != 0
                    or "không hiện điều khiển" not in str(exc).casefold()
                    or getattr(controller, "upload_started", False) is True
                ):
                    raise
                # Recheck redirects before retrying. In particular, do not
                # reload a Google sign-in/consent page as if it were a blank
                # editor, or discard a different video after a route change.
                controller.raise_for_page_error()
                current = urllib.parse.urlsplit(controller.page_url())
                if (
                    current.scheme != "https"
                    or current.hostname != "studio.youtube.com"
                    or current.path != expected.path
                ):
                    raise
                report(
                    97.6,
                    "Trang chỉnh sửa chưa dựng được giao diện · tải lại đúng video "
                    "một lần trước khi sửa metadata…",
                )

    @staticmethod
    def _content_row_upload_status(row_text: str) -> tuple[bool, str]:
        """Classify whether a Content row is still transferring the file.

        ``Pending`` is used for two different moments by Studio: it can
        accompany a live ``Uploading 43%`` row, or remain after the bytes have
        arrived while checks are pending.  The explicit upload marker/progress
        is therefore authoritative; post-upload processing is a safe handoff
        boundary because the row already exposes ``Edit draft``.
        """

        normalized = " ".join(str(row_text or "").split())
        lowered = normalized.casefold()
        percent, _remaining = _SemanticCdpController._parse_youtube_upload_progress(normalized)
        if percent is not None and percent >= 100:
            return False, "đã truyền đủ 100%"
        progress_match = re.search(
            r"(?:uploading|đang\s+tải\s+lên)\s*"
            r"([0-9]{1,3}(?:[.,][0-9]+)?)\s*%",
            lowered,
        )
        progress_detail = ""
        if progress_match:
            raw_percent = progress_match.group(1).replace(",", ".")
            try:
                percent = float(raw_percent)
            except ValueError:
                percent = None
            if percent is not None:
                percent_label = (
                    str(int(percent)) if percent.is_integer() else f"{percent:.1f}"
                )
                progress_detail = f"đang tải {percent_label}%"
        remaining_match = re.search(
            r"([0-9]+)\s*(?:seconds?|secs?|giây)\s*(?:left|remaining|còn)?",
            lowered,
        )
        if remaining_match:
            progress_detail += (
                (" · " if progress_detail else "")
                + f"còn {remaining_match.group(1)} giây"
            )
        if percent is not None and not progress_detail:
            progress_detail = f"đang tải {percent:g}%"
            if _remaining:
                progress_detail += f" · còn {_remaining}"
        if progress_detail:
            # A visible percentage is direct evidence that the file is still
            # being transferred, even when the same row also says Pending.
            return True, progress_detail
        failure_markers = (
            "upload failed",
            "upload aborted",
            "processing abandoned",
            "processing failed",
            "tải lên không thành công",
            "đã hủy tải lên",
            "アップロード失敗",
            "処理に失敗",
            "업로드 실패",
            "처리 실패",
        )
        if any(marker in lowered for marker in failure_markers):
            return True, "upload hoặc xử lý thất bại"
        resume_markers = (
            "continue uploading",
            "continue upload",
            "resume upload",
            "resume uploading",
            "retry upload",
            "tiếp tục tải lên",
            "tiep tuc tai len",
            "tiếp tục tải tệp lên",
            "tiep tuc tai tep len",
            "tiếp tục tải video lên",
            "tiep tuc tai video len",
            "アップロードを続行",
            "继续上传",
            "업로드 계속",
        )
        if any(marker in lowered for marker in resume_markers):
            return True, "cần tiếp tục tải tệp"
        transfer_markers = (
            "uploading",
            "đang tải lên",
            "アップロード中",
            "업로드 중",
        )
        if any(marker in lowered for marker in transfer_markers):
            return True, "đang tải tệp"
        post_transfer_markers = (
            "processing",
            "pending",
            "checks starting soon",
            "đang xử lý",
            "đang chuẩn bị",
            "đang chờ",
            "処理中",
            "처리 중",
        )
        if any(marker in lowered for marker in post_transfer_markers):
            return False, "đã upload xong · YouTube đang xử lý/check"
        return False, "đã sẵn sàng"

    def _probe_exact_video_in_content(
        self,
        youtube_url: str,
        client: DonutMcpClient,
        profile: DonutProfile,
        debug: dict[str, Any],
        report: ProgressCallback,
        cancel: threading.Event,
        *,
        timeout: float = 75.0,
    ) -> str:
        """Inspect the exact saved ID in a disposable Content target.

        This is deliberately a read-only progress probe. It may run several
        times while the upload dialog is still alive; it returns a URL when the
        exact row is no longer transferring or in a failure state. A pending
        post-upload row is an intentional handoff boundary. The caller then
        performs the stronger edit-target preflight before closing the original
        upload target.
        """

        video_id = _youtube_video_id(youtube_url)
        if not video_id:
            return ""
        ws_url = _mcp_open_minimized_cdp_window_ws_url(
            client,
            profile.profile_id,
            debug,
            "about:blank",
        )
        target_id = _cdp_page_target_id(ws_url)
        try:
            _mcp_set_cdp_window_state(
                client,
                profile.profile_id,
                target_id,
                "parked",
            )
            browser_bridge = _ensure_youtube_browser_bridge(
                client,
                profile.profile_id,
            )
            _probe_bridge, bridge_tab_id = _browser_extension_tab_for_target(
                client,
                profile.profile_id,
                target_id,
                bridge=browser_bridge,
            )
            with _CdpSession(ws_url) as cdp:
                controller = _SemanticCdpController(
                    cdp,
                    browser_bridge=browser_bridge,
                    bridge_profile_id=profile.profile_id,
                    bridge_tab_id=bridge_tab_id,
                    automation_target_id=target_id,
                    window_state=lambda state: _mcp_set_cdp_window_state(
                        client,
                        profile.profile_id,
                        target_id,
                        state,
                    ),
                )
                controller.enable()
                navigation_timeout = max(10.0, float(timeout))
                controller.navigate_and_wait(
                    YOUTUBE_STUDIO_URL,
                    timeout=navigation_timeout,
                    cancel_event=cancel,
                )
                channel_id = controller.wait_channel_id(
                    timeout=navigation_timeout,
                    cancel_event=cancel,
                )
                controller.navigate_and_wait(
                    f"https://studio.youtube.com/channel/{channel_id}/videos/upload"
                    "?filter=%5B%5D&sort=%7B%22columnType%22%3A%22date%22%2C%22sortOrder%22%3A%22DESCENDING%22%7D",
                    timeout=navigation_timeout,
                    cancel_event=cancel,
                )
                deadline = time.monotonic() + max(5.0, min(60.0, float(timeout)))
                while time.monotonic() < deadline:
                    self._cancelled(cancel)
                    controller.raise_for_page_error()
                    candidate = next(
                        (
                            item
                            for item in self._studio_draft_candidates(controller)
                            if item.video_id == video_id
                        ),
                        None,
                    )
                    if candidate is not None:
                        active, status_detail = self._content_row_upload_status(
                            candidate.row_text
                        )
                        if active:
                            report(
                                96,
                                f"Tab Nội dung thấy đúng ID {video_id} · "
                                f"{status_detail}"
                                + " · giữ tab upload, sẽ kiểm tra lại sau 3–4 phút",
                            )
                            return ""
                        report(
                            96,
                            f"Tab Nội dung thấy đúng ID {video_id}"
                            + f" · {status_detail} · có thể chuyển sang sửa metadata",
                        )
                        return f"https://youtu.be/{video_id}"
                    cancel.wait(1.0)
                report(
                    96,
                    f"Tab Nội dung chưa thấy đúng ID {video_id} · giữ tab upload, "
                    "sẽ kiểm tra lại sau 3–4 phút",
                )
        finally:
            _mcp_close_cdp_page_target(
                client,
                profile.profile_id,
                debug,
                target_id,
            )
        return ""

    @staticmethod
    def _close_upload_target_after_handoff(
        controller: _SemanticCdpController,
        client: DonutMcpClient,
        profile: DonutProfile,
        debug: dict[str, Any],
        report: ProgressCallback,
    ) -> bool:
        """Close the old upload tab only after a fresh exact target is ready."""

        closed = False
        if controller.has_background_bridge and controller.browser_bridge is not None:
            try:
                result = controller.browser_bridge.command(
                    profile.profile_id,
                    "close_tab",
                    {"tab_id": controller.bridge_tab_id},
                    timeout=8,
                )
                closed = bool(isinstance(result, dict) and result.get("closed"))
            except Exception:
                logger.warning(
                    "youtube_upload_handoff_bridge_close_failed profile_suffix=%s tab_id=%s",
                    profile.profile_id[-6:],
                    controller.bridge_tab_id,
                    exc_info=True,
                )
        if not closed:
            closed = _mcp_close_cdp_page_target(
                client,
                profile.profile_id,
                debug,
                controller.automation_target_id,
            )
        if closed:
            controller.mark_upload_target_handed_off()
            report(97.7, "Đã đóng tab upload cũ sau khi Content xác nhận đúng ID")
        else:
            # The final context cleanup will still close it after a successful
            # metadata save.  If metadata fails, retaining the target is safer
            # than losing the only recovery handle.
            logger.warning(
                "youtube_upload_handoff_target_still_open profile_suffix=%s target_suffix=%s",
                profile.profile_id[-6:],
                controller.automation_target_id[-8:],
            )
            report(97.7, "Đã xác nhận Content nhưng chưa đóng được tab cũ · vẫn giữ làm checkpoint")
        return closed

    def _discover_unique_recent_draft_url(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        record: dict[str, object],
        client: DonutMcpClient,
        profile: DonutProfile,
        debug: dict[str, Any],
        report: ProgressCallback,
        cancel: threading.Event,
    ) -> str:
        """Recover a lost target only from one recent, duration-matched row."""

        timestamp = str(record.get("started_at") or record.get("updated_at") or "")
        try:
            attempted_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if attempted_at.tzinfo is None:
                attempted_at = attempted_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - attempted_at.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return ""
        if age.total_seconds() < -300 or age.total_seconds() > 72 * 60 * 60:
            return ""
        expected_duration = self._source_video_duration_seconds(sources.video_path)
        if expected_duration is None:
            report(7, "Không đọc được thời lượng video · không dò bản nháp bằng suy đoán")
            return ""

        ws_url = _mcp_open_minimized_cdp_window_ws_url(
            client,
            profile.profile_id,
            debug,
            "about:blank",
        )
        target_id = _cdp_page_target_id(ws_url)
        try:
            _mcp_set_cdp_window_state(client, profile.profile_id, target_id, "parked")
            browser_bridge = _ensure_youtube_browser_bridge(
                client,
                profile.profile_id,
            )
            _discover_bridge, bridge_tab_id = _browser_extension_tab_for_target(
                client,
                profile.profile_id,
                target_id,
                bridge=browser_bridge,
            )
            with _CdpSession(ws_url) as cdp:
                controller = _SemanticCdpController(
                    cdp,
                    browser_bridge=browser_bridge,
                    bridge_profile_id=profile.profile_id,
                    bridge_tab_id=bridge_tab_id,
                    automation_target_id=target_id,
                    window_state=lambda state: _mcp_set_cdp_window_state(
                        client,
                        profile.profile_id,
                        target_id,
                        state,
                    ),
                )
                controller.enable()
                controller.navigate_and_wait(
                    YOUTUBE_STUDIO_URL,
                    timeout=90,
                    cancel_event=cancel,
                )
                channel_id = controller.wait_channel_id(
                    timeout=90,
                    cancel_event=cancel,
                )
                controller.navigate_and_wait(
                    f"https://studio.youtube.com/channel/{channel_id}/videos/upload"
                    "?filter=%5B%5D&sort=%7B%22columnType%22%3A%22date%22%2C%22sortOrder%22%3A%22DESCENDING%22%7D",
                    timeout=90,
                    cancel_event=cancel,
                )
                deadline = time.monotonic() + 45.0
                while time.monotonic() < deadline:
                    self._cancelled(cancel)
                    controller.raise_for_page_error()
                    candidates = self._studio_draft_candidates(controller)
                    selected = self._select_unique_recent_draft(
                        candidates,
                        sources,
                        expected_duration=expected_duration,
                        expected_date_markers=self._studio_date_markers(attempted_at),
                    )
                    if selected is not None:
                        report(
                            9,
                            "Đã tìm thấy đúng một bản nháp khớp tên, thời gian và "
                            "thời lượng · nhận lại video ID",
                        )
                        return f"https://youtu.be/{selected.video_id}"
                    cancel.wait(1.0)
        finally:
            _mcp_close_cdp_page_target(
                client,
                profile.profile_id,
                debug,
                target_id,
            )
        return ""

    def _resume_interrupted_upload_locked(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        record: dict[str, object],
        profile: DonutProfile,
        channel_name: str,
        identity: str,
        attempt_id: str,
        controller: _SemanticCdpController,
        report: ProgressCallback,
        cancel: threading.Event,
    ) -> bool:
        """Resume a visible Studio row that asks for the same file again.

        The row in the screenshot is not a completed draft and not a fresh
        upload page.  YouTube has retained an interrupted upload session, so
        the only safe action is to click its semantic "Continue uploading"
        control and bind the *same* checkpointed file back to that session.
        Never fall back to opening the upload wizard when the row is missing or
        ambiguous.
        """

        if not controller.has_background_bridge:
            return False
        self._cancelled(cancel)
        owned_target_id = str(record.get("target_id") or "").strip()
        live_target_id = str(getattr(controller, "automation_target_id", "") or "").strip()
        if owned_target_id and live_target_id and owned_target_id != live_target_id:
            raise YouTubeUploadUncertain(
                "Phiên YouTube bị gián đoạn không còn đúng target mà checkpoint sở hữu. "
                "Tool không chọn dòng cùng tên ở phiên khác."
            )
        state = controller.interrupted_upload_state(sources.video_path.name)
        if not state or state.get("interrupted") is not True:
            return False
        try:
            match_count = int(state.get("matchCount") or 0)
        except (TypeError, ValueError):
            match_count = 0
        if bool(state.get("ambiguous")) or match_count > 1:
            raise YouTubeUploadUncertain(
                "YouTube đang có nhiều dòng cùng yêu cầu tiếp tục tải lên cho "
                f"{sources.video_path.name}. Tool không đoán dòng nào và không upload lại."
            )
        if not state.get("found"):
            # A different interrupted row may be visible, but it does not
            # belong to this checkpoint.  Leave it untouched rather than
            # selecting a similarly named file.
            if bool(state.get("interrupted")):
                raise YouTubeUploadUncertain(
                    "YouTube có phiên tải lên bị gián đoạn nhưng không có dòng nào "
                    f"khớp chính xác với {sources.video_path.name}. Tool không đoán "
                    "và không upload lại file."
                )
            return False
        current = self._registry_get(identity)
        if str(current.get("attempt_id") or "") != attempt_id:
            raise YouTubeUploadUncertain(
                "Khóa lượt upload đã thay đổi trong lúc nối lại phiên bị gián đoạn. "
                "Tool dừng và không gắn lại file."
            )
        if bool(state.get("disabled")):
            raise YouTubeUploadUncertain(
                "YouTube đã nhận ra phiên tải lên bị gián đoạn nhưng nút tiếp tục "
                f"đang bị khóa cho {sources.video_path.name}. Checkpoint vẫn được giữ."
            )
        # A Content-list row can show the same default filename as a newer
        # render. Before clicking Continue, require the live owned target to
        # still expose the exact file input that belongs to this attempt. If
        # it does not, this is only a historical/recoverable row and must be
        # left for explicit manual confirmation.
        attached_before_resume = controller.attached_video()
        attached_name = unicodedata.normalize(
            "NFC", str(attached_before_resume.get("name") or "")
        ).casefold()
        try:
            attached_size = int(attached_before_resume.get("size") or 0)
        except (TypeError, ValueError):
            attached_size = 0
        expected_name = unicodedata.normalize("NFC", sources.video_path.name).casefold()
        expected_size = int(sources.video_path.stat().st_size)
        if attached_name != expected_name or attached_size != expected_size:
            raise YouTubeUploadUncertain(
                "YouTube chỉ hiển thị dòng tiếp tục tải lên có tên file trùng, "
                "nhưng target hiện tại không còn giữ đúng file nguồn. Tool không "
                "tự chọn để tránh nối nhầm video cũ; hãy xác nhận thủ công nếu cần."
            )

        try:
            resumed = controller.interrupted_upload_state(
                sources.video_path.name,
                resume=True,
            )
        except YouTubeUploadCancelled:
            raise
        except YouTubeUploadUncertain:
            raise
        except YouTubeUploadError as exc:
            raise YouTubeUploadUncertain(
                "YouTube đã nhận diện đúng phiên bị gián đoạn nhưng lỗi khi nối lại; "
                "tool không upload lại file."
            ) from exc
        if not resumed.get("clicked"):
            raise YouTubeUploadUncertain(
                "Không bấm được nút Tiếp tục tải lên của đúng phiên YouTube. "
                "Tool không mở lượt upload mới."
            )
        report(
            20,
            "Đã nhận đúng phiên YouTube bị gián đoạn · chuẩn bị nối lại cùng file, "
            "không tạo video thứ hai",
        )
        self._cancelled(cancel)
        try:
            controller.wait_file_input(
                "video",
                timeout=90,
                cancel_event=cancel,
            )
            controller.set_file(sources.video_path, "video")
        except YouTubeUploadCancelled:
            raise
        except YouTubeUploadUncertain:
            raise
        except YouTubeUploadError as exc:
            raise YouTubeUploadUncertain(
                "YouTube đã mở phiên nối lại nhưng không gắn được chính file nguồn; "
                "tool không tạo upload mới."
            ) from exc
        deadline = time.monotonic() + 20.0
        attached: dict[str, object] = {}
        while time.monotonic() < deadline:
            self._cancelled(cancel)
            try:
                attached = controller.attached_video()
            except YouTubeUploadCancelled:
                raise
            except YouTubeUploadError as exc:
                raise YouTubeUploadUncertain(
                    "YouTube không xác nhận được file vừa gắn vào phiên nối lại; "
                    "tool không upload lại."
                ) from exc
            actual_name = unicodedata.normalize(
                "NFC", str(attached.get("name") or "")
            ).casefold()
            try:
                actual_size = int(attached.get("size") or 0)
            except (TypeError, ValueError):
                actual_size = 0
            if actual_name == expected_name and actual_size == expected_size:
                break
            cancel.wait(0.25)
        else:
            raise YouTubeUploadUncertain(
                "YouTube đã mở lại phiên tải lên nhưng không xác nhận được đúng "
                f"tên và dung lượng của {sources.video_path.name}; tool không tiếp tục."
            )

        now = _now()
        self._registry_update_attempt(
            identity,
            attempt_id,
            {
                "status": "uploading",
                "resumed_interrupted_upload": True,
                "resumed_at": now,
                "updated_at": now,
            },
        )
        self._checkpoint(
            request,
            sources,
            status="uploading",
            progress=24,
            detail=(
                "Đã nối lại đúng phiên tải lên bị gián đoạn bằng cùng file và "
                "đã xác nhận tên/dung lượng; không tạo upload mới"
            ),
            extra={
                "attempt_id": attempt_id,
                "profile_name": profile.name,
                "channel_name": channel_name,
                "target_id": controller.automation_target_id,
                "bridge_tab_id": controller.bridge_tab_id,
                "resumed_interrupted_upload": True,
            },
        )
        return True

    def _resume_existing_content_upload_locked(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        record: dict[str, object],
        profile: DonutProfile,
        channel_name: str,
        identity: str,
        attempt_id: str,
        controller: _SemanticCdpController,
        report: ProgressCallback,
        cancel: threading.Event,
    ) -> bool:
        """Resume an exact Content-row draft after its original tab vanished.

        A Content row can outlive the upload wizard.  In that case the only
        safe recovery is to match the checkpointed video ID, click its own
        Continue control, and bind the same local file.  Filename matching is
        deliberately not used here because Studio often shows the title (or a
        generic label) instead of the source filename.
        """

        if record.get("metadata_only_recovery") is True:
            raise YouTubeUploadUncertain(
                "YouTube yêu cầu truyền lại dữ liệu; lượt xác minh metadata giữ checkpoint "
                "và nhường về hàng đợi truyền file."
            )
        if not controller.has_background_bridge:
            return False
        self._cancelled(cancel)
        video_url = str(record.get("youtube_url") or "").strip()
        video_id = _youtube_video_id(video_url)
        if not video_id:
            return False
        state = controller.content_upload_state(video_id, timeout=20)
        try:
            match_count = int(state.get("matchCount") or 0)
        except (TypeError, ValueError):
            match_count = 0
        if bool(state.get("ambiguous")) or match_count > 1:
            raise YouTubeUploadUncertain(
                f"YouTube có nhiều hàng cùng video ID {video_id}; tool không đoán hàng nào."
            )
        if not state or state.get("found") is not True:
            return False
        if state.get("interrupted") is not True:
            return False
        current = self._registry_get(identity)
        if str(current.get("attempt_id") or "") != attempt_id:
            raise YouTubeUploadUncertain(
                "Khóa lượt upload đã thay đổi trước khi nối lại bản nháp. "
                "Tool dừng và không gắn lại file."
            )
        if bool(state.get("disabled")):
            raise YouTubeUploadUncertain(
                f"Nút Tiếp tục tải lên của video {video_id} đang bị khóa; checkpoint vẫn được giữ."
            )

        try:
            resumed = controller.content_upload_state(
                video_id,
                resume=True,
                timeout=20,
            )
        except YouTubeUploadCancelled:
            raise
        except YouTubeUploadUncertain:
            raise
        except YouTubeUploadError as exc:
            raise YouTubeUploadUncertain(
                f"Không bấm được Tiếp tục tải lên của video {video_id}; tool không upload lại."
            ) from exc
        if not resumed.get("clicked"):
            raise YouTubeUploadUncertain(
                f"YouTube không xác nhận đã mở phiên tiếp tục của video {video_id}; "
                "tool không upload lại."
            )

        # The click has reopened the existing upload session.  Mark this
        # target before attaching the file so a failure keeps a recoverable
        # checkpoint instead of allowing cleanup to discard the only handle.
        controller.mark_upload_started()
        now = _now()
        self._registry_update_attempt(
            identity,
            attempt_id,
            {
                "status": "uploading",
                "youtube_url": video_url,
                "target_id": controller.automation_target_id,
                "bridge_tab_id": controller.bridge_tab_id,
                "resumed_content_upload": True,
                "resumed_at": now,
                "transfer_feedback_progress_at": now,
                "transfer_feedback_observed_at": now,
                "transfer_recovery_stalled": False,
                "updated_at": now,
            },
        )
        self._checkpoint(
            request,
            sources,
            status="uploading",
            progress=20,
            detail=(
                f"Đã mở lại đúng phiên của video {video_id} từ Nội dung; "
                "chuẩn bị gắn lại cùng file, không tạo upload mới"
            ),
            extra={
                "attempt_id": attempt_id,
                "profile_name": profile.name,
                "channel_name": channel_name,
                "youtube_url": video_url,
                "target_id": controller.automation_target_id,
                "bridge_tab_id": controller.bridge_tab_id,
                "resumed_content_upload": True,
                "transfer_complete": False,
            },
        )
        report(20, f"Đã mở lại đúng video {video_id} · chuẩn bị nối cùng file")
        self._cancelled(cancel)

        expected_name = unicodedata.normalize("NFC", sources.video_path.name).casefold()
        expected_size = int(sources.video_path.stat().st_size)
        try:
            controller.wait_file_input(
                "video",
                timeout=90,
                cancel_event=cancel,
            )
            controller.set_file(sources.video_path, "video")
        except YouTubeUploadCancelled:
            raise
        except YouTubeUploadUncertain:
            raise
        except YouTubeUploadError as exc:
            raise YouTubeUploadUncertain(
                f"Video {video_id} đã mở phiên nối lại nhưng không gắn được chính file nguồn; "
                "tool không tạo upload mới."
            ) from exc

        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            self._cancelled(cancel)
            try:
                attached = controller.attached_video()
            except YouTubeUploadCancelled:
                raise
            except YouTubeUploadError as exc:
                raise YouTubeUploadUncertain(
                    f"Không xác minh được file vừa gắn cho video {video_id}; "
                    "tool không upload lại."
                ) from exc
            actual_name = unicodedata.normalize(
                "NFC", str(attached.get("name") or "")
            ).casefold()
            try:
                actual_size = int(attached.get("size") or 0)
            except (TypeError, ValueError):
                actual_size = 0
            if actual_name == expected_name and actual_size == expected_size:
                break
            cancel.wait(0.25)
        else:
            raise YouTubeUploadUncertain(
                f"YouTube không xác nhận đúng tên và dung lượng file cho video {video_id}; "
                "tool không tiếp tục."
            )

        now = _now()
        self._registry_update_attempt(
            identity,
            attempt_id,
            {
                "status": "uploading",
                "youtube_url": video_url,
                "target_id": controller.automation_target_id,
                "bridge_tab_id": controller.bridge_tab_id,
                "resumed_content_upload": True,
                "updated_at": now,
                "transfer_requires_content_resume": False,
            },
        )
        self._checkpoint(
            request,
            sources,
            status="uploading",
            progress=24,
            detail=(
                f"Đã nối lại video {video_id} bằng cùng file và xác nhận "
                "tên/dung lượng; không tạo upload mới"
            ),
            extra={
                "attempt_id": attempt_id,
                "profile_name": profile.name,
                "channel_name": channel_name,
                "youtube_url": video_url,
                "target_id": controller.automation_target_id,
                "bridge_tab_id": controller.bridge_tab_id,
                "resumed_content_upload": True,
                "transfer_complete": False,
            },
        )
        return True

    def _resume_attached_locked(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        record: dict[str, object],
        report: ProgressCallback,
        cancel: threading.Event,
    ) -> YouTubeUploadResult:
        """Continue one live upload dialog without assigning its file input again."""

        ownership: dict[str, bool] = {}
        try:
            return self._resume_attached_locked_inner(
                request, sources, record, report, cancel, ownership=ownership
            )
        except (YouTubeUploadRecoveryPaused, YouTubeUploadTransferStalled) as exc:
            self._park_failed_recovery_profile(request, sources, exc, report)
            raise
        finally:
            self._close_completed_recovery_profile(request, sources, ownership)

    def _resume_attached_locked_inner(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        record: dict[str, object],
        report: ProgressCallback,
        cancel: threading.Event,
        *,
        ownership: dict[str, bool],
    ) -> YouTubeUploadResult:

        self._cancelled(cancel)
        attempt_id = str(record.get("attempt_id") or "")
        if not attempt_id:
            raise YouTubeUploadUncertain(
                "Phiên cũ thiếu mã lượt upload; tool không thể tiếp tục an toàn và không gắn lại file."
            )
        expected_name = unicodedata.normalize("NFC", sources.video_path.name).casefold()
        expected_size = int(sources.video_path.stat().st_size)
        report(3, "Đang tìm đúng tab YouTube còn giữ video đã gắn…")
        discovered_record: dict[str, object] | None = None
        with DonutMcpClient(request.endpoint, timeout=40) as client:
            client.require_docurandom_local()
            profile = client.get_profile(request.profile_id)
            if not profile.process_id:
                report(4, "Profile cũ đã đóng · mở lại chỉ để dò đúng bản nháp")
                launch_result = client.run_profile(
                    profile.profile_id,
                    YOUTUBE_STUDIO_URL,
                    retry_entitlement_after_user_action=request.manual_profile_recheck,
                )
                ownership["opened"] = (
                    str(launch_result or "").strip().casefold() != "already-running"
                )
                if ownership["opened"]:
                    self._registry_update_attempt(
                        self._request_identity(request, sources), str(record.get("attempt_id") or ""),
                        {"profile_started_by_workflow": True},
                    )
                deadline = time.monotonic() + 120.0
                while time.monotonic() < deadline:
                    self._cancelled(cancel)
                    profile = client.get_profile(request.profile_id)
                    if profile.process_id:
                        break
                    cancel.wait(0.5)
                if not profile.process_id:
                    raise YouTubeUploadUncertain(
                        "Profile chứa phiên upload cũ không mở lại được. Checkpoint vẫn giữ "
                        "nguyên và tool không gắn lại file."
                    )
            debug = client.get_debug_connection(profile.profile_id)
            try:
                browser_bridge = _ensure_youtube_browser_bridge(
                    client,
                    profile.profile_id,
                )
            except BrowserExtensionBridgeError:
                browser_bridge = None
            expected_target_id = str(record.get("target_id") or "").strip()
            sockets = _mcp_cdp_page_ws_urls(debug)
            for ws_url in sockets if browser_bridge is not None else ():
                self._cancelled(cancel)
                matched_attached_video = False
                try:
                    target_id = _cdp_page_target_id(ws_url)
                    if expected_target_id and target_id != expected_target_id:
                        continue
                    if expected_target_id:
                        _mcp_set_cdp_window_state(
                            client,
                            profile.profile_id,
                            target_id,
                            "parked",
                        )
                    _bridge, bridge_tab_id = _browser_extension_tab_for_target(
                        client,
                        profile.profile_id,
                        target_id,
                        bridge=browser_bridge,
                    )
                    with _CdpSession(ws_url) as cdp:
                        controller = _SemanticCdpController(
                            cdp,
                            browser_bridge=browser_bridge,
                            bridge_profile_id=profile.profile_id,
                            bridge_tab_id=bridge_tab_id,
                            automation_target_id=target_id,
                            window_state=lambda state: _mcp_set_cdp_window_state(
                                client,
                                profile.profile_id,
                                target_id,
                                state,
                            ),
                        )
                        controller.enable()
                        if "studio.youtube.com" not in controller.page_url().casefold():
                            continue
                        # A detached Content-list row is not enough evidence:
                        # every workspace may render to the same default name
                        # (for example ``Video 4.mp4``). Only an exact target
                        # still owned by this checkpoint may attempt resume;
                        # compatibility checkpoints without target ownership
                        # are deliberately left for manual handling.
                        resumed_interrupted_upload = False
                        if expected_target_id:
                            resumed_interrupted_upload = self._resume_interrupted_upload_locked(
                                request,
                                sources,
                                record,
                                profile,
                                str(record.get("channel_name") or profile.name),
                                self._request_identity(request, sources),
                                attempt_id,
                                controller,
                                report,
                                cancel,
                            )
                        if resumed_interrupted_upload:
                            # The file input was re-bound only after the
                            # semantic Continue-upload control matched the
                            # exact checkpoint filename and its local size was
                            # verified below.  Continue through the normal
                            # metadata/transfer path without creating a new
                            # upload attempt.
                            matched_attached_video = True
                        elif expected_target_id:
                            # New attempts durably store the exact app-owned
                            # target before the one file attachment. The upload
                            # wizard may not have materialized its title editor
                            # yet (the common slow-proxy/MFK failure), so exact
                            # target ownership is the proof here; requiring a
                            # textbox would make that recoverable state
                            # impossible to resume.
                            matched_attached_video = True
                        else:
                            # Compatibility path for checkpoints created before
                            # exact target ownership was persisted.
                            attached = controller.attached_video()
                            actual_name = unicodedata.normalize(
                                "NFC", str(attached.get("name") or "")
                            ).casefold()
                            try:
                                actual_size = int(attached.get("size") or 0)
                            except (TypeError, ValueError):
                                actual_size = 0
                            if actual_name != expected_name or actual_size != expected_size:
                                continue
                        matched_attached_video = True
                        current = self._registry_get(self._request_identity(request, sources))
                        if str(current.get("attempt_id") or "") != attempt_id:
                            raise YouTubeUploadUncertain(
                                "Khóa lượt upload đã thay đổi trong lúc khôi phục. Tool dừng và "
                                "không gắn lại video."
                            )
                        channel_name = str(
                            current.get("channel_name")
                            or record.get("channel_name")
                            or profile.name
                        )
                        self._registry_update_attempt(
                            self._request_identity(request, sources),
                            attempt_id,
                            {
                                "status": "uploading",
                                "resumed_at": _now(),
                                "updated_at": _now(),
                            },
                        )
                        self._checkpoint(
                            request,
                            sources,
                            status="uploading",
                            progress=25,
                            detail=(
                                (
                                    "Đã xác minh đúng tab automation sở hữu lượt gắn file · "
                                    if expected_target_id
                                    else "Đã xác minh đúng video đang gắn bằng tên và dung lượng · "
                                )
                                + "tiếp tục mà không upload lại file"
                            ),
                            extra={
                                "attempt_id": attempt_id,
                                "profile_name": profile.name,
                                "channel_name": channel_name,
                                "resumed": True,
                                "resumed_interrupted_upload": resumed_interrupted_upload,
                                "target_id": target_id,
                            },
                        )
                        controller.mark_upload_started()
                        report(25, "Đã khôi phục đúng phiên cũ · không gắn lại video")
                        return self._continue_attached_upload(
                            request,
                            sources,
                            profile,
                            channel_name,
                            self._request_identity(request, sources),
                            attempt_id,
                            controller,
                            client,
                            debug,
                            report,
                            cancel,
                        )
                except YouTubeUploadUncertain:
                    raise
                except (OSError, TimeoutError, YouTubeUploadError):
                    if matched_attached_video:
                        raise
                    continue
            report(
                6,
                "Tab upload cũ không còn · đang dò tối đa 10 video mới nhất bằng "
                "tên, thời gian và thời lượng",
            )
            discovered_url = self._discover_unique_recent_draft_url(
                request,
                sources,
                record,
                client,
                profile,
                debug,
                report,
                cancel,
            )
            if discovered_url:
                identity = self._request_identity(request, sources)
                channel_name = str(record.get("channel_name") or profile.name)
                updates: dict[str, object] = {
                    "status": "submitting",
                    "youtube_url": discovered_url,
                    "transfer_complete": True,
                    "draft_recovered": True,
                    "updated_at": _now(),
                }
                self._registry_update_attempt(identity, attempt_id, updates)
                discovered_record = {**record, **updates}
                self._checkpoint(
                    request,
                    sources,
                    status="submitting",
                    progress=97,
                    detail=(
                        "Đã nhận lại đúng một bản nháp khớp tên/thời gian/thời lượng; "
                        "chuẩn bị điền và xác minh toàn bộ metadata"
                    ),
                    extra={
                        "attempt_id": attempt_id,
                        "profile_name": profile.name,
                        "channel_name": channel_name,
                        "youtube_url": discovered_url,
                        "transfer_complete": True,
                        "draft_recovered": True,
                    },
                )
        if discovered_record is not None:
            return self._reconcile_submitted_locked(
                request,
                sources,
                discovered_record,
                report,
                cancel,
            )
        if browser_bridge is not None and expected_target_id and not any(
                _cdp_page_target_id(ws) == expected_target_id for ws in sockets):
            self._registry_update_attempt(self._request_identity(request, sources), attempt_id,
                                          {"recovery_requires_manual": True, "updated_at": _now()})
            raise YouTubeUploadRecoveryPaused(
                "Tab upload cũ đã mất và chưa xác định được duy nhất bản nháp. "
                "Đã dừng tự mở profile; giữ checkpoint để đối chiếu, không upload mới."
            )
        raise YouTubeUploadUncertain(
            "Không còn tab YouTube giữ video đã gắn và chưa tìm được đúng một bản nháp "
            "khớp đồng thời tên, thời gian, thời lượng. Tool không đoán video gần nhất "
            "và không upload lại file."
        )

    def _repair_allocated_video_locked(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        client: DonutMcpClient,
        profile: DonutProfile,
        debug: dict[str, Any],
        youtube_url: str,
        report: ProgressCallback,
        cancel: threading.Event,
    ) -> None:
        """Verify one allocated ID in Content, then fill and reload its metadata."""

        repair_ws = _mcp_open_minimized_cdp_window_ws_url(
            client,
            profile.profile_id,
            debug,
            "about:blank",
        )
        repair_target = _cdp_page_target_id(repair_ws)
        try:
            _mcp_set_cdp_window_state(
                client,
                profile.profile_id,
                repair_target,
                "parked",
            )
            repair_bridge = _ensure_youtube_browser_bridge(
                client,
                profile.profile_id,
            )
            _repair_bridge, repair_tab_id = _browser_extension_tab_for_target(
                client,
                profile.profile_id,
                repair_target,
                bridge=repair_bridge,
            )
            with _CdpSession(repair_ws) as repair_cdp:
                repair_controller = _SemanticCdpController(
                    repair_cdp,
                    browser_bridge=repair_bridge,
                    bridge_profile_id=profile.profile_id,
                    bridge_tab_id=repair_tab_id,
                    automation_target_id=repair_target,
                    window_state=lambda state: _mcp_set_cdp_window_state(
                        client,
                        profile.profile_id,
                        repair_target,
                        state,
                    ),
                )
                repair_controller.enable()
                self._prepare_exact_draft_edit_target(
                    repair_controller,
                    youtube_url,
                    cancel,
                    report,
                )
                with _youtube_metadata_ui_guard(cancel, report):
                    report(
                        97.7,
                        "Đang rà soát và điền đủ metadata trên đúng video đã tải lên…",
                    )
                    self._persist_and_verify_saved_metadata(
                        request,
                        sources,
                        repair_controller,
                        youtube_url,
                        report,
                        cancel,
                    )
        finally:
            _mcp_close_cdp_page_target(
                client,
                profile.profile_id,
                debug,
                repair_target,
            )

    def _reconcile_submitted_locked(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        record: dict[str, object],
        report: ProgressCallback,
        cancel: threading.Event,
    ) -> YouTubeUploadResult:
        """Read one submitted Studio dialog without repeating any mutation."""

        ownership: dict[str, bool] = {}
        try:
            return self._reconcile_submitted_locked_inner(
                request, sources, record, report, cancel, ownership=ownership
            )
        except (YouTubeUploadRecoveryPaused, YouTubeUploadTransferStalled) as exc:
            self._park_failed_recovery_profile(request, sources, exc, report)
            raise
        finally:
            self._close_completed_recovery_profile(request, sources, ownership)

    def _reconcile_submitted_locked_inner(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        record: dict[str, object],
        report: ProgressCallback,
        cancel: threading.Event,
        *,
        ownership: dict[str, bool],
    ) -> YouTubeUploadResult:

        self._cancelled(cancel)
        identity = self._request_identity(request, sources)
        completion_status = self._completion_status(request)
        draft_handoff_after = self._draft_handoff_after_seconds(
            self._source_video_duration_seconds(sources.video_path)
        )
        attempt_id = str(record.get("attempt_id") or "").strip()
        if not attempt_id:
            raise YouTubeUploadUncertain(
                "Lượt Lưu cũ thiếu mã sở hữu; tool không bấm hoặc upload lại video."
            )
        registered = self._registry_get(identity)
        if (registered.get("attempt_id") == attempt_id
                and registered.get("profile_id") == request.profile_id):
            # The final-submit checkpoint intentionally writes fewer fields.
            # Its durable registry still owns the raw CDP target and URL;
            # retain those handles so recovery can read exact-ID server
            # SUCCESS rather than being stranded in a bridge-only wait.
            record = dict(record)
            for field in ("target_id", "bridge_tab_id", "youtube_url"):
                if not record.get(field) and registered.get(field):
                    record[field] = registered[field]
        known_url = str(record.get("youtube_url") or "").strip()
        expected_title = " ".join(sources.metadata.title.casefold().split())
        try:
            expected_tab_id = int(record.get("bridge_tab_id") or 0)
        except (TypeError, ValueError):
            expected_tab_id = 0
        report(8, "Đang đọc lại đúng hộp YouTube đã bấm Lưu · chỉ kiểm tra trạng thái…")

        with DonutMcpClient(request.endpoint, timeout=40) as client:
            client.require_docurandom_local()
            profile = client.get_profile(request.profile_id)
            if not profile.process_id:
                # A closed profile has no live upload tab left to protect.  If
                # the old checkpoint already owns an allocated video ID, it is
                # safe to reopen the same profile on Studio's Content page for
                # a read-only existence check.  Older checkpoints often have
                # ``transfer_complete=False`` only because the browser was
                # closed before the first confirmation; refusing to reopen
                # them made a recoverable draft look permanently blocked.  The
                # Content-first URL is deliberate: it never submits a second
                # upload, and the exact edit page is opened only after the
                # existing video is found.
                known_video_id = _youtube_video_id(known_url)
                if known_video_id:
                    if record.get("transfer_complete") is True:
                        reopen_url = (
                            f"https://studio.youtube.com/video/{known_video_id}/edit"
                        )
                        report(9, "Đang mở lại profile để sửa đúng video đã truyền xong…")
                    else:
                        # /content serves Studio's generic error screen. Let
                        # the signed-in bootstrap resolve the selected channel;
                        # the exact-ID Content probe below then opens that
                        # channel's real /videos/upload route without a file.
                        reopen_url = YOUTUBE_STUDIO_URL
                        report(
                            9,
                            "Profile đã đóng · mở lại trang Nội dung để kiểm tra read-only "
                            "đúng video cũ, không upload lại…",
                        )
                    launch_result = client.run_profile(
                        profile.profile_id,
                        reopen_url,
                        retry_entitlement_after_user_action=request.manual_profile_recheck,
                    )
                    ownership["opened"] = (
                        str(launch_result or "").strip().casefold() != "already-running"
                    )
                    if ownership["opened"]:
                        self._registry_update_attempt(
                            self._request_identity(request, sources), str(record.get("attempt_id") or ""),
                            {"profile_started_by_workflow": True},
                        )
                    reopen_deadline = time.monotonic() + 120.0
                    while time.monotonic() < reopen_deadline:
                        self._cancelled(cancel)
                        profile = client.get_profile(request.profile_id)
                        if profile.process_id:
                            break
                        time.sleep(0.5)
                if not profile.process_id:
                    if not known_video_id:
                        self._registry_update_attempt(identity, attempt_id,
                                                      {"recovery_requires_manual": True, "updated_at": _now()})
                        raise YouTubeUploadRecoveryPaused(
                            "Profile chứa lượt Lưu cũ đã đóng và checkpoint chưa có mã video. "
                            "Dừng retry tự động, giữ file và khóa chống trùng để đối chiếu bản nháp."
                        )
                    raise YouTubeUploadUncertain(
                        "Profile chứa lượt Lưu cũ đã đóng. Checkpoint và khóa chống trùng vẫn "
                        "được giữ; tool không mở lượt upload mới."
                    )

            # New checkpoints own both the exact CDP target and extension tab.
            # Recover that target first so a stale Leave-site prompt can be
            # cancelled, the completed upload summary can be closed, and the
            # same allocated video can be repaired without another upload.
            expected_target_id = str(record.get("target_id") or "").strip()
            if (expected_target_id and known_url and record.get('transfer_complete') is True
                    and record.get('transfer_requires_content_resume') is not True):
                debug = client.get_debug_connection(profile.profile_id)
                owned_ws = next((ws for ws in _mcp_cdp_page_ws_urls(debug)
                    if _cdp_page_target_id(ws) == expected_target_id), '')
                if owned_ws:
                    bridge = _ensure_youtube_browser_bridge(client, profile.profile_id)
                    bridge, owned_tab_id = _browser_extension_tab_for_target(
                        client, profile.profile_id, expected_target_id, bridge=bridge)
                    _mcp_set_cdp_window_state(client, profile.profile_id, expected_target_id, 'parked')
                    with _CdpSession(owned_ws) as owned_cdp:
                        owned_controller = _SemanticCdpController(owned_cdp,
                            browser_bridge=bridge, bridge_profile_id=profile.profile_id,
                            bridge_tab_id=owned_tab_id, automation_target_id=expected_target_id)
                        owned_controller.enable()
                        self._commit_transferred_private_draft(owned_controller, known_url,
                            record, report, cancel)
            if (
                expected_target_id
                and expected_tab_id
                and record.get("transfer_requires_content_resume") is not True
                and not (
                    known_url
                    and record.get("transfer_complete") is True
                )
            ):
                debug = client.get_debug_connection(profile.profile_id)
                owned_ws = next(
                    (
                        ws_url
                        for ws_url in _mcp_cdp_page_ws_urls(debug)
                        if _cdp_page_target_id(ws_url) == expected_target_id
                    ),
                    "",
                )
                if owned_ws:
                    browser_bridge = _ensure_youtube_browser_bridge(
                        client,
                        profile.profile_id,
                    )
                    _mcp_set_cdp_window_state(
                        client,
                        profile.profile_id,
                        expected_target_id,
                        "parked",
                    )
                    with _CdpSession(owned_ws) as cdp:
                        controller = _SemanticCdpController(
                            cdp,
                            browser_bridge=browser_bridge,
                            bridge_profile_id=profile.profile_id,
                            bridge_tab_id=expected_tab_id,
                            automation_target_id=expected_target_id,
                            window_state=lambda state: _mcp_set_cdp_window_state(
                                client,
                                profile.profile_id,
                                expected_target_id,
                                state,
                            ),
                        )
                        if controller.cancel_pending_leave_site_dialog():
                            report(9, "Đã hủy hộp Leave site cũ và giữ nguyên tab upload")
                            time.sleep(0.5)
                        controller.enable()
                        owned_url = controller.youtube_url() or known_url
                        if owned_url:
                            def probe_owned_content_status() -> str:
                                return self._probe_exact_video_in_content(
                                    controller.youtube_url() or owned_url,
                                    client,
                                    profile,
                                    debug,
                                    report,
                                    cancel,
                                )

                            # A recovery attempt used to sample the old upload
                            # dialog only once. Large files, a parked Wayfern
                            # renderer, or a slow proxy could therefore return
                            # ``uncertain`` while bytes were still moving. The
                            # upload lease was then released and the next
                            # workspace started another transfer, producing a
                            # cascade of identical warnings. Keep this exact
                            # tab/attempt in the same upload lane and wait for
                            # the same stable transfer boundary used by a fresh
                            # upload. Cancellation remains the escape hatch.
                            owned_url, owned_complete = (
                                self._wait_for_upload_transfer_complete(
                                    controller,
                                    report,
                                    cancel,
                                    timeout=_YOUTUBE_TRANSFER_HARD_TIMEOUT_SECONDS,
                                    draft_content_probe=probe_owned_content_status,
                                    draft_probe_interval=_YOUTUBE_CONTENT_PROBE_INTERVAL_SECONDS,
                                    stall_warn_after=draft_handoff_after,
                                    draft_handoff_after=draft_handoff_after,
                                )
                            )
                            owned_url = owned_url or known_url
                            if owned_url:
                                known_url = owned_url
                            if not owned_complete:
                                detail = (
                                    "YouTube vẫn đang tải tệp trong đúng tab cũ; tool "
                                    "không rời trang và không upload lại"
                                )
                                self._registry_update_attempt(
                                    identity,
                                    attempt_id,
                                    {
                                        "status": "uncertain",
                                        "youtube_url": owned_url,
                                        "bridge_tab_id": expected_tab_id,
                                        "target_id": expected_target_id,
                                        "transfer_complete": False,
                                        "updated_at": _now(),
                                    },
                                )
                                self._checkpoint(
                                    request,
                                    sources,
                                    status="uncertain",
                                    progress=96,
                                    detail=detail,
                                    extra={
                                        "attempt_id": attempt_id,
                                        "profile_name": str(
                                            record.get("profile_name") or profile.name
                                        ),
                                        "channel_name": str(
                                            record.get("channel_name") or profile.name
                                        ),
                                        "youtube_url": owned_url,
                                        "bridge_tab_id": expected_tab_id,
                                        "target_id": expected_target_id,
                                        "transfer_complete": False,
                                    },
                                )
                                raise YouTubeUploadUncertain(
                                    f"{detail}. Hãy bấm Tiếp tục sau; checkpoint vẫn được giữ."
                                )
                            # The transfer is proven. Keep the exact URL, then
                            # leave this Polymer upload target untouched and
                            # continue through the fresh edit-target recovery
                            # below. This also upgrades checkpoints produced by
                            # older releases without clicking their unreliable
                            # processing-dialog Close button.
                            known_url = owned_url
                            record["transfer_complete"] = True
                            self._registry_update_attempt(
                                identity,
                                attempt_id,
                                {
                                    "status": "submitting",
                                    "youtube_url": owned_url,
                                    "transfer_complete": True,
                                    "updated_at": _now(),
                                },
                            )
                            self._checkpoint(
                                request,
                                sources,
                                status="submitting",
                                progress=97,
                                detail=(
                                    "Đã xác minh lượt cũ truyền tệp hoàn tất; mở target mới "
                                    "để lưu metadata, không upload lại file"
                                ),
                                extra={
                                    "attempt_id": attempt_id,
                                    "profile_name": str(
                                        record.get("profile_name") or profile.name
                                    ),
                                    "channel_name": str(
                                        record.get("channel_name") or profile.name
                                    ),
                                    "youtube_url": owned_url,
                                    "bridge_tab_id": expected_tab_id,
                                    "transfer_complete": True,
                                },
                            )
                            report(
                                12,
                                "Lượt cũ đã tải xong · chuyển sang target xác minh riêng",
                            )

            # A submitted checkpoint with an allocated URL can be repaired by
            # editing that exact video. This never attaches the source file or
            # creates a second upload, and lets old 2.4.2 "Saving" checkpoints
            # recover from their empty-draft metadata bug.
            if known_url and _youtube_video_id(known_url):
                # If the owned upload tab still exists, first prove that its
                # real file transfer is over. An allocated URL alone is not a
                # safe navigation boundary and would trigger beforeunload.
                if (
                    expected_tab_id and record.get("transfer_complete") is not True
                    and record.get("transfer_requires_content_resume") is not True
                ):
                    try:
                        pending_bridge = _ensure_youtube_browser_bridge(
                            client,
                            profile.profile_id,
                        )
                        pending_tabs = pending_bridge.command(
                            profile.profile_id,
                            "tabs_list",
                            timeout=8,
                        )
                        pending_tab_exists = any(
                            isinstance(item, dict)
                            and isinstance(item.get("id"), int)
                            and item.get("id") == expected_tab_id
                            for item in (
                                pending_tabs if isinstance(pending_tabs, list) else []
                            )
                        )
                        if pending_tab_exists:
                            pending_controller = _SemanticCdpController(
                                None,  # type: ignore[arg-type]
                                browser_bridge=pending_bridge,
                                bridge_profile_id=profile.profile_id,
                                bridge_tab_id=expected_tab_id,
                            )

                            def probe_pending_content_status() -> str:
                                return self._probe_exact_video_in_content(
                                    known_url,
                                    client,
                                    profile,
                                    debug,
                                    report,
                                    cancel,
                                )

                            pending_url, pending_complete = (
                                self._wait_for_upload_transfer_complete(
                                    pending_controller,
                                    report,
                                    cancel,
                                    timeout=_YOUTUBE_TRANSFER_HARD_TIMEOUT_SECONDS,
                                    draft_content_probe=probe_pending_content_status,
                                    draft_probe_interval=_YOUTUBE_CONTENT_PROBE_INTERVAL_SECONDS,
                                    stall_warn_after=draft_handoff_after,
                                    draft_handoff_after=draft_handoff_after,
                                )
                            )
                            if pending_url:
                                known_url = pending_url
                            if not pending_complete:
                                detail = (
                                    "YouTube vẫn đang tải tệp trong đúng tab cũ; tool "
                                    "không rời trang và không upload lại"
                                )
                                self._registry_update_attempt(
                                    identity,
                                    attempt_id,
                                    {
                                        "status": "uncertain",
                                        "youtube_url": known_url,
                                        "bridge_tab_id": expected_tab_id,
                                        "transfer_complete": False,
                                        "updated_at": _now(),
                                    },
                                )
                                self._checkpoint(
                                    request,
                                    sources,
                                    status="uncertain",
                                    progress=96,
                                    detail=detail,
                                    extra={
                                        "attempt_id": attempt_id,
                                        "profile_name": str(
                                            record.get("profile_name") or profile.name
                                        ),
                                        "channel_name": str(
                                            record.get("channel_name") or profile.name
                                        ),
                                        "youtube_url": known_url,
                                        "bridge_tab_id": expected_tab_id,
                                        "transfer_complete": False,
                                    },
                                )
                                raise YouTubeUploadUncertain(
                                    f"{detail}. Hãy bấm Tiếp tục sau; checkpoint vẫn được giữ."
                                )
                    except BrowserExtensionBridgeError as exc:
                        raise YouTubeUploadUncertain(
                            "Không đọc được trạng thái truyền tệp của đúng tab YouTube cũ. "
                            "Tool không chuyển trang hoặc upload lại để tránh làm hỏng bản nháp."
                        ) from exc
                debug = client.get_debug_connection(profile.profile_id)
                repair_ws = _mcp_open_minimized_cdp_window_ws_url(
                    client,
                    profile.profile_id,
                    debug,
                    "about:blank",
                )
                repair_target = _cdp_page_target_id(repair_ws)
                try:
                    _mcp_set_cdp_window_state(
                        client,
                        profile.profile_id,
                        repair_target,
                        "parked",
                    )
                    repair_bridge = _ensure_youtube_browser_bridge(
                        client,
                        profile.profile_id,
                    )
                    _repair_bridge, repair_tab_id = _browser_extension_tab_for_target(
                        client,
                        profile.profile_id,
                        repair_target,
                        bridge=repair_bridge,
                    )
                    with _CdpSession(repair_ws) as cdp:
                        controller = _SemanticCdpController(
                            cdp,
                            browser_bridge=repair_bridge,
                            bridge_profile_id=profile.profile_id,
                            bridge_tab_id=repair_tab_id,
                            automation_target_id=repair_target,
                            window_state=lambda state: _mcp_set_cdp_window_state(
                                client,
                                profile.profile_id,
                                repair_target,
                                state,
                            ),
                        )
                        controller.enable()
                        resumed_content_upload = False

                        def resume_content_upload_if_needed() -> bool:
                            nonlocal resumed_content_upload
                            if record.get("transfer_complete") is True:
                                return False
                            resumed_content_upload = self._resume_existing_content_upload_locked(
                                request,
                                sources,
                                record,
                                profile,
                                str(record.get("channel_name") or profile.name),
                                identity,
                                attempt_id,
                                controller,
                                report,
                                cancel,
                            )
                            return resumed_content_upload

                        self._prepare_exact_draft_edit_target(
                            controller,
                            known_url,
                            cancel,
                            report,
                            on_content_loaded=resume_content_upload_if_needed,
                        )
                        if resumed_content_upload:
                            return self._continue_attached_upload(
                                request,
                                sources,
                                profile,
                                str(record.get("channel_name") or profile.name),
                                identity,
                                attempt_id,
                                controller,
                                client,
                                debug,
                                report,
                                cancel,
                            )
                        with _youtube_metadata_ui_guard(cancel, report):
                            self._persist_and_verify_saved_metadata(
                                request,
                                sources,
                                controller,
                                known_url,
                                report,
                                cancel,
                            )
                    # Tab IDs from a stopped browser belong to the old
                    # process. Closing that number in a new process can hit
                    # an unrelated tab or produce a false cleanup warning.
                    if expected_tab_id and not ownership.get("opened"):
                        try:
                            cleanup_bridge = _ensure_youtube_browser_bridge(
                                client,
                                profile.profile_id,
                            )
                            cleanup_bridge.command(
                                profile.profile_id,
                                "close_tab",
                                {"tab_id": expected_tab_id},
                                timeout=8,
                            )
                        except BrowserExtensionBridgeError:
                            logger.warning(
                                "youtube_recovered_upload_tab_cleanup_failed "
                                "profile_suffix=%s tab_id=%s",
                                profile.profile_id[-6:],
                                expected_tab_id,
                            )
                    profile_name = str(record.get("profile_name") or profile.name)
                    channel_name = str(record.get("channel_name") or profile.name)
                    self._registry_update_attempt(
                        identity,
                        attempt_id,
                        {
                            "status": completion_status,
                            "youtube_url": known_url,
                            "confirmed_privacy": self._effective_privacy(request),
                            "transfer_complete": True,
                            "updated_at": _now(),
                        },
                    )
                    self._checkpoint(
                        request,
                        sources,
                        status=completion_status,
                        progress=100,
                        detail=(
                            "Đã mở lại đúng video và xác minh metadata đã lưu thật; "
                            "không upload lại file"
                        ),
                        extra={
                            "attempt_id": attempt_id,
                            "profile_name": profile_name,
                            "channel_name": channel_name,
                            "youtube_url": known_url,
                            "confirmed_privacy": self._effective_privacy(request),
                        },
                    )
                    report(100, "Đã sửa và xác minh đúng bản nháp cũ · không đăng trùng")
                    return YouTubeUploadResult(
                        profile_id=profile.profile_id,
                        profile_name=profile_name,
                        channel_name=channel_name,
                        video_path=sources.video_path,
                        package_path=sources.package_path,
                        youtube_url=known_url,
                        thumbnail_path=sources.thumbnail_path,
                        deferred_public=completion_status == "deferred_public",
                        privacy_status=self._effective_privacy(request),
                    )
                finally:
                    _mcp_close_cdp_page_target(
                        client,
                        profile.profile_id,
                        debug,
                        repair_target,
                    )

            bridge = _ensure_youtube_browser_bridge(client, profile.profile_id)
            tabs = bridge.command(profile.profile_id, "tabs_list", timeout=8)
            candidates = [
                item
                for item in (tabs if isinstance(tabs, list) else [])
                if isinstance(item, dict)
                and isinstance(item.get("id"), int)
                and "studio.youtube.com" in str(item.get("url") or "").casefold()
            ]
            if expected_tab_id:
                candidates.sort(key=lambda item: int(item.get("id") or 0) != expected_tab_id)

            selected_tab_id = 0
            selected_url = known_url
            for item in candidates:
                self._cancelled(cancel)
                tab_id = int(item.get("id") or 0)
                try:
                    link = bridge.command(
                        profile.profile_id,
                        "youtube_query",
                        {"tab_id": tab_id, "selector": "#video-link"},
                        timeout=8,
                    )
                    link_value = ""
                    if isinstance(link, dict) and link.get("found"):
                        for key in ("href", "text", "value"):
                            candidate_url = str(link.get(key) or "").strip()
                            if "youtu.be/" in candidate_url or "youtube.com/watch" in candidate_url:
                                link_value = candidate_url
                                break
                    title = bridge.command(
                        profile.profile_id,
                        "youtube_get_text",
                        {"tab_id": tab_id, "field": "title"},
                        timeout=8,
                    )
                    title_value = (
                        " ".join(str(title.get("value") or "").casefold().split())
                        if isinstance(title, dict) and title.get("found")
                        else ""
                    )
                    dialog_state = bridge.command(
                        profile.profile_id,
                        "youtube_query",
                        {"tab_id": tab_id, "selector": "ytcp-uploads-dialog"},
                        timeout=8,
                    )
                    dialog_text = (
                        " ".join(str(dialog_state.get("text") or "").casefold().split())
                        if isinstance(dialog_state, dict) and dialog_state.get("found")
                        else ""
                    )
                except BrowserExtensionBridgeError:
                    continue
                if (
                    (expected_tab_id and tab_id == expected_tab_id)
                    or (known_url and link_value == known_url)
                    or (not known_url and title_value == expected_title)
                    or (not known_url and expected_title and expected_title in dialog_text)
                ):
                    selected_tab_id = tab_id
                    selected_url = link_value or known_url
                    break

            if not selected_tab_id:
                if known_url and _youtube_server_submission_matches(
                    known_url,
                    sources.metadata,
                    self._effective_privacy(request),
                ):
                    profile_name = str(record.get("profile_name") or profile.name)
                    channel_name = str(record.get("channel_name") or profile.name)
                    self._registry_update_attempt(
                        identity,
                        attempt_id,
                        {
                            "status": completion_status,
                            "youtube_url": known_url,
                            "confirmed_privacy": self._effective_privacy(request),
                            "updated_at": _now(),
                        },
                    )
                    self._checkpoint(
                        request,
                        sources,
                        status=completion_status,
                        progress=100,
                        detail=(
                            "Máy chủ YouTube đã xác nhận đủ metadata sau khi cửa sổ "
                            "upload được đóng"
                        ),
                        extra={
                            "attempt_id": attempt_id,
                            "profile_name": profile_name,
                            "channel_name": channel_name,
                            "youtube_url": known_url,
                            "confirmed_privacy": self._effective_privacy(request),
                        },
                    )
                    report(100, "YouTube đã hoàn tất; cửa sổ upload riêng đã được đóng")
                    return YouTubeUploadResult(
                        profile_id=profile.profile_id,
                        profile_name=profile_name,
                        channel_name=channel_name,
                        video_path=sources.video_path,
                        package_path=sources.package_path,
                        youtube_url=known_url,
                        thumbnail_path=sources.thumbnail_path,
                        deferred_public=completion_status == "deferred_public",
                        privacy_status=self._effective_privacy(request),
                    )
                raise YouTubeUploadUncertain(
                    "Không tìm thấy đúng tab của lượt Lưu cũ. Tool giữ checkpoint và không "
                    "bấm Lưu hoặc upload lại; hãy giữ/mở đúng profile YouTube Studio."
                )

            dialog = bridge.command(
                profile.profile_id,
                "youtube_query",
                {"tab_id": selected_tab_id, "selector": "ytcp-uploads-dialog"},
                timeout=8,
            )
            saving = bool(
                isinstance(dialog, dict)
                and dialog.get("found")
                and dialog.get("laidOut") is not False
            )
            common = {
                "profile_name": str(record.get("profile_name") or profile.name),
                "channel_name": str(record.get("channel_name") or profile.name),
                "youtube_url": selected_url,
                "bridge_tab_id": selected_tab_id,
                "target_id": str(record.get("target_id") or ""),
            }
            transfer_complete = False
            if selected_url:
                selected_controller = _SemanticCdpController(
                    None,  # type: ignore[arg-type]
                    browser_bridge=bridge,
                    bridge_profile_id=profile.profile_id,
                    bridge_tab_id=selected_tab_id,
                )
                transfer_complete = selected_controller.youtube_submission_complete()
            if not transfer_complete and selected_url:
                # The upload tab can retain a stale Polymer dialog even after
                # the server has committed the file as a draft. Confirm that
                # exact draft from a separate parked Studio target before
                # returning another uncertain checkpoint.
                try:
                    candidate_debug = client.get_debug_connection(profile.profile_id)
                    discovered = self._probe_exact_video_in_content(
                        selected_url,
                        client,
                        profile,
                        candidate_debug,
                        report,
                        cancel,
                    )
                except Exception:
                    logger.warning(
                        "youtube_recent_draft_probe_failed",
                        exc_info=True,
                    )
                    discovered = ""
                if (
                    discovered
                    and _youtube_video_id(discovered) == _youtube_video_id(selected_url)
                ):
                    selected_url = discovered
                    transfer_complete = True
                    report(
                        97,
                        "Đã xác nhận đúng bản nháp đã tải xong · chuyển sang sửa metadata",
                    )
            if not transfer_complete or not selected_url:
                detail = (
                    "YouTube vẫn đang tải tệp trong đúng bản nháp; tool giữ tab và không upload lại"
                    if saving or selected_url
                    else "Hộp upload đã đổi trạng thái nhưng chưa có link xác nhận"
                )
                self._registry_update_attempt(
                    identity,
                    attempt_id,
                    {
                        "status": "uncertain",
                        "youtube_url": selected_url,
                        "bridge_tab_id": selected_tab_id,
                        "updated_at": _now(),
                    },
                )
                self._checkpoint(
                    request,
                    sources,
                    status="uncertain",
                    progress=96,
                    detail=detail,
                    extra={"attempt_id": attempt_id, **common},
                )
                raise YouTubeUploadUncertain(
                    f"{detail}. Checkpoint và khóa chống trùng vẫn được giữ; hãy bấm Tiếp "
                    "tục để kiểm tra lại sau."
                )

            # A closed Saving overlay or a public watch-page match is not a
            # complete proof: those surfaces cannot verify thumbnail,
            # audience or the altered/synthetic-content declaration. Open the
            # exact allocated video, fill every missing field, Save once, then
            # reload that edit URL and compare all fields before success.
            debug = client.get_debug_connection(profile.profile_id)
            self._registry_update_attempt(
                identity,
                attempt_id,
                {
                    "status": "submitting",
                    "youtube_url": selected_url,
                    "bridge_tab_id": selected_tab_id,
                    "transfer_complete": True,
                    "updated_at": _now(),
                },
            )
            self._checkpoint(
                request,
                sources,
                status="submitting",
                progress=97,
                detail="Tệp đã tải xong · đang rà soát toàn bộ metadata trên đúng video",
                extra={"attempt_id": attempt_id, "transfer_complete": True, **common},
            )
            self._repair_allocated_video_locked(
                request,
                sources,
                client,
                profile,
                debug,
                selected_url,
                report,
                cancel,
            )

            self._registry_update_attempt(
                identity,
                attempt_id,
                {
                    "status": completion_status,
                    "youtube_url": selected_url,
                    "bridge_tab_id": selected_tab_id,
                    "transfer_complete": True,
                    "updated_at": _now(),
                },
            )
            self._checkpoint(
                request,
                sources,
                status=completion_status,
                progress=100,
                detail=(
                    "Đã tải lại đúng video và xác nhận đủ title, mô tả, SEO, "
                    "thumbnail, đối tượng, AI và quyền riêng tư"
                ),
                extra={"attempt_id": attempt_id, **common},
            )
            try:
                bridge.command(
                    profile.profile_id,
                    "close_tab",
                    {"tab_id": selected_tab_id},
                    timeout=8,
                )
                report(99, "Đã đóng tab/cửa sổ upload riêng sau khi YouTube xác nhận")
            except BrowserExtensionBridgeError:
                logger.warning(
                    "youtube_upload_cleanup_tab_failed profile_suffix=%s tab_id=%s",
                    profile.profile_id[-6:],
                    selected_tab_id,
                )
            report(100, "YouTube đã lưu hoàn tất · không tạo lượt upload thứ hai")
            return YouTubeUploadResult(
                profile_id=profile.profile_id,
                profile_name=str(common["profile_name"]),
                channel_name=str(common["channel_name"]),
                video_path=sources.video_path,
                package_path=sources.package_path,
                youtube_url=selected_url,
                thumbnail_path=sources.thumbnail_path,
                deferred_public=completion_status == "deferred_public",
                privacy_status=self._effective_privacy(request),
            )

    @staticmethod
    def _paid_promotion_choice(controller: _SemanticCdpController) -> bool | None:
        """None means the legacy checkbox UI; preserve either explicit choice."""
        if not callable(getattr(controller, "find_dom", None)):
            return None
        no_choice = controller.find_dom(
            controller.dom_nodes(), attributes={"name": "VIDEO_PAID_PRODUCT_PLACEMENT_NO"},
            require_laid_out=True,
        )
        if no_choice is None:
            return None
        return bool(controller.dom_radio_checked("VIDEO_PAID_PRODUCT_PLACEMENT_NO")
                    or controller.dom_radio_checked("VIDEO_PAID_PRODUCT_PLACEMENT_NOTIFY"))

    @classmethod
    def _ensure_paid_promotion_choice(cls, controller: _SemanticCdpController,
                                      cancel: threading.Event) -> None:
        # Ordinary generated videos have always used the unchecked/no-sponsor
        # default. The new radio group requires that default explicitly; never
        # replace an existing Yes or No declaration.
        if cls._paid_promotion_choice(controller) is False:
            controller.select_dom_radio("VIDEO_PAID_PRODUCT_PLACEMENT_NO",
                                        timeout=30, cancel_event=cancel)
            if cls._paid_promotion_choice(controller) is not True:
                raise YouTubeUploadError("YouTube chưa xác nhận khai báo nội dung quảng cáo; chưa được lưu metadata.")

    @staticmethod
    def _ensure_advanced_edit_fields(
        controller: _SemanticCdpController,
        cancel: threading.Event,
    ) -> None:
        current = controller.find_dom(
            controller.dom_nodes(),
            attributes={"name": "VIDEO_HAS_ALTERED_CONTENT_YES"},
            require_laid_out=True,
        )
        if current is not None:
            return
        toggle = controller.wait_dom(
            attributes={"id": "toggle-button"},
            timeout=60,
            require_laid_out=True,
            cancel_event=cancel,
        )
        controller.press(toggle)
        try:
            controller.wait_dom(
                attributes={"name": "VIDEO_HAS_ALTERED_CONTENT_YES"},
                timeout=5,
                require_laid_out=True,
                cancel_event=cancel,
            )
            return
        except YouTubeUploadError:
            if not controller.has_background_bridge:
                raise
        # The edit page's custom ytcp-button does not consistently activate
        # from an Enter key while its dedicated window is off-screen. This is
        # a reversible disclosure control, so use the paired extension's exact
        # semantic selector as a fallback; no screen coordinate or user cursor
        # is involved.
        controller._bridge_command(
            "youtube_click",
            {"selector": "ytcp-video-metadata-editor #toggle-button"},
            timeout=10,
        )
        controller.wait_dom(
            attributes={"name": "VIDEO_HAS_ALTERED_CONTENT_YES"},
            timeout=30,
            require_laid_out=True,
            cancel_event=cancel,
        )

    @staticmethod
    def _open_edit_visibility(
        controller: _SemanticCdpController,
        cancel: threading.Event,
    ) -> bool:
        visibility_names = (
            "Edit video visibility status",
            "visibility status",
            "chế độ hiển thị",
            "trạng thái hiển thị",
            "公開設定",
        )
        bridge_visibility_selectors = (
            "ytcp-video-metadata-visibility #select-button",
            "ytcp-video-metadata-visibility #visibility-button",
            "ytcp-video-metadata-visibility button",
        )
        bridge_radio_selectors = (
            '[name="PRIVATE"]',
            '[name="UNLISTED"]',
            '[name="PUBLIC"]',
        )
        # The current /video/<id>/edit details page does not render the
        # upload-wizard visibility editor at all.  Probe the live DOM first so
        # a missing privacy widget is treated as "not applicable to this
        # draft-details route", rather than waiting 60 seconds for an AX
        # button which cannot exist.  The draft remains unpublished; privacy
        # is still carried in the checkpoint for the later publish step.
        if controller.has_background_bridge is True:
            shell_found = False
            radio_found = False
            for selector in (*bridge_visibility_selectors, *bridge_radio_selectors):
                try:
                    state = controller._bridge_command(
                        "youtube_query",
                        {"selector": selector},
                        timeout=8,
                    )
                except YouTubeUploadError:
                    continue
                if not isinstance(state, dict) or not state.get("found"):
                    continue
                if selector in bridge_visibility_selectors:
                    shell_found = True
                else:
                    radio_found = True
            if not shell_found and not radio_found:
                logger.info(
                    "youtube_edit_visibility_not_applicable route=video_edit"
                )
                return False
        # YouTube sometimes renders the visibility control in the live DOM but
        # omits its custom ytcp-button from the Accessibility tree.  The old
        # implementation let the AX timeout escape here, so a perfectly valid
        # saved draft stayed in the previous visibility state forever.  Keep
        # trusted CDP input as the first choice, then use the paired extension
        # only to activate the exact shadow-DOM control when AX is incomplete.
        button: dict[str, Any] | None = None
        ax_error: YouTubeUploadError | None = None
        try:
            button = controller.wait_for(
                role="button",
                names=visibility_names,
                contains=True,
                timeout=60,
                cancel_event=cancel,
            )
        except YouTubeUploadError as exc:
            ax_error = exc

        def bridge_open_visibility() -> None:
            if not controller.has_background_bridge:
                if ax_error is not None:
                    raise ax_error
                raise YouTubeUploadError(
                    "YouTube Studio không mở được bảng quyền riêng tư của video."
                )
            last_error: YouTubeUploadError | None = ax_error
            for selector in bridge_visibility_selectors:
                try:
                    state = controller._bridge_command(
                        "youtube_query",
                        {"selector": selector},
                        timeout=8,
                    )
                    if not isinstance(state, dict) or not state.get("found"):
                        continue
                    controller._bridge_command(
                        "youtube_click",
                        {"selector": selector},
                        timeout=10,
                    )
                    return
                except YouTubeUploadError as exc:
                    last_error = exc
            try:
                controller._bridge_command(
                    "youtube_click",
                    {"names": list(visibility_names)},
                    timeout=10,
                )
                return
            except YouTubeUploadError as exc:
                last_error = exc
            if last_error is not None:
                raise last_error
            raise YouTubeUploadError(
                "YouTube Studio không tìm thấy điều khiển quyền riêng tư trong DOM."
            )

        if button is None:
            bridge_open_visibility()
        else:
            try:
                controller.press(button)
            except YouTubeUploadError as exc:
                if not controller.has_background_bridge:
                    raise
                ax_error = exc
                bridge_open_visibility()
        try:
            controller.wait_dom(
                attributes={"name": "PRIVATE"},
                timeout=8,
                # Studio implements these choices as custom paper radio
                # buttons.  They are present, focusable and stateful while
                # DOM.getBoxModel still reports no box in an off-screen owned
                # window.  Presence is the reliable menu-open signal here.
                require_laid_out=False,
                cancel_event=cancel,
            )
            return True
        except YouTubeUploadError:
            if not controller.has_background_bridge:
                raise
        current_state = controller._bridge_command(
            "youtube_query",
            {"selector": "[name='PRIVATE']"},
            timeout=8,
        )
        if (
            isinstance(current_state, dict)
            and current_state.get("found")
        ):
            return
        controller._bridge_command(
            "youtube_click",
            {"selector": "ytcp-video-metadata-visibility #select-button"},
            timeout=10,
        )
        controller.wait_dom(
            attributes={"name": "PRIVATE"},
            timeout=30,
            require_laid_out=False,
            cancel_event=cancel,
        )
        return True

    def _commit_transferred_private_draft(self, controller, youtube_url, record, report, cancel):
        """Finish only the existing, fully transferred draft; never attach a file.

        Enter on Polymer Done could merely focus the button. Recovery then
        edited a draft page without a visibility widget forever. Commit that
        same wizard PRIVATE first; the normal fresh-page metadata/Unlisted
        verification still decides completion afterward.
        """
        video_id = _youtube_video_id(youtube_url)
        if (record.get('transfer_complete') is not True or not record.get('attempt_id')
                or not video_id or _youtube_video_id(str(record.get('youtube_url') or '')) != video_id):
            return False
        dialog = controller.find_dom(controller.dom_nodes(),
            node_names=('YTCP-UPLOADS-DIALOG',), attributes={'video-id': video_id}, require_laid_out=True)
        if dialog is None:
            # A reopened profile has new target IDs. YouTube exposes its
            # uncommitted draft through an exact edit page's Edit draft banner.
            # Never open that wizard from a different video or a generic page.
            route = urllib.parse.urlsplit(str(controller.page_url()))
            if route.netloc != 'studio.youtube.com' or route.path != f'/video/{video_id}/edit':
                return False
            edit_draft = controller.find(controller.nodes(), role='button',
                names=('Edit draft', 'Chỉnh sửa bản nháp', '下書きを編集'))
            if edit_draft is None:
                return False
            self._cancelled(cancel)
            # This is an actual native BUTTON from AX. Keyboard activation
            # reaches its handler even when a parked window drops pointer
            # input. The wizard ID below is still the authoritative proof.
            controller.press(edit_draft)
            controller.wait_dom(node_names=('YTCP-UPLOADS-DIALOG',),
                attributes={'video-id': video_id}, timeout=30,
                require_laid_out=True, cancel_event=cancel)
        report(97, 'Đang hoàn tất đúng bản nháp đã truyền xong ở trạng thái Riêng tư; không tải tệp mới')
        self._advance_upload_wizard_to_visibility(controller, report, cancel)
        dialog = controller.find_dom(controller.dom_nodes(),
            node_names=('YTCP-UPLOADS-DIALOG',), attributes={'video-id': video_id})
        if dialog is None:
            raise YouTubeUploadUncertain('Bản nháp đã đổi trong lúc phục hồi; giữ mã video, chưa xác nhận hoàn tất.')
        controller.select_dom_radio('PRIVATE', timeout=30, cancel_event=cancel)
        done = controller.wait_dom(attributes={'id': 'done-button'}, timeout=30,
            require_enabled=True, require_laid_out=True, cancel_event=cancel)
        self._cancelled(cancel)
        controller.click_dom(done)
        # Studio can acknowledge Done before its proxied save response closes
        # the dialog. Keep observing this same save, never click Done again.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self._cancelled(cancel)
            current = controller.find_dom(controller.dom_nodes(),
                node_names=('YTCP-UPLOADS-DIALOG',), attributes={'video-id': video_id}, require_laid_out=True)
            if current is None:
                return True
            cancel.wait(.3)
        raise YouTubeUploadUncertain('Đã bấm Lưu Riêng tư cho đúng bản nháp nhưng chưa xác nhận đóng; giữ receipt, chưa hoàn tất upload.')

    def _set_edit_visibility(
        self,
        controller: _SemanticCdpController,
        privacy: str,
        cancel: threading.Event,
        *,
        public_deadline_at: datetime | None = None,
    ) -> None:
        if privacy not in {"PRIVATE", "UNLISTED", "PUBLIC"}:
            raise YouTubeUploadError(
                f"Quyền riêng tư YouTube không được hỗ trợ: {privacy}."
            )
        if not self._open_edit_visibility(controller, cancel):
            return
        if controller.dom_radio_checked(privacy):
            controller.press_escape()
            return
        # Studio no longer assigns stable IDs to every visibility choice.  In
        # the current UI only PRIVATE has ``#private-radio-button`` while the
        # PUBLIC and UNLISTED hosts have no ID at all.  ``name`` is the shared,
        # stateful attribute across all three choices and is also what the
        # verification path reads below.
        option_selector = f'[name="{privacy}"]'
        option = controller.wait_dom(
            attributes={"name": privacy},
            timeout=30,
            require_laid_out=False,
            cancel_event=cancel,
        )
        _check_publication_deadline(public_deadline_at)
        try:
            controller.press(option, space=True)
        except YouTubeUploadError as exc:
            # Current Studio builds expose the stateful radio host in the DOM,
            # but that host is not always keyboard-focusable in an off-screen
            # automation window. Fall back to the exact paired-extension
            # selector without moving the user's cursor or activating a tab.
            if (
                not controller.has_background_bridge
                or "element is not focusable" not in str(exc).casefold()
            ):
                raise
            self._cancelled(cancel)
            _check_publication_deadline(public_deadline_at)
            controller._bridge_command(
                "youtube_click",
                {"selector": option_selector},
                timeout=10,
            )
        selected_deadline = time.monotonic() + 5.0
        while time.monotonic() < selected_deadline:
            self._cancelled(cancel)
            if controller.dom_radio_checked(privacy):
                break
            time.sleep(0.2)
        else:
            if not controller.has_background_bridge:
                raise YouTubeUploadError(
                    f"Không chọn được quyền riêng tư YouTube {privacy}."
                )
            self._cancelled(cancel)
            _check_publication_deadline(public_deadline_at)
            controller._bridge_command(
                "youtube_click",
                {"selector": option_selector},
                timeout=10,
            )
            verify_deadline = time.monotonic() + 8.0
            while time.monotonic() < verify_deadline:
                self._cancelled(cancel)
                if controller.dom_radio_checked(privacy):
                    break
                time.sleep(0.2)
            else:
                raise YouTubeUploadError(
                    f"Không xác nhận được quyền riêng tư YouTube {privacy}."
                )
        try:
            done = controller.wait_for(
                role="button",
                names=("Done", "Xong", "Hoàn tất", "完了"),
                timeout=30,
                require_enabled=True,
                cancel_event=cancel,
            )
            # The popup must commit and close before the page-level Save.
            # Enter can only focus this custom button, leaving its modal over
            # the editor and intercepting subsequent Save input.
            self._cancelled(cancel)
            _check_publication_deadline(public_deadline_at)
            controller.click_dom(done)
        except YouTubeUploadError:
            if not controller.has_background_bridge:
                raise
            # In a parked automation window Studio keeps the visibility popup
            # functional but excludes its controls from the accessibility tree.
            # The stable popup-local host is #save-button (labelled "Done").
            # Activate that exact tab/element through the paired bridge; the
            # page-level Save below remains the authoritative persistence step.
            done_state = controller._bridge_command(
                "youtube_query",
                {"selector": "ytcp-video-visibility-edit-popup #save-button"},
                timeout=8,
            )
            if not isinstance(done_state, dict) or not done_state.get("found"):
                raise
            if bool(done_state.get("disabled")) or (
                str(done_state.get("ariaDisabled") or "").casefold() == "true"
            ):
                raise YouTubeUploadError(
                    "Nút Done của quyền riêng tư YouTube đang bị khóa."
                )
            self._cancelled(cancel)
            _check_publication_deadline(public_deadline_at)
            controller._bridge_command(
                "youtube_click",
                {"selector": "ytcp-video-visibility-edit-popup #save-button"},
                timeout=10,
            )

    def _verify_edit_visibility(
        self,
        controller: _SemanticCdpController,
        privacy: str,
        cancel: threading.Event,
    ) -> bool:
        if not self._open_edit_visibility(controller, cancel):
            return False
        matches = controller.dom_radio_checked(privacy)
        controller.press_escape()
        return matches

    @staticmethod
    def _wait_edit_save_complete(
        controller: _SemanticCdpController,
        cancel: threading.Event,
        *,
        timeout: float = 30.0,
    ) -> bool:
        deadline = time.monotonic() + max(1.0, timeout)
        started_at = time.monotonic()
        disabled_since = 0.0
        pointer_save_sent = False
        while time.monotonic() < deadline:
            YouTubeStudioUploader._cancelled(cancel)
            node = controller.find_dom(
                controller.dom_nodes(),
                attributes={"id": "save"},
                require_laid_out=True,
            )
            attrs = controller._dom_attributes(node) if node is not None else {}
            disabled = (
                "disabled" in attrs
                or attrs.get("aria-disabled", "false").casefold() == "true"
            )
            if disabled:
                if not disabled_since:
                    disabled_since = time.monotonic()
                elif time.monotonic() - disabled_since >= 1.5:
                    return True
            else:
                disabled_since = 0.0
                # Enter can focus a custom ytcp-button without activating it.
                # Use one trusted click on its current verified DOM box; a
                # page-side synthetic click can animate without committing.
                if (
                    not pointer_save_sent
                    and node is not None
                    and time.monotonic() - started_at >= 3.0
                ):
                    controller.click_dom(node)
                    pointer_save_sent = True
            time.sleep(0.25)
        logger.warning(
            "youtube_edit_save_ui_confirmation_timeout; checking persisted "
            "metadata in an independent exact-video target"
        )
        # Studio sometimes accepts the save without changing #save's enabled
        # state. The caller immediately reloads the exact video URL and
        # verifies persisted fields; that readback is the authoritative gate.
        # Returning False lets that stronger check distinguish a saved draft
        # from a genuinely unsaved one without repeating the upload.
        return False

    @staticmethod
    def _wait_for_upload_transfer_complete(
        controller: _SemanticCdpController,
        report: ProgressCallback,
        cancel: threading.Event,
        *,
        timeout: float = _YOUTUBE_TRANSFER_HARD_TIMEOUT_SECONDS,
        poll_interval: float = 0.5,
        stable_for: float = 3.0,
        stall_warn_after: float = 10 * 60,
        stall_notice_interval: float = 5 * 60,
        stall_timeout: float = 30 * 60,
        draft_content_probe: Callable[[], str] | None = None,
        completed_draft_probe: Callable[[], str] | None = None,
        draft_probe_interval: float = _YOUTUBE_CONTENT_PROBE_INTERVAL_SECONDS,
        draft_handoff_after: float | None = None,
    ) -> tuple[str, bool]:
        """Wait for the real file-transfer boundary, not merely a video URL.

        Studio allocates ``#video-link`` while the upload summary can still be
        at (for example) 53%. Navigating at that point raises Chromium's
        beforeunload dialog and can strand an empty private draft. Completion
        must therefore remain true for a short stable window while an exact
        video URL is also present. While that widget is stale, the optional
        ``draft_content_probe`` opens a fresh Content target every few minutes
        to inspect the exact saved ID and report whether YouTube is still
        uploading/processing. It can hand off only after that exact row is
        ready; ``completed_draft_probe`` remains a compatibility alias for
        older callers.
        """

        started_at = time.monotonic()
        deadline = started_at + max(1.0, timeout)
        youtube_url = ""
        completion_since = 0.0
        percent_complete_observed = False
        link_reported = False
        next_wait_report = 0.0
        next_progress_probe = 0.0
        progress_probe_interval = min(
            2.0,
            max(0.01, max(0.0, stable_for) / 2.0),
        )
        last_percent: float | None = None
        last_remaining = ""
        # A tab can remain alive while the network/CDP progress counter stops
        # changing.  Keep waiting (a slow proxy is still recoverable), but do
        # not leave the user believing that a 96% transfer is healthy forever.
        last_progress_at = time.monotonic()
        next_stall_notice = last_progress_at + max(1.0, stall_warn_after)
        content_probe = draft_content_probe or completed_draft_probe
        content_probe_interval = max(0.01, float(draft_probe_interval))
        if draft_handoff_after is not None:
            first_content_probe_after = min(
                content_probe_interval,
                max(0.01, float(draft_handoff_after)),
            )
        else:
            # Preserve the old stall-based behavior for external/test callers
            # that only provide the compatibility callback.
            first_content_probe_after = min(
                content_probe_interval,
                max(0.01, float(stall_warn_after)),
            )
        next_content_probe = started_at + first_content_probe_after
        content_probe_count = 0
        # A bridge-only controller is used when the exact browser target was
        # parked/closed but its extension tab still owns the upload dialog.
        # It can read the live transfer state, but it has no raw CDP socket on
        # which to run page/AX error probes. The initial upload and exact-target
        # recovery still perform those probes; bridge-only recovery must not
        # fail before it can observe the percentage reaching 100%.
        can_probe_cdp_errors = getattr(controller, "cdp", None) is not None
        while time.monotonic() < deadline:
            YouTubeStudioUploader._cancelled(cancel)
            if can_probe_cdp_errors:
                controller.raise_for_page_error()
                controller.raise_for_upload_error()
            youtube_url = controller.youtube_url() or youtube_url
            now = time.monotonic()
            if youtube_url and not link_reported:
                report(
                    96,
                    "YouTube đã cấp link tạm thời · tệp vẫn đang tải, chưa rời trang",
                )
                link_reported = True
                next_wait_report = now + 20.0

            submission_complete_now = controller.youtube_submission_complete()
            if submission_complete_now:
                if not completion_since:
                    completion_since = now
            elif not percent_complete_observed:
                # A generic/temporary completion candidate must remain true
                # throughout the stability window. Previously one transient
                # true result stayed sticky forever and could turn 47% or 56%
                # into a completed upload three seconds later.
                completion_since = 0.0

            if now >= next_progress_probe:
                percent, remaining = controller.youtube_upload_progress()
                if percent is not None and percent >= 100.0:
                    # Studio can replace/hide the 100% status almost
                    # immediately. Keep this authoritative byte-transfer proof
                    # sticky instead of demanding that one transient DOM node
                    # remain visible for the whole stability window.
                    percent_complete_observed = True
                    if not completion_since:
                        completion_since = now
                elif percent is not None:
                    # A live percentage below 100 is direct proof that bytes
                    # are still being transferred. It overrides any stale
                    # processing/checks overlay seen in the same iteration.
                    percent_complete_observed = False
                    submission_complete_now = False
                    completion_since = 0.0
                progress_changed = (
                    percent is not None
                    and (
                        last_percent is None
                        or abs(percent - last_percent) >= 5.0
                        or remaining != last_remaining
                    )
                )
                if progress_changed:
                    percent_label = (
                        str(int(percent))
                        if percent is not None and percent.is_integer()
                        else f"{percent:.1f}"
                    )
                    detail = f"YouTube đang tải tệp · {percent_label}%"
                    if remaining:
                        detail += f" · còn {remaining}"
                    report(96, detail)
                    last_percent = percent
                    last_remaining = remaining
                    last_progress_at = now
                    next_stall_notice = now + max(1.0, stall_warn_after)
                    next_wait_report = now + 20.0
                # Probe at least once inside the completion stability window.
                # Otherwise a stale completion overlay can remain unchallenged
                # long enough to win while the visible counter is still <100%.
                next_progress_probe = now + progress_probe_interval

            if (
                (percent_complete_observed or submission_complete_now)
                and youtube_url
                and now - completion_since >= max(0.0, stable_for)
            ):
                report(
                    97,
                    "YouTube đã tải xong tệp · bắt đầu xác minh dữ liệu đã lưu",
                )
                return youtube_url, True

            # Keep checking a fresh Content target every few minutes. This is
            # intentionally independent from the upload dialog's potentially
            # stale percentage: the probe is read-only and the exact ID is the
            # only identifier allowed to authorize a later handoff.
            if (
                content_probe is not None
                and youtube_url
                and now >= next_content_probe
            ):
                content_probe_count += 1
                elapsed_minutes = max(1, int((now - started_at) // 60))
                report(
                    96,
                    f"Đến lần kiểm tra Nội dung #{content_probe_count} "
                    f"sau khoảng {elapsed_minutes} phút · giữ nguyên tab upload",
                )
                try:
                    probed_url = str(content_probe() or "").strip()
                except Exception:
                    logger.warning(
                        "youtube_content_probe_failed",
                        exc_info=True,
                    )
                    probed_url = ""
                    report(
                        96,
                        "Tab Nội dung chưa đọc được lần này · giữ tab upload, "
                        "sẽ kiểm tra lại sau 3–4 phút",
                    )
                next_content_probe = time.monotonic() + content_probe_interval
                expected_id = _youtube_video_id(youtube_url)
                probed_id = _youtube_video_id(probed_url)
                if (
                    probed_url and (not expected_id or probed_id == expected_id)
                    and not percent_complete_observed
                ):
                    feedback_reader = getattr(controller, "server_upload_progress", None)
                    feedback = feedback_reader(expected_id or probed_id) if callable(feedback_reader) else (None, "")
                    server_percent = feedback[0] if isinstance(feedback, tuple) and len(feedback) == 2 else None
                    processing_handoff = False
                    if server_percent is None:
                        # Require two independent observations for a parked
                        # original dialog whose hosts no longer have boxes:
                        # the fresh exact-ID Content row and its dedicated
                        # terminal processing status. A missing percentage or
                        # Content row alone still cannot authorize handoff.
                        delay_reader = getattr(controller, "youtube_parked_transfer_status_complete", None)
                        live_percent, _ = controller.youtube_upload_progress()
                        if live_percent is None and callable(delay_reader):
                            processing_handoff = delay_reader(expected_id or probed_id) is True
                    if (server_percent is None and not processing_handoff) or (
                        server_percent is not None and server_percent < 100
                    ):
                        report(96, "Bản nháp đã xuất hiện nhưng chưa có bằng chứng truyền đủ tệp · giữ nguyên tab upload")
                        continue
                if probed_url and (not expected_id or probed_id == expected_id):
                    report(
                        97,
                        "Đã thấy đúng bản nháp đã tải xong trong Nội dung · đóng luồng upload cũ và sửa metadata",
                    )
                    return probed_url, True
                if probed_url and expected_id and probed_id != expected_id:
                    logger.warning(
                        "youtube_content_probe_id_mismatch expected=%s discovered=%s",
                        expected_id,
                        probed_id,
                    )
                    report(
                        96,
                        "Tab Nội dung trả về ID khác checkpoint · bỏ qua kết quả và giữ tab upload",
                    )

            if link_reported and now >= next_wait_report:
                report(
                    96,
                    "YouTube vẫn đang tải tệp · tool giữ nguyên tab và tiếp tục chờ",
                )
                next_wait_report = now + 20.0

            if (
                link_reported
                and now >= next_stall_notice
                and not (
                    (percent_complete_observed or submission_complete_now)
                    and youtube_url
                )
            ):
                # The periodic Content probe above is the only draft proof
                # used here. Do not run a second heuristic search at the stall
                # boundary, because it could inspect a different row and it
                # would defeat the intended 3–4 minute cadence.
                stalled_for = max(1, int((now - last_progress_at) // 60))
                report(
                    96,
                    "YouTube tạm đứng · chưa thấy tiến triển trong "
                    f"{stalled_for} phút; tool vẫn giữ nguyên tab và không upload lại",
                )
                next_stall_notice = now + max(1.0, stall_notice_interval)

            # Do not occupy an upload lane forever when the tab is alive but
            # the network/proxy has stopped delivering bytes.  Returning an
            # incomplete result lets the caller persist the exact attempt and
            # schedule a safe retry; it never authorizes a second attachment.
            if (
                (link_reported or last_percent is not None)
                and now - last_progress_at >= max(0.01, stall_timeout)
            ):
                stalled_for = max(1, int((now - last_progress_at) // 60))
                report(
                    96,
                    "YouTube upload không có tiến triển trong "
                    f"{stalled_for} phút; đã giữ checkpoint để tự thử lại đúng tab, "
                    "không upload lại file",
                )
                return youtube_url, False
            cancel.wait(max(0.01, poll_interval))

        return youtube_url, False

    @contextmanager
    def _metadata_readback_controller(
        self, request, sources, controller, youtube_url, cancel, *, force_fresh=False,
    ):
        """Read persisted values without discarding a genuinely unsaved editor."""
        video_id = _youtube_video_id(youtube_url)
        if not video_id:
            raise YouTubeUploadError("Không có đúng video ID để kiểm tra metadata.")
        edit_url = f"https://studio.youtube.com/video/{video_id}/edit"
        if not force_fresh:
            try:
                controller.navigate_and_wait(edit_url, timeout=120, cancel_event=cancel)
            except YouTubeUploadUncertain:
                force_fresh = True
            else:
                yield controller
                return
        # An allocated URL is insufficient. Independent readback is allowed
        # only after this exact lease proves that the file transfer completed.
        record = self._registry_get(self._request_identity(request, sources))
        if record.get("transfer_complete") is not True:
            raise YouTubeUploadUncertain(
                "Tệp chưa xác nhận truyền xong; giữ nguyên tab và checkpoint."
            )
        with DonutMcpClient(request.endpoint, timeout=40) as client:
            profile = client.get_profile(request.profile_id)
            if not profile.running:
                raise YouTubeUploadUncertain("Profile xác minh đã đóng; giữ checkpoint.")
            debug = client.get_debug_connection(request.profile_id)
            ws_url = _mcp_open_minimized_cdp_window_ws_url(
                client, request.profile_id, debug, edit_url,
            )
            target_id = _cdp_page_target_id(ws_url)
            try:
                _mcp_set_cdp_window_state(client, request.profile_id, target_id, "parked")
                bridge, tab_id = _browser_extension_tab_for_target(
                    client, request.profile_id, target_id,
                    bridge=controller.browser_bridge,
                )
                with _CdpSession(ws_url) as cdp:
                    readback = _SemanticCdpController(
                        cdp, browser_bridge=bridge, bridge_profile_id=request.profile_id,
                        bridge_tab_id=tab_id, automation_target_id=target_id,
                    )
                    readback.enable()
                    logger.info("youtube_independent_metadata_readback video_id=%s", video_id)
                    yield readback
            finally:
                _mcp_close_cdp_page_target(client, request.profile_id, debug, target_id)

    def _saved_metadata_mismatches(self, request, sources, controller, cancel):
        controller.wait_dom(
            attributes={"id": "textbox"}, ancestor_ids=("title-textarea",),
            timeout=120, require_laid_out=True, cancel_event=cancel,
        )
        title = controller.named_text("title", timeout=30, require_laid_out=True, cancel_event=cancel)
        description = controller.named_text("description", timeout=30, require_laid_out=True, cancel_event=cancel)
        self._ensure_advanced_edit_fields(controller, cancel)
        mismatches = []
        if title.rstrip("\r\n") != sources.metadata.title.rstrip("\r\n"):
            mismatches.append("title")
        if description.rstrip("\r\n") != sources.metadata.description.rstrip("\r\n"):
            mismatches.append("description")
        if sources.metadata.tags and not controller.tag_labels_match(
            controller.tag_labels(), tuple(sources.metadata.tags),
        ):
            mismatches.append("SEO tags")
        if not controller.dom_radio_checked("VIDEO_MADE_FOR_KIDS_NOT_MFK"):
            mismatches.append("not-made-for-kids")
        if request.contains_synthetic_media and not controller.dom_radio_checked("VIDEO_HAS_ALTERED_CONTENT_YES"):
            mismatches.append("AI declaration")
        if self._paid_promotion_choice(controller) is False:
            mismatches.append("paid promotion declaration")
        if not self._verify_edit_visibility(controller, self._effective_privacy(request), cancel):
            mismatches.append("privacy")
        if sources.thumbnail_path is not None:
            try:
                controller.wait_custom_thumbnail_ready(timeout=45, cancel_event=cancel)
            except YouTubeUploadError:
                mismatches.append("thumbnail")
        return mismatches

    def _persist_and_verify_saved_metadata(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        controller: _SemanticCdpController,
        youtube_url: str,
        report: ProgressCallback,
        cancel: threading.Event,
    ) -> None:
        """Edit the allocated video, reload it, and prove Studio persisted data.

        The upload dialog is not authoritative: its Polymer fields can display
        text that never reaches the saved draft.  A fresh /video/<id>/edit load
        is tied to the exact allocated video and is therefore both safe from
        duplicate uploads and strong enough to verify persisted metadata.
        """

        if self._effective_privacy(request) == "PUBLIC":
            from .publication_assets import valid_thumbnail
            if (sources.thumbnail_path is None or not valid_thumbnail(sources.thumbnail_path)
                    or not sources.metadata.title.strip() or not sources.metadata.description.strip()
                    or not sources.metadata.tags):
                raise YouTubeUploadError("Chưa đủ thumbnail/metadata hợp lệ; không được Public.")
        video_id = _youtube_video_id(youtube_url)
        if not video_id:
            raise YouTubeUploadError(
                "YouTube chưa cấp mã video hợp lệ để xác minh metadata đã lưu."
            )
        percent, _remaining = controller.server_upload_progress(video_id)
        if percent is None or percent >= 100:
            identity = self._request_identity(request, sources)
            record = self._registry_get(identity)
            attempt_id = str(record.get("attempt_id") or "")
            owns_video = bool(attempt_id) and _youtube_video_id(
                str(record.get("youtube_url") or "")
            ) == video_id
            if owns_video and record.get("transfer_requires_content_resume") is True:
                if percent is None:
                    # Content can show a pending draft while feedback is absent.
                    # That cannot reverse positive evidence of a partial file.
                    raise YouTubeUploadUncertain(
                        "Tệp cũ chưa xác nhận truyền xong và YouTube chưa trả tiến độ mới; "
                        "giữ đúng bản nháp, chờ Tiếp tục tải lên, chưa lưu metadata."
                    )
                self._registry_update_attempt(identity, attempt_id, {
                    "transfer_complete": True,
                    "transfer_requires_content_resume": False,
                    "transfer_feedback_percent": percent,
                    "updated_at": _now(),
                })
        if percent is not None and percent < 100:
            identity = self._request_identity(request, sources)
            record = self._registry_get(identity)
            attempt_id = str(record.get("attempt_id") or "")
            if attempt_id and _youtube_video_id(str(record.get("youtube_url") or "")) == video_id:
                stalled = self._record_partial_transfer_feedback(identity, attempt_id, percent)
                self._registry_update_attempt(identity, attempt_id, {
                    "transfer_complete": False,
                    "transfer_feedback_percent": percent,
                    "transfer_requires_content_resume": True,
                    "updated_at": _now(),
                })
                self._checkpoint(
                    request, sources, status="uncertain", progress=96,
                    detail=f"YouTube xác nhận đúng video mới tải {percent:.0f}%; giữ lượt cũ để nối truyền tệp.",
                    extra={
                        **{key: record.get(key) for key in ("profile_name", "channel_name", "target_id", "bridge_tab_id")},
                        "attempt_id": attempt_id, "youtube_url": youtube_url,
                        "transfer_complete": False, "transfer_requires_content_resume": True,
                    },
                )
                if stalled:
                    raise YouTubeUploadTransferStalled(
                        f"Tiến độ máy chủ của đúng video đứng ở {percent:.2f}% ít nhất 15 phút. "
                        "Giữ mã video và file gốc, nhường profile rồi nối lại đúng bản nháp."
                    )
            raise YouTubeUploadUncertain(
                f"YouTube xác nhận đúng video mới tải {percent:.0f}%; chưa lưu metadata. "
                "Checkpoint và mã video được giữ để nối lại đúng lượt truyền tệp."
            )
        edit_url = f"https://studio.youtube.com/video/{video_id}/edit"
        expected_tags = tuple(sources.metadata.tags)
        privacy = self._effective_privacy(request)
        thumbnail_requested = sources.thumbnail_path is not None
        thumbnail_applied = False

        last_mismatches: list[str] = []
        reuse_current_edit = False
        for attempt in range(2):
            self._cancelled(cancel)
            if not reuse_current_edit:
                controller.navigate_and_wait(
                    edit_url, timeout=120, cancel_event=cancel,
                )
            controller.wait_dom(
                attributes={"id": "textbox"},
                ancestor_ids=("title-textarea",),
                timeout=120,
                require_laid_out=True,
                cancel_event=cancel,
            )

            owned_record = self._registry_get(self._request_identity(request, sources))
            if self._commit_transferred_private_draft(controller, youtube_url,
                    owned_record, report, cancel):
                controller.navigate_and_wait(edit_url, timeout=120, cancel_event=cancel)
                controller.wait_dom(attributes={'id': 'textbox'}, ancestor_ids=('title-textarea',),
                    timeout=120, require_laid_out=True, cancel_event=cancel)

            controller.set_named_text(
                "title",
                sources.metadata.title,
                verify_timeout=90,
                cancel_event=cancel,
            )
            controller.set_named_text(
                "description",
                sources.metadata.description,
                verify_timeout=60,
                cancel_event=cancel,
            )
            controller.select_dom_radio(
                "VIDEO_MADE_FOR_KIDS_NOT_MFK",
                timeout=45,
                cancel_event=cancel,
            )
            self._ensure_advanced_edit_fields(controller, cancel)
            self._ensure_paid_promotion_choice(controller, cancel)
            if request.contains_synthetic_media:
                controller.select_dom_radio(
                    "VIDEO_HAS_ALTERED_CONTENT_YES",
                    timeout=45,
                    cancel_event=cancel,
                )
            if expected_tags:
                controller.set_tags(
                    expected_tags,
                    timeout=90,
                    cancel_event=cancel,
                )
            if sources.thumbnail_path is not None and not thumbnail_applied:
                selected_thumbnail = sources.thumbnail_path
                try:
                    controller.set_file(selected_thumbnail, "image")
                    controller.wait_custom_thumbnail_ready(
                        timeout=30,
                        cancel_event=cancel,
                    )
                    thumbnail_applied = True
                except (OSError, YouTubeUploadError) as original_exc:
                    # Try the user's exact file first. Only after Studio does
                    # not confirm it, create one non-destructive compatible
                    # JPEG copy; the original image is never changed.
                    fallback = _youtube_thumbnail_fallback(
                        request.workspace,
                        selected_thumbnail,
                    )
                    logger.warning(
                        "youtube_thumbnail_original_rejected video_id=%s original=%s fallback=%s detail=%s",
                        video_id,
                        selected_thumbnail.name,
                        fallback.name,
                        original_exc,
                    )
                    controller.set_file(fallback, "image")
                    controller.wait_custom_thumbnail_ready(
                        timeout=60,
                        cancel_event=cancel,
                    )
                    thumbnail_applied = True
            self._set_edit_visibility(controller, privacy, cancel)

            # Re-read after every blur/popup transition. This catches the old
            # fake-input failure before the irreversible metadata Save.
            staged_title = controller.named_text(
                "title", timeout=30, require_laid_out=True, cancel_event=cancel
            )
            staged_description = controller.named_text(
                "description", timeout=30, require_laid_out=True, cancel_event=cancel
            )
            if staged_title.rstrip("\r\n") != sources.metadata.title.rstrip("\r\n"):
                raise YouTubeUploadError(
                    "YouTube đã xóa title trước bước Save; tool dừng để không lưu sai."
                )
            if staged_description.rstrip("\r\n") != sources.metadata.description.rstrip("\r\n"):
                raise YouTubeUploadError(
                    "YouTube đã xóa mô tả trước bước Save; tool dừng để không lưu sai."
                )

            save = controller.wait_dom(
                attributes={"id": "save"},
                timeout=60,
                require_laid_out=True,
                cancel_event=cancel,
            )
            save_attrs = controller._dom_attributes(save)
            save_enabled = not (
                "disabled" in save_attrs
                or save_attrs.get("aria-disabled", "false").casefold() == "true"
            )
            save_confirmed = not save_enabled
            if save_enabled:
                controller.click_dom(save)
                save_confirmed = self._wait_edit_save_complete(controller, cancel)

            report(98, "Đang tải lại chính video để xác minh metadata đã lưu thật…")
            with self._metadata_readback_controller(
                request, sources, controller, youtube_url, cancel,
                force_fresh=not save_confirmed,
            ) as readback:
                last_mismatches = self._saved_metadata_mismatches(
                    request, sources, readback, cancel,
                )
                reuse_current_edit = readback is not controller
            if "thumbnail" in last_mismatches:
                thumbnail_applied = False
            if not last_mismatches:
                report(
                    99,
                    "YouTube đã lưu thật và tải lại khớp title, mô tả, SEO, AI, "
                    "đối tượng, quyền riêng tư"
                    + (" và thumbnail" if thumbnail_requested else ""),
                )
                return
            logger.warning(
                "youtube_saved_metadata_mismatch video_id=%s attempt=%d fields=%s",
                video_id,
                attempt + 1,
                ",".join(last_mismatches),
            )

        raise YouTubeUploadError(
            "YouTube tải lại video nhưng metadata vẫn không khớp: "
            + ", ".join(last_mismatches)
        )

    @staticmethod
    def _advance_upload_wizard_to_visibility(
        controller: _SemanticCdpController,
        report: ProgressCallback,
        cancel: threading.Event,
        *,
        timeout: float = 5 * 60,
        allocated_url_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Resume the upload wizard from whichever pre-submit step is live.

        Studio can rebuild the Details page for minutes while a proxied file is
        attaching. A resumed checkpoint may therefore be on Details, Video
        elements, Checks, or Visibility. Detect live semantic controls on each
        pass instead of assuming that exactly three Next clicks remain.
        """

        deadline = time.monotonic() + max(1.0, timeout)
        next_clicks = 0
        last_next_click_at = 0.0
        audience_confirmed = False
        remembered_url = ""
        next_url_probe = 0.0
        while time.monotonic() < deadline:
            YouTubeStudioUploader._cancelled(cancel)
            controller.raise_for_page_error()
            controller.raise_for_upload_error()
            now = time.monotonic()
            if allocated_url_callback is not None and now >= next_url_probe:
                allocated_url = controller.youtube_url()
                if allocated_url and allocated_url != remembered_url:
                    allocated_url_callback(allocated_url)
                    remembered_url = allocated_url
                next_url_probe = now + 3.0
            nodes = controller.dom_nodes()
            private = controller.find_dom(
                nodes,
                attributes={"name": "PRIVATE"},
                require_laid_out=True,
            )
            done = controller.find_dom(
                nodes,
                attributes={"id": "done-button"},
                require_laid_out=True,
            )
            if private is not None or done is not None:
                return

            audience = controller.find_dom(
                nodes,
                attributes={"name": "VIDEO_MADE_FOR_KIDS_NOT_MFK"},
                require_laid_out=True,
            )
            if audience is not None:
                controller.select_dom_radio(
                    "VIDEO_MADE_FOR_KIDS_NOT_MFK",
                    timeout=60,
                    cancel_event=cancel,
                )
                if not audience_confirmed:
                    report(47, "Đã đặt Không dành cho trẻ em")
                    audience_confirmed = True

            next_button = controller.find_dom(
                controller.dom_nodes(),
                attributes={"id": "next-button"},
                require_laid_out=True,
            )
            attrs = controller._dom_attributes(next_button) if next_button else {}
            enabled = bool(
                next_button is not None
                and "disabled" not in attrs
                and attrs.get("aria-disabled", "false").casefold() != "true"
            )
            if enabled and next_clicks < 3:
                controller.press(next_button)
                next_clicks += 1
                last_next_click_at = time.monotonic()
                report(
                    min(79, 52 + next_clicks * 9),
                    "YouTube · đang đi tới bước quyền riêng tư",
                )
                cancel.wait(1.0)
                continue
            if next_clicks >= 3 and time.monotonic() - last_next_click_at >= 15.0:
                raise YouTubeUploadError(
                    "YouTube Studio không chuyển tới bước quyền riêng tư sau ba lệnh Next. "
                    "Tool giữ checkpoint và sẽ nhận diện lại đúng bước, không bấm liên tục."
                )
            cancel.wait(0.5)

        raise YouTubeUploadError(
            "YouTube Studio chưa tải xong các bước chi tiết/đối tượng/quyền riêng tư. "
            "Tool đã giữ đúng tab và checkpoint để tự nối lại, không upload lại file."
        )

    def _try_completed_wizard_handoff(
        self, request, sources, identity, attempt_id, controller, report, cancel,
    ) -> YouTubeUploadResult | None:
        """A broken wizard cannot hold a proven transferred file indefinitely."""
        read_dialog = getattr(controller, "upload_dialog_url", None)
        url = read_dialog() if callable(read_dialog) else ""
        if not isinstance(url, str) or not _youtube_video_id(url):
            return None
        record = self._registry_get(identity)
        if record.get("attempt_id") != attempt_id or record.get("profile_id") != request.profile_id:
            raise YouTubeUploadUncertain("Lượt sở hữu đã đổi trước khi nối metadata; không thao tác thêm.")
        if record.get("youtube_url") != url:
            self._registry_update_attempt(identity, attempt_id, {"youtube_url": url, "updated_at": _now()})
            self._checkpoint(request, sources, status="uploading", progress=25,
                detail="Đã giữ mã video từ đúng dialog upload; chưa coi là truyền xong.",
                extra={"attempt_id": attempt_id, "youtube_url": url,
                       "target_id": controller.automation_target_id, "bridge_tab_id": controller.bridge_tab_id})
        if not controller.youtube_submission_complete():
            return None
        video_id = _youtube_video_id(url)
        percent, _remaining = controller.server_upload_progress(video_id)
        if percent is None or not math.isfinite(percent) or percent < 100:
            return None
        changes = {"status": "submitting", "youtube_url": url, "transfer_complete": True,
                   "transfer_requires_content_resume": False, "metadata_only_recovery": True,
                   "transfer_feedback_percent": float(percent), "transfer_feedback_observed_at": _now(),
                   "updated_at": _now()}
        if any(isinstance(old, dict) and _youtube_video_id(str(old.get("youtube_url") or "")) == video_id
               for old in record.get("replacement_history", [])):
            # YouTube may match the same file and continue its old allocation
            # itself. There is only one actual video in this case. Retain the
            # immutable history and explicitly bind its proved completion.
            changes["replacement_resumed_video_id"] = video_id
        current = self._registry_update_attempt(identity, attempt_id, changes)
        self._checkpoint(request, sources, status="submitting", progress=97,
            detail="Máy chủ xác nhận đúng video truyền xong; chuyển sang xác minh metadata, không gửi file nữa.",
            extra={"attempt_id": attempt_id, "youtube_url": url, "transfer_complete": True,
                   "target_id": controller.automation_target_id, "bridge_tab_id": controller.bridge_tab_id})
        report(97, "Tệp đã truyền xong · nối đúng mã video để xác minh metadata ngoài wizard")
        return self._reconcile_submitted_locked(request, sources, current, report, cancel)

    def _continue_attached_upload(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        profile: DonutProfile,
        channel_name: str,
        identity: str,
        attempt_id: str,
        controller: _SemanticCdpController,
        client: DonutMcpClient,
        debug: dict[str, Any],
        report: ProgressCallback,
        cancel: threading.Event,
    ) -> YouTubeUploadResult:
        """Fill metadata and submit after the one irreversible file attachment."""

        completion_status = self._completion_status(request)
        draft_handoff_after = self._draft_handoff_after_seconds(
            self._source_video_duration_seconds(sources.video_path)
        )

        handoff = self._try_completed_wizard_handoff(
            request, sources, identity, attempt_id, controller, report, cancel)
        if handoff is not None:
            return handoff

        # The upload wizard rebuilds its Polymer textboxes while bytes are
        # being attached. On slower/proxied profiles it can visibly accept
        # title/description and then restore the filename, leaving a blank
        # draft. Do not treat that transient form as authoritative. Keep all
        # metadata in the checkpoint, upload one private draft, then write and
        # reload-verify everything on /video/<exact-id>/edit below.
        report(36, "Đã giữ metadata trong checkpoint · sẽ lưu sau khi tệp tải xong")
        remembered_url = ""

        def remember_allocated_url(youtube_url: str) -> None:
            nonlocal remembered_url
            remembered_url = youtube_url
            self._registry_update_attempt(
                identity,
                attempt_id,
                {
                    "status": "uploading",
                    "youtube_url": youtube_url,
                    "bridge_tab_id": controller.bridge_tab_id,
                    "target_id": controller.automation_target_id,
                    "updated_at": _now(),
                },
            )
            self._checkpoint(
                request,
                sources,
                status="uploading",
                progress=24,
                detail=(
                    "YouTube đã cấp đúng video ID · giữ checkpoint để hoàn tất/"
                    "sửa metadata mà không upload lại"
                ),
                extra={
                    "attempt_id": attempt_id,
                    "profile_name": profile.name,
                    "channel_name": channel_name,
                    "youtube_url": youtube_url,
                    "bridge_tab_id": controller.bridge_tab_id,
                    "target_id": controller.automation_target_id,
                },
            )

        self._advance_upload_wizard_to_visibility(
            controller,
            report,
            cancel,
            allocated_url_callback=remember_allocated_url,
        )

        # The first Save only closes the one upload attempt. Keep that draft
        # private until a fresh edit page has persisted and reloaded all
        # metadata; only then apply the requested final visibility.
        initial_privacy = "PRIVATE"
        controller.select_dom_radio(initial_privacy, timeout=60, cancel_event=cancel)
        report(82, "Đã giữ bản nháp riêng tư để xác minh metadata trước khi phát hành")
        allocated_url = controller.youtube_url() or remembered_url
        self._registry_update_attempt(
            identity,
            attempt_id,
            {
                "status": "uploading",
                "youtube_url": allocated_url,
                "bridge_tab_id": controller.bridge_tab_id,
                "target_id": controller.automation_target_id,
                "updated_at": _now(),
            },
        )
        self._checkpoint(
            request,
            sources,
            status="uploading",
            progress=82,
            detail="Đã điền metadata · chờ YouTube hoàn tất upload ở bản nháp riêng tư",
            extra={
                "attempt_id": attempt_id,
                "profile_name": profile.name,
                "channel_name": channel_name,
                "youtube_url": allocated_url,
                "bridge_tab_id": controller.bridge_tab_id,
                "target_id": controller.automation_target_id,
            },
        )

        done_button = controller.wait_dom(
            attributes={"id": "done-button"},
            timeout=60 * 60,
            require_enabled=True,
            cancel_event=cancel,
        )
        self._registry_update_attempt(
            identity,
            attempt_id,
            {
                "status": "submitting",
                "bridge_tab_id": controller.bridge_tab_id,
                "updated_at": _now(),
            },
        )
        self._checkpoint(
            request,
            sources,
            status="submitting",
            progress=94,
            detail="Đã khóa trước lần bấm Lưu duy nhất · không cho phép bấm lại",
            extra={
                "attempt_id": attempt_id,
                "profile_name": profile.name,
                "channel_name": channel_name,
                # This checkpoint replaces the earlier allocation checkpoint.
                # Retain its exact video and target before Save or cancellation.
                "youtube_url": allocated_url,
                "bridge_tab_id": controller.bridge_tab_id,
                "target_id": controller.automation_target_id,
            },
        )
        controller.click_dom(done_button)
        # Keep the dedicated window parked off-screen throughout transfer.
        # Unlike a fully minimized window this preserves Studio's layout and
        # live progress text, but it never covers or activates the user's tab.
        report(96, "YouTube đã nhận lệnh lưu · đang chờ tệp tải lên hoàn tất thật sự")

        def probe_content_status() -> str:
            # Every probe opens and closes its own parked Content target. The
            # upload target stays untouched until the exact row is ready and
            # the later edit-target preflight has succeeded.
            probe_url = controller.youtube_url() or remembered_url
            return self._probe_exact_video_in_content(
                probe_url,
                client,
                profile,
                debug,
                report,
                cancel,
            )

        youtube_url, submission_complete = self._wait_for_upload_transfer_complete(
            controller,
            report,
            cancel,
            draft_content_probe=probe_content_status,
            draft_probe_interval=_YOUTUBE_CONTENT_PROBE_INTERVAL_SECONDS,
            stall_warn_after=draft_handoff_after,
            draft_handoff_after=draft_handoff_after,
        )
        if youtube_url:
            self._registry_update_attempt(
                identity,
                attempt_id,
                {
                    "status": "submitting",
                    "youtube_url": youtube_url,
                    "bridge_tab_id": controller.bridge_tab_id,
                    "transfer_complete": submission_complete,
                    "updated_at": _now(),
                },
            )
            self._checkpoint(
                request,
                sources,
                status="submitting",
                progress=97 if submission_complete else 96,
                detail=(
                    "Tệp đã tải xong · chuẩn bị đóng hộp upload và xác minh metadata"
                    if submission_complete
                    else "YouTube đã cấp link tạm · tệp chưa tải xong, không rời trang"
                ),
                extra={
                    "attempt_id": attempt_id,
                    "profile_name": profile.name,
                    "channel_name": channel_name,
                    "youtube_url": youtube_url,
                    "bridge_tab_id": controller.bridge_tab_id,
                    "transfer_complete": submission_complete,
                },
            )
        if youtube_url and submission_complete:
            # Do not depend on Studio's foreground ``Close`` control. Current
            # Polymer builds can expose a visible processing dialog whose
            # button reports/clicks successfully but never dismisses while the
            # automation window is parked. The transfer is already proven and
            # the exact video id is durable, so edit that one video in a fresh
            # owned target. The fresh target first verifies the exact ID in the
            # Content list and loads its edit form; only then is the old upload
            # tab handed off and closed.
            report(97.5, "Tệp đã tải xong · mở đúng video trong cửa sổ xác minh riêng")
            repair_ws = _mcp_open_minimized_cdp_window_ws_url(
                client,
                profile.profile_id,
                debug,
                "about:blank",
            )
            repair_target = _cdp_page_target_id(repair_ws)
            try:
                _mcp_set_cdp_window_state(
                    client,
                    profile.profile_id,
                    repair_target,
                    "parked",
                )
                repair_bridge = _ensure_youtube_browser_bridge(
                    client,
                    profile.profile_id,
                )
                _repair_bridge, repair_tab_id = _browser_extension_tab_for_target(
                    client,
                    profile.profile_id,
                    repair_target,
                    bridge=repair_bridge,
                )
                with _CdpSession(repair_ws) as repair_cdp:
                    repair_controller = _SemanticCdpController(
                        repair_cdp,
                        browser_bridge=repair_bridge,
                        bridge_profile_id=profile.profile_id,
                        bridge_tab_id=repair_tab_id,
                        automation_target_id=repair_target,
                        window_state=lambda state: _mcp_set_cdp_window_state(
                            client,
                            profile.profile_id,
                            repair_target,
                            state,
                        ),
                    )
                    repair_controller.enable()
                    self._prepare_exact_draft_edit_target(
                        repair_controller,
                        youtube_url,
                        cancel,
                        report,
                    )
                    self._close_upload_target_after_handoff(
                        controller,
                        client,
                        profile,
                        debug,
                        report,
                    )
                    with _youtube_metadata_ui_guard(cancel, report):
                        report(97.7, "Đang ghi metadata trên đúng video đã tải xong…")
                        self._persist_and_verify_saved_metadata(
                            request,
                            sources,
                            repair_controller,
                            youtube_url,
                            report,
                            cancel,
                        )
            finally:
                _mcp_close_cdp_page_target(
                    client,
                    profile.profile_id,
                    debug,
                    repair_target,
                )
            submission_complete = True
        if not youtube_url or not submission_complete:
            if youtube_url:
                self._registry_update_attempt(
                    identity,
                    attempt_id,
                    {
                        "status": "uncertain",
                        "youtube_url": youtube_url,
                        "bridge_tab_id": controller.bridge_tab_id,
                        "transfer_complete": False,
                        "updated_at": _now(),
                    },
                )
                self._checkpoint(
                    request,
                    sources,
                    status="uncertain",
                    progress=96,
                    detail="YouTube đã cấp link nhưng máy chủ chưa xác nhận đủ metadata",
                    extra={
                        "attempt_id": attempt_id,
                        "profile_name": profile.name,
                        "channel_name": channel_name,
                        "youtube_url": youtube_url,
                        "bridge_tab_id": controller.bridge_tab_id,
                        "transfer_complete": False,
                    },
                )
            detail = (
                "YouTube vẫn chưa bật nút Close vì tệp chưa tải xong sau thời hạn"
                if youtube_url and not submission_complete
                else "tool chưa lấy được đường dẫn xác nhận"
            )
            raise YouTubeUploadUncertain(
                "YouTube đã nhận đúng một lần bấm Lưu nhưng "
                f"{detail}. Tool sẽ không bấm hoặc upload lại; hãy kiểm tra YouTube "
                "Studio trong profile đã chọn."
            )
        self._registry_update_attempt(
            identity,
            attempt_id,
            {
                "status": completion_status,
                "youtube_url": youtube_url,
                "confirmed_privacy": self._effective_privacy(request),
                "updated_at": _now(),
            },
        )
        self._checkpoint(
            request,
            sources,
            status=completion_status,
            progress=99,
            detail="YouTube đã xác nhận lượt upload duy nhất bằng đường dẫn video",
            extra={
                "attempt_id": attempt_id,
                "profile_name": profile.name,
                "channel_name": channel_name,
                "youtube_url": youtube_url,
            },
        )

        result = YouTubeUploadResult(
            profile_id=profile.profile_id,
            profile_name=profile.name,
            channel_name=channel_name,
            video_path=sources.video_path,
            package_path=sources.package_path,
            youtube_url=youtube_url,
            thumbnail_path=sources.thumbnail_path,
            deferred_public=completion_status == "deferred_public",
            privacy_status=self._effective_privacy(request),
        )
        self._checkpoint(
            request,
            sources,
            status=completion_status,
            progress=100,
            detail="YouTube upload hoàn tất",
            extra={
                "attempt_id": attempt_id,
                "profile_name": profile.name,
                "channel_name": channel_name,
                "youtube_url": youtube_url,
            },
        )
        report(100, "Upload YouTube hoàn tất")
        return result

    def _upload_locked(
        self,
        request: YouTubeUploadRequest,
        sources: WorkspaceUploadSources,
        report: ProgressCallback,
        cancel: threading.Event,
    ) -> YouTubeUploadResult:
        identity = self._request_identity(request, sources)
        self._cancelled(cancel)
        report(3, "Đang đọc profile từ Donut…")
        self._checkpoint(request, sources, status="running", progress=3, detail="Đọc profile Donut")
        with DonutMcpClient(request.endpoint, timeout=40) as client:
            client.require_docurandom_local()
            profile = client.get_profile(request.profile_id)
            if profile.browser.casefold() not in {"wayfern", "camoufox"}:
                raise YouTubeUploadError("Profile đã chọn không hỗ trợ bộ điều khiển local.")
            opened_by_upload = False
            try:
                if not profile.process_id:
                    report(5, f"Đang tự mở profile {profile.name} bằng Donut MCP…")
                    launch_result = client.run_profile(
                        profile.profile_id,
                        YOUTUBE_STUDIO_URL,
                        retry_entitlement_after_user_action=request.manual_profile_recheck,
                    )
                    opened_by_upload = (
                        str(launch_result or "").strip().casefold() != "already-running"
                    )
                    deadline = time.monotonic() + 120.0
                    while time.monotonic() < deadline:
                        self._cancelled(cancel)
                        profile = client.get_profile(request.profile_id)
                        if profile.process_id:
                            break
                        time.sleep(0.75)
                    if not profile.process_id:
                        raise YouTubeUploadError("Donut MCP không mở được profile đã chọn.")

                debug = client.get_debug_connection(profile.profile_id)
                with _prepared_youtube_upload_tab(
                    client,
                    profile,
                    debug,
                    report,
                    cancel,
                ) as (controller, channel_name):
                    report(12, "Đang kiểm tra kênh YouTube trong profile…")
                    controller.raise_for_page_error()
                    self._checkpoint(
                        request,
                        sources,
                        status="running",
                        progress=15,
                        detail="Đã xác minh profile/kênh qua bộ điều khiển local",
                        extra={"profile_name": profile.name, "channel_name": channel_name},
                    )

                    identity = self._request_identity(request, sources)
                    attempt_id = uuid.uuid4().hex
                    attempt_started_at = _now()
                    attempt_record: dict[str, object] = {
                        "status": "attaching",
                        "profile_started_by_workflow": opened_by_upload,
                        "attempt_id": attempt_id,
                        "workspace_id": request.workspace.workspace_id,
                        "profile_id": request.profile_id,
                        "profile_name": profile.name,
                        "channel_name": channel_name,
                        "video_path": str(sources.video_path),
                        "video_name": sources.video_path.name,
                        "title": sources.metadata.title,
                        "description": sources.metadata.description,
                        "seo_tags": list(sources.metadata.tags),
                        "thumbnail_path": (
                            str(sources.thumbnail_path)
                            if sources.thumbnail_path is not None
                            else ""
                        ),
                        "made_for_kids": False,
                        "contains_synthetic_media": request.contains_synthetic_media,
                        "privacy": self._effective_privacy(request),
                        "final_privacy": request.privacy,
                        "deferred_public": bool(
                            request.hold_public_until_next_morning
                            and request.privacy == "PUBLIC"
                        ),
                        "youtube_url": "",
                        "target_id": controller.automation_target_id,
                        "bridge_tab_id": controller.bridge_tab_id,
                        "started_at": attempt_started_at,
                        "updated_at": attempt_started_at,
                    }
                    # Attaching the file is the irreversible boundary: YouTube may
                    # create a draft immediately. Persist the global claim before
                    # issuing DOM.setFileInputFiles, then never attach this content
                    # hash to the same profile a second time automatically.
                    existing_claim = self._registry_claim(identity, attempt_record)
                    if existing_claim:
                        if str(existing_claim.get("status") or "").casefold() == "complete":
                            report(100, "Video đã hoàn tất ở lượt khác · không gắn file lần hai")
                            return YouTubeUploadResult(
                                profile_id=request.profile_id,
                                profile_name=str(
                                    existing_claim.get("profile_name") or profile.name
                                ),
                                channel_name=str(
                                    existing_claim.get("channel_name") or channel_name
                                ),
                                video_path=sources.video_path,
                                package_path=sources.package_path,
                                youtube_url=str(existing_claim.get("youtube_url") or ""),
                                thumbnail_path=sources.thumbnail_path,
                                privacy_status=_confirmed_privacy_from_record(existing_claim),
                            )
                        raise YouTubeUploadUncertain(
                            "Một tiến trình khác đã giữ khóa cho chính video/profile này. Tool "
                            "không gắn file và từ chối tạo lượt upload thứ hai."
                        )
                    self._checkpoint(
                        request,
                        sources,
                        status="attaching",
                        progress=20,
                        detail="Đã khóa lượt upload duy nhất · chuẩn bị gắn file vào YouTube",
                        extra={
                            "attempt_id": attempt_id,
                            "profile_name": profile.name,
                            "channel_name": channel_name,
                            "target_id": controller.automation_target_id,
                            "bridge_tab_id": controller.bridge_tab_id,
                        },
                    )
                    controller.mark_upload_started()
                    controller.set_file(sources.video_path, "video")
                    report(24, f"Đã chọn đúng video · {sources.video_path.name}")
                    self._registry_update_attempt(
                        identity,
                        attempt_id,
                        {"status": "uploading", "updated_at": _now()},
                    )
                    self._checkpoint(
                        request,
                        sources,
                        status="uploading",
                        progress=24,
                        detail="Đã gắn file video một lần duy nhất vào YouTube Studio",
                        extra={
                            "attempt_id": attempt_id,
                            "profile_name": profile.name,
                            "channel_name": channel_name,
                            "target_id": controller.automation_target_id,
                            "bridge_tab_id": controller.bridge_tab_id,
                        },
                    )
                    result = self._continue_attached_upload(
                        request,
                        sources,
                        profile,
                        channel_name,
                        identity,
                        attempt_id,
                        controller,
                        client,
                        debug,
                        report,
                        cancel,
                    )
                return result
            finally:
                if opened_by_upload:
                    status = str(self._registry_get(identity).get("status") or "").casefold()
                    # Never tear down an attached/unsaved upload on an error.
                    # Persisted ownership lets its eventual recovery free the
                    # slot, even when that later call found it already open.
                    if status in {"", "complete", "deferred_public", "queued", "running", "claimed"}:
                        self._close_profile_started_by_workflow(client, profile.profile_id)
