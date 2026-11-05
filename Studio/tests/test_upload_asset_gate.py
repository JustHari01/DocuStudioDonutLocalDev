from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from PIL import Image
from docurandom.workspace import Workspace
from docurandom.donut_mcp import DonutMcpEndpoint
from docurandom.youtube_upload import (
    WorkspaceUploadSources, YouTubeMetadata, YouTubeStudioUploader,
    YouTubeUploadRequest, YouTubeUploadError, _assert_complete_upload_assets,
)


class UploadAssetGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        now = datetime.now(timezone.utc).isoformat()
        self.workspace = Workspace('gate-test', 'Gate', str(self.root), now, now)
        self.workspace.ensure_layout()
        self.video = self.workspace.output_dir / 'Video.mp4'
        self.video.write_bytes(b'completed video')
        self.package = self.workspace.output_dir / 'Package.txt'
        self.package.write_text('TIÊU ĐỀ\nTitle\nMÔ TẢ\nDescription\nSEO TAGS\ntag\n', encoding='utf-8')
        self.thumbnail = self.workspace.output_dir / 'Thumbnail.png'
        Image.new('RGB', (1280, 720), 'blue').save(self.thumbnail)
        self.sources = WorkspaceUploadSources(self.video, self.package, self.thumbnail,
                                               YouTubeMetadata('Title', 'Description', tags=('tag',)))

    def test_complete_assets_allowed(self):
        _assert_complete_upload_assets(self.sources)

    def test_missing_or_corrupt_thumbnail_and_metadata_stop_before_browser(self):
        bad = self.root / 'corrupt.png'
        bad.write_bytes(b'not an image')
        small = self.root / 'small.png'
        Image.new('RGB', (64, 64)).save(small)
        failures = [replace(self.sources, thumbnail_path=None),
                    replace(self.sources, thumbnail_path=bad),
                    replace(self.sources, thumbnail_path=small),
                    replace(self.sources, metadata=YouTubeMetadata('', 'Description', tags=('tag',))),
                    replace(self.sources, metadata=YouTubeMetadata('Title', '', tags=('tag',))),
                    replace(self.sources, metadata=YouTubeMetadata('Title', 'Description'))]
        for sources in failures:
            with self.subTest(sources=sources):
                uploader = YouTubeStudioUploader(registry_path=self.root/'registry.json')
                request = YouTubeUploadRequest(workspace=self.workspace, profile_id='profile',
                                               endpoint=DonutMcpEndpoint('http://127.0.0.1:1/mcp', 'http://127.0.0.1:1', 'test'),
                                               video_path=self.video)
                with (mock.patch('docurandom.youtube_upload.resolve_workspace_upload_sources', return_value=sources),
                      mock.patch('docurandom.youtube_upload._wait_for_render_upload_ready'),
                      mock.patch.object(uploader.coordinator, 'acquire') as acquire,
                      mock.patch.object(uploader, '_checkpoint') as checkpoint,
                      self.assertRaises(YouTubeUploadError)):
                    uploader.upload(request)
                acquire.assert_not_called()
                checkpoint.assert_not_called()

    def test_deleted_thumbnail_is_rejected_when_rechecked(self):
        _assert_complete_upload_assets(self.sources)
        self.thumbnail.unlink()
        with self.assertRaises(YouTubeUploadError):
            _assert_complete_upload_assets(self.sources)
