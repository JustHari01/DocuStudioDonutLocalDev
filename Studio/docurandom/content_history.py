from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from .storage import atomic_write_json, atomic_write_text


CONTENT_HISTORY_VERSION = 1
MAX_STORED_TITLES = 10_000
MAX_PROMPT_TITLES = 300
MAX_PROMPT_HISTORY_CHARACTERS = 24_000
_LOCKS_GUARD = threading.RLock()
_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.expanduser().resolve(strict=False)).casefold()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def normalize_content_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    return "".join(character for character in normalized if character.isalnum())


def find_duplicate_title(
    title: str,
    previous_titles: Iterable[str],
    *,
    similarity_threshold: float = 0.92,
) -> tuple[str, float] | None:
    candidate = normalize_content_title(title)
    if not candidate:
        return None
    best: tuple[str, float] | None = None
    for previous in previous_titles:
        existing = normalize_content_title(previous)
        if not existing:
            continue
        if candidate == existing:
            return str(previous).strip(), 1.0
        # Very short titles often share generic words. Only use fuzzy blocking
        # once both titles contain enough information for a reliable match.
        if min(len(candidate), len(existing)) < 12:
            continue
        score = SequenceMatcher(None, candidate, existing, autojunk=False).ratio()
        if score >= similarity_threshold and (best is None or score > best[1]):
            best = (str(previous).strip(), score)
    return best


def select_prompt_title_history(
    titles: Iterable[str],
    *,
    max_items: int = MAX_PROMPT_TITLES,
    max_characters: int = MAX_PROMPT_HISTORY_CHARACTERS,
) -> tuple[str, ...]:
    cleaned = [str(title).strip() for title in titles if str(title).strip()]
    selected: list[str] = []
    used = 0
    for title in reversed(cleaned):
        line_cost = len(title) + 12
        if selected and (len(selected) >= max_items or used + line_cost > max_characters):
            break
        if not selected and line_cost > max_characters:
            title = title[: max(1, max_characters - 12)].rstrip()
            line_cost = len(title) + 12
        selected.append(title)
        used += line_cost
    selected.reverse()
    return tuple(selected)


def format_title_history_for_prompt(
    titles: Iterable[str],
    *,
    total_count: int | None = None,
) -> str:
    selected = select_prompt_title_history(titles)
    if not selected:
        return ""
    total = max(len(selected), int(total_count or len(selected)))
    lines = "\n".join(f"- [x] {title}" for title in selected)
    omitted = total - len(selected)
    scope = (
        f"Danh sách dưới đây là {len(selected)} tiêu đề gần nhất trong tổng số {total} tiêu đề đã làm."
        if omitted > 0
        else f"Danh sách dưới đây gồm toàn bộ {total} tiêu đề đã làm."
    )
    return (
        "CHỐNG TRÙNG NỘI DUNG — BẮT BUỘC\n"
        f"{scope}\n"
        "Không được làm lại cùng chủ đề, cùng sự kiện, cùng luận điểm hoặc chỉ đổi cách diễn đạt "
        "của bất kỳ nội dung nào trong danh sách. Hãy chọn một nội dung khác rõ rệt.\n\n"
        f"{lines}"
    )


@dataclass(frozen=True)
class ContentHistoryEntry:
    title: str
    normalized_title: str
    recorded_at: str
    video_path: str = ""
    package_path: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "normalized_title": self.normalized_title,
            "recorded_at": self.recorded_at,
            "video_path": self.video_path,
            "package_path": self.package_path,
        }


class ContentHistoryStore:
    def __init__(self, path: Path, text_path: Path | None = None) -> None:
        self.path = path.expanduser().resolve(strict=False)
        self.text_path = (
            text_path.expanduser().resolve(strict=False)
            if text_path is not None
            else self.path.with_suffix(".txt")
        )
        self._lock = _path_lock(self.path)

    @staticmethod
    def _entry_from_value(value: object) -> ContentHistoryEntry | None:
        if not isinstance(value, dict):
            return None
        title = str(value.get("title", "")).strip()
        normalized = normalize_content_title(title)
        if not title or not normalized:
            return None
        return ContentHistoryEntry(
            title=title,
            normalized_title=normalized,
            recorded_at=str(value.get("recorded_at", "")).strip(),
            video_path=str(value.get("video_path", "")).strip(),
            package_path=str(value.get("package_path", "")).strip(),
        )

    def _load(self) -> list[ContentHistoryEntry]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            values = raw.get("titles") if isinstance(raw, dict) else None
            if not isinstance(values, list):
                raise ValueError("invalid content history")
            entries = [self._entry_from_value(value) for value in values]
            return [entry for entry in entries if entry is not None]
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            backup = self.path.with_name(f"{self.path.stem}.corrupt.json")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            return []

    def entries(self) -> tuple[ContentHistoryEntry, ...]:
        with self._lock:
            return tuple(self._load())

    def ensure_files(self) -> None:
        with self._lock:
            entries = self._load()
            if not self.path.is_file() or not self.text_path.is_file():
                self._write(entries)

    def titles(self) -> tuple[str, ...]:
        return tuple(entry.title for entry in self.entries())

    def prompt_titles(self) -> tuple[str, ...]:
        return select_prompt_title_history(self.titles())

    def _write(self, entries: list[ContentHistoryEntry]) -> None:
        atomic_write_json(
            self.path,
            {
                "version": CONTENT_HISTORY_VERSION,
                "titles": [entry.to_dict() for entry in entries[-MAX_STORED_TITLES:]],
            },
        )
        lines = [
            "CHECKLIST NỘI DUNG ĐÃ LÀM — DOCURANDOM",
            "",
            "File này chỉ được cập nhật sau khi YouTube xác nhận upload hoặc người dùng đánh dấu đã upload thủ công.",
            "Video mới render nhưng chưa upload sẽ không được tính vào checklist chống trùng.",
            "",
        ]
        lines.extend(
            f"- [x] {entry.title}"
            for entry in entries[-MAX_STORED_TITLES:]
        )
        atomic_write_text(self.text_path, "\n".join(lines).rstrip() + "\n")

    def add(
        self,
        title: str,
        *,
        video_path: Path | None = None,
        package_path: Path | None = None,
        recorded_at: str | None = None,
    ) -> bool:
        clean_title = re.sub(r"\s+", " ", str(title)).strip()
        normalized = normalize_content_title(clean_title)
        if not clean_title or not normalized:
            raise ValueError("Tiêu đề chống trùng đang trống.")
        with self._lock:
            entries = self._load()
            if any(entry.normalized_title == normalized for entry in entries):
                return False
            entries.append(
                ContentHistoryEntry(
                    title=clean_title,
                    normalized_title=normalized,
                    recorded_at=recorded_at or datetime.now(timezone.utc).isoformat(),
                    video_path=str(video_path or ""),
                    package_path=str(package_path or ""),
                )
            )
            self._write(entries[-MAX_STORED_TITLES:])
            return True

    def remove(
        self,
        *,
        title: str | None = None,
        video_path: Path | None = None,
        package_path: Path | None = None,
    ) -> int:
        """Remove entries belonging to an unconfirmed upload.

        The checklist is intentionally upload-confirmed only.  This helper is
        used when an older build recorded a title at render time and the user
        starts a new session without having uploaded that video.
        """

        normalized = normalize_content_title(str(title or "").strip())
        video_key = (
            str(video_path.expanduser().resolve(strict=False)).casefold()
            if video_path is not None
            else ""
        )
        package_key = (
            str(package_path.expanduser().resolve(strict=False)).casefold()
            if package_path is not None
            else ""
        )
        if not normalized and not video_key and not package_key:
            return 0
        with self._lock:
            entries = self._load()
            kept: list[ContentHistoryEntry] = []
            removed = 0
            for entry in entries:
                entry_video = str(Path(entry.video_path).expanduser().resolve(strict=False)).casefold() if entry.video_path else ""
                entry_package = str(Path(entry.package_path).expanduser().resolve(strict=False)).casefold() if entry.package_path else ""
                matches = bool(
                    (normalized and entry.normalized_title == normalized)
                    or (video_key and entry_video == video_key)
                    or (package_key and entry_package == package_key)
                )
                if matches:
                    removed += 1
                else:
                    kept.append(entry)
            if removed:
                self._write(kept)
            return removed
