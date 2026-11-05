"""Bound local generated media without changing YouTube or upload history.

The latest confirmed upload is retained. An older Public payload is eligible
only after a successor upload is durably confirmed. Pending/uncertain uploads
and live checkpoint references always win over cleanup, including an explicit
legacy-discard policy. Small packages, manifests, prompts and State survive.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
from pathlib import Path
import stat

from .storage import atomic_write_json
from .workspace import Workspace
from .workspace_scheduler import VIETNAM_TIMEZONE, vietnam_now

_LOGGER = logging.getLogger("docurandom.media_retention")
_BULK_DIRECTORIES = ("Output", "Media", "VOICEVOX", "Selected Thumbnail", "Thumbnails")
_MEDIA_SUFFIXES = frozenset({
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts", ".m2ts",
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
})
_LIVE_STATE = (
    "youtube_upload.json", "youtube_archived_recovery.json",
    "manual_youtube_upload.json", "pipeline_checkpoint.json", "flow_history.json",
    "thumbnail-ai.json", "settings.json",
)


@dataclass(frozen=True)
class MediaRetentionResult:
    removed_files: int = 0
    removed_bytes: int = 0
    cleaned_archives: tuple[str, ...] = ()
    protected_archives: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    blocked_reason: str = ""
    dry_run: bool = False


def _read(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return dict(default or {})
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Không đọc được trạng thái dọn dữ liệu: {path.name}")
    return value


def _stamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=VIETNAM_TIMEZONE) if parsed.tzinfo is None else parsed


def _assert_confined(path: Path, root: Path) -> None:
    """Validate both canonical containment and every existing path component.

    Reject junctions/reparse points as well as symlinks, even links that happen
    to point inside the workspace. Recheck immediately before each unlink.
    """
    path.absolute().relative_to(root)
    path.resolve(strict=False).relative_to(root)
    current = root
    for part in (None, *path.relative_to(root).parts):
        if part is not None:
            current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Bỏ qua đường dẫn liên kết: {current}")


def _archive_path(value: object, root: Path, group: str = "Uploaded") -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Kho video thiếu đường dẫn")
    path = Path(raw).absolute()
    _assert_confined(path, root)
    if path.parent != root / group:
        raise ValueError(f"Kho video nằm ngoài {group}: {path}")
    return path


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _payload_files(archive: Path, root: Path) -> list[Path]:
    files = []
    for name in _BULK_DIRECTORIES:
        directory = archive / name
        _assert_confined(directory, root)
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise ValueError(f"Thư mục media không hợp lệ: {directory}")
        # Explicit traversal: pathlib.rglob may silently skip a junction or
        # unreadable folder. Any such uncertainty must retain this archive.
        pending = [directory]
        while pending:
            folder = pending.pop()
            for path in folder.iterdir():
                _assert_confined(path, root)
                if path.is_dir():
                    pending.append(path)
                elif path.is_file() and path.suffix.casefold() in _MEDIA_SUFFIXES:
                    files.append(path)
    return files


def prune_workspace_media(workspace: Workspace, *, dry_run: bool = False) -> MediaRetentionResult:
    """Clean proven obsolete archive media; never touch active root folders.

    ``media_retention_policy.json.discard_archives_before`` is an explicit
    user-authorized one-time legacy cutoff, not inferred from schedule resets
    or generic remote errors. An invalid ledger/checkpoint disables deletion.
    """
    # Lazy import keeps session_archive's confirmation hook free of a cycle.
    from .session_archive import _ARCHIVE_LOCK

    with _ARCHIVE_LOCK:
        try:
            return _prune(workspace, dry_run=dry_run)
        except (OSError, ValueError, UnicodeError) as exc:
            _LOGGER.warning("media_retention_blocked workspace=%s reason=%s", workspace.name, exc)
            return MediaRetentionResult(blocked_reason=str(exc), dry_run=dry_run)


def _prune(workspace: Workspace, *, dry_run: bool) -> MediaRetentionResult:
    root = workspace.root.absolute()
    _assert_confined(root, root)
    state = workspace.state_dir
    _assert_confined(state, root)
    policy = _read(state / "media_retention_policy.json")
    if policy.get("enabled") is False:
        return MediaRetentionResult(blocked_reason="Đã tắt dọn media local", dry_run=dry_run)
    cutoff = _stamp(policy.get("discard_archives_before"))
    if policy.get("discard_archives_before") and cutoff is None:
        raise ValueError("Mốc bỏ kho video cũ không hợp lệ")
    for name in ("media_retention_policy.json", "uploaded_sessions.json", "deferred_public_queue.json"):
        _assert_confined(state / name, root)
    registry = _read(state / "uploaded_sessions.json", {"sessions": {}})
    sessions = registry.get("sessions")
    queue = _read(state / "deferred_public_queue.json", {"items": []}).get("items")
    if not isinstance(sessions, dict) or not all(isinstance(v, dict) for v in sessions.values()):
        raise ValueError("Sổ upload chưa đọc được đầy đủ; giữ nguyên mọi file")
    if not isinstance(queue, list) or not all(isinstance(v, dict) and v.get("identity") for v in queue):
        raise ValueError("Hàng đợi Public chưa đọc được đầy đủ; giữ nguyên mọi file")

    protected_ids = {str(item["identity"]) for item in queue}
    protected_paths = {_archive_path(item.get("archive_dir"), root) for item in queue}
    live_urls = set()
    references = []
    for name in _LIVE_STATE:
        _assert_confined(state / name, root)
        checkpoint = _read(state / name)
        if isinstance(checkpoint.get("identity"), str):
            protected_ids.add(checkpoint["identity"])
        if checkpoint.get("youtube_url"):
            live_urls.add(str(checkpoint["youtube_url"]))
        for raw in _strings(checkpoint):
            candidate = Path(raw)
            if candidate.is_absolute():
                references.append(candidate.resolve(strict=False))

    records = []
    errors = []
    for identity, entry in sessions.items():
        try:
            archive = _archive_path(entry.get("archive_dir"), root)
            _assert_confined(archive / "archive_manifest.json", root)
            manifest = _read(archive / "archive_manifest.json")
            if (manifest.get("schema") != "docurandom-uploaded-session-v1"
                    or manifest.get("archive_id") != identity
                    or not entry.get("youtube_url")
                    or manifest.get("youtube_url") != entry.get("youtube_url")):
                continue
            stamp = _stamp(entry.get("archived_at"))
            if stamp is None or _stamp(manifest.get("archived_at_vietnam")) != stamp:
                continue
            public = (entry.get("status") == "complete"
                      and entry.get("visibility_status") == "PUBLIC"
                      and manifest.get("visibility_status") == "PUBLIC"
                      and (not entry.get("published_at") or _stamp(entry["published_at"]) is not None))
            pending = (entry.get("status") == "pending_public"
                       and entry.get("visibility_status") == "UNLISTED_PENDING_PUBLIC"
                       and manifest.get("visibility_status") == "UNLISTED_PENDING_PUBLIC")
            queued = any(item.get("identity") == identity
                         and item.get("youtube_url") == entry.get("youtube_url")
                         and item.get("privacy") == "UNLISTED"
                         and item.get("final_privacy") == "PUBLIC"
                         for item in queue)
            abandoned = cutoff is not None and stamp < cutoff
            records.append((archive, identity, entry, manifest, stamp, public, pending and queued, abandoned))
        except (OSError, ValueError, UnicodeError) as exc:
            errors.append(f"{identity}: {exc}")

    # Keep all active Unlisted entries, even in legacy multi-entry queues.
    # Pending without a queue is uncertainty, never permission to delete.
    confirmed = [r for r in records if (r[5] or r[6]) and not r[7]]
    newest = max(confirmed, key=lambda r: r[4], default=None)
    if newest is not None:
        # Protect every tie; never select an arbitrary video for deletion.
        protected_paths.update(r[0] for r in confirmed if r[4] == newest[4])

    def protected(archive, identity="", url=""):
        return (archive in protected_paths or identity in protected_ids or url in live_urls
                or any(p.is_relative_to(archive) for p in references))

    eligible = []
    kept = []
    for archive, identity, entry, manifest, stamp, public, pending, abandoned in records:
        published = _stamp(entry.get("published_at")) or stamp
        if protected(archive, identity, str(entry.get("youtube_url"))):
            kept.append(str(archive))
        elif abandoned:
            eligible.append((archive, manifest, "user-discarded-legacy-session"))
        elif public and newest is not None and newest[4] > published:
            eligible.append((archive, manifest, "public-followed-by-confirmed-upload"))
        else:
            kept.append(str(archive))

    skipped_root = root / "Skipped"
    _assert_confined(skipped_root, root)
    if skipped_root.is_dir():
        for archive in skipped_root.iterdir():
            try:
                _archive_path(archive, root, "Skipped")
                if not archive.is_dir():
                    continue
                _assert_confined(archive / "archive_manifest.json", root)
                manifest = _read(archive / "archive_manifest.json")
                if (manifest.get("schema") != "docurandom-skipped-session-v1"
                        or not manifest.get("archive_id")
                        or _stamp(manifest.get("archived_at_vietnam")) is None):
                    continue
                if protected(archive):
                    kept.append(str(archive))
                else:
                    eligible.append((archive, manifest, "skipped-session"))
            except (OSError, ValueError, UnicodeError) as exc:
                errors.append(str(exc))

    removed_files = removed_bytes = 0
    cleaned = []
    for archive, manifest, reason in eligible:
        try:
            files = _payload_files(archive, root)
            if not files:
                continue
            sizes = {path: path.stat().st_size for path in files}
            if dry_run:
                removed_files += len(files)
                removed_bytes += sum(sizes.values())
                cleaned.append(str(archive))
                continue
            prior = manifest.get("local_media_cleanup")
            prior = prior if isinstance(prior, dict) else {}
            manifest["local_media_cleanup"] = {
                "status": "pruning", "reason": reason, "started_at": vietnam_now().isoformat(),
                "removed_files": int(prior.get("removed_files") or 0),
                "removed_bytes": int(prior.get("removed_bytes") or 0),
                "planned_files": len(files), "planned_bytes": sum(sizes.values()),
            }
            _assert_confined(archive / "archive_manifest.json", root)
            atomic_write_json(archive / "archive_manifest.json", manifest)
            failures = []
            count = freed = 0
            for path in files:
                try:
                    _assert_confined(path, root)
                    path.unlink()
                    count += 1
                    freed += sizes[path]
                except (OSError, ValueError) as exc:
                    failures.append(f"{path}: {exc}")
            removed_files += count
            removed_bytes += freed
            if count:
                cleaned.append(str(archive))
            errors.extend(failures)
            manifest["local_media_cleanup"].update({
                "status": "partial" if failures else "complete",
                "completed_at": vietnam_now().isoformat(),
                "removed_files": int(prior.get("removed_files") or 0) + count,
                "removed_bytes": int(prior.get("removed_bytes") or 0) + freed,
                "errors": failures[:20],
            })
            atomic_write_json(archive / "archive_manifest.json", manifest)
        except (OSError, ValueError, UnicodeError) as exc:
            errors.append(f"{archive}: {exc}")

    result = MediaRetentionResult(removed_files, removed_bytes, tuple(cleaned), tuple(kept), tuple(errors), dry_run=dry_run)
    if not dry_run and (removed_files or errors):
        report = {
            "at": vietnam_now().isoformat(), "removed_files": removed_files,
            "removed_bytes": removed_bytes, "cleaned_archives": cleaned,
            "protected_archives": kept, "errors": errors[:50],
        }
        report_path = state / "media_retention_last_run.json"
        _assert_confined(report_path, root)
        atomic_write_json(report_path, report)
        _LOGGER.info("media_retention workspace=%s files=%s freed_bytes=%s errors=%s",
                     workspace.name, removed_files, removed_bytes, len(errors))
    return result
