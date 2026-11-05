"""Recover an interrupted Content row without opening a new upload wizard."""
from __future__ import annotations

import re
import json
import logging
from pathlib import Path
import threading
import time
import unicodedata
import urllib.parse
from typing import Any

from .youtube_upload import (
    YouTubeUploadUncertain, YouTubeStudioUploader, YouTubeUploadRequest,
    WorkspaceUploadSources, YouTubeMetadata, _CdpSession, _SemanticCdpController,
    _mcp_cdp_page_ws_urls, _cdp_page_target_id, _ensure_youtube_browser_bridge,
)
from .donut_manager import resolve_donut_mcp_endpoint
from .donut_mcp import DonutMcpClient
from .workspace import Workspace

logger = logging.getLogger(__name__)


def _walk(node: dict[str, Any]):
    yield node
    for key in ("children", "shadowRoots"):
        for child in node.get(key, []):
            yield from _walk(child)


def _attrs(node: dict[str, Any]) -> dict[str, str]:
    raw = node.get("attributes", [])
    return dict(zip(raw[::2], raw[1::2]))


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def exact_interrupted_file_input(
    document: dict[str, Any], title: str, original_filename: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Find one existing row using its full title AND requested source name.

    The caller must also prove the file/profile hash against the immutable
    upload identity and receive partial feedback for the checkpointed video
    ID. This helper alone never authorizes attachment or creates a session.
    """
    expected_title = _normalized(title)
    expected_file = _normalized(original_filename)
    if not expected_title or not expected_file:
        return None
    matches = []
    for row in _walk(document):
        if row.get("nodeName") != "YTCP-VIDEO-ROW":
            continue
        nodes = list(_walk(row))
        titles = [n for n in nodes if _attrs(n).get("id") == "video-title"]
        if not any(_normalized(_attrs(n).get("aria-label", "")) == expected_title for n in titles):
            continue
        text = _normalized(" ".join(n.get("nodeValue", "") for n in nodes if n.get("nodeName") == "#text"))
        if not any(marker in text for marker in (
            "quá trình tải lên bị gián đoạn", "upload interrupted", "upload was interrupted",
        )):
            continue
        if not re.search(r"(?<![\w.])" + re.escape(expected_file) + r"(?![\w.])", text):
            continue
        controls = [n for n in nodes if n.get("nodeName") == "YTCP-BUTTON"
                    and "resume-upload-button" in _attrs(n).get("class", "").split()
                    and "hidden" not in _attrs(n)
                    and _attrs(n).get("aria-disabled") != "true"]
        inputs = [n for n in nodes if n.get("nodeName") == "INPUT" and _attrs(n).get("type") == "file"]
        if len(controls) != 1 or len(inputs) != 1:
            continue
        matches.append((inputs[0], controls[0]))
    if len(matches) > 1:
        raise YouTubeUploadUncertain("Nhiều bản nháp trùng tiêu đề và tên file; không gắn file vào hàng đoán.")
    return matches[0] if matches else None


def exact_content_row(document: dict[str, Any], video_id: str) -> dict[str, Any] | None:
    matches = []
    for row in _walk(document):
        if row.get("nodeName") != "YTCP-VIDEO-ROW":
            continue
        ids = set()
        for node in _walk(row):
            for key, value in _attrs(node).items():
                if key not in {"href", "src", "data-video-id", "video-id"}:
                    continue
                if key in {"data-video-id", "video-id"}:
                    ids.add(value)
                parsed = urllib.parse.urlsplit(value)
                if parsed.hostname in {"youtube.com", "www.youtube.com"} and parsed.path == "/watch":
                    ids.update(urllib.parse.parse_qs(parsed.query).get("v", ()))
                match = re.search(r"/(?:video|vi(?:_[A-Za-z0-9]+)?)/([A-Za-z0-9_-]{6,20})/", parsed.path)
                if match:
                    ids.add(match.group(1))
        if video_id in ids:
            matches.append(row)
    if len(matches) > 1:
        raise YouTubeUploadUncertain("Nhiều hàng cùng video ID; không chọn phần trăm của video khác.")
    return matches[0] if matches else None


class ArchivedUploadRecovery(YouTubeStudioUploader):
    """Monitor a proven old transfer without replacing the successor's state."""

    @staticmethod
    def _checkpoint_path(workspace: Workspace) -> Path:
        return workspace.state_dir / "youtube_archived_recovery.json"

    def recover(self, workspace: Workspace, cancel: threading.Event) -> None:
        checkpoint = json.loads(self._checkpoint_path(workspace).read_text(encoding="utf-8-sig"))
        if checkpoint.get("status") in {"complete", "deferred_public"}:
            return
        request = YouTubeUploadRequest(
            workspace, resolve_donut_mcp_endpoint(), str(checkpoint["profile_id"]),
            privacy="PUBLIC", hold_public_until_next_morning=True,
            contains_synthetic_media=checkpoint.get("contains_synthetic_media", True),
        )
        sources = WorkspaceUploadSources(
            Path(checkpoint["video_path"]), Path(checkpoint["package_path"]),
            Path(checkpoint["thumbnail_path"]) if checkpoint.get("thumbnail_path") else None,
            YouTubeMetadata(checkpoint["title"], checkpoint["description"], tags=tuple(checkpoint.get("seo_tags", ()))),
        )
        identity = self._request_identity(request, sources)
        record = self._registry_get(identity)
        if (identity != checkpoint.get("identity")
                or record.get("attempt_id") != checkpoint.get("attempt_id")
                or record.get("profile_id") != request.profile_id
                or record.get("archived_upload_recovery") is not True):
            raise YouTubeUploadUncertain("Bản lưu phục hồi không khớp khóa file/profile/lượt cũ.")
        attempt = str(record["attempt_id"])
        url = str(record["youtube_url"])
        from .youtube_upload import _youtube_video_id
        video_id = _youtube_video_id(url)
        if not video_id:
            raise YouTubeUploadUncertain("Lượt cũ thiếu video ID; không tạo lượt upload khác.")
        def report(percent: float, detail: str) -> None:
            logger.info("archived_youtube_recovery workspace=%s video=%s progress=%.1f %s", workspace.name, video_id, percent, detail)
        with self.coordinator.acquire(request.profile_id, cancel, report, job_kind="upload", priority=300, preempt_lower=False):
            if record.get("transfer_complete") is not True:
                with DonutMcpClient(request.endpoint, timeout=30) as client:
                    profile = client.get_profile(request.profile_id)
                    if not profile.process_id:
                        raise YouTubeUploadUncertain("Profile lượt cũ đã đóng; giữ file và checkpoint phục hồi.")
                    debug = client.get_debug_connection(request.profile_id)
                    ws = next((s for s in _mcp_cdp_page_ws_urls(debug)
                               if _cdp_page_target_id(s) == record.get("target_id")), "")
                    if not ws:
                        raise YouTubeUploadUncertain("Tab truyền cũ không còn; không gắn lại file tự động.")
                    bridge = _ensure_youtube_browser_bridge(client, profile.profile_id)
                    with _CdpSession(ws) as cdp:
                        controller = _SemanticCdpController(
                            cdp, browser_bridge=bridge, bridge_profile_id=profile.profile_id,
                            bridge_tab_id=int(record["bridge_tab_id"]),
                            automation_target_id=str(record["target_id"]),
                        )
                        last_percent = -1.0
                        last_progress_at = time.monotonic()
                        complete = False
                        while time.monotonic() - last_progress_at < 30 * 60:
                            self._cancelled(cancel)
                            document = cdp.command("DOM.getDocument", {"depth": -1, "pierce": True})["root"]
                            row = exact_content_row(document, video_id)
                            text = " ".join(n.get("nodeValue", "") for n in _walk(row) if n.get("nodeName") == "#text") if row else ""
                            if "quá trình tải lên bị gián đoạn" in _normalized(text):
                                raise YouTubeUploadUncertain("Lượt truyền cũ lại bị gián đoạn; không tự gắn file lần hai.")
                            percent, remaining = controller._parse_youtube_upload_progress(text)
                            if percent is None:
                                percent, remaining = cdp.upload_feedback(video_id, timeout=12)
                            if percent is not None:
                                if percent > last_percent:
                                    last_progress_at = time.monotonic()
                                    last_percent = percent
                                    report(percent, "Đang nối đúng lượt cũ" + (" · còn " + remaining if remaining else ""))
                                    self._registry_update_attempt(identity, attempt, {"transfer_feedback_percent": percent})
                                    self._checkpoint(request, sources, status="uploading", progress=percent, detail="Đang nối đúng video cũ; video kế tiếp giữ nguyên", extra={
                                        "attempt_id": attempt, "youtube_url": url,
                                        "target_id": record["target_id"], "bridge_tab_id": record["bridge_tab_id"],
                                        "transfer_complete": False, "archived_upload_recovery": True,
                                    })
                                if percent >= 100:
                                    complete = True
                                    break
                            cancel.wait(5)
                        if not complete:
                            raise YouTubeUploadUncertain("Lượt cũ không tiến triển 30 phút; checkpoint vẫn được giữ.")
                self._registry_update_attempt(identity, attempt, {"transfer_complete": True, "transfer_requires_content_resume": False, "status": "submitting"})
            record = self._registry_get(identity)
            try:
                self._reconcile_submitted_locked_inner(request, sources, record, report, cancel, ownership={})
            except Exception as exc:
                self._cancelled(cancel)
                self._recover_locked_upload(
                    request, sources, identity, report, cancel, exc, metadata_only=True,
                )
            report(100, "Đã xác minh đủ tệp và metadata Unlisted; chờ lịch Public")


def start_archived_upload_recoveries(uploader: YouTubeStudioUploader, workspaces, cancel: threading.Event) -> None:
    for workspace in workspaces:
        path = ArchivedUploadRecovery._checkpoint_path(workspace)
        if not path.is_file():
            continue
        def work(selected=workspace):
            try:
                ArchivedUploadRecovery(uploader.coordinator, registry_path=uploader.registry_path).recover(selected, cancel)
            except Exception:
                logger.exception("archived_youtube_recovery_preserved workspace=%s", selected.name)
        threading.Thread(target=work, name=f"archived-youtube-{workspace.name}", daemon=True).start()
