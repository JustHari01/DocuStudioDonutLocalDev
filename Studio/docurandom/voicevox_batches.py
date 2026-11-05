"""Bounded voice audits with durable, independently verified part receipts."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable

from .storage import atomic_write_json
from .voicevox_project import (
    MAX_AUDIT_BOXES, VoicevoxAuditResult, VoicevoxProjectError,
    build_passthrough_audit_plan, build_audit_attachment, load_voicevox_project, project_boxes,
    voicevox_project_digest, write_final_project, write_project_checkpoint,
)


def _talk(data: dict[str, object]) -> dict:
    return data["talk"] if isinstance(data.get("talk"), dict) else data


def _verify_part(original: dict, audited: dict) -> None:
    before, after = project_boxes(original), project_boxes(audited)
    if len(before) != len(after):
        raise VoicevoxProjectError("Số box của phần audit đã thay đổi.")
    for old, new in zip(before, after, strict=True):
        if (old.audio_key, old.text, old.engine_id, old.speaker_id) != (
            new.audio_key, new.text, new.engine_id, new.speaker_id
        ):
            raise VoicevoxProjectError("Phần audit đã đổi nội dung hoặc danh tính giọng.")


def audit_project_batches(
    project_data: dict[str, object], *, source_path: Path,
    allowed_style_ids: Iterable[int],
    audit_single: Callable[[dict, Path], tuple[dict, VoicevoxAuditResult]],
    progress: Callable[[str], None], check_cancel: Callable[[], None],
    max_bytes: int | None = None, styles=(),
) -> tuple[dict[str, object], VoicevoxAuditResult]:
    boxes = project_boxes(project_data)
    allowed_style_ids = tuple(allowed_style_ids)
    digest = voicevox_project_digest(project_data)
    root = source_path.parent / (source_path.stem + "_audit_parts") / digest
    if max_bytes is not None:
        root = root / f"bytes-{int(max_bytes)}"
    root.mkdir(parents=True, exist_ok=True)
    merged = copy.deepcopy(project_data)
    merged_items = _talk(merged)["audioItems"]
    source_talk = _talk(project_data)
    results: list[tuple[int, VoicevoxAuditResult]] = []
    covered: set[str] = set()
    script_context = [{"boxIndex": box.index, "text": box.text} for box in boxes]
    def make_part(offset, selected):
        keys = [box.audio_key for box in selected]
        part = copy.deepcopy(project_data)
        talk = _talk(part)
        talk["audioKeys"] = keys
        talk["audioItems"] = {key: copy.deepcopy(source_talk["audioItems"][key]) for key in keys}
        part["docurandomAuditContext"] = {
            "projectDigest": digest, "totalBoxCount": len(boxes),
            "globalBoxStart": offset + 1, "globalBoxEnd": offset + len(selected),
            "script": script_context,
        }
        return part

    parts = []
    offset = 0
    while offset < len(boxes):
        size = min(MAX_AUDIT_BOXES, len(boxes) - offset)
        while True:
            selected = boxes[offset:offset + size]
            part = make_part(offset, selected)
            if max_bytes is None or len(build_audit_attachment(part, styles=styles)) <= max_bytes:
                break
            if size == 1:
                raise VoicevoxProjectError("Ngữ cảnh audit vượt giới hạn byte; giữ project, không gửi tệp quá lớn.")
            size = max(1, size // 2)
        parts.append((offset, selected, part))
        offset += size
    part_count = len(parts)
    for part_number, (offset, selected, part) in enumerate(parts, 1):
        check_cancel()
        part_path = root / f"part-{part_number:04d}.vvproj"
        receipt_path = root / f"part-{part_number:04d}.json"
        part_digest = voicevox_project_digest(part)
        audited = result = None
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
            final = part_path.with_name(part_path.stem + "_VOICEVOX_FINAL_audited.vvproj")
            content = part_path.with_name(part_path.stem + "_content_FINAL.txt")
            if (receipt.get("schema_version") == 1 and receipt.get("source_digest") == part_digest
                    and receipt.get("used_safe_fallback") is False):
                candidate = load_voicevox_project(final)
                _verify_part(part, candidate)
                if voicevox_project_digest(candidate) != receipt.get("audited_digest"):
                    raise VoicevoxProjectError("Phần FINAL đã đổi sau checkpoint audit.")
                if content.read_text(encoding="utf-8") != "\n".join(box.text for box in selected) + "\n":
                    raise VoicevoxProjectError("Nội dung phần FINAL không khớp.")
                result = VoicevoxAuditResult(
                    final, content, len(selected), int(receipt["preset_count"]),
                    int(receipt["override_count"]), int(receipt["pronunciation_count"]),
                    tuple(sorted({box.style_id for box in project_boxes(candidate)})),
                    tuple(int(i) for i in receipt["pronunciation_indexes"]),
                )
                audited = candidate
        except (OSError, ValueError, KeyError, TypeError, VoicevoxProjectError):
            pass
        if audited is None:
            final_candidate = part_path.with_name(part_path.stem + "_VOICEVOX_FINAL_audited.vvproj")
            if final_candidate.is_file() and not receipt_path.is_file():
                raise VoicevoxProjectError(
                    "Phần audit đã có FINAL nhưng thiếu biên nhận; giữ checkpoint để xác minh, không gửi audit trùng."
                )
            write_project_checkpoint(part_path, part)
            progress(f"Audit VOICEVOX phần {part_number}/{part_count} · box {offset + 1}–{offset + len(selected)} · giữ ngữ cảnh toàn kịch bản…")
            audited, result = audit_single(part, part_path)
            _verify_part(part, audited)
            if any(box.style_id not in allowed_style_ids for box in project_boxes(audited)):
                raise VoicevoxProjectError("Phần audit đổi style ngoài nhân vật được phép.")
            atomic_write_json(receipt_path, {
                "schema_version": 1, "source_digest": part_digest,
                "audited_digest": voicevox_project_digest(audited),
                "preset_count": result.preset_count, "override_count": result.override_count,
                "pronunciation_count": result.pronunciation_count,
                "pronunciation_indexes": list(result.pronunciation_indexes),
                "used_safe_fallback": result.used_safe_fallback,
            })
        else:
            progress(f"Đã xác minh phần audit {part_number}/{part_count} từ checkpoint · không gửi lại.")
        assert result is not None
        for box in project_boxes(audited):
            if box.audio_key in covered:
                raise VoicevoxProjectError("Phần audit có box trùng.")
            covered.add(box.audio_key)
            merged_items[box.audio_key] = copy.deepcopy(_talk(audited)["audioItems"][box.audio_key])
        results.append((offset, result))
    if covered != {box.audio_key for box in boxes}:
        raise VoicevoxProjectError("Audit chưa bao phủ mọi box của project.")
    _verify_part(project_data, merged)
    plan = build_passthrough_audit_plan(merged, allowed_style_ids=allowed_style_ids)
    final = write_final_project(source_path=source_path, project_data=merged, plan=plan)
    return merged, replace(
        final, preset_count=sum(r.preset_count for _, r in results),
        override_count=sum(r.override_count for _, r in results),
        pronunciation_count=sum(r.pronunciation_count for _, r in results),
        pronunciation_indexes=tuple(offset + i for offset, r in results for i in r.pronunciation_indexes),
        used_safe_fallback=any(r.used_safe_fallback for _, r in results),
    )
