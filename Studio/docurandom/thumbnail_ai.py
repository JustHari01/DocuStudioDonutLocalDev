from __future__ import annotations

from dataclasses import dataclass,replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import uuid

from .storage import atomic_write_json
from .workspace import Workspace
from .youtube_upload import parse_thumbnail_prompt


THUMBNAIL_AI_STATE_VERSION = 1
THUMBNAIL_AI_STATE_FILENAME = "thumbnail-ai.json"
THUMBNAIL_CONFIRMATION = "ĐÃ TẠO"
THUMBNAIL_AI_RECOVERY_STALE_SECONDS = 15 * 60
THUMBNAIL_AI_MAX_AUTOMATIC_RETRIES = 3
THUMBNAIL_AI_MAX_READONLY_PROBES = 4
THUMBNAIL_AI_MAX_RECOVERY_PROBES = 4
THUMBNAIL_AI_MAX_RECOVERY_CHATS = 3
THUMBNAIL_AI_MAX_FAILED_SOURCE_PROBES = 3


def thumbnail_source_read_exhausted(state: dict[str, object]) -> bool:
    """Old terminal receipts retain their failed-read budget across restarts.

    A known negative response permits source verification, not indefinite
    verification attempts. Bytes already collected remain eligible for review.
    """
    if state.get('status') != 'terminal_no_image' or state.get('staged_image'):
        return False
    # A finished failure can have a different, corrective strategy available.
    # Failed collection reads must not veto that strategy's own durable budget.
    if (thumbnail_canvas_repair_remaining_seconds(state) is not None
            or thumbnail_canvas_transport_remaining_seconds(state) is not None):
        return False
    count = state.get('retry_probe_failures', 0)
    if type(count) is not int or count < 0:
        return True
    return count >= THUMBNAIL_AI_MAX_FAILED_SOURCE_PROBES


def _recovery_generation_exhausted(state: dict[str, object]) -> bool:
    """New recovery reservations allow one request, never an extra repair send."""
    record = state.get('recovery_chat') or {}
    if not isinstance(record,dict) or record.get('policy_version') != 3: return False
    count = record.get('generation_attempts',1)
    return type(count) is not int or count < 0 or count >= 1


class ThumbnailAIError(RuntimeError):
    pass


@dataclass(frozen=True)
class ThumbnailAIClaim:
    action: str
    prompt: str
    prompt_sha256: str
    package_sha256: str
    existing_path: Path | None = None
    detail: str = ""
    read_only_collection: bool = False
    source_only: bool = False
    generation_request: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _state_path(workspace: Workspace) -> Path:
    return workspace.state_dir / THUMBNAIL_AI_STATE_FILENAME


def thumbnail_ai_automatic_recovery_remaining_seconds(
    state: dict[str, object],
) -> int | None:
    """Return the stale wait for a submitted thumbnail turn that can be probed.

    The result only schedules a read/recovery attempt. It never authorizes a
    resend; a fresh turn still requires the bridge's exact-thread idle proof.
    """

    status = str(state.get("status") or "").casefold()
    if _source_probe_upgrade_available(state):
        return 0
    if status == "delivery_retry_pending" and state.get("submitted") is False:
        try:
            updated = datetime.fromisoformat(str(state["updated_at"]).replace("Z", "+00:00"))
            if updated.tzinfo is None or int(state.get("delivery_retry_count") or 0) >= 1:
                return None
            age = max(0.0, (datetime.now(timezone.utc) - updated).total_seconds())
            return max(0, int(60 - age))
        except (ValueError, TypeError, KeyError, OverflowError):
            return None
    if (
        state.get("submitted") is not True
        or status not in {"error", "running", "submitted", "recovering", "uncertain", "confirmation_pending", "terminal_no_image", "image_received", "confirming"}
        or (
            (_automatic_retry_count(state) >= THUMBNAIL_AI_MAX_AUTOMATIC_RETRIES
             or _recovery_probe_count(state) >= THUMBNAIL_AI_MAX_RECOVERY_PROBES)
            and _readonly_probe_count(state) >= THUMBNAIL_AI_MAX_READONLY_PROBES
        )
    ):
        return None
    updated_at = str(state.get("updated_at") or "").strip()
    if not updated_at:
        return None
    try:
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None
    wait = (30 * 60 if _recovery_probe_count(state) >= THUMBNAIL_AI_MAX_RECOVERY_PROBES
            else THUMBNAIL_AI_RECOVERY_STALE_SECONDS)
    return max(0, int(wait - age))


def _source_probe_upgrade_available(state: dict) -> bool:
    return bool(state.get('submitted') is True and state.get('thread_id')
        and state.get('status')!='complete' and not state.get('staged_image')
        and _readonly_probe_count(state)>=THUMBNAIL_AI_MAX_READONLY_PROBES
        and int(state.get('source_read_version')or 0)<2)


def read_thumbnail_ai_state(workspace: Workspace) -> dict[str, object]:
    try:
        value = json.loads(_state_path(workspace).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def thumbnail_ai_pending_receipt_error(detail: object) -> bool:
    """Recognize a bridge lock; it is uncertainty, not a pre-submit failure."""
    text = str(detail or "").casefold()
    return any(marker in text for marker in (
        "chatgpt đang giữ một lượt chưa thu hồi",
        "chatgpt_pending_submission_locked",
        "chatgpt_recovery_pending:",
        "interrupted chatgpt turn; recover its dom before sending another prompt",
    ))


def thumbnail_ai_terminal_retry_remaining_seconds(state: dict[str, object]) -> int | None:
    """Retry a finished negative image turn, with a durable bounded budget."""
    if _recovery_generation_exhausted(state): return None
    detail = str(state.get("detail") or "").split('\nĐọc lại hội thoại:', 1)[0].casefold()
    if (
        state.get("status") != "terminal_no_image"
        or state.get("submitted") is not False
        or state.get("thumbnail_path")
        or state.get("recovery_chat")
        or not str(state.get("thread_id") or "").strip()
        or _automatic_retry_count(state) >= THUMBNAIL_AI_MAX_AUTOMATIC_RETRIES
        or "chatgpt_terminal_response_no_image:" not in detail
        or any(marker in detail for marker in (
            "policy", "chính sách", "quota", "rate limit", "giới hạn", "hết lượt", "captcha",
        ))
    ):
        return None
    try:
        updated = datetime.fromisoformat(str(state["updated_at"]).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            return None
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        if age < 0:
            return None
        wait = (30 * 60 if _readonly_probe_count(state) >= THUMBNAIL_AI_MAX_READONLY_PROBES
                else THUMBNAIL_AI_RECOVERY_STALE_SECONDS)
        return max(0, int(wait - age))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def thumbnail_recovery_chat_remaining_seconds(state: dict[str, object]) -> int | None:
    """Prefer a clean chat after a proved finished technical image failure.

    Eligibility schedules source verification, never a blind resend. Account
    cooldowns remain enforced by the shared ChatGPT queue.
    """
    if (_recovery_generation_exhausted(state)
            or thumbnail_canvas_repair_remaining_seconds(state) is not None
            or thumbnail_canvas_transport_remaining_seconds(state) is not None): return None
    detail=str(state.get('detail')or'').split('\nĐọc lại hội thoại:', 1)[0].casefold()
    record=state.get('recovery_chat')or{}
    try:
        count=int(record.get('count')or 0) if isinstance(record,dict) else THUMBNAIL_AI_MAX_RECOVERY_CHATS
    except (ValueError,TypeError,OverflowError):
        return None
    if (state.get('status')!='terminal_no_image' or state.get('submitted')is not False
        or state.get('readonly_collection') or state.get('staged_image') or state.get('thumbnail_path')
        or count<0 or count>=THUMBNAIL_AI_MAX_RECOVERY_CHATS or not state.get('thread_id')
        or 'chatgpt_terminal_response_no_image:' not in detail
        or any(marker in detail for marker in ('policy','chính sách','quota','rate limit','rate_limited','http 429','giới hạn','hết lượt','captcha','đăng nhập','sign in'))):return None
    try:
        updated=datetime.fromisoformat(str(state.get('retry_probe_failed_at')or state['updated_at']).replace('Z','+00:00'))
        if updated.tzinfo is None:return None
        age=(datetime.now(timezone.utc)-updated).total_seconds()
        failures=max(0,int(state.get('retry_probe_failures')or 0))
        wait=min(1800,60*2**min(max(0,failures-1),5))if failures else 60
        return max(0,int(wait-age))if age>=0 else None
    except(KeyError,ValueError,TypeError,OverflowError):return None


def thumbnail_recovery_retry_remaining_seconds(state: dict[str,object])->int|None:
    if _recovery_generation_exhausted(state): return None
    record=state.get('recovery_chat')or{}
    detail=str(state.get('detail')or'').split('\nĐọc lại hội thoại:', 1)[0].casefold()
    if (state.get('status')!='terminal_no_image' or state.get('submitted')is not False
        or not isinstance(record,dict) or not record.get('new_thread_id')
        or record.get('new_thread_id')!=state.get('thread_id') or record.get('retry_count',0)!=0
        or state.get('staged_image') or state.get('readonly_collection')
        or not any(s in detail for s in ('công cụ tạo ảnh gặp lỗi','không khởi tạo','nhận nhầm','image generation failed','initialization failed'))
        or any(s in detail for s in ('policy','chính sách','quota','rate limit','giới hạn','captcha'))):return None
    try:
        # A successful read must not restart the generation cooldown. Old
        # read budgets bound inconclusive collection, not a new, independently
        # proved finished negative. The reservation below still verifies exact
        # source absence and spends the one permitted recovery-chat retry.
        updated=datetime.fromisoformat(str(record.get('reserved_at')or state['updated_at']).replace('Z','+00:00'))
        if updated.tzinfo is None:return None
        age=(datetime.now(timezone.utc)-updated).total_seconds()
        generation_wait=max(0,int(15*60-age))if age>=0 else None
        if generation_wait is None:return None
        # Collection failures have their own durable backoff. They must not
        # bypass the delay simply because generation was reserved hours ago.
        if state.get('retry_probe_failed_at'):
            failed=datetime.fromisoformat(str(state['retry_probe_failed_at']).replace('Z','+00:00'))
            if failed.tzinfo is None:return None
            failures=max(1,int(state.get('retry_probe_failures')or 1))
            probe_age=(datetime.now(timezone.utc)-failed).total_seconds()
            if probe_age<0:return None
            generation_wait=max(generation_wait,max(0,int(min(1800,60*2**min(failures-1,5))-probe_age)))
        return generation_wait
    except(KeyError,ValueError,TypeError):return None


def thumbnail_prompt_repair_remaining_seconds(state:dict[str,object])->int|None:
    if _recovery_generation_exhausted(state): return None
    detail=str(state.get('detail')or'').split('\nĐọc lại hội thoại:',1)[0].casefold()
    if (state.get('status')!='terminal_no_image' or state.get('submitted')is not False
        or state.get('generation_wire_version')==2 or state.get('wire_repair')
        or (_readonly_probe_count(state)<THUMBNAIL_AI_MAX_READONLY_PROBES
            and int((state.get('recovery_chat')or{}).get('retry_count')or 0)<1)
        or state.get('staged_image') or state.get('readonly_collection') or not state.get('thread_id')
        or not any(s in detail for s in ('nhận nhầm','chỉnh sửa ảnh có sẵn','thiếu ảnh nguồn','edit an existing image'))
        or any(s in detail for s in ('policy','chính sách','quota','rate limit','giới hạn','captcha'))):return None
    try:
        failed=datetime.fromisoformat(str(state.get('retry_probe_failed_at')or state['updated_at']).replace('Z','+00:00'))
        if failed.tzinfo is None:return None
        age=(datetime.now(timezone.utc)-failed).total_seconds()
        if age<0:return None
        failures=max(0,int(state.get('retry_probe_failures')or 0))
        wait=min(1800,60*2**min(max(0,failures-1),5))if failures else 60
        return max(0,int(wait-age))
    except(KeyError,TypeError,ValueError,OverflowError):return None


def thumbnail_canvas_repair_remaining_seconds(state: dict[str, object]) -> int | None:
    """One material-bound repair for an explicitly missing image source."""
    # A missing-source correction is a separate strategy, not another blind
    # send of the exhausted recovery request. Its own one-use record survives.
    if int((state.get('corrective_probe_failures') or {}).get('repair_image_source', 0)) >= 3:
        return None
    detail = str(state.get('detail') or '').split('\nĐọc lại hội thoại:', 1)[0].casefold()
    if (state.get('status') != 'terminal_no_image' or state.get('submitted') is not False
            or state.get('generation_wire_version', 1) not in {1, 2}
            or (_automatic_retry_count(state) < 1 and int((state.get('recovery_chat') or {}).get('count', 0)) < 1)
            or (state.get('generation_wire_version', 1) == 1 and not any(s in detail
                for s in ('thiếu ảnh nguồn', 'yêu cầu ảnh nguồn', 'missing source image', 'no usable image target')))
            or state.get('canvas_repair') or not state.get('thread_id')
            or state.get('staged_image') or state.get('readonly_collection')
            or 'chatgpt_terminal_response_no_image:' not in detail
            or not any(s in detail for s in ('chỉnh sửa ảnh có sẵn', 'thiếu ảnh nguồn', 'yêu cầu ảnh nguồn', 'edit an existing image', 'no usable image target'))
            or any(s in detail for s in ('policy', 'chính sách', 'quota', 'rate limit', 'giới hạn', 'captcha'))):
        return None
    try:
        failed = datetime.fromisoformat(str(state.get('retry_probe_failed_at') or state['updated_at']).replace('Z', '+00:00'))
        if failed.tzinfo is None:
            return None
        age = (datetime.now(timezone.utc) - failed).total_seconds()
        if age < 0:
            return None
        failures = max(0, int(state.get('retry_probe_failures') or 0))
        wait = min(1800, 60 * 2 ** min(max(0, failures - 1), 5)) if failures else 60
        return max(0, int(wait - age))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def thumbnail_canvas_transport_remaining_seconds(state: dict[str, object]) -> int | None:
    if int((state.get('corrective_probe_failures') or {}).get('repair_canvas_transport', 0)) >= 3:
        return None
    detail = str(state.get('detail') or '').split('\nĐọc lại hội thoại:', 1)[0].casefold()
    if (state.get('status') != 'terminal_no_image' or state.get('submitted') is not False
            or state.get('generation_wire_version') != 3 or not state.get('canvas_repair')
            or state.get('canvas_transport_repair') or state.get('staged_image') or state.get('readonly_collection')
            or 'chatgpt_terminal_response_no_image:' not in detail or '.txt' not in detail
            or 'png' not in detail
            or any(s in detail for s in ('policy', 'chính sách', 'quota', 'rate limit', 'giới hạn', 'captcha'))):
        return None
    try:
        at = datetime.fromisoformat(str(state.get('retry_probe_failed_at') or state['updated_at']).replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - at).total_seconds()
        if at.tzinfo is None or age < 0:
            return None
        failures = max(0, int(state.get('retry_probe_failures') or 0))
        wait = min(1800, 60 * 2 ** min(max(0, failures - 1), 5)) if failures else 60
        return max(0, int(wait - age))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _automatic_retry_count(state: dict[str, object]) -> int:
    try:
        count = int(state.get("automatic_retry_count") or 0)
        return count if count >= 0 else THUMBNAIL_AI_MAX_AUTOMATIC_RETRIES
    except (TypeError, ValueError, OverflowError):
        return THUMBNAIL_AI_MAX_AUTOMATIC_RETRIES


def _readonly_probe_count(state: dict[str, object]) -> int:
    try:
        count = int(state.get("readonly_probe_count") or 0)
        return count if count >= 0 else THUMBNAIL_AI_MAX_READONLY_PROBES
    except (TypeError, ValueError, OverflowError):
        return THUMBNAIL_AI_MAX_READONLY_PROBES


def _recovery_probe_count(state: dict[str, object]) -> int:
    try:
        count = int(state.get("recovery_probe_count") or 0)
        return count if count >= 0 else THUMBNAIL_AI_MAX_RECOVERY_PROBES
    except (TypeError, ValueError, OverflowError):
        return THUMBNAIL_AI_MAX_RECOVERY_PROBES


def record_thumbnail_ai_loading_failure(workspace: Workspace, package_path: Path,
                                       claim: ThumbnailAIClaim, detail: str) -> bool:
    """Back off an inconclusive read before reserving a new image turn.

    A loaded page with an unconfirmed image receipt needs the same durable
    delay as a failed page load. Neither result proves permission to resend.
    """
    state = read_thumbnail_ai_state(workspace)
    if (
        state.get("status") != "terminal_no_image"
        or state.get("submitted") is not False
        or state.get("package_sha256") != claim.package_sha256
        or state.get("prompt_sha256") != claim.prompt_sha256
        or _sha256_bytes(package_path.read_bytes()) != claim.package_sha256
    ):
        return False
    _write_state(workspace, package_path=package_path,
                 package_sha256=claim.package_sha256, prompt_sha256=claim.prompt_sha256,
                 status="terminal_no_image", submitted=False,
                 readonly_probe_count=_readonly_probe_count(state) + 1,
                 detail=str(state.get("detail") or "").split("\nĐọc lại hội thoại:", 1)[0]
                 + "\nĐọc lại hội thoại: " + detail)
    if claim.action in {'recover_new_chat','retry_recovery_chat','repair_prompt_transport','repair_image_source','repair_canvas_transport'}:
        saved=read_thumbnail_ai_state(workspace)
        saved['retry_probe_failures']=min(32,int(state.get('retry_probe_failures')or 0)+1)
        saved['retry_probe_failed_at']=_now()
        if claim.action in {'repair_image_source','repair_canvas_transport'}:
            probes = dict(state.get('corrective_probe_failures') or {})
            probes[claim.action] = min(3, int(probes.get(claim.action, 0)) + 1)
            saved['corrective_probe_failures'] = probes
        atomic_write_json(_state_path(workspace),saved)
    return True


def _write_state(
    workspace: Workspace,
    *,
    package_path: Path,
    package_sha256: str,
    prompt_sha256: str,
    status: str,
    detail: str = "",
    thumbnail_path: Path | None = None,
    response_id: str | None = None,
    thread_id: str | None = None,
    source: str = "chatgpt-web",
    submitted: bool | None = None,
    automatic_retry_count: int | None = None,
    recovery_from_thread_id: str | None = None,
    readonly_probe_count: int | None = None,
    recovery_probe_count: int | None = None,
    recovery_receipt_created_at: float | None = None,
    readonly_collection: bool | None = None,
) -> dict[str, object]:
    previous = read_thumbnail_ai_state(workspace)
    created_at = str(previous.get("created_at") or "") or _now()
    same_request = (
        previous.get("package_sha256") == package_sha256
        and previous.get("prompt_sha256") == prompt_sha256
    )
    value: dict[str, object] = {
        "version": THUMBNAIL_AI_STATE_VERSION,
        "workspace_id": workspace.workspace_id,
        "package_path": str(package_path.resolve(strict=False)),
        "package_sha256": package_sha256,
        "prompt_sha256": prompt_sha256,
        "status": status,
        "detail": str(detail),
        "source": source,
        "submitted": (
            bool(previous.get("submitted")) if submitted is None else bool(submitted)
        ),
        "response_id": str(
            response_id
            if response_id is not None
            else (previous.get("response_id") if same_request else "")
            or ""
        ),
        "thread_id": str(
            thread_id
            if thread_id is not None
            else (previous.get("thread_id") if same_request else "")
            or ""
        ),
        "automatic_retry_count": (
            max(0, int(automatic_retry_count))
            if automatic_retry_count is not None
            else (_automatic_retry_count(previous) if same_request else 0)
        ),
        "readonly_probe_count": (
            max(0, int(readonly_probe_count))
            if readonly_probe_count is not None
            else (_readonly_probe_count(previous) if same_request else 0)
        ),
        "delivery_retry_count": int(previous.get("delivery_retry_count") or 0) if same_request else 0,
        "recovery_probe_count": (max(0, int(recovery_probe_count)) if recovery_probe_count is not None
                                 else (_recovery_probe_count(previous) if same_request else 0)),
        "recovery_receipt_created_at": (recovery_receipt_created_at if recovery_receipt_created_at is not None
                                        else (previous.get("recovery_receipt_created_at", 0) if same_request else 0)),
        "staged_image": previous.get("staged_image") if same_request else None,
        "readonly_collection": (bool(readonly_collection) if readonly_collection is not None
                                else (bool(previous.get("readonly_collection")) if same_request else False)),
        "recovery_from_thread_id": str(
            recovery_from_thread_id
            if recovery_from_thread_id is not None
            else (previous.get("recovery_from_thread_id") if same_request else "")
            or ""
        ),
        "thumbnail_path": (
            str(thumbnail_path.resolve(strict=False)) if thumbnail_path is not None else ""
        ),
        "created_at": created_at,
        "updated_at": _now(),
        "recovery_chat": previous.get("recovery_chat") if same_request else None,
        "corrective_probe_failures": previous.get('corrective_probe_failures', {}) if same_request else {},
        "source_read_version": int(previous.get('source_read_version')or 0) if same_request else 0,
        "retry_probe_failures": int(previous.get('retry_probe_failures')or 0) if same_request else 0,
        "retry_probe_failed_at": str(previous.get('retry_probe_failed_at')or '') if same_request else '',
        "generation_wire_version": int(previous.get('generation_wire_version')or 1) if same_request else 1,
        "wire_repair": previous.get('wire_repair') if same_request else None,
        "canvas_repair": previous.get('canvas_repair') if same_request else None,
        "canvas_transport_repair": previous.get('canvas_transport_repair') if same_request else None,
        "isolated_chat": previous.get('isolated_chat') if same_request else None,
    }
    from .conversation_links import retain_thumbnail_link
    from .storage import app_data_dir
    retain_thumbnail_link(workspace,value,app_data_dir()/'chat-bridge/bridge-state.json')
    atomic_write_json(_state_path(workspace), value)
    return value


def extract_complete_thumbnail_prompt(package_path: Path) -> str:
    block = parse_thumbnail_prompt(package_path)
    lines = block.splitlines()
    marker_index = -1
    for index, line in enumerate(lines):
        normalized = unicodedata.normalize("NFKD", line.strip()).casefold()
        normalized = "".join(
            char for char in normalized if not unicodedata.combining(char)
        ).replace("đ", "d")
        normalized = re.sub(r"[^a-z0-9]+", "", normalized)
        if normalized in {
            "promptthumbnailhoanchinh",
            "completepromptthumbnail",
            "completethumbnailprompt",
        }:
            marker_index = index
            break
    prompt = "\n".join(lines[marker_index + 1 :]).strip() if marker_index >= 0 else block.strip()
    if not prompt:
        raise ThumbnailAIError(
            "Phần PROMPT THUMBNAIL HOÀN CHỈNH trong file Tổng hợp đang trống."
        )
    from .content_quality import thumbnail_prompt_problem
    if problem:=thumbnail_prompt_problem(prompt):
        raise ThumbnailAIError('CONTENT_PACKAGE_INCONSISTENT: '+problem+'; cần phục hồi gói nội dung trước tạo ảnh.')
    from .thumbnail_contract import prepare_prompt
    return prepare_prompt(package_path, prompt)


def _claim_thumbnail_ai_generation_base(
    workspace: Workspace,
    package_path: Path,
) -> ThumbnailAIClaim:
    package = package_path.expanduser().resolve(strict=True)
    package_bytes = package.read_bytes()
    package_sha256 = _sha256_bytes(package_bytes)
    prompt = extract_complete_thumbnail_prompt(package)
    prompt_sha256 = _sha256_bytes(prompt.encode("utf-8"))
    state = read_thumbnail_ai_state(workspace)
    same_request = (
        state.get("version") == THUMBNAIL_AI_STATE_VERSION
        and state.get("package_path") == str(package)
        and state.get("prompt_sha256") == prompt_sha256
    )
    content_checkpoint = package.with_name(package.stem + "_AI_CHECKPOINT.json")
    if same_request and content_checkpoint.is_file():
        try:
            video_thread = str(json.loads(content_checkpoint.read_text(encoding="utf-8-sig")).get("thread_id") or "")
            related_threads = {str(state.get("thread_id") or ""), str(state.get("recovery_from_thread_id") or "")}
            related_threads.add(str((state.get('recovery_chat')or{}).get('origin_thread_id')or''))
            related_threads.add(str((state.get('isolated_chat')or{}).get('content_thread_id')or''))
            if video_thread and video_thread not in related_threads:
                same_request = False
        except (OSError, ValueError, TypeError):
            raise ThumbnailAIError("Không đọc được checkpoint hội thoại của đúng video; giữ lượt thumbnail.")
    if same_request and state.get("package_sha256") != package_sha256:
        # Rebind only the file hash; metadata repairs do not replenish image
        # attempts or restart the stale-turn clock.
        state["package_sha256"] = package_sha256
        atomic_write_json(_state_path(workspace), state)
    if same_request:
        raw_path = str(state.get("thumbnail_path") or "").strip()
        existing = Path(raw_path).expanduser().resolve(strict=False) if raw_path else None
        if state.get("status") == "complete" and existing is not None and existing.is_file():
            return ThumbnailAIClaim(
                "reuse",
                prompt,
                prompt_sha256,
                package_sha256,
                existing_path=existing,
                detail="Thumbnail ChatGPT của đúng file Tổng hợp này đã hoàn tất.",
            )
        status = str(state.get("status") or "").casefold()
        if state.get("staged_image") and status != "complete":
            cached_claim = ThumbnailAIClaim("install_cached", prompt, prompt_sha256, package_sha256)
            cached = load_thumbnail_candidate(workspace, cached_claim, str(state.get("thread_id") or ""))
            if cached is None:
                raise ThumbnailAIError("Ảnh đã nhận không còn đọc được; không tạo lại ảnh tự động.")
            return ThumbnailAIClaim("install_cached", prompt, prompt_sha256, package_sha256,
                                    existing_path=cached[0],detail="Đã kiểm tra SHA-256 và bytes ảnh của đúng prompt/chat.")
        fresh_wait = thumbnail_recovery_chat_remaining_seconds(state)
        if fresh_wait is not None:
            return ThumbnailAIClaim('recover_new_chat' if fresh_wait == 0 else 'blocked',
                prompt,prompt_sha256,package_sha256,
                detail='Đọc lại đúng lượt cũ trước khi chuyển nguyên prompt thumbnail sang chat mới.'
                if fresh_wait == 0 else f'Chờ {fresh_wait} giây trước khi kiểm tra lượt cũ để mở chat thumbnail mới.')
        transport_fix = thumbnail_canvas_transport_remaining_seconds(state)
        if transport_fix is not None:
            return ThumbnailAIClaim('repair_canvas_transport' if transport_fix == 0 else 'blocked', prompt, prompt_sha256, package_sha256,
                detail='Đối soát lượt PNG bị đổi đuôi .txt trước một lần sửa truyền tệp.' if transport_fix == 0 else f'Chờ {transport_fix} giây để đối soát ảnh sai định dạng.')
        canvas_wait = thumbnail_canvas_repair_remaining_seconds(state)
        if canvas_wait is not None:
            return ThumbnailAIClaim('repair_image_source' if canvas_wait == 0 else 'blocked', prompt, prompt_sha256, package_sha256,
                detail='Đối soát ảnh cũ trước một lượt phục hồi bằng khung ảnh trống.' if canvas_wait == 0
                else f'Chờ {canvas_wait} giây trước khi kiểm tra nguồn ảnh để phục hồi.')
        transport_wait=thumbnail_prompt_repair_remaining_seconds(state)
        if transport_wait is not None:
            return ThumbnailAIClaim('repair_prompt_transport'if transport_wait==0 else 'blocked',prompt,prompt_sha256,package_sha256,
                detail=('Đổi đúng một lần cách diễn đạt yêu cầu tạo ảnh; giữ nguyên thiết kế, chữ Nhật và chat video.'
                        if transport_wait==0 else f'Chờ {transport_wait} giây trước khi kiểm tra lại nguồn thumbnail.'))
        if _source_probe_upgrade_available(state):
            return ThumbnailAIClaim('recover',prompt,prompt_sha256,package_sha256,
                read_only_collection=True,source_only=True,
                detail='Đọc nguồn đúng hội thoại một lần bằng cơ chế mới; không mở DOM hoặc gửi prompt.')
        if state.get("readonly_collection") is True and str(state.get("thread_id") or "").strip():
            return ThumbnailAIClaim(
                "recover", prompt, prompt_sha256, package_sha256,
                detail="Chỉ thu hồi thumbnail đã có trong đúng hội thoại; không gửi thêm yêu cầu tạo ảnh.",
                read_only_collection=True,
            )
        if status in {"error", "uncertain"} and thumbnail_ai_pending_receipt_error(state.get("detail")):
            # Older UI builds lost this lock when /responses rejected a new
            # request before its own submit callback. Retain the previous
            # bridge receipt and its clock without claiming a new acceptance.
            if state.get("submitted") is not True or status != "uncertain":
                state["submitted"] = True
                state["status"] = status = "uncertain"
                atomic_write_json(_state_path(workspace), state)
        followup = state.get("operator_followup")
        if (
            status == "operator_retry_authorized"
            and isinstance(followup, dict)
            and followup.get("authorization") == "retry-once-in-same-conversation"
            and str(followup.get("thread_id") or "") == str(state.get("thread_id") or "")
            and str(state.get("thread_id") or "").strip()
            and Path(str(followup.get("retired_receipt_path") or "")).is_file()
        ):
            # An operator can explicitly authorize a new image attempt after
            # inspecting a stalled accepted turn. Its receipt remains in the
            # repair journal. Consume this one-use authorization before the
            # caller sends; do not describe the old turn as never submitted.
            _write_state(
                workspace,
                package_path=package,
                package_sha256=package_sha256,
                prompt_sha256=prompt_sha256,
                status="running",
                detail="Đã nhận quyền thử thêm đúng một lượt thumbnail trong hội thoại cũ; biên nhận lượt trước được giữ trong hồ sơ sửa lỗi.",
                source="operator-authorized-one-followup",
                submitted=False,
                automatic_retry_count=THUMBNAIL_AI_MAX_AUTOMATIC_RETRIES,
                readonly_probe_count=0,
                recovery_probe_count=0,
                recovery_receipt_created_at=0,
            )
            return ThumbnailAIClaim(
                "start_checked", prompt, prompt_sha256, package_sha256,
                detail="Thử thumbnail thêm một lần theo yêu cầu sửa lỗi, giữ nguyên hội thoại.",
            )
        if status == "operator_retry_authorized":
            raise ThumbnailAIError("Không xác minh được quyền thử thêm thumbnail và biên nhận hội thoại cũ; giữ checkpoint.")
        if status == "delivery_retry_pending" and thumbnail_ai_automatic_recovery_remaining_seconds(state) == 0:
            proof = state.get("unsubmitted_image_proof") or {}
            expected = hashlib.sha256(" ".join(unicodedata.normalize("NFC", thumbnail_generation_request(prompt)).split()).encode()).hexdigest()
            if (proof.get("safe_to_resend") is not True or proof.get("thread_id") != state.get("thread_id")
                    or proof.get("prompt_sha256") != expected):
                raise ThumbnailAIError("Không có bằng chứng Bridge cho đúng lượt ảnh chưa gửi; giữ checkpoint.")
            _write_state(workspace, package_path=package, package_sha256=package_sha256,
                         prompt_sha256=prompt_sha256, status="running", submitted=False,
                         recovery_probe_count=0, recovery_receipt_created_at=0,
                         detail="Bridge đã chứng minh lượt ảnh chưa gửi; thử phục hồi truyền đúng một lần.")
            saved = read_thumbnail_ai_state(workspace)
            saved["delivery_retry_count"] = 1
            saved["unsubmitted_image_proof"] = proof
            atomic_write_json(_state_path(workspace), saved)
            return ThumbnailAIClaim("start", prompt, prompt_sha256, package_sha256)
        if thumbnail_recovery_retry_remaining_seconds(state)==0:
            return ThumbnailAIClaim('retry_recovery_chat',prompt,prompt_sha256,package_sha256,
                detail='Thử lại đúng một lần trong chat phục hồi sau lỗi tạo ảnh tạm thời đã kết thúc.')
        if thumbnail_recovery_chat_remaining_seconds(state) == 0:
            return ThumbnailAIClaim('recover_new_chat',prompt,prompt_sha256,package_sha256,
                detail='Chỉ tạo một chat phục hồi sau khi nguồn hội thoại chứng minh lượt cũ đã kết thúc không có ảnh.')
        if thumbnail_ai_terminal_retry_remaining_seconds(state) == 0:
            return ThumbnailAIClaim(
                "retry_terminal", prompt, prompt_sha256, package_sha256,
                detail="Lượt cũ xác nhận không tạo ảnh; cần kiểm tra bridge trước một lượt thử riêng.",
            )
        if status == "error" and state.get("submitted") is not True:
            _write_state(
                workspace,
                package_path=package,
                package_sha256=package_sha256,
                prompt_sha256=prompt_sha256,
                status="running",
                detail="Lượt trước lỗi trước khi gửi; đã cấp lại lượt an toàn.",
                submitted=False,
            )
            return ThumbnailAIClaim(
                "start",
                prompt,
                prompt_sha256,
                package_sha256,
                detail="Lượt trước chưa gửi nên có thể thử lại an toàn.",
            )
        remaining = thumbnail_ai_automatic_recovery_remaining_seconds(state)
        if remaining == 0 and str(state.get("thread_id") or "").strip():
            read_only = ((_automatic_retry_count(state) >= THUMBNAIL_AI_MAX_AUTOMATIC_RETRIES
                          and not state.get("recovery_receipt_created_at"))
                         or _recovery_probe_count(state) >= THUMBNAIL_AI_MAX_RECOVERY_PROBES)
            _write_state(
                workspace,
                package_path=package,
                package_sha256=package_sha256,
                prompt_sha256=prompt_sha256,
                status="recovering",
                detail=(
                    "Đang tự kiểm tra kết quả lượt ChatGPT đã gửi; "
                    + str(state.get("detail") or "checkpoint được giữ.")
                ),
                submitted=True,
                # Only reserve_thumbnail_readonly_probe, after queue
                # acquisition, spends this counter. Queueing is not a read.
            )
            return ThumbnailAIClaim(
                "confirm_existing" if state.get("staged_image") else "recover",
                prompt,
                prompt_sha256,
                package_sha256,
                detail="Đang kiểm tra đúng hội thoại ChatGPT trước khi thử lại.",
                read_only_collection=read_only,
            )
        if status in {
            "error",
            "running",
            "submitted",
            "recovering",
            "image_received",
            "confirming",
            "uncertain",
            "overridden",
            "terminal_no_image",
            "confirmation_pending",
            "delivery_retry_pending",
            "delivery_failed",
        }:
            return ThumbnailAIClaim(
                "blocked",
                prompt,
                prompt_sha256,
                package_sha256,
                detail=str(state.get("detail") or "Lượt tạo thumbnail trước chưa thể xác minh."),
            )
    _write_state(
        workspace,
        package_path=package,
        package_sha256=package_sha256,
        prompt_sha256=prompt_sha256,
        status="running",
        detail="Đã khóa đúng một lượt tạo thumbnail ChatGPT Web.",
        submitted=False,
        readonly_collection=False,
    )
    return ThumbnailAIClaim("start", prompt, prompt_sha256, package_sha256)


def thumbnail_request_for_claim(claim:ThumbnailAIClaim)->str:
    return claim.generation_request or thumbnail_generation_request(claim.prompt)


def thumbnail_text_to_image_request(prompt:str)->str:
    design=re.sub(r'\bPhotoshop\b','photorealistic',prompt,flags=re.I)
    design=re.sub(r'\b(?:composite|compositing)\b','unified scene',design,flags=re.I)
    design=re.sub(r'assembling (?:several )?(?:genuine|real) photographs','generating every element from text',design,flags=re.I)
    return ('[DOCURANDOM_STAGE=thumbnail-text-to-image-v2]\n'
        'Generate exactly ONE new 1280x720 PNG image directly from this text description. '
        'Create every visual element as part of this new image. Preserve all Japanese lettering, colors and layout in the design below. '
        'Return the generated image as the result of this turn.\n\nDESIGN:\n'+design)


def thumbnail_canvas_bytes() -> bytes:
    import io
    from PIL import Image
    output = io.BytesIO()
    Image.new('RGB', (1280, 720), (236, 236, 236)).save(output, format='PNG', compress_level=9)
    return output.getvalue()


def thumbnail_canvas_request(prompt: str) -> str:
    return ('[DOCURANDOM_STAGE=thumbnail-canvas-repair-v3]\n'
        'Edit the attached empty 1280x720 canvas into exactly ONE finished YouTube thumbnail. '
        'Replace the entire blank canvas with the complete scene described below. '
        'The attached canvas is the source image for the image editing tool. '
        'Keep every specified Japanese text string, color and layout. '
        'Return the finished image, not the original blank canvas.\n\nDESIGN:\n' + prompt)


def thumbnail_canvas_transport_request(prompt: str) -> str:
    return thumbnail_canvas_request(prompt).replace('[DOCURANDOM_STAGE=thumbnail-canvas-repair-v3]',
        '[DOCURANDOM_STAGE=thumbnail-canvas-transport-v4]', 1)


def thumbnail_canvas_previous_request(workspace: Workspace, claim: ThumbnailAIClaim) -> str:
    state = read_thumbnail_ai_state(workspace)
    return thumbnail_text_to_image_request(claim.prompt) if state.get('generation_wire_version') == 2 else thumbnail_generation_request(claim.prompt)


def prepare_isolated_thumbnail_chat(workspace: Workspace, package_path: Path,
                                    content_thread_id: str = '') -> str:
    """Reserve a thumbnail-only thread once per video, before any browser work.

    The user's separate-chat policy supersedes unfinished legacy thumbnail
    requests. Their receipts are archived intact, never reclassified as unsent
    or failed. Completed/cached image assets are retained instead of replaced.
    """
    package=package_path.expanduser().resolve(strict=True)
    package_sha=_sha256_bytes(package.read_bytes())
    prompt_sha=_sha256_bytes(extract_complete_thumbnail_prompt(package).encode('utf-8'))
    state=read_thumbnail_ai_state(workspace)
    same=state.get('package_path')==str(package) and state.get('prompt_sha256')==prompt_sha
    record=state.get('isolated_chat')or{}
    if same and (state.get('staged_image') or
            (state.get('status')=='complete' and state.get('thumbnail_path') and Path(str(state['thumbnail_path'])).is_file())):
        return str(state.get('thread_id')or'')
    if (same and isinstance(record,dict) and record.get('policy_version')==1
            and record.get('prompt_sha256')==prompt_sha and state.get('thread_id')
            and state.get('thread_id')!=content_thread_id):
        return str(state['thread_id'])
    from .storage import app_data_dir
    from .conversation_links import retain_thumbnail_link
    bridge=app_data_dir()/'chat-bridge/bridge-state.json'
    prior_thread=str(state.get('thread_id')or'')
    journal=''
    if state:
        retain_thumbnail_link(workspace,state,bridge)
        archive=workspace.state_dir/'repair-journal'/('thumbnail-before-isolation-'+str(state.get('prompt_sha256')or prompt_sha)[:16]+'-'+uuid.uuid4().hex+'.json')
        atomic_write_json(archive,{'reason':'user-requested-separate-thumbnail-chat',
            'previous_state':state,'previous_submission_still_uncertain':state.get('submitted')is True})
        journal=str(archive)
    thread='thread_docurandom_'+uuid.uuid4().hex
    _write_state(workspace,package_path=package,package_sha256=package_sha,prompt_sha256=prompt_sha,
        status='reserved',submitted=False,thread_id=thread,response_id='',
        automatic_retry_count=0,readonly_probe_count=0,recovery_probe_count=0,
        recovery_receipt_created_at=0,readonly_collection=False,
        detail='Đã dành chat riêng cho thumbnail của đúng file Tổng hợp; chưa gửi prompt.')
    fresh=read_thumbnail_ai_state(workspace)
    fresh.update(generation_wire_version=1,recovery_chat=None,wire_repair=None,
        canvas_repair=None,canvas_transport_repair=None,retry_probe_failures=0,
        retry_probe_failed_at='',source_read_version=0,delivery_retry_count=0,
        recovery_from_thread_id='',isolated_chat={'policy_version':1,'reserved_at':_now(),
            'origin_thread_id':thread,'content_thread_id':content_thread_id,
            'prompt_sha256':prompt_sha,'previous_thread_id':prior_thread,'previous_receipt_path':journal})
    fresh.pop('conversation_url',None)
    retain_thumbnail_link(workspace,fresh,bridge)
    atomic_write_json(_state_path(workspace),fresh)
    return thread


def claim_thumbnail_ai_generation(workspace:Workspace,package_path:Path,*,
                                  dedicated_chat:bool=False,content_thread_id:str='')->ThumbnailAIClaim:
    if dedicated_chat:
        prepare_isolated_thumbnail_chat(workspace,package_path,content_thread_id)
    claim=_claim_thumbnail_ai_generation_base(workspace,package_path)
    state=read_thumbnail_ai_state(workspace)
    staged = state.get('staged_image') or {}
    rejected = workspace.state_dir / 'thumbnail-rejections' / (str(staged.get('sha256', '')) + '.json')
    if claim.action == 'install_cached' and rejected.is_file():
        receipt = json.loads(rejected.read_text(encoding='utf-8'))
        if receipt.get('prompt_sha256') == claim.prompt_sha256:
            used = workspace.state_dir / 'repair-journal' / ('thumbnail-quality-' + claim.prompt_sha256 + '.json')
            return replace(claim, action='blocked' if used.exists() else 'retry_rejected',
                detail='Ảnh đúng lượt không đạt kiểm tra nội dung; chỉ phục hồi một lần sau khi xác minh lượt đã kết thúc.')
    if claim.action == 'repair_canvas_transport' or (state.get('generation_wire_version') == 4
        and state.get('prompt_sha256') == claim.prompt_sha256):
        return replace(claim, generation_request=thumbnail_canvas_transport_request(claim.prompt))
    if claim.action == 'repair_image_source' or (state.get('generation_wire_version') == 3
        and state.get('prompt_sha256') == claim.prompt_sha256):
        return replace(claim, generation_request=thumbnail_canvas_request(claim.prompt))
    if claim.action=='repair_prompt_transport' or (state.get('generation_wire_version')==2
        and state.get('prompt_sha256')==claim.prompt_sha256):
        return replace(claim,generation_request=thumbnail_text_to_image_request(claim.prompt))
    return claim


def reserve_rejected_thumbnail(workspace, package_path, claim, old_thread, new_thread, proof):
    expected = hashlib.sha256(' '.join(unicodedata.normalize('NFC', thumbnail_request_for_claim(claim)).split()).encode()).hexdigest()
    state = read_thumbnail_ai_state(workspace)
    record = workspace.state_dir / 'repair-journal' / ('thumbnail-quality-' + claim.prompt_sha256 + '.json')
    if (record.exists() or not isinstance(proof, dict) or proof.get('complete') is not True
        or proof.get('prompt_sha256') != expected or not proof.get('matched_user_ids')
        or not proof.get('image_message_ids') or state.get('thread_id') != old_thread
        or state.get('prompt_sha256') != claim.prompt_sha256
        or _sha256_bytes(package_path.read_bytes()) != claim.package_sha256):
        return False
    from .conversation_links import saved_conversation_url
    from .storage import app_data_dir
    url = saved_conversation_url(app_data_dir() / 'chat-bridge/bridge-state.json', old_thread)
    if not url or url.rsplit('/', 1)[-1] != proof.get('conversation_id'):
        return False
    atomic_write_json(record, {'previous_state': state, 'previous_url': url, 'new_thread': new_thread,
                              'source_proof': proof, 'reserved_at': _now(), 'count': 1})
    _write_state(workspace, package_path=package_path, package_sha256=claim.package_sha256,
        prompt_sha256=claim.prompt_sha256, status='running', thread_id=new_thread, submitted=False,
        automatic_retry_count=_automatic_retry_count(state)+1, response_id='', recovery_from_thread_id=old_thread,
        detail='Đã xác minh ảnh cũ không đạt; tạo đúng một ảnh thay thế trong chat thumbnail mới.')
    return True


def reserve_thumbnail_prompt_repair(workspace:Workspace,package_path:Path,claim:ThumbnailAIClaim,
                                   thread_id:str,proof:object)->bool:
    state=read_thumbnail_ai_state(workspace)
    old_request=thumbnail_generation_request(claim.prompt)
    digest=lambda text:hashlib.sha256(' '.join(unicodedata.normalize('NFC',text).split()).encode()).hexdigest()
    if (thumbnail_prompt_repair_remaining_seconds(state)!=0 or state.get('thread_id')!=thread_id
        or state.get('package_sha256')!=claim.package_sha256 or state.get('prompt_sha256')!=claim.prompt_sha256
        or _sha256_bytes(package_path.read_bytes())!=claim.package_sha256
        or not isinstance(proof,dict) or proof.get('complete')is not True
        or proof.get('terminal_no_image')is not True or proof.get('prompt_sha256')!=digest(old_request)
        or proof.get('image_message_ids')!=[] or not proof.get('matched_user_ids')
        or any(s in str(proof.get('terminal_detail')or'').casefold()for s in ('policy','chính sách','quota','rate limit','giới hạn','captcha'))):return False
    from .conversation_links import saved_conversation_url
    from .storage import app_data_dir
    url=saved_conversation_url(app_data_dir()/'chat-bridge/bridge-state.json',thread_id)
    if not url or url.rsplit('/',1)[-1]!=proof.get('conversation_id'):return False
    state.update(generation_wire_version=2,wire_repair={'count':1,'reserved_at':_now(),
        'thread_id':thread_id,'source_proof':proof,'previous_request_sha256':digest(old_request),
        'request_sha256':digest(thumbnail_request_for_claim(claim))})
    atomic_write_json(_state_path(workspace),state)
    _write_state(workspace,package_path=package_path,package_sha256=claim.package_sha256,
        prompt_sha256=claim.prompt_sha256,status='running',submitted=False,thread_id=thread_id,
        detail='Đã xác minh lượt cũ không có ảnh; thử đúng một lần bằng mô tả text-to-image trong cùng chat, giữ nguyên thiết kế và bộ đếm.')
    return True


def reserve_thumbnail_canvas_repair(workspace: Workspace, package_path: Path, claim: ThumbnailAIClaim,
                                    thread_id: str, proof: object) -> bool:
    state = read_thumbnail_ai_state(workspace)
    digest = lambda text: hashlib.sha256(' '.join(unicodedata.normalize('NFC', text).split()).encode()).hexdigest()
    old_request = thumbnail_canvas_previous_request(workspace, claim)
    if (thumbnail_canvas_repair_remaining_seconds(state) != 0 or state.get('thread_id') != thread_id
            or state.get('package_sha256') != claim.package_sha256 or state.get('prompt_sha256') != claim.prompt_sha256
            or _sha256_bytes(package_path.read_bytes()) != claim.package_sha256
            or not isinstance(proof, dict) or proof.get('complete') is not True
            or proof.get('terminal_no_image') is not True or proof.get('prompt_sha256') != digest(old_request)
            or proof.get('image_message_ids') != [] or not proof.get('matched_user_ids')
            or any(s in str(proof.get('terminal_detail') or '').casefold()
                for s in ('policy', 'chính sách', 'quota', 'rate limit', 'giới hạn', 'captcha'))):
        return False
    from .conversation_links import saved_conversation_url
    from .storage import app_data_dir
    url = saved_conversation_url(app_data_dir() / 'chat-bridge/bridge-state.json', thread_id)
    if not url or url.rsplit('/', 1)[-1] != proof.get('conversation_id'):
        return False
    canvas = thumbnail_canvas_bytes()
    canvas_path = workspace.state_dir / 'thumbnail-source-canvas-v3.png'
    canvas_path.write_bytes(canvas)
    record = {'count': 1, 'reserved_at': _now(), 'thread_id': thread_id,
        'previous_generation_wire_version': state.get('generation_wire_version', 1),
        'source_proof': proof, 'previous_request_sha256': digest(old_request),
        'request_sha256': digest(thumbnail_canvas_request(claim.prompt)),
        'canvas_path': str(canvas_path), 'canvas_sha256': _sha256_bytes(canvas)}
    atomic_write_json(workspace.state_dir / 'repair-journal' / ('thumbnail-canvas-' + claim.prompt_sha256[:16] + '.json'),
        {'previous_state': state, 'reservation': record, 'conversation_url': url})
    state.update(generation_wire_version=3, canvas_repair=record)
    atomic_write_json(_state_path(workspace), state)
    _write_state(workspace, package_path=package_path, package_sha256=claim.package_sha256,
        prompt_sha256=claim.prompt_sha256, status='running', submitted=False, thread_id=thread_id,
        detail='Đã xác minh lượt cũ không có ảnh; cấp đúng một lượt sửa trên khung trống trong cùng chat.')
    return True


def thumbnail_canvas_attachments(workspace: Workspace, claim: ThumbnailAIClaim):
    if not thumbnail_request_for_claim(claim).startswith(('[DOCURANDOM_STAGE=thumbnail-canvas-repair-v3]', '[DOCURANDOM_STAGE=thumbnail-canvas-transport-v4]')):
        return ()
    from .content_ai import ChatAttachment
    state = read_thumbnail_ai_state(workspace)
    record = state.get('canvas_repair') or {}
    expected_path = workspace.state_dir / 'thumbnail-source-canvas-v3.png'
    if (state.get('prompt_sha256') != claim.prompt_sha256 or state.get('package_sha256') != claim.package_sha256
            or record.get('canvas_path') != str(expected_path) or not expected_path.is_file()):
        raise ThumbnailAIError('Không còn khung ảnh đúng checkpoint; không gửi lượt thumbnail mới.')
    data = expected_path.read_bytes()
    sha = _sha256_bytes(data)
    if sha != record.get('canvas_sha256') or sha != _sha256_bytes(thumbnail_canvas_bytes()):
        raise ThumbnailAIError('Khung ảnh nguồn không còn nguyên vẹn; giữ checkpoint thumbnail.')
    return (ChatAttachment('DocuRandom-Thumbnail-Canvas-' + sha[:12] + '.png', data, 'image/png'),)


def reserve_thumbnail_canvas_transport(workspace: Workspace, package_path: Path, claim: ThumbnailAIClaim,
                                      thread_id: str, proof: object) -> bool:
    state = read_thumbnail_ai_state(workspace)
    digest = lambda text: hashlib.sha256(' '.join(unicodedata.normalize('NFC', text).split()).encode()).hexdigest()
    if (thumbnail_canvas_transport_remaining_seconds(state) != 0 or state.get('thread_id') != thread_id
            or state.get('package_sha256') != claim.package_sha256 or state.get('prompt_sha256') != claim.prompt_sha256
            or _sha256_bytes(package_path.read_bytes()) != claim.package_sha256
            or not isinstance(proof, dict) or proof.get('complete') is not True or proof.get('terminal_no_image') is not True
            or proof.get('prompt_sha256') != digest(thumbnail_canvas_request(claim.prompt))
            or proof.get('image_message_ids') != [] or not proof.get('matched_user_ids')
            or '.txt' not in str(proof.get('terminal_detail') or '').casefold()
            or any(s in str(proof.get('terminal_detail') or '').casefold() for s in ('policy','chính sách','quota','rate limit','giới hạn','captcha'))):
        return False
    from .conversation_links import saved_conversation_url
    from .storage import app_data_dir
    url = saved_conversation_url(app_data_dir()/'chat-bridge/bridge-state.json', thread_id)
    if not url or url.rsplit('/',1)[-1] != proof.get('conversation_id'):
        return False
    thumbnail_canvas_attachments(workspace, claim)
    record = {'count': 1, 'reserved_at': _now(), 'thread_id': thread_id, 'source_proof': proof,
        'request_sha256': digest(thumbnail_canvas_transport_request(claim.prompt))}
    atomic_write_json(workspace.state_dir/'repair-journal'/('thumbnail-canvas-transport-'+claim.prompt_sha256[:16]+'.json'),
        {'previous_state': state, 'reservation': record, 'conversation_url': url})
    state.update(generation_wire_version=4, canvas_transport_repair=record)
    atomic_write_json(_state_path(workspace), state)
    _write_state(workspace, package_path=package_path, package_sha256=claim.package_sha256,
        prompt_sha256=claim.prompt_sha256, thread_id=thread_id, status='running', submitted=False,
        detail='Lượt cũ xác nhận nhận sai đuôi tệp; thử đúng một lần bằng PNG giữ nguyên tên định dạng.')
    return True


def validate_thumbnail_canvas_result(workspace: Workspace, claim: ThumbnailAIClaim, data: bytes) -> None:
    state = read_thumbnail_ai_state(workspace)
    if state.get('prompt_sha256') != claim.prompt_sha256 or not state.get('canvas_repair'):
        return
    import io
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        if image.convert('RGB').getextrema() == ((236, 236), (236, 236), (236, 236)):
            raise ThumbnailAIError('CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: Kết quả vẫn là khung trống, chưa có thumbnail; giữ video và checkpoint.')


def stage_thumbnail_candidate(workspace: Workspace, claim: ThumbnailAIClaim,
                              thread_id: str, response_id: str, data: bytes) -> Path:
    """Keep validated bytes before confirmation, without enabling publication."""
    from .thumbnail_manager import inspect_thumbnail_bytes
    validate_thumbnail_canvas_result(workspace, claim, data)
    inspection = inspect_thumbnail_bytes(data)
    state = read_thumbnail_ai_state(workspace)
    if state.get("prompt_sha256") != claim.prompt_sha256 or state.get("thread_id") != thread_id:
        raise ThumbnailAIError("Checkpoint ảnh đã đổi; không gắn ảnh vào video khác.")
    root = workspace.state_dir / "thumbnail-candidates"
    root.mkdir(parents=True, exist_ok=True)
    target = root / (inspection.sha256 + inspection.extension)
    with tempfile.NamedTemporaryFile(dir=root, prefix="candidate-", suffix=".tmp", delete=False) as output:
        temporary = Path(output.name)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    state["staged_image"] = {"path": str(target), "sha256": inspection.sha256,
        "prompt_sha256": claim.prompt_sha256, "thread_id": thread_id, "response_id": response_id}
    atomic_write_json(_state_path(workspace), state)
    return target


def load_thumbnail_candidate(workspace: Workspace, claim: ThumbnailAIClaim,
                             thread_id: str) -> tuple[Path, bytes, str] | None:
    from .thumbnail_manager import inspect_thumbnail_bytes
    record = read_thumbnail_ai_state(workspace).get("staged_image")
    if not record:
        return None
    if not isinstance(record, dict) or record.get("prompt_sha256") != claim.prompt_sha256 or record.get("thread_id") != thread_id:
        raise ThumbnailAIError("Ảnh chờ xác nhận không thuộc đúng prompt/hội thoại; giữ checkpoint.")
    path = Path(str(record.get("path") or "")).resolve(strict=False)
    if path.parent != (workspace.state_dir / "thumbnail-candidates").resolve(strict=False):
        raise ThumbnailAIError("Đường dẫn ảnh chờ xác nhận nằm ngoài thư mục quản lý.")
    data = path.read_bytes()
    validate_thumbnail_canvas_result(workspace, claim, data)
    if inspect_thumbnail_bytes(data).sha256 != record.get("sha256"):
        raise ThumbnailAIError("Ảnh chờ xác nhận đã thay đổi; không dùng hoặc tạo lại tự động.")
    return path, data, str(record.get("response_id") or "")


def update_thumbnail_ai_state(
    workspace: Workspace,
    package_path: Path,
    claim: ThumbnailAIClaim,
    *,
    status: str,
    detail: str = "",
    thumbnail_path: Path | None = None,
    response_id: str | None = None,
    thread_id: str | None = None,
    source: str = "chatgpt-web",
    submitted: bool | None = None,
    automatic_retry_count: int | None = None,
    recovery_from_thread_id: str | None = None,
) -> None:
    if status in {"error", "uncertain"} and thumbnail_ai_pending_receipt_error(detail):
        status = "uncertain"
        submitted = True
    _write_state(
        workspace,
        package_path=package_path,
        package_sha256=claim.package_sha256,
        prompt_sha256=claim.prompt_sha256,
        status=status,
        detail=detail,
        thumbnail_path=thumbnail_path,
        response_id=response_id,
        thread_id=thread_id,
        source=source,
        submitted=submitted,
        automatic_retry_count=automatic_retry_count,
        recovery_from_thread_id=recovery_from_thread_id,
    )


def reserve_thumbnail_ai_automatic_retry(
    workspace: Workspace,
    package_path: Path,
    claim: ThumbnailAIClaim,
    *,
    previous_thread_id: str,
    new_thread_id: str,
    terminal_no_image: bool = False,
    allow_same_conversation: bool = False,
) -> bool:
    """Durably reserve one isolated retry after exact stale-turn proof."""

    state = read_thumbnail_ai_state(workspace)
    if (
        state.get("package_sha256") != claim.package_sha256
        or state.get("prompt_sha256") != claim.prompt_sha256
        or str(state.get("thread_id") or "").strip() != previous_thread_id.strip()
        or _automatic_retry_count(state) >= THUMBNAIL_AI_MAX_AUTOMATIC_RETRIES
        or not new_thread_id.strip()
        or (new_thread_id.strip() == previous_thread_id.strip() and not allow_same_conversation)
        or state.get("workspace_id") != workspace.workspace_id
        or (terminal_no_image and thumbnail_ai_terminal_retry_remaining_seconds(state) != 0)
    ):
        return False
    try:
        if (
            _sha256_bytes(package_path.read_bytes()) != claim.package_sha256
            or _sha256_bytes(extract_complete_thumbnail_prompt(package_path).encode("utf-8"))
            != claim.prompt_sha256
        ):
            return False
    except (OSError, ThumbnailAIError):
        return False
    _write_state(
        workspace,
        package_path=package_path,
        package_sha256=claim.package_sha256,
        prompt_sha256=claim.prompt_sha256,
        status="running",
        detail=(
            "Lượt cũ đã được xác minh đứng yên; đang thử đúng một lần "
            + ("trong đúng cuộc hội thoại video." if allow_same_conversation else "trong cuộc hội thoại ChatGPT mới.")
        ),
        response_id="",
        thread_id=new_thread_id,
        submitted=False,
        automatic_retry_count=_automatic_retry_count(state) + 1,
        recovery_probe_count=0,
        recovery_receipt_created_at=0,
        readonly_probe_count=0,
        recovery_from_thread_id=previous_thread_id,
    )
    return True


def reserve_thumbnail_recovery_chat(workspace: Workspace,package_path: Path,claim: ThumbnailAIClaim,
                                    previous_thread_id: str,new_thread_id: str,proof: object)->bool:
    state=read_thumbnail_ai_state(workspace)
    expected=hashlib.sha256(' '.join(unicodedata.normalize('NFC',thumbnail_request_for_claim(claim)).split()).encode()).hexdigest()
    if (thumbnail_recovery_chat_remaining_seconds(state)!=0 or state.get('thread_id')!=previous_thread_id
        or state.get('package_sha256')!=claim.package_sha256 or state.get('prompt_sha256')!=claim.prompt_sha256
        or _sha256_bytes(package_path.read_bytes())!=claim.package_sha256
        or not new_thread_id or new_thread_id==previous_thread_id or not isinstance(proof,dict)
        or proof.get('complete')is not True or proof.get('terminal_no_image')is not True
        or proof.get('prompt_sha256')!=expected or proof.get('image_message_ids')!=[]
        or proof.get('latest_request_prompt_sha256',expected)!=expected
        or not proof.get('matched_user_ids')
        or any(s in str(proof.get('terminal_detail')or'').casefold() for s in ('quota','policy','chính sách','rate limit','rate_limited','http 429','giới hạn','hết lượt','captcha','đăng nhập','sign in'))):return False
    from .conversation_links import saved_conversation_url
    from .storage import app_data_dir
    old_url=saved_conversation_url(app_data_dir()/'chat-bridge/bridge-state.json',previous_thread_id)
    if not old_url or old_url.rsplit('/',1)[-1]!=proof.get('conversation_id'):return False
    from .conversation_links import retain_thumbnail_link
    retain_thumbnail_link(workspace,state,app_data_dir()/'chat-bridge/bridge-state.json')
    previous_record=state.get('recovery_chat')or{}
    count=int(previous_record.get('count')or 0)+1
    journal=workspace.state_dir/'repair-journal'/('thumbnail-before-recovery-'+claim.prompt_sha256[:16]+'-'+str(count)+'.json')
    atomic_write_json(journal,{'previous_state':state,'source_proof':proof,'previous_url':old_url})
    record={'count':count,'policy_version':3,'generation_attempts':1,'reserved_at':_now(),'old_thread_id':previous_thread_id,'new_thread_id':new_thread_id,
            'origin_thread_id':previous_record.get('origin_thread_id')or state.get('recovery_from_thread_id')or previous_thread_id,
            'old_url':old_url,'receipt_path':str(journal)}
    state.update(recovery_chat=record,generation_wire_version=1)
    primary=str(state.get('detail')or'').split('\nĐọc lại hội thoại:',1)[0].casefold()
    if count>=2 or any(s in primary for s in ('thiếu ảnh nguồn','yêu cầu ảnh nguồn','missing source image')):
        canvas=thumbnail_canvas_bytes()
        canvas_path=workspace.state_dir/'thumbnail-source-canvas-v3.png'
        canvas_path.write_bytes(canvas)
        request=thumbnail_canvas_request(claim.prompt)
        digest=lambda text:hashlib.sha256(' '.join(unicodedata.normalize('NFC',text).split()).encode()).hexdigest()
        state.update(generation_wire_version=3,canvas_repair={'count':1,'reserved_at':_now(),
            'thread_id':new_thread_id,'source_proof':proof,'previous_request_sha256':expected,
            'request_sha256':digest(request),'canvas_path':str(canvas_path),
            'canvas_sha256':_sha256_bytes(canvas),'fresh_chat':True})
    atomic_write_json(_state_path(workspace),state)
    _write_state(workspace,package_path=package_path,package_sha256=claim.package_sha256,
        prompt_sha256=claim.prompt_sha256,status='running',submitted=False,thread_id=new_thread_id,
        response_id='',recovery_from_thread_id=previous_thread_id,readonly_probe_count=0,
        recovery_probe_count=0,recovery_receipt_created_at=0,
        detail=f'Đã xác minh lượt cũ kết thúc không có ảnh; chuyển nguyên prompt sang chat mới ({count}/{THUMBNAIL_AI_MAX_RECOVERY_CHATS}), giữ link và nguyên liệu cũ.')
    return True


def reserve_thumbnail_recovery_probe(workspace: Workspace, claim: ThumbnailAIClaim, thread_id: str) -> int | None:
    """Spend a read/repair attempt only after the shared queue lease is acquired."""
    state = read_thumbnail_ai_state(workspace)
    if (state.get("package_sha256") != claim.package_sha256 or state.get("prompt_sha256") != claim.prompt_sha256
        or state.get("thread_id") != thread_id or state.get("submitted") is not True
        or _recovery_probe_count(state) >= THUMBNAIL_AI_MAX_RECOVERY_PROBES):
        return None
    count = _recovery_probe_count(state) + 1
    state["recovery_probe_count"] = count
    atomic_write_json(_state_path(workspace), state)
    return count


def reserve_thumbnail_recovery_retry(workspace: Workspace,package_path: Path,claim: ThumbnailAIClaim,
                                    thread_id: str,proof: object)->bool:
    state=read_thumbnail_ai_state(workspace)
    expected=hashlib.sha256(' '.join(unicodedata.normalize('NFC',thumbnail_request_for_claim(claim)).split()).encode()).hexdigest()
    if (thumbnail_recovery_retry_remaining_seconds(state)!=0 or state.get('thread_id')!=thread_id
        or state.get('package_sha256')!=claim.package_sha256 or state.get('prompt_sha256')!=claim.prompt_sha256
        or _sha256_bytes(package_path.read_bytes())!=claim.package_sha256
        or not isinstance(proof,dict) or proof.get('complete')is not True
        or proof.get('terminal_no_image')is not True or proof.get('prompt_sha256')!=expected
        or proof.get('image_message_ids')!=[] or not proof.get('matched_user_ids')
        or any(s in str(proof.get('terminal_detail')or'').casefold()for s in ('policy','chính sách','quota','rate limit','giới hạn','captcha'))):return False
    from .conversation_links import saved_conversation_url
    from .storage import app_data_dir
    url=saved_conversation_url(app_data_dir()/'chat-bridge/bridge-state.json',thread_id)
    if not url or url.rsplit('/',1)[-1]!=proof.get('conversation_id'):return False
    record=dict(state['recovery_chat'],retry_count=1,retry_reserved_at=_now(),retry_source_proof=proof)
    state['recovery_chat']=record
    atomic_write_json(_state_path(workspace),state)
    _write_state(workspace,package_path=package_path,package_sha256=claim.package_sha256,
        prompt_sha256=claim.prompt_sha256,status='running',submitted=False,thread_id=thread_id,
        detail='Đã đối soát không có ảnh; thử thêm đúng một lần trong cùng chat phục hồi. Giữ nguyên link và bộ đếm cũ.')
    return True


def reserve_thumbnail_source_probe(workspace: Workspace,claim: ThumbnailAIClaim,thread_id: str)->bool:
    state=read_thumbnail_ai_state(workspace)
    if (not _source_probe_upgrade_available(state) or state.get('thread_id')!=thread_id
        or state.get('package_sha256')!=claim.package_sha256 or state.get('prompt_sha256')!=claim.prompt_sha256):return False
    state['source_read_version']=2
    atomic_write_json(_state_path(workspace),state)
    return True


def reserve_thumbnail_readonly_probe(workspace: Workspace, claim: ThumbnailAIClaim, thread_id: str) -> bool:
    """Read-only leases also consume their durable budget, so a hold cannot spin forever."""
    state = read_thumbnail_ai_state(workspace)
    if (state.get("package_sha256") != claim.package_sha256 or state.get("prompt_sha256") != claim.prompt_sha256
            or state.get("thread_id") != thread_id or _readonly_probe_count(state) >= THUMBNAIL_AI_MAX_READONLY_PROBES):
        return False
    state["readonly_probe_count"] = _readonly_probe_count(state) + 1
    atomic_write_json(_state_path(workspace), state)
    return True


def release_thumbnail_recovery_probe(workspace: Workspace, claim: ThumbnailAIClaim, thread_id: str, count: int) -> None:
    """An authentication/quota hold does not consume an image reconciliation attempt."""
    state = read_thumbnail_ai_state(workspace)
    if (state.get("package_sha256") == claim.package_sha256 and state.get("prompt_sha256") == claim.prompt_sha256
        and state.get("thread_id") == thread_id and state.get("submitted") is True
        and state.get("status") in {"recovering", "error", "uncertain"}
        and _recovery_probe_count(state) == count):
        state["recovery_probe_count"] = max(0, count - 1)
        atomic_write_json(_state_path(workspace), state)


def record_thumbnail_recovery_receipt(workspace: Workspace, claim: ThumbnailAIClaim, thread_id: str, created_at: object) -> bool:
    state = read_thumbnail_ai_state(workspace)
    if (state.get("package_sha256") != claim.package_sha256 or state.get("prompt_sha256") != claim.prompt_sha256
        or state.get("thread_id") != thread_id):
        raise ThumbnailAIError("Checkpoint đã đổi; không gắn biên nhận phục hồi vào lượt khác.")
    if not isinstance(created_at, (int, float)) or created_at <= 0:
        # A completed turn can legitimately clear the backend receipt. Read
        # its result without claiming that the video/checkpoint changed.
        return False
    if state.get("recovery_receipt_created_at") != created_at:
        state["recovery_probe_count"] = 0
        state["readonly_probe_count"] = 0
    state["recovery_receipt_created_at"] = created_at
    atomic_write_json(_state_path(workspace), state)
    return True


def record_unsubmitted_thumbnail_delivery(workspace: Workspace, package_path: Path,
                                         claim: ThumbnailAIClaim, thread_id: str, proof: object) -> bool:
    state = read_thumbnail_ai_state(workspace)
    expected = hashlib.sha256(" ".join(unicodedata.normalize("NFC", thumbnail_request_for_claim(claim)).split()).encode()).hexdigest()
    if (not isinstance(proof, dict) or proof.get("safe_to_resend") is not True
            or proof.get("thread_id") != thread_id or proof.get("prompt_sha256") != expected
            or state.get("thread_id") != thread_id or state.get("staged_image")
            or state.get("package_sha256") != claim.package_sha256
            or state.get("prompt_sha256") != claim.prompt_sha256
            or _sha256_bytes(package_path.read_bytes()) != claim.package_sha256):
        return False
    exhausted = int(state.get("delivery_retry_count") or 0) >= 1
    _write_state(workspace, package_path=package_path, package_sha256=claim.package_sha256,
                 prompt_sha256=claim.prompt_sha256, submitted=False,
                 status="delivery_failed" if exhausted else "delivery_retry_pending",
                 detail=("Bridge chứng minh lượt ảnh chưa gửi; đã hết một lần phục hồi truyền, giữ nguyên nguyên liệu."
                         if exhausted else "Bridge chứng minh lượt ảnh chưa gửi; chờ phục hồi truyền đúng một lần trong cùng chat."))
    state = read_thumbnail_ai_state(workspace)
    state["unsubmitted_image_proof"] = proof
    atomic_write_json(_state_path(workspace), state)
    return True


def mark_thumbnail_overridden(
    workspace: Workspace,
    *,
    source: str,
    thumbnail_path: Path | None,
) -> None:
    state = read_thumbnail_ai_state(workspace)
    package_path = Path(str(state.get("package_path") or workspace.output_dir)).resolve(
        strict=False
    )
    _write_state(
        workspace,
        package_path=package_path,
        package_sha256=str(state.get("package_sha256") or ""),
        prompt_sha256=str(state.get("prompt_sha256") or ""),
        status="overridden",
        detail=f"Thumbnail hiện tại đã được thay bằng nguồn {source}.",
        thumbnail_path=thumbnail_path,
        source=source,
    )


def _normalize_thumbnail_prompt(prompt: str) -> str:
    """Keep image prompts in the text-to-image path.

    The generated thumbnail brief historically described a Photoshop/editorial
    *composite*. ChatGPT can interpret those words as an image-edit request and
    ask for a source upload even when the brief contains no source image. The
    visual requirements are preserved, while edit-oriented boilerplate is
    rewritten locally before the single image-generation turn.
    """
    value = str(prompt or "").strip()
    value = value.replace(
        "Create exactly a 1280×720 true 16:9 YouTube thumbnail for a Global Japan Comparison documentary, "
        "using a hand-built real-photo Photoshop editorial composite with authentic industrial photography, "
        "believable factory lighting, natural imperfections, mild compression, used metal surfaces, oil stains, "
        "chips, scratched paint, realistic skin texture, and credible shadows.",
        "Create one original 1280×720 16:9 photorealistic documentary image generated directly from text, "
        "with authentic industrial lighting, used metal surfaces, oil stains, chips, realistic skin and shadows.",
    )
    value = value.replace("HYBRID composition:", "Single-scene composition:")
    value = value.replace(
        "Unified scene only, no hard split-screen divider, no news collage, no browser UI, no phone UI, "
        "no glossy AI look, no CGI, no polished luxury poster.",
        "Use one unified workshop scene with documentary realism, not a split-screen, collage, interface, "
        "CGI render, or glossy poster.",
    )
    value = re.sub(r"\bPhotoshop\b", "original", value, flags=re.IGNORECASE)
    value = value.replace("editorial composite", "documentary image")
    value = value.replace("hand-built real-photo", "original photorealistic")

    # The long conclusion and layout boilerplate tend to push the image tool
    # back into its edit/composite interpretation. The concrete subject,
    # composition, exact text and micro-labels above remain intact.
    value = value.replace(
        "Do not reveal a final winner, final purchasing decision, apology, contract change, or complete outcome. ",
        "",
    )
    value = value.replace(
        "Make all three large lines extremely bold, high-contrast, mobile-readable, each kept on one physical line, "
        "with strong black stroke and subtle shadow; YELLOW and RED should be the strongest visual hierarchy, "
        "CYAN slightly smaller but still dominant.",
        "Make the three large lines bold, high-contrast and mobile-readable.",
    )
    value = value.replace("Preserve faces, machines, flags and evidence from text overlap. ", "")
    value = re.sub(
        r"\s+(?:The image|The scene) must communicate(?: within one second)?\s*:.*$",
        "",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    return value


def thumbnail_generation_request(prompt: str) -> str:
    normalized_prompt = _normalize_thumbnail_prompt(prompt)
    return (
        "[DOCURANDOM_STAGE=thumbnail]\n"
        "Hãy dùng công cụ tạo hình ảnh của ChatGPT để tạo đúng MỘT thumbnail YouTube từ "
        "prompt hoàn chỉnh dưới đây. Phải tạo ảnh thật trong chính lượt này, không chỉ mô tả, "
        "không tiếp tục kịch bản, không xuất lại file tổng hợp, không trả tiêu đề/mô tả. "
        "không viết lại prompt và không tạo nhiều phương án. Giữ nguyên tuyệt đối mọi chuỗi chữ "
        "Đây là yêu cầu TẠO ẢNH MỚI TỪ ĐẦU, không phải chỉnh sửa ảnh có sẵn; không cần ảnh nguồn, "
        "không yêu cầu người dùng tải ảnh lên, và nếu giao diện hỏi thì chọn tạo ảnh mới. "
        "Nhật được yêu cầu trong prompt. Output hợp lệ duy nhất của lượt này là artifact ảnh; "
        "không trả lời bằng văn bản, không giải thích, không thêm danh sách hay phương án.\n\n"
        "--- PROMPT THUMBNAIL HOÀN CHỈNH ---\n"
        f"{normalized_prompt}\n"
        "--- HẾT PROMPT ---"
    )


def thumbnail_confirmation_request() -> str:
    return (
        "[DOCURANDOM_STAGE=thumbnail-confirm]\n"
        "Hãy kiểm tra đúng lượt ngay trước trong cuộc trò chuyện này. Nếu ảnh thumbnail đã được "
        f"tạo thành công và đang hiển thị, chỉ trả lời chính xác: {THUMBNAIL_CONFIRMATION}\n"
        "Nếu ảnh không được tạo, bị từ chối, lỗi chính sách, lỗi mạng hoặc còn đang tạo, không được "
        "trả lời ĐÃ TẠO; hãy trả lời ngắn theo mẫu KHÔNG TẠO ĐƯỢC: <lý do thật>."
    )


def is_thumbnail_confirmation(value: str) -> bool:
    clean = unicodedata.normalize("NFC", str(value or "")).strip()
    clean = clean.strip('"“”\'`').strip()
    return clean.casefold() == THUMBNAIL_CONFIRMATION.casefold()


def is_thumbnail_negative_confirmation(value: str) -> bool:
    """Return whether ChatGPT explicitly says the preceding image was not made.

    This is deliberately narrower than a generic non-success response.  A
    transient DOM/readback failure must keep the submitted checkpoint blocked,
    while the explicit ``KHÔNG TẠO ĐƯỢC`` reply can be checked once more without
    generating a second image.
    """

    clean = unicodedata.normalize("NFC", str(value or "")).strip()
    clean = clean.strip('"“”\'`').strip().casefold()
    return clean.startswith("không tạo được")
