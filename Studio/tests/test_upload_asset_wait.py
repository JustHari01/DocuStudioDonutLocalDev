from types import SimpleNamespace
from unittest.mock import Mock,patch
from pathlib import Path
from docurandom.workspace import Workspace
from docurandom.upload_asset_guard import current_asset_wait,record_asset_wait
from docurandom.youtube_upload import WorkspaceUploadSources,YouTubeMetadata
from docurandom.ui import DocuRandomApp

def setup(tmp_path):
    w=Workspace('workspace','Demo',str(tmp_path),'','');w.ensure_layout()
    video=w.output_dir/'video.mp4';video.write_bytes(b'same video')
    package=w.output_dir/'video.txt';package.write_text('missing tags')
    sources=WorkspaceUploadSources(video,package,None,YouTubeMetadata('title','description'))
    record_asset_wait(w,sources,'missing tags')
    return w,sources

def test_wait_survives_restart_and_releases_only_when_assets_change(tmp_path):
    w,sources=setup(tmp_path)
    assert current_asset_wait(w)['detail']=='missing tags'
    again=Workspace(w.workspace_id,w.name,w.root_dir,'','')
    assert current_asset_wait(again)
    sources.package_path.write_text('repaired tags')
    assert current_asset_wait(w) is None

def test_new_thumbnail_selection_releases_wait(tmp_path):
    w,sources=setup(tmp_path)
    thumb=w.output_dir/'new.png';thumb.write_bytes(b'image')
    assert current_asset_wait(w,thumbnail_path=thumb) is None

def test_unchanged_assets_never_start_another_upload_worker(tmp_path):
    w,sources=setup(tmp_path)
    app=DocuRandomApp.__new__(DocuRandomApp)
    app.workspace=w;app.closed=app.closing_after_cancel=False
    app._effective_youtube_thumbnail=Mock(return_value=None)
    app.youtube_status_var=Mock();app._schedule_log_once=Mock()
    with patch('docurandom.ui.resolve_donut_mcp_endpoint')as endpoint,patch('docurandom.ui.threading.Thread')as thread:
        for _ in range(20):app._start_youtube_upload(sources.video_path,automatic=True)
    endpoint.assert_not_called();thread.assert_not_called()

def test_old_upload_cannot_clear_new_video_asset_wait(tmp_path):
    w,_=setup(tmp_path)
    app=DocuRandomApp.__new__(DocuRandomApp);app.workspace=w;app.youtube_upload_busy=False
    app.youtube_profile_launch_blocked=False;app.youtube_status_var=Mock()
    app.schedule_active_stage='youtube-retry';app._save_config=Mock();app.youtube_uploader=Mock()
    assert app._reconcile_stale_youtube_schedule()
    app.youtube_uploader.latest_workspace_upload_state.assert_not_called()
    assert app.schedule_active_stage=='ready'

def test_old_confirmed_registry_never_discards_a_new_live_render(tmp_path):
    import json
    w,sources=setup(tmp_path)
    (w.state_dir/'youtube-asset-wait.json').write_text('{}')
    w.pipeline_checkpoint_path.write_text(json.dumps({'stages':{'render':{
        'status':'complete','artifacts':{'video':str(sources.video_path)}}}}))
    app=DocuRandomApp.__new__(DocuRandomApp);app.workspace=w;app.youtube_upload_busy=False
    app.youtube_profile_launch_blocked=False;app.youtube_status_var=Mock()
    app.schedule_active_stage='youtube-retry';app._save_config=Mock();app.youtube_uploader=Mock()
    app.youtube_uploader.latest_workspace_upload_state.return_value={'status':'complete','identity':'old','youtube_url':'https://youtu.be/OldVideo123'}
    app._start_next_continuous_workflow=Mock()
    with patch('docurandom.ui.confirmed_uploaded_session_matches',return_value=True):
        assert app._reconcile_stale_youtube_schedule()
    assert app.schedule_active_stage=='ready'
    app._start_next_continuous_workflow.assert_not_called()
