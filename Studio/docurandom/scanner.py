from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import time
from collections import Counter
from fractions import Fraction
from pathlib import Path
from threading import Event
from typing import Iterable

from PIL import Image

from .ffmpeg_manager import find_tools
from .models import MediaInfo


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".jfif",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".webm",
    ".m4v",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".ts",
    ".mts",
    ".m2ts",
    ".gif",
}

AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".wma",
}

SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
_INTERNAL_MEDIA_DIR_NAMES = frozenset({"_watermark_clean"})


def natural_key(value: str) -> tuple[object, ...]:
    """Return a Unicode-friendly natural-sort key (1, 2, 10)."""
    parts = re.split(r"(\d+)", value.casefold().replace("\\", "/"))
    return tuple(int(part) if part.isdigit() else part for part in parts)


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _raise_if_cancelled(cancel_event: Event | None, message: str) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError(message)


def _candidate_paths(
    source_dir: Path,
    recursive: bool,
    extensions: set[str],
    cancel_event: Event | None = None,
) -> Iterable[Path]:
    iterator = source_dir.rglob("*") if recursive else source_dir.iterdir()
    for path in iterator:
        _raise_if_cancelled(cancel_event, "media scan cancelled")
        if recursive:
            try:
                relative_parts = path.relative_to(source_dir).parts[:-1]
            except ValueError:
                relative_parts = ()
            if any(
                part.casefold() in _INTERNAL_MEDIA_DIR_NAMES
                for part in relative_parts
            ):
                continue
        if path.is_file() and path.suffix.casefold() in extensions:
            yield path


def _relative_natural_key(
    path: Path,
    source_dir: Path,
    cancel_event: Event | None,
) -> tuple[object, ...]:
    _raise_if_cancelled(cancel_event, "media scan cancelled")
    return natural_key(path.relative_to(source_dir).as_posix())


def _rate_to_float(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _sha256_file(path: Path, cancel_event: Event | None = None) -> str:
    _raise_if_cancelled(cancel_event, "media scan cancelled")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            _raise_if_cancelled(cancel_event, "media scan cancelled")
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    _raise_if_cancelled(cancel_event, "media scan cancelled")
    return digest.hexdigest()


def _remove_exact_duplicates(
    paths: list[Path],
    cancel_event: Event | None = None,
) -> tuple[list[Path], list[str]]:
    """Remove byte-identical sources while avoiding hashing unique-size files."""
    sizes: dict[Path, int | None] = {}
    for path in paths:
        _raise_if_cancelled(cancel_event, "media scan cancelled")
        try:
            sizes[path] = path.stat().st_size
        except OSError:
            sizes[path] = None
    size_counts: Counter[int] = Counter()
    for size in sizes.values():
        _raise_if_cancelled(cancel_event, "media scan cancelled")
        if size is not None:
            size_counts[size] += 1
    repeated_sizes = {size for size, count in size_counts.items() if count > 1}
    seen: dict[tuple[int, str], Path] = {}
    unique: list[Path] = []
    warnings: list[str] = []
    for path in paths:
        _raise_if_cancelled(cancel_event, "media scan cancelled")
        size = sizes[path]
        if size is None or size not in repeated_sizes:
            unique.append(path)
            continue
        try:
            digest = _sha256_file(path, cancel_event)
        except InterruptedError:
            raise
        except OSError:
            unique.append(path)
            continue
        key = (size, digest)
        original = seen.get(key)
        if original is None:
            seen[key] = path
            unique.append(path)
        else:
            warnings.append(
                f"Bỏ qua {path.name}: nội dung trùng hoàn toàn với {original.name}"
            )
    return unique, warnings


def probe_media_file(
    path: Path,
    ffprobe_path: Path,
    cancel_event: Event | None = None,
    timeout: float = 30,
) -> dict[str, object]:
    command = [
        str(ffprobe_path),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    returncode, stdout, stderr = _run_probe_process(
        command,
        cancel_event=cancel_event,
        timeout=timeout,
        cancel_message="video probe cancelled",
    )
    if returncode != 0:
        raise ValueError(stderr.strip() or "ffprobe không đọc được tệp")
    try:
        data = json.loads(stdout)
        streams = data.get("streams", [])
        video_stream = next(
            stream for stream in streams if stream.get("codec_type") == "video"
        )
        format_data = data.get("format", {})
        duration = float(
            video_stream.get("duration")
            or format_data.get("duration")
            or 0.0
        )
        width = int(video_stream.get("width") or 0)
        height = int(video_stream.get("height") or 0)
    except (StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("tệp không có luồng video hợp lệ") from exc
    if duration <= 0 or width <= 0 or height <= 0:
        raise ValueError("không xác định được thời lượng hoặc kích thước video")

    rotation = 0
    tags = video_stream.get("tags") or {}
    try:
        rotation = int(float(tags.get("rotate", 0)))
    except (TypeError, ValueError):
        rotation = 0
    for side_data in video_stream.get("side_data_list") or []:
        if side_data.get("rotation") is not None:
            try:
                rotation = int(float(side_data["rotation"]))
            except (TypeError, ValueError):
                pass
    if abs(rotation) % 180 == 90:
        width, height = height, width

    fps = _rate_to_float(
        str(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "")
    )
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    return {
        "width": width,
        "height": height,
        "duration": duration,
        "fps": fps,
        "codec_name": str(video_stream.get("codec_name") or ""),
        "has_audio": has_audio,
    }


def _stop_probe_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Stop and reap a probe process without leaving it behind."""
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        return process.communicate(timeout=0.75)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        return process.communicate()


def _run_probe_process(
    command: list[str],
    *,
    cancel_event: Event | None,
    timeout: float,
    cancel_message: str,
) -> tuple[int, str, str]:
    _raise_if_cancelled(cancel_event, cancel_message)
    timeout_seconds = float(timeout)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_creation_flags(),
    )
    started = time.monotonic()
    try:
        while True:
            _raise_if_cancelled(cancel_event, cancel_message)
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                stdout, stderr = _stop_probe_process(process)
                raise subprocess.TimeoutExpired(
                    command,
                    timeout,
                    output=stdout,
                    stderr=stderr,
                )
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        _raise_if_cancelled(cancel_event, cancel_message)
        return int(process.returncode or 0), stdout, stderr
    except BaseException:
        if process.poll() is None:
            _stop_probe_process(process)
        raise


def probe_audio_duration(
    path: Path,
    ffprobe_path: Path,
    cancel_event: Event | None = None,
    timeout: float = 30,
) -> float:
    command = [
        str(ffprobe_path),
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=duration:format=duration",
        "-of",
        "json",
        str(path),
    ]
    returncode, stdout, stderr = _run_probe_process(
        command,
        cancel_event=cancel_event,
        timeout=timeout,
        cancel_message="audio probe cancelled",
    )
    if returncode != 0:
        raise ValueError(stderr.strip() or "ffprobe không đọc được audio")
    try:
        data = json.loads(stdout)
        streams = data.get("streams") or []
        stream_duration = streams[0].get("duration") if streams else None
        duration = float(stream_duration or data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("không xác định được thời lượng audio") from exc
    if duration <= 0:
        raise ValueError("audio không có thời lượng hợp lệ")
    return duration


def find_audio_files(
    source_dir: Path,
    recursive: bool = False,
    cancel_event: Event | None = None,
) -> list[Path]:
    _raise_if_cancelled(cancel_event, "media scan cancelled")
    source_dir = source_dir.expanduser().resolve()
    _raise_if_cancelled(cancel_event, "media scan cancelled")
    if not source_dir.is_dir():
        return []
    result = sorted(
        _candidate_paths(source_dir, recursive, AUDIO_EXTENSIONS, cancel_event),
        key=lambda item: _relative_natural_key(item, source_dir, cancel_event),
    )
    _raise_if_cancelled(cancel_event, "media scan cancelled")
    return result


def scan_media(
    source_dir: Path,
    recursive: bool = False,
    ffprobe_path: Path | None = None,
    cancel_event: Event | None = None,
) -> tuple[list[MediaInfo], list[str]]:
    _raise_if_cancelled(cancel_event, "media scan cancelled")
    source_dir = source_dir.expanduser().resolve()
    _raise_if_cancelled(cancel_event, "media scan cancelled")
    if not source_dir.is_dir():
        raise ValueError("Thư mục media không tồn tại hoặc không thể truy cập.")

    paths = sorted(
        _candidate_paths(source_dir, recursive, SUPPORTED_EXTENSIONS, cancel_event),
        key=lambda item: _relative_natural_key(item, source_dir, cancel_event),
    )
    _raise_if_cancelled(cancel_event, "media scan cancelled")
    paths, duplicate_warnings = _remove_exact_duplicates(paths, cancel_event)
    media: list[MediaInfo] = []
    warnings: list[str] = list(duplicate_warnings)
    resolved_ffprobe = ffprobe_path

    for path in paths:
        _raise_if_cancelled(cancel_event, "media scan cancelled")
        relative_path = path.relative_to(source_dir).as_posix()
        if path.suffix.casefold() in IMAGE_EXTENSIONS:
            try:
                _raise_if_cancelled(cancel_event, "media scan cancelled")
                with Image.open(path) as image:
                    image.verify()
                _raise_if_cancelled(cancel_event, "media scan cancelled")
                with Image.open(path) as image:
                    width, height = image.size
                    format_name = image.format or path.suffix.lstrip(".").upper()
                _raise_if_cancelled(cancel_event, "media scan cancelled")
                if width < 2 or height < 2:
                    raise ValueError("kích thước ảnh không hợp lệ")
                media.append(
                    MediaInfo(
                        path=path.resolve(),
                        relative_path=relative_path,
                        width=width,
                        height=height,
                        format_name=format_name,
                        media_type="image",
                    )
                )
            except InterruptedError:
                raise
            except Exception as exc:
                warnings.append(f"Bỏ qua {path.name}: {exc}")
            continue

        try:
            _raise_if_cancelled(cancel_event, "media scan cancelled")
            if resolved_ffprobe is None:
                tools = find_tools()
                resolved_ffprobe = tools.ffprobe if tools else None
                _raise_if_cancelled(cancel_event, "media scan cancelled")
            if resolved_ffprobe is None:
                raise ValueError("cần FFmpeg/FFprobe để đọc video")
            info = probe_media_file(
                path,
                resolved_ffprobe,
                cancel_event=cancel_event,
            )
            media.append(
                MediaInfo(
                    path=path.resolve(),
                    relative_path=relative_path,
                    width=int(info["width"]),
                    height=int(info["height"]),
                    format_name=path.suffix.lstrip(".").upper(),
                    media_type="video",
                    duration_seconds=round(float(info["duration"]), 6),
                    fps=round(float(info["fps"]), 6),
                    codec_name=str(info["codec_name"]),
                    has_audio=bool(info["has_audio"]),
                )
            )
        except InterruptedError:
            raise
        except Exception as exc:
            warnings.append(f"Bỏ qua {path.name}: {exc}")

    _raise_if_cancelled(cancel_event, "media scan cancelled")
    return media, warnings


def scan_images(
    source_dir: Path,
    recursive: bool = False,
    ffprobe_path: Path | None = None,
    cancel_event: Event | None = None,
) -> tuple[list[MediaInfo], list[str]]:
    """Backward-compatible name; now returns both images and videos."""
    return scan_media(
        source_dir,
        recursive=recursive,
        ffprobe_path=ffprobe_path,
        cancel_event=cancel_event,
    )
