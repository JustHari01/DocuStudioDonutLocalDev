import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import docurandom.youtube_like_monitor as monitor


class CheckviewProfileOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'owners.json'
        for name, value in (
            ('_CHECKVIEW_PROFILE_OWNERS_PATH', self.path),
            ('_CHECKVIEW_TARGET_RECORDS', {'profile': {'target_id': 'checkview'}}),
        ):
            patcher = mock.patch.object(monitor, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(monitor, '_checkview_process_birth', return_value=987654321)
        self.birth = patcher.start()
        self.addCleanup(patcher.stop)
        self.source = monitor.DonutYouTubeRealtimeSource('http://127.0.0.1:21690')
        self.client = mock.Mock()
        self.profile = SimpleNamespace(process_id=1234, browser='wayfern')
        self.client.get_profile.return_value = self.profile
        self.client.get_debug_connection.return_value = {'targets_url':'http://127.0.0.1:12345/json'}
        self.pages = [
            {'type':'page','id':'checkview','url':'https://studio.youtube.com/video/ExactId1234/analytics/tab-overview'},
            {'type':'page','id':'original','url':'https://studio.youtube.com/channel/UCExactChannel'},
        ]
        patcher = mock.patch.object(monitor, '_read_debug_targets', side_effect=lambda _: self.pages)
        patcher.start()
        self.addCleanup(patcher.stop)

    def remember(self):
        monitor._remember_checkview_profile('profile',1234)

    def test_started_scan_records_exact_process_identity(self):
        self.client.get_profile.side_effect = [SimpleNamespace(process_id=None,browser='wayfern'),self.profile]
        self.client.run_profile.return_value = 'started'
        self.client.profile_debug_ready.return_value = True
        self.assertTrue(self.source._wait_for_profile(self.client,'profile',threading.Event()))
        owner = monitor._load_checkview_profile_owners()['profile']
        self.assertEqual((owner['process_id'],owner['process_birth']),(1234,987654321))

    def test_restart_reclaims_and_closes_owned_idle_session_after_scan(self):
        self.remember()
        self.client.profile_debug_ready.return_value = True
        self.source._scan_open_profile_once = mock.Mock(return_value=())
        with mock.patch.object(self.source,'_close_profile_opened_for_scan') as close:
            self.assertEqual(self.source._scan_profile_once(self.client,'profile',cancel=threading.Event(),report=mock.Mock()),())
        self.client.run_profile.assert_not_called()
        close.assert_called_once()

    def test_restart_cleans_owned_session_even_when_scan_fails(self):
        self.remember()
        self.client.profile_debug_ready.return_value = True
        self.source._scan_open_profile_once = mock.Mock(side_effect=monitor.YouTubeRealtimeError('Incomplete 60 minute scan'))
        with mock.patch.object(self.source,'_close_profile_opened_for_scan') as close:
            with self.assertRaises(monitor.YouTubeRealtimeError):
                self.source._scan_profile_once(self.client,'profile',cancel=threading.Event(),report=mock.Mock())
        close.assert_called_once()

    def test_saved_checkview_target_does_not_own_manual_browser(self):
        self.assertFalse(self.source._retained_scan_profile_can_close(self.client,'profile'))
        self.client.get_debug_connection.assert_not_called()

    def test_process_restart_or_pid_reuse_revokes_old_ownership(self):
        for new_pid, birth in ((9999,987654321),(1234,99999)):
            with self.subTest(pid=new_pid,birth=birth):
                self.birth.return_value = 987654321
                self.remember()
                self.client.get_profile.return_value = SimpleNamespace(process_id=new_pid)
                self.birth.return_value = birth
                self.assertFalse(self.source._retained_scan_profile_can_close(self.client,'profile'))
                self.assertNotIn('profile',monitor._load_checkview_profile_owners())

    def test_extra_user_upload_editor_or_analytics_tab_is_preserved(self):
        self.remember()
        for url in ('https://example.com/','https://studio.youtube.com/video/ExactId1234/edit',
                    'https://studio.youtube.com/channel/UCExactChannel/videos/upload',
                    'https://studio.youtube.com/video/ExactId1234/analytics/tab-overview'):
            with self.subTest(url=url):
                self.pages.append({'type':'page','id':'user-tab','url':url})
                self.assertFalse(self.source._retained_scan_profile_can_close(self.client,'profile'))
                self.pages.pop()
        self.assertIn('profile',monitor._load_checkview_profile_owners())

    def test_owned_target_navigated_to_upload_cannot_be_closed(self):
        self.remember()
        self.pages[0]['url'] = 'https://studio.youtube.com/channel/UCExactChannel/videos/upload'
        self.assertFalse(self.source._retained_scan_profile_can_close(self.client,'profile'))

    def test_missing_process_birth_cannot_adopt_profile_when_read_fails(self):
        self.path.write_text(json.dumps({'profiles':{'profile':{'process_id':1234}}}),encoding='utf-8')
        self.birth.return_value = None
        self.assertFalse(self.source._retained_scan_profile_can_close(self.client,'profile'))

    def test_corrupt_ledger_fails_closed_and_is_not_overwritten(self):
        self.path.write_text('{broken',encoding='utf-8')
        self.assertFalse(self.source._retained_scan_profile_can_close(self.client,'profile'))
        with self.assertRaises(monitor.YouTubeRealtimeError): self.remember()
        self.assertEqual(self.path.read_text(encoding='utf-8'),'{broken')

    def test_unavailable_catalog_preserves_session_and_ownership(self):
        self.remember()
        self.client.get_debug_connection.side_effect = OSError('CDP unavailable')
        self.assertFalse(self.source._retained_scan_profile_can_close(self.client,'profile'))
        self.assertIn('profile',monitor._load_checkview_profile_owners())

    def test_process_change_during_final_check_is_preserved(self):
        self.remember()
        self.client.get_profile.side_effect = [self.profile,SimpleNamespace(process_id=9999)]
        self.assertFalse(self.source._retained_scan_profile_can_close(self.client,'profile'))

    def test_confirmed_close_removes_durable_ownership(self):
        self.remember()
        self.client.get_profile.return_value = SimpleNamespace(process_id=None)
        self.source._close_profile_opened_for_scan(self.client,'profile',mock.Mock())
        self.client.call_tool.assert_called_once_with('batch_stop_profiles',{'profile_ids':['profile']},timeout=20)
        self.assertNotIn('profile',monitor._load_checkview_profile_owners())


if __name__ == '__main__': unittest.main()
