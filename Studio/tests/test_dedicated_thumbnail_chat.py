import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from docurandom.thumbnail_ai import (claim_thumbnail_ai_generation, read_thumbnail_ai_state,
    update_thumbnail_ai_state, prepare_isolated_thumbnail_chat)
from docurandom.conversation_links import retain_thumbnail_link
from docurandom.ui import DocuRandomApp
from tests.test_thumbnail_isolated_recovery import setup


def test_each_package_reserves_its_own_thumbnail_thread_and_restart_keeps_it(tmp_path):
    w,p,old,proof,data=setup(tmp_path)
    cp=p.with_name(p.stem+'_AI_CHECKPOINT.json')
    cp.write_text(json.dumps({'thread_id':'content-master'}))
    with patch('docurandom.storage.app_data_dir',return_value=data):
        claim=claim_thumbnail_ai_generation(w,p,dedicated_chat=True,content_thread_id='content-master')
        first=read_thumbnail_ai_state(w)
        assert claim.action=='start'
        assert first['thread_id']!='content-master'
        assert first['isolated_chat']['content_thread_id']=='content-master'
        assert prepare_isolated_thumbnail_chat(w,p,'content-master')==first['thread_id']
        second=claim_thumbnail_ai_generation(w,p,dedicated_chat=True,content_thread_id='content-master')
        assert second.action=='blocked'
        assert read_thumbnail_ai_state(w)['thread_id']==first['thread_id']
        q=w.output_dir/'second.txt';q.write_text(p.read_text().replace('Create exactly this picture','Create another picture'))
        claim2=claim_thumbnail_ai_generation(w,q,dedicated_chat=True,content_thread_id='second-content')
        assert claim2.action=='start'
        assert read_thumbnail_ai_state(w)['thread_id'] not in {first['thread_id'],'second-content'}


def test_user_policy_migration_archives_uncertainty_without_touching_old_native_receipt_or_video(tmp_path):
    w,p,claim,proof,data=setup(tmp_path)
    update_thumbnail_ai_state(w,p,claim,status='uncertain',submitted=True,thread_id='old_video_thread',
        automatic_retry_count=3,detail='old pending receipt')
    old=read_thumbnail_ai_state(w);old.update(readonly_probe_count=4,recovery_probe_count=4)
    (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(old))
    bridge=data/'chat-bridge/bridge-state.json';raw=bridge.read_bytes()
    video=p.with_suffix('.mp4');video.write_bytes(b'original rendered video')
    with patch('docurandom.storage.app_data_dir',return_value=data):
        new=claim_thumbnail_ai_generation(w,p,dedicated_chat=True,content_thread_id='content-master')
    state=read_thumbnail_ai_state(w)
    archived=json.loads(Path(state['isolated_chat']['previous_receipt_path']).read_text())
    assert archived['previous_state']['submitted']is True
    assert archived['previous_state']['readonly_probe_count']==4
    assert archived['previous_submission_still_uncertain']is True
    assert new.action=='start'and state['submitted']is False
    assert state['thread_id']!=old['thread_id']
    assert bridge.read_bytes()==raw and video.read_bytes()==b'original rendered video'
    history=json.loads((w.state_dir/'thumbnail-conversations.json').read_text())['records']
    assert any(r['thread_id']=='old_video_thread' for r in history)


def test_completed_thumbnail_is_reused_without_a_new_conversation(tmp_path):
    w,p,claim,proof,data=setup(tmp_path)
    img=w.state_dir/'existing.png';img.write_bytes(b'previous verified asset')
    update_thumbnail_ai_state(w,p,claim,status='complete',submitted=True,thumbnail_path=img)
    with patch('docurandom.storage.app_data_dir',return_value=data):
        result=claim_thumbnail_ai_generation(w,p,dedicated_chat=True,content_thread_id='content-master')
    assert result.action=='reuse'and result.existing_path==img
    assert not read_thumbnail_ai_state(w).get('isolated_chat')


def test_audit_context_uses_master_checkpoint_even_with_recent_thumbnail_recovery(tmp_path):
    w,p,claim,proof,data=setup(tmp_path)
    p.with_name(p.stem+'_AI_CHECKPOINT.json').write_text(json.dumps({'thread_id':'actual-content'}))
    with patch('docurandom.storage.app_data_dir',return_value=data):
        claim_thumbnail_ai_generation(w,p,dedicated_chat=True,content_thread_id='actual-content')
    app=DocuRandomApp.__new__(DocuRandomApp);app.workspace=w
    app.video_chat_package=p;app.video_chat_thread_id='stale-thumbnail';app.video_chat_response_id='image-response'
    app.content_last_result=None
    assert app._resolve_video_chat_context(p)==('actual-content','')
    assert app.video_chat_thread_id=='actual-content'


def test_real_worker_starts_in_dedicated_chat_and_retains_url_with_video_identity(tmp_path):
    from tests.test_stalled_thumbnail_repair import recovered_app,run_worker
    app,p,state_path=recovered_app(tmp_path)
    state_path.unlink()  # Newly exported package has never requested a thumbnail.
    app.video_chat_thread_id='content-master';app.video_chat_response_id='content-response'
    app.video_chat_package=p
    app._resolve_video_chat_context.return_value=('content-master','content-response')
    data=tmp_path/'Data';bridge=data/'chat-bridge';bridge.mkdir(parents=True)
    url='https://chatgpt.com/c/6ad114bc-6e10-83ec-8988-3363f072eb80'
    out=io.BytesIO();Image.new('RGB',(1280,720),(10,70,90)).save(out,format='PNG')
    calls=[]
    class Chat:
        def __init__(self,*args,**kw):
            self.thread_id=kw['thread_id'];self.response_id=kw['response_id']
            assert self.thread_id!='content-master' and not self.response_id
        def generate_image(self,prompt,**kw):
            assert not kw.get('attachments')
            calls.append(self.thread_id)
            (bridge/'bridge-state.json').write_text(json.dumps({'savedTurns':{'thread:'+self.thread_id:{'chatUrl':url}}}))
            kw['on_status']('submitted','accepted')
            return SimpleNamespace(data=out.getvalue(),response_id='image',mime_type='image/png')
        def send(self,*args,**kw):return 'ĐÃ TẠO'
    with patch('docurandom.storage.app_data_dir',return_value=data):
        run_worker(app,p,Chat)
    state=read_thumbnail_ai_state(app.workspace)
    assert state['status']=='complete'and len(calls)==1
    assert state['conversation_url']==url
    assert app.video_chat_thread_id=='content-master'and app.video_chat_response_id=='content-response'
    registry=json.loads((data/'reports/thumbnail-chat-links.json').read_text())
    row=next(r for r in registry['current']if r['workspace_id']==app.workspace.workspace_id)
    assert row['url']==url and row['package_path']==str(p.resolve())
    assert row['package_sha256']==state['package_sha256'] and row['dedicated_chat']is True
    assert 'Video.txt'in (data/'reports/THUMBNAIL-CHAT-LINKS.md').read_text(encoding='utf-8')


def test_url_index_keeps_both_videos_and_updates_status_instead_of_duplicating(tmp_path):
    w,p,claim,proof,data=setup(tmp_path)
    with patch('docurandom.storage.app_data_dir',return_value=data):
        first=read_thumbnail_ai_state(w)
        first.update(status='complete',thumbnail_path='first.png')
        retain_thumbnail_link(w,first,data/'chat-bridge/bridge-state.json')
        retain_thumbnail_link(w,first,data/'chat-bridge/bridge-state.json')
        second=dict(first,package_sha256='second-video-hash',status='submitted')
        retain_thumbnail_link(w,second,data/'chat-bridge/bridge-state.json')
    registry=json.loads((data/'reports/thumbnail-chat-links.json').read_text())
    assert len(registry['current'])==1 and len(registry['history'])==2
    assert registry['future_auto_export_enabled']is True


def test_exported_package_starts_thumbnail_before_voice_and_flow(tmp_path):
    from unittest.mock import Mock
    from docurandom.content_ai import ContentArtifacts,ContentRunResult
    p=tmp_path/'video.txt';p.write_text('complete exported package')
    result=ContentRunResult(ContentArtifacts('Title','Description','Thumbnail design','A: voice','A: voice',('scene',)),
        p,tmp_path/'transcript.txt',tmp_path/'checkpoint.json','content-master',())
    app=DocuRandomApp.__new__(DocuRandomApp);app.workspace=None
    app.content_snapshot={'flow_enabled':True,'output_dir':tmp_path}
    app.voicevox_backup_dir_var=Mock();app.root=Mock();trace=[]
    for name in ['_cancel_content_precheck_watchdog','_set_content_progress','_set_voicevox_script',
                 '_refresh_voicevox_backup_summary','_refresh_smart_action','_save_config',
                 '_append_content_chat','_refresh_content_controls']:
        setattr(app,name,Mock())
    app._record_pipeline_stage=Mock(side_effect=lambda stage,*a:trace.append(stage))
    def thumbnail(value):
        assert value.package_path.is_file() and 'package'in trace
        trace.append('thumbnail')
    app._start_chatgpt_thumbnail_generation=Mock(side_effect=thumbnail)
    app._start_pipeline_voice_prefetch=Mock(side_effect=lambda:trace.append('voice'))
    app._start_flow_image_generation=Mock()
    app._apply_content_result(result)
    assert trace.index('package')<trace.index('thumbnail')<trace.index('voice')<trace.index('flow_images')
    assert app.pipeline_voice_deferred_for_thumbnail is False
    assert app.video_chat_thread_id=='content-master'


def test_concurrent_url_updates_do_not_lose_other_workspace_records(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from docurandom.workspace import Workspace
    data=tmp_path/'Data';bridge=data/'chat-bridge';bridge.mkdir(parents=True)
    url='https://chatgpt.com/c/6ad114bc-6e10-83ec-8988-3363f072eb80'
    (bridge/'bridge-state.json').write_text(json.dumps({'savedTurns':{
        'thread:t'+str(i):{'chatUrl':url}for i in range(8)}}))
    def update(i):
        w=Workspace(str(i),'Workspace '+str(i),str(tmp_path/str(i)),'','');w.ensure_layout()
        row={'thread_id':'t'+str(i),'package_path':str(tmp_path/str(i)/'video.txt'),
             'package_sha256':str(i),'prompt_sha256':str(i),'updated_at':'now'}
        retain_thumbnail_link(w,row,bridge/'bridge-state.json')
    with ThreadPoolExecutor(max_workers=4)as pool:list(pool.map(update,range(8)))
    registry=json.loads((data/'reports/thumbnail-chat-links.json').read_text())
    assert len(registry['current'])==len(registry['history'])==8


def test_disabled_schedule_does_not_migrate_legacy_thumbnail_automatically(tmp_path):
    from unittest.mock import Mock
    w,p,claim,proof,data=setup(tmp_path)
    app=DocuRandomApp.__new__(DocuRandomApp);app.workspace=w
    app.closed=app.closing_after_cancel=False
    app.youtube_thumbnail_auto_ai_var=Mock();app.youtube_thumbnail_auto_ai_var.get.return_value=True
    app._effective_youtube_thumbnail=Mock(return_value=None)
    app.schedule_armed_var=Mock();app.schedule_armed_var.get.return_value=False
    app._start_chatgpt_thumbnail_generation=Mock()
    before=(w.state_dir/'thumbnail-ai.json').read_bytes()
    with patch('docurandom.ui.find_workspace_content_package',return_value=p):
        app._recover_pending_thumbnail_ai()
    app._start_chatgpt_thumbnail_generation.assert_not_called()
    assert (w.state_dir/'thumbnail-ai.json').read_bytes()==before
