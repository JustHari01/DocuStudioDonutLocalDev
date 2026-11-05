from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import atomic_write_json


PIPELINE_STAGES = (
    "precheck",
    "chatgpt",
    "package",
    "flow_images",
    "voicevox",
    "watermark",
    "render",
)
_LOCK = threading.RLock()
_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}


@dataclass(frozen=True)
class WorkspacePipelineOutcome:
    """Durable workspace status reconstructed without opening the editor."""

    state: str
    status: str
    progress: float
    video_paths: tuple[Path, ...] = ()
    updated_at: str = ""
    resume_stage: str = "precheck"

    @property
    def video_name(self) -> str:
        return self.video_paths[0].name if self.video_paths else ""

    @property
    def video_count(self) -> int:
        return len(self.video_paths)

    def to_activity(self) -> dict[str, object]:
        return {
            "active": False,
            "progress": self.progress,
            "status": self.status,
            "state": self.state,
            "video_name": self.video_name,
            "video_path": str(self.video_paths[0]) if self.video_paths else "",
            "video_count": self.video_count,
            "updated_at": self.updated_at,
            "resume_stage": self.resume_stage,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_pipeline_checkpoint(path: Path) -> dict[str, Any]:
    import json

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def render_completed_by(
    path: Path,
    deadline: datetime,
    *,
    output_dir: Path,
) -> bool:
    """Prove the current output finished before an upload occurrence.

    A scheduler wake-up after the clock is not evidence of a late render.
    Match the durable render artifact and reject files changed after that
    completion, so a reused filename cannot inherit an old completion time.
    """
    try:
        document = read_pipeline_checkpoint(path)
        stages = document.get("stages", {})
        render = stages.get("render", {})
        if render.get("status") != "complete":
            return False
        completed_at = datetime.fromisoformat(
            str(render.get("updated_at") or "").replace("Z", "+00:00")
        )
        if completed_at.tzinfo is None or deadline.tzinfo is None:
            return False
        if completed_at > deadline:
            return False
        video = Path(render["artifacts"]["video"]).resolve(strict=False)
        video.relative_to(output_dir.resolve(strict=False))
        stat = video.stat()
        return bool(
            video.is_file()
            and video.suffix.casefold() in _VIDEO_EXTENSIONS
            and stat.st_size > 0
            and stat.st_mtime <= completed_at.timestamp()
        )
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return False


def update_pipeline_stage(
    path: Path,
    *,
    identity: dict[str, object],
    stage: str,
    status: str,
    detail: str = "",
    artifacts: dict[str, object] | None = None,
    master_manifest: dict[str, object] | None = None,
) -> dict[str, Any]:
    if stage not in PIPELINE_STAGES:
        raise ValueError(f"Unknown pipeline stage: {stage}")
    with _LOCK:
        document = read_pipeline_checkpoint(path)
        if document.get("identity") != identity:
            document = {
                "version": 1,
                "identity": identity,
                "created_at": _now(),
                "stages": {},
            }
        stages = document.get("stages")
        if not isinstance(stages, dict):
            stages = {}
            document["stages"] = stages
        stage_value: dict[str, object] = {
            "status": status,
            "detail": detail,
            "updated_at": _now(),
        }
        if artifacts:
            stage_value["artifacts"] = artifacts
        stages[stage] = stage_value
        document["updated_at"] = _now()
        if master_manifest is not None:
            document['master_manifest'] = master_manifest
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, document)
        return document


def pipeline_resume_summary(path: Path) -> tuple[str, list[str]]:
    document = read_pipeline_checkpoint(path)
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, dict):
        return "precheck", ["Chưa có checkpoint quy trình; sẽ kiểm tra từ đầu."]
    labels = {
        "precheck": "Precheck",
        "chatgpt": "ChatGPT",
        "package": "File tổng hợp",
        "flow_images": "Tạo ảnh Flow",
        "voicevox": "VOICEVOX + SRT",
        "watermark": "Xóa watermark",
        "render": "Render video",
    }
    report: list[str] = []
    resume_stage = "precheck"
    found_incomplete = False
    for stage in PIPELINE_STAGES:
        value = stages.get(stage)
        status = str(value.get("status", "missing")) if isinstance(value, dict) else "missing"
        detail = str(value.get("detail", "")) if isinstance(value, dict) else ""
        report.append(f"{labels[stage]}: {status}" + (f" · {detail}" if detail else ""))
        if not found_incomplete and status not in {"complete", "skipped"}:
            resume_stage = stage
            found_incomplete = True
    if not found_incomplete:
        resume_stage = "complete"
    return resume_stage, report


def inspect_workspace_pipeline(
    checkpoint_path: Path,
    history_path: Path,
    output_dir: Path,
) -> WorkspacePipelineOutcome:
    """Recover completion/error/video names after the application restarts."""

    import json

    document = read_pipeline_checkpoint(checkpoint_path)
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, dict):
        stages = {}

    candidates: dict[str, Path] = {}

    def add_video(raw: object) -> None:
        text = str(raw or "").strip().strip('"')
        if not text:
            return
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = output_dir / path
        path = path.resolve(strict=False)
        if path.is_file() and path.suffix.casefold() in _VIDEO_EXTENSIONS:
            candidates[str(path).casefold()] = path

    render_value = stages.get("render")
    if isinstance(render_value, dict):
        artifacts = render_value.get("artifacts")
        if isinstance(artifacts, dict):
            add_video(artifacts.get("video"))

    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
    except Exception:
        history = {}
    renders = history.get("renders") if isinstance(history, dict) else None
    if isinstance(renders, list):
        for item in renders:
            if isinstance(item, dict):
                add_video(item.get("output_file"))

    try:
        for path in output_dir.iterdir():
            if (
                path.is_file()
                and path.suffix.casefold() in _VIDEO_EXTENSIONS
                and not path.name.startswith(".")
            ):
                add_video(path)
    except OSError:
        pass

    def modified(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    videos = tuple(sorted(candidates.values(), key=modified, reverse=True))
    updated_at = str(document.get("updated_at") or "") if document else ""
    if not updated_at and videos:
        updated_at = datetime.fromtimestamp(
            modified(videos[0]), timezone.utc
        ).isoformat()

    stage_labels = {
        "precheck": "kiểm tra ban đầu",
        "chatgpt": "viết nội dung ChatGPT",
        "package": "xuất file tổng hợp",
        "flow_images": "tạo ảnh Flow",
        "voicevox": "tạo giọng và subtitle",
        "watermark": "xử lý ảnh",
        "render": "render video",
    }
    resume_stage, _report = pipeline_resume_summary(checkpoint_path)
    completed = 0
    issue_stage = ""
    issue_status = ""
    issue_detail = ""
    for stage in PIPELINE_STAGES:
        value = stages.get(stage)
        status = str(value.get("status", "missing")) if isinstance(value, dict) else "missing"
        detail = str(value.get("detail", "")) if isinstance(value, dict) else ""
        if status in {"complete", "skipped"}:
            completed += 1
        if status in {"error", "cancelled", "paused"}:
            issue_stage, issue_status, issue_detail = stage, status, detail

    progress = 100.0 * completed / max(1, len(PIPELINE_STAGES))
    render_status = (
        str(render_value.get("status", "missing"))
        if isinstance(render_value, dict)
        else "missing"
    )
    video_suffix = ""
    if videos:
        video_suffix = f" · Video: {videos[0].name}"
        if len(videos) > 1:
            video_suffix += f" (+{len(videos) - 1})"

    # A valid final render is the terminal truth. Earlier stage errors can
    # remain in an old checkpoint after a manual repair, but they must not
    # hide a video that the final render stage actually completed.
    if render_status == "complete" and videos:
        return WorkspacePipelineOutcome(
            "completed",
            f"Đã render xong · {videos[0].name}"
            + (f" · tổng {len(videos)} video" if len(videos) > 1 else ""),
            100.0,
            videos,
            updated_at,
            "complete",
        )
    if issue_status == "error":
        label = stage_labels.get(issue_stage, issue_stage)
        detail = issue_detail.strip() or "Mở workspace để xem log và tiếp tục."
        return WorkspacePipelineOutcome(
            "error",
            f"Lỗi ở {label}: {detail}{video_suffix}",
            progress,
            videos,
            updated_at,
            resume_stage,
        )
    if issue_status in {"cancelled", "paused"}:
        label = stage_labels.get(issue_stage, issue_stage)
        return WorkspacePipelineOutcome(
            "incomplete",
            f"Đang dở ở {label} · có thể bấm Tiếp tục{video_suffix}",
            progress,
            videos,
            updated_at,
            resume_stage,
        )
    if stages:
        label = stage_labels.get(resume_stage, resume_stage)
        if resume_stage == "complete" and not videos:
            status = "Checkpoint hoàn tất nhưng không tìm thấy file video"
        else:
            status = f"Chưa hoàn tất · đang dở từ {label}"
        return WorkspacePipelineOutcome(
            "incomplete",
            status + video_suffix,
            progress,
            videos,
            updated_at,
            resume_stage,
        )
    if videos:
        return WorkspacePipelineOutcome(
            "completed",
            f"Đã có video · {videos[0].name}"
            + (f" · tổng {len(videos)} video" if len(videos) > 1 else ""),
            100.0,
            videos,
            updated_at,
            "complete",
        )
    return WorkspacePipelineOutcome(
        "idle",
        "Chưa bắt đầu render",
        0.0,
        (),
        updated_at,
        "precheck",
    )
