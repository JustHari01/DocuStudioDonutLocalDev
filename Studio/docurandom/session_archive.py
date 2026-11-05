from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import re
import shutil
import threading
from urllib.parse import urlparse
import unicodedata

from .content_history import ContentHistoryStore
from .storage import atomic_write_json
from .workspace import Workspace
from .workspace_scheduler import VIETNAM_TIMEZONE, vietnam_now
from .youtube_upload import parse_content_package


_ARCHIVE_LOCK = threading.RLock()
_LOGGER = logging.getLogger("docurandom.session_archive")
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
DEFERRED_PUBLIC_QUEUE_FILENAME = "deferred_public_queue.json"
DEFERRED_PUBLIC_STALE_FILENAME = "deferred_public_stale.json"
# A just-confirmed upload can briefly be absent from the Studio Content list
# while YouTube finishes indexing it.  Only an exact not-found result older
# than this grace period is allowed to quarantine a queue item.
DEFERRED_PUBLIC_MISSING_GRACE_MINUTES = 15


class SessionArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class UploadedSessionArchive:
    archive_id: str
    archive_dir: Path
    video_path: Path
    package_path: Path
    thumbnail_path: Path | None
    moved_items: int


@dataclass(frozen=True)
class SkippedSessionArchive:
    archive_id: str
    archive_dir: Path
    moved_items: int
    reason: str


@dataclass(frozen=True)
class WorkspaceSessionReset:
    """Result of clearing only disposable state for the current video."""

    changed: bool
    removed_state_files: tuple[str, ...]
    settings_changed: bool


@dataclass(frozen=True)
class UploadedContentHistorySync:
    """Result of reconciling confirmed uploads into the title history."""

    added_titles: tuple[str, ...]
    existing_titles: tuple[str, ...]
    skipped_sessions: tuple[str, ...]


_CONFIRMED_UPLOAD_STATUSES = frozenset(
    {
        "complete",
        "deferred_public",
        "pending_public",
        "stale_pending_public",
    }
)


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


_CURRENT_SESSION_STATE_FILES = (
    "pipeline_checkpoint.json",
    "flow_history.json",
    "youtube_upload.json",
    "thumbnail-ai.json",
    "manual_youtube_upload.json",
)


def _deferred_public_queue_path(workspace: Workspace) -> Path:
    """Return the durable queue that survives the current-session cleanup."""

    return workspace.state_dir / DEFERRED_PUBLIC_QUEUE_FILENAME


def load_deferred_public_queue(
    workspace: Workspace,
) -> tuple[dict[str, object], ...]:
    """Read videos packaged locally that still need the next PUBLIC change.

    The queue deliberately lives outside ``_CURRENT_SESSION_STATE_FILES``.  A
    new production cycle may therefore clean Media/Output and its disposable
    checkpoints without losing the exact YouTube URL/profile that must be
    promoted later.
    """

    with _ARCHIVE_LOCK:
        value = _read_json(_deferred_public_queue_path(workspace))
        items = value.get("items") if isinstance(value, dict) else None
        if not isinstance(items, list):
            return ()
        return tuple(dict(item) for item in items if isinstance(item, dict))


def reconcile_uploaded_content_history(
    workspace: Workspace,
) -> UploadedContentHistorySync:
    """Backfill titles from durable, confirmed YouTube upload evidence.

    The title checklist is intentionally independent from the large media
    archives.  A registry entry is considered eligible only after YouTube has
    supplied a URL and the session reached one of the confirmed archive
    states.  The title is resolved in this order:

    1. the registry (so the history survives archive cleanup),
    2. the archived content package,
    3. the archived upload checkpoint, and
    4. the deferred-public queue.

    This is idempotent and deliberately does not infer titles from arbitrary
    folders or uncertain checkpoints.
    """

    registry_path = workspace.state_dir / "uploaded_sessions.json"
    registry = _read_json(registry_path)
    sessions = registry.get("sessions") if isinstance(registry, dict) else None
    if not isinstance(sessions, dict):
        return UploadedContentHistorySync((), (), ())

    history = ContentHistoryStore(
        workspace.content_history_path,
        workspace.content_history_text_path,
    )
    queue_titles = {
        str(item.get("identity") or "").strip(): str(item.get("title") or "").strip()
        for item in load_deferred_public_queue(workspace)
        if str(item.get("identity") or "").strip()
    }
    uploaded_root = workspace.uploaded_dir.resolve(strict=False)
    added: list[str] = []
    existing: list[str] = []
    skipped: list[str] = []
    registry_changed = False

    def is_under_uploaded(path: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(uploaded_root)
            return True
        except ValueError:
            return False

    def existing_file(value: object) -> Path | None:
        raw = str(value or "").strip().strip('"')
        if not raw:
            return None
        candidate = Path(raw).expanduser().resolve(strict=False)
        if not is_under_uploaded(candidate) or not candidate.is_file():
            return None
        return candidate

    for identity, raw_entry in list(sessions.items()):
        if not isinstance(raw_entry, dict):
            continue
        normalized_identity = str(identity or "").strip()
        status = str(raw_entry.get("status") or "").strip().casefold()
        youtube_url = str(raw_entry.get("youtube_url") or "").strip()
        if (
            not normalized_identity
            or status not in _CONFIRMED_UPLOAD_STATUSES
            or not youtube_url
        ):
            continue

        raw_archive_dir = str(raw_entry.get("archive_dir") or "").strip().strip('"')
        archive_dir: Path | None = (
            Path(raw_archive_dir).expanduser().resolve(strict=False)
            if raw_archive_dir
            else None
        )
        if (
            archive_dir is None
            or not is_under_uploaded(archive_dir)
            or not archive_dir.is_dir()
        ):
            # A user may already have removed the archive.  A title already
            # persisted in the registry can still be safely retained, but no
            # untrusted external path is inspected.
            archive_dir = None

        package_path = existing_file(raw_entry.get("package_path"))
        if package_path is None and archive_dir:
            output_dir = archive_dir / "Output"
            candidates = tuple(
                sorted(
                    path
                    for path in output_dir.glob("*.txt")
                    if path.is_file()
                    and not path.name.casefold().endswith("_chat.txt")
                    and "master-prompt" not in path.name.casefold()
                )
            ) if output_dir.is_dir() else ()
            if len(candidates) == 1:
                package_path = candidates[0].resolve(strict=False)

        title = str(raw_entry.get("title") or "").strip()
        if not title and package_path is not None:
            try:
                title = parse_content_package(package_path).title.strip()
            except Exception:
                title = ""

        if not title and archive_dir:
            archived_checkpoint = _read_json(
                archive_dir / "State" / "youtube_upload.json"
            )
            title = str(archived_checkpoint.get("title") or "").strip()

        if not title:
            title = queue_titles.get(normalized_identity, "")

        if not title:
            skipped.append(normalized_identity)
            continue

        updated_entry = dict(raw_entry)
        if str(updated_entry.get("title") or "").strip() != title:
            updated_entry["title"] = title
            sessions[normalized_identity] = updated_entry
            registry_changed = True

        video_path = existing_file(raw_entry.get("video_path"))
        if history.add(
            title,
            video_path=video_path,
            package_path=package_path,
        ):
            added.append(title)
        else:
            existing.append(title)

    if registry_changed:
        atomic_write_json(
            registry_path,
            {"version": int(registry.get("version") or 1), "sessions": sessions},
        )

    result = UploadedContentHistorySync(
        tuple(added),
        tuple(existing),
        tuple(skipped),
    )
    if added or registry_changed or skipped:
        _LOGGER.info(
            "uploaded content history reconciled workspace=%s added=%d existing=%d skipped=%d registry_updated=%s",
            workspace.workspace_id,
            len(result.added_titles),
            len(result.existing_titles),
            len(result.skipped_sessions),
            registry_changed,
        )
    return result


def is_deferred_public_item_structurally_valid(
    item: dict[str, object],
) -> bool:
    """Return whether a queue entry contains enough durable upload identity.

    The queue is the source of truth for a confirmed UNLISTED upload.  A
    partially written/legacy entry is not such proof and must not block the
    next ready video forever.  This check intentionally stays local; it does
    not claim that YouTube still has the remote video.
    """

    identity = str(item.get("identity") or "").strip()
    profile_id = str(item.get("profile_id") or "").strip()
    youtube_url = str(item.get("youtube_url") or "").strip()
    privacy = str(item.get("privacy") or "").strip().upper()
    final_privacy = str(item.get("final_privacy") or "").strip().upper()
    if not identity or not profile_id or not youtube_url:
        return False
    if privacy != "UNLISTED" or final_privacy != "PUBLIC":
        return False
    try:
        parsed = urlparse(youtube_url)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if host not in {"youtu.be", "youtube.com", "m.youtube.com"}:
        return False
    if host == "youtu.be":
        return bool(parsed.path.strip("/"))
    return bool(parsed.path.startswith("/watch") and parsed.query)


def load_schedulable_deferred_public_queue(
    workspace: Workspace | None,
) -> tuple[dict[str, object], ...]:
    """Load only queue entries that can prove a confirmed UNLISTED upload.

    Invalid entries are moved to the recoverable stale sidecar.  The normal
    ``load_deferred_public_queue`` function remains lossless for diagnostics
    and archive migration.
    """

    if workspace is None:
        return ()
    items = load_deferred_public_queue(workspace)
    valid: list[dict[str, object]] = []
    for item in items:
        if is_deferred_public_item_structurally_valid(item):
            valid.append(item)
            continue
        quarantine_deferred_public_item(
            workspace,
            str(item.get("identity") or ""),
            reason="queue-entry-invalid-no-confirmed-unlisted-proof",
        )
    return tuple(valid)


def confirmed_uploaded_session_matches(
    workspace: Workspace,
    identity: str,
    youtube_url: str,
) -> bool:
    """Prove a registry upload was durably archived under this workspace.

    The check is local and read-only. A pending-public upload also needs its
    exact structurally valid queue item, because that queue owns the later
    FIFO visibility change.
    """

    normalized_identity = str(identity or "").strip()
    normalized_url = str(youtube_url or "").strip()
    if not normalized_identity or not normalized_url:
        return False
    with _ARCHIVE_LOCK:
        registry = _read_json(workspace.state_dir / "uploaded_sessions.json")
        sessions = registry.get("sessions") if isinstance(registry, dict) else None
        entry = sessions.get(normalized_identity) if isinstance(sessions, dict) else None
        if not isinstance(entry, dict):
            return False
        status = str(entry.get("status") or "").strip().casefold()
        if status not in _CONFIRMED_UPLOAD_STATUSES:
            return False
        if str(entry.get("youtube_url") or "").strip() != normalized_url:
            return False
        raw_archive = str(entry.get("archive_dir") or "").strip()
        if not raw_archive:
            return False
        archive_dir = Path(raw_archive).expanduser().resolve(strict=False)
        uploaded_root = workspace.uploaded_dir.resolve(strict=False)
        try:
            archive_dir.relative_to(uploaded_root)
        except ValueError:
            return False
        if not archive_dir.is_dir():
            return False
        if status == "pending_public":
            queue = _read_json(_deferred_public_queue_path(workspace))
            items = queue.get("items") if isinstance(queue, dict) else None
            return any(
                isinstance(item, dict)
                and str(item.get("identity") or "").strip() == normalized_identity
                and str(item.get("youtube_url") or "").strip() == normalized_url
                and is_deferred_public_item_structurally_valid(item)
                for item in (items if isinstance(items, list) else [])
            )
        return True


def deferred_public_item_is_old_enough(
    item: dict[str, object],
    *,
    now: datetime | None = None,
    grace_minutes: int = DEFERRED_PUBLIC_MISSING_GRACE_MINUTES,
) -> bool:
    """Return whether an exact remote-not-found result is safe to trust."""

    raw = str(item.get("queued_at") or "").strip()
    if not raw:
        # Unknown-age legacy records remain fail-closed until a user or a
        # future reconciliation path can identify their upload attempt.
        return False
    try:
        queued_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if queued_at.tzinfo is None:
        queued_at = queued_at.replace(tzinfo=VIETNAM_TIMEZONE)
    else:
        queued_at = queued_at.astimezone(VIETNAM_TIMEZONE)
    current = now or vietnam_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=VIETNAM_TIMEZONE)
    else:
        current = current.astimezone(VIETNAM_TIMEZONE)
    return current - queued_at >= timedelta(minutes=max(0, grace_minutes))


def is_deferred_public_not_found_error(message: str) -> bool:
    """Classify only deterministic exact-video absence, never generic errors."""

    folded = "".join(
        character
        for character in unicodedata.normalize("NFKD", str(message or "").casefold())
        if not unicodedata.combining(character)
    ).replace("đ", "d")
    markers = (
        "youtube chua thay dung video id",
        "khong tim thay dung video",
        "khong tim thay video",
        "video khong ton tai",
        "video not found",
        "no video found",
    )
    return any(marker in folded for marker in markers)


def reconcile_deferred_public_queue_for_schedule_start(
    workspace: Workspace,
    *,
    started_at: datetime,
) -> tuple[dict[str, object], ...]:
    """Keep only the current schedule lane and quarantine older queue items.

    A workspace may contain pending-public records written by an older build.
    They must not steal the next publication clock from the current rendered
    video.  The records are moved to a recoverable sidecar file instead of
    being deleted, while the newest item created after the current schedule
    start remains the one active lane.
    """

    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=VIETNAM_TIMEZONE)
    else:
        started_at = started_at.astimezone(VIETNAM_TIMEZONE)

    def queued_at(item: dict[str, object]) -> datetime | None:
        raw = str(item.get("queued_at") or "").strip()
        if not raw:
            return None
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=VIETNAM_TIMEZONE)
        return value.astimezone(VIETNAM_TIMEZONE)

    with _ARCHIVE_LOCK:
        queue_path = _deferred_public_queue_path(workspace)
        queue_value = _read_json(queue_path)
        raw_items = queue_value.get("items") if isinstance(queue_value, dict) else None
        items = [dict(item) for item in raw_items if isinstance(item, dict)] \
            if isinstance(raw_items, list) else []
        if not items:
            return ()

        current_items = [
            item
            for item in items
            if (queued_at(item) is not None and queued_at(item) >= started_at)
        ]
        # Unknown timestamps are legacy records.  Keep the newest known item
        # only when no current-cycle item exists; it remains recoverable and
        # will not be confused with a newly rendered output.
        if current_items:
            active = [
                max(current_items, key=lambda item: queued_at(item) or started_at)
            ]
        else:
            active = []
        active_ids = {
            str(item.get("identity") or "").strip()
            for item in active
            if str(item.get("identity") or "").strip()
        }
        stale = [
            item
            for item in items
            if str(item.get("identity") or "").strip() not in active_ids
        ]

        stale_path = workspace.state_dir / DEFERRED_PUBLIC_STALE_FILENAME
        if stale:
            stale_value = _read_json(stale_path)
            previous = stale_value.get("items") if isinstance(stale_value, dict) else None
            archived = [dict(item) for item in previous if isinstance(item, dict)] \
                if isinstance(previous, list) else []
            archived.extend(
                {
                    "quarantined_at": vietnam_now().isoformat(),
                    "reason": "older-than-current-schedule-lane",
                    "item": dict(item),
                }
                for item in stale
            )
            atomic_write_json(
                stale_path,
                {"version": 1, "items": archived},
            )

        atomic_write_json(
            queue_path,
            {"version": 1, "items": active},
        )
        return tuple(active)


def _upsert_deferred_public_queue_item(
    workspace: Workspace,
    item: dict[str, object],
) -> None:
    identity = str(item.get("identity") or "").strip()
    if not identity:
        raise SessionArchiveError(
            "Video chờ chuyển Công khai thiếu khóa chống đăng trùng."
        )
    path = _deferred_public_queue_path(workspace)
    value = _read_json(path)
    raw_items = value.get("items") if isinstance(value, dict) else None
    items = [dict(entry) for entry in raw_items if isinstance(entry, dict)] \
        if isinstance(raw_items, list) else []
    replaced = False
    for index, entry in enumerate(items):
        if str(entry.get("identity") or "").strip() == identity:
            items[index] = dict(item)
            replaced = True
            break
    if not replaced:
        items.append(dict(item))
    atomic_write_json(
        path,
        {"version": 1, "items": items},
    )


def quarantine_deferred_public_item(
    workspace: Workspace,
    identity: str,
    *,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Move one unverified queue entry to a recoverable stale sidecar.

    This is deliberately not a delete.  The archived video, manifest and
    evidence remain available so a user can inspect or repair the remote
    state.  The scheduler only stops treating the entry as an authoritative
    PUBLIC-promotion lane after it has been moved here.
    """

    normalized_identity = str(identity or "").strip()
    if not normalized_identity:
        return False
    quarantined = now or vietnam_now()
    if quarantined.tzinfo is None:
        quarantined = quarantined.replace(tzinfo=VIETNAM_TIMEZONE)
    else:
        quarantined = quarantined.astimezone(VIETNAM_TIMEZONE)
    reason_text = str(reason or "").strip()[:800] or "unspecified"

    with _ARCHIVE_LOCK:
        queue_path = _deferred_public_queue_path(workspace)
        queue_value = _read_json(queue_path)
        raw_items = queue_value.get("items") if isinstance(queue_value, dict) else None
        items = [dict(entry) for entry in raw_items if isinstance(entry, dict)] \
            if isinstance(raw_items, list) else []
        matching = [
            entry
            for entry in items
            if str(entry.get("identity") or "").strip() == normalized_identity
        ]
        if not matching:
            return False

        remaining = [
            entry
            for entry in items
            if str(entry.get("identity") or "").strip() != normalized_identity
        ]
        atomic_write_json(queue_path, {"version": 1, "items": remaining})

        stale_path = workspace.state_dir / DEFERRED_PUBLIC_STALE_FILENAME
        stale_value = _read_json(stale_path)
        raw_stale = stale_value.get("items") if isinstance(stale_value, dict) else None
        stale_items = [dict(entry) for entry in raw_stale if isinstance(entry, dict)] \
            if isinstance(raw_stale, list) else []
        stale_items.extend(
            {
                "quarantined_at": quarantined.isoformat(),
                "reason": reason_text,
                "item": dict(entry),
            }
            for entry in matching
        )
        atomic_write_json(stale_path, {"version": 1, "items": stale_items})

        # Keep the upload registry and manifest explicit about why this item
        # no longer owns a scheduler slot.  No archived media is touched.
        registry_path = workspace.state_dir / "uploaded_sessions.json"
        registry = _read_json(registry_path)
        sessions = registry.get("sessions") if isinstance(registry, dict) else None
        session = sessions.get(normalized_identity) if isinstance(sessions, dict) else None
        if isinstance(session, dict):
            updated_session = dict(session)
            updated_session.update(
                {
                    "status": "stale_pending_public",
                    "visibility_status": "STALE_UNVERIFIED",
                    "quarantined_at": quarantined.isoformat(),
                    "quarantine_reason": reason_text,
                }
            )
            sessions[normalized_identity] = updated_session
            atomic_write_json(registry_path, {"version": 1, "sessions": sessions})

            archive_dir = Path(str(updated_session.get("archive_dir") or ""))
            manifest_path = archive_dir / "archive_manifest.json"
            manifest = _read_json(manifest_path)
            if manifest:
                manifest.update(
                    {
                        "visibility_status": "STALE_UNVERIFIED",
                        "quarantined_at_vietnam": quarantined.isoformat(),
                        "quarantine_reason": reason_text,
                    }
                )
                atomic_write_json(manifest_path, manifest)
        return True


def mark_deferred_public_published(
    workspace: Workspace,
    identity: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Remove one promoted item and finalize its local Uploaded registry."""

    normalized_identity = str(identity or "").strip()
    if not normalized_identity:
        return False
    published = now or vietnam_now()
    if published.tzinfo is None:
        published = published.replace(tzinfo=VIETNAM_TIMEZONE)
    else:
        published = published.astimezone(VIETNAM_TIMEZONE)
    published_iso = published.isoformat()

    with _ARCHIVE_LOCK:
        queue_path = _deferred_public_queue_path(workspace)
        queue_value = _read_json(queue_path)
        raw_items = queue_value.get("items") if isinstance(queue_value, dict) else None
        items = [dict(entry) for entry in raw_items if isinstance(entry, dict)] \
            if isinstance(raw_items, list) else []
        matching = [
            entry
            for entry in items
            if str(entry.get("identity") or "").strip() == normalized_identity
        ]

        registry_path = workspace.state_dir / "uploaded_sessions.json"
        registry = _read_json(registry_path)
        sessions = registry.get("sessions") if isinstance(registry, dict) else None
        session = sessions.get(normalized_identity) if isinstance(sessions, dict) else None
        if not matching and not (
            isinstance(session, dict)
            and str(session.get("status") or "").casefold() == "complete"
        ):
            return False

        if isinstance(session, dict):
            updated_session = dict(session)
            updated_session.update(
                {
                    "status": "complete",
                    "visibility_status": "PUBLIC",
                    "published_at": published_iso,
                }
            )
            sessions[normalized_identity] = updated_session
            atomic_write_json(registry_path, {"version": 1, "sessions": sessions})

            archive_dir = Path(str(updated_session.get("archive_dir") or ""))
            manifest_path = archive_dir / "archive_manifest.json"
            manifest = _read_json(manifest_path)
            if manifest:
                manifest["visibility_status"] = "PUBLIC"
                manifest["published_at_vietnam"] = published_iso
                atomic_write_json(manifest_path, manifest)

        remaining = [
            entry
            for entry in items
            if str(entry.get("identity") or "").strip() != normalized_identity
        ]
        if remaining or queue_path.is_file():
            atomic_write_json(
                queue_path,
                {"version": 1, "items": remaining},
            )
        return True


def clear_archived_session_schedule_state(workspace: Workspace) -> bool:
    """Release scheduler ownership after a confirmed session is archived.

    The schedule definitions and their independent workflow/upload clocks stay
    intact.  Only the durable pointers that describe the just-finished video
    are cleared, including the two legacy deferred flags.
    """

    with _ARCHIVE_LOCK:
        if not workspace.settings_path.is_file():
            return False
        settings = _read_json(workspace.settings_path)
        original = dict(settings)
        for key in (
            "schedule_active_shift_id",
            "schedule_active_occurrence_id",
            "schedule_active_stage",
        ):
            settings[key] = ""
        settings["schedule_deferred_upload"] = False
        settings["schedule_deferred_public"] = False
        settings["pipeline_render_pending"] = False
        if settings == original:
            return False
        atomic_write_json(workspace.settings_path, settings)
        return True


def archive_confirmed_youtube_session(
    workspace: Workspace,
) -> UploadedSessionArchive | None:
    """Package a locally confirmed YouTube session when all source files exist.

    This is intentionally a no-op for incomplete/uncertain checkpoints.  It is
    used at application startup to migrate sessions written by an older build
    and therefore never guesses a missing video, package, or YouTube URL.
    """

    checkpoint = _read_json(workspace.state_dir / "youtube_upload.json")
    status = str(checkpoint.get("status") or "").casefold()
    if status not in {"complete", "deferred_public"}:
        return None
    youtube_url = str(checkpoint.get("youtube_url") or "").strip()
    video_path = Path(str(checkpoint.get("video_path") or "")).expanduser()
    package_path = Path(str(checkpoint.get("package_path") or "")).expanduser()
    if not youtube_url or not video_path.is_file() or not package_path.is_file():
        return None
    raw_thumbnail = str(checkpoint.get("thumbnail_path") or "").strip()
    thumbnail_path = Path(raw_thumbnail).expanduser() if raw_thumbnail else None
    if thumbnail_path is not None and not thumbnail_path.is_file():
        thumbnail_path = None
    return archive_uploaded_session(
        workspace,
        video_path=video_path,
        package_path=package_path,
        thumbnail_path=thumbnail_path,
        youtube_url=youtube_url,
        profile_name=str(checkpoint.get("profile_name") or "Profile đã chọn"),
        channel_name=str(checkpoint.get("channel_name") or "Kênh đã upload"),
        pending_public=status == "deferred_public",
    )


def _managed_session_directories_empty(workspace: Workspace) -> tuple[bool, bool]:
    """Return (empty, any_directory_missing) without following external paths."""

    missing = False
    for directory in (
        workspace.media_dir,
        workspace.output_dir,
        workspace.voicevox_dir,
    ):
        if not directory.exists():
            missing = True
            continue
        try:
            if any(path.is_file() for path in directory.rglob("*")):
                return False, missing
        except OSError:
            # An unreadable directory is never treated as permission to erase
            # a recoverable checkpoint.
            return False, missing
    return True, missing


def reset_workspace_session_state(workspace: Workspace) -> WorkspaceSessionReset:
    """Forget the current video checkpoint without deleting user media.

    Schedules, YouTube/Donut profile selection, render preferences, uploaded
    archives, diagnostic logs and the content-dedup checklist are deliberately
    preserved.  This is the single implementation used by both the explicit
    launcher button and automatic reconciliation after manual file deletion.
    """

    with _ARCHIVE_LOCK:
        workspace.ensure_layout()
        removed: list[str] = []
        for name in _CURRENT_SESSION_STATE_FILES:
            path = workspace.state_dir / name
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(name)
            except OSError as exc:
                raise SessionArchiveError(
                    f"Không xóa được trạng thái phiên {name}: {exc}"
                ) from exc

        settings = _read_json(workspace.settings_path)
        original = dict(settings)
        settings["schedule_active_shift_id"] = ""
        settings["schedule_active_occurrence_id"] = ""
        settings["schedule_active_stage"] = ""
        settings["pipeline_render_pending"] = False

        # Thumbnail/audio generated for the deleted session must not remain as
        # broken paths.  Existing external user selections remain untouched.
        for key in ("youtube_thumbnail_path", "audio_path"):
            raw = str(settings.get(key) or "").strip().strip('"')
            if raw and not Path(raw).expanduser().is_file():
                settings[key] = ""

        settings_changed = settings != original
        if settings_changed:
            atomic_write_json(workspace.settings_path, settings)
        return WorkspaceSessionReset(
            changed=bool(removed or settings_changed),
            removed_state_files=tuple(removed),
            settings_changed=settings_changed,
        )


def reconcile_deleted_workspace_session(
    workspace: Workspace,
) -> WorkspaceSessionReset:
    """Clear an orphaned checkpoint after its workspace files were deleted.

    A newly failed precheck can legitimately have empty folders, so emptiness
    alone is not enough.  Automatic reset requires either a deleted managed
    directory or evidence that the pipeline had already produced material for
    the now-empty session.  Ambiguous cases remain recoverable through the
    explicit launcher button.
    """

    empty, directory_missing = _managed_session_directories_empty(workspace)
    if not empty:
        return WorkspaceSessionReset(False, (), False)

    checkpoint = _read_json(workspace.pipeline_checkpoint_path)
    stages = checkpoint.get("stages") if isinstance(checkpoint, dict) else None
    material_stage = False
    if isinstance(stages, dict):
        for name in (
            "chatgpt",
            "package",
            "flow_images",
            "voicevox",
            "watermark",
            "render",
        ):
            value = stages.get(name)
            status = (
                str(value.get("status") or "")
                if isinstance(value, dict)
                else ""
            )
            if status == "complete":
                material_stage = True
                break

    youtube_state_exists = (workspace.state_dir / "youtube_upload.json").is_file()
    flow_state_exists = workspace.flow_history_path.is_file()
    state_exists = bool(
        workspace.pipeline_checkpoint_path.is_file()
        or youtube_state_exists
        or flow_state_exists
    )
    if not state_exists:
        # Recreate folders a user removed even when no checkpoint remains.
        if directory_missing:
            workspace.ensure_layout()
        return WorkspaceSessionReset(False, (), False)
    if not (directory_missing or material_stage or youtube_state_exists):
        return WorkspaceSessionReset(False, (), False)
    return reset_workspace_session_state(workspace)


def _safe_name(value: str, *, fallback: str) -> str:
    cleaned = _INVALID_FILENAME.sub("-", value).strip(" .-")
    return (cleaned[:80] or fallback).rstrip(" .")


def _unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}__{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise SessionArchiveError(f"Không thể tạo tên lưu kho riêng cho {path.name}.")


def _move_directory_contents(source: Path, destination: Path) -> int:
    source.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    moved = 0
    for item in tuple(source.iterdir()):
        target = _unique_target(destination / item.name)
        shutil.move(str(item), str(target))
        moved += 1
    return moved


def _copy_state_snapshot(workspace: Workspace, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    names = {
        "pipeline_checkpoint.json",
        "flow_history.json",
        "youtube_upload.json",
        "manual_youtube_upload.json",
        "thumbnail-ai.json",
        "render_history.json",
        "content_history.json",
        "settings.json",
        "workspace.json",
        "diagnostic.log",
        "diagnostic.previous.log",
        DEFERRED_PUBLIC_QUEUE_FILENAME,
    }
    for source in workspace.state_dir.iterdir():
        if not source.is_file() or (
            source.name not in names and not source.name.startswith("latest-")
        ):
            continue
        try:
            shutil.copy2(source, destination / source.name)
        except OSError as exc:
            raise SessionArchiveError(
                f"Không sao lưu được trạng thái phiên {source.name}: {exc}"
            ) from exc


def archive_uploaded_session(
    workspace: Workspace,
    *,
    video_path: Path,
    package_path: Path,
    thumbnail_path: Path | None,
    youtube_url: str,
    profile_name: str,
    channel_name: str,
    pending_public: bool = False,
    now: datetime | None = None,
) -> UploadedSessionArchive:
    """Archive one confirmed upload, then clear disposable session state.

    ``pending_public`` is used only for a video that is already confirmed and
    intentionally UNLISTED until the next publication slot.  The local session is still
    packaged immediately; a small durable queue keeps the later visibility
    promotion independent from the next production cycle.
    """

    if not str(youtube_url or "").strip():
        raise SessionArchiveError(
            "YouTube chưa trả URL xác nhận; từ chối lưu kho và dọn phiên."
        )
    checkpoint_path = workspace.state_dir / "youtube_upload.json"
    with _ARCHIVE_LOCK:
        upload_checkpoint = _read_json(checkpoint_path)
        checkpoint_status = str(upload_checkpoint.get("status") or "").casefold()
        if pending_public:
            if checkpoint_status != "deferred_public":
                raise SessionArchiveError(
                    "Checkpoint YouTube không ở trạng thái chờ Công khai; "
                    "từ chối di chuyển dữ liệu phiên."
                )
            if str(upload_checkpoint.get("final_privacy") or "").upper() != "PUBLIC":
                raise SessionArchiveError(
                    "Checkpoint chờ Công khai không có quyền riêng tư đích PUBLIC."
                )
        elif checkpoint_status != "complete":
            raise SessionArchiveError(
                "Checkpoint YouTube chưa hoàn tất; từ chối di chuyển dữ liệu phiên."
            )
        identity = str(upload_checkpoint.get("identity") or "").strip()
        if not identity:
            raise SessionArchiveError(
                "Checkpoint YouTube thiếu khóa chống đăng trùng; từ chối dọn phiên."
            )

        registry_path = workspace.state_dir / "uploaded_sessions.json"
        registry = _read_json(registry_path)
        sessions = registry.get("sessions") if isinstance(registry, dict) else None
        if not isinstance(sessions, dict):
            sessions = {}
        existing = sessions.get(identity)
        expected_archive_status = "pending_public" if pending_public else "complete"
        if (
            isinstance(existing, dict)
            and str(existing.get("status") or "").casefold()
            == expected_archive_status
        ):
            archive_dir = Path(str(existing.get("archive_dir") or ""))
            if archive_dir.is_dir():
                if pending_public and not any(
                    str(item.get("identity") or "").strip() == identity
                    for item in load_deferred_public_queue(workspace)
                ):
                    archived_state = _read_json(
                        archive_dir / "State" / "youtube_upload.json"
                    )
                    _upsert_deferred_public_queue_item(
                        workspace,
                        {
                            "identity": identity,
                            "attempt_id": str(
                                archived_state.get("attempt_id")
                                or upload_checkpoint.get("attempt_id")
                                or ""
                            ),
                            "workspace_id": str(
                                archived_state.get("workspace_id")
                                or upload_checkpoint.get("workspace_id")
                                or workspace.workspace_id
                            ),
                            "profile_id": str(
                                archived_state.get("profile_id")
                                or upload_checkpoint.get("profile_id")
                                or ""
                            ),
                            "profile_name": str(
                                archived_state.get("profile_name")
                                or upload_checkpoint.get("profile_name")
                                or profile_name
                            ),
                            "channel_name": str(
                                archived_state.get("channel_name")
                                or upload_checkpoint.get("channel_name")
                                or channel_name
                            ),
                            "youtube_url": str(
                                existing.get("youtube_url")
                                or archived_state.get("youtube_url")
                                or youtube_url
                            ),
                            "archive_dir": str(archive_dir),
                            "privacy": "UNLISTED",
                            "final_privacy": "PUBLIC",
                            "title": str(
                                archived_state.get("title")
                                or upload_checkpoint.get("title")
                                or ""
                            ),
                            "description": str(
                                archived_state.get("description")
                                or upload_checkpoint.get("description")
                                or ""
                            ),
                        },
                    )
                clear_archived_session_schedule_state(workspace)
                return UploadedSessionArchive(
                    archive_id=identity,
                    archive_dir=archive_dir,
                    video_path=Path(str(existing.get("video_path") or "")),
                    package_path=Path(str(existing.get("package_path") or "")),
                    thumbnail_path=(
                        Path(str(existing.get("thumbnail_path")))
                        if existing.get("thumbnail_path")
                        else None
                    ),
                    moved_items=int(existing.get("moved_items") or 0),
                )

        completed = now or vietnam_now()
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=VIETNAM_TIMEZONE)
        else:
            completed = completed.astimezone(VIETNAM_TIMEZONE)
        try:
            video_created = datetime.fromtimestamp(
                video_path.stat().st_mtime,
                VIETNAM_TIMEZONE,
            )
        except OSError:
            video_created = completed
        session_stamp = video_created.strftime("%Y-%m-%d_%H-%M-%S")
        folder_name = (
            f"{session_stamp}_"
            f"{_safe_name(video_path.stem, fallback='video')}_{identity[:8]}"
        )
        archive_dir = workspace.uploaded_dir / folder_name
        archive_dir.mkdir(parents=True, exist_ok=True)

        sessions[identity] = {
            "status": "archiving",
            "archive_dir": str(archive_dir),
            "youtube_url": youtube_url,
            "started_at": completed.isoformat(),
        }
        atomic_write_json(
            registry_path,
            {"version": 1, "sessions": sessions},
        )

        state_destination = archive_dir / "State"
        _copy_state_snapshot(workspace, state_destination)
        prompts_destination = archive_dir / "Prompts"
        if workspace.prompt_dir.is_dir():
            shutil.copytree(
                workspace.prompt_dir,
                prompts_destination,
                dirs_exist_ok=True,
            )

        original_video = video_path.resolve(strict=False)
        original_package = package_path.resolve(strict=False)
        original_thumbnail = (
            thumbnail_path.resolve(strict=False) if thumbnail_path is not None else None
        )
        try:
            archived_video = archive_dir / "Output" / original_video.relative_to(
                workspace.output_dir.resolve(strict=False)
            )
            archived_package = archive_dir / "Output" / original_package.relative_to(
                workspace.output_dir.resolve(strict=False)
            )
        except ValueError as exc:
            raise SessionArchiveError(
                "Video hoặc file Tổng hợp nằm ngoài Output; từ chối dọn phiên."
            ) from exc
        archived_thumbnail: Path | None = None
        if original_thumbnail is not None:
            try:
                relative_thumbnail = original_thumbnail.relative_to(workspace.output_dir)
                archived_thumbnail = archive_dir / "Output" / relative_thumbnail
            except ValueError:
                try:
                    relative_thumbnail = original_thumbnail.relative_to(workspace.media_dir)
                    archived_thumbnail = archive_dir / "Media" / relative_thumbnail
                except ValueError:
                    archived_thumbnail = archive_dir / "Selected Thumbnail" / original_thumbnail.name
                    archived_thumbnail.parent.mkdir(parents=True, exist_ok=True)
                    if original_thumbnail.is_file():
                        shutil.copy2(original_thumbnail, archived_thumbnail)

        moved_items = 0
        moved_items += _move_directory_contents(
            workspace.output_dir,
            archive_dir / "Output",
        )
        moved_items += _move_directory_contents(
            workspace.media_dir,
            archive_dir / "Media",
        )
        moved_items += _move_directory_contents(
            workspace.voicevox_dir,
            archive_dir / "VOICEVOX",
        )

        if archived_video.is_file():
            renamed_video = _unique_target(
                archived_video.with_name(
                    f"{session_stamp}_{_safe_name(original_video.stem, fallback='video')}"
                    f"{original_video.suffix.casefold()}"
                )
            )
            archived_video.rename(renamed_video)
            archived_video = renamed_video
        if archived_package.is_file():
            renamed_package = _unique_target(
                archived_package.with_name(
                    f"{session_stamp}_{_safe_name(original_package.stem, fallback='package')}"
                    f"{original_package.suffix.casefold()}"
                )
            )
            archived_package.rename(renamed_package)
            archived_package = renamed_package
        if archived_thumbnail is not None and archived_thumbnail.is_file():
            renamed_thumbnail = _unique_target(
                archived_thumbnail.with_name(
                    f"{session_stamp}_{_safe_name(original_thumbnail.stem, fallback='thumbnail')}"
                    f"{original_thumbnail.suffix.casefold()}"
                )
            )
            archived_thumbnail.rename(renamed_thumbnail)
            archived_thumbnail = renamed_thumbnail

        manifest = {
            "schema": "docurandom-uploaded-session-v1",
            "archive_id": identity,
            "archived_at_vietnam": completed.isoformat(),
            "video_created_at_vietnam": video_created.isoformat(),
            "youtube_url": youtube_url,
            "profile_name": profile_name,
            "channel_name": channel_name,
            "original": {
                "video": str(original_video),
                "package": str(original_package),
                "thumbnail": str(original_thumbnail) if original_thumbnail else "",
            },
            "archived": {
                "video": str(archived_video),
                "package": str(archived_package),
                "thumbnail": str(archived_thumbnail) if archived_thumbnail else "",
            },
            "moved_items": moved_items,
            "visibility_status": "UNLISTED_PENDING_PUBLIC"
            if pending_public
            else str(upload_checkpoint.get("privacy") or "").upper(),
            "final_privacy": str(
                upload_checkpoint.get("final_privacy")
                or upload_checkpoint.get("privacy")
                or ""
            ).upper(),
        }
        atomic_write_json(archive_dir / "archive_manifest.json", manifest)

        sessions[identity] = {
            "status": "pending_public" if pending_public else "complete",
            "archive_dir": str(archive_dir),
            "video_path": str(archived_video),
            "package_path": str(archived_package),
            "thumbnail_path": str(archived_thumbnail) if archived_thumbnail else "",
            "youtube_url": youtube_url,
            # Keep the small anti-duplicate identity beside the upload
            # registry.  The large archive may be deleted later to reclaim
            # disk space; the title must remain recoverable without it.
            "title": str(upload_checkpoint.get("title") or "").strip(),
            "archived_at": completed.isoformat(),
            "moved_items": moved_items,
            "visibility_status": "UNLISTED_PENDING_PUBLIC"
            if pending_public
            else str(upload_checkpoint.get("privacy") or "").upper(),
            "final_privacy": str(
                upload_checkpoint.get("final_privacy")
                or upload_checkpoint.get("privacy")
                or ""
            ).upper(),
        }
        atomic_write_json(
            registry_path,
            {"version": 1, "sessions": sessions},
        )

        if pending_public:
            _upsert_deferred_public_queue_item(
                workspace,
                {
                    "identity": identity,
                    "attempt_id": str(upload_checkpoint.get("attempt_id") or ""),
                    "workspace_id": str(
                        upload_checkpoint.get("workspace_id") or workspace.workspace_id
                    ),
                    "profile_id": str(upload_checkpoint.get("profile_id") or ""),
                    "profile_name": str(
                        upload_checkpoint.get("profile_name") or profile_name
                    ),
                    "channel_name": str(
                        upload_checkpoint.get("channel_name") or channel_name
                    ),
                    "youtube_url": youtube_url,
                    "archive_dir": str(archive_dir),
                    "video_path": str(archived_video),
                    "package_path": str(archived_package),
                    "thumbnail_path": (
                        str(archived_thumbnail) if archived_thumbnail else ""
                    ),
                    "privacy": str(upload_checkpoint.get("privacy") or "UNLISTED").upper(),
                    "final_privacy": str(
                        upload_checkpoint.get("final_privacy") or "PUBLIC"
                    ).upper(),
                    "title": str(upload_checkpoint.get("title") or ""),
                    "description": str(upload_checkpoint.get("description") or ""),
                    "seo_tags": upload_checkpoint.get("seo_tags")
                    if isinstance(upload_checkpoint.get("seo_tags"), list)
                    else [],
                    "queued_at": completed.isoformat(),
                },
            )

        for disposable in (
            workspace.pipeline_checkpoint_path,
            workspace.flow_history_path,
            checkpoint_path,
            workspace.state_dir / "thumbnail-ai.json",
        ):
            try:
                disposable.unlink(missing_ok=True)
            except OSError as exc:
                raise SessionArchiveError(
                    f"Đã lưu kho nhưng chưa dọn được {disposable.name}: {exc}"
                ) from exc

        # Release only the just-finished video's ownership.  Schedule
        # definitions and independent workflow/upload clocks remain enabled so
        # the next cycle can claim its own slot immediately.
        clear_archived_session_schedule_state(workspace)

        # Only a durable successor upload can release the previous Public
        # video's local media. Keep the new archive and all active drafts.
        # Cleanup failure must never turn a confirmed upload into a retry.
        from .media_retention import prune_workspace_media

        try:
            prune_workspace_media(workspace)
        except Exception:
            _LOGGER.exception("media_retention_after_upload workspace=%s", workspace.name)

        return UploadedSessionArchive(
            archive_id=identity,
            archive_dir=archive_dir,
            video_path=archived_video,
            package_path=archived_package,
            thumbnail_path=archived_thumbnail,
            moved_items=moved_items,
        )


def archive_skipped_session(
    workspace: Workspace,
    *,
    reason: str,
    shift_id: str,
    occurrence_id: str,
    now: datetime | None = None,
) -> SkippedSessionArchive:
    """Move a failed scheduled session aside and leave its workspace clean."""

    explanation = str(reason or "Ca không hoàn tất trước giờ đăng.").strip()
    completed = now or vietnam_now()
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=VIETNAM_TIMEZONE)
    else:
        completed = completed.astimezone(VIETNAM_TIMEZONE)
    archive_id = (
        f"{completed.strftime('%Y%m%dT%H%M%S')}-"
        f"{_safe_name(shift_id, fallback='schedule')}"
    )
    with _ARCHIVE_LOCK:
        skipped_root = workspace.root / "Skipped"
        skipped_root.mkdir(parents=True, exist_ok=True)
        archive_dir = _unique_target(
            skipped_root
            / (
                f"{completed.strftime('%Y-%m-%d_%H-%M-%S')}_"
                f"{_safe_name(shift_id, fallback='schedule')}"
            )
        )
        archive_dir.mkdir(parents=True, exist_ok=False)

        _copy_state_snapshot(workspace, archive_dir / "State")
        if workspace.prompt_dir.is_dir():
            shutil.copytree(
                workspace.prompt_dir,
                archive_dir / "Prompts",
                dirs_exist_ok=True,
            )
        moved_items = 0
        moved_items += _move_directory_contents(
            workspace.output_dir,
            archive_dir / "Output",
        )
        moved_items += _move_directory_contents(
            workspace.media_dir,
            archive_dir / "Media",
        )
        moved_items += _move_directory_contents(
            workspace.voicevox_dir,
            archive_dir / "VOICEVOX",
        )
        atomic_write_json(
            archive_dir / "archive_manifest.json",
            {
                "schema": "docurandom-skipped-session-v1",
                "archive_id": archive_id,
                "archived_at_vietnam": completed.isoformat(),
                "shift_id": str(shift_id),
                "occurrence_id": str(occurrence_id),
                "reason": explanation,
                "moved_items": moved_items,
            },
        )
        for disposable in (
            workspace.pipeline_checkpoint_path,
            workspace.flow_history_path,
            workspace.state_dir / "youtube_upload.json",
            workspace.state_dir / "manual_youtube_upload.json",
            workspace.state_dir / "thumbnail-ai.json",
        ):
            try:
                disposable.unlink(missing_ok=True)
            except OSError as exc:
                raise SessionArchiveError(
                    f"Đã tách phiên lỗi nhưng chưa dọn được {disposable.name}: {exc}"
                ) from exc
        return SkippedSessionArchive(
            archive_id=archive_id,
            archive_dir=archive_dir,
            moved_items=moved_items,
            reason=explanation,
        )
