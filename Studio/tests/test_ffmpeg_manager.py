from __future__ import annotations

import io
import urllib.error
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from docurandom import ffmpeg_manager


class _ArchiveResponse(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self) -> _ArchiveResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _ffmpeg_archive() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("ffmpeg-build/bin/ffmpeg.exe", b"ffmpeg")
        archive.writestr("ffmpeg-build/bin/ffprobe.exe", b"ffprobe")
    return payload.getvalue()


def test_ensure_tools_uses_backup_source_when_primary_fails() -> None:
    with TemporaryDirectory() as temporary_directory:
        app_root = Path(temporary_directory)
        expected = ffmpeg_manager.FFmpegTools(
            app_root / "tools" / "ffmpeg.exe",
            app_root / "tools" / "ffprobe.exe",
        )
        with (
            mock.patch.object(ffmpeg_manager, "app_data_dir", return_value=app_root),
            mock.patch.object(
                ffmpeg_manager,
                "find_tools",
                side_effect=[None, expected],
            ),
            mock.patch.object(
                ffmpeg_manager.urllib.request,
                "urlopen",
                side_effect=[
                    urllib.error.URLError("primary unavailable"),
                    _ArchiveResponse(_ffmpeg_archive()),
                ],
            ) as urlopen,
        ):
            result = ffmpeg_manager.ensure_tools()

        assert result == expected
        assert expected.ffmpeg.read_bytes() == b"ffmpeg"
        assert expected.ffprobe.read_bytes() == b"ffprobe"
        assert urlopen.call_count == 2
        requested_urls = [call.args[0].full_url for call in urlopen.call_args_list]
        assert requested_urls == list(ffmpeg_manager.FFMPEG_WINDOWS_ZIPS)
