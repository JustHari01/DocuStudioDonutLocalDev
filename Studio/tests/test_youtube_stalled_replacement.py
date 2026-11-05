import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from PIL import Image

from docurandom.donut_mcp import DonutMcpEndpoint
from docurandom.workspace import Workspace
from docurandom.youtube_upload import (
    YouTubeStudioUploader, YouTubeUploadError, YouTubeUploadUncertain,
    YouTubeUploadRequest, WorkspaceUploadSources, YouTubeMetadata, _SemanticCdpController,
)


class StalledReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace('workspace-id', 'workspace', str(self.root / 'workspace'), '', '')
        self.workspace.ensure_layout()
        self.video = self.workspace.output_dir / 'same.mp4'
        self.video.write_bytes(b'original-video-bytes')
        self.package = self.workspace.output_dir / 'same.txt'
        self.package.write_text('original metadata', encoding='utf-8')
        self.thumbnail = self.workspace.state_dir / 'thumbnail.png'
        Image.new('RGB', (640, 360), 'navy').save(self.thumbnail)
        self.sources = WorkspaceUploadSources(self.video, self.package, self.thumbnail,
                                             YouTubeMetadata('title', 'description', tags=('test tag',)))
        self.request = YouTubeUploadRequest(self.workspace, DonutMcpEndpoint.parse('http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv'), 'profile-id', privacy='PUBLIC')
        self.uploader = YouTubeStudioUploader(registry_path=self.root / 'registry.json')
        self.identity = self.uploader._request_identity(self.request, self.sources)
        self.attempt = 'a' * 32
        self.now = datetime.now(timezone.utc)
        self.record = {
            'status': 'uncertain', 'attempt_id': self.attempt, 'workspace_id': self.workspace.workspace_id,
            'profile_id': self.request.profile_id, 'youtube_url': 'https://youtu.be/ExactOld123',
            'transfer_complete': False, 'transfer_recovery_stalled': True,
            'title': 'title',
            'transfer_feedback_percent': 14.74,
            'transfer_feedback_progress_at': (self.now - timedelta(hours=2)).isoformat(),
            'transfer_feedback_observed_at': (self.now - timedelta(minutes=2)).isoformat(),
        }
        self.uploader._registry_set(self.identity, self.record)
        self.checkpoint = {'identity': self.identity, 'attempt_id': self.attempt, 'youtube_url': self.record['youtube_url'], 'status': 'uncertain'}
        self.checkpoint_path = self.uploader._checkpoint_path(self.workspace)
        self.checkpoint_path.write_text(json.dumps(self.checkpoint), encoding='utf-8')
        self.probe = {'observed_at': self.now.isoformat(), 'server_percent': 14.74,
                      'exact_row_found': True, 'resume_unavailable': True,
                      'owned_profile_closed': True, 'video_id': 'ExactOld123'}

    def approve(self, **kwargs):
        return self.uploader.authorize_stalled_upload_replacement(
            self.request, self.sources, expected_attempt_id=self.attempt,
            probe=kwargs.get('probe', self.probe), operator_approved=kwargs.get('approved', True))

    def test_archives_exact_checkpoint_and_claim_then_reserves_one_budget(self):
        ready = self.approve()
        self.assertEqual(ready['status'], 'replacement_ready')
        self.assertEqual(ready['replacement_budget_used'], 1)
        archive = Path(ready['replacement_checkpoint'])
        self.assertEqual(json.loads(archive.read_text()), self.checkpoint)
        self.assertEqual(json.loads((archive.parent / 'registry-claim.json').read_text()), self.record)
        self.assertTrue(ready['replacement_history'][0]['public_blocked'])
        checkpoint = json.loads(self.checkpoint_path.read_text(encoding='utf-8'))
        self.assertTrue(checkpoint['replacement_ready'])
        self.assertEqual(checkpoint['youtube_url'], '')
        self.assertEqual(self.video.read_bytes(), b'original-video-bytes')

    def test_refuses_missing_approval_and_changed_bytes_without_changing_lease(self):
        with self.assertRaises(YouTubeUploadUncertain):
            self.approve(approved=False)
        self.video.write_bytes(b'different-file')
        with self.assertRaises(YouTubeUploadUncertain):
            self.approve()
        self.assertEqual(self.uploader._registry_get(self.identity), self.record)

    def test_refuses_bad_or_stale_or_progressing_or_ambiguous_proof(self):
        for changes in ({'server_percent': 100}, {'server_percent': float('nan')},
                        {'server_percent': 15}, {'video_id': 'OtherVideo'},
                        {'exact_row_found': False}, {'resume_unavailable': False},
                        {'owned_profile_closed': False},
                        {'observed_at': (self.now - timedelta(minutes=3)).isoformat()},
                        {'observed_at': self.now.replace(tzinfo=None).isoformat()}):
            with self.subTest(changes=changes), self.assertRaises(YouTubeUploadUncertain):
                self.approve(probe={**self.probe, **changes})
            self.assertEqual(self.uploader._registry_get(self.identity), self.record)

    def test_completed_or_wrong_owner_cannot_be_replaced(self):
        for changes in ({'transfer_complete': True}, {'attempt_id': 'b' * 32},
                        {'workspace_id': 'another-workspace'}, {'profile_id': 'another-profile'},
                        {'status': 'complete'}):
            with self.subTest(changes=changes):
                self.uploader._registry_set(self.identity, {**self.record, **changes})
                with self.assertRaises(YouTubeUploadUncertain):
                    self.approve()

    def test_second_replacement_is_rejected_across_new_uploader_instance(self):
        self.approve()
        self.uploader = YouTubeStudioUploader(registry_path=self.uploader.registry_path)
        with self.assertRaises(YouTubeUploadUncertain):
            self.approve()

    def test_stale_worker_cannot_update_the_reserved_replacement_lease(self):
        ready = self.approve()
        self.assertNotEqual(ready['attempt_id'], self.attempt)
        with self.assertRaises(YouTubeUploadUncertain):
            self.uploader._registry_update_attempt(self.identity, self.attempt, {'status': 'complete'})
        self.assertEqual(self.uploader._registry_get(self.identity)['status'], 'replacement_ready')

    def test_changed_package_cannot_replace_the_old_video(self):
        self.sources = WorkspaceUploadSources(self.video, self.package, None, YouTubeMetadata('successor title', 'description'))
        with self.assertRaisesRegex(YouTubeUploadUncertain, 'Gói nội dung đã đổi'):
            self.approve()
        self.assertEqual(self.uploader._registry_get(self.identity), self.record)

    def test_two_competing_claims_consume_only_one_replacement(self):
        self.approve()
        barrier = threading.Barrier(2)
        results = []
        def claim(name):
            uploader = YouTubeStudioUploader(registry_path=self.uploader.registry_path)
            barrier.wait(timeout=3)
            results.append(uploader._registry_claim(self.identity, {
                'attempt_id': name, 'workspace_id': self.workspace.workspace_id,
                'profile_id': self.request.profile_id, 'privacy': 'UNLISTED', 'status': 'attaching'}))
        workers = [threading.Thread(target=claim, args=(name,)) for name in ('first', 'second')]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=5)
        self.assertEqual(sum(not result for result in results), 1)
        current = self.uploader._registry_get(self.identity)
        self.assertEqual(current['replacement_budget_used'], 1)
        self.assertEqual(len(current['replacement_history']), 1)

    def test_public_or_wrong_workspace_claim_cannot_consume_replacement(self):
        self.approve()
        for changes in ({'privacy': 'PUBLIC'}, {'workspace_id': 'wrong'}, {'profile_id': 'wrong'}):
            with self.subTest(changes=changes):
                result = self.uploader._registry_claim(self.identity, {
                    'attempt_id': 'new', 'workspace_id': self.workspace.workspace_id,
                    'profile_id': self.request.profile_id, 'privacy': 'UNLISTED', **changes})
                self.assertEqual(result['status'], 'replacement_ready')

    def test_old_video_public_is_blocked_before_connecting_browser(self):
        self.approve()
        with mock.patch('docurandom.youtube_upload.DonutMcpClient') as client:
            with self.assertRaisesRegex(YouTubeUploadError, 'khóa Public'):
                self.uploader.promote_deferred_public(endpoint=self.request.endpoint,
                    profile_id=self.request.profile_id, youtube_url=self.record['youtube_url'])
            client.assert_not_called()
        self.uploader._assert_public_video_not_superseded(self.request.profile_id, 'NewVideo123')

    def test_durable_stall_budget_stops_after_three_spaced_probes(self):
        with mock.patch('docurandom.youtube_upload.datetime') as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            for offset in (0, 31, 62):
                clock.now.return_value = self.now + timedelta(seconds=offset)
                self.assertTrue(self.uploader._record_partial_transfer_feedback(self.identity, self.attempt, 14.74))
        fresh = YouTubeStudioUploader(registry_path=self.uploader.registry_path)
        self.assertTrue(fresh.upload_recovery_requires_manual(self.identity, self.attempt))
        self.assertIn('3 lượt', fresh.upload_recovery_stop_reason(self.identity, self.attempt))

    def test_same_tick_does_not_exhaust_budget_and_progress_resets_count(self):
        with mock.patch('docurandom.youtube_upload.datetime') as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.now.return_value = self.now
            for _ in range(4):
                self.uploader._record_partial_transfer_feedback(self.identity, self.attempt, 14.74)
            self.assertEqual(self.uploader._registry_get(self.identity)['transfer_stall_recovery_count'], 1)
            self.assertFalse(self.uploader._record_partial_transfer_feedback(self.identity, self.attempt, 15.0))
            self.assertEqual(self.uploader._registry_get(self.identity)['transfer_stall_recovery_count'], 0)

    def test_interrupted_ready_checkpoint_is_repaired_and_upload_is_held_unlisted(self):
        self.approve()
        self.checkpoint_path.write_text(json.dumps(self.checkpoint), encoding='utf-8')
        with (mock.patch('docurandom.youtube_upload.resolve_workspace_upload_sources', return_value=self.sources),
              mock.patch('docurandom.youtube_upload._wait_for_render_upload_ready'),
              mock.patch.object(self.uploader, '_wait_for_existing_file_transfers'),
              mock.patch.object(self.uploader, '_upload_locked', return_value=mock.sentinel.result) as upload):
            self.assertIs(self.uploader.upload(self.request), mock.sentinel.result)
        actual_request = upload.call_args.args[0]
        self.assertEqual(self.uploader._effective_privacy(actual_request), 'UNLISTED')
        self.assertEqual(self.uploader._completion_status(actual_request), 'deferred_public')
        self.assertEqual(json.loads(self.checkpoint_path.read_text(encoding='utf-8')).get('youtube_url', ''), '')

    def test_ui_does_not_arm_another_timer_for_durably_stopped_transfer(self):
        from docurandom.ui import DocuRandomApp
        self.uploader._registry_update_attempt(self.identity, self.attempt, {
            'recovery_requires_manual': True, 'recovery_stop_reason': 'Stopped after three probes'})
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.closed = app.closing_after_cancel = False
        app.workspace = self.workspace
        app.youtube_uploader = self.uploader
        app.root = mock.Mock()
        app.youtube_percent_var = mock.Mock()
        app.youtube_status_var = mock.Mock()
        app._append_log = mock.Mock()
        app.youtube_recovery_after_id = None
        app._schedule_youtube_checkpoint_recovery('restart')
        app.root.after.assert_not_called()
        app.youtube_status_var.set.assert_called_with('Stopped after three probes')

    def test_terminal_notice_does_not_promise_automatic_recovery(self):
        from docurandom.ui import DocuRandomApp
        self.uploader._registry_update_attempt(self.identity, self.attempt, {'recovery_requires_manual': True})
        app = DocuRandomApp.__new__(DocuRandomApp)
        app.workspace = self.workspace
        app.youtube_uploader = self.uploader
        app._youtube_recovery_notice_is_due = mock.Mock(return_value=True)
        app._notify_remote = mock.Mock()
        app._notify_youtube_recovery_once('stalled transfer')
        message = app._notify_remote.call_args.args[1]
        self.assertIn('ĐÃ DỪNG TỰ MỞ PROFILE', message)
        self.assertNotIn('Tool sẽ tự nối lại', message)

    def test_dialog_id_is_scoped_to_one_visible_dialog(self):
        controller = _SemanticCdpController(mock.Mock())
        dialog = {'nodeName': 'YTCP-UPLOADS-DIALOG', 'attributes': ['video-id', 'ExactOld123'], 'backendNodeId': 1}
        controller.dom_nodes = mock.Mock(return_value=[dialog])
        controller.dom_node_has_box = mock.Mock(return_value=True)
        self.assertEqual(controller.upload_dialog_url(), 'https://youtu.be/ExactOld123')
        controller.dom_nodes.return_value = [{**dialog, 'attributes': ['video-id', 'ExactOld123', 'hidden', '']}]
        self.assertEqual(controller.upload_dialog_url(), '')
        controller.dom_nodes.return_value = [dialog, {**dialog, 'attributes': ['video-id', 'Different12']}]
        self.assertEqual(controller.upload_dialog_url(), '')

    def test_partial_or_missing_server_feedback_cannot_handoff_despite_ui_100(self):
        controller = mock.Mock(spec=_SemanticCdpController)
        controller.upload_dialog_url.return_value = self.record['youtube_url']
        controller.youtube_submission_complete.return_value = True
        for percent in (None, 98.66, float('nan')):
            with self.subTest(percent=percent), mock.patch.object(self.uploader, '_reconcile_submitted_locked') as reconcile:
                controller.server_upload_progress.return_value = (percent, '')
                self.assertIsNone(self.uploader._try_completed_wizard_handoff(
                    self.request, self.sources, self.identity, self.attempt, controller, mock.Mock(), threading.Event()))
                reconcile.assert_not_called()
                controller.set_file.assert_not_called()
        self.assertEqual(self.uploader._registry_get(self.identity), self.record)

    def test_server_complete_same_video_is_adopted_without_public_before_metadata(self):
        self.approve()
        new_attempt = 'b' * 32
        self.uploader._registry_claim(self.identity, {'attempt_id': new_attempt, 'status': 'uploading',
            'workspace_id': self.workspace.workspace_id, 'profile_id': self.request.profile_id, 'privacy': 'UNLISTED'})
        controller = mock.Mock(spec=_SemanticCdpController)
        controller.automation_target_id = 'owned-target'
        controller.bridge_tab_id = 123
        controller.upload_dialog_url.return_value = self.record['youtube_url']
        controller.youtube_submission_complete.return_value = True
        controller.server_upload_progress.return_value = (100.0, '')
        with mock.patch.object(self.uploader, '_reconcile_submitted_locked', return_value=mock.sentinel.result):
            result = self.uploader._try_completed_wizard_handoff(self.request, self.sources,
                self.identity, new_attempt, controller, mock.Mock(), threading.Event())
        self.assertIs(result, mock.sentinel.result)
        current = self.uploader._registry_get(self.identity)
        self.assertEqual(current['replacement_resumed_video_id'], 'ExactOld123')
        self.assertTrue(current['transfer_complete'])
        controller.set_file.assert_not_called()
        controller.press.assert_not_called()
        with self.assertRaises(YouTubeUploadError):
            self.uploader._assert_public_video_not_superseded(self.request.profile_id, 'ExactOld123')
        self.uploader._registry_update_attempt(self.identity, new_attempt,
            {'status': 'deferred_public', 'privacy': 'UNLISTED'})
        self.uploader._assert_public_video_not_superseded(self.request.profile_id, 'ExactOld123')

    def test_normal_metadata_completion_adopts_same_id_but_keeps_other_id_blocked(self):
        self.approve()
        new_attempt = 'b' * 32
        self.uploader._registry_claim(self.identity, {'attempt_id': new_attempt, 'status': 'uploading',
            'workspace_id': self.workspace.workspace_id, 'profile_id': self.request.profile_id, 'privacy': 'UNLISTED'})
        self.uploader._registry_update_attempt(self.identity, new_attempt, {
            'status': 'deferred_public', 'youtube_url': 'https://youtu.be/OtherNew123',
            'transfer_complete': True, 'privacy': 'UNLISTED'})
        with self.assertRaises(YouTubeUploadError):
            self.uploader._assert_public_video_not_superseded(self.request.profile_id, 'ExactOld123')
        self.uploader._registry_update_attempt(self.identity, new_attempt, {'youtube_url': self.record['youtube_url']})
        self.assertEqual(self.uploader._registry_get(self.identity)['replacement_resumed_video_id'], 'ExactOld123')
        self.uploader._assert_public_video_not_superseded(self.request.profile_id, 'ExactOld123')

    def test_allocated_dialog_id_is_preserved_before_a_broken_visibility_step(self):
        self.uploader._registry_update_attempt(self.identity, self.attempt, {'youtube_url': ''})
        controller = mock.Mock(spec=_SemanticCdpController)
        controller.automation_target_id = 'owned-target'
        controller.bridge_tab_id = 123
        controller.upload_dialog_url.return_value = self.record['youtube_url']
        controller.youtube_submission_complete.return_value = False
        result = self.uploader._try_completed_wizard_handoff(self.request, self.sources,
            self.identity, self.attempt, controller, mock.Mock(), threading.Event())
        self.assertIsNone(result)
        current = self.uploader._registry_get(self.identity)
        self.assertEqual(current['youtube_url'], self.record['youtube_url'])
        self.assertFalse(current['transfer_complete'])
        controller.server_upload_progress.assert_not_called()
        controller.set_file.assert_not_called()


if __name__ == '__main__':
    unittest.main()
