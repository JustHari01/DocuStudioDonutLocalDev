import copy
from pathlib import Path
import pytest
from docurandom.voicevox_project import (
    VoicevoxStyleOption, VoicevoxProjectError, build_voicevox_project,
    build_passthrough_audit_plan, write_final_project, project_boxes,
    load_voicevox_project, build_audit_attachment,
)
from docurandom.voicevox_batches import audit_project_batches

def make_project(count=501):
    query={"accent_phrases":[{"moras":[{"text":"テ","consonant":"t",
        "consonant_length":0.05,"vowel":"e","vowel_length":0.1,"pitch":5.0}],
        "accent":1,"is_interrogative":False}],"speedScale":1.0,"pitchScale":0.0,
        "intonationScale":1.0,"volumeScale":1.0,"pauseLengthScale":1.0,
        "prePhonemeLength":0.1,"postPhonemeLength":0.1,"outputSamplingRate":24000,
        "outputStereo":False,"kana":"テ'"}
    return build_voicevox_project(app_version="0.25.2",
        lines=[f"技術の確認、{i}。" for i in range(count)],queries=[query]*count,
        style=VoicevoxStyleOption("Narrator","locked-speaker","Normal",10))

def audited_part(data, path):
    updated=copy.deepcopy(data)
    for item in updated["talk"]["audioItems"].values():item["query"]["speedScale"]=1.11
    plan=build_passthrough_audit_plan(updated,allowed_style_ids=(10,))
    return updated,write_final_project(source_path=path,project_data=updated,plan=plan)

def run(data,path,callback):
    return audit_project_batches(data,source_path=path,allowed_style_ids=(10,),
        audit_single=callback,progress=lambda _:None,check_cancel=lambda:None)

def test_large_project_audits_every_box_in_bounded_parts_and_preserves_identity(tmp_path):
    data=make_project(); before=copy.deepcopy(data); counts=[]
    def callback(part,path):
        counts.append(len(project_boxes(part)))
        assert part["docurandomAuditContext"]["totalBoxCount"]==501
        assert len(part["docurandomAuditContext"]["script"])==501
        assert b'scriptContext' in build_audit_attachment(part,styles=[])
        return audited_part(part,path)
    merged,result=run(data,tmp_path/'voice.vvproj',callback)
    assert counts==[500,1]
    assert data==before
    assert result.box_count==501 and result.preset_count==2
    assert result.used_safe_fallback is False
    after=project_boxes(load_voicevox_project(result.project_path))
    assert [b.audio_key for b in after]==[b.audio_key for b in project_boxes(before)]
    assert all(b.query['speedScale']==1.11 and b.speaker_id=='locked-speaker' for b in after)
    assert 'docurandomAuditContext' not in merged

def test_partial_failure_resumes_only_unfinished_part(tmp_path):
    data=make_project(); calls=[]
    def interrupted(part,path):
        calls.append(len(project_boxes(part)))
        if len(calls)==2:raise VoicevoxProjectError('Uncertain turn retained')
        return audited_part(part,path)
    with pytest.raises(VoicevoxProjectError,match='Uncertain'):run(data,tmp_path/'voice.vvproj',interrupted)
    resumed=[]
    def callback(part,path):resumed.append(len(project_boxes(part)));return audited_part(part,path)
    _,result=run(data,tmp_path/'voice.vvproj',callback)
    assert resumed==[1] and result.box_count==501
    run(data,tmp_path/'voice.vvproj',lambda *_:pytest.fail('A verified part must not be audited again'))

def test_audit_cannot_change_text_or_speaker(tmp_path):
    data=make_project()
    def callback(part,path):
        part=copy.deepcopy(part);first=next(iter(part['talk']['audioItems'].values()))
        first['text']='Changed script'
        return audited_part(part,path)
    with pytest.raises(VoicevoxProjectError,match='nội dung'):run(data,tmp_path/'voice.vvproj',callback)

def test_completed_part_without_receipt_is_not_blindly_submitted_again(tmp_path):
    data=make_project()
    run(data,tmp_path/'voice.vvproj',audited_part)
    next(tmp_path.glob('voice_audit_parts/*/part-0001.json')).unlink()
    with pytest.raises(VoicevoxProjectError,match='thiếu biên nhận'):
        run(data,tmp_path/'voice.vvproj',lambda *_:pytest.fail('Uncertain receipt cannot authorize retry'))
