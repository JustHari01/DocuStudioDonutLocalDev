from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import io
import os
from pathlib import Path
import re
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

from .workspace import Workspace


MAX_THUMBNAIL_BYTES = 20 * 1024 * 1024
MAX_THUMBNAIL_PIXELS = 60_000_000
_FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}


class ThumbnailError(ValueError):
    pass


@dataclass(frozen=True)
class ThumbnailInspection:
    extension: str
    width: int
    height: int
    size: int
    sha256: str


@dataclass(frozen=True)
class ThumbnailInstallResult:
    path: Path
    inspection: ThumbnailInspection
    archived_paths: tuple[Path, ...] = ()


def inspect_thumbnail_bytes(data: bytes) -> ThumbnailInspection:
    payload = bytes(data)
    if not payload:
        raise ThumbnailError("Ảnh thumbnail đang trống.")
    if len(payload) > MAX_THUMBNAIL_BYTES:
        raise ThumbnailError("Ảnh thumbnail vượt giới hạn nhận 20 MB của Telegram Bot API.")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
            if image_format not in _FORMAT_EXTENSIONS:
                raise ThumbnailError("Chỉ nhận thumbnail JPG, PNG hoặc WEBP.")
            if width < 1 or height < 1:
                raise ThumbnailError("Ảnh thumbnail không có kích thước hợp lệ.")
            if width * height > MAX_THUMBNAIL_PIXELS:
                raise ThumbnailError("Ảnh thumbnail có độ phân giải quá lớn để xử lý an toàn.")
            image.verify()
    except ThumbnailError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ThumbnailError(f"Không đọc được ảnh thumbnail: {exc}") from exc
    return ThumbnailInspection(
        extension=_FORMAT_EXTENSIONS[image_format],
        width=int(width),
        height=int(height),
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def managed_thumbnail_dir(workspace: Workspace) -> Path:
    return workspace.state_dir / "thumbnails"


def _managed_thumbnail_roots(workspace: Workspace) -> tuple[Path, ...]:
    # Keep recognizing the pre-2.3.75 Telegram-only directory so an update can
    # archive or clear old managed images without touching user-owned files.
    return (
        managed_thumbnail_dir(workspace),
        workspace.state_dir / "telegram-thumbnails",
    )


def _safe_history_name(path: Path, *, digest: str = "") -> str:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    clean_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem).strip("-.") or "thumbnail"
    suffix = path.suffix.casefold() if path.suffix else ".img"
    digest_suffix = f"-{digest[:10]}" if digest else ""
    return f"{timestamp}-{clean_stem}{digest_suffix}{suffix}"


def is_managed_thumbnail(workspace: Workspace, path: Path) -> bool:
    resolved = path.resolve(strict=False)
    for root in _managed_thumbnail_roots(workspace):
        try:
            resolved.relative_to(root.resolve(strict=False))
        except ValueError:
            continue
        return True
    return False


def archive_managed_thumbnails(workspace: Workspace) -> tuple[Path, ...]:
    archived: list[Path] = []
    history = managed_thumbnail_dir(workspace) / "history"
    for root in _managed_thumbnail_roots(workspace):
        for candidate in sorted(root.glob("current.*")) if root.is_dir() else ():
            if not candidate.is_file():
                continue
            history.mkdir(parents=True, exist_ok=True)
            target = history / _safe_history_name(candidate)
            os.replace(candidate, target)
            archived.append(target)
    return tuple(archived)


def install_workspace_thumbnail(
    workspace: Workspace,
    data: bytes,
) -> ThumbnailInstallResult:
    inspection = inspect_thumbnail_bytes(data)
    workspace.ensure_layout()
    root = managed_thumbnail_dir(workspace)
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".incoming-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    target = root / f"current{inspection.extension}"
    archived: tuple[Path, ...] = ()
    try:
        temporary.write_bytes(data)
        # Verify the exact bytes written before replacing the active thumbnail.
        written = inspect_thumbnail_bytes(temporary.read_bytes())
        if written.sha256 != inspection.sha256:
            raise ThumbnailError("Ảnh thumbnail thay đổi trong lúc lưu; đã hủy thay thế.")
        archived = archive_managed_thumbnails(workspace)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return ThumbnailInstallResult(
        path=target.resolve(strict=False),
        inspection=inspection,
        archived_paths=archived,
    )


def clear_workspace_thumbnail(
    workspace: Workspace,
    selected_path: Path | None,
) -> tuple[Path, ...]:
    # Never delete a user's original file outside DocuRandom. Clearing an
    # external selection only detaches it; managed Telegram images are moved
    # into history so an accidental remote deletion remains recoverable.
    if selected_path is not None and not is_managed_thumbnail(workspace, selected_path):
        return ()
    return archive_managed_thumbnails(workspace)


def thumbnail_preview_bytes(path: Path, *, max_edge: int = 1280) -> bytes:
    """Return a compact JPEG preview suitable for Telegram sendPhoto."""

    source = path.expanduser().resolve(strict=True)
    try:
        with Image.open(source) as image:
            image.load()
            preview = ImageOps.exif_transpose(image).convert("RGB")
            preview.thumbnail((max(320, int(max_edge)), max(320, int(max_edge))))
            output = io.BytesIO()
            preview.save(output, format="JPEG", quality=88, optimize=True)
            payload = output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ThumbnailError(f"Không tạo được ảnh xem trước thumbnail: {exc}") from exc
    if not payload:
        raise ThumbnailError("Ảnh xem trước thumbnail đang trống.")
    return payload
