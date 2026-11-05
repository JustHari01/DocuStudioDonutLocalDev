"""Real-engine fault-injection canary; leaves the production voice untouched."""
import copy,json,sys,threading
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from docurandom.voicevox import VoicevoxClient,VoicevoxStyle,VoicevoxSynthesisSettings,synthesize_script
from docurandom.voicevox_project import (load_voicevox_project,project_boxes,project_query_to_engine,build_voicevox_project,
    VoicevoxStyleOption,build_passthrough_audit_plan,write_final_project,render_items_from_project)
from docurandom.voice_repair import ensure_voice_quality
from docurandom.storage import atomic_write_json
root=Path(__file__).resolve().parents[1]/'artifacts/daita14-e2e-20261021/voice-repair-canary';root.mkdir(parents=True,exist_ok=True)
live=Path(r'D:\Youtube\daita14\VOICEVOX');meta=json.loads((live/'DaiTa14.voicevox.json').read_text())
signal=json.loads((live/'DaiTa14.voicevox.signal.json').read_text())
index=max(signal['boxes'],key=lambda r:r['peak'])['box'];boxes=project_boxes(load_voicevox_project(live/'DaiTa14_VOICEVOX_FINAL_audited.vvproj'))
selected=[boxes[index-1],boxes[0],boxes[1]]
queries=[project_query_to_engine(b.query)for b in selected];queries[0]['volumeScale']=2.0
style=VoicevoxStyle(**meta['style']);settings=VoicevoxSynthesisSettings(**meta['settings'])
data=build_voicevox_project(app_version='0.25.2',lines=[b.text for b in selected],queries=queries,
    style=VoicevoxStyleOption(style.speaker_name,style.speaker_uuid,style.style_name,style.style_id))
plan=build_passthrough_audit_plan(data,allowed_style_ids=[style.style_id]);final=write_final_project(source_path=root/'Canary.vvproj',project_data=data,plan=plan)
client=VoicevoxClient(meta['engine_endpoint']);script='\n'.join(b.text for b in selected);calls=[];real=VoicevoxClient.synthesis
def counted(self,query,style_id,**kw):
    calls.append({'kana':query.get('kana'),'volume':query['volumeScale']});return real(self,query,style_id,**kw)
with patch.object(VoicevoxClient,'synthesis',counted):
    result=synthesize_script(client=client,script=script,style=style,settings=settings,output_path=root/'Canary.wav',
        audited_items=render_items_from_project(data),audited_project_path=final.project_path)
    from docurandom.voice_safety import audit_signal
    initial=audit_signal(result.metadata_path)
    assert initial['status']=='warning' and any(i['kind']=='clipping'for i in initial['issues']), 'Fault injection did not produce measured clipping'
    print('INITIAL SIGNAL',initial['status'],initial['issues'],flush=True)
    repaired,qa=ensure_voice_quality(result,script,cancel=threading.Event(),progress=lambda t:print(t,flush=True),cpu_threads=4)
    outcome=audit_signal(repaired.metadata_path)
    atomic_write_json(root/'proof.json',{'production_unchanged':True,'injected_fault':'one box volumeScale=2.0',
        'original_production_box':index,'initial_issues':initial['issues'],'after_status':outcome['status'],
        'qa_status':qa.status,'synthesis_calls':calls,'expected_unchanged_box_reuse':True})
    assert outcome['status']=='passed' and qa.status=='passed'
    assert sum(c['kana']==queries[1].get('kana')for c in calls)<=1
    assert sum(c['kana']==queries[2].get('kana')for c in calls)<=1
    print('REAL_REPAIR_PASS',len(calls),'synthesis calls',flush=True)
