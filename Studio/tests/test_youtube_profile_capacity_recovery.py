from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, patch

import pytest

from docurandom.youtube_upload import (
    YouTubeStudioUploader, YouTubeUploadRecoveryPaused, YouTubeUploadTransferStalled,
    YouTubeUploadUncertain, ProfileUploadCoordinator, DonutProfile,
    _SemanticCdpController,
)
from docurandom.ui import DocuRandomApp


@pytest.fixture
def state(tmp_path):
    uploader = YouTubeStudioUploader(registry_path=tmp_path/'registry.json')
    record = {'status': 'uncertain', 'attempt_id': 'same-attempt', 'profile_id': 'owned',
              'profile_started_by_workflow': True, 'youtube_url': 'https://youtu.be/ExactVideo1',
              'transfer_complete': False, 'transfer_requires_content_resume': True}
    uploader._registry_set('identity', record)
    return uploader, record


def test_progress_watchdog_survives_retry_and_resets_on_any_increase(state):
    uploader, record = state
    assert not uploader._record_partial_transfer_feedback('identity', 'same-attempt', 14.7)
    first = uploader._registry_get('identity')
    old = (datetime.now(timezone.utc)-timedelta(minutes=16)).isoformat()
    uploader._registry_update_attempt('identity', 'same-attempt', {
        'transfer_feedback_progress_at': old, 'transfer_feedback_observed_at': old})
    assert uploader._record_partial_transfer_feedback('identity', 'same-attempt', 14.7)
    assert uploader._registry_get('identity')['transfer_feedback_progress_at'] == old
    assert not uploader._record_partial_transfer_feedback('identity', 'same-attempt', 14.70001)
    assert uploader._registry_get('identity')['transfer_feedback_progress_at'] != old
    assert first['attempt_id'] == 'same-attempt'


@pytest.mark.parametrize('bad_time', ['garbage', '2026-01-01T00:00:00',
                                    (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()])
def test_invalid_or_future_clock_never_authorizes_profile_stop(state, bad_time):
    uploader, record = state
    uploader._registry_update_attempt('identity', 'same-attempt', {
        'transfer_feedback_percent': 99, 'transfer_feedback_progress_at': bad_time,
        'transfer_feedback_observed_at': bad_time})
    assert not uploader._record_partial_transfer_feedback('identity', 'same-attempt', 99)


@pytest.mark.parametrize('owned,checkpoint_matches,stalled,fresh,complete,expected', [
    (True, True, True, True, False, True),
    (False, True, True, True, False, False),
    (True, False, True, True, False, False),
    (True, True, False, True, False, False),
    (True, True, True, False, False, False),
    (True, True, True, True, True, False),
])
def test_only_proven_stalled_owned_attempt_can_release_profile(
        state, owned, checkpoint_matches, stalled, fresh, complete, expected):
    uploader, record = state
    observed = datetime.now(timezone.utc)-timedelta(seconds=0 if fresh else 120)
    uploader._registry_update_attempt('identity', 'same-attempt', {
        'profile_started_by_workflow': owned, 'transfer_recovery_stalled': stalled,
        'transfer_feedback_observed_at': observed.isoformat(), 'transfer_complete': complete})
    request = SimpleNamespace(profile_id='owned', endpoint=Mock())
    client = MagicMock()
    client.__enter__.return_value = client
    client.get_profile.return_value = DonutProfile('owned','Test','wayfern',process_id=42)
    with (patch.object(uploader,'_request_identity',return_value='identity'),
          patch.object(uploader,'_upload_claim_matches_current_checkpoint',return_value=checkpoint_matches),
          patch('docurandom.youtube_upload.DonutMcpClient',return_value=client),
          patch.object(uploader,'_close_profile_started_by_workflow') as close):
        uploader._park_failed_recovery_profile(request, Mock(), YouTubeUploadTransferStalled('stalled'), Mock())
        assert close.called == expected
        if expected:
            close.assert_called_once_with(client, 'owned')
            after = uploader._registry_get('identity')
            assert after['attempt_id'] == 'same-attempt'
            assert after['youtube_url'] == 'https://youtu.be/ExactVideo1'
            assert after['transfer_requires_content_resume'] is True
        client.run_profile.assert_not_called()


@pytest.mark.parametrize('url,expected', [
    ('https://studio.youtube.com/channel/UCabcdef', True),
    ('about:blank', True),
    ('https://studio.youtube.com/video/OtherVideo1/edit', False),
    ('https://studio.youtube.com/channel/UCabcdef/videos/upload', False),
    ('https://example.com/', False),
])
def test_missing_draft_only_closes_empty_or_dashboard_profile(state, url, expected):
    uploader, record = state
    request = SimpleNamespace(profile_id='owned',endpoint=Mock())
    client = MagicMock()
    client.__enter__.return_value = client
    client.get_profile.return_value = DonutProfile('owned','Test','wayfern',process_id=42)
    client.get_debug_connection.return_value = {'targets_url':'http://127.0.0.1:9222/json'}
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps([{'type':'page','url':url}]).encode()
    with (patch.object(uploader,'_request_identity',return_value='identity'),
          patch.object(uploader,'_upload_claim_matches_current_checkpoint',return_value=True),
          patch('docurandom.youtube_upload.DonutMcpClient',return_value=client),
          patch('docurandom.youtube_upload.urllib.request.urlopen',return_value=response),
          patch.object(uploader,'_close_profile_started_by_workflow') as close):
        uploader._park_failed_recovery_profile(request,Mock(),YouTubeUploadRecoveryPaused('missing'),Mock())
        assert close.called == expected
        client.run_profile.assert_not_called()


@pytest.mark.parametrize('error', [YouTubeUploadTransferStalled('stalled'), YouTubeUploadRecoveryPaused('missing')])
def test_boundary_errors_do_not_immediately_reopen_or_reattach(state, error):
    uploader, _ = state
    with patch.object(uploader,'_upload_locked') as attach, patch.object(uploader,'_registry_get') as read:
        with pytest.raises(type(error)):
            uploader._recover_locked_upload(Mock(),Mock(),'identity',Mock(),threading.Event(),error)
        read.assert_not_called()
        attach.assert_not_called()


def test_manual_barrier_is_scoped_to_same_attempt(state):
    uploader, _ = state
    uploader._registry_update_attempt('identity','same-attempt',{'recovery_requires_manual':True})
    assert uploader.upload_recovery_requires_manual('identity','same-attempt')
    assert not uploader.upload_recovery_requires_manual('identity','new-attempt')


def test_missing_draft_cancels_reopen_timer_without_disarming_public_schedule():
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.closed = app.closing_after_cancel = False
    app.workspace = Mock()
    app.youtube_recovery_after_id = None
    app.youtube_uploader = Mock()
    app.youtube_uploader.upload_recovery_requires_manual.return_value = True
    app._youtube_recovery_guard = Mock(return_value=('identity','profile','video','package','same-attempt'))
    app._cancel_youtube_checkpoint_recovery = Mock()
    app._append_log = Mock()
    app.youtube_percent_var = Mock()
    app.youtube_status_var = Mock()
    app.schedule_armed_var = Mock()
    app.root = Mock()
    app._schedule_youtube_checkpoint_recovery('missing target')
    app._cancel_youtube_checkpoint_recovery.assert_called_once_with(reset_count=True)
    app.root.after.assert_not_called()
    app.schedule_armed_var.set.assert_not_called()
    app._schedule_youtube_checkpoint_recovery('same missing target')
    app._append_log.assert_called_once()


def test_visibility_lane_does_not_cancel_live_transfer_on_another_profile():
    coordinator = ProfileUploadCoordinator()
    transfer_cancel = threading.Event()
    with coordinator.acquire('uploading',transfer_cancel):
        with coordinator.acquire('public',threading.Event(),job_kind='metadata-recovery',priority=300,preempt_lower=False):
            assert not transfer_cancel.is_set()


@pytest.mark.parametrize('missing_profile,missing_target,expected_pause', [
    (True, True, True), (False, True, True), (False, False, False),
])
def test_readonly_waiting_inspection_frees_missing_target_without_interrupting_other_transfer(
        state, missing_profile, missing_target, expected_pause):
    uploader, _ = state
    uploader._registry_update_attempt('identity','same-attempt',
                                     {'youtube_url':'','target_id':'old-target'})
    request = SimpleNamespace(profile_id='owned',endpoint=Mock())
    client = MagicMock()
    client.__enter__.return_value = client
    client.get_profile.return_value = DonutProfile('owned','Test','wayfern',
                                                  process_id=None if missing_profile else 42)
    client.get_debug_connection.return_value = {'targets_url':'http://127.0.0.1:9222/json'}
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps([{
        'type':'page','id':'new-target' if missing_target else 'old-target',
        'url':'https://studio.youtube.com/channel/UCabcdef'}]).encode()
    transfer_cancel = threading.Event()
    with (uploader.coordinator.acquire('other-upload',transfer_cancel),
          patch.object(uploader,'_upload_claim_matches_current_checkpoint',return_value=True),
          patch('docurandom.youtube_upload.DonutMcpClient',return_value=client),
          patch('docurandom.youtube_upload.urllib.request.urlopen',return_value=response),
          patch.object(uploader,'_park_failed_recovery_profile') as park):
        if expected_pause:
            with pytest.raises(YouTubeUploadRecoveryPaused):
                uploader._inspect_waiting_recovery_profile(request,Mock(),'identity',threading.Event(),Mock())
            assert uploader._registry_get('identity')['recovery_requires_manual'] is True
            park.assert_called_once()
        else:
            uploader._inspect_waiting_recovery_profile(request,Mock(),'identity',threading.Event(),Mock())
            park.assert_not_called()
        assert not transfer_cancel.is_set()
        client.run_profile.assert_not_called()


@pytest.mark.parametrize('stalled', [False, True])
def test_waiting_inspection_reads_exact_server_progress_and_parks_only_after_watchdog(state, stalled):
    uploader, _ = state
    old = (datetime.now(timezone.utc)-timedelta(minutes=16)).isoformat()
    if stalled:
        uploader._registry_update_attempt('identity','same-attempt',{
            'transfer_feedback_percent':14.7,'transfer_feedback_progress_at':old,
            'transfer_feedback_observed_at':old})
    request = SimpleNamespace(profile_id='owned',endpoint=Mock())
    client = MagicMock()
    client.__enter__.return_value = client
    client.get_profile.return_value = DonutProfile('owned','Test','wayfern',process_id=42)
    client.get_debug_connection.return_value = {'targets_url':'http://127.0.0.1:9222/json'}
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps([{'type':'page','id':'original',
        'url':'https://studio.youtube.com/content','webSocketDebuggerUrl':'ws://127.0.0.1:9222/page/original'}]).encode()
    cdp = MagicMock()
    cdp.__enter__.return_value = cdp
    cdp.upload_feedback.return_value = (14.7,'remaining 24 minutes')
    with (patch.object(uploader,'_upload_claim_matches_current_checkpoint',return_value=True),
          patch('docurandom.youtube_upload.DonutMcpClient',return_value=client),
          patch('docurandom.youtube_upload.urllib.request.urlopen',return_value=response),
          patch('docurandom.youtube_upload._CdpSession',return_value=cdp),
          patch.object(uploader,'_park_failed_recovery_profile') as park):
        if stalled:
            with pytest.raises(YouTubeUploadTransferStalled):
                uploader._inspect_waiting_recovery_profile(request,Mock(),'identity',threading.Event(),Mock())
            park.assert_called_once()
        else:
            uploader._inspect_waiting_recovery_profile(request,Mock(),'identity',threading.Event(),Mock())
            park.assert_not_called()
        cdp.upload_feedback.assert_called_once_with('ExactVideo1',timeout=5)
        cdp.command.assert_not_called()
        client.run_profile.assert_not_called()


def test_failed_waiting_diagnostic_never_stops_profile(state):
    uploader, _ = state
    request = SimpleNamespace(profile_id='owned',endpoint=Mock())
    with (patch.object(uploader,'_upload_claim_matches_current_checkpoint',return_value=True),
          patch('docurandom.youtube_upload.DonutMcpClient',side_effect=TimeoutError),
          patch.object(uploader,'_park_failed_recovery_profile') as park):
        uploader._inspect_waiting_recovery_profile(request,Mock(),'identity',threading.Event(),Mock())
        park.assert_not_called()


def test_waiting_inspection_rechecks_state_after_waiting_for_profile_lease(state):
    uploader, _ = state
    request = SimpleNamespace(profile_id='owned',endpoint=Mock())
    lease = MagicMock()
    lease.__enter__.side_effect = lambda: uploader._registry_update_attempt(
        'identity','same-attempt',{'status':'submitting'})
    with (patch.object(uploader,'_upload_claim_matches_current_checkpoint',return_value=True),
          patch.object(uploader.coordinator,'acquire',return_value=lease),
          patch('docurandom.youtube_upload.DonutMcpClient') as client):
        uploader._inspect_waiting_recovery_profile(request,Mock(),'identity',threading.Event(),Mock())
        client.assert_not_called()


@pytest.mark.parametrize('dialog_text,status_text,status_visible,expected', [
    ('Quá trình xử lý bị chậm trễ đến vài giờ.', 'Quá trình xử lý bị chậm trễ đến vài giờ.', True, True),
    ('Processing delayed for several hours.', 'Processing delayed for several hours.', True, True),
    ('Đã tải lên 97% Quá trình xử lý bị chậm trễ đến vài giờ.', 'Quá trình xử lý bị chậm trễ đến vài giờ.', True, False),
    ('Quá trình xử lý bị chậm trễ đến vài giờ.', 'Đã tải lên 15%', True, False),
    ('Title mentions processing delayed', 'Uploading file', True, False),
    ('Processing delayed for several hours.', 'Processing delayed for several hours.', False, False),
])
def test_processing_delay_handoff_requires_the_live_dedicated_transfer_status(
        dialog_text,status_text,status_visible,expected):
    bridge = Mock()
    def query(_profile,command,params,*,timeout):
        assert command=='youtube_query'
        if params['selector']=='ytcp-uploads-dialog':
            return {'found':True,'laidOut':True,'text':dialog_text}
        if params['selector']=='ytcp-video-upload-progress':
            return {'found':True,'laidOut':status_visible,'text':status_text}
        return {'found':False}
    bridge.command.side_effect=query
    controller=_SemanticCdpController(Mock(),browser_bridge=bridge,
                                     bridge_profile_id='owned',bridge_tab_id=42)
    assert controller.youtube_submission_complete() is expected


@pytest.mark.parametrize('video_id,status_text,duplicate,expected', [
    ('ExactVideo1','Quá trình xử lý bị chậm trễ đến vài giờ.',False,True),
    ('ExactVideo1','Processing delayed for several hours.',False,True),
    ('OtherVideo1','Processing delayed for several hours.',False,False),
    ('ExactVideo1','Processing delayed. Đã tải lên 97%',False,False),
    ('ExactVideo1','Uploading 99%',False,False),
    ('ExactVideo1','Checks complete',False,False),
    ('ExactVideo1','Video đã tải lên xong Đang xử lý video',False,True),
    ('ExactVideo1','Processing delayed for several hours.',True,False),
])
def test_parked_processing_status_is_bound_to_one_exact_original_wizard(
        video_id,status_text,duplicate,expected):
    status={'nodeName':'YTCP-VIDEO-UPLOAD-PROGRESS','children':[
        {'nodeName':'SPAN','children':[{'nodeName':'#text','nodeValue':status_text}]}]}
    dialog={'nodeName':'YTCP-UPLOADS-DIALOG','attributes':['video-id','ExactVideo1'],
            'shadowRoots':[{'nodeName':'#document-fragment','children':[status]}]}
    cdp=Mock()
    cdp.command.return_value={'root':{'nodeName':'#document','children':
        [dialog,dialog] if duplicate else [dialog]}}
    controller=_SemanticCdpController(cdp)
    assert controller.youtube_parked_transfer_status_complete(video_id) is expected
    cdp.command.assert_called_once_with('DOM.getDocument',{'depth':-1,'pierce':True})


@pytest.mark.parametrize('server_percent,live_percent,delay_proof,probe_id,expected', [
    (None,None,True,'ExactVideo1',True),
    (None,None,False,'ExactVideo1',False),
    (97.0,None,True,'ExactVideo1',False),
    (None,97.0,True,'ExactVideo1',False),
    (None,None,True,'OtherVideo1',False),
])
def test_fresh_content_and_parked_processing_proof_require_both_observations(
        server_percent,live_percent,delay_proof,probe_id,expected):
    controller=Mock()
    controller.cdp=None
    controller.youtube_url.return_value='https://youtu.be/ExactVideo1'
    controller.youtube_submission_complete.return_value=False
    controller.youtube_upload_progress.return_value=(live_percent,'')
    controller.server_upload_progress.return_value=(server_percent,'')
    controller.youtube_parked_transfer_status_complete.return_value=delay_proof
    url,complete=YouTubeStudioUploader._wait_for_upload_transfer_complete(
        controller,Mock(),threading.Event(),timeout=.04,poll_interval=.001,
        draft_content_probe=lambda:f'https://youtu.be/{probe_id}',
        draft_probe_interval=.005,draft_handoff_after=.001)
    assert complete is expected
    assert url==f'https://youtu.be/{probe_id}' if expected else url=='https://youtu.be/ExactVideo1'
