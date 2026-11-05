from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

from .storage import app_data_dir


FFMPEG_WINDOWS_ZIP = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
)
FFMPEG_WINDOWS_ZIPS = (
    FFMPEG_WINDOWS_ZIP,
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip",
)


class DownloadCancelled(Exception):
    pass


@dataclass(frozen=True)
class FFmpegTools:
    ffmpeg: Path
    ffprobe: Path


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _is_working(executable: Path, version_arg: str = "-version") -> bool:
    try:
        completed = subprocess.run(
            [str(executable), version_arg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
            creationflags=_creation_flags(),
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _bundled_roots() -> list[Path]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        roots.append(Path(bundle_dir))
    roots.append(Path(__file__).resolve().parents[1])
    return roots


def find_tools() -> FFmpegTools | None:
    override = os.environ.get("DOCURANDOM_FFMPEG")
    candidates: list[tuple[Path, Path]] = []
    if override:
        override_path = Path(override).expanduser()
        if override_path.is_dir():
            candidates.append(
                (override_path / "ffmpeg.exe", override_path / "ffprobe.exe")
            )
        else:
            candidates.append((override_path, override_path.with_name("ffprobe.exe")))

    for root in _bundled_roots():
        candidates.extend(
            [
                (root / "ffmpeg" / "ffmpeg.exe", root / "ffmpeg" / "ffprobe.exe"),
                (root / "tools" / "ffmpeg.exe", root / "tools" / "ffprobe.exe"),
            ]
        )

    cache = app_data_dir() / "tools"
    candidates.append((cache / "ffmpeg.exe", cache / "ffprobe.exe"))

    path_ffmpeg = shutil.which("ffmpeg")
    path_ffprobe = shutil.which("ffprobe")
    if path_ffmpeg and path_ffprobe:
        candidates.append((Path(path_ffmpeg), Path(path_ffprobe)))

    for ffmpeg, ffprobe in candidates:
        if ffmpeg.is_file() and ffprobe.is_file() and _is_working(ffmpeg):
            return FFmpegTools(ffmpeg.resolve(), ffprobe.resolve())
    return None


def ensure_tools(
    *,
    status_callback: Callable[[str], None] | None = None,
    download_callback: Callable[[int, int], None] | None = None,
    cancel_event: Event | None = None,
) -> FFmpegTools:
    existing = find_tools()
    if existing:
        return existing
    if os.name != "nt":
        raise RuntimeError(
            "Không tìm thấy FFmpeg. Hãy cài ffmpeg và ffprobe hoặc đặt biến "
            "DOCURANDOM_FFMPEG."
        )

    status = status_callback or (lambda _message: None)
    progress = download_callback or (lambda _current, _total: None)
    cancelled = cancel_event or Event()
    cache_dir = app_data_dir() / "tools"
    cache_dir.mkdir(parents=True, exist_ok=True)
    status("Đang tải FFmpeg lần đầu (khoảng 110 MB)…")

    download_errors: list[str] = []
    for source_index, source_url in enumerate(FFMPEG_WINDOWS_ZIPS, start=1):
        archive_path: Path | None = None
        try:
            request = urllib.request.Request(
                source_url,
                headers={"User-Agent": "DocuRandomStudio/1.0"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                total = int(response.headers.get("Content-Length", "0") or 0)
                with tempfile.NamedTemporaryFile(
                    suffix=".zip", prefix="docurandom_ffmpeg_", delete=False
                ) as archive:
                    archive_path = Path(archive.name)
                    current = 0
                    while True:
                        if cancelled.is_set():
                            raise DownloadCancelled()
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        archive.write(chunk)
                        current += len(chunk)
                        progress(current, total)

            if cancelled.is_set():
                raise DownloadCancelled()
            status("Đang giải nén FFmpeg…")
            assert archive_path is not None
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.namelist()
                for executable_name in ("ffmpeg.exe", "ffprobe.exe"):
                    suffix = f"/bin/{executable_name}"
                    matches = [
                        name for name in members if name.casefold().endswith(suffix)
                    ]
                    if not matches:
                        raise RuntimeError(f"Gói tải về thiếu {executable_name}.")
                    target = cache_dir / executable_name
                    temporary = target.with_suffix(".download")
                    with (
                        archive.open(matches[0]) as source,
                        temporary.open("wb") as dest,
                    ):
                        shutil.copyfileobj(source, dest)
                    os.replace(temporary, target)
            break
        except DownloadCancelled:
            raise RuntimeError("Đã hủy tải FFmpeg.") from None
        except Exception as exc:
            download_errors.append(f"nguồn {source_index}: {exc}")
            if source_index < len(FFMPEG_WINDOWS_ZIPS):
                status("Nguồn FFmpeg chính chưa phản hồi, đang đổi nguồn dự phòng…")
        finally:
            if archive_path:
                try:
                    archive_path.unlink(missing_ok=True)
                except OSError:
                    pass
    else:
        raise RuntimeError(
            "Không thể tải FFmpeg tự động từ các nguồn dự phòng. "
            "Kiểm tra kết nối mạng rồi thử lại. Chi tiết: "
            + " | ".join(download_errors)
        )

    tools = find_tools()
    if not tools:
        raise RuntimeError("FFmpeg đã tải nhưng không thể khởi chạy.")
    status("FFmpeg đã sẵn sàng.")
    return tools
