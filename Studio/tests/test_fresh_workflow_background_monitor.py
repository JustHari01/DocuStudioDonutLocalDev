"""Checkview must not prevent an explicit new cycle from starting."""
from pathlib import Path
import pytest
from docurandom.ui import DocuRandomApp
from docurandom.workspace import Workspace

@pytest.fixture
def fresh(tmp_path):
    workspace = Workspace('fresh', 'Fresh', str(tmp_path / 'workspace'), '', '')
    workspace.ensure_layout()
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.workspace = workspace
    for name in ('rendering', 'voicevox_busy', 'pipeline_voice_busy', 'content_busy',
                 'flow_generation_busy', 'thumbnail_ai_busy', 'youtube_upload_busy',
                 'smm_busy', 'smm_monitor_busy'):
        setattr(app, name, False)
    app.last_content_package = None
    app.last_video = None
    return app

@pytest.mark.parametrize('background', ['smm_busy', 'smm_monitor_busy'])
def test_fresh_cycle_can_start_while_background_checkview_runs(fresh, background):
    setattr(fresh, background, True)
    # Updating still waits for the same worker; only production is independent.
    assert fresh.has_update_blocking_tasks()
    assert fresh._prepare_fresh_content_session(confirm=False)
    assert not (fresh.workspace.root / 'Skipped').exists()
    assert not fresh.workspace.pipeline_checkpoint_path.exists()

@pytest.mark.parametrize('worker', ['rendering', 'voicevox_busy', 'pipeline_voice_busy',
                                  'content_busy', 'flow_generation_busy', 'thumbnail_ai_busy',
                                  'youtube_upload_busy'])
def test_production_and_upload_workers_still_protect_current_session(fresh, worker):
    setattr(fresh, worker, True)
    assert not fresh._prepare_fresh_content_session(confirm=False)

def test_background_monitor_does_not_authorize_discarding_unuploaded_files(fresh):
    fresh.smm_monitor_busy = True
    video = fresh.workspace.output_dir / 'Unconfirmed.mp4'
    video.write_bytes(b'keep this unconfirmed video')
    fresh._append_log = lambda text: None
    class Status:
        def set(self, value): self.value = value
    fresh.content_status_var = Status()
    assert not fresh._prepare_fresh_content_session(confirm=False)
    assert video.read_bytes() == b'keep this unconfirmed video'
    assert not (fresh.workspace.root / 'Skipped').exists()
