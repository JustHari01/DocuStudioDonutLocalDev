import json
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from docurandom import google_workspace as g
from docurandom import google_routine_policy as policy


@pytest.fixture
def workspace(tmp_path):
    d=tmp_path/'.docurandom';d.mkdir()
    (d/'settings.json').write_text('{"youtube_profile_id":"exact-profile","schedule_armed":false}')
    return SimpleNamespace(state_dir=d,settings_path=d/'settings.json',workspace_id='a'*32,name='DaiTa4')


@pytest.mark.parametrize('field,value',[
    ('daily_cycle_limit',0),('daily_cycle_limit',True),('max_cycle_minutes',61),
    ('active_start','9:00'),('active_end','24:00'),('weekdays',[]),('weekdays',[True]),('weekdays',[7]),
])
def test_invalid_policy_is_not_saved(workspace,field,value):
    with pytest.raises(ValueError):g.save_config(workspace,{field:value})
    assert not(g.root(workspace)/'config.json').exists()


def test_same_time_means_whole_selected_day():
    c=policy.validate({'weekdays':[0],'active_start':'10:00','active_end':'10:00'})
    assert policy.in_window(c,datetime(2026,10,26,0,tzinfo=g.VN))
    assert not policy.in_window(c,datetime(2026,10,27,10,tzinfo=g.VN))


def test_overnight_window_is_assigned_to_starting_weekday():
    c=policy.validate({'weekdays':[0],'active_start':'22:00','active_end':'02:00'})
    assert policy.in_window(c,datetime(2026,10,26,23,tzinfo=g.VN))
    assert policy.in_window(c,datetime(2026,10,27,1,tzinfo=g.VN))
    assert not policy.in_window(c,datetime(2026,10,26,1,tzinfo=g.VN))
    assert not policy.in_window(c,datetime(2026,10,27,2,tzinfo=g.VN))


def test_next_window_handles_weekend_and_minute_boundary():
    c=policy.validate({'weekdays':[0],'active_start':'09:00','active_end':'17:00'})
    sunday=datetime(2026,10,25,19,59,45,tzinfo=g.VN)
    assert policy.next_window(c,sunday)==datetime(2026,10,26,9,tzinfo=g.VN)


def test_cycle_reservation_is_once_across_restart_and_midnight():
    clock=datetime(2026,10,28,23,59,tzinfo=g.VN);state={}
    policy.reserve(state,'same',clock)
    restarted=json.loads(json.dumps(state))
    policy.reserve(restarted,'same',clock+timedelta(minutes=2))
    assert restarted==state
    policy.account_elapsed(restarted,clock,2.5)
    policy.account_elapsed(restarted,clock,-10)
    assert restarted['daily_usage']['2026-10-28']['elapsed_seconds']==2.5


def test_daily_limit_allows_old_cycle_but_not_another_new_cycle():
    clock=datetime(2026,10,28,20,tzinfo=g.VN);state={}
    for i in range(3):policy.reserve(state,str(i),clock)
    wait=policy.gate(policy.DEFAULTS,state,clock)
    assert wait['status']=='daily_limit'and wait['next_run_at']=='2026-10-29T00:00:00+07:00'
    state.update(cycle_id='2',plan=[{'app':'search'}])
    assert policy.gate(policy.DEFAULTS,state,clock)is None


def test_manual_request_waits_for_window_without_profile_lease(workspace,monkeypatch):
    g.save_config(workspace,{'enabled':False,'active_start':'09:00','active_end':'17:00'})
    monkeypatch.setattr(g,'now',lambda:datetime(2026,10,28,20,tzinfo=g.VN))
    coordinator=Mock();store=SimpleNamespace(get=lambda _:workspace)
    service=g.GoogleService(store,coordinator);service.run_now(workspace);service.tick(workspace)
    state=g.read(g.root(workspace)/'state.json')
    assert state['status']=='waiting_window'and state['manual_requested']
    coordinator.try_acquire.assert_not_called()


def test_daily_limit_survives_manual_request_and_service_restart(workspace,monkeypatch):
    clock=datetime(2026,10,28,20,tzinfo=g.VN);monkeypatch.setattr(g,'now',lambda:clock)
    state={}
    for i in range(3):policy.reserve(state,str(i),clock)
    g.atomic_write_json(g.root(workspace)/'state.json',state)
    coordinator=Mock();store=SimpleNamespace(get=lambda _:workspace)
    service=g.GoogleService(store,coordinator);service.run_now(workspace)
    g.GoogleService(store,coordinator).tick(workspace)
    assert g.read(g.root(workspace)/'state.json')['status']=='daily_limit'
    coordinator.try_acquire.assert_not_called()


def test_preview_is_side_effect_free_and_keeps_pending_plan(workspace):
    plan=[{'app':'docs','action':'create','title':'kept','once_key':'b'*64}]
    g.atomic_write_json(g.root(workspace)/'state.json',{'cycle_id':'same','plan':plan})
    g.atomic_write_json(g.root(workspace)/'jobs'/('b'*64+'.json'),{'status':'submitted'})
    before={p:p.read_bytes()for p in workspace.state_dir.rglob('*')if p.is_file()}
    preview=g.preview(workspace,{'apps':['maps'],'create_files':False})
    assert preview['retained_plan']and preview['steps'][0]['title']=='kept'
    assert preview['steps'][0]['receipt_status']=='submitted'
    assert before=={p:p.read_bytes()for p in workspace.state_dir.rglob('*')if p.is_file()}


def test_diagnostics_never_pairs_launches_or_writes_to_google(workspace,monkeypatch):
    from docurandom import browser_extension_bridge
    bridge=Mock();bridge.status.return_value=SimpleNamespace(connected=True,extension_version='1.1.42')
    bridge.command.side_effect=[{'capabilities':['google-workspace-v2']},{'tabs':[{'id':1,'url_digest':'a'*64}]}]
    monkeypatch.setattr(browser_extension_bridge,'get_browser_extension_bridge',lambda:bridge)
    report=g.GoogleService(Mock(),Mock()).diagnostics(workspace)
    assert [call.args[1]for call in bridge.command.call_args_list]==['ping','google_inventory']
    bridge.begin_pairing.assert_not_called()
    assert report['connection']['google_protocol_ready']and report['creates_files']is False
    assert len(report['apps'])==26 and all(a['authenticated_now']=='not_verified'for a in report['apps'])
    assert not(g.root(workspace)/'jobs').exists()
    assert not(g.root(workspace)/'state.json').exists()


def test_disconnected_diagnostics_does_not_send_command(workspace,monkeypatch):
    from docurandom import browser_extension_bridge
    bridge=Mock();bridge.status.return_value=SimpleNamespace(connected=False)
    monkeypatch.setattr(browser_extension_bridge,'get_browser_extension_bridge',lambda:bridge)
    report=g.GoogleService(Mock(),Mock()).diagnostics(workspace)
    assert not report['connection']['connected']
    bridge.command.assert_not_called()


def test_time_budget_stops_before_a_command_without_charging_uncertain_retry(workspace,monkeypatch):
    bridge=Mock();runner=g.GoogleRunner(workspace,bridge,'profile')
    runner.deadline=100;monkeypatch.setattr(g.time,'monotonic',lambda:101)
    with pytest.raises(InterruptedError):runner.command('google_create',{})
    bridge.command.assert_not_called()
    assert not(g.root(workspace)/'jobs').exists()


def test_daily_report_counts_logical_cycles_not_retries(workspace):
    clock=g.now();state={'activity_cycle_id':'one','status':'recovering'}
    policy.reserve(state,'one',clock)
    g.record_activity(workspace,state);g.record_activity(workspace,state)
    state['status']='complete';g.record_activity(workspace,state)
    report=g.read(g.root(workspace)/'daily-summary.json')
    assert report['cycles_started']==1 and report['cycles_complete_in_retained_history']==1
    assert report['recovery_events_in_retained_history']==2
    assert report['measurement']=='local_task_results_only'


def test_notebook_fits_small_window_and_preview_does_not_run_google(workspace):
    # Windows' bundled Tcl owns one application root per process. Keep the real
    # layout check out of the interpreter used by the shared UI interaction suite.
    import subprocess,sys
    script=r'''
import sys,tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from tkinter import ttk
from docurandom.google_workspace_ui import settings_dialog
d=Path(sys.argv[1]);workspace=SimpleNamespace(state_dir=d,settings_path=d/'settings.json',workspace_id='a'*32,name='DaiTa4')
owner=tk.Tk();owner.withdraw();service=Mock()
try:
 dialog=settings_dialog(owner,workspace,service);dialog.geometry('800x660');dialog.update_idletasks()
 def children(widget):
  result=[]
  for child in widget.winfo_children():result.append(child);result.extend(children(child))
  return result
 widgets=children(dialog);tabs=next(w for w in widgets if isinstance(w,ttk.Notebook));assert len(tabs.tabs())==3
 for tab in tabs.tabs():tabs.select(tab);dialog.update_idletasks()
 buttons={w.cget('text'):w for w in widgets if isinstance(w,ttk.Button)}
 buttons['Xem kế hoạch'].invoke();dialog.update_idletasks()
 service.run_now.assert_not_called();service.diagnostics.assert_not_called()
 for name in ['Lưu thiết lập','Chạy một lượt ngay','Xem kế hoạch','Kiểm tra kết nối']:
  b=buttons[name];assert 0<=b.winfo_rooty()-dialog.winfo_rooty()<dialog.winfo_height()-b.winfo_height()
finally:owner.destroy()
'''
    result=subprocess.run([sys.executable,'-X','utf8','-c',script,str(workspace.state_dir)],capture_output=True,text=True,encoding='utf8',timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
