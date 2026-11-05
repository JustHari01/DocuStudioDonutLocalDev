"""Regression coverage for Public A -> upload Unlisted B, using local fixtures."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from docurandom.donut_mcp import DonutMcpEndpoint
from docurandom.session_archive import mark_deferred_public_published
from docurandom.ui import DocuRandomApp
from docurandom.workspace import Workspace
from docurandom.workspace_scheduler import ScheduleShift, ScheduleEvaluation, VIETNAM_TIMEZONE
from docurandom.youtube_upload import (
    YouTubeStudioUploader, YouTubeUploadRequest, YouTubeUploadWaitingForPublic,
    has_pending_prior_public, resolve_workspace_upload_sources,
)

class Var:
    def __init__(self, value): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value

@pytest.fixture
def chain(tmp_path):
    workspace = Workspace('chain', 'Chain', str(tmp_path/'workspace'), '', '')
    workspace.ensure_layout()
    video = workspace.output_dir/'B.mp4'
    video.write_bytes(b'completed successor B')
    package = workspace.output_dir/'B.txt'
    package.write_text('TIÊU ĐỀ\n\nVideo B\n\nMÔ TẢ\n\nDistinct video B content\n\nSEO TAGS\n\nJapanese technology\n', encoding='utf-8')
    thumbnail = workspace.output_dir / 'B-thumbnail.png'
    Image.new('RGB', (1280, 720), (10, 20, 30)).save(thumbnail)
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.workspace = workspace
    app.youtube_thumbnail_var = Var(str(thumbnail))
    app.closed = app.closing_after_cancel = app.youtube_upload_busy = False
    app.schedule_armed_var = Var(True)
    app.schedule_shifts = [ScheduleShift(str(h), '05/10/2026 01:00', f'05/10/2026 {h:02}:30')
                           for h in (4, 8, 13)]
    app.schedule_active_shift_id = '4'
    app.schedule_active_stage = 'ready'
    app.schedule_stage_after_public = True
    app.schedule_stage_upload_deferred = app.schedule_deferred_upload = False
    app.schedule_empty_queue_public_fallback = False
    app.schedule_manual_resume_required = False
    app.schedule_last_event = ''
    app.youtube_status_var = Var('')
    app.youtube_percent_var = Var('')
    app.youtube_pending_videos = []
    app.youtube_selected_profile_id = 'profile'
    app._scheduled_video_is_ready = Mock(return_value=True)
    for name in ('_save_config','_append_log','_append_exception_log','_schedule_log_once',
                 '_start_next_continuous_workflow','_start_deferred_public_promotion'):
        setattr(app, name, Mock())
    app._start_youtube_upload = Mock(side_effect=lambda **kw: setattr(app,'youtube_upload_busy',True))
    request = YouTubeUploadRequest(workspace, DonutMcpEndpoint.parse(
        'http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv'), 'profile',
        privacy='PUBLIC', video_path=video, package_path=package, thumbnail_path=thumbnail,
        hold_public_until_next_morning=True, wait_for_prior_public=True)
    uploader = YouTubeStudioUploader(registry_path=tmp_path/'registry.json')
    uploader._wait_for_existing_file_transfers = Mock()
    uploader._execute_locked_upload = Mock(return_value=object())
    uploader.coordinator = Mock()
    uploader.coordinator.acquire.return_value.__enter__ = Mock()
    uploader.coordinator.acquire.return_value.__exit__ = Mock(return_value=False)
    return app, request, uploader

def queue(workspace, *identities):
    items = [{'identity': i,'profile_id':'profile','youtube_url':f'https://youtu.be/{i}',
              'privacy':'UNLISTED','final_privacy':'PUBLIC'} for i in identities]
    (workspace.state_dir/'deferred_public_queue.json').write_text(
        json.dumps({'version':1,'items':items}), encoding='utf-8')

def at(hour, minute=0, day=5):
    return datetime(2026,10,day,hour,minute,tzinfo=VIETNAM_TIMEZONE)

def test_a_unlisted_blocks_b_until_verified_public_then_b_blocks_c(chain):
    app, request, _ = chain
    queue(app.workspace,'A')
    before = (app.workspace.state_dir/'deferred_public_queue.json').read_bytes()
    assert not app._stage_ready_video_for_schedule_lane(at(3))
    assert not app._stage_ready_video_for_schedule_lane(at(4,30))
    app._start_youtube_upload.assert_not_called()
    assert (app.workspace.state_dir/'deferred_public_queue.json').read_bytes() == before
    # Public completion, rather than start/claim, releases B's upload.
    assert mark_deferred_public_published(app.workspace,'A',now=at(4,31))
    with patch('docurandom.ui.vietnam_now',return_value=at(4,31)):
        app._continue_after_public_promotion()
    app._start_youtube_upload.assert_called_once_with(automatic=True,defer_public_until_slot=True)
    queue(app.workspace,'B')
    app.youtube_upload_busy = False
    app.schedule_active_stage = 'ready'
    assert not app._stage_ready_video_for_schedule_lane(at(6))
    assert app._start_youtube_upload.call_count == 1

def test_legacy_backlog_stays_intact_and_blocks_new_upload_after_one_public(chain):
    app, _, _ = chain
    queue(app.workspace,'A','B','C')
    assert mark_deferred_public_published(app.workspace,'A',now=at(4,31))
    with patch('docurandom.ui.vietnam_now',return_value=at(4,31)):
        app._continue_after_public_promotion()
    app._start_youtube_upload.assert_not_called()
    items=json.loads((app.workspace.state_dir/'deferred_public_queue.json').read_text())['items']
    assert [i['identity'] for i in items] == ['B','C']

def test_uploader_refuses_fresh_b_before_using_profile_or_writing_checkpoint(chain):
    app, request, uploader = chain
    queue(app.workspace,'A')
    with patch('docurandom.youtube_upload._wait_for_render_upload_ready'), pytest.raises(YouTubeUploadWaitingForPublic):
        uploader.upload(request)
    uploader.coordinator.acquire.assert_not_called()
    uploader._execute_locked_upload.assert_not_called()
    assert not (app.workspace.state_dir/'youtube_upload.json').exists()
    assert not uploader.registry_path.exists()

def test_uploader_rechecks_barrier_after_waiting_in_global_lane(chain):
    app, request, uploader = chain
    @contextmanager
    def lease(*args,**kwargs):
        queue(app.workspace,'A')
        yield
    uploader.coordinator.acquire.side_effect = lease
    with patch('docurandom.youtube_upload._wait_for_render_upload_ready'), pytest.raises(YouTubeUploadWaitingForPublic):
        uploader.upload(request)
    uploader._execute_locked_upload.assert_not_called()

def test_existing_exact_b_draft_can_reconcile_without_new_upload(chain):
    app, request, uploader = chain
    queue(app.workspace,'A')
    sources=resolve_workspace_upload_sources(request.workspace,video_path=request.video_path,package_path=request.package_path)
    identity=uploader._request_identity(request,sources)
    record={'identity':identity,'attempt_id':'existing-B','status':'uncertain','progress':96,
            'profile_id':'profile','youtube_url':'https://youtu.be/ExactDraftB'}
    uploader._registry_set(identity,record)
    (app.workspace.state_dir/'youtube_upload.json').write_text(json.dumps(record),encoding='utf-8')
    with patch('docurandom.youtube_upload._wait_for_render_upload_ready'):
        uploader.upload(request)
    uploader._execute_locked_upload.assert_called_once()
    assert uploader._execute_locked_upload.call_args.args[4]['attempt_id']=='existing-B'
    assert uploader._execute_locked_upload.call_args.args[3] is None

@pytest.mark.parametrize('contents',['{bad','[]','{}'])
def test_unreadable_public_queue_fails_closed_without_touching_data(chain,contents):
    app, request, uploader=chain
    p=app.workspace.state_dir/'deferred_public_queue.json'; p.write_text(contents,encoding='utf-8')
    with pytest.raises(YouTubeUploadWaitingForPublic):
        has_pending_prior_public(app.workspace)
    with patch('docurandom.youtube_upload._wait_for_render_upload_ready'),pytest.raises(YouTubeUploadWaitingForPublic):
        uploader.upload(request)
    assert p.read_text()==contents
    uploader._execute_locked_upload.assert_not_called()

def test_direct_automatic_start_cannot_bypass_gate_with_queued_checkpoint(chain):
    app, request, _=chain
    queue(app.workspace,'A')
    (app.workspace.state_dir/'youtube_upload.json').write_text(json.dumps({'status':'queued','progress':0}),encoding='utf-8')
    with patch('docurandom.ui.confirmed_manual_upload',return_value=None),patch('docurandom.ui.resolve_donut_mcp_endpoint') as endpoint:
        DocuRandomApp._start_youtube_upload(app,request.video_path,automatic=True)
    endpoint.assert_not_called()
    assert app.schedule_active_stage=='ready'
    assert request.video_path.exists()


def test_resumed_automatic_draft_stays_unlisted_even_if_staging_flag_was_lost(chain):
    app, request, _ = chain
    queue(app.workspace,'A')
    (app.workspace.state_dir/'youtube_upload.json').write_text(json.dumps({
        'status':'uncertain','video_path':str(request.video_path),
        'youtube_url':'https://youtu.be/ExactDraftB'}),encoding='utf-8')
    app.last_content_package=request.package_path
    app.youtube_privacy_var=Var('Công khai')
    app.youtube_ai_var=Var(True)
    app.youtube_thumbnail_var=Var('')
    app.smm_monitor_busy=False
    app.youtube_upload_token=0
    app.youtube_progress_var=Var(0)
    app._cancel_youtube_checkpoint_recovery=Mock()
    app._automatic_thumbnail_required_but_missing=Mock(return_value=False)
    app._effective_youtube_thumbnail=Mock(return_value=None)
    app._should_hold_public_until_morning=Mock(return_value=False)
    app._refresh_youtube_controls=Mock()
    with patch('docurandom.ui.confirmed_manual_upload',return_value=None),\
         patch('docurandom.ui.resolve_donut_mcp_endpoint',return_value=request.endpoint),\
         patch('docurandom.ui.YouTubeUploadRequest',wraps=YouTubeUploadRequest) as constructor,\
         patch('docurandom.ui.threading.Thread'):
        DocuRandomApp._start_youtube_upload(app,request.video_path,automatic=True)
    assert constructor.call_args.kwargs['hold_public_until_next_morning'] is True
    assert constructor.call_args.kwargs['wait_for_prior_public'] is True

@pytest.mark.parametrize('hour,minute,allowed',[(0,1,False),(4,29,False),(4,30,True),(8,30,True),(13,30,True),(15,0,True),(15,30,False),(23,59,False)])
def test_public_window_does_not_reopen_at_midnight(chain,hour,minute,allowed):
    app,_,_=chain
    assert app._automatic_public_promotion_allowed(at(hour,minute)) is allowed

def test_prior_day_missed_public_is_advanced_without_publishing(chain):
    app,_,_=chain
    old=at(8,30,day=4)
    app.schedule_shifts[1]=replace(app.schedule_shifts[1],upload_at='04/10/2026 08:30')
    queue(app.workspace,'A')
    with patch('docurandom.ui.vietnam_now',return_value=at(0,1)):
        app._trigger_scheduled_upload(ScheduleEvaluation('due',old,'old-0830'),shift_id='8')
    assert app.schedule_shifts[1].upload_at=='05/10/2026 08:30'
    app._start_youtube_upload.assert_not_called()
    app._start_deferred_public_promotion.assert_not_called()
    assert has_pending_prior_public(app.workspace)
