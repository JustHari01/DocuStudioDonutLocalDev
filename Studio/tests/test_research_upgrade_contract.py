import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
import pytest

from docurandom.recovery_policy import (
    RecoveryJournal, Outcome, classify, hold_youtube_auth, youtube_auth_blocked,
    request_youtube_auth_probe,
)
from docurandom.thumbnail_design import extract_headlines, design_spec
from docurandom.thumbnail_contract import prepare_prompt, review_image
from docurandom.audit_receipt import send_voice_audit
from docurandom.research_upgrade import canonical_master, master_manifest, record_story_context
from docurandom.pipeline_checkpoint import update_pipeline_stage, read_pipeline_checkpoint


@pytest.mark.parametrize('prompt', [
    'first line 「日本の技術」 in yellow\nsecond line 「工場で検証」 in cyan\nthird line 「何が違う!?」 in red',
    'TOP YELLOW LINE:\n「日本の技術」\nMIDDLE CYAN LINE:\n「工場で検証」\nBOTTOM RED LINE:\n「何が違う!?」',
    'YELLOW TOP: 日本の技術\nCYAN MIDDLE: 工場で検証\nRED BOTTOM: 何が違う!?',
])
def test_all_operator_formats_have_one_design(prompt):
    assert extract_headlines(prompt) == ['日本の技術', '工場で検証', '何が違う!?']
    assert [r['color'] for r in design_spec(prompt)['headlines']] == ['yellow', 'cyan', 'red']


def test_incomplete_design_never_guesses():
    with pytest.raises(ValueError, match='missing exact'):
        extract_headlines('TOP YELLOW LINE: 「日本の技術」')


def test_recovery_budget_survives_restart(tmp_path):
    clock = datetime(2026, 10, 23, tzinfo=timezone.utc)
    j = RecoveryJournal(tmp_path)
    assert j.record('same-job', Outcome.UNCERTAIN, now=clock)
    assert j.reserve_probe('same-job', now=clock) == 'wait'
    for seconds in [120, 300, 900]:
        clock += timedelta(seconds=seconds)
        j = RecoveryJournal(tmp_path)
        j.record('same-job', Outcome.UNCERTAIN, now=clock)
        assert j.reserve_probe('same-job', now=clock) == 'reserved'
    assert RecoveryJournal(tmp_path).reserve_probe('same-job', now=clock+timedelta(days=1)) == 'exhausted'


def test_auth_barrier_is_profile_scoped_and_explicit(tmp_path):
    assert classify('https://accounts.google.com/v3/signin') == Outcome.AUTH_REQUIRED
    assert hold_youtube_auth(tmp_path, 'p1', 'login')
    assert not hold_youtube_auth(tmp_path, 'p1', 'login')
    assert youtube_auth_blocked(tmp_path, 'p1')
    assert not youtube_auth_blocked(tmp_path, 'p2')
    request_youtube_auth_probe(tmp_path)
    assert not youtube_auth_blocked(tmp_path, 'p1')
    assert hold_youtube_auth(tmp_path, 'p1', 'still logged out')


def test_retry_after_is_not_extended_by_polling(tmp_path):
    clock = datetime(2026, 10, 23, tzinfo=timezone.utc)
    j = RecoveryJournal(tmp_path)
    j.record('limited', Outcome.RATE_LIMITED, 'retry-after-seconds=33', now=clock)
    j.record('limited', Outcome.RATE_LIMITED, 'retry-after-seconds=33', now=clock+timedelta(seconds=20))
    assert j.reserve_probe('limited', now=clock+timedelta(seconds=33)) == 'reserved'


def test_voice_response_receipt_prevents_a_second_send(tmp_path):
    chat = Mock(thread_id='exact-thread', response_id='response-1')
    chat.send.return_value = '{"schemaVersion":1}'
    attachment = SimpleNamespace(data='日本語'.encode())
    result = send_voice_audit(chat, tmp_path, 'audit', attachment, cancel=None, on_status=Mock())
    assert send_voice_audit(chat, tmp_path, 'audit', attachment, cancel=None, on_status=Mock()) == result
    chat.send.assert_called_once()


def test_uncertain_voice_audit_only_recovers(tmp_path):
    chat = Mock(thread_id='exact-thread', response_id='response-1')
    chat.send.side_effect = TimeoutError('unknown delivery')
    attachment = SimpleNamespace(data='日本語'.encode())
    with pytest.raises(TimeoutError):
        send_voice_audit(chat, tmp_path, 'audit', attachment, cancel=None, on_status=Mock())
    chat.recover_pending.return_value = None
    with pytest.raises(RuntimeError, match='do not resubmit'):
        send_voice_audit(chat, tmp_path, 'audit', attachment, cancel=None, on_status=Mock())
    chat.send.assert_called_once()


def pilot(tmp_path):
    state = tmp_path / '.docurandom'
    state.mkdir(exist_ok=True)
    (state / 'learning-policy.json').write_text(json.dumps({
        'schema_version':1, 'enabled':True,'channel_id':'UCnWW9-QkcA773UV8v0xPqvw',
        'research_upgrade':{'enabled':True,'domain':'G-13'},
        'thumbnail_contract':{'line_colors':['yellow','cyan','red']},
    }), encoding='utf-8')
    return SimpleNamespace(state_dir=state)


def test_canonical_master_is_versioned_and_retains_protocol(tmp_path):
    master = canonical_master(pilot(tmp_path))
    assert master_manifest(master)['version'] == '11.1.0-r1'
    for marker in ['[[part-', '[[mo-ta]]', '[[prompt-thumbnail]]', '[[prompt-hinh-anh]]', 'TIẾP']:
        assert marker in master
    assert 'EVERY_EIGHT_CUE_WINDOW_HAS_SHORT_MEDIUM_LONG' not in master
    assert 'LAYOUT_VARIATION: FORBIDDEN' not in master
    assert canonical_master(None) is None


def test_pipeline_manifest_does_not_reset_existing_stages(tmp_path):
    p = tmp_path/'pipeline.json';identity = {'master_prompt_sha256':'same'}
    update_pipeline_stage(p, identity=identity, stage='chatgpt', status='complete')
    update_pipeline_stage(p, identity=identity, stage='voicevox', status='error', master_manifest={'version':'legacy'})
    assert read_pipeline_checkpoint(p)['stages']['chatgpt']['status'] == 'complete'


def test_empty_ocr_never_installs_an_unverified_image(tmp_path):
    w = pilot(tmp_path)
    (w.state_dir/'thumbnail-ai.json').write_text('{"thread_id":"thumbnail-chat"}', encoding='utf-8')
    prompt = 'first line 「日本の技術」 in yellow\nsecond line 「工場で検証」 in cyan\nthird line 「何が違う!?」 in red'
    buf=io.BytesIO();Image.new('RGB',(1280,720)).save(buf,format='PNG')
    review={'passed':True,'observed_headlines':extract_headlines(prompt),'observed_colors':['yellow','cyan','red'],
            'flags_correct':True,'subject_correct':True,'text_uncropped':True}
    with patch('docurandom.thumbnail_ocr.inspect',return_value={'passed':False,'status':'unavailable','headline_checks':[]}), patch('docurandom.quality_chat.ask_json',return_value=review) as ask:
        with pytest.raises(ValueError,match='VERIFICATION_UNAVAILABLE'):
            review_image(w,prompt,buf.getvalue())
        assert ask.call_args.kwargs['attachments'][0].data


def test_missing_research_brief_remains_explicitly_unavailable(tmp_path):
    w=pilot(tmp_path);package=tmp_path/'video.txt';package.write_text('body')
    record_story_context(w, SimpleNamespace(package_path=package, turns=(), thread_id='exact'))
    row=json.loads(next((w.state_dir/'research').glob('*-context.json')).read_text(encoding='utf-8'))
    assert row['status']=='unavailable' and 'brief' not in row


def test_chapters_follow_actual_subtitle_times(tmp_path):
    from docurandom.research_upgrade import synchronize_chapters
    w=pilot(tmp_path)
    (tmp_path/'Prompts').mkdir();(tmp_path/'Output').mkdir()
    (tmp_path/'Prompts/MASTER_PROMPT_CURRENT.txt').write_text('DOCURANDOM_RESEARCH_MASTER_VERSION=11.0.0-r1',encoding='utf-8')
    package=tmp_path/'Output/video.txt';package.write_text('00:00 導入\n04:00 比較\n09:00 結論\n',encoding='utf-8')
    subtitle=tmp_path/'voice.srt';subtitle.write_text('1\n00:00:00,000 --> 00:00:04,000\n最初の話です。\n\n2\n00:00:12,000 --> 00:00:18,000\n比較を始めます。\n\n3\n00:01:30,000 --> 00:01:35,000\n結論です。\n',encoding='utf-8')
    with patch('docurandom.quality_chat.ask_json',return_value={'uncertain':False,'anchors':[
        {'title':t,'cue_index':i}for i,t in enumerate(['導入','比較','結論'],1)]}):
        synchronize_chapters(w,package,subtitle)
    assert package.read_text(encoding='utf-8-sig')=='00:00 導入\n00:12 比較\n01:30 結論\n'


def test_unknown_edit_visibility_does_not_count_as_public(tmp_path):
    from docurandom.youtube_upload import YouTubeStudioUploader
    uploader=YouTubeStudioUploader(registry_path=tmp_path/'registry.json')
    with patch.object(uploader,'_open_edit_visibility',return_value=False):
        assert uploader._verify_edit_visibility(Mock(),'PUBLIC',Mock()) is False


def test_legacy_voice_backup_migrates_without_losing_original(tmp_path):
    from tests.test_voicevox_project import _project
    from docurandom.voice_repair import preserve_original_project
    from docurandom.voicevox_project import load_voicevox_project, voicevox_project_digest
    project=_project();legacy=tmp_path/'original-project.json'
    legacy.write_text(json.dumps(project,ensure_ascii=False),encoding='utf-8-sig')
    before=legacy.read_bytes()
    result=preserve_original_project(tmp_path,project)
    assert result.suffix=='.vvproj'
    assert voicevox_project_digest(load_voicevox_project(result))==voicevox_project_digest(project)
    assert legacy.read_bytes()==before
    assert preserve_original_project(tmp_path,project)==result


def test_large_analytics_retains_all_metrics_and_history(tmp_path):
    from docurandom.channel_runtime import analysis_request
    memory=SimpleNamespace(root=tmp_path,context=lambda: 'HISTORY ' * 2400)
    evidence=[{'evidence_id':str(i),'metrics':{'views':None},'details':'日本語'*400}for i in range(60)]
    request,attachments=analysis_request(memory,evidence,{},'daily-test')
    assert len(request.encode('utf-8'))<48*1024 and len(attachments)==1
    assert memory.context() in request
    payload=json.loads(attachments[0].data)
    assert payload['evidence']==evidence
    assert payload['evidence'][0]['metrics']['views'] is None
    assert (tmp_path/attachments[0].name).read_bytes()==attachments[0].data
    assert attachments[0].name.endswith('.txt') and attachments[0].mime_type=='text/plain'


def test_ocr_only_normalizes_ellipsis_not_lexical_errors():
    from docurandom.thumbnail_ocr import lexical_normal
    assert lexical_normal('穴径合格…肉厚は!?')==lexical_normal('穴径合格肉厚は!?')
    assert lexical_normal('穴径合格…肉厚は!?')!=lexical_normal('穴径合格肉厚は?')
    assert lexical_normal('穴径合格…肉厚は!?')!=lexical_normal('穴経合格肉厚は!?')


def test_semantic_gate_still_requires_complete_punctuation(tmp_path):
    w=pilot(tmp_path);(w.state_dir/'thumbnail-ai.json').write_text('{"thread_id":"exact"}')
    prompt='first line 「日本の技術」 in yellow\nsecond line 「工場で検証」 in cyan\nthird line 「穴径合格…肉厚は!?」 in red'
    image=io.BytesIO();Image.new('RGB',(1280,720)).save(image,format='PNG')
    review={'passed':True,'observed_headlines':['日本の技術','工場で検証','穴径合格肉厚は!?'],
        'observed_colors':['yellow','cyan','red'],'flags_correct':True,'subject_correct':True,'text_uncropped':True}
    with patch('docurandom.thumbnail_ocr.inspect',return_value={'passed':True}),patch('docurandom.quality_chat.ask_json',return_value=review):
        with pytest.raises(ValueError,match='VISUAL_CONTRACT_FAILED'):
            review_image(w,prompt,image.getvalue())


def test_review_wait_is_absolute_and_exhaustion_persists():
    from docurandom.quality_chat import review_retry_delay
    now=datetime(2026,10,23,tzinfo=timezone.utc)
    message='CHATGPT_RECOVERY_PENDING: quality review wait; next-probe-at=2026-10-23T00:02:00+00:00; preserve exact receipt'
    assert review_retry_delay(message,now=now)==120
    assert review_retry_delay(message,now=now+timedelta(seconds=70))==50
    assert review_retry_delay(message,now=now+timedelta(seconds=200))==0
    assert review_retry_delay(message.replace('review wait','review exhausted'),now=now)==-1


def test_logged_out_monitor_never_opens_a_profile(tmp_path):
    from docurandom.ui import DocuRandomApp
    app=DocuRandomApp.__new__(DocuRandomApp)
    app.workspace=pilot(tmp_path);app.closed=app.closing_after_cancel=app.smm_monitor_busy=False
    app.smm_auto_like_var=Mock();app.smm_auto_like_var.get.return_value=True
    app.youtube_selected_profile_id='profile';app.smm_monitor_status_var=Mock()
    hold_youtube_auth(app.workspace.state_dir,'profile','login required')
    app._start_smm_monitor_scan(allow_orders=True)
    assert 'Chờ đăng nhập' in app.smm_monitor_status_var.set.call_args.args[0]


def test_cached_review_wait_does_not_start_another_worker(tmp_path):
    from tests.test_stalled_thumbnail_repair import recovered_app
    from docurandom.ui import DocuRandomApp
    app,package,path=recovered_app(tmp_path)
    row=json.loads(path.read_text(encoding='utf-8-sig'))
    row['detail']='CHATGPT_RECOVERY_PENDING: quality review wait; next-probe-at=2099-01-01T00:00:00+00:00; preserve exact receipt'
    path.write_text(json.dumps(row))
    app._schedule_thumbnail_ai_retry=Mock()
    with patch('docurandom.ui.threading.Thread') as worker:
        app._start_chatgpt_thumbnail_generation(package)
        worker.assert_not_called()
    app._schedule_thumbnail_ai_retry.assert_called_once()


@pytest.mark.parametrize('failures',[3,32,39,-1,'32'])
def test_terminal_legacy_receipt_cannot_restart_an_exhausted_source_reader(tmp_path,failures):
    from tests.test_stalled_thumbnail_repair import recovered_app
    from docurandom.ui import DocuRandomApp
    app,package,path=recovered_app(tmp_path)
    row=json.loads(path.read_text(encoding='utf-8-sig'))
    row.update(status='terminal_no_image',submitted=False,staged_image=None,
        retry_probe_failures=failures,readonly_probe_count=39,
        detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: image tool failed')
    path.write_text(json.dumps(row))
    app._cancel_thumbnail_ai_retry=Mock()
    app.youtube_thumbnail_status_var=Mock()
    with patch('docurandom.ui.threading.Thread') as worker:
        app._start_chatgpt_thumbnail_generation(package)
        worker.assert_not_called()
    app._cancel_thumbnail_ai_retry.assert_called_once()
    app._cancel_thumbnail_ai_retry.reset_mock()
    assert DocuRandomApp._schedule_thumbnail_ai_retry(app,package,'retry') is False
    app._cancel_thumbnail_ai_retry.assert_called_once()
    assert json.loads(path.read_text())==row


def test_saved_candidate_is_not_lost_to_a_terminal_source_budget():
    from docurandom.thumbnail_ai import thumbnail_source_read_exhausted
    state={'status':'terminal_no_image','retry_probe_failures':32,'staged_image':{'sha256':'saved'}}
    assert thumbnail_source_read_exhausted(state) is False
    state.update(staged_image=None,retry_probe_failures=2)
    assert thumbnail_source_read_exhausted(state) is False


def test_new_recovery_request_cannot_spend_another_send_via_repair_aliases():
    from docurandom import thumbnail_ai as module
    state={'status':'terminal_no_image','submitted':False,'thread_id':'fresh',
        'automatic_retry_count':1,'retry_probe_failures':0,'readonly_probe_count':4,
        'generation_wire_version':3,'canvas_repair':{'count':1},
        'detail':'CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: thiếu ảnh nguồn .txt PNG',
        'updated_at':'2020-01-01T00:00:00+00:00',
        'recovery_chat':{'policy_version':3,'generation_attempts':1,'new_thread_id':'fresh','retry_count':0,'count':1}}
    for name in ['thumbnail_ai_terminal_retry_remaining_seconds','thumbnail_recovery_chat_remaining_seconds',
        'thumbnail_recovery_retry_remaining_seconds','thumbnail_prompt_repair_remaining_seconds',
        'thumbnail_canvas_repair_remaining_seconds']:
        assert getattr(module,name)(state) is None,name
    # A diagnosed file-transport correction owns a separate persistent once-only budget.
    assert module.thumbnail_canvas_transport_remaining_seconds(state)==0
    state['canvas_transport_repair']={'count':1}
    assert module.thumbnail_canvas_transport_remaining_seconds(state) is None
