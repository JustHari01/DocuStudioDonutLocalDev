from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .storage import app_data_dir, atomic_write_json


VOICEVOX_QA_SCHEMA_VERSION = 1
VOICEVOX_QA_MODEL = "small"
VOICEVOX_QA_MAX_ROUTINE_LINES = 12

_MODEL_LOCK = threading.RLock()
_MODEL: object | None = None
_MODEL_THREADS = 0
_MODEL_NAME = ""
_TRANSCRIBE_LOCK = threading.RLock()


@dataclass(frozen=True)
class VoicevoxQaLine:
    box_index: int
    expected: str
    recognized: str
    similarity: float
    warning: bool


@dataclass(frozen=True)
class VoicevoxQaResult:
    report_path: Path
    status: str
    checked_count: int
    warning_indexes: tuple[int, ...]
    reused: bool = False
    detail: str = ""
    synthesis_result: object | None = None


def select_voicevox_qa_lines(
    lines: Sequence[str],
    *,
    pronunciation_indexes: Iterable[int] = (),
    limit: int = VOICEVOX_QA_MAX_ROUTINE_LINES,
) -> tuple[int, ...]:
    """Pick high-risk boxes without transcribing an entire long narration."""

    if not lines:
        return ()
    mandatory = {
        int(index)
        for index in pronunciation_indexes
        if 1 <= int(index) <= len(lines)
    }
    scored: list[tuple[int, int]] = []
    for index, line in enumerate(lines, 1):
        score = 0
        if index in {1, len(lines)}:
            score += 6
        if re.search(r"[A-Za-zＡ-Ｚａ-ｚ0-9０-９]", line):
            score += 5
        if re.search(r"[〇零一二三四五六七八九十百千万億兆]", line):
            score += 2
        if re.search(r"[・＝=％%℃°+＋−-]", line):
            score += 2
        if score:
            scored.append((score, index))
    routine_slots = max(0, int(limit) - len(mandatory))
    selected = set(mandatory)
    selected.update(
        index
        for _score, index in sorted(scored, key=lambda item: (-item[0], item[1]))[
            :routine_slots
        ]
    )
    return tuple(sorted(selected))


def _normalized_japanese(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(
        character
        for character in normalized
        if character.isalnum()
        or "\u3040" <= character <= "\u30ff"
        or "\u3400" <= character <= "\u9fff"
    )


def voicevox_transcript_similarity(expected: str, recognized: str) -> float:
    left = _normalized_japanese(expected)
    right = _normalized_japanese(recognized)
    if not left or not right:
        return 0.0
    return round(SequenceMatcher(None, left, right, autojunk=False).ratio(), 4)


def _qa_signature(
    *,
    lines: Sequence[str],
    indexes: Sequence[int],
    parts_dir: Path,
    model_name: str = VOICEVOX_QA_MODEL,
) -> str:
    files: list[dict[str, object]] = []
    for index in indexes:
        path = parts_dir / f"{index:04d}.wav"
        stat = path.stat()
        files.append(
            {
                "index": index,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    payload = {
        "schema": VOICEVOX_QA_SCHEMA_VERSION,
        "model": model_name,
        "lines": list(lines),
        "indexes": list(indexes),
        "files": files,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _load_model(cpu_threads: int, model_name: str = VOICEVOX_QA_MODEL) -> object:
    global _MODEL, _MODEL_THREADS, _MODEL_NAME
    threads = max(1, min(6, int(cpu_threads)))
    with _MODEL_LOCK:
        if _MODEL is not None and _MODEL_THREADS == threads and _MODEL_NAME == model_name:
            return _MODEL
        from faster_whisper import WhisperModel

        model_root = app_data_dir() / "Speech QA" / "models"
        model_root.mkdir(parents=True, exist_ok=True)
        _MODEL = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=threads,
            num_workers=1,
            download_root=str(model_root),
        )
        _MODEL_THREADS = threads
        _MODEL_NAME = model_name
        return _MODEL


def _recognize_part(path: Path, *, cpu_threads: int, model_name: str = VOICEVOX_QA_MODEL) -> str:
    with path.open("rb") as stream:
        audio_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    cache = app_data_dir() / "Speech QA" / "transcripts" / model_name / (audio_sha + ".json")
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if (saved.get("audio_sha256") == audio_sha and saved.get("model") == model_name
                and saved.get("decoder") == "ja-v1" and isinstance(saved.get("recognized"), str)):
            return saved["recognized"]
    except (OSError, ValueError, TypeError):
        pass
    with _TRANSCRIBE_LOCK:
        model = _load_model(cpu_threads, model_name)
        segments, _info = model.transcribe(
            str(path), language="ja", beam_size=3 if model_name == "large-v3" else 1,
            best_of=1, condition_on_previous_text=False, vad_filter=False,
        )
        result = "".join(str(segment.text).strip() for segment in segments).strip()
        atomic_write_json(cache, {"audio_sha256": audio_sha, "model": model_name,
                                  "decoder": "ja-v1", "recognized": result})
        return result


def audit_voicevox_audio(
    *,
    metadata_path: Path,
    lines: Sequence[str],
    pronunciation_indexes: Iterable[int] = (),
    cpu_threads: int = 4,
    cancel_event: threading.Event | None = None,
    progress: Callable[[str], None] | None = None,
    recognizer: Callable[[Path], str] | None = None,
    full_audit: bool = False,
    model_name: str = VOICEVOX_QA_MODEL,
) -> VoicevoxQaResult:
    """Run a bounded, non-destructive Japanese ASR check on selected boxes.

    ASR is intentionally advisory: a low similarity is written as a warning
    and never mutates a correct VOICEVOX query based on a probabilistic model.
    """

    target = metadata_path.expanduser().resolve(strict=False)
    report_path = target.with_name(f"{target.stem}.qa.json")
    try:
        metadata = json.loads(target.read_text(encoding="utf-8"))
        parts_checkpoint = Path(str(metadata.get("parts_checkpoint") or ""))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        parts_checkpoint = Path()
    parts_dir = parts_checkpoint.parent if parts_checkpoint.name else None
    indexes = tuple(range(1, len(lines) + 1)) if full_audit else select_voicevox_qa_lines(
        lines,
        pronunciation_indexes=pronunciation_indexes,
    )
    available = tuple(
        index
        for index in indexes
        if parts_dir is not None and (parts_dir / f"{index:04d}.wav").is_file()
    )
    if not available:
        atomic_write_json(
            report_path,
            {
                "schema_version": VOICEVOX_QA_SCHEMA_VERSION,
                "status": "skipped",
                "detail": "Không có WAV từng box hợp lệ để hậu kiểm.",
                "checked_indexes": [],
            },
        )
        return VoicevoxQaResult(
            report_path,
            "skipped",
            0,
            (),
            detail="Không có WAV từng box hợp lệ để hậu kiểm.",
        )
    try:
        assert parts_dir is not None
        signature = _qa_signature(lines=lines, indexes=available, parts_dir=parts_dir, model_name=model_name)
    except OSError as exc:
        return VoicevoxQaResult(report_path, "skipped", 0, (), detail=str(exc))
    try:
        previous = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        previous = {}
    if (
        isinstance(previous, dict)
        and previous.get("signature") == signature
        and previous.get("status") in {"passed", "warning"}
    ):
        warnings = tuple(
            int(value) for value in previous.get("warning_indexes", [])
        )
        return VoicevoxQaResult(
            report_path,
            str(previous.get("status") or "passed"),
            len(previous.get("lines", [])),
            warnings,
            reused=True,
        )

    if progress:
        progress(
            f"Đang hậu kiểm phát âm tiếng Nhật bằng Whisper {model_name} trên CPU; "
            "lần đầu có thể cần tải model một lần."
        )
    checked: list[VoicevoxQaLine] = []
    try:
        for position, index in enumerate(available, 1):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Đã hủy hậu kiểm giọng.")
            if progress:
                progress(f"Đang hậu kiểm box {index} · {position}/{len(available)}…")
            part = parts_dir / f"{index:04d}.wav"
            recognized = (
                recognizer(part)
                if recognizer is not None
                else _recognize_part(part, cpu_threads=cpu_threads, model_name=model_name)
            )
            similarity = voicevox_transcript_similarity(lines[index - 1], recognized)
            # Only flag gross disagreement. Japanese ASR may choose equivalent
            # kanji/kana spellings, so a stricter threshold creates false alarms.
            warning = similarity < 0.30
            checked.append(
                VoicevoxQaLine(
                    box_index=index,
                    expected=lines[index - 1],
                    recognized=recognized,
                    similarity=similarity,
                    warning=warning,
                )
            )
    except ImportError:
        detail = "Runtime Whisper chưa được đóng gói; bỏ qua hậu kiểm, WAV vẫn hợp lệ."
        atomic_write_json(
            report_path,
            {
                "schema_version": VOICEVOX_QA_SCHEMA_VERSION,
                "signature": signature,
                "status": "skipped",
                "detail": detail,
                "checked_indexes": [],
            },
        )
        return VoicevoxQaResult(report_path, "skipped", 0, (), detail=detail)
    except InterruptedError:
        raise
    except Exception as exc:
        detail = f"Whisper tạm không khả dụng: {type(exc).__name__}: {exc}"
        atomic_write_json(
            report_path,
            {
                "schema_version": VOICEVOX_QA_SCHEMA_VERSION,
                "signature": signature,
                "status": "skipped",
                "detail": detail,
                "checked_indexes": [],
            },
        )
        return VoicevoxQaResult(report_path, "skipped", 0, (), detail=detail)

    warning_indexes = tuple(item.box_index for item in checked if item.warning)
    status = "warning" if warning_indexes else "passed"
    if full_audit and len(available) != len(lines):
        status = "incomplete"
    atomic_write_json(
        report_path,
        {
            "schema_version": VOICEVOX_QA_SCHEMA_VERSION,
            "signature": signature,
            "model": model_name,
            "coverage": "full" if full_audit and len(available) == len(lines) else "partial",
            "device": "cpu",
            "compute_type": "int8",
            "status": status,
            "checked_indexes": list(available),
            "warning_indexes": list(warning_indexes),
            "lines": [
                {
                    "box_index": item.box_index,
                    "expected": item.expected,
                    "recognized": item.recognized,
                    "similarity": item.similarity,
                    "warning": item.warning,
                }
                for item in checked
            ],
        },
    )
    return VoicevoxQaResult(
        report_path,
        status,
        len(checked),
        warning_indexes,
    )
