"""Bind automatic narration to the current video's exported package."""
from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

from .content_ai import extract_voicevox_script_from_package
from .content_quality import narration_problem, narration_duration_problem, minimum_narration_characters
from .storage import atomic_write_json
from .voicevox import VoicevoxError, parse_script_lines
from .voicevox_project import VoicevoxProjectError, load_voicevox_project, project_boxes


def narration_digest(script: str) -> str:
    return hashlib.sha256("\n".join(parse_script_lines(script)).encode("utf-8")).hexdigest()


def package_narration(path: Path) -> str:
    try:
        script = extract_voicevox_script_from_package(path.read_text(encoding="utf-8-sig"))
        problem = narration_problem(script)
        if problem:
            raise VoicevoxError("NARRATION_CONTENT_INCOMPLETE: " + problem)
        parse_script_lines(script)
    except (OSError, UnicodeError, ValueError, VoicevoxError) as exc:
        raise VoicevoxError(
            "File tổng hợp hiện tại chưa có thoại VOICEVOX hợp lệ; "
            "không dùng kịch bản hoặc audio của video cũ. Chi tiết: " + str(exc)
        ) from exc
    return script


def validate_narration_duration(package: Path | None, seconds: float | None = None,
                                 requested_minutes: float = 0) -> None:
    if package is None:
        return
    roots = (package.parent, package.parent.parent)
    checkpoint = {}
    state_root = package.parent.parent
    for root in roots:
        for path in (root / '.docurandom/pipeline_checkpoint.json', root / 'State/pipeline_checkpoint.json'):
            if path.is_file():
                checkpoint = json.loads(path.read_text(encoding='utf-8-sig'))
                state_root = root
                break
        if checkpoint:
            break
    requested = requested_minutes or float(checkpoint.get('identity', {}).get('minutes', 0) or 0)
    if not requested:
        return  # Legacy/manual packages do not claim a production duration.
    voice = checkpoint.get('stages', {}).get('voicevox', {}).get('artifacts', {})
    if seconds is None:
        seconds = voice.get('duration_seconds')
        audio_name = Path(str(voice.get('audio') or '')).name
        for audio in (state_root / 'VOICEVOX' / audio_name,):
            if audio.is_file() and audio.suffix.casefold() == '.wav':
                with wave.open(str(audio), 'rb') as stream:
                    seconds = stream.getnframes() / stream.getframerate()
                break
    if seconds is None:
        raise VoicevoxError('NARRATION_CONTENT_INCOMPLETE: Thiếu bằng chứng thời lượng giọng đọc của đúng video.')
    problem = narration_duration_problem(float(seconds), requested)
    if problem:
        raise VoicevoxError(problem)


def record_narration_duration_repair(package: Path, seconds: float, minutes: float,
                                     script: str) -> None:
    """A short real synthesis asks the same content checkpoint for a longer script."""
    path = package.with_name(package.stem + '_AI_CHECKPOINT.json')
    if not path.is_file() or not narration_duration_problem(seconds, minutes):
        return
    document = json.loads(path.read_text(encoding='utf-8-sig'))
    if document.get('project_name') != package.stem:
        raise VoicevoxError('Checkpoint nội dung không thuộc đúng video cần bổ sung.')
    # Use the measured speech rate instead of retrying the unchanged script.
    required = max(minimum_narration_characters(minutes),
                   int(len(script) * minutes * 60 * .70 / max(1, seconds) * 1.15))
    document['minimum_narration_characters'] = max(
        int(document.get('minimum_narration_characters', 0) or 0), required)
    document['narration_repair_reason'] = narration_duration_problem(seconds, minutes)
    atomic_write_json(path, document)


def validate_narration_snapshot(package: Path | None, script: str, digest: str) -> None:
    if package is None:
        return  # Standalone manual narration keeps the user's editor text.
    if not digest or narration_digest(script) != digest or narration_digest(package_narration(package)) != digest:
        raise VoicevoxError(
            "Gói nội dung đã đổi sau khi xếp hàng giọng đọc; giữ checkpoint "
            "và tạo lại giọng cho đúng video trước khi render."
        )


def validate_project_narration(package: Path, project: Path) -> str:
    expected = narration_digest(package_narration(package))
    try:
        actual = narration_digest("\n".join(box.text for box in project_boxes(load_voicevox_project(project))))
    except (OSError, ValueError, VoicevoxError, VoicevoxProjectError) as exc:
        raise VoicevoxError("Không đọc được project giọng của video; giữ checkpoint trước upload.") from exc
    if actual != expected:
        raise VoicevoxError("Giọng đọc không khớp file tổng hợp hiện tại; phải tạo lại trước render/upload.")
    validate_narration_duration(package)
    return expected
