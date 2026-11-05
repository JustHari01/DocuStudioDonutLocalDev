"""Repair only the assets of an exact archived Unlisted video."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from PIL import Image

from .content_ai import CHATGPT_WEB_AUTO_MODEL, ChatGPTWebChat
from .storage import atomic_write_json
from .narration_identity import narration_digest, package_narration, validate_project_narration
from .voicevox import VoicevoxError
from .thumbnail_ai import extract_complete_thumbnail_prompt, thumbnail_generation_request
from .thumbnail_manager import install_workspace_thumbnail
from .workspace import Workspace
from .youtube_upload import WorkspaceUploadSources, YouTubeUploadError, parse_content_package


def _read(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise YouTubeUploadError("Checkpoint nguyên liệu Public không hợp lệ; giữ video.")
    return value


def valid_thumbnail(path: str | Path) -> bool:
    if not str(path).strip():
        return False
    try:
        with Image.open(path) as image:
            if image.width < 320 or image.height < 180:
                return False
            image.verify()
        return True
    except (OSError, ValueError):
        return False


def publication_package(item: dict):
    archive = Path(str(item.get("archive_dir") or "")).resolve()
    package = Path(str(item.get("package_path") or "")).resolve()
    if not item.get("archive_dir") or not package.is_relative_to(archive):
        raise YouTubeUploadError("Package Public không thuộc đúng phiên đã upload.")
    metadata = parse_content_package(package)
    if not metadata.title.strip() or not metadata.description.strip() or not metadata.tags:
        raise YouTubeUploadError("Thiếu tiêu đề, mô tả hoặc SEO tags; giữ Không công khai.")
    from .content_ai import content_package_problems
    if problems:=content_package_problems(package):
        raise YouTubeUploadError('CONTENT_PACKAGE_INCONSISTENT: '+ '; '.join(problems.values())+'; giữ Không công khai.')
    # Current workspace C can no longer prove the narration of queued B.
    # Inspect B's own archived project and checkpoint, never the live paths
    # retained in that checkpoint before the archive was moved.
    checkpoint = _read(archive / "State" / "pipeline_checkpoint.json")
    voice = checkpoint.get("stages", {}).get("voicevox", {})
    artifacts = voice.get("artifacts", {}) if isinstance(voice, dict) else {}
    # Archiving prefixes the package with a timestamp, but preserves the
    # VOICEVOX filenames. Rebase only the recorded filename inside this
    # archive; the checkpoint's former live path must never be read.
    recorded_project = str(artifacts.get("audited_project") or "") if isinstance(artifacts, dict) else ""
    project_name = Path(recorded_project).name if recorded_project else f"{package.stem}_VOICEVOX_FINAL_audited.vvproj"
    project_dir = (archive / "VOICEVOX").resolve()
    project = (project_dir / project_name).resolve()
    try:
        if project.parent != project_dir or not project.is_relative_to(archive):
            raise VoicevoxError("Project giọng không thuộc đúng phiên đã upload.")
        if project.is_file():
            validate_project_narration(package, project)
        elif isinstance(artifacts, dict) and artifacts.get("audit_status") == "applied":
            raise VoicevoxError("Thiếu đúng project giọng đã audit của video trong hàng chờ.")
        digest = str(artifacts.get("script_sha256") or "") if isinstance(artifacts, dict) else ""
        if digest and digest != narration_digest(package_narration(package)):
            raise VoicevoxError("Giọng đọc trong hàng chờ thuộc video khác.")
    except VoicevoxError as exc:
        raise YouTubeUploadError(f"Giữ Không công khai: {exc}") from exc
    return archive, package, metadata


def publication_sources(item: dict) -> WorkspaceUploadSources:
    _archive, package, metadata = publication_package(item)
    thumbnail = str(item.get("thumbnail_path") or "").strip()
    if not thumbnail or not valid_thumbnail(thumbnail):
        raise YouTubeUploadError("Thiếu thumbnail hợp lệ; phải tạo và xác minh trước Public.")
    return WorkspaceUploadSources(Path(str(item.get("video_path") or "")), package,
                                  Path(thumbnail), metadata)


def commit_publication_thumbnail(workspace: Workspace, item: dict, thumbnail: Path) -> dict:
    """Update an existing queue identity only; never resurrect a removed video."""
    path = workspace.state_dir / "deferred_public_queue.json"
    value = _read(path)
    entries = value.get("items")
    if not isinstance(entries, list):
        raise YouTubeUploadError("Hàng đợi Public đã đổi; không gắn thumbnail nhầm video.")
    for entry in entries:
        if (isinstance(entry, dict) and entry.get("identity") == item.get("identity")
                and entry.get("youtube_url") == item.get("youtube_url")
                and entry.get("package_path") == item.get("package_path")):
            entry["thumbnail_path"] = str(thumbnail)
            atomic_write_json(path, value)
            return dict(entry)
    raise YouTubeUploadError("Video đã rời hàng đợi; giữ ảnh, không tạo lại mục Public.")


def ensure_publication_assets(workspace: Workspace, item: dict, status, pool, cancel,
                              report) -> tuple[dict, WorkspaceUploadSources]:
    """Generate/recover one image with a durable lease; do not touch current C."""
    try:
        return item, publication_sources(item)
    except YouTubeUploadError:
        pass
    archive = Path(str(item.get("archive_dir") or "")).resolve()
    package = Path(str(item.get("package_path") or "")).resolve()
    if not item.get("archive_dir") or not package.is_file() or not package.is_relative_to(archive):
        raise YouTubeUploadError("Không có đúng package đã upload để bổ sung thumbnail.")
    # Only thumbnail failures are repaired here. A narration mismatch must
    # never spend another image turn or proceed to Public with a new picture.
    publication_package(item)
    metadata = parse_content_package(package)
    if not metadata.title.strip() or not metadata.description.strip() or not metadata.tags:
        raise YouTubeUploadError("Package thiếu metadata bắt buộc; không tạo ảnh hoặc Public.")
    if not getattr(status, "operational", False):
        raise YouTubeUploadError("Đang chờ ChatGPT Web sẵn sàng để bổ sung thumbnail.")
    state_path = archive / "State" / "publication-thumbnail-repair.json"
    state = _read(state_path)
    package_hash = hashlib.sha256(package.read_bytes()).hexdigest()
    if state and (state.get("identity") != item.get("identity") or
                  state.get("package_sha256") != package_hash):
        raise YouTubeUploadError("Checkpoint sửa thumbnail khác video/package; giữ nguyên.")
    if state.get("thumbnail_path") and valid_thumbnail(str(state["thumbnail_path"])):
        updated = commit_publication_thumbnail(workspace, item, Path(state["thumbnail_path"]))
        return updated, publication_sources(updated)
    if state.get("status") == "terminal_no_image" and state.get("thread_id"):
        # The user may have retried successfully in the visible ChatGPT window.
        # Collect that result before any cooldown or automatic resubmission.
        existing_chat = ChatGPTWebChat(status.endpoint, CHATGPT_WEB_AUTO_MODEL, package.parent,
                                      status.access_token, thread_id=str(state["thread_id"]))
        existing = existing_chat.read_existing_generated_image()
        if existing is not None:
            repair_workspace = replace(workspace, root_dir=str(archive / "PublicationRepair"))
            installed = install_workspace_thumbnail(repair_workspace, existing.data)
            state.update(status="complete", submitted=False, thumbnail_path=str(installed.path),
                         updated_at=datetime.now(timezone.utc).isoformat(),
                         detail="Đã thu hồi ảnh từ lượt thử lại trong đúng hội thoại.")
            state.pop("next_retry_at", None)
            atomic_write_json(state_path, state)
            updated = commit_publication_thumbnail(workspace, item, installed.path)
            return updated, publication_sources(updated)
    now = datetime.now(timezone.utc)
    retry_at = state.get("next_retry_at")
    if retry_at and now < datetime.fromisoformat(str(retry_at)):
        raise YouTubeUploadError("Thumbnail đang chờ lượt thử tiếp theo; giữ Không công khai.")
    old = _read(archive / "State" / "thumbnail-ai.json") if not state else state
    pending = old.get("submitted") is True and old.get("status") != "complete"
    thread = str(old.get("thread_id") or "") if pending else ""
    if pending and not thread:
        raise YouTubeUploadError("Lượt ảnh cũ chưa rõ kết quả và thiếu thread; chưa gửi ảnh mới.")
    attempts = int(state.get("attempts") or 0)
    if attempts >= 3 and not pending:
        last = datetime.fromisoformat(str(state["updated_at"]))
        if now < last + timedelta(hours=24):
            raise YouTubeUploadError("Ba lượt tạo thumbnail chưa thành công; giữ video và chờ nghỉ 24 giờ.")
        attempts = 0
    acquired = pool.acquire(cancel_event=cancel, label=f"{workspace.name} · bổ sung thumbnail Public",
                            on_wait=lambda p, a, limit: report(2, "Đang chờ lượt ChatGPT bổ sung thumbnail…"))
    if not acquired:
        raise YouTubeUploadError("Đã hủy chờ bổ sung thumbnail; giữ Không công khai.")
    chat = None
    try:
        chat = ChatGPTWebChat(status.endpoint, CHATGPT_WEB_AUTO_MODEL, package.parent,
                              status.access_token, thread_id=thread or None,
                              response_id=str(old.get("response_id") or "") if pending else "")
        state = {"identity": item["identity"], "package_sha256": package_hash,
                 "thread_id": chat.thread_id, "response_id": chat.response_id,
                 "status": "recovering" if pending else "running", "submitted": True,
                 "attempts": attempts if pending else attempts + 1,
                 "updated_at": now.isoformat()}
        # Persist before any request. A transport error thereafter permits read-only recovery.
        atomic_write_json(state_path, state)
        on_status = lambda stage, message: report(3, f"Thumbnail · {message}")
        if pending:
            value = chat.recover_pending(cancel_event=cancel, on_status=on_status,
                                         output_mode="generated_image")
            generated = chat.parse_generated_image_response(value)
        else:
            generated = chat.generate_image(thumbnail_generation_request(
                extract_complete_thumbnail_prompt(package)), cancel_event=cancel, on_status=on_status)
        repair_workspace = replace(workspace, root_dir=str(archive / "PublicationRepair"))
        installed = install_workspace_thumbnail(repair_workspace, generated.data)
        if cancel.is_set():
            raise YouTubeUploadError("Đã hủy sửa thumbnail; video vẫn Không công khai.")
        state.update(status="complete", submitted=False, thumbnail_path=str(installed.path),
                     response_id=chat.response_id, updated_at=datetime.now(timezone.utc).isoformat())
        atomic_write_json(state_path, state)
        updated = commit_publication_thumbnail(workspace, item, installed.path)
        report(5, "Đã có thumbnail hợp lệ cho đúng video Unlisted; chưa chuyển Public.")
        return updated, publication_sources(updated)
    except Exception as exc:
        if state.get("status") != "complete":
            terminal = "CHATGPT_TERMINAL_RESPONSE_NO_IMAGE:" in str(exc)
            state.update(status="terminal_no_image" if terminal else "uncertain",
                         submitted=not terminal, detail=str(exc)[:1000],
                         response_id=chat.response_id if chat else "",
                         updated_at=datetime.now(timezone.utc).isoformat(),
                         next_retry_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat())
            atomic_write_json(state_path, state)
        raise
    finally:
        pool.release()
