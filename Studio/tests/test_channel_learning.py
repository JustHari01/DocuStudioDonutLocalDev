import json
from pathlib import Path
import pytest

from docurandom.channel_learning import ChannelMemory, production_context
from docurandom.channel_analytics import parse_number, snapshot_from_ax

CHANNEL = 'UCnWW9-QkcA773UV8v0xPqvw'


def snapshot(value=145):
    return {'channel_id':CHANNEL,'collected_at':'2026-10-21T09:00:00+00:00',
            'source_url':f'https://studio.youtube.com/channel/{CHANNEL}/analytics/tab-overview/period-default',
            'report_range':{'label':'28 ngày qua','dates':'23/9–20/10/2026'},
            'metrics':{'views':value,'ctr':None},'availability':{'views':'observed','ctr':'unavailable'}}


def lesson(evidence,status='hypothesis'):
    return {'id':'hook-one','claim':'A scoped possible hook improvement','status':status,
            'scope':'Channel test only, not a causal finding','evidence_ids':[evidence],
            'limitations':'Low volume; traffic mix unknown','reason':'Explicit test proposal'}


def test_history_survives_restart_and_rejected_findings_are_retained(tmp_path):
    m=ChannelMemory(tmp_path,CHANNEL);m.initialize({'language':'ja'})
    e=m.add_snapshot(snapshot())
    assert m.add_snapshot(snapshot())==e
    m.apply_analysis({'channel_id':CHANNEL,'lessons':[lesson(e)]},snapshot_ids=[e])
    old=m.export().read_text(encoding='utf-8')
    m.apply_analysis({'channel_id':CHANNEL,'lessons':[lesson(e,'rejected')]},snapshot_ids=[e])
    new=ChannelMemory(tmp_path,CHANNEL)
    assert len(new.snapshots())==1
    assert new.lessons()[0]['status']=='rejected'
    with new.connect() as db: assert db.execute('SELECT COUNT(*) FROM lessons').fetchone()[0]==2
    assert any(p.read_text(encoding='utf-8')==old for p in (m.root/'versions').glob('*.md'))
    new.initialize({'language':'en','overwrite':'not allowed'})
    assert '"language": "ja"' in new.context()


def test_wrong_channel_and_unsourced_patches_do_not_mutate_memory(tmp_path):
    m=ChannelMemory(tmp_path,CHANNEL);m.initialize({'language':'ja'});e=m.add_snapshot(snapshot())
    with pytest.raises(ValueError):m.add_snapshot(dict(snapshot(),channel_id='UC'+'z'*22))
    good=lesson(e);bad=dict(lesson(e),id='bad',evidence_ids=['invented'])
    with pytest.raises(ValueError):m.apply_analysis({'channel_id':CHANNEL,'lessons':[good,bad]},snapshot_ids=[e])
    assert m.lessons()==[]
    with pytest.raises(ValueError):m.apply_analysis({'channel_id':CHANNEL,'charter':{},'lessons':[]},snapshot_ids=[e])


def test_numeric_zero_is_not_missing_and_nan_is_rejected(tmp_path):
    m=ChannelMemory(tmp_path,CHANNEL);m.initialize({});m.add_snapshot(snapshot(0))
    assert m.snapshots()[0]['metrics']=={'views':0,'ctr':None}
    with pytest.raises(ValueError):m.add_snapshot(snapshot(float('nan')))
    assert parse_number('1,2','vi')==1.2
    assert parse_number('1.234','vi')==1234
    assert parse_number('1,234','en')==1234
    assert parse_number('1.2万','ja')==12000
    assert parse_number('—','vi') is None


def test_request_reservation_never_overwrites_uncertain_prompt(tmp_path):
    m=ChannelMemory(tmp_path,CHANNEL)
    job=m.reserve('daily-2026-10-21','original')
    m.job_state(job['id'],'uncertain',detail='accepted but not recovered')
    with pytest.raises(ValueError):m.reserve(job['id'],'changed')
    assert m.reserve(job['id'],'original')['status']=='uncertain'


def test_corrupt_ledger_restores_verified_backup_and_keeps_damaged_copy(tmp_path):
    m=ChannelMemory(tmp_path,CHANNEL);m.initialize({'language':'ja'});e=m.add_snapshot(snapshot())
    m.database.write_bytes(b'damaged SQLite file')
    restored=ChannelMemory(tmp_path,CHANNEL)
    assert restored.snapshots()[0]['evidence_id']==e
    assert len(list(restored.root.glob('history.damaged-*.sqlite3')))==1


def test_recovery_does_not_apply_a_completed_analysis_twice(tmp_path):
    m=ChannelMemory(tmp_path,CHANNEL);m.initialize({});e=m.add_snapshot(snapshot())
    result={'channel_id':CHANNEL,'lessons':[lesson(e)]}
    m.apply_analysis(result,snapshot_ids=[e],analysis_id='daily-one')
    m.apply_analysis(result,snapshot_ids=[e],analysis_id='daily-one')
    assert m.lessons()[0]['revision']==1


def test_resume_never_changes_existing_master_identity(tmp_path):
    class W:state_dir=tmp_path
    (tmp_path/'learning-policy.json').write_text(json.dumps({'schema_version':1,'enabled':True,'channel_id':CHANNEL}),encoding='utf-8')
    m=ChannelMemory(tmp_path,CHANNEL);m.initialize({'language':'ja'})
    assert production_context(W(),resume=True)==''
    assert CHANNEL in production_context(W(),resume=False)


def test_actual_studio_accessibility_fixture_and_channel_guard():
    fixture=Path('artifacts/channel-learning-20261021/daita14-overview-ax.json')
    if not fixture.exists():pytest.skip('Local real-runtime fixture unavailable')
    raw=json.loads(fixture.read_text(encoding='utf-8'))
    value=snapshot_from_ax(raw['nodes'],channel_id=CHANNEL,url=snapshot()['source_url'],report='overview')
    assert value['metrics']['views']==145 and value['metrics']['watch_hours']==1.2
    assert value['metrics']['subscribers_net'] is None  # displayed dash, not zero
    # 23 realtime views and latest video's CTR do not leak into 28-day primary KPIs.
    assert value['metrics']['ctr_percent'] is None
    with pytest.raises(ValueError):snapshot_from_ax(raw['nodes'],channel_id=CHANNEL,url='https://accounts.google.com/',report='overview')
