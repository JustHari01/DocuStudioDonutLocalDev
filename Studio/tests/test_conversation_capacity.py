from types import SimpleNamespace
from unittest.mock import Mock,patch
from docurandom.conversation_capacity import reserve_full_conversation_recovery,complete_full_conversation_recovery
from docurandom.ui import DocuRandomApp

def test_full_chat_restart_is_bounded_until_a_fresh_content_run_completes(tmp_path):
    w=SimpleNamespace(state_dir=tmp_path)
    assert reserve_full_conversation_recovery(w,'master','thread_old_1234')
    assert not reserve_full_conversation_recovery(w,'master','thread_new_5678')
    complete_full_conversation_recovery(w,'thread_old_1234')
    assert not reserve_full_conversation_recovery(w,'master','thread_new_5678')
    complete_full_conversation_recovery(w,'thread_new_5678')
    assert reserve_full_conversation_recovery(w,'master','thread_third_1234')

def test_full_chat_never_discards_a_render_or_valid_package(tmp_path):
    w=SimpleNamespace(state_dir=tmp_path)
    app=SimpleNamespace(workspace=w,_schedules_armed=lambda:True,_pipeline_auto_repair_enabled=lambda:True,
        last_content_package=None,last_video=None,youtube_upload_busy=False,
        content_runner=SimpleNamespace(chat=SimpleNamespace(thread_id='thread_old_1234')),
        content_snapshot={'master_prompt':'master'},_append_log=Mock(),root=SimpleNamespace(after_idle=Mock()))
    with patch('docurandom.ui.find_workspace_content_package',return_value=tmp_path/'video.txt'):
        assert not DocuRandomApp._restart_full_conversation_if_safe(app,'CHATGPT_CONVERSATION_FULL: full')
    with patch('docurandom.ui.find_workspace_content_package',return_value=None):
        app.last_video=tmp_path/'video.mp4'
        assert not DocuRandomApp._restart_full_conversation_if_safe(app,'CHATGPT_CONVERSATION_FULL: full')
        app.last_video=None
        assert DocuRandomApp._restart_full_conversation_if_safe(app,'CHATGPT_CONVERSATION_FULL: full')
        assert not DocuRandomApp._restart_full_conversation_if_safe(app,'CHATGPT_CONVERSATION_FULL: full')
    assert app.root.after_idle.call_count==1
