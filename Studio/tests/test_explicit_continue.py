import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from docurandom.telegram_bot import TelegramCommand

def handler(app):
    # Exercise the production dispatcher without creating a second live hub.
    source=Path(__file__).parents[1]/'docurandom/ui.py'
    tree=ast.parse(source.read_text(encoding='utf-8-sig'))
    function=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='execute_telegram_command')
    function.body=[n for n in function.body if not isinstance(n,ast.Nonlocal)]
    namespace={'TelegramCommand':TelegramCommand,
        'telegram_workspace_resolution':lambda _args:([app.workspace],''),
        'ensure_workspace_session':lambda _workspace:app,
        'telegram_target_app':lambda _selector:app}
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),'exec'),namespace)
    return namespace['execute_telegram_command']

def test_explicit_continue_acknowledges_manual_hold_but_active_worker_is_not_restarted():
    app=SimpleNamespace(workspace=SimpleNamespace(name='DaiTa10',workspace_id='a'*32),
        has_active_user_tasks=Mock(return_value=True),has_update_blocking_tasks=Mock(return_value=False),
        scanning=False,audio_preflight_active=False,
        _clear_manual_pipeline_resume_barrier=Mock(),_start_content_pipeline=Mock())
    execute=handler(app)
    command=TelegramCommand(0,'continue','a'*32,'local maintenance')
    assert 'đã nhận lệnh tiếp tục' in execute(command)
    app._start_content_pipeline.assert_called_once_with(resume=True,acknowledge_terminal_barrier=True)
    app.has_update_blocking_tasks.assert_called_once_with(include_background_monitor=False)
    app.has_update_blocking_tasks.return_value=True
    assert 'không tạo lượt tiếp tục trùng' in execute(command)
    assert app._start_content_pipeline.call_count==1


def test_background_checkview_cannot_block_content_continue_but_blocks_update():
    from docurandom.ui import DocuRandomApp
    app=SimpleNamespace(rendering=False,voicevox_busy=False,content_busy=False,
        flow_generation_busy=False,youtube_upload_busy=False,smm_busy=False,smm_monitor_busy=True)
    assert DocuRandomApp.has_update_blocking_tasks(app)
    assert not DocuRandomApp.has_update_blocking_tasks(app,include_background_monitor=False)
    for flag in ('rendering','voicevox_busy','content_busy','flow_generation_busy','youtube_upload_busy'):
        setattr(app,flag,True)
        assert DocuRandomApp.has_update_blocking_tasks(app,include_background_monitor=False)
        setattr(app,flag,False)


def test_local_skip_uses_native_archive_once_and_rejects_replacement():
    app=SimpleNamespace(workspace=SimpleNamespace(name='DaiTa13',workspace_id='a'*32),
        _local_maintenance_skip_allowed=Mock(return_value=True),
        reset_workspace_checkpoint=Mock(return_value=(True,'stopping and archiving')))
    execute=handler(app)
    command=TelegramCommand(0,'skip','a'*32,'local maintenance')
    assert 'Đã nhận Skip' in execute(command)
    app.reset_workspace_checkpoint.assert_called_once_with()
    app._local_maintenance_skip_allowed.return_value=False
    assert 'không Skip video mới' in execute(command)
    assert app.reset_workspace_checkpoint.call_count==1


def test_local_skip_accepts_failed_auto_recovery_but_preserves_running_and_completed_turns(tmp_path):
    import json
    from docurandom.ui import DocuRandomApp
    from docurandom.workspace import Workspace
    workspace=Workspace('a'*32,'DaiTa4',str(tmp_path/'workspace'),'','')
    workspace.ensure_layout()
    app=SimpleNamespace(workspace=workspace,explicit_skip_pending=False,
        schedule_manual_resume_required=False,scanning=False,audio_preflight_active=False,
        has_update_blocking_tasks=Mock(return_value=False))
    allowed=lambda:DocuRandomApp._local_maintenance_skip_allowed(app)
    assert not allowed()
    workspace.pipeline_checkpoint_path.write_text(json.dumps({'stages':{'chatgpt':{'status':'error'}}}),encoding='utf-8')
    assert allowed()
    app.has_update_blocking_tasks.assert_called_with(include_background_monitor=False)
    app.has_update_blocking_tasks.return_value=True
    assert not allowed()
    app.has_update_blocking_tasks.return_value=False
    workspace.pipeline_checkpoint_path.write_text(json.dumps({'stages':{'chatgpt':{'status':'complete'},'voicevox':{'status':'pending'}}}),encoding='utf-8')
    (workspace.state_dir/'thumbnail-ai.json').write_text(json.dumps({'status':'error','submitted':True}),encoding='utf-8')
    assert allowed()
    queued=workspace.state_dir/'deferred_public_queue.json'
    queued.write_text(json.dumps({'items':[{'identity':'previous-unlisted'}]}),encoding='utf-8')
    before=queued.read_bytes()
    assert allowed() and queued.read_bytes()==before
    upload=workspace.state_dir/'youtube_upload.json'
    upload.write_text(json.dumps({'status':'deferred_public','video_id':'already-uploaded'}),encoding='utf-8')
    assert not allowed()
    upload.unlink()
    workspace.pipeline_checkpoint_path.write_text(json.dumps({'stages':{'chatgpt':{'status':'error'},'render':{'status':'complete'}}}),encoding='utf-8')
    assert not allowed()
    workspace.pipeline_checkpoint_path.write_text(json.dumps({'stages':{'chatgpt':{'status':'running'}}}),encoding='utf-8')
    (workspace.state_dir/'thumbnail-ai.json').unlink()
    assert not allowed()


def test_bound_skip_can_cancel_failed_thumbnail_queue_but_cannot_touch_replacement(tmp_path):
    import json
    from docurandom.ui import DocuRandomApp
    from docurandom.workspace import Workspace
    workspace=Workspace('a'*32,'DaiTa12',str(tmp_path/'workspace'),'','')
    workspace.ensure_layout()
    app=SimpleNamespace(workspace=workspace,explicit_skip_pending=False,
        schedule_manual_resume_required=False,scanning=False,audio_preflight_active=False,
        has_update_blocking_tasks=Mock(return_value=True))
    ai=workspace.output_dir/'DaiTa12_AI_CHECKPOINT.json'
    old_thread='thread_docurandom_old_failed_turn'
    ai.write_text(json.dumps({'thread_id':old_thread}),encoding='utf-8')
    workspace.pipeline_checkpoint_path.write_text(json.dumps({'identity':{'project_name':'DaiTa12'},
        'stages':{'chatgpt':{'status':'complete'},'voicevox':{'status':'pending'}}}),encoding='utf-8')
    thumb=workspace.state_dir/'thumbnail-ai.json'
    thumb.write_text(json.dumps({'status':'recovering','thread_id':old_thread,
        'detail':'CHATGPT_CONVERSATION_LOAD_FAILED: bounded recovery failed'}),encoding='utf-8')
    allowed=lambda thread='':DocuRandomApp._local_maintenance_skip_allowed(app,thread)
    assert not allowed()
    assert not allowed('thread_docurandom_different_turn')
    assert allowed(old_thread)
    before=workspace.pipeline_checkpoint_path.read_bytes()
    ai.write_text(json.dumps({'thread_id':'thread_docurandom_new_turn'}),encoding='utf-8')
    assert not allowed(old_thread)
    assert workspace.pipeline_checkpoint_path.read_bytes()==before
    ai.write_text(json.dumps({'thread_id':old_thread}),encoding='utf-8')
    (workspace.state_dir/'youtube_upload.json').write_text(json.dumps({'status':'deferred_public'}),encoding='utf-8')
    assert not allowed(old_thread)


def test_dispatcher_passes_expected_thread_to_native_skip_guard():
    app=SimpleNamespace(workspace=SimpleNamespace(name='DaiTa12',workspace_id='a'*32),
        _local_maintenance_skip_allowed=Mock(return_value=True),
        reset_workspace_checkpoint=Mock(return_value=(True,'stopping and archiving')))
    command=TelegramCommand(0,'skip','a'*32,'local maintenance',expected_thread_id='thread_docurandom_failed')
    assert 'Đã nhận Skip' in handler(app)(command)
    app._local_maintenance_skip_allowed.assert_called_once_with('thread_docurandom_failed')
    app.reset_workspace_checkpoint.assert_called_once_with()
