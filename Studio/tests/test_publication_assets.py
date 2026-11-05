import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from docurandom.content_ai import ContentAIError
from docurandom.publication_assets import (publication_sources, ensure_publication_assets,
                                         commit_publication_thumbnail)
from docurandom.workspace import Workspace
from docurandom.youtube_upload import YouTubeUploadError, YouTubeStudioUploader


def setup_video(tmp_path):
    w = Workspace("id", "Test", str(tmp_path / "workspace"), "", "")
    w.ensure_layout()
    archive = w.uploaded_dir / "B"
    (archive / "Output").mkdir(parents=True)
    package = archive / "Output" / "B.txt"
    package.write_text("TIÊU ĐỀ\nVideo B\n\nMÔ TẢ\nDescription B\n\nSEO TAGS\ntag B, documentary\n\n"
                       "PROMPT THUMBNAIL\nPROMPT THUMBNAIL HOÀN CHỈNH\nCreate a CNC thumbnail.\n",
                       encoding="utf-8")
    item = {"identity": "B", "youtube_url": "https://youtu.be/B1234567890",
            "archive_dir": str(archive), "package_path": str(package),
            "video_path": str(archive / "Output/B.mp4"), "thumbnail_path": ""}
    (w.state_dir / "deferred_public_queue.json").write_text(json.dumps({"items": [item]}))
    status = SimpleNamespace(operational=True, endpoint="http://127.0.0.1:1", access_token="token")
    pool = Mock(); pool.acquire.return_value = True
    return w, item, status, pool


def image_bytes():
    data = io.BytesIO(); Image.new("RGB", (1280, 720), "blue").save(data, "PNG")
    return data.getvalue()


def test_missing_or_corrupt_image_cannot_make_public_sources(tmp_path):
    w, item, *_ = setup_video(tmp_path)
    with pytest.raises(YouTubeUploadError, match="thumbnail"):
        publication_sources(item)
    corrupt = tmp_path / "corrupt.png"; corrupt.write_bytes(b"invalid")
    with pytest.raises(YouTubeUploadError, match="thumbnail"):
        publication_sources(dict(item, thumbnail_path=str(corrupt)))


def test_missing_metadata_does_not_submit_image(tmp_path):
    w, item, status, pool = setup_video(tmp_path)
    package = Path(item['package_path'])
    package.write_text("TIÊU ĐỀ\nB\n\nMÔ TẢ\nDescription\n", encoding="utf-8")
    with patch("docurandom.publication_assets.ChatGPTWebChat") as chat:
        with pytest.raises(YouTubeUploadError):
            ensure_publication_assets(w, item, status, pool, threading.Event(), Mock())
        chat.assert_not_called()


def test_repair_archived_b_does_not_touch_active_c_and_follows_web_model(tmp_path):
    w, item, status, pool = setup_video(tmp_path)
    current = w.state_dir / "thumbnail-ai.json"; current.write_text('{"belongs":"C"}')
    chat = Mock(thread_id="fresh", response_id="response")
    chat.generate_image.return_value = SimpleNamespace(data=image_bytes())
    with patch("docurandom.publication_assets.ChatGPTWebChat", return_value=chat) as factory:
        updated, sources = ensure_publication_assets(w, item, status, pool, threading.Event(), Mock())
    assert factory.call_args.args[1] == "chatgpt-web/auto"
    assert current.read_text() == '{"belongs":"C"}'
    assert sources.thumbnail_path.is_relative_to(Path(item['archive_dir']))
    assert updated['youtube_url'] == item['youtube_url']
    chat.generate_image.assert_called_once(); pool.release.assert_called_once()
    with patch("docurandom.publication_assets.ChatGPTWebChat") as factory:
        ensure_publication_assets(w, updated, status, pool, threading.Event(), Mock())
        factory.assert_not_called()


def test_transport_loss_survives_restart_and_recovers_without_new_submission(tmp_path):
    w, item, status, pool = setup_video(tmp_path)
    chat = Mock(thread_id="exact", response_id="response")
    chat.generate_image.side_effect = ContentAIError("transport lost")
    with patch("docurandom.publication_assets.ChatGPTWebChat", return_value=chat):
        with pytest.raises(ContentAIError):
            ensure_publication_assets(w, item, status, pool, threading.Event(), Mock())
    path = Path(item['archive_dir']) / 'State/publication-thumbnail-repair.json'
    state = json.loads(path.read_text()); state.pop('next_retry_at'); path.write_text(json.dumps(state))
    chat = Mock(thread_id="exact", response_id="response")
    chat.parse_generated_image_response.return_value = SimpleNamespace(data=image_bytes())
    with patch("docurandom.publication_assets.ChatGPTWebChat", return_value=chat) as factory:
        ensure_publication_assets(w, item, status, pool, threading.Event(), Mock())
    assert factory.call_args.kwargs['thread_id'] == 'exact'
    chat.recover_pending.assert_called_once(); chat.generate_image.assert_not_called()


def test_removed_video_is_not_resurrected_in_public_queue(tmp_path):
    w, item, *_ = setup_video(tmp_path)
    (w.state_dir / 'deferred_public_queue.json').write_text('{"items":[]}')
    with pytest.raises(YouTubeUploadError, match="rời hàng đợi"):
        commit_publication_thumbnail(w, item, tmp_path / "image.png")
    assert json.loads((w.state_dir / 'deferred_public_queue.json').read_text())['items'] == []


def test_manual_retry_image_is_collected_before_cooldown_without_resending(tmp_path):
    w, item, status, pool = setup_video(tmp_path)
    import hashlib
    checkpoint = Path(item['archive_dir']) / 'State/publication-thumbnail-repair.json'
    checkpoint.parent.mkdir()
    checkpoint.write_text(json.dumps({'identity':'B', 'package_sha256':hashlib.sha256(Path(item['package_path']).read_bytes()).hexdigest(),
        'status':'terminal_no_image','submitted':False,'thread_id':'exact',
        'next_retry_at':'2099-01-01T00:00:00+00:00'}))
    current = w.state_dir/'thumbnail-ai.json'; current.write_text('{"belongs":"C"}')
    chat = Mock(thread_id='exact', response_id='')
    chat.read_existing_generated_image.return_value = SimpleNamespace(data=image_bytes())
    with patch('docurandom.publication_assets.ChatGPTWebChat', return_value=chat):
        updated, sources = ensure_publication_assets(w,item,status,pool,threading.Event(),Mock())
    assert sources.thumbnail_path.is_file() and updated['youtube_url']==item['youtube_url']
    assert current.read_text()=='{"belongs":"C"}'
    chat.generate_image.assert_not_called()
    assert json.loads(checkpoint.read_text())['status']=='complete'


def test_manual_retry_still_running_does_not_start_another_image(tmp_path):
    w, item, status, pool = setup_video(tmp_path)
    import hashlib
    checkpoint = Path(item['archive_dir']) / 'State/publication-thumbnail-repair.json'
    checkpoint.parent.mkdir()
    checkpoint.write_text(json.dumps({'identity':'B', 'package_sha256':hashlib.sha256(Path(item['package_path']).read_bytes()).hexdigest(),
        'status':'terminal_no_image','submitted':False,'thread_id':'exact'}))
    chat = Mock(); chat.read_existing_generated_image.side_effect = ContentAIError('still generating')
    with patch('docurandom.publication_assets.ChatGPTWebChat',return_value=chat), pytest.raises(ContentAIError):
        ensure_publication_assets(w,item,status,pool,threading.Event(),Mock())
    chat.generate_image.assert_not_called()


def test_public_promotion_without_assets_does_not_open_profile():
    uploader = object.__new__(YouTubeStudioUploader)
    uploader._assert_public_video_not_superseded = Mock()
    with patch('docurandom.youtube_upload.DonutMcpClient') as client:
        with pytest.raises(YouTubeUploadError, match='Thiếu nguyên liệu'):
            uploader.promote_deferred_public(endpoint=Mock(), profile_id='p', youtube_url='https://youtu.be/B1234567890')
        client.assert_not_called()
