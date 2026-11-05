from unittest.mock import patch
from docurandom.workflow_notifications import failure_notice_key,reserve_failure_notice

def test_repeated_probe_variants_across_restart_emit_one_recovery_notice(tmp_path):
    path=tmp_path/'notices.json';identity=['same-video','same-chat',0]
    key=failure_notice_key('thumbnail','THUMBNAILAIERROR','CHATGPT_RECOVERY_PENDING: wait',True,identity)
    assert reserve_failure_notice({},key,now=100000,recovering=True,journal=path)[0]
    changed=failure_notice_key('thumbnail','THUMBNAILAIERROR','CHATGPT_CONVERSATION_LOAD_FAILED: DOM 0',True,identity)
    assert changed==key
    for minutes in (17,34,60,120):
        assert not reserve_failure_notice({},changed,now=100000+minutes*60,recovering=True,journal=path)[0]

def test_quota_manual_escalation_and_new_video_remain_immediate(tmp_path):
    identity=['video','chat',0];path=tmp_path/'notices.json';now=100000
    keys=[failure_notice_key('thumbnail','ERR',detail,recovering,id_)for detail,recovering,id_ in [
        ('CHATGPT_RECOVERY_PENDING',True,identity),('HTTP 429',True,identity),
        ('CHATGPT_RECOVERY_PENDING',False,identity),('CHATGPT_RECOVERY_PENDING',True,['new-video','new-chat',0]),
        ('CHATGPT_RECOVERY_PENDING',True,['video','chat',1])]]
    assert len(set(keys))==5
    for key in keys:assert reserve_failure_notice({},key,now=now,recovering=True,journal=path)[0]

def test_journal_write_failure_cannot_stop_the_local_workflow(tmp_path):
    with patch('docurandom.workflow_notifications.atomic_write_json',side_effect=OSError('disk unavailable')):
        allowed,cache=reserve_failure_notice({},'key',now=1000,recovering=True,journal=tmp_path/'notices.json')
    assert allowed and cache['key']==1000
