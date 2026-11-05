import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from docurandom.ui import DocuRandomApp

def app(tmp_path,record):
    a=DocuRandomApp.__new__(DocuRandomApp);a.workspace=SimpleNamespace(state_dir=tmp_path)
    a.closed=a.closing_after_cancel=a.youtube_upload_busy=False
    a._schedules_armed=Mock(return_value=True);a.schedule_stage_upload_deferred=True
    a._start_youtube_upload=Mock()
    (tmp_path/'youtube_upload.json').write_text(json.dumps(record))
    return a

def test_known_unsent_preflight_uses_normal_automatic_gate(tmp_path):
    a=app(tmp_path,{'status':'error','progress':0})
    a.resume_unlisted_after_preflight_repair()
    a._start_youtube_upload.assert_called_once_with(automatic=True)

@pytest.mark.parametrize('change',[{'status':'uncertain'},{'attempt_id':'existing'},{'youtube_url':'https://youtu.be/existing'}, {'submitted':True},{'transfer_complete':True}])
def test_possible_prior_submission_never_creates_upload(tmp_path,change):
    a=app(tmp_path,{'status':'error',**change});a.resume_unlisted_after_preflight_repair()
    a._start_youtube_upload.assert_not_called()

def test_does_not_enable_disabled_schedule_or_force_public(tmp_path):
    a=app(tmp_path,{'status':'error'});a._schedules_armed.return_value=False
    a.resume_unlisted_after_preflight_repair();a._start_youtube_upload.assert_not_called()
    a._schedules_armed.return_value=True;a.schedule_stage_upload_deferred=False
    a.resume_unlisted_after_preflight_repair();a._start_youtube_upload.assert_not_called()
