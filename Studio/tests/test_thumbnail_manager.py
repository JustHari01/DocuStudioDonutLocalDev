from __future__ import annotations

from datetime import datetime, timezone
import io
from pathlib import Path

from PIL import Image
import pytest

from docurandom.thumbnail_manager import (
    ThumbnailError,
    clear_workspace_thumbnail,
    inspect_thumbnail_bytes,
    install_workspace_thumbnail,
    is_managed_thumbnail,
    thumbnail_preview_bytes,
)
from docurandom.workspace import Workspace


def _workspace(root: Path) -> Workspace:
    now = datetime.now(timezone.utc).isoformat()
    workspace = Workspace("telegram-thumb", "Telegram Thumb", str(root), now, now)
    workspace.ensure_layout()
    return workspace


def _image_bytes(color: tuple[int, int, int], *, image_format: str = "PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (1280, 720), color).save(output, format=image_format)
    return output.getvalue()


def test_thumbnail_bytes_are_verified_and_identified() -> None:
    result = inspect_thumbnail_bytes(_image_bytes((20, 40, 60)))

    assert result.extension == ".png"
    assert (result.width, result.height) == (1280, 720)
    assert len(result.sha256) == 64

    with pytest.raises(ThumbnailError):
        inspect_thumbnail_bytes(b"not-an-image")


def test_replacing_managed_thumbnail_archives_previous_file(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    first = install_workspace_thumbnail(workspace, _image_bytes((255, 0, 0)))
    first_bytes = first.path.read_bytes()
    second = install_workspace_thumbnail(workspace, _image_bytes((0, 255, 0), image_format="JPEG"))

    assert second.path.suffix == ".jpg"
    assert second.path.is_file()
    assert len(second.archived_paths) == 1
    assert second.archived_paths[0].read_bytes() == first_bytes
    assert is_managed_thumbnail(workspace, second.path)


def test_clear_never_deletes_external_user_image(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    external = tmp_path / "outside.png"
    external.write_bytes(_image_bytes((1, 2, 3)))

    archived = clear_workspace_thumbnail(workspace, external)

    assert archived == ()
    assert external.is_file()


def test_clear_moves_managed_thumbnail_into_history(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    installed = install_workspace_thumbnail(workspace, _image_bytes((1, 2, 3)))

    archived = clear_workspace_thumbnail(workspace, installed.path)

    assert not installed.path.exists()
    assert len(archived) == 1
    assert archived[0].is_file()


def test_legacy_telegram_thumbnail_is_still_managed_and_archived(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    legacy = workspace.state_dir / "telegram-thumbnails" / "current.png"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(_image_bytes((9, 8, 7)))

    assert is_managed_thumbnail(workspace, legacy)
    archived = clear_workspace_thumbnail(workspace, legacy)

    assert not legacy.exists()
    assert len(archived) == 1
    assert archived[0].is_file()


def test_preview_is_a_compact_valid_jpeg(tmp_path: Path) -> None:
    source = tmp_path / "wide.png"
    source.write_bytes(_image_bytes((11, 22, 33)))

    preview = thumbnail_preview_bytes(source, max_edge=640)

    with Image.open(io.BytesIO(preview)) as image:
        assert image.format == "JPEG"
        assert image.size == (640, 360)
