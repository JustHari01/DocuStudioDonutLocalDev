import threading
from types import SimpleNamespace, MethodType
from unittest.mock import Mock
from docurandom.ui import DocuRandomApp

def test_native_skip_cancels_thumbnail_only_worker_and_stale_bridge_callback():
    app=SimpleNamespace(explicit_skip_pending=False, thumbnail_ai_waiting_for_bridge_package='old-package',
        thumbnail_ai_busy=True,thumbnail_ai_token=3,thumbnail_ai_cancel_event=threading.Event(),thumbnail_ai_chat=Mock(),
        content_token=0,pipeline_voice_token=0,voicevox_token=0,voicevox_audit_token=0,flow_check_token=0,
        codex_check_token=0,scan_token=0,audio_probe_token=0,youtube_upload_token=0,
        ignored_render_job_ids=set(),job_states={},scan_cancel_event=threading.Event(),
        content_busy=False,flow_generation_busy=False,pipeline_voice_busy=False,voicevox_busy=False,rendering=False,
        youtube_upload_busy=False,root=Mock())
    for name in ('_cancel_thumbnail_ai_retry','_append_log','_cancel_schedule_tick','_cancel_youtube_checkpoint_recovery',
                 '_cancel_pending_pipeline_auto_resume','_cancel_audio_render_preflight','_stop_smm_monitor',
                 '_save_config','_refresh_global_state','_finish_explicit_workspace_skip_when_idle'):
        setattr(app,name,Mock())
    app._cancel_thumbnail_ai_for_replacement=MethodType(DocuRandomApp._cancel_thumbnail_ai_for_replacement,app)
    chat=app.thumbnail_ai_chat
    DocuRandomApp._request_explicit_workspace_skip(app,reason='User approved this exact turn')
    assert app.explicit_skip_pending
    assert app.thumbnail_ai_cancel_event.is_set()
    chat.cancel.assert_called_once()
    assert app.thumbnail_ai_chat is None and not app.thumbnail_ai_busy
    assert app.thumbnail_ai_waiting_for_bridge_package is None
    assert app.thumbnail_ai_token>3
    app._cancel_thumbnail_ai_retry.assert_called_once()
