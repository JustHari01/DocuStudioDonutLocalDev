from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import threading
import pytest
from docurandom.automatic_materials import missing_metadata, read_materials, repair_metadata
from docurandom.content_ai import ContentArtifacts, format_content_package
from docurandom.thumbnail_ai import read_thumbnail_ai_state, thumbnail_source_read_exhausted, claim_thumbnail_ai_generation, reserve_thumbnail_canvas_repair
from tests.test_thumbnail_canvas_repair import prepared


def setup(tmp_path, **changes):
    w=SimpleNamespace(state_dir=tmp_path/'.docurandom');w.state_dir.mkdir()
    p=tmp_path/'video.txt'
    data=dict(title='', description='既存の説明', thumbnail_prompt='Create factory documentary thumbnail',
        voicevox_script='日本の技術を確認します。',voicevox_export_script='A: 日本の技術を確認します。',image_prompts=('Factory documentary photo',),seo_tags=())
    data.update(changes);p.write_text(format_content_package(ContentArtifacts(**data)),encoding='utf8')
    return w,p


def test_missing_metadata_repairs_only_absent_fields_and_preserves_creative_assets(tmp_path):
    w,p=setup(tmp_path);before=read_materials(p)
    assert missing_metadata(p)==['title','seo_tags']
    with patch('docurandom.quality_chat.ask_json',return_value={'title':'技術の記録','description':'ignored replacement','seo_tags':['技術','工場']}):
        assert repair_metadata(w,p,cancel=threading.Event())
    after=read_materials(p)
    from docurandom.youtube_upload import parse_content_package
    effective=parse_content_package(p)
    assert after==before
    assert effective.description==before.description
    assert effective.title=='技術の記録'
    for field in ('voicevox_script','image_prompts','thumbnail_prompt'):
        assert getattr(after,field)==getattr(before,field)
    assert list(w.state_dir.glob('material-repair/*/original.txt'))


def test_uncertain_metadata_response_does_not_write_package(tmp_path):
    w,p=setup(tmp_path);before=p.read_bytes()
    with patch('docurandom.quality_chat.ask_json',side_effect=RuntimeError('pending')):
        with pytest.raises(RuntimeError):repair_metadata(w,p,cancel=threading.Event())
    assert p.read_bytes()==before


def test_metadata_concurrent_edit_is_not_overwritten(tmp_path):
    w,p=setup(tmp_path)
    def answer(*a,**k):
        p.write_text('new operator file');return {'title':'技術','seo_tags':['記録']}
    with patch('docurandom.quality_chat.ask_json',side_effect=answer):
        with pytest.raises(ValueError,match='changed during repair'):repair_metadata(w,p,cancel=threading.Event())
    assert p.read_text()=='new operator file'


def test_canvas_correction_has_own_once_only_budget_after_old_generation_exhaustion(tmp_path):
    w,p,proof,data=prepared(tmp_path)
    import json
    state=read_thumbnail_ai_state(w)
    state['recovery_chat'].update(policy_version=3,generation_attempts=1)
    state['retry_probe_failures']=32
    (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(state))
    assert not thumbnail_source_read_exhausted(state)
    with patch('docurandom.storage.app_data_dir',return_value=data):
        claim=claim_thumbnail_ai_generation(w,p)
        assert claim.action=='repair_image_source'
        assert not reserve_thumbnail_canvas_repair(w,p,claim,state['thread_id'],{})
        assert reserve_thumbnail_canvas_repair(w,p,claim,state['thread_id'],proof)
        assert not reserve_thumbnail_canvas_repair(w,p,claim,state['thread_id'],proof)
    after=read_thumbnail_ai_state(w)
    assert after['automatic_retry_count']==3 and after['retry_probe_failures']==32
    assert after['recovery_chat']['generation_attempts']==1


def test_legacy_english_headlines_preserve_complete_japanese(tmp_path):
    from docurandom.thumbnail_design import extract_headlines
    assert extract_headlines('Top-left, bold yellow with black outline: 「対重点検は時代遅れw」\nMiddle-left, bright cyan with black outline: 「作業員を疑った工場」\nBottom-left, vivid red with shadow: 「Z軸負荷に異変が！」')==['対重点検は時代遅れw','作業員を疑った工場','Z軸負荷に異変が！']


def test_auto_thumbnail_is_required_even_when_generation_checkbox_is_off(tmp_path):
    from docurandom.ui import DocuRandomApp
    from unittest.mock import Mock
    app=DocuRandomApp.__new__(DocuRandomApp)
    app._effective_youtube_thumbnail=Mock(return_value=None)
    app.youtube_thumbnail_auto_ai_var=Mock();app.youtube_thumbnail_auto_ai_var.get.return_value=False
    app.last_content_package=tmp_path/'video.txt';app.last_content_package.write_text('existing package')
    assert app._automatic_thumbnail_required_but_missing()


def test_voice_audit_third_request_corrects_exact_schema_and_digest(tmp_path):
    from docurandom.ui import DocuRandomApp
    from docurandom.voicevox_project import VoicevoxStyleOption, build_voicevox_project, voicevox_project_digest
    from tests.test_voicevox_audit_runtime import _query
    from unittest.mock import Mock
    import json
    style=VoicevoxStyleOption('Narrator','speaker-a','Normal',10)
    project=build_voicevox_project(app_version='0.25.2',lines=('技術の確認です。',),queries=(_query('テスト'),),style=style)
    digest=voicevox_project_digest(project)
    valid={'schemaVersion':1,'sourceDigest':digest,'presets':[{'id':'base','function':'明瞭','params':{'speedScale':1.0,'pitchScale':0.0,'intonationScale':1.0,'volumeScale':1.0,'pauseLengthScale':1.0,'prePhonemeLength':0.1,'postPhonemeLength':0.1}}],
        'mappings':[{'presetId':'base','styleId':10,'boxIndexes':[1]}],'overrides':[],'pronunciations':[]}
    app=DocuRandomApp.__new__(DocuRandomApp)
    app._send_voicevox_audit_to_chatgpt=Mock(side_effect=['{}','{}',json.dumps(valid)])
    app._audit_voicevox_project_data(project_data=project,source_path=tmp_path/'video.vvproj',styles=(style,),
        client=Mock(),cancel_event=threading.Event(),progress=Mock())
    assert app._send_voicevox_audit_to_chatgpt.call_count==3
    prompt=app._send_voicevox_audit_to_chatgpt.call_args.kwargs['prompt']
    assert digest in prompt and 'DOCURANDOM_AUDIT_SCHEMA_CORRECTION_V1' in prompt


def test_corrective_source_reads_also_stop_after_three_failed_probes(tmp_path):
    from docurandom.thumbnail_ai import record_thumbnail_ai_loading_failure
    import json
    w,p,proof,data=prepared(tmp_path)
    state=read_thumbnail_ai_state(w);state['retry_probe_failures']=32
    state['recovery_chat'].update(policy_version=3,generation_attempts=1)
    (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(state))
    claim=claim_thumbnail_ai_generation(w,p)
    for _ in range(3):
        assert record_thumbnail_ai_loading_failure(w,p,claim,'source unavailable')
    after=read_thumbnail_ai_state(w)
    assert after['corrective_probe_failures']['repair_image_source']==3
    assert thumbnail_source_read_exhausted(after)


def test_repaired_metadata_is_hash_bound_and_wakes_upload_wait(tmp_path):
    from docurandom.upload_asset_guard import fingerprint
    from docurandom.youtube_upload import parse_content_package, YouTubeUploadError
    w,p=setup(tmp_path)
    before=fingerprint({'package_path':str(p)})
    with patch('docurandom.quality_chat.ask_json',return_value={'title':'技術の記録','seo_tags':['工場']}):
        repair_metadata(w,p,cancel=threading.Event())
    assert fingerprint({'package_path':str(p)})!=before
    assert missing_metadata(p)==[]
    p.write_text(p.read_text()+'changed original')
    with pytest.raises(YouTubeUploadError):parse_content_package(p)


def test_known_bare_preset_id_is_recovered_without_weakening_digest_or_box_checks(tmp_path):
    from docurandom.voicevox_project import VoicevoxStyleOption, build_voicevox_project, voicevox_project_digest, parse_audit_plan, VoicevoxProjectError
    from tests.test_voicevox_audit_runtime import _query
    import json
    style=VoicevoxStyleOption('Narrator','speaker-a','Normal',10)
    project=build_voicevox_project(app_version='0.25.2',lines=('技術の確認です。',),queries=(_query('テスト'),),style=style)
    valid={'schemaVersion':1,'sourceDigest':voicevox_project_digest(project),
        'presets':[{'id':'base','function':'明瞭','params':{'speedScale':1.0,'pitchScale':0.0,'intonationScale':1.0,'volumeScale':1.0,'pauseLengthScale':1.0,'prePhonemeLength':0.1,'postPhonemeLength':0.1}}],
        'mappings':[{'presetId':'base','styleId':10,'boxIndexes':[1]}],'overrides':[],'pronunciations':[]}
    broken=json.dumps(valid).replace('"id": "base",','"base",')
    plan=parse_audit_plan(broken,project_data=project,allowed_style_ids=(10,))
    assert plan is not None
    with pytest.raises(VoicevoxProjectError):
        parse_audit_plan(broken.replace(voicevox_project_digest(project),'wrong'),project_data=project,allowed_style_ids=(10,))
    with pytest.raises(VoicevoxProjectError):
        parse_audit_plan(broken.replace('"boxIndexes": [1]','"boxIndexes": [2]'),project_data=project,allowed_style_ids=(10,))


def test_scheduler_readiness_failure_wakes_material_repair_before_upload_can_start(tmp_path):
    from docurandom.ui import DocuRandomApp
    from docurandom.youtube_upload import YouTubeUploadError
    from unittest.mock import Mock
    app=DocuRandomApp.__new__(DocuRandomApp)
    app.workspace=SimpleNamespace(state_dir=tmp_path);app.last_content_package=None
    app._effective_youtube_thumbnail=Mock(return_value=None)
    app._repair_missing_upload_materials=Mock()
    with patch('docurandom.ui.resolve_workspace_upload_sources',side_effect=YouTubeUploadError('missing title')):
        assert not app._scheduled_video_is_ready()
    app._repair_missing_upload_materials.assert_called_once()


def test_ocr_does_not_merge_small_machine_labels_into_headline():
    from docurandom.thumbnail_ocr import line_groups
    def row(text,x,y,w,h):return {'txt':text,'score':.99,'box':[[x,y],[x+w,y],[x+w,y+h],[x,y+h]]}
    groups=line_groups([row('作業員を疑った工場',18,424,649,94),row('Z軸ドライブ負荷',798,443,130,26)])
    assert {g['items'][0]['txt']for g in groups}=={'作業員を疑った工場','Z軸ドライブ負荷'}


@pytest.mark.parametrize('glyph,confidence,accepted',[('Z',.96,True),('X',.99,False),('Z',.60,False)])
def test_confusable_japanese_glyph_needs_independent_confident_latin_read(glyph,confidence,accepted):
    from docurandom.thumbnail_ocr import verify_leading_latin
    import numpy as np
    from unittest.mock import Mock
    group={'items':[{'txt':'乙軸負荷に異変が!','score':.95,'box':[[7,532],[908,532],[908,696],[7,696]]}]}
    row={'txt':glyph,'score':confidence,'box':[[8,10],[100,10],[100,150],[8,150]]}
    engine=Mock();engine.return_value.to_json.return_value=[row]
    with patch('docurandom.thumbnail_ocr._LATIN_ENGINE',engine):
        assert bool(verify_leading_latin(group,'Z軸負荷に異変が！',np.zeros((720,1280,3),dtype=np.uint8)))is accepted
        assert verify_leading_latin(group,'Z軸荷重に異変が！',np.zeros((720,1280,3),dtype=np.uint8))is None


@pytest.mark.parametrize('user_cancelled',[False,True])
def test_review_watchdog_is_recoverable_but_user_cancellation_remains_cancellation(tmp_path,user_cancelled):
    from docurandom.quality_chat import ask_json
    from docurandom.content_ai import ContentAICancelled
    from unittest.mock import Mock
    cancel=threading.Event()
    if user_cancelled:cancel.set()
    status=SimpleNamespace(operational=True,endpoint='http://127.0.0.1:1',access_token='test-token',available_models=())
    chat=Mock();chat.send.side_effect=ContentAICancelled('local watchdog')
    with patch('docurandom.quality_chat.check_chatgpt_web_bridge',return_value=status),patch('docurandom.quality_chat.ChatGPTWebChat',return_value=chat),patch('docurandom.quality_chat.resolve_chatgpt_web_model',return_value='selected-model'),patch('docurandom.quality_chat.app_data_dir',return_value=tmp_path):
        with pytest.raises(ContentAICancelled if user_cancelled else RuntimeError):
            ask_json(tmp_path/'reviews','exact audit request',cancel=cancel)
    import json
    receipt=json.loads(next((tmp_path/'reviews').glob('*.json')).read_text())
    assert receipt['status']=='uncertain'
    assert chat.send.call_count==1


def test_repaired_cached_image_cannot_bypass_quality_review_during_restart(tmp_path):
    from tests.test_stalled_thumbnail_repair import recovered_app
    from docurandom.thumbnail_ai import stage_thumbnail_candidate
    from docurandom.ui import DocuRandomApp
    from unittest.mock import Mock
    from PIL import Image
    import io,json
    app,package,state_path=recovered_app(tmp_path)
    claim=claim_thumbnail_ai_generation(app.workspace,package)
    b=io.BytesIO();Image.new('RGB',(1280,720),'blue').save(b,format='PNG')
    stage_thumbnail_candidate(app.workspace,claim,'exact-old-thread','image-response',b.getvalue())
    state=read_thumbnail_ai_state(app.workspace);state['canvas_repair']={'count':1};state_path.write_text(json.dumps(state))
    app.content_cli_status=None;app.codex_checking=False
    app._check_codex_async=Mock();app._apply_thumbnail_selection=Mock()
    app.youtube_thumbnail_status_var=Mock()
    with patch('docurandom.thumbnail_contract.contract',return_value={}):
        app._start_chatgpt_thumbnail_generation(package)
    assert read_thumbnail_ai_state(app.workspace)['status']!='complete'
    app._apply_thumbnail_selection.assert_not_called()
    app._check_codex_async.assert_called_once()
