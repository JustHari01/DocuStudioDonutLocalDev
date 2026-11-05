from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import MediaInfo, RenderPlan, RenderSettings, ScenePlan, TransitionPlan
from .storage import atomic_write_json


RENDER_CHECKPOINT_VERSION = 1
RENDER_CHECKPOINT_FOLDER = ".docurandom-render-checkpoints"
_TRANSIENT_SETTING_FIELDS = {
    "hardware_accelerated",
    "parallel_jobs",
    "performance_mode",
    "runtime_summary",
    "threads_per_job",
    "video_encoder",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolved_text(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _file_identity(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve(strict=False)
    value: dict[str, object] = {"path": str(resolved)}
    try:
        stat = resolved.stat()
    except OSError:
        value["missing"] = True
    else:
        value.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    return value


def render_request_identity(
    media: list[MediaInfo] | tuple[MediaInfo, ...],
    settings: RenderSettings,
) -> str:
    """Return the stable identity of one resumable render request.

    Runtime scheduling/encoder choices are intentionally excluded. A job can
    therefore resume after an app update or after falling back from GPU to CPU,
    while any source, audio, subtitle, visual setting, or output-name change
    creates a different checkpoint.
    """

    settings_value: dict[str, object] = {}
    for item in fields(settings):
        if item.name in _TRANSIENT_SETTING_FIELDS:
            continue
        value = getattr(settings, item.name)
        settings_value[item.name] = (
            _resolved_text(value) if isinstance(value, Path) else value
        )

    media_value: list[dict[str, object]] = []
    for item in media:
        entry = asdict(item)
        entry["path"] = _resolved_text(item.path)
        entry["file"] = _file_identity(item.path)
        media_value.append(entry)

    payload = {
        "checkpoint_version": RENDER_CHECKPOINT_VERSION,
        "media": media_value,
        "settings": settings_value,
        "audio_file": _file_identity(settings.audio_path),
        "subtitle_file": _file_identity(settings.subtitle_path),
        "cta_file": _file_identity(settings.cta_path),
        "logo_file": _file_identity(settings.logo_path),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def render_checkpoint_dir(
    media: list[MediaInfo] | tuple[MediaInfo, ...],
    settings: RenderSettings,
) -> Path:
    identity = render_request_identity(media, settings)
    return settings.output_dir / RENDER_CHECKPOINT_FOLDER / identity


def _checkpoint_path(directory: Path) -> Path:
    return directory / "checkpoint.json"


def read_render_checkpoint(directory: Path) -> dict[str, Any]:
    try:
        value = json.loads(_checkpoint_path(directory).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def save_render_plan_checkpoint(
    directory: Path,
    *,
    request_identity: str,
    plan: RenderPlan,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    previous = read_render_checkpoint(directory)
    created_at = str(previous.get("created_at", "")).strip() or _now()
    atomic_write_json(
        _checkpoint_path(directory),
        {
            "version": RENDER_CHECKPOINT_VERSION,
            "request_identity": request_identity,
            "created_at": created_at,
            "updated_at": _now(),
            "status": "running",
            "completed_scenes": 0,
            "total_scenes": len(plan.scenes),
            "plan": plan.to_dict(),
        },
    )


def load_render_plan_checkpoint(
    directory: Path,
    *,
    request_identity: str,
) -> RenderPlan | None:
    document = read_render_checkpoint(directory)
    if (
        document.get("version") != RENDER_CHECKPOINT_VERSION
        or document.get("request_identity") != request_identity
    ):
        return None
    value = document.get("plan")
    if not isinstance(value, dict):
        return None
    try:
        plan_value = dict(value)
        scenes = plan_value.get("scenes")
        transitions = plan_value.get("transitions")
        if not isinstance(scenes, list) or not isinstance(transitions, list):
            return None
        plan_value["scenes"] = [
            ScenePlan(**dict(item)) for item in scenes if isinstance(item, dict)
        ]
        plan_value["transitions"] = [
            TransitionPlan(**dict(item))
            for item in transitions
            if isinstance(item, dict)
        ]
        plan = RenderPlan(**plan_value)
    except (TypeError, ValueError):
        return None
    if len(plan.scenes) != len(scenes) or len(plan.transitions) != len(transitions):
        return None
    if not plan.seed or not plan.manifest_signature:
        return None
    return plan


def update_render_checkpoint_state(
    directory: Path,
    *,
    status: str,
    completed_scenes: int,
    total_scenes: int,
    detail: str = "",
) -> None:
    document = read_render_checkpoint(directory)
    if not document:
        return
    document.update(
        {
            "updated_at": _now(),
            "status": status,
            "completed_scenes": max(0, int(completed_scenes)),
            "total_scenes": max(0, int(total_scenes)),
            "detail": str(detail),
        }
    )
    atomic_write_json(_checkpoint_path(directory), document)
