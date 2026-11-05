"""Validate manual completion before retiring an old automation failure."""

from datetime import datetime
import json
from pathlib import Path

from .workspace import Workspace
from .youtube_upload import YouTubeUploadError, parse_content_package


def _read(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _timestamp(value: object) -> float:
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError("Manual completion must have a timezone")
    return stamp.timestamp()


def confirmed_manual_upload(workspace: Workspace) -> dict[str, object] | None:
    """Match the human confirmation to the current rendered video, fail closed.

    Old markers have no file identity, so require the exact render checkpoint,
    package title and completion time too. Never infer success for an unresolved
    browser-upload checkpoint. This function performs no upload or profile edit.
    """
    try:
        marker = _read(workspace.state_dir / "manual_youtube_upload.json")
        if marker.get("status") != "manual_complete":
            return None
        video = Path(str(marker["video_path"])).resolve(strict=True)
        package = Path(str(marker["package_path"])).resolve(strict=True)
        output = workspace.output_dir.resolve(strict=True)
        if not video.is_file() or not package.is_file():
            return None
        if not video.is_relative_to(output) or not package.is_relative_to(output):
            return None
        if parse_content_package(package).title != marker.get("title"):
            return None
        marked_at = _timestamp(marker["marked_at"])
        pipeline = _read(workspace.pipeline_checkpoint_path)
        render = pipeline["stages"]["render"]
        if render["status"] != "complete":
            return None
        if Path(str(render["artifacts"]["video"])).resolve(strict=True) != video:
            return None
        if _timestamp(render["updated_at"]) > marked_at:
            return None
        for name, path in (("video", video), ("package", package)):
            stat = path.stat()
            if stat.st_mtime > marked_at:
                return None
            identity = marker.get(name + "_identity")
            if identity is not None and identity != {
                "size": stat.st_size, "mtime_ns": stat.st_mtime_ns
            }:
                return None
        upload_path = workspace.state_dir / "youtube_upload.json"
        upload = _read(upload_path)
        if upload_path.is_file() and not upload:
            return None
        if upload and upload.get("status") != "complete":
            return None
        if upload:
            if Path(str(upload.get("video_path", ""))).resolve() != video:
                return None
        return marker
    except (KeyError, TypeError, ValueError, OSError, YouTubeUploadError):
        return None


def manual_upload_marker(video: Path, package: Path, *, title: str, profile_id: str) -> dict[str, object]:
    def identity(path: Path) -> dict[str, int]:
        stat = path.stat()
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    return {
        "version": 2,
        "status": "manual_complete",
        "title": title,
        "profile_id": profile_id,
        "video_path": str(video.resolve(strict=True)),
        "package_path": str(package.resolve(strict=True)),
        "video_identity": identity(video),
        "package_identity": identity(package),
        "marked_at": datetime.now().astimezone().isoformat(),
    }


def clear_completed_manual_launch_block(workspace: Workspace, settings: dict[str, object]) -> bool:
    """Release only a failure preceding confirmation of this exact session.

    Donut still checks authorization on every new launch. A newer refusal stays
    blocked even if an older manual marker remains on disk.
    """
    if not settings.get("youtube_profile_launch_blocked"):
        return False
    selected = str(settings.get("youtube_profile_id") or "").strip()
    blocked = str(settings.get("youtube_profile_launch_blocked_profile_id") or "").strip()
    if not selected or blocked != selected:
        return False
    marker = confirmed_manual_upload(workspace)
    if marker is None or marker.get("profile_id", selected) != selected:
        return False
    blocked_at = settings.get("youtube_profile_launch_blocked_at")
    if blocked_at:
        try:
            if _timestamp(blocked_at) > _timestamp(marker["marked_at"]):
                return False
        except (ValueError, TypeError, KeyError):
            return False
    settings.update({
        "youtube_profile_launch_blocked": False,
        "youtube_profile_launch_block_reason": "",
        "youtube_profile_launch_blocked_profile_id": "",
        "youtube_profile_launch_blocked_at": "",
    })
    return True
