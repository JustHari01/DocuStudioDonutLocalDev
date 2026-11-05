from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from docurandom import google_workspace as g


@pytest.fixture
def workspace(tmp_path):
    state=tmp_path/'.docurandom';state.mkdir()
    w=SimpleNamespace(state_dir=state,workspace_id='workspace',name='Fixture',settings_path=state/'settings.json')
    g.atomic_write_json(w.settings_path,{'youtube_profile_id':'profile'})
    g.save_config(w,{'apps':['docs','sheets'],'apps_per_cycle':2,'create_files':True})
    return w


def completed_artifacts(w, wrong_profile=False, wrong_app=False):
    c={**g.config(w),'profile_id':'profile'}
    steps,_=g.steps_for(w,c,{})
    for step in steps:
        g.atomic_write_json(g.root(w)/'jobs'/(step['once_key']+'.json'),{
            'status':'complete','profile_id':'other'if wrong_profile else'profile',
            'result':{'status':'complete','app':'other'if wrong_app else step['app'],'action':'create',
                      'url':'https://docs.google.com/document/d/fixture/edit'}})
    return steps


def test_daily_completed_files_do_not_launch_pair_or_consume_another_cycle(workspace):
    completed_artifacts(workspace)
    coordinator=Mock();service=g.GoogleService(Mock(),coordinator)
    service.run_now(workspace);service.tick(workspace)
    state=g.read(g.root(workspace)/'state.json')
    assert state['status']=='cached' and state['profile_cache_hits']==1
    assert not state.get('manual_requested') and not state.get('daily_usage')
    assert not state.get('last_success_by_app')  # Cached results are not a new server verification.
    assert len(state['results'])==2
    coordinator.try_acquire.assert_not_called()
    coordinator.cancel_idle_waiter.assert_called_once_with('google:workspace')
    service.tick(workspace)
    assert g.read(g.root(workspace)/'state.json')['profile_cache_hits']==1


@pytest.mark.parametrize('damage',['missing','profile','app'])
def test_incomplete_or_mismatched_cached_artifact_keeps_real_work_queued(workspace,damage):
    steps=completed_artifacts(workspace,wrong_profile=damage=='profile',wrong_app=damage=='app')
    if damage=='missing':(g.root(workspace)/'jobs'/(steps[0]['once_key']+'.json')).unlink()
    coordinator=Mock();coordinator.try_acquire.return_value=None
    service=g.GoogleService(Mock(),coordinator);service.run_now(workspace);service.tick(workspace)
    assert g.read(g.root(workspace)/'state.json')['status']=='queued'
    coordinator.try_acquire.assert_called_once()


def test_retained_old_plan_is_never_replaced_by_new_planner(workspace):
    completed_artifacts(workspace)
    old=[{'app':'docs','action':'browse','url':g.APPS['docs'][1]}]
    g.atomic_write_json(g.root(workspace)/'state.json',{'cycle_id':'old','plan':old,'profile_id':'profile'})
    coordinator=Mock();coordinator.try_acquire.return_value=None
    service=g.GoogleService(Mock(),coordinator);service.run_now(workspace);service.tick(workspace)
    assert g.read(g.root(workspace)/'state.json')['plan']==old
    coordinator.try_acquire.assert_called_once()


@pytest.mark.parametrize('pid,owned_pid,prior_session,was_running,expect_close',[
    (None,None,None,True,False), (True,True,'session',True,False),
    (99,99,'old-session',True,False), (99,99,'session',True,True),
    (99,None,None,True,False), (99,None,None,False,True),
])
def test_profile_release_requires_real_pid_same_browser_session_and_ownership(
        workspace,monkeypatch,pid,owned_pid,prior_session,was_running,expect_close):
    from docurandom import donut_manager,donut_mcp,youtube_upload
    g.save_config(workspace,{'apps':['search'],'apps_per_cycle':1,'create_files':False})
    initial={}
    if owned_pid is not None:initial['owned_profile_pid']=owned_pid
    if prior_session:initial['profile_baseline']={'session_id':prior_session,'tabs':[]}
    g.atomic_write_json(g.root(workspace)/'state.json',initial)
    client=Mock();client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
    running=[was_running]
    client.get_profile.side_effect=lambda _:SimpleNamespace(running=running[0],process_id=pid)
    client.run_profile.side_effect=lambda *_:running.__setitem__(0,True)
    monkeypatch.setattr(donut_mcp,'DonutMcpClient',lambda *a,**k:client)
    monkeypatch.setattr(donut_manager,'resolve_donut_mcp_endpoint',lambda:'fixture')
    bridge=Mock();bridge.command.return_value={'session_id':'session','tabs':[],'owned_tab_ids':[]}
    monkeypatch.setattr(youtube_upload,'_ensure_youtube_browser_bridge',lambda *a,**k:bridge)
    close=Mock();monkeypatch.setattr(youtube_upload.YouTubeStudioUploader,'_close_profile_started_by_workflow',close)
    monkeypatch.setattr(g.GoogleRunner,'run_step',lambda *a:{'status':'complete','app':'search','action':'browse'})
    coordinator=Mock();coordinator.try_acquire.return_value=nullcontext()
    service=g.GoogleService(Mock(),coordinator);service.run_now(workspace);service.tick(workspace)
    assert g.read(g.root(workspace)/'state.json')['status']=='complete'
    assert close.called is expect_close
    if expect_close:assert 'owned_profile_pid'not in g.read(g.root(workspace)/'state.json')
    assert client.run_profile.call_count==(0 if was_running else 1)
