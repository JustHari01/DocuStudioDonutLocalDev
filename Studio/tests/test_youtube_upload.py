from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from PIL import Image

from docurandom.browser_extension_bridge import BrowserExtensionBridgeError
from docurandom.youtube_upload import (
    _CdpSession,
    _SemanticCdpController,
    _YouTubeDraftCandidate,
    _cdp_page_target_id,
    _ensure_youtube_browser_bridge,
    _mcp_close_cdp_page_target,
    _mcp_open_background_cdp_page_ws_url,
    _mcp_open_cdp_page_ws_url,
    _mcp_open_minimized_cdp_window_ws_url,
    _mcp_cdp_page_ws_url,
    _prepared_youtube_upload_tab,
    _youtube_server_submission_matches,
    _youtube_thumbnail_fallback,
    _youtube_video_id,
    ProfileUploadCoordinator,
    YouTubeMetadata,
    YouTubeStudioUploader,
    YouTubeUploadRequest,
    YouTubeUploadResult,
    YouTubeUploadCancelled,
    YouTubeUploadError,
    YouTubeUploadUncertain,
    YouTubeVisibilityPromotionResult,
    WorkspaceUploadSources,
    _apply_upload_visibility_cutoff,
    _post_render_upload_not_before,
    _wait_for_render_upload_ready,
    find_workspace_content_package,
    parse_content_package,
    parse_thumbnail_prompt,
    resolve_workspace_upload_sources,
)
from docurandom.donut_mcp import DonutMcpEndpoint, DonutProfile
from docurandom.workspace import Workspace


def _workspace(root: Path) -> Workspace:
    now = datetime.now(timezone.utc).isoformat()
    workspace = Workspace("workspace-1", "Demo", str(root), now, now)
    workspace.ensure_layout()
    return workspace


class TransferFeedbackRegressionTests(unittest.TestCase):
    def test_content_row_without_progress_never_proves_file_complete(self):
        controller = mock.Mock()
        controller.cdp = None
        controller.youtube_url.return_value = 'https://youtu.be/Exact123'
        controller.youtube_submission_complete.return_value = False
        controller.youtube_upload_progress.return_value = (None, '')
        controller.server_upload_progress.return_value = (None, '')
        reports = mock.Mock()
        url, complete = YouTubeStudioUploader._wait_for_upload_transfer_complete(
            controller, reports, threading.Event(), timeout=.04, poll_interval=.001,
            draft_content_probe=lambda: 'https://youtu.be/Exact123',
            draft_probe_interval=.005, draft_handoff_after=.001,
        )
        self.assertEqual(url, 'https://youtu.be/Exact123')
        self.assertFalse(complete)
        controller.server_upload_progress.assert_called_with('Exact123')
        self.assertFalse(any('đóng luồng upload cũ' in str(c) for c in reports.call_args_list))

    def test_content_row_cannot_close_a_still_transferring_original_tab(self):
        class Controller:
            def youtube_url(self): return 'https://youtu.be/smvUqEuMkcQ'
            def youtube_submission_complete(self): return False
            def youtube_upload_progress(self): return 14.0,''
            def server_upload_progress(self,_video_id): return self.feedback,''
        for percent in (14.0,None):
            with self.subTest(percent=percent):
                controller=Controller(); controller.feedback=percent
                url,complete=YouTubeStudioUploader._wait_for_upload_transfer_complete(
                    controller,mock.Mock(),threading.Event(),timeout=.04,poll_interval=.001,
                    draft_content_probe=lambda:'https://youtu.be/smvUqEuMkcQ',draft_probe_interval=.005,draft_handoff_after=.001,
                )
                self.assertEqual(url,'https://youtu.be/smvUqEuMkcQ')
                self.assertFalse(complete)

    def test_exact_server_feedback_overrides_old_completion_flag_before_any_save(self):
        uploader = YouTubeStudioUploader(registry_path=Path('unused-registry.json'))
        request = mock.Mock()
        sources = mock.Mock()
        controller = mock.Mock()
        controller.server_upload_progress.return_value = (14.7408, '24 phút')
        record = {'attempt_id': 'same-attempt', 'youtube_url': 'https://youtu.be/smvUqEuMkcQ', 'transfer_complete': True, 'bridge_tab_id': 71, 'target_id': 'same-target'}
        with (
            mock.patch.object(uploader, '_request_identity', return_value='same-identity'),
            mock.patch.object(uploader, '_registry_get', return_value=record),
            mock.patch.object(uploader, '_registry_update_attempt') as update,
            mock.patch.object(uploader, '_checkpoint') as checkpoint,
            self.assertRaisesRegex(YouTubeUploadUncertain, '15%'),
        ):
            uploader._persist_and_verify_saved_metadata(request,sources,controller,'https://youtu.be/smvUqEuMkcQ',mock.Mock(),threading.Event())
        self.assertEqual(update.call_args.args[:2], ('same-identity','same-attempt'))
        self.assertFalse(update.call_args.args[2]['transfer_complete'])
        self.assertTrue(update.call_args.args[2]['transfer_requires_content_resume'])
        self.assertEqual(checkpoint.call_args.kwargs['extra']['target_id'], 'same-target')
        self.assertEqual(checkpoint.call_args.kwargs['extra']['bridge_tab_id'], 71)
        controller.navigate_and_wait.assert_not_called()
        controller.set_file.assert_not_called()
        controller.set_named_text.assert_not_called()
        controller.press.assert_not_called()

    def test_missing_feedback_does_not_clear_known_partial_transfer_or_save(self):
        uploader = YouTubeStudioUploader(registry_path=Path('unused-registry.json'))
        controller = mock.Mock()
        controller.server_upload_progress.return_value = (None, '')
        record = {
            'attempt_id': 'same-attempt', 'youtube_url': 'https://youtu.be/smvUqEuMkcQ',
            'transfer_complete': False, 'transfer_requires_content_resume': True,
            'transfer_feedback_percent': 14.7408,
        }
        with (
            mock.patch.object(uploader, '_request_identity', return_value='same-identity'),
            mock.patch.object(uploader, '_registry_get', return_value=record),
            mock.patch.object(uploader, '_registry_update_attempt') as update,
            self.assertRaisesRegex(YouTubeUploadUncertain, 'chưa trả tiến độ mới'),
        ):
            uploader._persist_and_verify_saved_metadata(
                mock.Mock(), mock.Mock(), controller, record['youtube_url'],
                mock.Mock(), threading.Event(),
            )
        update.assert_not_called()
        controller.navigate_and_wait.assert_not_called()
        controller.set_named_text.assert_not_called()
        controller.set_file.assert_not_called()
        controller.press.assert_not_called()

    def test_exact_complete_feedback_clears_partial_guard_before_metadata_edit(self):
        uploader = YouTubeStudioUploader(registry_path=Path('unused-registry.json'))
        controller = mock.Mock()
        controller.server_upload_progress.return_value = (100.0, '')
        record = {
            'attempt_id': 'same-attempt', 'youtube_url': 'https://youtu.be/smvUqEuMkcQ',
            'transfer_complete': False, 'transfer_requires_content_resume': True,
        }
        class MetadataEditReached(Exception): pass
        controller.navigate_and_wait.side_effect = MetadataEditReached
        sources = mock.Mock()
        sources.metadata.tags = ()
        with (
            mock.patch.object(uploader, '_request_identity', return_value='same-identity'),
            mock.patch.object(uploader, '_registry_get', return_value=record),
            mock.patch.object(uploader, '_registry_update_attempt') as update,
            self.assertRaises(MetadataEditReached),
        ):
            uploader._persist_and_verify_saved_metadata(
                mock.Mock(), sources, controller, record['youtube_url'],
                mock.Mock(), threading.Event(),
            )
        self.assertEqual(update.call_args.args[:2], ('same-identity', 'same-attempt'))
        self.assertTrue(update.call_args.args[2]['transfer_complete'])
        self.assertFalse(update.call_args.args[2]['transfer_requires_content_resume'])
        controller.set_file.assert_not_called()

    def test_localized_row_at_fourteen_percent_is_never_ready(self):
        for row in ('Đã tải lên 14%. Còn 24 phút. Nháp', '14% uploaded. Edit draft', 'アップロード済み 14%'):
            with self.subTest(row=row):
                active,detail=YouTubeStudioUploader._content_row_upload_status(row)
                self.assertTrue(active)
                self.assertIn('14%',detail)
        self.assertFalse(YouTubeStudioUploader._content_row_upload_status('Đã tải lên 100%. Đang xử lý')[0])

    def test_server_feedback_is_bound_to_exact_video_and_discards_other_ids(self):
        feedback={'continuationContents':[
            {'uploadFeedbackItemContinuation':{'id':{'videoId':'Foreign123'},'contents':[{'transferProgressBar':{'fractionCompleted':1}}]}},
            {'uploadFeedbackItemContinuation':{'id':{'videoId':'smvUqEuMkcQ'},'contents':[{'transferProgressBar':{'fractionCompleted':.1474,'remainingTimeMessage':{'simpleText':'24 phút'}}}]}},
        ]}
        session=_CdpSession('ws://127.0.0.1/test')
        session._ws=mock.Mock()
        session._ws.recv.side_effect=[
            json.dumps({'method':'Network.responseReceived','params':{'requestId':'r1','response':{'url':'https://studio.youtube.com/youtubei/v1/upload/feedback','status':200}}}),
            json.dumps({'method':'Network.loadingFinished','params':{'requestId':'r1'}}),
        ]
        with mock.patch.object(session,'command',side_effect=[{}, {'body':json.dumps(feedback)}, {}]) as command:
            percent,remaining=session.upload_feedback('smvUqEuMkcQ')
        self.assertAlmostEqual(percent,14.74)
        self.assertEqual(remaining,'24 phút')
        command.assert_has_calls([mock.call('Network.enable'),mock.call('Network.getResponseBody',{'requestId':'r1'}),mock.call('Network.disable')])


class YouTubePackageTests(unittest.TestCase):
    def test_post_render_wait_crossing_automatic_cutoff_changes_public_to_unlisted(self) -> None:
        cutoff = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
        request = YouTubeUploadRequest(
            workspace=mock.Mock(),
            endpoint=mock.Mock(),
            profile_id="profile-test",
            privacy="PUBLIC",
            automatic_public_cutoff_at=cutoff,
        )

        before_cutoff = _apply_upload_visibility_cutoff(
            request,
            current=cutoff - timedelta(seconds=1),
        )
        after_cutoff = _apply_upload_visibility_cutoff(
            request,
            current=cutoff + timedelta(seconds=1),
        )

        self.assertFalse(before_cutoff.hold_public_until_next_morning)
        self.assertTrue(after_cutoff.hold_public_until_next_morning)
        self.assertEqual(YouTubeStudioUploader._effective_privacy(after_cutoff), "UNLISTED")

    def test_public_visibility_is_held_as_unlisted_for_late_schedule(self) -> None:
        request = YouTubeUploadRequest(
            workspace=mock.Mock(),
            endpoint=mock.Mock(),
            profile_id="profile-1",
            privacy="PUBLIC",
            hold_public_until_next_morning=True,
        )

        self.assertEqual(YouTubeStudioUploader._effective_privacy(request), "UNLISTED")
        self.assertEqual(YouTubeStudioUploader._completion_status(request), "deferred_public")

        normal_request = YouTubeUploadRequest(
            workspace=mock.Mock(),
            endpoint=mock.Mock(),
            profile_id="profile-1",
            privacy="PUBLIC",
        )
        self.assertEqual(YouTubeStudioUploader._effective_privacy(normal_request), "PUBLIC")
        self.assertEqual(YouTubeStudioUploader._completion_status(normal_request), "complete")

    def test_draft_handoff_grace_scales_for_long_videos(self) -> None:
        self.assertEqual(
            YouTubeStudioUploader._draft_handoff_after_seconds(None),
            10 * 60,
        )
        self.assertEqual(
            YouTubeStudioUploader._draft_handoff_after_seconds(29 * 60),
            10 * 60,
        )
        self.assertEqual(
            YouTubeStudioUploader._draft_handoff_after_seconds(30 * 60),
            15 * 60,
        )
        self.assertEqual(
            YouTubeStudioUploader._draft_handoff_after_seconds(61 * 60),
            20 * 60,
        )

    def test_content_row_progress_is_reported_and_not_treated_as_ready(self) -> None:
        active, detail = YouTubeStudioUploader._content_row_upload_status(
            "Uploading 43% · Pending · 28 seconds left · Edit draft"
        )
        self.assertTrue(active)
        self.assertIn("43%", detail)
        self.assertIn("28 giây", detail)

        active, detail = YouTubeStudioUploader._content_row_upload_status(
            "Private · Uploaded"
        )
        self.assertFalse(active)
        self.assertEqual(detail, "đã sẵn sàng")

    def test_visibility_promotion_never_attaches_a_file(self) -> None:
        uploader = object.__new__(YouTubeStudioUploader)
        uploader.coordinator = mock.Mock()
        uploader._registry_get = mock.Mock(return_value={})
        uploader.coordinator.acquire.return_value = mock.MagicMock()
        endpoint = DonutMcpEndpoint(
            "http://127.0.0.1:17841/mcp/abcdefghijklmnop",
            "http://127.0.0.1:17841",
            "…mnop",
        )
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.get_profile.return_value = DonutProfile(
            "profile-1",
            "Profile 1",
            "wayfern",
            process_id=123,
        )
        client.get_debug_connection.return_value = {
            "targets_url": "http://127.0.0.1:9222/json",
        }
        controller = mock.MagicMock(spec=_SemanticCdpController)
        controller.wait_dom.return_value = {"backendDOMNodeId": 1}
        controller._dom_attributes.return_value = {}
        bridge = mock.Mock()

        with (
            mock.patch("docurandom.youtube_upload.DonutMcpClient", return_value=client),
            mock.patch(
                "docurandom.youtube_upload._mcp_open_minimized_cdp_window_ws_url",
                return_value="ws://127.0.0.1:9222/devtools/page/target-1",
            ),
            mock.patch(
                "docurandom.youtube_upload._cdp_page_target_id",
                return_value="target-1",
            ),
            mock.patch(
                "docurandom.youtube_upload._mcp_set_cdp_window_state"
            ),
            mock.patch(
                "docurandom.youtube_upload._ensure_youtube_browser_bridge",
                return_value=bridge,
            ),
            mock.patch(
                "docurandom.youtube_upload._browser_extension_tab_for_target",
                return_value=(bridge, 77),
            ),
            mock.patch("docurandom.youtube_upload._CdpSession") as cdp_session,
            mock.patch(
                "docurandom.youtube_upload._SemanticCdpController",
                return_value=controller,
            ),
            mock.patch.object(
                uploader,
                "_prepare_exact_draft_edit_target",
            ) as prepare_exact,
            mock.patch.object(uploader, "_set_edit_visibility") as set_visibility,
            mock.patch.object(
                uploader,
                "_verify_edit_visibility",
                side_effect=[False, True],
            ),
            mock.patch.object(uploader, "_persist_and_verify_saved_metadata") as verify_metadata,
            mock.patch("docurandom.publication_assets.valid_thumbnail", return_value=True),
            mock.patch.object(uploader, "_registry_update_attempt") as registry_update,
            mock.patch.object(uploader, "_assert_public_video_not_superseded"),
            mock.patch.object(
                YouTubeStudioUploader,
                "_wait_edit_save_complete",
            ),
            mock.patch("docurandom.youtube_upload._mcp_close_cdp_page_target"),
        ):
            cdp_session.return_value.__enter__.return_value = mock.Mock()
            result = uploader.promote_deferred_public(
                endpoint=endpoint,
                profile_id="profile-1",
                youtube_url="https://youtu.be/Queue123",
                identity="i" * 64,
                attempt_id="attempt-1",
                report=mock.Mock(),
                cancel_event=threading.Event(),
                publication_sources=WorkspaceUploadSources(Path("video.mp4"), Path("package.txt"),
                    Path("image.png"), YouTubeMetadata("Title", "Description", tags=("tag",))),
                publication_workspace=mock.Mock(),
            )

        self.assertIsInstance(result, YouTubeVisibilityPromotionResult)
        self.assertEqual(result.youtube_url, "https://youtu.be/Queue123")
        prepare_exact.assert_called_once()
        verify_metadata.assert_called_once()
        set_visibility.assert_called_once()
        self.assertEqual(set_visibility.call_args.args[1], "PUBLIC")
        controller.set_file.assert_not_called()
        registry_update.assert_called_once()

    def test_content_row_pending_distinguishes_uploading_from_post_upload_checks(
        self,
    ) -> None:
        active, detail = YouTubeStudioUploader._content_row_upload_status(
            "Processing delayed · Checks starting soon · Pending · Edit draft"
        )
        self.assertFalse(active)
        self.assertIn("đã upload xong", detail)

        active, detail = YouTubeStudioUploader._content_row_upload_status(
            "Draft · Edit draft"
        )
        self.assertFalse(active)
        self.assertEqual(detail, "đã sẵn sàng")

    def test_content_row_continue_upload_is_not_a_completed_draft(self) -> None:
        active, detail = YouTubeStudioUploader._content_row_upload_status(
            "Bản nháp · Pending · Tiếp tục tải lên · Chỉnh sửa bản nháp"
        )
        self.assertTrue(active)
        self.assertEqual(detail, "cần tiếp tục tải tệp")

    def test_thumbnail_fallback_preserves_original_and_fits_youtube(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubeThumb_") as temp:
            workspace = _workspace(Path(temp))
            source = Path(temp) / "selected.png"
            Image.new("RGB", (1672, 941), (20, 40, 80)).save(source, format="PNG")
            original = source.read_bytes()

            fallback = _youtube_thumbnail_fallback(workspace, source)

            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(fallback.suffix.casefold(), ".jpg")
            self.assertLessEqual(fallback.stat().st_size, 1_900_000)
            with Image.open(fallback) as image:
                self.assertLessEqual(image.width, 1280)
                self.assertLessEqual(image.height, 720)

    def test_server_submission_proof_requires_exact_metadata_and_unlisted_state(self) -> None:
        metadata = YouTubeMetadata(
            title="Tiêu đề kiểm thử",
            description="Mô tả kiểm thử",
            tags=("tag một", "tag hai"),
        )
        html = json.dumps(
            {
                "videoDetails": {
                    "title": metadata.title,
                    "shortDescription": metadata.description,
                    "keywords": list(metadata.tags),
                },
                "microformat": {"isUnlisted": True},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        with mock.patch(
            "docurandom.youtube_upload.urllib.request.urlopen",
            side_effect=lambda *_args, **_kwargs: io.BytesIO(html),
        ):
            self.assertTrue(
                _youtube_server_submission_matches(
                    "https://youtu.be/abcDEF_123",
                    metadata,
                    "UNLISTED",
                )
            )
            self.assertFalse(
                _youtube_server_submission_matches(
                    "https://youtu.be/abcDEF_123",
                    metadata,
                    "PUBLIC",
                )
            )

    def test_server_submission_proof_rejects_wrong_tags(self) -> None:
        metadata = YouTubeMetadata("Title", "Description", tags=("expected",))
        html = (
            '{"title":"Title","shortDescription":"Description",'
            '"keywords":["wrong"],"isUnlisted":true}'
        ).encode("utf-8")
        with mock.patch(
            "docurandom.youtube_upload.urllib.request.urlopen",
            return_value=io.BytesIO(html),
        ):
            self.assertFalse(
                _youtube_server_submission_matches(
                    "https://www.youtube.com/watch?v=abcDEF_123",
                    metadata,
                    "UNLISTED",
                )
            )

    def test_youtube_video_id_accepts_confirmation_links_only(self) -> None:
        self.assertEqual(_youtube_video_id("https://youtu.be/abcDEF_123"), "abcDEF_123")
        self.assertEqual(
            _youtube_video_id("https://www.youtube.com/watch?v=abcDEF_123&feature=share"),
            "abcDEF_123",
        )
        self.assertEqual(_youtube_video_id("https://example.com/watch?v=abcDEF_123"), "")

    def test_checklist_after_title_is_not_counted_as_title(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            package = Path(temp) / "video.txt"
            package.write_text(
                """TIÊU ĐỀ

Một tiêu đề ngắn hợp lệ

CHECKLIST NỘI DUNG ĐÃ LÀM (CHỐNG TRÙNG)

- [x] Một tiêu đề cũ rất dài nhưng không phải tiêu đề hiện tại
- [x] Một tiêu đề khác trong sổ chống trùng

MÔ TẢ

Mô tả hợp lệ.
""",
                encoding="utf-8-sig",
            )

            metadata = parse_content_package(package)

            self.assertEqual(metadata.title, "Một tiêu đề ngắn hợp lệ")

    def test_parses_title_description_hashtags_and_seo_tags(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            package = Path(temp) / "demo.txt"
            package.write_text(
                """TIÊU ĐỀ

Nhật Bản thử nghiệm dây chuyền mới

MÔ TẢ

Nội dung mô tả bằng tiếng Nhật.

HASTAG SEO

#日本 #技術

SEO tags

Nhật Bản, công nghiệp, công nghệ

PROMPT THUMBNAIL

thumbnail prompt
""",
                encoding="utf-8-sig",
            )

            metadata = parse_content_package(package)

            self.assertEqual(metadata.title, "Nhật Bản thử nghiệm dây chuyền mới")
            self.assertIn("#日本 #技術", metadata.description)
            self.assertEqual(metadata.hashtags, ("#日本", "#技術"))
            self.assertEqual(metadata.tags, ("Nhật Bản", "công nghiệp", "công nghệ"))
            self.assertEqual(parse_thumbnail_prompt(package), "thumbnail prompt")

    def test_japanese_tag_separators_and_hashtag_lists_keep_individual_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "package.txt"
            package.write_text(
                "Tiêu đề\nTest\nMô tả\nVideo description text.\nSEO tags\n"
                "#海外の反応 #日本製CNC\n日本製CNC、工具監視，Japan technology；工場保全\n",
                encoding="utf-8",
            )
            metadata = parse_content_package(package)
            self.assertEqual(metadata.tags, (
                "海外の反応", "日本製CNC", "工具監視", "Japan technology", "工場保全",
            ))

    def test_finds_checkpoint_package_for_remote_thumbnail_prompt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            workspace = _workspace(Path(temp))
            package = workspace.output_dir / "Video1.txt"
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả\n\nPROMPT THUMBNAIL\n\nremote prompt\n",
                encoding="utf-8-sig",
            )
            workspace.pipeline_checkpoint_path.write_text(
                json.dumps(
                    {
                        "stages": {
                            "package": {
                                "status": "complete",
                                "artifacts": {"path": str(package)},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(find_workspace_content_package(workspace), package.resolve())
            self.assertEqual(parse_thumbnail_prompt(package), "remote prompt")

    def test_sources_use_exact_checkpoint_video_and_package(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video1.mp4"
            package = workspace.output_dir / "Video1.txt"
            video.write_bytes(b"video")
            package.write_text(
                """TIÊU ĐỀ

Demo title

MÔ TẢ

Demo description

PROMPT THUMBNAIL

x
""",
                encoding="utf-8-sig",
            )
            workspace.pipeline_checkpoint_path.write_text(
                json.dumps(
                    {
                        "stages": {
                            "package": {"status": "complete", "artifacts": {"path": str(package)}},
                            "render": {"status": "complete", "artifacts": {"video": str(video)}},
                        }
                    }
                ),
                encoding="utf-8",
            )

            sources = resolve_workspace_upload_sources(workspace)

            self.assertEqual(sources.video_path, video.resolve())
            self.assertEqual(sources.package_path, package.resolve())
            self.assertEqual(sources.metadata.title, "Demo title")

    def test_sources_reject_renderer_part_file_while_render_is_running(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubePart_") as temp:
            workspace = _workspace(Path(temp))
            part = workspace.output_dir / ".Video1.part.mp4"
            package = workspace.output_dir / "Video1.txt"
            part.write_bytes(b"incomplete video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo title\n\nMÔ TẢ\n\nDemo description\n",
                encoding="utf-8-sig",
            )
            workspace.pipeline_checkpoint_path.write_text(
                json.dumps(
                    {
                        "stages": {
                            "package": {"status": "complete", "artifacts": {"path": str(package)}},
                            "render": {"status": "running", "artifacts": {"video": str(part)}},
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(YouTubeUploadError, "Render video chưa hoàn tất"):
                resolve_workspace_upload_sources(
                    workspace,
                    video_path=part,
                    package_path=package,
                )

    def test_sources_recover_final_video_from_completed_part_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubePartRecovery_") as temp:
            workspace = _workspace(Path(temp))
            part = workspace.output_dir / ".Video1.part.mp4"
            video = workspace.output_dir / "Video1.mp4"
            package = workspace.output_dir / "Video1.txt"
            video.write_bytes(b"complete video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo title\n\nMÔ TẢ\n\nDemo description\n",
                encoding="utf-8-sig",
            )
            workspace.pipeline_checkpoint_path.write_text(
                json.dumps(
                    {
                        "stages": {
                            "package": {"status": "complete", "artifacts": {"path": str(package)}},
                            "render": {"status": "complete", "artifacts": {"video": str(part)}},
                        }
                    }
                ),
                encoding="utf-8",
            )

            for staged_path in (part, workspace.output_dir / "Video1.part.mp4"):
                sources = resolve_workspace_upload_sources(
                    workspace,
                    video_path=staged_path,
                    package_path=package,
                )

                self.assertEqual(sources.video_path, video.resolve())

    def test_completed_render_gets_five_minute_persisted_upload_cooldown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubeCooldown_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video1.mp4"
            video.write_bytes(b"complete video")
            completed = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
            completed_epoch = completed.timestamp()
            file_mtime = completed_epoch + 75
            os.utime(video, (file_mtime, file_mtime))
            workspace.pipeline_checkpoint_path.write_text(
                json.dumps(
                    {
                        "stages": {
                            "render": {
                                "status": "complete",
                                "updated_at": completed.isoformat(),
                                "artifacts": {"video": str(video)},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            not_before = _post_render_upload_not_before(workspace, video)

            self.assertIsNotNone(not_before)
            self.assertAlmostEqual(not_before or 0, file_mtime + 300, places=2)

    def test_completed_render_upload_wait_is_cancellable_before_file_hashing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubeCooldownCancel_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video1.mp4"
            video.write_bytes(b"complete video")
            workspace.pipeline_checkpoint_path.write_text(
                json.dumps(
                    {
                        "stages": {
                            "render": {
                                "status": "complete",
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                                "artifacts": {"video": str(video)},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            cancelled = threading.Event()
            cancelled.set()

            with self.assertRaises(YouTubeUploadCancelled):
                _wait_for_render_upload_ready(
                    workspace,
                    video,
                    cancelled,
                    lambda _percent, _message: None,
                )

    def test_uncertain_checkpoint_for_render_part_blocks_retry_of_final_sibling(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubePartUncertain_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video1.mp4"
            part = workspace.output_dir / ".Video1.part.mp4"
            package = workspace.output_dir / "Video1.txt"
            video.write_bytes(b"final rendered video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            workspace.pipeline_checkpoint_path.write_text(
                json.dumps(
                    {
                        "stages": {
                            "render": {
                                "status": "complete",
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                                "artifacts": {"video": str(video)},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            workspace.state_dir.mkdir(parents=True, exist_ok=True)
            (workspace.state_dir / "youtube_upload.json").write_text(
                json.dumps(
                    {
                        "status": "uncertain",
                        "progress": 97,
                        "transfer_complete": True,
                        "attempt_id": "prior-attempt",
                        "youtube_url": "https://youtu.be/allocated-video",
                        "video_path": str(part),
                    }
                ),
                encoding="utf-8",
            )
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                video_path=video,
                package_path=package,
            )
            uploader = YouTubeStudioUploader(
                registry_path=Path(temp) / "youtube-registry.json"
            )

            with (
                mock.patch(
                    "docurandom.youtube_upload._wait_for_render_upload_ready"
                ),
                mock.patch.object(
                    uploader,
                    "_request_identity",
                    side_effect=AssertionError("must block before hashing/re-upload"),
                ) as identity_call,
            ):
                with self.assertRaisesRegex(YouTubeUploadUncertain, "file .part"):
                    uploader.upload(request)

            identity_call.assert_not_called()

    def test_sources_recover_one_video_copied_back_without_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video1.mp4"
            package = workspace.output_dir / "Video1.txt"
            video.write_bytes(b"video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo title\n\nMÔ TẢ\n\nDemo description\n",
                encoding="utf-8-sig",
            )

            sources = resolve_workspace_upload_sources(workspace)

            self.assertEqual(sources.video_path, video.resolve())
            self.assertEqual(sources.package_path, package.resolve())

    def test_sources_refuse_ambiguous_copied_back_videos(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            workspace = _workspace(Path(temp))
            (workspace.output_dir / "Video1.mp4").write_bytes(b"one")
            (workspace.output_dir / "Video2.mp4").write_bytes(b"two")
            package = workspace.output_dir / "Video1.txt"
            package.write_text(
                "TIÊU ĐỀ\n\nDemo title\n\nMÔ TẢ\n\nDemo description\n",
                encoding="utf-8-sig",
            )

            with self.assertRaisesRegex(YouTubeUploadError, "Không xác định được duy nhất video"):
                resolve_workspace_upload_sources(workspace)

    def test_explicit_thumbnail_is_used_even_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            root = Path(temp)
            workspace = _workspace(root / "workspace")
            video = workspace.output_dir / "Video1.mp4"
            package = workspace.output_dir / "Video1.txt"
            thumbnail = root / "selected-thumbnail.png"
            video.write_bytes(b"video")
            thumbnail.write_bytes(b"thumbnail")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo title\n\nMÔ TẢ\n\nDemo description\n",
                encoding="utf-8-sig",
            )

            sources = resolve_workspace_upload_sources(
                workspace,
                video_path=video,
                package_path=package,
                thumbnail_path=thumbnail,
            )

            self.assertEqual(sources.thumbnail_path, thumbnail.resolve())

    def test_invalid_explicit_thumbnail_is_rejected_before_browser_control(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            root = Path(temp)
            workspace = _workspace(root / "workspace")
            video = workspace.output_dir / "Video1.mp4"
            package = workspace.output_dir / "Video1.txt"
            invalid = root / "thumbnail.txt"
            video.write_bytes(b"video")
            invalid.write_text("not an image", encoding="utf-8")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo title\n\nMÔ TẢ\n\nDemo description\n",
                encoding="utf-8-sig",
            )

            with self.assertRaisesRegex(YouTubeUploadError, "Thumbnail đã chọn"):
                resolve_workspace_upload_sources(
                    workspace,
                    video_path=video,
                    package_path=package,
                    thumbnail_path=invalid,
                )


class LocalCdpControllerTests(unittest.TestCase):
    def test_custom_thumbnail_ready_requires_live_preview_in_custom_editor(self) -> None:
        controller = _SemanticCdpController(mock.Mock())
        nodes = [
            {
                "nodeId": 1,
                "backendNodeId": 101,
                "nodeName": "DIV",
                "attributes": ["id", "custom-still-editor-wrapper"],
            },
            {
                "nodeId": 2,
                "parentId": 1,
                "backendNodeId": 202,
                "nodeName": "BUTTON",
                "attributes": ["id", "preview-button", "aria-label", "Uploaded thumbnail"],
            },
        ]
        controller.dom_nodes = mock.Mock(return_value=nodes)  # type: ignore[method-assign]
        controller.dom_node_has_box = mock.Mock(return_value=True)  # type: ignore[method-assign]

        self.assertTrue(controller.custom_thumbnail_ready())
        controller.dom_node_has_box.return_value = False
        self.assertFalse(controller.custom_thumbnail_ready())

    def test_thumbnail_failure_is_not_silently_reported_as_metadata_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubeThumbFail_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video1.mp4"
            package = workspace.output_dir / "Video1.txt"
            thumbnail = Path(temp) / "selected.png"
            video.write_bytes(b"video")
            package.write_text(
                """TIÊU ĐỀ

Demo

MÔ TẢ

Nội dung mô tả

SEO TAGS
demo, documentary
""",
                encoding="utf-8-sig",
            )
            Image.new("RGB", (1280, 720), (20, 40, 80)).save(thumbnail)
            sources = resolve_workspace_upload_sources(
                workspace,
                video_path=video,
                package_path=package,
                thumbnail_path=thumbnail,
            )
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                privacy="PUBLIC",
                video_path=video,
                package_path=package,
                thumbnail_path=thumbnail,
            )
            controller = mock.MagicMock(spec=_SemanticCdpController)
            controller.server_upload_progress.return_value = (None, "")
            controller.named_text.side_effect = ["Demo", "Nội dung mô tả"]
            controller.wait_dom.return_value = {"backendNodeId": 101}
            controller.set_file.side_effect = YouTubeUploadError("image rejected")
            uploader = YouTubeStudioUploader(
                registry_path=Path(temp) / "registry.json"
            )

            with (
                mock.patch.object(uploader, "_ensure_advanced_edit_fields"),
                mock.patch.object(uploader, "_set_edit_visibility"),
                self.assertRaisesRegex(YouTubeUploadError, "image rejected"),
            ):
                uploader._persist_and_verify_saved_metadata(
                    request,
                    sources,
                    controller,
                    "https://youtu.be/abcDEF_123",
                    lambda _percent, _message: None,
                    threading.Event(),
                )

            self.assertEqual(controller.set_file.call_count, 2)
            self.assertFalse(
                any(
                    call.kwargs.get("attributes") == {"id": "save"}
                    for call in controller.wait_dom.call_args_list
                )
            )

    def test_navigation_waits_for_the_new_loader_before_using_existing_tab(self) -> None:
        class FakeCdp:
            def __init__(self) -> None:
                self.frame_calls = 0

            def command(
                self, method: str, _params: dict[str, object] | None = None
            ) -> dict[str, object]:
                if method == "Page.navigate":
                    return {"loaderId": "new-loader"}
                if method == "Page.getFrameTree":
                    self.frame_calls += 1
                    loader = "old-loader" if self.frame_calls == 1 else "new-loader"
                    return {
                        "frameTree": {
                            "frame": {
                                "url": "https://studio.youtube.com/",
                                "loaderId": loader,
                            }
                        }
                    }
                if method == "Runtime.evaluate":
                    return {"result": {"value": "complete"}}
                return {}

            def evaluate(self, _expression: str) -> object:
                return "complete"

        fake = FakeCdp()
        controller = _SemanticCdpController(fake)  # type: ignore[arg-type]

        controller.navigate_and_wait("https://studio.youtube.com/", timeout=1)

        self.assertGreaterEqual(fake.frame_calls, 2)

    def test_navigation_accepts_redirect_loader_after_rejecting_stale_dom(self) -> None:
        class FakeCdp:
            def __init__(self) -> None:
                self.frame_calls = 0

            def command(
                self, method: str, _params: dict[str, object] | None = None
            ) -> dict[str, object]:
                if method == "Page.navigate":
                    return {"loaderId": "initial-navigation-loader"}
                if method == "Page.getFrameTree":
                    self.frame_calls += 1
                    if self.frame_calls <= 2:
                        frame = {
                            "url": "about:blank",
                            "loaderId": "blank-loader",
                        }
                    else:
                        frame = {
                            "url": "https://studio.youtube.com/channel/demo",
                            "loaderId": "redirect-loader",
                        }
                    return {"frameTree": {"frame": frame}}
                if method == "Runtime.evaluate":
                    return {"result": {"value": "complete"}}
                return {}

            def evaluate(self, _expression: str) -> object:
                return "complete"

        fake = FakeCdp()
        controller = _SemanticCdpController(fake)  # type: ignore[arg-type]

        controller.navigate_and_wait("https://studio.youtube.com/", timeout=1)

        self.assertGreaterEqual(fake.frame_calls, 3)

    def test_dom_click_uses_target_local_input_without_runtime(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class FakeCdp:
            def command(
                self, method: str, params: dict[str, object] | None = None
            ) -> dict[str, object]:
                calls.append((method, params or {}))
                return {"result": {"value": True}}

        controller = _SemanticCdpController(FakeCdp())  # type: ignore[arg-type]
        controller.click({"backendNodeId": 42})

        methods = [method for method, _params in calls]
        self.assertEqual(
            methods,
            [
                "DOM.focus",
                "Input.dispatchKeyEvent",
                "Input.dispatchKeyEvent",
            ],
        )
        self.assertEqual(calls[1][1]["type"], "keyDown")
        self.assertNotIn("Runtime.callFunctionOn", methods)

    def test_open_new_profile_tab_uses_only_loopback_cdp(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "webSocketDebuggerUrl": (
                    "ws://127.0.0.1:9222/devtools/page/youtube-studio"
                )
            }
        ).encode("utf-8")

        with mock.patch("urllib.request.urlopen", return_value=response) as opened:
            ws_url = _mcp_open_cdp_page_ws_url(
                {
                    "browser_url": "http://127.0.0.1:9222",
                    "targets_url": "http://127.0.0.1:9222/json",
                },
                "https://studio.youtube.com/",
            )

        request = opened.call_args.args[0]
        self.assertEqual(request.method, "PUT")
        self.assertTrue(request.full_url.startswith("http://127.0.0.1:9222/json/new?"))
        self.assertEqual(ws_url, "ws://127.0.0.1:9222/devtools/page/youtube-studio")

    def test_background_tab_is_bound_to_the_created_target_id(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def cdp_command(
                self,
                _profile_id: str,
                method: str,
                params: dict[str, object],
            ) -> dict[str, object]:
                self.calls.append((method, params))
                if method == "Target.createTarget":
                    return {"targetId": "monitor-target"}
                return {"success": True}

        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            [
                {
                    "id": "user-tab",
                    "type": "page",
                    "webSocketDebuggerUrl": (
                        "ws://127.0.0.1:9222/devtools/page/user-tab"
                    ),
                },
                {
                    "id": "monitor-target",
                    "type": "page",
                    "webSocketDebuggerUrl": (
                        "ws://127.0.0.1:9222/devtools/page/monitor-target"
                    ),
                },
            ]
        ).encode("utf-8")
        client = FakeClient()

        with mock.patch("urllib.request.urlopen", return_value=response):
            ws_url = _mcp_open_background_cdp_page_ws_url(
                client,  # type: ignore[arg-type]
                "profile",
                {"targets_url": "http://127.0.0.1:9222/json"},
                "https://studio.youtube.com/",
            )

        self.assertEqual(
            ws_url,
            "ws://127.0.0.1:9222/devtools/page/monitor-target",
        )
        self.assertEqual(
            client.calls,
            [
                (
                    "Target.createTarget",
                    {
                        "url": "https://studio.youtube.com/",
                        "background": True,
                    },
                )
            ],
        )
        self.assertEqual(_cdp_page_target_id(ws_url), "monitor-target")

    def test_minimized_window_owns_active_automation_target(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def cdp_command(
                self,
                _profile_id: str,
                method: str,
                params: dict[str, object],
            ) -> dict[str, object]:
                self.calls.append((method, params))
                if method == "Target.createTarget":
                    return {"targetId": "automation-target"}
                if method == "Browser.getWindowForTarget":
                    return {"windowId": 42, "bounds": {"windowState": "normal"}}
                if method == "Browser.getWindowBounds":
                    return {"bounds": {"windowState": "minimized"}}
                return {}

        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            [
                {
                    "id": "automation-target",
                    "type": "page",
                    "webSocketDebuggerUrl": (
                        "ws://127.0.0.1:9222/devtools/page/automation-target"
                    ),
                }
            ]
        ).encode("utf-8")
        client = FakeClient()

        with mock.patch("urllib.request.urlopen", return_value=response):
            ws_url = _mcp_open_minimized_cdp_window_ws_url(
                client,  # type: ignore[arg-type]
                "profile",
                {"targets_url": "http://127.0.0.1:9222/json"},
                "about:blank",
            )

        self.assertEqual(
            ws_url,
            "ws://127.0.0.1:9222/devtools/page/automation-target",
        )
        self.assertEqual(
            client.calls,
            [
                (
                    "Target.createTarget",
                    {
                        "url": "about:blank",
                        "background": True,
                        "newWindow": True,
                    },
                ),
                (
                    "Browser.getWindowForTarget",
                    {"targetId": "automation-target"},
                ),
                (
                    "Browser.setWindowBounds",
                    {
                        "windowId": 42,
                        "bounds": {"windowState": "minimized"},
                    },
                ),
                (
                    "Browser.getWindowBounds",
                    {"windowId": 42},
                ),
            ],
        )
        self.assertNotIn("Target.activateTarget", [method for method, _ in client.calls])

    def test_owned_background_target_close_waits_until_catalog_removes_it(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def cdp_command(
                self,
                _profile_id: str,
                method: str,
                params: dict[str, object],
            ) -> dict[str, object]:
                self.calls.append((method, params))
                return {"success": True}

        present = mock.MagicMock()
        present.__enter__.return_value = present
        present.read.return_value = json.dumps(
            [{"id": "monitor-target", "type": "page"}]
        ).encode("utf-8")
        gone = mock.MagicMock()
        gone.__enter__.return_value = gone
        gone.read.return_value = b"[]"
        client = FakeClient()

        with mock.patch(
            "urllib.request.urlopen",
            side_effect=(present, gone),
        ):
            closed = _mcp_close_cdp_page_target(
                client,  # type: ignore[arg-type]
                "profile",
                {"targets_url": "http://127.0.0.1:9222/json"},
                "monitor-target",
            )

        self.assertTrue(closed)
        self.assertEqual(
            client.calls,
            [
                (
                    "Target.closeTarget",
                    {"targetId": "monitor-target"},
                )
            ],
        )

    def test_loopback_cdp_disables_websocket_message_size_limit(self) -> None:
        socket = mock.Mock()
        with mock.patch(
            "docurandom.youtube_upload.websocket_connect",
            return_value=socket,
        ) as connect:
            with _CdpSession("ws://127.0.0.1:12345/devtools/page/test"):
                pass
        self.assertIsNone(connect.call_args.kwargs["max_size"])
        self.assertIsNone(connect.call_args.kwargs["proxy"])
        socket.close.assert_called_once()

    def test_cdp_session_rejects_non_loopback_websocket(self) -> None:
        with (
            mock.patch("docurandom.youtube_upload.websocket_connect") as connect,
            self.assertRaisesRegex(YouTubeUploadError, "không nằm trên máy này"),
        ):
            _CdpSession("wss://example.test/devtools/page/test").__enter__()
        connect.assert_not_called()

    def test_incidental_beforeunload_does_not_abort_read_only_cdp_command(self) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.sent: list[dict[str, object]] = []
                self.messages = iter(
                    (
                        json.dumps(
                            {
                                "method": "Page.javascriptDialogOpening",
                                "params": {"type": "beforeunload"},
                            }
                        ),
                        json.dumps(
                            {
                                "id": 1,
                                "result": {
                                    "frameTree": {"frame": {"url": "https://studio.youtube.com/"}}
                                },
                            }
                        ),
                    )
                )

            def send(self, message: str) -> None:
                self.sent.append(json.loads(message))

            def recv(self, timeout: float) -> str:
                self.timeout = timeout
                return next(self.messages)

        socket = FakeSocket()
        session = _CdpSession("ws://127.0.0.1:12345/devtools/page/test")
        session._ws = socket

        result = session.command("Page.getFrameTree")

        self.assertEqual(result["frameTree"]["frame"]["url"], "https://studio.youtube.com/")
        self.assertEqual(socket.sent[1]["method"], "Page.handleJavaScriptDialog")
        self.assertEqual(socket.sent[1]["params"], {"accept": False})

    def test_beforeunload_still_blocks_an_actual_navigation(self) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.sent: list[dict[str, object]] = []

            def send(self, message: str) -> None:
                self.sent.append(json.loads(message))

            def recv(self, timeout: float) -> str:
                self.timeout = timeout
                return json.dumps(
                    {
                        "method": "Page.javascriptDialogOpening",
                        "params": {"type": "beforeunload"},
                    }
                )

        socket = FakeSocket()
        session = _CdpSession("ws://127.0.0.1:12345/devtools/page/test")
        session._ws = socket

        with self.assertRaisesRegex(YouTubeUploadUncertain, "giữ nguyên tab"):
            session.command("Page.navigate", {"url": "https://studio.youtube.com/"})
        self.assertEqual(socket.sent[1]["method"], "Page.handleJavaScriptDialog")

    def test_evaluate_returns_runtime_value_without_donut_paid_tools(self) -> None:
        session = _CdpSession("ws://127.0.0.1:12345/devtools/page/test")
        calls: list[tuple[str, dict[str, object] | None]] = []

        def command(method: str, params: dict[str, object] | None = None) -> dict[str, object]:
            calls.append((method, params))
            return {"result": {"type": "object", "value": {"ready": True}}}

        session.command = command  # type: ignore[method-assign]

        self.assertEqual(session.evaluate("(()=>({ready:true}))()"), {"ready": True})
        self.assertEqual(calls[0][0], "Runtime.evaluate")
        self.assertTrue(calls[0][1]["userGesture"])

    def test_paid_plan_rejection_is_reported_without_retrying(self) -> None:
        class FakeSocket:
            def send(self, _message: str) -> None:
                return None

            def recv(self, timeout: float) -> str:
                self.timeout = timeout
                return json.dumps(
                    {
                        "id": 1,
                        "error": {
                            "code": -32000,
                            "message": "Browser automation requires a paid Donut Browser plan.",
                        },
                    }
                )

        session = _CdpSession("ws://127.0.0.1:12345/devtools/page/test")
        session._ws = FakeSocket()

        with self.assertRaisesRegex(
            YouTubeUploadError,
            "không có quyền browser automation",
        ):
            session.command("Runtime.evaluate", {"expression": "location.href"})

    def test_semantic_text_entry_uses_dom_focus_without_mouse_or_runtime(self) -> None:
        class FakeCdp:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object] | None]] = []
                self.text = ""

            def command(
                self, method: str, params: dict[str, object] | None = None
            ) -> dict[str, object]:
                self.calls.append((method, params))
                if method == "Input.insertText" and params is not None:
                    self.text = str(params.get("text") or "")
                if method == "Accessibility.getPartialAXTree":
                    return {"nodes": [{"value": {"value": self.text}}]}
                return {}

        cdp = FakeCdp()
        controller = _SemanticCdpController(cdp)  # type: ignore[arg-type]

        controller.set_text({"backendNodeId": 321}, "日本語のタイトル")

        methods = [method for method, _params in cdp.calls]
        self.assertEqual(methods[0], "DOM.focus")
        self.assertIn("Input.insertText", methods)
        self.assertEqual(methods[-1], "Input.dispatchKeyEvent")
        self.assertNotIn("Runtime.evaluate", methods)
        self.assertNotIn("Input.dispatchMouseEvent", methods)
        inserted = next(params for method, params in cdp.calls if method == "Input.insertText")
        self.assertEqual(inserted, {"text": "日本語のタイトル"})

    def test_background_bridge_uses_trusted_cdp_for_studio_metadata(self) -> None:
        class FakeBridge:
            def __init__(self) -> None:
                self.text = ""
                self.checked = False
                self.calls: list[tuple[str, dict[str, object]]] = []

            def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                self.calls.append((command, dict(params)))
                if command == "youtube_set_text":
                    self.text = str(params.get("text") or "")
                    return {"pageResult": None}
                if command == "youtube_get_text":
                    return {"found": True, "value": self.text, "laidOut": False}
                if command == "youtube_query":
                    return {
                        "found": True,
                        "checked": self.checked,
                        "ariaChecked": "true" if self.checked else "false",
                    }
                if command == "youtube_click":
                    if "name=" in str(params.get("selector") or ""):
                        self.checked = True
                    return {"pageResult": None}
                raise AssertionError(command)

        class FakeCdp:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object] | None]] = []
                self.text = ""
                self.radio_checked = False

            def command(
                self, method: str, params: dict[str, object] | None = None
            ) -> dict[str, object]:
                self.calls.append((method, params))
                if method == "Input.insertText" and params is not None:
                    self.text = str(params.get("text") or "")
                if (
                    method == "Input.dispatchKeyEvent"
                    and params is not None
                    and params.get("type") == "keyDown"
                    and params.get("key") == " "
                ):
                    self.radio_checked = True
                if method == "Accessibility.getPartialAXTree":
                    return {"nodes": [{"value": {"value": self.text}}]}
                if method == "DOM.getFlattenedDocument":
                    attributes = [
                        "name",
                        "VIDEO_MADE_FOR_KIDS_NOT_MFK",
                        "aria-checked",
                        "true" if self.radio_checked else "false",
                    ]
                    if self.radio_checked:
                        attributes.extend(["checked", ""])
                    return {
                        "nodes": [
                            {
                                "nodeId": 1,
                                "backendNodeId": 321,
                                "nodeName": "TP-YT-PAPER-RADIO-BUTTON",
                                "attributes": attributes,
                            }
                        ]
                    }
                return {}

        cdp = FakeCdp()
        bridge = FakeBridge()
        controller = _SemanticCdpController(
            cdp,  # type: ignore[arg-type]
            browser_bridge=bridge,  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )
        controller.wait_dom = mock.Mock(  # type: ignore[method-assign]
            return_value={"backendNodeId": 321}
        )

        controller.set_named_text("title", "Metadata nền", verify_timeout=2)
        controller.select_dom_radio("VIDEO_MADE_FOR_KIDS_NOT_MFK", timeout=2)
        controller.press(
            {
                "backendNodeId": 10,
                "attributes": ["id", "next-button"],
            }
        )

        self.assertEqual(cdp.text, "Metadata nền")
        self.assertEqual(bridge.text, "")
        self.assertTrue(cdp.radio_checked)
        self.assertFalse(bridge.calls)
        methods = [method for method, _params in cdp.calls]
        self.assertIn("DOM.focus", methods)
        self.assertIn("Input.insertText", methods)
        self.assertGreaterEqual(methods.count("Input.dispatchKeyEvent"), 6)

    def test_studio_metadata_retries_when_polymer_restores_first_value(self) -> None:
        class ResetOnceCdp:
            def __init__(self) -> None:
                self.text = ""
                self.insert_count = 0
                self.reads_after_insert = 0

            def command(
                self, method: str, params: dict[str, object] | None = None
            ) -> dict[str, object]:
                if method == "Input.insertText" and params is not None:
                    self.text = str(params.get("text") or "")
                    self.insert_count += 1
                    self.reads_after_insert = 0
                if method == "Accessibility.getPartialAXTree":
                    self.reads_after_insert += 1
                    if self.insert_count == 1 and self.reads_after_insert >= 2:
                        self.text = "Video 1"
                    return {"nodes": [{"value": {"value": self.text}}]}
                return {}

        cdp = ResetOnceCdp()
        controller = _SemanticCdpController(cdp)  # type: ignore[arg-type]
        controller.wait_dom = mock.Mock(  # type: ignore[method-assign]
            return_value={"backendNodeId": 654}
        )

        controller.set_named_text("title", "Tiêu đề chính xác", verify_timeout=4)

        self.assertEqual(cdp.insert_count, 2)
        self.assertEqual(cdp.text, "Tiêu đề chính xác")

    def test_background_bridge_reads_video_link_while_studio_is_saving(self) -> None:
        class FakeBridge:
            def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                self.last = (command, dict(params), timeout)
                if params.get("selector") == "#video-link":
                    return {
                        "found": True,
                        "href": "https://youtu.be/mak6zRJnf24",
                        "text": "https://youtu.be/mak6zRJnf24",
                    }
                return {"found": False}

        cdp = mock.Mock()
        controller = _SemanticCdpController(
            cdp,
            browser_bridge=FakeBridge(),  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )

        self.assertEqual(controller.youtube_url(), "https://youtu.be/mak6zRJnf24")
        cdp.command.assert_not_called()

    def test_background_bridge_reads_content_state_for_thumbnail_only_video_id(self) -> None:
        class FakeBridge:
            def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                self.last = (command, dict(params), timeout)
                self.asserted = command == "youtube_content_state"
                return {
                    "tabId": 177,
                    "found": True,
                    "videoId": "agXtWYwYGYE",
                    "title": "DaiTa8",
                    "rowText": "DaiTa8 · Bản nháp · Tiếp tục tải lên",
                    "hasContinue": True,
                    "interrupted": True,
                    "clicked": False,
                    "matchCount": 1,
                }

        bridge = FakeBridge()
        controller = _SemanticCdpController(
            mock.Mock(),
            browser_bridge=bridge,  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )

        state = controller.content_upload_state("agXtWYwYGYE")

        self.assertTrue(bridge.asserted)
        self.assertTrue(state["found"])
        self.assertTrue(state["interrupted"])
        self.assertEqual(bridge.last[1]["video_id"], "agXtWYwYGYE")
        self.assertEqual(bridge.last[1]["action"], "probe")
        controller.cdp.command.assert_not_called()

    def test_studio_draft_candidates_use_extension_transport_when_paired(self) -> None:
        class FakeBridge:
            def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                self.last = (command, dict(params), timeout)
                return {
                    "tabId": 177,
                    "candidates": [
                        {
                            "videoId": "agXtWYwYGYE",
                            "title": "DaiTa8",
                            "rowText": "DaiTa8 · Bản nháp · Tiếp tục tải lên",
                            "position": 0,
                        }
                    ],
                }

        bridge = FakeBridge()
        cdp = mock.Mock()
        controller = _SemanticCdpController(
            cdp,
            browser_bridge=bridge,  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )

        candidates = YouTubeStudioUploader._studio_draft_candidates(controller)

        self.assertEqual([item.video_id for item in candidates], ["agXtWYwYGYE"])
        self.assertEqual(candidates[0].title, "DaiTa8")
        self.assertEqual(bridge.last[0], "youtube_content_state")
        cdp.evaluate.assert_not_called()

    def test_removed_upload_dialog_requires_positive_transfer_proof(self) -> None:
        class FakeBridge:
            def __init__(self) -> None:
                self.dialog_present = True

            def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                self.last = (command, dict(params), timeout)
                if params.get("selector") == "#close-button, button[aria-label='Close']":
                    return {"found": False, "laidOut": False, "text": ""}
                return {
                    "found": self.dialog_present,
                    "laidOut": self.dialog_present,
                    "text": "Saving..." if self.dialog_present else "",
                }

        bridge = FakeBridge()
        controller = _SemanticCdpController(
            mock.Mock(),
            browser_bridge=bridge,  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )

        self.assertFalse(controller.youtube_submission_complete())
        bridge.dialog_present = False
        with mock.patch.object(controller, "server_upload_progress", return_value=(14.0, "24 phút")):
            self.assertFalse(controller.youtube_submission_complete())
        with mock.patch.object(controller, "server_upload_progress", return_value=(None, "")):
            self.assertFalse(controller.youtube_submission_complete())
        with mock.patch.object(controller, "server_upload_progress", return_value=(100.0, "")):
            self.assertTrue(controller.youtube_submission_complete())

    def test_owned_target_is_reactivated_before_trusted_button_input(self) -> None:
        class FakeCdp:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object] | None]] = []

            def command(
                self,
                method: str,
                params: dict[str, object] | None = None,
                *,
                timeout: float | None = None,
            ) -> dict[str, object]:
                self.calls.append((method, params))
                return {}

        cdp = FakeCdp()
        window_states: list[str] = []
        controller = _SemanticCdpController(
            cdp,  # type: ignore[arg-type]
            automation_target_id="owned-upload-target",
            window_state=window_states.append,
        )

        controller.press({"backendNodeId": 321})

        self.assertEqual(window_states, ["parked"])
        methods = [method for method, _params in cdp.calls]
        self.assertLess(methods.index("Target.activateTarget"), methods.index("DOM.focus"))

    def test_enabled_close_does_not_complete_while_transfer_is_below_100_percent(self) -> None:
        class FakeBridge:
            def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                selector = str(params.get("selector") or "")
                if selector == "ytcp-dialog, tp-yt-paper-dialog":
                    return {"found": False}
                if selector == "ytcp-uploads-dialog":
                    return {
                        "found": True,
                        "laidOut": True,
                        "text": "Đã tải được 42% · Còn 25 giây",
                    }
                if selector == "#close-button, button[aria-label='Close']":
                    return {
                        "found": True,
                        "laidOut": True,
                        "disabled": False,
                        "ariaDisabled": "false",
                    }
                raise AssertionError((command, params, timeout))

        controller = _SemanticCdpController(
            mock.Mock(),
            browser_bridge=FakeBridge(),  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )

        self.assertFalse(controller.youtube_submission_complete())

    def test_owned_upload_wizard_completion_without_separate_popup(self) -> None:
        for text, laid_out, expected in (
            ("Video đã tải lên xong Đang xử lý video Còn 3 phút", True, True),
            ("Video upload complete Processing HD version", True, True),
            ("動画のアップロードが完了 処理中", True, True),
            ("Video đã tải lên xong", False, False),
            ("Đang xử lý video Các bước kiểm tra đã hoàn tất", True, False),
            ("Đã tải được 15% Video đã tải lên xong", True, False),
        ):
            with self.subTest(text=text, laid_out=laid_out):
                class FakeBridge:
                    def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                        if params.get("selector") == "ytcp-uploads-dialog":
                            return {"found": True, "laidOut": laid_out, "text": text}
                        return {"found": False}

                controller = _SemanticCdpController(
                    mock.Mock(),
                    browser_bridge=FakeBridge(),  # type: ignore[arg-type]
                    bridge_profile_id="profile-test",
                    bridge_tab_id=177,
                )
                self.assertEqual(controller.youtube_submission_complete(), expected)

    def test_live_percent_below_100_wins_over_stale_completion_overlay(self) -> None:
        class FakeBridge:
            def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                selector = str(params.get("selector") or "")
                if selector == "ytcp-uploads-dialog":
                    return {
                        "found": True,
                        "laidOut": True,
                        "text": "56% uploaded · 12 seconds remaining",
                    }
                if selector == "ytcp-dialog, tp-yt-paper-dialog":
                    return {
                        "found": True,
                        "laidOut": True,
                        "text": "Video upload complete",
                    }
                raise AssertionError((command, params, timeout))

        controller = _SemanticCdpController(
            mock.Mock(),
            browser_bridge=FakeBridge(),  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )

        self.assertFalse(controller.youtube_submission_complete())

    def test_edit_visibility_menu_accepts_hidden_stateful_radio(self) -> None:
        controller = mock.MagicMock(spec=_SemanticCdpController)
        controller.wait_for.return_value = {"backendDOMNodeId": 101}
        controller.wait_dom.return_value = {
            "backendNodeId": 202,
            "attributes": [
                "name",
                "PRIVATE",
                "role",
                "radio",
                "aria-checked",
                "true",
            ],
        }

        YouTubeStudioUploader._open_edit_visibility(controller, threading.Event())

        controller.press.assert_called_once_with({"backendDOMNodeId": 101})
        controller.wait_dom.assert_called_once()
        self.assertFalse(controller.wait_dom.call_args.kwargs["require_laid_out"])
        controller._bridge_command.assert_not_called()

    def test_edit_visibility_falls_back_to_bridge_when_ax_button_is_missing(self) -> None:
        controller = mock.MagicMock(spec=_SemanticCdpController)
        controller.has_background_bridge = True
        controller.wait_for.side_effect = YouTubeUploadError(
            "visibility button is missing from AX"
        )
        controller.wait_dom.return_value = {
            "backendNodeId": 202,
            "attributes": ["name", "PRIVATE"],
        }

        def bridge_command(command, params, *, timeout):  # type: ignore[no-untyped-def]
            if command == "youtube_query":
                return {"found": params["selector"] == "ytcp-video-metadata-visibility #select-button"}
            if command == "youtube_click":
                return {"clicked": True}
            raise AssertionError((command, params, timeout))

        controller._bridge_command.side_effect = bridge_command

        YouTubeStudioUploader._open_edit_visibility(controller, threading.Event())

        controller._bridge_command.assert_any_call(
            "youtube_click",
            {"selector": "ytcp-video-metadata-visibility #select-button"},
            timeout=10,
        )
        controller.wait_dom.assert_called_once()
        self.assertFalse(controller.wait_dom.call_args.kwargs["require_laid_out"])

    def test_edit_visibility_absent_on_details_route_is_nonfatal(self) -> None:
        controller = mock.MagicMock(spec=_SemanticCdpController)
        controller.has_background_bridge = True
        controller._bridge_command.return_value = {"found": False}

        opened = YouTubeStudioUploader._open_edit_visibility(
            controller, threading.Event()
        )

        self.assertFalse(opened)
        controller.wait_for.assert_not_called()
        controller.wait_dom.assert_not_called()

    def test_edit_visibility_falls_back_when_radio_host_is_not_focusable(self) -> None:
        uploader = object.__new__(YouTubeStudioUploader)
        controller = mock.MagicMock(spec=_SemanticCdpController)
        option = {"backendNodeId": 202}
        done = {"backendDOMNodeId": 303}
        controller.has_background_bridge = True
        controller.wait_dom.return_value = option
        controller.wait_for.return_value = done
        controller.dom_radio_checked.side_effect = [False, True]

        def press(node, *, space=False):  # type: ignore[no-untyped-def]
            if node == option and space:
                raise YouTubeUploadError(
                    "Donut CDP lỗi DOM.focus: {'code': -32000, "
                    "'message': 'Element is not focusable'}"
                )

        controller.press.side_effect = press

        with mock.patch.object(
            YouTubeStudioUploader,
            "_open_edit_visibility",
        ):
            uploader._set_edit_visibility(controller, "PUBLIC", threading.Event())

        controller._bridge_command.assert_called_once_with(
            "youtube_click",
            {"selector": '[name="PUBLIC"]'},
            timeout=10,
        )
        controller.click_dom.assert_called_once_with(done)

    def test_edit_visibility_falls_back_to_popup_done_when_ax_button_is_hidden(self) -> None:
        uploader = object.__new__(YouTubeStudioUploader)
        controller = mock.MagicMock(spec=_SemanticCdpController)
        option = {"backendNodeId": 202}
        controller.has_background_bridge = True
        controller.wait_dom.return_value = option
        controller.dom_radio_checked.side_effect = [False, True]
        controller.wait_for.side_effect = YouTubeUploadError("Done is hidden from AX")
        controller._bridge_command.return_value = {
            "found": True,
            "disabled": False,
            "ariaDisabled": "false",
        }

        with mock.patch.object(
            YouTubeStudioUploader,
            "_open_edit_visibility",
        ):
            uploader._set_edit_visibility(controller, "PUBLIC", threading.Event())

        self.assertEqual(
            controller._bridge_command.call_args_list[-2:],
            [
                mock.call(
                    "youtube_query",
                    {"selector": "ytcp-video-visibility-edit-popup #save-button"},
                    timeout=8,
                ),
                mock.call(
                    "youtube_click",
                    {"selector": "ytcp-video-visibility-edit-popup #save-button"},
                    timeout=10,
                ),
            ],
        )

    def test_background_bridge_does_not_complete_from_hidden_or_aria_disabled_close(self) -> None:
        class FakeBridge:
            def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                selector = str(params.get("selector") or "")
                if selector == "ytcp-dialog, tp-yt-paper-dialog":
                    return {"found": False}
                if selector == "ytcp-uploads-dialog":
                    return {
                        "found": True,
                        "laidOut": False,
                        "text": "Video processing. Checks complete.",
                    }
                if selector == "#close-button, button[aria-label='Close']":
                    return {
                        "found": True,
                        "laidOut": True,
                        "disabled": False,
                        "ariaDisabled": "true",
                    }
                raise AssertionError((command, params, timeout))

        controller = _SemanticCdpController(
            mock.Mock(),
            browser_bridge=FakeBridge(),  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )

        self.assertFalse(controller.youtube_submission_complete())

    def test_background_bridge_accepts_processing_confirmation_after_save(self) -> None:
        class FakeBridge:
            def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                selector = str(params.get("selector") or "")
                if selector == "ytcp-dialog, tp-yt-paper-dialog":
                    return {
                        "found": True,
                        "laidOut": True,
                        "text": "Video processing. Video upload complete. Processing complete.",
                    }
                if selector == "ytcp-uploads-dialog":
                    return {
                        "found": True,
                        "laidOut": True,
                        "text": "Video processing Checks complete. No issues found.",
                    }
                if selector == "#close-button, button[aria-label='Close']":
                    return {
                        "found": True,
                        "laidOut": True,
                        "disabled": False,
                    }
                raise AssertionError((command, params, timeout))

        controller = _SemanticCdpController(
            mock.Mock(),
            browser_bridge=FakeBridge(),  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )

        self.assertTrue(controller.youtube_submission_complete())

    def test_completed_upload_dialog_is_closed_before_navigation(self) -> None:
        class FakeBridge:
            def __init__(self) -> None:
                self.dialog_reads = 0
                self.clicks = 0

            def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                selector = str(params.get("selector") or "")
                if selector == "ytcp-dialog, tp-yt-paper-dialog":
                    return {"found": False}
                if selector == "ytcp-uploads-dialog":
                    self.dialog_reads += 1
                    return {
                        "found": self.dialog_reads < 3,
                        "laidOut": self.dialog_reads < 3,
                        "text": "100% uploaded. Video processing. Checks complete.",
                    }
                if selector == "#close-button, button[aria-label='Close']":
                    return {
                        "found": True,
                        "laidOut": True,
                        "disabled": False,
                        "ariaDisabled": "false",
                    }
                if command == "youtube_click" and selector == "#close-button":
                    self.clicks += 1
                    return {"clicked": True}
                raise AssertionError((command, params, timeout))

        controller = _SemanticCdpController(
            mock.Mock(),
            browser_bridge=FakeBridge(),  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )
        controller.close_completed_upload_dialog(timeout=0.1)

        self.assertEqual(controller.browser_bridge.clicks, 1)  # type: ignore[union-attr]

    def test_background_bridge_reads_vietnamese_upload_percent_and_eta(self) -> None:
        class FakeBridge:
            def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                return {
                    "found": True,
                    "laidOut": True,
                    "text": (
                        "Video 100% bí mật Đã tải được 53% · Còn 2 phút "
                        "Không đóng thẻ trình duyệt"
                    ),
                }

        controller = _SemanticCdpController(
            mock.Mock(),
            browser_bridge=FakeBridge(),  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )

        self.assertEqual(controller.youtube_upload_progress(), (53.0, "2 phút"))

    def test_background_bridge_reads_english_upload_percent_and_eta(self) -> None:
        class FakeBridge:
            def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                return {
                    "found": True,
                    "laidOut": True,
                    "text": "87% uploaded · 19 seconds remaining",
                }

        controller = _SemanticCdpController(
            mock.Mock(),
            browser_bridge=FakeBridge(),  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )

        self.assertEqual(
            controller.youtube_upload_progress(),
            (87.0, "19 seconds"),
        )

    def test_background_bridge_reads_short_vietnamese_upload_percent(self) -> None:
        class FakeBridge:
            def command(self, _profile_id, command, params, *, timeout):  # type: ignore[no-untyped-def]
                return {
                    "found": True,
                    "laidOut": True,
                    "text": "Đã tải 96% · đang xử lý",
                }

        controller = _SemanticCdpController(
            mock.Mock(),
            browser_bridge=FakeBridge(),  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )

        self.assertEqual(controller.youtube_upload_progress(), (96.0, ""))

    def test_wait_for_upload_transfer_does_not_treat_early_link_as_complete(self) -> None:
        class UploadStillRunning:
            def youtube_url(self) -> str:
                return "https://youtu.be/allocated_early"

            def youtube_submission_complete(self) -> bool:
                return False

            def youtube_upload_progress(self) -> tuple[float, str]:
                return 53.0, "2 phút"

            def raise_for_page_error(self) -> None:
                return None

            def raise_for_upload_error(self) -> None:
                return None

        reports: list[str] = []
        url, complete = YouTubeStudioUploader._wait_for_upload_transfer_complete(
            UploadStillRunning(),  # type: ignore[arg-type]
            lambda _percent, detail: reports.append(detail),
            threading.Event(),
            timeout=0.04,
            poll_interval=0.005,
            stable_for=0.01,
        )

        self.assertEqual(url, "https://youtu.be/allocated_early")
        self.assertFalse(complete)
        self.assertTrue(any("link tạm thời" in detail for detail in reports))
        self.assertTrue(any("53%" in detail for detail in reports))

    def test_wait_for_upload_transfer_requires_stable_close_before_continuing(self) -> None:
        class UploadFinishes:
            def __init__(self) -> None:
                self.reads = 0

            def youtube_url(self) -> str:
                return "https://youtu.be/allocated_early"

            def youtube_submission_complete(self) -> bool:
                self.reads += 1
                return self.reads >= 3

            def youtube_upload_progress(self) -> tuple[float | None, str]:
                return (None, "") if self.reads >= 3 else (87.0, "19 giây")

            def raise_for_page_error(self) -> None:
                return None

            def raise_for_upload_error(self) -> None:
                return None

        reports: list[str] = []
        controller = UploadFinishes()
        url, complete = YouTubeStudioUploader._wait_for_upload_transfer_complete(
            controller,  # type: ignore[arg-type]
            lambda _percent, detail: reports.append(detail),
            threading.Event(),
            timeout=0.2,
            poll_interval=0.005,
            stable_for=0.015,
        )

        self.assertEqual(url, "https://youtu.be/allocated_early")
        self.assertTrue(complete)
        self.assertGreaterEqual(controller.reads, 4)
        self.assertTrue(any("tải xong tệp" in detail for detail in reports))

    def test_wait_for_upload_transfer_rejects_false_completion_below_100(self) -> None:
        class UploadStillRunning:
            def youtube_url(self) -> str:
                return "https://youtu.be/still_uploading"

            def youtube_submission_complete(self) -> bool:
                return True

            def youtube_upload_progress(self) -> tuple[float, str]:
                return 56.0, "12 giây"

            def raise_for_page_error(self) -> None:
                return None

            def raise_for_upload_error(self) -> None:
                return None

        url, complete = YouTubeStudioUploader._wait_for_upload_transfer_complete(
            UploadStillRunning(),  # type: ignore[arg-type]
            lambda _percent, _detail: None,
            threading.Event(),
            timeout=0.06,
            poll_interval=0.005,
            stable_for=0.02,
        )

        self.assertEqual(url, "https://youtu.be/still_uploading")
        self.assertFalse(complete)

    def test_wait_for_upload_transfer_keeps_transient_100_percent_as_proof(self) -> None:
        class UploadReplacesProgressImmediately:
            def __init__(self) -> None:
                self.progress_reads = 0

            def youtube_url(self) -> str:
                return "https://youtu.be/complete_once"

            def youtube_submission_complete(self) -> bool:
                return False

            def youtube_upload_progress(self) -> tuple[float | None, str]:
                self.progress_reads += 1
                return (100.0, "") if self.progress_reads == 1 else (None, "")

            def raise_for_page_error(self) -> None:
                return None

            def raise_for_upload_error(self) -> None:
                return None

        url, complete = YouTubeStudioUploader._wait_for_upload_transfer_complete(
            UploadReplacesProgressImmediately(),  # type: ignore[arg-type]
            lambda _percent, _detail: None,
            threading.Event(),
            timeout=0.05,
            poll_interval=0.005,
            stable_for=0.01,
        )

        self.assertEqual(url, "https://youtu.be/complete_once")
        self.assertTrue(complete)

    def test_wait_for_upload_transfer_reports_and_releases_a_stalled_transfer(self) -> None:
        class UploadStalled:
            def youtube_url(self) -> str:
                return "https://youtu.be/stalled_upload"

            def youtube_submission_complete(self) -> bool:
                return False

            def youtube_upload_progress(self) -> tuple[float, str]:
                return 53.0, "2 phút"

            def raise_for_page_error(self) -> None:
                return None

            def raise_for_upload_error(self) -> None:
                return None

        reports: list[str] = []
        url, complete = YouTubeStudioUploader._wait_for_upload_transfer_complete(
            UploadStalled(),  # type: ignore[arg-type]
            lambda _percent, detail: reports.append(detail),
            threading.Event(),
            timeout=0.2,
            poll_interval=0.005,
            stable_for=0.01,
            stall_warn_after=0.01,
            stall_notice_interval=0.01,
            stall_timeout=0.04,
        )

        self.assertEqual(url, "https://youtu.be/stalled_upload")
        self.assertFalse(complete)
        self.assertTrue(any("không có tiến triển" in detail for detail in reports))

    def test_wait_for_upload_transfer_promotes_a_verified_recent_draft(self) -> None:
        class UploadDialogStaleButDraftReady:
            def server_upload_progress(self, _video_id: str):
                return 100.0, ""
            def youtube_url(self) -> str:
                return "https://youtu.be/allocated_upload"

            def youtube_submission_complete(self) -> bool:
                return False

            def youtube_upload_progress(self) -> tuple[float, str]:
                return 96.0, ""

            def raise_for_page_error(self) -> None:
                return None

            def raise_for_upload_error(self) -> None:
                return None

        reports: list[str] = []
        url, complete = YouTubeStudioUploader._wait_for_upload_transfer_complete(
            UploadDialogStaleButDraftReady(),  # type: ignore[arg-type]
            lambda _percent, detail: reports.append(detail),
            threading.Event(),
            timeout=1.2,
            poll_interval=0.005,
            stable_for=0.01,
            stall_warn_after=0.01,
            completed_draft_probe=lambda: "https://youtu.be/allocated_upload",
        )

        self.assertEqual(url, "https://youtu.be/allocated_upload")
        self.assertTrue(complete)
        self.assertTrue(any("bản nháp đã tải xong" in detail for detail in reports))

    def test_wait_for_upload_transfer_handoffs_to_content_after_grace_period(self) -> None:
        class UploadDialogStillShowingStaleProgress:
            def server_upload_progress(self, _video_id: str):
                return 100.0, ""
            def youtube_url(self) -> str:
                return "https://youtu.be/allocated_handoff"

            def youtube_submission_complete(self) -> bool:
                return False

            def youtube_upload_progress(self) -> tuple[float, str]:
                return 96.0, ""

        reports: list[str] = []
        url, complete = YouTubeStudioUploader._wait_for_upload_transfer_complete(
            UploadDialogStillShowingStaleProgress(),  # type: ignore[arg-type]
            lambda _percent, detail: reports.append(detail),
            threading.Event(),
            timeout=1.2,
            poll_interval=0.005,
            stable_for=0.01,
            draft_handoff_after=0.01,
            completed_draft_probe=lambda: "https://youtu.be/allocated_handoff",
        )

        self.assertEqual(url, "https://youtu.be/allocated_handoff")
        self.assertTrue(complete)
        self.assertTrue(any("Nội dung" in detail for detail in reports))

    def test_wait_for_upload_transfer_rechecks_content_until_exact_draft_is_ready(self) -> None:
        class UploadDialogStillShowingStaleProgress:
            def server_upload_progress(self, _video_id: str):
                return 100.0, ""
            def youtube_url(self) -> str:
                return "https://youtu.be/periodic_handoff"

            def youtube_submission_complete(self) -> bool:
                return False

            def youtube_upload_progress(self) -> tuple[float, str]:
                return 96.0, ""

        probe_calls = 0
        reports: list[str] = []

        def probe_content() -> str:
            nonlocal probe_calls
            probe_calls += 1
            return (
                "https://youtu.be/periodic_handoff"
                if probe_calls >= 2
                else ""
            )

        url, complete = YouTubeStudioUploader._wait_for_upload_transfer_complete(
            UploadDialogStillShowingStaleProgress(),  # type: ignore[arg-type]
            lambda _percent, detail: reports.append(detail),
            threading.Event(),
            timeout=0.2,
            poll_interval=0.005,
            stable_for=0.01,
            draft_content_probe=probe_content,
            draft_probe_interval=0.02,
            draft_handoff_after=0.01,
        )

        self.assertEqual(url, "https://youtu.be/periodic_handoff")
        self.assertTrue(complete)
        self.assertGreaterEqual(probe_calls, 2)
        self.assertTrue(any("kiểm tra Nội dung #1" in detail for detail in reports))

    def test_exact_draft_resume_waits_for_lazily_loaded_content_row(self) -> None:
        controller = mock.MagicMock(spec=_SemanticCdpController)
        controller.wait_channel_id.return_value = 'channel'
        loaded = False
        reads = 0

        def rows(_controller):
            nonlocal loaded, reads
            reads += 1
            if reads == 1:
                return ()
            loaded = True
            return (_YouTubeDraftCandidate('Exact123', 'Draft', 'Đã tải lên 14% Draft Private', 0),)

        resume = mock.Mock(side_effect=lambda: loaded)
        with mock.patch.object(YouTubeStudioUploader, '_studio_draft_candidates', side_effect=rows):
            YouTubeStudioUploader._prepare_exact_draft_edit_target(
                controller, 'https://youtu.be/Exact123', threading.Event(), mock.Mock(),
                timeout=3, on_content_loaded=resume,
            )
        resume.assert_called_once()
        self.assertGreaterEqual(reads, 2)
        self.assertEqual(controller.navigate_and_wait.call_count, 2)

    def test_prepare_exact_draft_edit_target_checks_saved_id_in_content(self) -> None:
        controller = mock.MagicMock(spec=_SemanticCdpController)
        controller.wait_channel_id.return_value = "channel-123"
        reports: list[str] = []

        with mock.patch.object(
            YouTubeStudioUploader,
            "_studio_draft_candidates",
            return_value=(
                _YouTubeDraftCandidate(
                    "allocated_handoff",
                    "Draft",
                    "Draft Private",
                    0,
                ),
            ),
        ):
            YouTubeStudioUploader._prepare_exact_draft_edit_target(
                controller,
                "https://youtu.be/allocated_handoff",
                threading.Event(),
                lambda _percent, detail: reports.append(detail),
                timeout=1,
            )

        urls = [call.args[0] for call in controller.navigate_and_wait.call_args_list]
        self.assertEqual(urls[0], "https://studio.youtube.com/")
        self.assertIn("/videos/upload?filter=", urls[1])
        self.assertEqual(
            urls[2],
            "https://studio.youtube.com/video/allocated_handoff/edit",
        )
        self.assertTrue(any("allocated_handoff" in detail for detail in reports))

    def test_recovery_waits_with_bridge_only_controller_without_raw_cdp_probes(self) -> None:
        class BridgeOnlyUpload:
            cdp = None

            def __init__(self) -> None:
                self.reads = 0

            def youtube_url(self) -> str:
                return "https://youtu.be/recovered_upload"

            def youtube_submission_complete(self) -> bool:
                self.reads += 1
                return self.reads >= 2

            def youtube_upload_progress(self) -> tuple[float | None, str]:
                return (None, "") if self.reads >= 2 else (87.0, "19 giây")

            def raise_for_page_error(self) -> None:
                raise AssertionError("bridge-only recovery must not use raw CDP")

            def raise_for_upload_error(self) -> None:
                raise AssertionError("bridge-only recovery must not use raw CDP")

        controller = BridgeOnlyUpload()
        url, complete = YouTubeStudioUploader._wait_for_upload_transfer_complete(
            controller,  # type: ignore[arg-type]
            lambda _percent, _detail: None,
            threading.Event(),
            timeout=0.2,
            poll_interval=0.005,
            stable_for=0.01,
        )

        self.assertEqual(url, "https://youtu.be/recovered_upload")
        self.assertTrue(complete)

    def test_tag_chip_labels_match_saved_tags_without_locale_suffix_dependency(self) -> None:
        controller = _SemanticCdpController(None)  # type: ignore[arg-type]

        self.assertTrue(
            controller.tag_labels_match(
                (
                    "DocuRandom. Press enter to edit",
                    "日本の技術。Enterキーで編集",
                ),
                ("DocuRandom", "日本の技術"),
            )
        )
        self.assertFalse(
            controller.tag_labels_match(
                ("DocuRandom. Press enter to edit",),
                ("DocuRandom", "missing"),
            )
        )

    def test_managed_bridge_pairs_automatically_inside_exact_profile(self) -> None:
        class FakeBridge:
            def __init__(self) -> None:
                self.wait_calls = 0
                self.pair_profile = ""
                self.cancelled = False

            def wait_for_profile(self, profile_id, *, timeout):  # type: ignore[no-untyped-def]
                self.wait_calls += 1
                self.asserted_profile = profile_id
                return self.wait_calls > 1

            def begin_pairing(self, profile_id, *, ttl):  # type: ignore[no-untyped-def]
                self.pair_profile = profile_id

            def cancel_pairing(self):  # type: ignore[no-untyped-def]
                self.cancelled = True

            def command(self, profile_id, command, *, timeout):  # type: ignore[no-untyped-def]
                self.asserted_profile = profile_id
                self.asserted_command = command
                return {
                    "extensionVersion": "1.1.14",
                    "capabilities": [
                        "background-tab",
                        "youtube-set-text",
                        "youtube-native-edit-v2",
                        "youtube-tags-v1",
                        "youtube-tags-commit-v2",
                        "youtube-click",
                        "youtube-query",
                        "youtube-query-active-v2",
                        "youtube-active-controls-v2",
                        "youtube-upload-completion-v3",
                        "youtube-attached-video",
                        "youtube-content-state-v1",
                        "bridge-keepalive",
                    ],
                }

        bridge = FakeBridge()
        client = mock.MagicMock()

        def cdp_command(_profile_id, method, params):  # type: ignore[no-untyped-def]
            if method == "Target.createTarget":
                return {"targetId": "auto-pair-target"}
            if method == "Browser.getWindowForTarget":
                return {"windowId": 991}
            if method in {"Browser.setWindowBounds", "Target.closeTarget"}:
                return {"success": True}
            raise AssertionError(method)

        client.cdp_command.side_effect = cdp_command
        with mock.patch(
            "docurandom.youtube_upload.get_browser_extension_bridge",
            return_value=bridge,
        ):
            result = _ensure_youtube_browser_bridge(
                client,
                "profile-test",
                timeout=2,
            )

        self.assertIs(result, bridge)
        self.assertEqual(bridge.pair_profile, "profile-test")
        self.assertTrue(bridge.cancelled)
        create = next(
            call
            for call in client.cdp_command.call_args_list
            if call.args[1] == "Target.createTarget"
        )
        self.assertIn("chrome-extension://", create.args[2]["url"])
        self.assertIn("docurandom_pair=1", create.args[2]["url"])
        self.assertTrue(
            any(
                call.args[1] == "Target.closeTarget"
                for call in client.cdp_command.call_args_list
            )
        )

    def test_attached_video_probe_is_read_only_through_extension(self) -> None:
        class FakeBridge:
            def command(self, _profile_id, command, _params, *, timeout):  # type: ignore[no-untyped-def]
                self.last = (command, timeout)
                return {
                    "name": "Video 3.mp4",
                    "size": 123456789,
                    "type": "video/mp4",
                    "count": 1,
                }

        cdp = mock.Mock()
        bridge = FakeBridge()
        controller = _SemanticCdpController(
            cdp,
            browser_bridge=bridge,  # type: ignore[arg-type]
            bridge_profile_id="profile-test",
            bridge_tab_id=177,
        )

        attached = controller.attached_video()

        self.assertEqual(attached["name"], "Video 3.mp4")
        self.assertEqual(attached["size"], 123456789)
        cdp.command.assert_not_called()
        self.assertEqual(bridge.last[0], "youtube_attached_video")

    def test_upload_recovery_fails_closed_without_extension_transport(self) -> None:
        controller = _SemanticCdpController(mock.Mock())

        with self.assertRaisesRegex(
            YouTubeUploadError,
            "không rơi về Runtime.evaluate native",
        ):
            YouTubeStudioUploader._studio_draft_candidates(controller)

    def test_stable_dom_id_is_independent_of_visible_language(self) -> None:
        controller = _SemanticCdpController(None)  # type: ignore[arg-type]
        nodes = [
            {
                "nodeId": 10,
                "backendNodeId": 99,
                "nodeName": "YTCP-BUTTON",
                "attributes": [
                    "id",
                    "next-button",
                    "aria-label",
                    "次へ",
                ],
            }
        ]

        selected = controller.find_dom(nodes, attributes={"id": "next-button"})

        self.assertIsNotNone(selected)
        self.assertEqual(selected["backendNodeId"], 99)

    def test_native_upload_button_is_selected_inside_stable_wrapper(self) -> None:
        controller = _SemanticCdpController(None)  # type: ignore[arg-type]
        nodes = [
            {
                "nodeId": 10,
                "backendNodeId": 90,
                "nodeName": "YTCP-BUTTON",
                "attributes": ["id", "upload-button", "tabindex", "-1"],
            },
            {
                "nodeId": 11,
                "parentId": 10,
                "backendNodeId": 91,
                "nodeName": "YTCP-BUTTON-SHAPE",
                "attributes": [],
            },
            {
                "nodeId": 12,
                "parentId": 11,
                "backendNodeId": 92,
                "nodeName": "BUTTON",
                "attributes": ["aria-label", "動画をアップロード", "tabindex", "0"],
            },
        ]

        selected = controller.find_dom(
            nodes,
            node_names=("BUTTON",),
            ancestor_ids=("upload-button",),
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["backendNodeId"], 92)

    def test_laid_out_dom_filter_skips_newer_hidden_studio_template(self) -> None:
        cdp = mock.Mock()
        cdp.command.side_effect = lambda method, params=None: (
            {"model": {"width": 400}}
            if method == "DOM.getBoxModel" and params == {"backendNodeId": 101}
            else {}
        )
        controller = _SemanticCdpController(cdp)
        nodes = [
            {
                "nodeId": 1,
                "backendNodeId": 101,
                "nodeName": "DIV",
                "attributes": ["id", "textbox", "contenteditable", "true"],
            },
            {
                "nodeId": 2,
                "backendNodeId": 202,
                "nodeName": "DIV",
                "attributes": ["id", "textbox", "contenteditable", "true"],
            },
        ]

        selected = controller.find_dom(
            nodes,
            attributes={"id": "textbox", "contenteditable": "true"},
            require_laid_out=True,
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["backendNodeId"], 101)

    def test_upload_tab_uses_direct_channel_route_without_taking_active_tab(self) -> None:
        client = mock.MagicMock()
        client.cdp_command.side_effect = lambda _profile, command, _params: (
            {"windowId": 700}
            if command == "Browser.getWindowForTarget"
            else {
                "bounds": {
                    "windowState": "normal",
                    "left": -32000,
                    "top": -32000,
                    "width": 1280,
                    "height": 800,
                }
            }
            if command == "Browser.getWindowBounds"
            else {}
        )
        profile = DonutProfile("profile-test", "Test", "wayfern", process_id=123)
        session = mock.MagicMock()
        session.__enter__.return_value = mock.sentinel.cdp
        controller = mock.MagicMock(spec=_SemanticCdpController)
        controller.wait_channel_id.return_value = "UCdemo123"
        controller.channel_name.return_value = "Demo channel"
        controller.upload_started = False

        with (
            mock.patch(
                "docurandom.youtube_upload._mcp_open_minimized_cdp_window_ws_url",
                return_value="ws://127.0.0.1:9222/devtools/page/upload-target",
            ),
            mock.patch("docurandom.youtube_upload._CdpSession", return_value=session),
            mock.patch(
                "docurandom.youtube_upload._SemanticCdpController",
                return_value=controller,
            ),
            mock.patch(
                "docurandom.youtube_upload._mcp_close_cdp_page_target",
                return_value=True,
            ),
            mock.patch(
                "docurandom.youtube_upload._browser_extension_tab_for_target",
                return_value=(mock.sentinel.browser_bridge, 177),
            ),
            mock.patch(
                "docurandom.youtube_upload._ensure_youtube_browser_bridge",
                return_value=mock.sentinel.browser_bridge,
            ),
        ):
            with _prepared_youtube_upload_tab(
                client,
                profile,
                {"targets_url": "http://127.0.0.1:9222/json"},
                lambda _percent, _message: None,
                threading.Event(),
            ) as prepared:
                self.assertEqual(prepared, (controller, "Demo channel"))

        navigations = [call.args[0] for call in controller.navigate_and_wait.call_args_list]
        self.assertEqual(navigations[0], "https://studio.youtube.com/")
        self.assertEqual(
            navigations[1],
            "https://studio.youtube.com/channel/UCdemo123/videos/upload?d=ud",
        )
        controller.wait_file_input.assert_called_once_with(
            "video",
            timeout=60,
            cancel_event=mock.ANY,
        )
        controller.wait_laid_out_dom.assert_called_once_with(
            attributes={"id": "dialog"},
            node_names=("YTCP-DIALOG",),
            timeout=60,
            cancel_event=mock.ANY,
        )
        self.assertIn(
            mock.call(
                "profile-test",
                "Browser.setWindowBounds",
                {
                    "windowId": 700,
                    "bounds": {
                        "left": -32000,
                        "top": -32000,
                        "width": 1280,
                        "height": 800,
                    },
                },
            ),
            client.cdp_command.call_args_list,
        )
        controller.click.assert_not_called()

    def test_metadata_mismatch_stops_instead_of_clicking_next(self) -> None:
        class FakeCdp:
            def command(
                self, method: str, _params: dict[str, object] | None = None
            ) -> dict[str, object]:
                if method == "Accessibility.getPartialAXTree":
                    return {"nodes": [{"value": {"value": "wrong value"}}]}
                return {}

        controller = _SemanticCdpController(FakeCdp())  # type: ignore[arg-type]

        with self.assertRaisesRegex(YouTubeUploadError, "metadata có thể sai"):
            controller.set_text(
                {"backendNodeId": 44},
                "expected value",
                verify_timeout=0.01,
            )

    def test_video_input_never_falls_back_to_thumbnail_input(self) -> None:
        controller = _SemanticCdpController(None)  # type: ignore[arg-type]
        thumbnail_only = [
            {
                "nodeId": 1,
                "backendNodeId": 10,
                "nodeName": "INPUT",
                "attributes": [
                    "id",
                    "file-loader",
                    "type",
                    "file",
                    "accept",
                    "image/jpeg,image/png",
                ],
            }
        ]
        youtube_video_input = {
            "nodeId": 2,
            "backendNodeId": 20,
            "nodeName": "INPUT",
            "attributes": [
                "type",
                "file",
                "name",
                "Filedata",
                "multiple",
                "",
            ],
        }

        self.assertIsNone(controller.find_file_input(thumbnail_only, "video"))
        self.assertEqual(
            controller.find_file_input(thumbnail_only, "image")["backendNodeId"],
            10,
        )
        self.assertEqual(
            controller.find_file_input(thumbnail_only + [youtube_video_input], "video")[
                "backendNodeId"
            ],
            20,
        )

    def test_proxy_error_page_is_reported_explicitly(self) -> None:
        class FakeCdp:
            def command(
                self, method: str, _params: dict[str, object] | None = None
            ) -> dict[str, object]:
                if method == "Page.getFrameTree":
                    return {
                        "frameTree": {
                            "frame": {
                                "url": "chrome-error://chromewebdata/",
                                "unreachableUrl": "https://studio.youtube.com/",
                            }
                        }
                    }
                if method == "Accessibility.getFullAXTree":
                    return {
                        "nodes": [
                            {
                                "name": {"value": "ERR_PROXY_CONNECTION_FAILED"},
                                "value": {"value": ""},
                            }
                        ]
                    }
                return {}

        controller = _SemanticCdpController(FakeCdp())  # type: ignore[arg-type]

        with self.assertRaisesRegex(YouTubeUploadError, "Proxy của profile"):
            controller.raise_for_page_error()

    def test_logged_out_redirect_is_reported_before_page_actions(self) -> None:
        class FakeCdp:
            def command(
                self, method: str, _params: dict[str, object] | None = None
            ) -> dict[str, object]:
                if method == "Page.getFrameTree":
                    return {
                        "frameTree": {
                            "frame": {
                                "url": (
                                    "https://accounts.google.com/ServiceLogin?continue="
                                    "https%3A%2F%2Fstudio.youtube.com%2F&secret=hidden"
                                )
                            }
                        }
                    }
                return {}

        controller = _SemanticCdpController(FakeCdp())  # type: ignore[arg-type]

        with self.assertRaisesRegex(YouTubeUploadError, "mất phiên đăng nhập") as raised:
            controller.raise_for_page_error()
        self.assertNotIn("secret=hidden", str(raised.exception))

    def test_rejects_remote_cdp_target_list(self) -> None:
        with self.assertRaisesRegex(YouTubeUploadError, "không nằm trên máy này"):
            _mcp_cdp_page_ws_url(
                {"targets_url": "http://example.com:9222/json"},
                timeout=0.01,
            )

    def test_visible_upload_alert_is_forwarded_in_any_language(self) -> None:
        class FakeCdp:
            def command(
                self, method: str, _params: dict[str, object] | None = None
            ) -> dict[str, object]:
                if method == "Accessibility.getFullAXTree":
                    return {
                        "nodes": [
                            {
                                "ignored": False,
                                "role": {"value": "alert"},
                                "name": {"value": "アップロードに失敗しました"},
                            }
                        ]
                    }
                return {}

        controller = _SemanticCdpController(FakeCdp())  # type: ignore[arg-type]
        controller.mark_upload_started()

        with self.assertRaisesRegex(YouTubeUploadError, "アップロードに失敗しました"):
            controller.raise_for_upload_error()


class ProfileUploadCoordinatorTests(unittest.TestCase):
    def test_completed_metadata_can_finish_during_another_file_transfer(self):
        coordinator = ProfileUploadCoordinator()
        transfer_cancel = threading.Event()
        transfer = coordinator.acquire('uploading', transfer_cancel)
        metadata = coordinator.acquire('transferred', threading.Event(), job_kind='metadata-recovery', preempt_lower=False)
        self.assertFalse(transfer_cancel.is_set())
        self.assertIsNone(coordinator.try_acquire('monitor', threading.Event()))
        acquired = threading.Event()
        def next_file():
            with coordinator.acquire('new-file', threading.Event()): acquired.set()
        worker = threading.Thread(target=next_file)
        worker.start()
        self.assertFalse(acquired.wait(.05))
        transfer.__exit__(None, None, None)
        self.assertFalse(acquired.wait(.05))
        metadata.__exit__(None, None, None)
        self.assertTrue(acquired.wait(1))
        worker.join(1)

    def test_metadata_never_overlaps_same_profile_or_second_metadata_job(self):
        coordinator = ProfileUploadCoordinator()
        transfer = coordinator.acquire('profile', threading.Event())
        cancel = threading.Event()
        acquired = threading.Event()
        def same_profile():
            try:
                with coordinator.acquire('profile', cancel, job_kind='metadata-recovery', preempt_lower=False): acquired.set()
            except YouTubeUploadCancelled: pass
        worker = threading.Thread(target=same_profile)
        worker.start()
        self.assertFalse(acquired.wait(.05))
        cancel.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        transfer.__exit__(None, None, None)
        first = coordinator.acquire('one', threading.Event(), job_kind='metadata-recovery', preempt_lower=False)
        def second_metadata():
            with coordinator.acquire('two', threading.Event(), job_kind='metadata-recovery', preempt_lower=False): acquired.set()
        worker = threading.Thread(target=second_metadata)
        worker.start()
        self.assertFalse(acquired.wait(.05))
        first.__exit__(None, None, None)
        self.assertTrue(acquired.wait(1))
        worker.join(1)

    def test_same_profile_uploads_are_fifo(self) -> None:
        coordinator = ProfileUploadCoordinator(
            max_parallel_uploads=2, upload_start_interval=0
        )
        first_cancel = threading.Event()
        first = coordinator.acquire("profile-a", first_cancel)
        acquired: list[str] = []

        def wait_for_same() -> None:
            with coordinator.acquire("profile-a", threading.Event()):
                acquired.append("same")

        thread = threading.Thread(target=wait_for_same)
        thread.start()
        time.sleep(0.05)
        self.assertEqual(acquired, [])
        first.__exit__(None, None, None)
        thread.join(timeout=2)
        self.assertEqual(acquired, ["same"])

    def test_different_profiles_wait_for_global_upload_lane(self) -> None:
        coordinator = ProfileUploadCoordinator(
            max_parallel_uploads=1, upload_start_interval=0
        )
        first = coordinator.acquire("profile-a", threading.Event())
        acquired: list[str] = []

        def wait_for_other() -> None:
            with coordinator.acquire("profile-b", threading.Event()):
                acquired.append("other")

        thread = threading.Thread(target=wait_for_other)
        thread.start()
        time.sleep(0.05)
        self.assertEqual(acquired, [])
        first.__exit__(None, None, None)
        thread.join(timeout=2)
        self.assertEqual(acquired, ["other"])

    def test_cancel_removes_waiting_ticket(self) -> None:
        coordinator = ProfileUploadCoordinator(
            max_parallel_uploads=1, upload_start_interval=0
        )
        first = coordinator.acquire("profile-a", threading.Event())
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(YouTubeUploadCancelled):
            coordinator.acquire("profile-a", cancel)
        first.__exit__(None, None, None)

    def test_periodic_monitor_skips_profile_instead_of_queueing(self) -> None:
        coordinator = ProfileUploadCoordinator(
            max_parallel_uploads=1, upload_start_interval=0
        )
        upload = coordinator.acquire("profile-a", threading.Event())

        monitor = coordinator.try_acquire(
            "profile-a",
            threading.Event(),
            job_kind="monitor",
            priority=0,
        )

        self.assertIsNone(monitor)
        upload.__exit__(None, None, None)

    def test_periodic_monitor_on_other_profile_skips_global_upload(self) -> None:
        coordinator = ProfileUploadCoordinator(
            max_parallel_uploads=1, upload_start_interval=0
        )
        upload = coordinator.acquire("profile-a", threading.Event())

        monitor = coordinator.try_acquire(
            "profile-b",
            threading.Event(),
            job_kind="monitor",
            priority=0,
        )

        self.assertIsNone(monitor)
        upload.__exit__(None, None, None)

    def test_only_one_profile_monitor_can_run_at_once(self) -> None:
        coordinator = ProfileUploadCoordinator(
            max_parallel_uploads=4,
            max_parallel_monitors=4,
            upload_start_interval=0,
        )
        first = coordinator.try_acquire("profile-a", threading.Event())
        second = coordinator.try_acquire("profile-b", threading.Event())

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIsNone(
            coordinator.try_acquire("profile-c", threading.Event())
        )

        assert first is not None
        first.__exit__(None, None, None)
        second = coordinator.try_acquire("profile-b", threading.Event())
        self.assertIsNotNone(second)
        assert second is not None
        second.__exit__(None, None, None)

    def test_upload_preempts_active_monitor_at_safe_cancel_checkpoint(self) -> None:
        coordinator = ProfileUploadCoordinator(
            max_parallel_uploads=1, upload_start_interval=0
        )
        monitor_cancel = threading.Event()
        monitor = coordinator.try_acquire(
            "profile-a",
            monitor_cancel,
            job_kind="monitor",
            priority=0,
        )
        self.assertIsNotNone(monitor)
        acquired: list[str] = []

        def wait_for_upload() -> None:
            with coordinator.acquire(
                "profile-a",
                threading.Event(),
                job_kind="upload",
                priority=100,
                preempt_lower=True,
            ):
                acquired.append("upload")

        thread = threading.Thread(target=wait_for_upload)
        thread.start()
        self.assertTrue(monitor_cancel.wait(timeout=1.0))
        self.assertEqual(acquired, [])
        assert monitor is not None
        monitor.__exit__(None, None, None)
        thread.join(timeout=2.0)
        self.assertEqual(acquired, ["upload"])

    def test_next_upload_waits_for_previous_upload_and_settle_interval(self) -> None:
        coordinator = ProfileUploadCoordinator(
            max_parallel_uploads=4, upload_start_interval=0.12
        )
        first_started_at = time.monotonic()
        first = coordinator.acquire("profile-a", threading.Event())
        acquired_at: list[float] = []

        def wait_for_other() -> None:
            with coordinator.acquire("profile-b", threading.Event()):
                acquired_at.append(time.monotonic())

        thread = threading.Thread(target=wait_for_other)
        thread.start()
        time.sleep(0.02)
        self.assertEqual(acquired_at, [])
        first.__exit__(None, None, None)
        thread.join(timeout=2.0)

        self.assertEqual(len(acquired_at), 1)
        self.assertGreaterEqual(acquired_at[0] - first_started_at, 0.09)

    def test_different_profiles_upload_one_at_a_time_even_if_more_lanes_are_requested(self) -> None:
        coordinator = ProfileUploadCoordinator(
            max_parallel_uploads=4, upload_start_interval=0
        )
        first = coordinator.acquire("profile-a", threading.Event())
        acquired = threading.Event()

        def wait_for_other() -> None:
            with coordinator.acquire("profile-b", threading.Event()):
                acquired.set()

        thread = threading.Thread(target=wait_for_other)
        thread.start()
        time.sleep(0.05)
        self.assertFalse(acquired.is_set())
        first.__exit__(None, None, None)
        self.assertTrue(acquired.wait(1.0))
        thread.join(timeout=1.0)

    def test_capacity_provider_cannot_raise_single_upload_lane(self) -> None:
        coordinator = ProfileUploadCoordinator(
            max_parallel_uploads=4,
            upload_start_interval=0,
            capacity_provider=lambda: 4,
        )
        first = coordinator.acquire("profile-a", threading.Event())
        acquired = threading.Event()

        def wait_for_other() -> None:
            with coordinator.acquire("profile-b", threading.Event()):
                acquired.set()

        thread = threading.Thread(target=wait_for_other)
        thread.start()
        time.sleep(0.05)
        self.assertFalse(acquired.is_set())
        first.__exit__(None, None, None)
        self.assertTrue(acquired.wait(1.0))
        thread.join(timeout=1.0)


class YouTubeIdempotencyTests(unittest.TestCase):
    def test_restart_waits_for_durable_transfer_before_new_upload_takes_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / 'video.mp4'
            package = workspace.output_dir / 'video.txt'
            video.write_bytes(b'new video')
            package.write_text('TIÊU ĐỀ\n\nDemo video\n\nMÔ TẢ\n\nSample text\n', encoding='utf-8-sig')
            Image.new("RGB", (1280, 720), "blue").save(workspace.output_dir / "thumbnail.png")
            package.write_text(package.read_text(encoding="utf-8-sig") + "\nSEO TAGS\n\ndemo\n", encoding="utf-8-sig")
            request = YouTubeUploadRequest(workspace, DonutMcpEndpoint.parse('http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv'), 'new-profile', video_path=video, package_path=package)
            coordinator = mock.MagicMock()
            uploader = YouTubeStudioUploader(registry_path=Path(temp)/'registry.json', coordinator=coordinator)
            old_workspace = _workspace(Path(temp)/'old-workspace')
            original = {'status': 'uploading', 'attempt_id': 'original-attempt', 'profile_id': 'old-profile', 'video_path': str(old_workspace.output_dir/'old.mp4'), 'youtube_url': 'https://youtu.be/Old123', 'transfer_complete': False}
            (old_workspace.state_dir/'youtube_upload.json').write_text(json.dumps({'identity': 'old-identity', 'attempt_id': 'original-attempt'}), encoding='utf-8')
            uploader._registry_set('old-identity', original)
            waiting = threading.Event()
            cancel = threading.Event()
            errors = []

            def run_new_upload():
                try:
                    uploader.upload(request, progress=lambda _percent, message: waiting.set() if 'Đang chờ nối' in message else None, cancel_event=cancel)
                except Exception as exc:
                    errors.append(exc)

            with (
                mock.patch('docurandom.youtube_upload._wait_for_render_upload_ready'),
                mock.patch.object(uploader, '_upload_locked', return_value=mock.Mock()) as attach,
            ):
                thread = threading.Thread(target=run_new_upload)
                thread.start()
                try:
                    self.assertTrue(waiting.wait(2.0), repr(errors))
                    coordinator.acquire.assert_not_called()
                    attach.assert_not_called()
                    self.assertEqual(uploader._registry_get('old-identity'), original)
                    uploader._registry_set('old-identity', {**original, 'transfer_complete': True})
                    thread.join(2.0)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])
                    coordinator.acquire.assert_called_once()
                    attach.assert_called_once()
                finally:
                    cancel.set()
                    thread.join(2.0)

    def test_restart_transfer_wait_is_cancellable_and_preserves_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            uploader = YouTubeStudioUploader(registry_path=Path(temp)/'registry.json')
            old_workspace = _workspace(Path(temp)/'old-workspace')
            original = {'status': 'submitting', 'attempt_id': 'same-attempt', 'video_path': str(old_workspace.output_dir/'old.mp4'), 'transfer_complete': False}
            (old_workspace.state_dir/'youtube_upload.json').write_text(json.dumps({'identity': 'old', 'attempt_id': 'same-attempt'}), encoding='utf-8')
            uploader._registry_set('old', original)
            cancel = threading.Event()
            with self.assertRaises(YouTubeUploadCancelled):
                uploader._wait_for_existing_file_transfers('new', cancel, lambda *_: cancel.set())
            self.assertEqual(uploader._registry_get('old'), original)

    def test_restart_wait_allows_confirmed_or_explicitly_interrupted_transfers(self):
        with tempfile.TemporaryDirectory() as temp:
            uploader = YouTubeStudioUploader(registry_path=Path(temp)/'registry.json')
            uploader._registry_set('same', {'status': 'uploading', 'attempt_id': 'own-attempt'})
            for record in (
                {'status': 'complete', 'attempt_id': 'done'},
                {'status': 'uploading', 'attempt_id': 'done', 'transfer_complete': True},
                {'status': 'uploading', 'attempt_id': 'resume', 'transfer_requires_content_resume': True},
                {'status': 'uncertain', 'attempt_id': 'interrupted'},
            ):
                with self.subTest(record=record):
                    uploader._registry_set('other', record)
                    report = mock.Mock()
                    uploader._wait_for_existing_file_transfers('same', threading.Event(), report)
                    report.assert_not_called()

    def test_historical_upload_claim_does_not_reserve_current_transfer_lane(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = _workspace(Path(temp)/'workspace')
            uploader = YouTubeStudioUploader(registry_path=Path(temp)/'registry.json')
            history = {'status': 'uploading', 'attempt_id': 'historical-attempt', 'video_path': str(workspace.output_dir/'old.mp4')}
            uploader._registry_set('historical-identity', history)
            (workspace.state_dir/'youtube_upload.json').write_text(json.dumps({'identity': 'current-identity', 'attempt_id': 'current-attempt'}), encoding='utf-8')
            report = mock.Mock()
            uploader._wait_for_existing_file_transfers('new', threading.Event(), report)
            report.assert_not_called()
            self.assertEqual(uploader._registry_get('historical-identity'), history)

    def test_claim_lookup_preserves_active_guard_during_registry_sharing_violation(self):
        uploader = YouTubeStudioUploader(registry_path=Path('managed')/'registry.json')
        record = {'workspace_id': 'workspace', 'attempt_id': 'active-attempt'}
        registry_text = json.dumps({'workspaces': [{'workspace_id': 'workspace', 'root_dir': 'workspace-root'}]})
        checkpoint_text = json.dumps({'identity': 'active-identity', 'attempt_id': 'active-attempt'})
        with (
            mock.patch.object(Path, 'read_text', side_effect=[PermissionError('sharing violation'), PermissionError('sharing violation'), registry_text, checkpoint_text]),
            mock.patch('docurandom.youtube_upload.time.sleep'),
        ):
            self.assertTrue(uploader._upload_claim_matches_current_checkpoint('active-identity', record))
        for error in (PermissionError('permanent denial'), OSError('disk failure')):
            with (
                self.subTest(error=error),
                mock.patch.object(Path, 'read_text', side_effect=error),
                mock.patch('docurandom.youtube_upload.time.sleep'),
            ):
                with self.assertRaisesRegex(YouTubeUploadError, 'Không đọc được danh sách workspace'):
                    uploader._upload_claim_matches_current_checkpoint('active-identity', record)
        with mock.patch.object(Path, 'read_text', return_value='{broken json'):
            with self.assertRaisesRegex(YouTubeUploadError, 'Không đọc được danh sách workspace'):
                uploader._upload_claim_matches_current_checkpoint('active-identity', record)

    def test_only_a_proven_transferred_video_uses_metadata_lane(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / 'video.mp4'
            package = workspace.output_dir / 'video.txt'
            video.write_bytes(b'video')
            package.write_text('TIÊU ĐỀ\n\nDemo video\n\nMÔ TẢ\n\nSample text\n', encoding='utf-8-sig')
            request = YouTubeUploadRequest(workspace, DonutMcpEndpoint.parse('http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv'), 'profile', video_path=video, package_path=package)
            sources = resolve_workspace_upload_sources(workspace, video_path=video, package_path=package)
            coordinator = mock.MagicMock()
            uploader = YouTubeStudioUploader(registry_path=Path(temp)/'registry.json', coordinator=coordinator)
            identity = uploader._request_identity(request, sources)
            for transferred in (False, True):
                with self.subTest(transferred=transferred):
                    record = {'status': 'uncertain', 'attempt_id': 'same', 'profile_id': 'profile', 'youtube_url': 'https://youtu.be/Exact123', 'transfer_complete': transferred}
                    uploader._registry_set(identity, record)
                    uploader._checkpoint(request, sources, status='uncertain', progress=97, detail='saved draft', extra=record)
                    with mock.patch.object(uploader, '_reconcile_submitted_locked', return_value=mock.Mock()), mock.patch.object(uploader, '_upload_locked') as attach:
                        uploader.upload(request)
                        attach.assert_not_called()
                    self.assertEqual(coordinator.acquire.call_args.kwargs, {'job_kind': 'metadata-recovery', 'preempt_lower': False} if transferred else {'priority': 150})

    def test_metadata_lane_cannot_resume_file_transfer_from_content_row(self):
        uploader = YouTubeStudioUploader()
        controller = mock.Mock()
        with self.assertRaises(YouTubeUploadUncertain):
            uploader._resume_existing_content_upload_locked(
                mock.Mock(), mock.Mock(), {'metadata_only_recovery': True}, mock.Mock(), 'channel', 'identity', 'attempt', controller, mock.Mock(), threading.Event(),
            )
        controller.set_file.assert_not_called()
        controller.content_upload_state.assert_not_called()

    def test_metadata_recovery_never_falls_back_to_file_attachment_after_lost_lease(self):
        uploader = YouTubeStudioUploader()
        request = mock.Mock()
        with (
            mock.patch.object(uploader, '_registry_get', return_value={}),
            mock.patch.object(uploader, '_checkpoint_path', return_value=Path('no-checkpoint.json')),
            mock.patch.object(uploader, '_upload_locked') as attach,
        ):
            error = YouTubeUploadError('connection reset')
            with self.assertRaises(YouTubeUploadError):
                uploader._recover_locked_upload(request, mock.Mock(), 'lease', mock.Mock(), threading.Event(), error, metadata_only=True)
            attach.assert_not_called()

    def test_initial_profile_launch_error_releases_only_before_attachment(self):
        uploader = YouTubeStudioUploader()
        request = mock.Mock(profile_id='profile', endpoint=mock.Mock(), manual_profile_recheck=False)
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.get_profile.side_effect = [
            DonutProfile('profile', 'name', 'wayfern'),
            DonutProfile('profile', 'name', 'wayfern', process_id=42),
        ]
        client.run_profile.return_value = 'launched'
        client.get_debug_connection.side_effect = YouTubeUploadError('connection failed')
        with (
            mock.patch('docurandom.youtube_upload.DonutMcpClient', return_value=client),
            mock.patch.object(uploader, '_request_identity', return_value='lease'),
            mock.patch.object(uploader, '_checkpoint'),
            mock.patch.object(uploader, '_registry_get', return_value={'status': 'queued'}),
            mock.patch.object(uploader, '_registry_claim') as claim,
            mock.patch.object(uploader, '_close_profile_started_by_workflow') as close,
        ):
            with self.assertRaisesRegex(YouTubeUploadError, 'connection failed'):
                uploader._upload_locked(request, mock.Mock(), mock.Mock(), threading.Event())
            close.assert_called_once_with(client, 'profile')
            claim.assert_not_called()

    def test_closed_draft_reopens_channel_bootstrap_without_reattaching_file(self):
        for transferred, expected_url in (
            (False, 'https://studio.youtube.com/'),
            (True, 'https://studio.youtube.com/video/Exact123/edit'),
        ):
            with self.subTest(transferred=transferred):
                uploader = YouTubeStudioUploader()
                request = mock.Mock(profile_id='profile', endpoint=mock.Mock(), manual_profile_recheck=False)
                sources = mock.Mock()
                sources.metadata.title = 'Original title'
                record = {'status': 'uncertain', 'attempt_id': 'same', 'profile_id': 'profile',
                          'youtube_url': 'https://youtu.be/Exact123', 'transfer_complete': transferred}
                original = dict(record)
                client = mock.MagicMock()
                client.__enter__.return_value = client
                client.get_profile.side_effect = [
                    DonutProfile('profile', 'name', 'wayfern'),
                    DonutProfile('profile', 'name', 'wayfern', process_id=42),
                ]
                client.run_profile.return_value = 'launched'
                client.get_debug_connection.side_effect = YouTubeUploadError('stop before any save/file input')
                with (
                    mock.patch('docurandom.youtube_upload.DonutMcpClient', return_value=client),
                    mock.patch.object(uploader, '_request_identity', return_value='lease'),
                    mock.patch.object(uploader, '_source_video_duration_seconds', return_value=None),
                    mock.patch.object(uploader, '_registry_get', return_value=record),
                    mock.patch.object(uploader, '_registry_update_attempt'),
                    mock.patch.object(uploader, '_upload_locked') as attach,
                ):
                    with self.assertRaisesRegex(YouTubeUploadError, 'stop before any save/file input'):
                        uploader._reconcile_submitted_locked_inner(
                            request, sources, record, mock.Mock(), threading.Event(), ownership={'opened': False},
                        )
                    client.run_profile.assert_called_once_with('profile', expected_url, retry_entitlement_after_user_action=False)
                    attach.assert_not_called()
                    self.assertEqual(record, original)

    def test_persisted_ownership_releases_completed_profile_after_reuse(self):
        uploader = YouTubeStudioUploader()
        request = mock.Mock(profile_id='owned', endpoint=mock.Mock())
        client = mock.MagicMock()
        client.__enter__.return_value = client
        with (
            mock.patch.object(uploader, '_request_identity', return_value='lease'),
            mock.patch.object(uploader, '_registry_get') as record,
            mock.patch.object(uploader, '_close_profile_started_by_workflow') as close,
            mock.patch('docurandom.youtube_upload.DonutMcpClient', return_value=client),
        ):
            for status in ('complete', 'deferred_public', 'uncertain', 'uploading'):
                record.return_value = {'status': status, 'profile_started_by_workflow': True}
                uploader._close_completed_recovery_profile(request, mock.Mock(), {'opened': False})
                if status in ('complete', 'deferred_public'):
                    close.assert_called_once_with(client, 'owned')
                else:
                    close.assert_not_called()
                close.reset_mock()
            record.return_value = {'status': 'complete'}
            uploader._close_completed_recovery_profile(request, mock.Mock(), {'opened': False})
            close.assert_not_called()

    def test_independent_readback_requires_proven_transfer(self):
        uploader = YouTubeStudioUploader()
        with (
            mock.patch.object(uploader, '_request_identity', return_value='lease'),
            mock.patch.object(uploader, '_registry_get', return_value={'transfer_complete': False}),
            mock.patch('docurandom.youtube_upload.DonutMcpClient') as client,
        ):
            with self.assertRaises(YouTubeUploadUncertain):
                with uploader._metadata_readback_controller(
                    mock.Mock(), mock.Mock(), mock.Mock(), 'https://youtu.be/Exact123',
                    threading.Event(), force_fresh=True,
                ):
                    self.fail('Unfinished transfer must not be confirmed')
            client.assert_not_called()

    def test_independent_readback_closes_only_its_new_target(self):
        uploader = YouTubeStudioUploader()
        request = mock.Mock(profile_id='profile', endpoint=mock.Mock())
        original = mock.Mock(automation_target_id='original-upload')
        original.navigate_and_wait.side_effect = YouTubeUploadUncertain('unsaved')
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.get_profile.return_value = DonutProfile('profile', 'name', 'wayfern', process_id=42)
        with (
            mock.patch.object(uploader, '_request_identity', return_value='lease'),
            mock.patch.object(uploader, '_registry_get', return_value={'transfer_complete': True}),
            mock.patch('docurandom.youtube_upload.DonutMcpClient', return_value=client),
            mock.patch('docurandom.youtube_upload._mcp_open_minimized_cdp_window_ws_url',
                       return_value='ws://localhost:9222/devtools/page/verification') as open_target,
            mock.patch('docurandom.youtube_upload._browser_extension_tab_for_target', return_value=(mock.Mock(), 22)),
            mock.patch('docurandom.youtube_upload._CdpSession'),
            mock.patch('docurandom.youtube_upload._mcp_set_cdp_window_state') as park,
            mock.patch('docurandom.youtube_upload._SemanticCdpController') as controller,
            mock.patch('docurandom.youtube_upload._mcp_close_cdp_page_target') as close_target,
        ):
            with self.assertRaisesRegex(ValueError, 'verification failed'):
                with uploader._metadata_readback_controller(
                    request, mock.Mock(), original, 'https://youtu.be/Exact123', threading.Event(),
                ) as readback:
                    self.assertIs(readback, controller.return_value)
                    raise ValueError('verification failed')
            self.assertEqual(open_target.call_args.args[-1], 'https://studio.youtube.com/video/Exact123/edit')
            self.assertEqual(close_target.call_args.args[-1], 'verification')
            self.assertEqual(park.call_args.args[-2:], ('verification', 'parked'))
            client.run_profile.assert_not_called()
            original.set_file.assert_not_called()

    def test_trusted_pointer_save_uses_exact_dom_box(self):
        cdp = mock.Mock()
        cdp.command.return_value = {'model': {'content': [10, 20, 30, 20, 30, 40, 10, 40]}}
        controller = _SemanticCdpController(cdp)
        with mock.patch.object(controller, 'prepare_trusted_input'):
            controller.click_dom({'backendDOMNodeId': 17})
        self.assertEqual(cdp.command.call_args_list[0], mock.call('DOM.getBoxModel', {'backendNodeId': 17}))
        events = [call.args[1] for call in cdp.command.call_args_list[1:]]
        self.assertEqual([event['type'] for event in events], ['mouseMoved', 'mousePressed', 'mouseReleased'])
        self.assertEqual([event['buttons'] for event in events], [0, 1, 0])
        self.assertTrue(all((event['x'], event['y']) == (20, 30) for event in events))

    def test_save_pointer_fallback_is_bounded_to_one_click(self):
        controller = mock.Mock()
        controller.find_dom.return_value = {'backendDOMNodeId': 17}
        controller._dom_attributes.return_value = {}
        with (
            mock.patch('docurandom.youtube_upload.time.monotonic', side_effect=iter(i / 2 for i in range(100))),
            mock.patch('docurandom.youtube_upload.time.sleep'),
        ):
            self.assertFalse(YouTubeStudioUploader._wait_edit_save_complete(controller, threading.Event(), timeout=7))
        controller.click_dom.assert_called_once_with({'backendDOMNodeId': 17})

    def test_completed_recovery_releases_only_its_own_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomRecoveryClose_") as temp:
            uploader = YouTubeStudioUploader(
                registry_path=Path(temp) / "youtube-registry.json"
            )
            request = mock.Mock(profile_id="profile-test", endpoint=mock.Mock())
            sources = mock.Mock()
            client = mock.MagicMock()
            client.__enter__.return_value = client

            with (
                mock.patch.object(uploader, "_request_identity", return_value="video-key"),
                mock.patch.object(uploader, "_registry_get") as registry_get,
                mock.patch.object(uploader, "_close_profile_started_by_workflow") as close,
                mock.patch("docurandom.youtube_upload.DonutMcpClient", return_value=client),
            ):
                for status in ("complete", "deferred_public"):
                    with self.subTest(status=status):
                        registry_get.return_value = {"status": status}
                        uploader._close_completed_recovery_profile(
                            request, sources, {"opened": True}
                        )
                        close.assert_called_once_with(client, "profile-test")
                        close.reset_mock()

                registry_get.return_value = {"status": "uncertain"}
                uploader._close_completed_recovery_profile(
                    request, sources, {"opened": True}
                )
                uploader._close_completed_recovery_profile(
                    request, sources, {"opened": False}
                )
                close.assert_not_called()

    def test_recovery_wrappers_release_only_after_proven_completion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomRecoveryWrapper_") as temp:
            uploader = YouTubeStudioUploader(
                registry_path=Path(temp) / "youtube-registry.json"
            )
            request = mock.Mock()
            sources = mock.Mock()
            record: dict[str, object] = {}
            report = mock.Mock()
            cancel = threading.Event()
            expected = mock.Mock()

            for wrapper_name, inner_name in (
                ("_resume_attached_locked", "_resume_attached_locked_inner"),
                ("_reconcile_submitted_locked", "_reconcile_submitted_locked_inner"),
            ):
                with self.subTest(wrapper=wrapper_name):
                    def finish(*args, ownership, **kwargs):  # type: ignore[no-untyped-def]
                        ownership["opened"] = True
                        return expected

                    with (
                        mock.patch.object(uploader, inner_name, side_effect=finish),
                        mock.patch.object(
                            uploader, "_close_completed_recovery_profile"
                        ) as close,
                    ):
                        result = getattr(uploader, wrapper_name)(
                            request, sources, record, report, cancel
                        )
                        self.assertIs(result, expected)
                        close.assert_called_once_with(
                            request, sources, {"opened": True}
                        )

    def test_lost_tab_draft_requires_one_name_and_duration_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubeDraftMatch_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "DaiTa16.mp4"
            package = workspace.output_dir / "DaiTa16.txt"
            video.write_bytes(b"video")
            package.write_text(
                "TIÊU ĐỀ\n\nTiêu đề hoàn chỉnh\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            sources = resolve_workspace_upload_sources(
                workspace,
                video_path=video,
                package_path=package,
            )
            matching = _YouTubeDraftCandidate(
                "abcDEF_123",
                "Video thumbnail: DaiTa16",
                "DaiTa16 Draft Private 08:37 Sep 10, 2026",
                0,
            )
            wrong_duration = _YouTubeDraftCandidate(
                "wrong_123",
                "DaiTa16",
                "DaiTa16 Draft Private 02:15 Sep 10, 2026",
                1,
            )

            selected = YouTubeStudioUploader._select_unique_recent_draft(
                (matching, wrong_duration),
                sources,
                expected_duration=517.0,
                expected_date_markers=("sep 10, 2026",),
            )

            self.assertEqual(selected, matching)
            self.assertIsNone(
                YouTubeStudioUploader._select_unique_recent_draft(
                    (
                        matching,
                        _YouTubeDraftCandidate(
                            "duplicate1",
                            "DaiTa16",
                            "DaiTa16 Draft Private 08:37 Sep 10, 2026",
                            1,
                        ),
                    ),
                    sources,
                    expected_duration=517.0,
                    expected_date_markers=("sep 10, 2026",),
                )
            )

            blank = _YouTubeDraftCandidate(
                "blank_123",
                "Video thumbnail",
                "Draft Private 08:37 Sep 10, 2026",
                0,
            )
            self.assertEqual(
                YouTubeStudioUploader._select_unique_recent_draft(
                    (blank,),
                    sources,
                    expected_duration=517.0,
                    expected_date_markers=("sep 10, 2026",),
                ),
                blank,
            )

    def test_lost_target_recovers_unique_draft_without_reattaching(self) -> None:
        class DraftRecoveryUploader(YouTubeStudioUploader):
            def _discover_unique_recent_draft_url(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return "https://youtu.be/abcDEF_123"

            def _reconcile_submitted_locked(  # type: ignore[no-untyped-def]
                self, request, sources, record, report, cancel
            ):
                self.recovered_record = dict(record)
                return YouTubeUploadResult(
                    request.profile_id,
                    "Test",
                    "Test channel",
                    sources.video_path,
                    sources.package_path,
                    str(record["youtube_url"]),
                )

        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubeLostTarget_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "DaiTa16.mp4"
            package = workspace.output_dir / "DaiTa16.txt"
            video.write_bytes(b"video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                video_path=video,
                package_path=package,
            )
            uploader = DraftRecoveryUploader(
                registry_path=Path(temp) / "youtube-registry.json"
            )
            sources = resolve_workspace_upload_sources(
                workspace,
                video_path=video,
                package_path=package,
            )
            identity = uploader._request_identity(request, sources)
            record = {
                "status": "uploading",
                "attempt_id": "original-attempt",
                "profile_id": request.profile_id,
                "target_id": "lost-target",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            uploader._registry_set(identity, record)
            profile = DonutProfile(
                "profile-test",
                "Test",
                "wayfern",
                process_id=1234,
            )
            client = mock.MagicMock()
            client.__enter__.return_value = client
            client.get_profile.return_value = profile
            client.get_debug_connection.return_value = {"targets_url": "unused"}

            with (
                mock.patch("docurandom.youtube_upload.DonutMcpClient", return_value=client),
                mock.patch(
                    "docurandom.youtube_upload._mcp_cdp_page_ws_urls",
                    return_value=(),
                ),
                mock.patch(
                    "docurandom.youtube_upload._ensure_youtube_browser_bridge",
                    side_effect=BrowserExtensionBridgeError("bridge target gone"),
                ),
            ):
                result = uploader._resume_attached_locked(
                    request,
                    sources,
                    record,
                    lambda _percent, _detail: None,
                    threading.Event(),
                )

            self.assertEqual(result.youtube_url, "https://youtu.be/abcDEF_123")
            self.assertTrue(uploader.recovered_record["transfer_complete"])
            self.assertTrue(uploader.recovered_record["draft_recovered"])

    def test_completed_identity_is_not_uploaded_twice(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video1.mp4"
            package = workspace.output_dir / "Video1.txt"
            video.write_bytes(b"video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            endpoint = DonutMcpEndpoint.parse(
                "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
            )
            request = YouTubeUploadRequest(
                workspace,
                endpoint,
                "profile-test",
                video_path=video,
                package_path=package,
            )
            uploader = YouTubeStudioUploader()
            sources = resolve_workspace_upload_sources(
                workspace, video_path=video, package_path=package
            )
            uploader._checkpoint(
                request,
                sources,
                status="complete",
                progress=100,
                detail="done",
                extra={"profile_name": "Test", "channel_name": "Demo channel"},
            )

            result = uploader.upload(request)

            self.assertEqual(result.profile_name, "Test")
            self.assertEqual(result.channel_name, "Demo channel")

    def test_failure_is_persisted_for_the_tool_to_display_and_resume(self) -> None:
        class FailingUploader(YouTubeStudioUploader):
            def _upload_locked(self, request, sources, report, cancel):  # type: ignore[no-untyped-def]
                raise YouTubeUploadError(
                    "Proxy của profile không tải được YouTube Studio (ERR_TUNNEL_CONNECTION_FAILED)."
                )

        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video1.mp4"
            package = workspace.output_dir / "Video1.txt"
            video.write_bytes(b"video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            Image.new("RGB", (1280, 720), "blue").save(workspace.output_dir / "thumbnail.png")
            package.write_text(package.read_text(encoding="utf-8-sig") + "\nSEO TAGS\n\ndemo\n", encoding="utf-8-sig")
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                video_path=video,
                package_path=package,
            )

            with mock.patch(
                "docurandom.youtube_upload._YOUTUBE_RECOVERY_DELAYS",
                (0.0, 0.0, 0.0),
            ):
                with self.assertRaisesRegex(YouTubeUploadError, "ERR_TUNNEL"):
                    FailingUploader().upload(request)

            checkpoint = json.loads(
                (workspace.state_dir / "youtube_upload.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["status"], "error")
            self.assertIn("Proxy của profile", checkpoint["detail"])

    def test_proxy_failure_before_attachment_reopens_once_and_succeeds(self) -> None:
        class RecoveringUploader(YouTubeStudioUploader):
            calls = 0

            def _upload_locked(self, request, sources, report, cancel):  # type: ignore[no-untyped-def]
                self.calls += 1
                if self.calls == 1:
                    raise YouTubeUploadError(
                        "Profile không tải được YouTube Studio "
                        "(net::ERR_PROXY_CONNECTION_FAILED)."
                    )
                return YouTubeUploadResult(
                    profile_id=request.profile_id,
                    profile_name="Test",
                    channel_name="Demo channel",
                    video_path=sources.video_path,
                    package_path=sources.package_path,
                    youtube_url="https://youtu.be/demo123",
                    thumbnail_path=sources.thumbnail_path,
                )

        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video1.mp4"
            package = workspace.output_dir / "Video1.txt"
            video.write_bytes(b"video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            Image.new("RGB", (1280, 720), "blue").save(workspace.output_dir / "thumbnail.png")
            package.write_text(package.read_text(encoding="utf-8-sig") + "\nSEO TAGS\n\ndemo\n", encoding="utf-8-sig")
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                video_path=video,
                package_path=package,
            )
            uploader = RecoveringUploader()

            with mock.patch(
                "docurandom.youtube_upload._YOUTUBE_RECOVERY_DELAYS",
                (0.0,),
            ):
                result = uploader.upload(request)

            self.assertEqual(uploader.calls, 2)
            self.assertEqual(result.youtube_url, "https://youtu.be/demo123")

    def test_same_video_content_has_one_identity_even_if_filename_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            workspace = _workspace(Path(temp))
            first = workspace.output_dir / "Video1.mp4"
            second = workspace.output_dir / "renamed-copy.mp4"
            package = workspace.output_dir / "Video1.txt"
            first.write_bytes(b"the exact same rendered video")
            second.write_bytes(first.read_bytes())
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            endpoint = DonutMcpEndpoint.parse(
                "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
            )
            uploader = YouTubeStudioUploader(
                registry_path=Path(temp) / "youtube-registry.json"
            )
            first_request = YouTubeUploadRequest(
                workspace,
                endpoint,
                "profile-test",
                video_path=first,
                package_path=package,
            )
            second_request = YouTubeUploadRequest(
                workspace,
                endpoint,
                "profile-test",
                video_path=second,
                package_path=package,
            )
            first_sources = resolve_workspace_upload_sources(
                workspace, video_path=first, package_path=package
            )
            second_sources = resolve_workspace_upload_sources(
                workspace, video_path=second, package_path=package
            )

            self.assertEqual(
                uploader._request_identity(first_request, first_sources),
                uploader._request_identity(second_request, second_sources),
            )

    def test_global_in_flight_claim_blocks_a_second_upload_attempt(self) -> None:
        class NeverRunsUploader(YouTubeStudioUploader):
            called = False

            def _upload_locked(self, request, sources, report, cancel):  # type: ignore[no-untyped-def]
                self.called = True
                raise AssertionError("a duplicate attempt reached the browser")

        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video1.mp4"
            package = workspace.output_dir / "Video1.txt"
            video.write_bytes(b"video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                video_path=video,
                package_path=package,
            )
            uploader = NeverRunsUploader(
                registry_path=Path(temp) / "youtube-registry.json"
            )
            sources = resolve_workspace_upload_sources(
                workspace, video_path=video, package_path=package
            )
            identity = uploader._request_identity(request, sources)
            uploader._registry_set(identity, {"status": "uploading"})

            with self.assertRaisesRegex(YouTubeUploadUncertain, "từ chối tạo lượt thứ hai"):
                uploader.upload(request)
            self.assertFalse(uploader.called)

    def test_uncertain_early_checkpoint_resumes_same_attempt_without_new_upload(self) -> None:
        class RecoveryUploader(YouTubeStudioUploader):
            started_new = False
            resumed = False

            def _upload_locked(self, request, sources, report, cancel):  # type: ignore[no-untyped-def]
                self.started_new = True
                raise AssertionError("recovery must never start a new upload")

            def _resume_attached_locked(  # type: ignore[no-untyped-def]
                self, request, sources, record, report, cancel
            ):
                self.resumed = True
                self.assert_record = dict(record)
                return YouTubeUploadResult(
                    request.profile_id,
                    "Youtube 4",
                    "Khen Nhật",
                    sources.video_path,
                    sources.package_path,
                    "https://youtu.be/recovered",
                )

        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubeResume_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video 3.mp4"
            package = workspace.output_dir / "Video 3.txt"
            video.write_bytes(b"the already attached video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                video_path=video,
                package_path=package,
            )
            uploader = RecoveryUploader(
                registry_path=Path(temp) / "youtube-registry.json"
            )
            sources = resolve_workspace_upload_sources(
                workspace, video_path=video, package_path=package
            )
            identity = uploader._request_identity(request, sources)
            uploader._registry_set(
                identity,
                {
                    "status": "uploading",
                    "attempt_id": "one-original-attempt",
                    "profile_id": request.profile_id,
                },
            )
            uploader._checkpoint(
                request,
                sources,
                status="uncertain",
                progress=24,
                detail="CDP frame exceeded the old 1 MiB limit",
            )

            result = uploader.upload(request)

            self.assertTrue(uploader.resumed)
            self.assertFalse(uploader.started_new)
            self.assertEqual(uploader.assert_record["attempt_id"], "one-original-attempt")
            self.assertEqual(result.youtube_url, "https://youtu.be/recovered")

    def test_metadata_failure_auto_resumes_the_same_draft_without_reattaching(self) -> None:
        class RecoveryUploader(YouTubeStudioUploader):
            new_attempt_calls = 0
            resumed = False

            def _upload_locked(self, request, sources, report, cancel):  # type: ignore[no-untyped-def]
                self.new_attempt_calls += 1
                identity = self._request_identity(request, sources)
                attempt_id = "one-durable-attempt"
                self._registry_claim(
                    identity,
                    {
                        "status": "uploading",
                        "attempt_id": attempt_id,
                        "profile_id": request.profile_id,
                        "target_id": "owned-upload-target",
                    },
                )
                self._checkpoint(
                    request,
                    sources,
                    status="uploading",
                    progress=24,
                    detail="file attached before MFK loaded",
                    extra={"attempt_id": attempt_id},
                )
                raise YouTubeUploadError("MFK control is still loading")

            def _resume_attached_locked(  # type: ignore[no-untyped-def]
                self, request, sources, record, report, cancel
            ):
                self.resumed = True
                return YouTubeUploadResult(
                    request.profile_id,
                    "Test",
                    "Test channel",
                    sources.video_path,
                    sources.package_path,
                    "https://youtu.be/recovered",
                )

        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubeAutoRepair_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video 1.mp4"
            package = workspace.output_dir / "Video 1.txt"
            video.write_bytes(b"video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            Image.new("RGB", (1280, 720), "blue").save(workspace.output_dir / "thumbnail.png")
            package.write_text(package.read_text(encoding="utf-8-sig") + "\nSEO TAGS\n\ndemo\n", encoding="utf-8-sig")
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                video_path=video,
                package_path=package,
            )
            uploader = RecoveryUploader(
                registry_path=Path(temp) / "youtube-registry.json"
            )

            with mock.patch(
                "docurandom.youtube_upload._YOUTUBE_RECOVERY_DELAYS",
                (0.0,),
            ):
                result = uploader.upload(request)

            self.assertTrue(uploader.resumed)
            self.assertEqual(uploader.new_attempt_calls, 1)
            self.assertEqual(result.youtube_url, "https://youtu.be/recovered")

    def test_allocated_video_checkpoint_uses_metadata_repair_path(self) -> None:
        class RecoveryUploader(YouTubeStudioUploader):
            started_new = False
            resumed_dialog = False
            repaired = False

            def _upload_locked(self, request, sources, report, cancel):  # type: ignore[no-untyped-def]
                self.started_new = True
                raise AssertionError("must not upload file again")

            def _resume_attached_locked(self, request, sources, record, report, cancel):  # type: ignore[no-untyped-def]
                self.resumed_dialog = True
                raise AssertionError("allocated video must use exact edit repair")

            def _reconcile_submitted_locked(  # type: ignore[no-untyped-def]
                self, request, sources, record, report, cancel
            ):
                self.repaired = True
                return YouTubeUploadResult(
                    request.profile_id,
                    "Test",
                    "Test channel",
                    sources.video_path,
                    sources.package_path,
                    str(record["youtube_url"]),
                )

        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubeExactDraft_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video 1.mp4"
            package = workspace.output_dir / "Video 1.txt"
            video.write_bytes(b"video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                video_path=video,
                package_path=package,
            )
            uploader = RecoveryUploader(
                registry_path=Path(temp) / "youtube-registry.json"
            )
            sources = resolve_workspace_upload_sources(
                workspace,
                video_path=video,
                package_path=package,
            )
            identity = uploader._request_identity(request, sources)
            record = {
                "status": "uploading",
                "attempt_id": "same-attempt",
                "profile_id": request.profile_id,
                "youtube_url": "https://youtu.be/abcDEF_123",
            }
            uploader._registry_set(identity, record)
            uploader._checkpoint(
                request,
                sources,
                status="uncertain",
                progress=82,
                detail="metadata incomplete",
                extra=record,
            )

            result = uploader.upload(request)

            self.assertTrue(uploader.repaired)
            self.assertFalse(uploader.started_new)
            self.assertFalse(uploader.resumed_dialog)
            self.assertEqual(result.youtube_url, "https://youtu.be/abcDEF_123")

    def test_upload_wizard_resume_detects_remaining_steps_instead_of_fixed_clicks(self) -> None:
        controller = mock.MagicMock(spec=_SemanticCdpController)
        phase = {"value": 0}
        audience = {"backendNodeId": 10, "attributes": ["name", "VIDEO_MADE_FOR_KIDS_NOT_MFK"]}
        next_button = {"backendNodeId": 11, "attributes": ["id", "next-button"]}
        private = {"backendNodeId": 12, "attributes": ["name", "PRIVATE"]}

        def find_dom(_nodes, *, attributes=None, **_kwargs):  # type: ignore[no-untyped-def]
            attributes = attributes or {}
            if attributes == {"name": "PRIVATE"} and phase["value"] >= 3:
                return private
            if attributes == {"name": "VIDEO_MADE_FOR_KIDS_NOT_MFK"} and phase["value"] == 0:
                return audience
            if attributes == {"id": "next-button"} and phase["value"] < 3:
                return next_button
            return None

        def press(node):  # type: ignore[no-untyped-def]
            if node is next_button:
                phase["value"] += 1

        controller.dom_nodes.return_value = []
        controller.find_dom.side_effect = find_dom
        controller._dom_attributes.side_effect = lambda node: {} if node else {}
        controller.press.side_effect = press
        cancel = mock.MagicMock(spec=threading.Event)
        cancel.is_set.return_value = False
        cancel.wait.return_value = False

        YouTubeStudioUploader._advance_upload_wizard_to_visibility(
            controller,
            lambda _percent, _message: None,
            cancel,
            timeout=1,
        )

        self.assertEqual(phase["value"], 3)
        controller.select_dom_radio.assert_called_once_with(
            "VIDEO_MADE_FOR_KIDS_NOT_MFK",
            timeout=60,
            cancel_event=cancel,
        )

    def test_live_recovery_verifies_exact_attached_file_and_never_sets_video_again(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubeLiveResume_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video 3.mp4"
            package = workspace.output_dir / "Video 3.txt"
            video.write_bytes(b"the exact bytes already attached")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                video_path=video,
                package_path=package,
            )
            uploader = YouTubeStudioUploader(
                registry_path=Path(temp) / "youtube-registry.json"
            )
            sources = resolve_workspace_upload_sources(
                workspace, video_path=video, package_path=package
            )
            identity = uploader._request_identity(request, sources)
            record = {
                "status": "uploading",
                "attempt_id": "original-only-attempt",
                "channel_name": "Khen Nhật",
                "target_id": "upload",
            }
            uploader._registry_set(identity, record)

            profile = DonutProfile(
                "profile-test",
                "Youtube 4",
                "wayfern",
                process_id=1234,
            )
            client = mock.MagicMock()
            client.__enter__.return_value = client
            client.get_profile.return_value = profile
            client.get_debug_connection.return_value = {"targets_url": "unused"}
            session = mock.MagicMock()
            session.__enter__.return_value = mock.sentinel.cdp
            controller = mock.MagicMock(spec=_SemanticCdpController)
            controller.page_url.return_value = "https://studio.youtube.com/channel/demo/videos/upload"
            controller.attached_video.return_value = {
                "name": video.name,
                "size": video.stat().st_size,
                "type": "video/mp4",
                "count": 1,
            }
            controller._bridge_command.return_value = {
                "found": True,
                "value": video.stem,
            }
            expected = YouTubeUploadResult(
                profile.profile_id,
                profile.name,
                "Khen Nhật",
                video,
                package,
                "https://youtu.be/recovered",
            )
            continue_upload = mock.Mock(return_value=expected)
            uploader._continue_attached_upload = continue_upload  # type: ignore[method-assign]

            with (
                mock.patch("docurandom.youtube_upload.DonutMcpClient", return_value=client),
                mock.patch(
                    "docurandom.youtube_upload._mcp_cdp_page_ws_urls",
                    return_value=("ws://127.0.0.1:1234/devtools/page/upload",),
                ),
                mock.patch("docurandom.youtube_upload._CdpSession", return_value=session),
                mock.patch(
                    "docurandom.youtube_upload._SemanticCdpController",
                    return_value=controller,
                ),
                mock.patch(
                    "docurandom.youtube_upload._ensure_youtube_browser_bridge",
                    return_value=mock.sentinel.browser_bridge,
                ),
                mock.patch(
                    "docurandom.youtube_upload._browser_extension_tab_for_target",
                    return_value=(mock.sentinel.browser_bridge, 177),
                ),
                mock.patch(
                    "docurandom.youtube_upload._mcp_set_cdp_window_state",
                    return_value=None,
                ),
            ):
                result = uploader._resume_attached_locked(
                    request,
                    sources,
                    record,
                    lambda _percent, _detail: None,
                    threading.Event(),
                )

            self.assertEqual(result.youtube_url, "https://youtu.be/recovered")
            controller.attached_video.assert_not_called()
            controller.set_file.assert_not_called()
            continue_upload.assert_called_once()

    def test_interrupted_upload_resumes_same_file_only_after_unique_filename_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubeInterruptedResume_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video 4.mp4"
            package = workspace.output_dir / "Video 4.txt"
            video.write_bytes(b"the exact interrupted upload bytes")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                video_path=video,
                package_path=package,
            )
            uploader = YouTubeStudioUploader(
                registry_path=Path(temp) / "youtube-registry.json"
            )
            sources = resolve_workspace_upload_sources(
                workspace, video_path=video, package_path=package
            )
            identity = uploader._request_identity(request, sources)
            controller = mock.MagicMock(spec=_SemanticCdpController)
            controller.has_background_bridge = True
            controller.automation_target_id = "interrupted-target"
            controller.bridge_tab_id = 177
            controller.interrupted_upload_state.side_effect = [
                {
                    "interrupted": True,
                    "found": True,
                    "matchCount": 1,
                    "rowText": "Video 4.mp4 · Quá trình tải lên bị gián đoạn",
                },
                {"interrupted": True, "found": True, "clicked": True, "matchCount": 1},
            ]
            controller.attached_video.return_value = {
                "name": video.name,
                "size": video.stat().st_size,
                "count": 1,
            }
            profile = DonutProfile(
                "profile-test",
                "Test",
                "wayfern",
                process_id=1234,
            )
            record = {
                "status": "uncertain",
                "attempt_id": "interrupted-attempt",
                "profile_id": request.profile_id,
            }
            uploader._registry_set(identity, record)

            resumed = uploader._resume_interrupted_upload_locked(
                request,
                sources,
                record,
                profile,
                "Demo channel",
                identity,
                "interrupted-attempt",
                controller,
                lambda _percent, _detail: None,
                threading.Event(),
            )

            self.assertTrue(resumed)
            controller.interrupted_upload_state.assert_has_calls(
                [
                    mock.call(video.name),
                    mock.call(video.name, resume=True),
                ]
            )
            controller.wait_file_input.assert_called_once()
            controller.set_file.assert_called_once_with(video, "video")
            checkpoint = json.loads(
                (workspace.state_dir / "youtube_upload.json").read_text(encoding="utf-8")
            )
            self.assertTrue(checkpoint["resumed_interrupted_upload"])
            self.assertEqual(checkpoint["status"], "uploading")

    def test_interrupted_upload_does_not_guess_when_no_exact_row_matches(self) -> None:
        controller = mock.MagicMock(spec=_SemanticCdpController)
        controller.has_background_bridge = True
        controller.interrupted_upload_state.return_value = {
            "interrupted": False,
            "found": False,
            "matchCount": 0,
        }
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubeInterruptedNoGuess_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video 4.mp4"
            package = workspace.output_dir / "Video 4.txt"
            video.write_bytes(b"video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                video_path=video,
                package_path=package,
            )
            sources = resolve_workspace_upload_sources(
                workspace, video_path=video, package_path=package
            )
            resumed = YouTubeStudioUploader()._resume_interrupted_upload_locked(
                request,
                sources,
                {"attempt_id": "attempt"},
                DonutProfile("profile-test", "Test", "wayfern", process_id=1),
                "Demo channel",
                "identity",
                "attempt",
                controller,
                lambda _percent, _detail: None,
                threading.Event(),
            )
            self.assertFalse(resumed)
            controller.set_file.assert_not_called()

    def test_content_draft_resume_uses_video_id_when_row_hides_filename(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTubeContentResume_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "DaiTa8.mp4"
            package = workspace.output_dir / "DaiTa8.txt"
            video.write_bytes(b"the exact content-row bytes")
            package.write_text(
                "TIÊU ĐỀ\n\nDaiTa8\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                privacy="UNLISTED",
                video_path=video,
                package_path=package,
            )
            uploader = YouTubeStudioUploader(
                registry_path=Path(temp) / "youtube-registry.json"
            )
            sources = resolve_workspace_upload_sources(
                workspace, video_path=video, package_path=package
            )
            identity = uploader._request_identity(request, sources)
            attempt_id = "content-resume-attempt"
            record = {
                "status": "uncertain",
                "attempt_id": attempt_id,
                "profile_id": request.profile_id,
                "youtube_url": "https://youtu.be/agXtWYwYGYE",
                "transfer_complete": False,
            }
            uploader._registry_set(identity, record)
            controller = mock.MagicMock(spec=_SemanticCdpController)
            controller.has_background_bridge = True
            controller.automation_target_id = "content-target"
            controller.bridge_tab_id = 177
            controller.content_upload_state.side_effect = [
                {
                    "found": True,
                    "interrupted": True,
                    "matchCount": 1,
                    "videoId": "agXtWYwYGYE",
                    "title": "DaiTa8",
                },
                {
                    "found": True,
                    "interrupted": True,
                    "clicked": True,
                    "matchCount": 1,
                    "videoId": "agXtWYwYGYE",
                },
            ]
            controller.attached_video.return_value = {
                "name": video.name,
                "size": video.stat().st_size,
                "count": 1,
            }
            profile = DonutProfile(
                "profile-test",
                "DaiTa8",
                "wayfern",
                process_id=1234,
            )

            resumed = uploader._resume_existing_content_upload_locked(
                request,
                sources,
                record,
                profile,
                "DaiTa8 channel",
                identity,
                attempt_id,
                controller,
                lambda _percent, _detail: None,
                threading.Event(),
            )

            self.assertTrue(resumed)
            controller.content_upload_state.assert_has_calls(
                [
                    mock.call("agXtWYwYGYE", timeout=20),
                    mock.call("agXtWYwYGYE", resume=True, timeout=20),
                ]
            )
            controller.wait_file_input.assert_called_once()
            controller.set_file.assert_called_once_with(video, "video")
            controller.mark_upload_started.assert_called_once()
            checkpoint = json.loads(
                (workspace.state_dir / "youtube_upload.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["status"], "uploading")
            self.assertEqual(checkpoint["youtube_url"], "https://youtu.be/agXtWYwYGYE")
            self.assertTrue(checkpoint["resumed_content_upload"])
            self.assertFalse(uploader._registry_get(identity)["transfer_requires_content_resume"])

    def test_corrupt_global_registry_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            registry = Path(temp) / "youtube-registry.json"
            registry.write_text("not-json", encoding="utf-8")
            uploader = YouTubeStudioUploader(registry_path=registry)

            with self.assertRaisesRegex(YouTubeUploadError, "từ chối upload"):
                uploader._registry_get("video-identity")

    def test_atomic_registry_claim_allows_exactly_one_winner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            registry = Path(temp) / "youtube-registry.json"
            first = YouTubeStudioUploader(registry_path=registry)
            second = YouTubeStudioUploader(registry_path=registry)
            results: list[dict[str, object]] = []
            barrier = threading.Barrier(2)

            def claim(uploader: YouTubeStudioUploader, attempt_id: str) -> None:
                barrier.wait(timeout=2)
                results.append(
                    uploader._registry_claim(
                        "same-video-and-profile",
                        {"status": "attaching", "attempt_id": attempt_id},
                    )
                )

            threads = [
                threading.Thread(target=claim, args=(first, "first")),
                threading.Thread(target=claim, args=(second, "second")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

            self.assertEqual(sum(not item for item in results), 1)
            self.assertEqual(sum(bool(item) for item in results), 1)
            stored = first._registry_get("same-video-and-profile")
            self.assertIn(stored["attempt_id"], {"first", "second"})

    def test_error_after_file_attachment_becomes_non_retryable_uncertain_state(self) -> None:
        class AttachedFailingUploader(YouTubeStudioUploader):
            def _upload_locked(self, request, sources, report, cancel):  # type: ignore[no-untyped-def]
                self._checkpoint(
                    request,
                    sources,
                    status="uploading",
                    progress=24,
                    detail="file attached",
                )
                raise YouTubeUploadError("YouTube web bị ngắt giữa bước metadata")

        with tempfile.TemporaryDirectory(prefix="DocuRandomYouTube_") as temp:
            workspace = _workspace(Path(temp))
            video = workspace.output_dir / "Video1.mp4"
            package = workspace.output_dir / "Video1.txt"
            video.write_bytes(b"video")
            package.write_text(
                "TIÊU ĐỀ\n\nDemo\n\nMÔ TẢ\n\nMô tả demo\n",
                encoding="utf-8-sig",
            )
            Image.new("RGB", (1280, 720), "blue").save(workspace.output_dir / "thumbnail.png")
            package.write_text(package.read_text(encoding="utf-8-sig") + "\nSEO TAGS\n\ndemo\n", encoding="utf-8-sig")
            request = YouTubeUploadRequest(
                workspace,
                DonutMcpEndpoint.parse(
                    "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
                ),
                "profile-test",
                video_path=video,
                package_path=package,
            )
            uploader = AttachedFailingUploader(
                registry_path=Path(temp) / "youtube-registry.json"
            )

            with self.assertRaisesRegex(YouTubeUploadUncertain, "khóa mọi lần thử lại"):
                uploader.upload(request)

            checkpoint = json.loads(
                (workspace.state_dir / "youtube_upload.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["status"], "uncertain")
            self.assertIn("chưa xác nhận hoàn tất", checkpoint["detail"])


class ExactEditorRecoveryTests(unittest.TestCase):
    def controller(self):
        controller = mock.MagicMock(spec=_SemanticCdpController)
        controller.upload_started = False
        controller.page_url.return_value = 'https://studio.youtube.com/video/Exact123/edit'
        return controller

    def test_empty_editor_reloads_same_id_once_before_metadata(self):
        controller = self.controller()
        controller.wait_dom.side_effect = [
            YouTubeUploadError('YouTube Studio không hiện điều khiển id=textbox trước thời hạn.'),
            {'backendNodeId': 9},
        ]
        report = mock.Mock()
        YouTubeStudioUploader._load_exact_draft_editor(
            controller, controller.page_url.return_value, threading.Event(), report, timeout=2,
        )
        self.assertEqual(controller.navigate_and_wait.call_count, 2)
        self.assertEqual(controller.wait_dom.call_count, 2)
        self.assertTrue(all(c.args[0] == controller.page_url.return_value for c in controller.navigate_and_wait.call_args_list))
        controller.click.assert_not_called()
        report.assert_called_once()

    def test_persistent_empty_editor_stops_after_one_reload(self):
        controller = self.controller()
        controller.wait_dom.side_effect = YouTubeUploadError('Không hiện điều khiển id=textbox')
        with self.assertRaises(YouTubeUploadError):
            YouTubeStudioUploader._load_exact_draft_editor(
                controller, controller.page_url.return_value, threading.Event(), mock.Mock(), timeout=2,
            )
        self.assertEqual(controller.navigate_and_wait.call_count, 2)

    def test_different_video_route_and_attached_upload_are_not_reloaded(self):
        for attached, current in (
            (True, 'https://studio.youtube.com/video/Exact123/edit'),
            (False, 'https://studio.youtube.com/video/Other123/edit'),
            (False, 'https://accounts.google.com/signin'),
        ):
            with self.subTest(attached=attached, current=current):
                controller = self.controller()
                controller.upload_started = attached
                controller.page_url.return_value = current
                controller.wait_dom.side_effect = YouTubeUploadError('Không hiện điều khiển id=textbox')
                with self.assertRaises(YouTubeUploadError):
                    YouTubeStudioUploader._load_exact_draft_editor(
                        controller, 'https://studio.youtube.com/video/Exact123/edit', threading.Event(), mock.Mock(), timeout=2,
                    )
                self.assertEqual(controller.navigate_and_wait.call_count, 1)

    def test_auth_and_browser_errors_do_not_enter_empty_editor_recovery(self):
        controller = self.controller()
        controller.wait_dom.side_effect = YouTubeUploadError('Profile đã mất đăng nhập')
        with self.assertRaises(YouTubeUploadError):
            YouTubeStudioUploader._load_exact_draft_editor(
                controller, controller.page_url.return_value, threading.Event(), mock.Mock(), timeout=2,
            )
        self.assertEqual(controller.navigate_and_wait.call_count, 1)


if __name__ == "__main__":
    unittest.main()
