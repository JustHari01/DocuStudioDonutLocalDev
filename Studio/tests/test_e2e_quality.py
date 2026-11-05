import copy,hashlib,io,json,wave
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock,patch
import numpy as np
import pytest
from docurandom.thumbnail_contract import prepare_prompt,japanese
from docurandom.voice_safety import pcm_metrics,bounded_repair
from docurandom.voice_repair import apply_local_patches
from docurandom.video_analytics import ab_admission,video_snapshot
from docurandom.native_ab import run_native_ab
from tests.test_voicevox_project import _project
from docurandom.voicevox_project import project_boxes


def test_colour_repair_preserves_japanese_and_requires_exact_structure(tmp_path):
    state=tmp_path/'.docurandom';state.mkdir();out=tmp_path/'Output';out.mkdir();p=out/'Video.txt';p.write_text('story')
    (state/'learning-policy.json').write_text(json.dumps({'schema_version':1,'enabled':True,'channel_id':'UC'+'a'*22,
       'thumbnail_contract':{'line_colors':['yellow','cyan','red']}}))
    prompt='first line 「日本の検査は過剰w」 in bold white; second line 「寸法合格なのに異音!?」 in bold yellow; third line 「歯当たりに残る盲点」 in bold white with only 「盲点」 highlighted red.'
    fixed=prepare_prompt(p,prompt)
    assert japanese(prompt)==japanese(fixed)
    assert 'bold cyan' in fixed and 'bold yellow' in fixed and 'bold red' in fixed
    with pytest.raises(ValueError):prepare_prompt(p,'Some vague image prompt')


def test_local_mora_patch_never_changes_identity_or_other_moras():
    original=_project();b=project_boxes(original)[0]
    patch_data={'audio_key':b.audio_key,'text_sha256':hashlib.sha256(b.text.encode()).hexdigest(),
                'reason':'Contextual prosody audit','phrases':[{'phrase_index':0,'moras':[{'mora_index':0,'pitch':5.2}]}]}
    result=apply_local_patches(original,[patch_data],allowed_keys={b.audio_key})
    assert project_boxes(result)[0].query['accentPhrases'][0]['moras'][0]['pitch']==5.2
    assert project_boxes(result)[0].query['accentPhrases'][0]['moras'][1]==b.query['accentPhrases'][0]['moras'][1]
    assert project_boxes(result)[1]==project_boxes(original)[1]
    bad=copy.deepcopy(patch_data);bad['phrases'][0]['moras'][0]['text']='違'
    with pytest.raises(ValueError):apply_local_patches(original,[bad],allowed_keys={b.audio_key})
    with pytest.raises(ValueError):apply_local_patches(original,[patch_data],allowed_keys=set())


def test_signal_and_repair_no_progress_stop(tmp_path):
    path=tmp_path/'bad.wav'
    with wave.open(str(path),'wb')as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(8000);f.writeframes(np.full(800,32767,dtype='<i2').tobytes())
    assert pcm_metrics(path)['clipped_fraction']==1
    repair=Mock(return_value='unchanged')
    with pytest.raises(ValueError,match='NO_PROGRESS'):
        bounded_repair(tmp_path,'initial',assess=lambda _:{'status':'warning','issues':[{'box':1,'kind':'clipping'}]},repair=repair,identity='x')
    assert repair.call_count==1


def test_native_ab_no_writes_without_verified_window_or_capability():
    now=datetime.now(timezone.utc);video={'video_id':'abcdefghijk','visibility':'Public','published_at':(now-timedelta(days=5)).isoformat()}
    reach={'collected_at':now.isoformat(),'report_range':{'label':'28 ngày qua'},'metrics':{'impressions':5000}}
    admission=ab_admission(video,reach,now=now,capability=True)
    assert not admission['eligible'] and 'seven_day_window_not_verified'in admission['reasons']
    with patch('docurandom.native_ab.StudioInspector')as inspector:
        assert run_native_ab(None,admission,{},endpoint=None,policy={})['no_mutation']
        inspector.assert_not_called()
    reach['report_range']['label']='7 ngày qua'
    assert ab_admission(video,reach,now=now,capability=True)['eligible']


def test_video_missing_data_is_not_zero():
    page={'channel_id':'UC'+'a'*22,'video_id':'abcdefghijk','url':'https://studio.youtube.com/video/abcdefghijk/analytics/tab-reach_viewers/period-default',
          'report':'reach','collected_at':datetime.now(timezone.utc).isoformat(),
          'observations':[{'role':'StaticText','text':t}for t in ['7 ngày qua','Số lượt hiển thị hình thu nhỏ','Số lượt hiển thị hình thu nhỏ','1.234','Tỷ lệ nhấp vào hình thu nhỏ','—']]}
    result=video_snapshot(page,{})
    assert result['metrics']['impressions']==1234 and result['metrics']['ctr_percent']is None


def test_workflow_acceptance_requires_ordered_real_receipts(tmp_path):
    from types import SimpleNamespace
    from docurandom.workflow_acceptance import observe
    w=SimpleNamespace(workspace_id='pilot',state_dir=tmp_path)
    package=tmp_path/'B.txt';package.write_text('B')
    config={'workspace_id':'pilot','a_identity':'A','b_package_sha256':hashlib.sha256(package.read_bytes()).hexdigest(),
            'b_content_thread_id':'thread-B','evidence':{}}
    def save(name,value):(tmp_path/name).write_text(json.dumps(value),encoding='utf-8')
    save('workflow-acceptance.json',config)
    save('uploaded_sessions.json',{'sessions':{'A':{'visibility_status':'UNLISTED_PENDING_PUBLIC'}}})
    assert observe(w)['evidence']=={}
    save('uploaded_sessions.json',{'sessions':{'A':{'visibility_status':'PUBLIC','youtube_url':'a','published_at':'2026-10-22T13:31:00+07:00'}}})
    item={'identity':'B','package_path':str(package),'youtube_url':'b','queued_at':'2026-10-22T13:30:00+07:00'}
    save('deferred_public_queue.json',{'items':[item]})
    assert set(observe(w)['evidence'])=={'a_public'}  # pre-Public upload never proves this sequence
    item['queued_at']='2026-10-22T13:40:00+07:00';save('deferred_public_queue.json',{'items':[item]})
    save('pipeline_checkpoint.json',{'created_at':'2026-10-22T13:41:00+07:00','stages':{'chatgpt':{'status':'running','artifacts':{'thread_id':'thread-C'}}}})
    assert observe(w)['status']=='complete'
    assert observe(w)['evidence']['c_started']['thread_id']=='thread-C'  # survives restart


def test_analysis_compaction_preserves_metrics_and_missing_flags():
    from docurandom.channel_runtime import analysis_evidence
    rows=[{'evidence_id':'a','video_id':'abcdefghijk','report':'reach','source_url':'https://studio.youtube.com/video/abcdefghijk/analytics',
           'metrics':{'impressions':0,'ctr':None},'availability':{'impressions':'observed','ctr':'unavailable'}}]
    result,catalogue=analysis_evidence(rows)
    item=result[0];assert item['metrics']=={'impressions':0}
    assert 'https://studio.youtube.com'+item['source_path']==rows[0]['source_url']
    codes=catalogue['records'][item['availability_ref']]
    restored={f:catalogue['statuses'][code]for f,code in zip(catalogue['fields'],codes)if code!=-1}
    assert restored==rows[0]['availability']


def test_analysis_historical_citations_must_be_supplied_and_exist(tmp_path):
    from docurandom.channel_learning import ChannelMemory
    from docurandom.channel_runtime import supplied_evidence_ids
    from tests.test_channel_learning import snapshot
    m=ChannelMemory(tmp_path,'UCnWW9-QkcA773UV8v0xPqvw')
    old=m.add_snapshot(snapshot(1));new=m.add_snapshot(snapshot(2));unknown='f'*64
    assert supplied_evidence_ids(m,'Historical evidence '+old+' unknown '+unknown)==[old]
    assert new not in supplied_evidence_ids(m,old)


def test_dashboard_reads_utf8_without_utf8_process_mode(tmp_path):
    import os,subprocess,sys
    from docurandom.channel_learning import ChannelMemory
    cid='UC'+'a'*22
    (tmp_path/'learning-policy.json').write_text(json.dumps({'schema_version':1,'enabled':True,'channel_id':cid}),encoding='utf-8')
    m=ChannelMemory(tmp_path,cid);m.initialize({'name':'日本の工作機械'})
    details=m.root/'video-details';details.mkdir()
    (details/'cursor.json').write_text(json.dumps({'status':'日本語を確認','pending':[]},ensure_ascii=False),encoding='utf-8')
    code='from pathlib import Path;from types import SimpleNamespace;from docurandom.learning_dashboard import summary;import sys;w=SimpleNamespace(name="pilot",state_dir=Path(sys.argv[1]));s=summary(w);assert "\\u65e5\\u672c\\u8a9e" in s;print("CP1252_UTF8_FILES_PASS")'
    env=dict(os.environ,PYTHONUTF8='0',PYTHONIOENCODING='cp1252')
    run=subprocess.run([sys.executable,'-X','utf8=0','-c',code,str(tmp_path)],env=env,capture_output=True,text=True)
    assert run.returncode==0,run.stderr
