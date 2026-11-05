from types import SimpleNamespace
from unittest.mock import Mock
from datetime import datetime,timedelta,timezone
import json
from docurandom import google_workspace as g
from docurandom.thumbnail_ai import thumbnail_canvas_repair_remaining_seconds

def test_cancel_clears_manual_queue_without_resetting_plan_or_receipts(tmp_path):
    w=SimpleNamespace(workspace_id='w',state_dir=tmp_path,name='Test')
    store=SimpleNamespace(get=lambda _:w);coordinator=Mock()
    service=g.GoogleService(store,coordinator);service.requested.add('w');service.waiter_keys.add('google:w')
    g.atomic_write_json(g.root(w)/'state.json',{'status':'queued','manual_requested':True,'cycle_id':'keep','plan':[{'once_key':'keep-receipt'}],'recovery_attempts':3})
    service.control('google_cancel','w')
    state=g.read(g.root(w)/'state.json')
    assert not state['manual_requested'] and state['cycle_id']=='keep' and state['recovery_attempts']==3
    assert state['plan']==[{'once_key':'keep-receipt'}]and not service.requested
    coordinator.cancel_idle_waiter.assert_called_once_with('google:w')

def test_missing_source_canvas_uses_completed_fresh_chat_without_forging_counter():
    state={'status':'terminal_no_image','submitted':False,'generation_wire_version':1,
        'automatic_retry_count':0,'recovery_chat':{'count':1},'thread_id':'thread',
        'detail':'CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: no usable image target',
        'updated_at':(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()}
    assert thumbnail_canvas_repair_remaining_seconds(state)==0
    assert state['automatic_retry_count']==0
    state['canvas_repair']={'count':1};assert thumbnail_canvas_repair_remaining_seconds(state)is None
    state.pop('canvas_repair');state['detail']+=' quota';assert thumbnail_canvas_repair_remaining_seconds(state)is None
