import hashlib
import json
import unicodedata
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from docurandom.thumbnail_ai import (
    claim_thumbnail_ai_generation, read_thumbnail_ai_state,
    reserve_thumbnail_recovery_chat, thumbnail_recovery_chat_remaining_seconds,
    thumbnail_request_for_claim, thumbnail_generation_request,
    thumbnail_canvas_request, thumbnail_canvas_transport_request,
    record_thumbnail_ai_loading_failure,
)
from tests.test_thumbnail_isolated_recovery import setup


def digest(text):
    return hashlib.sha256(' '.join(unicodedata.normalize('NFC', text).split()).encode()).hexdigest()


def save(w, state):
    (w.state_dir / 'thumbnail-ai.json').write_text(json.dumps(state), encoding='utf-8')


def test_first_finished_failure_prefers_new_chat_without_waiting_for_same_chat_retry(tmp_path):
    w, p, _, proof, data = setup(tmp_path)
    s = read_thumbnail_ai_state(w)
    s.update(automatic_retry_count=0, readonly_probe_count=28,
             detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: KHÔNG TẠO ĐƯỢC: công cụ không khởi tạo được ảnh.')
    save(w, s)
    claim = claim_thumbnail_ai_generation(w, p)
    assert claim.action == 'recover_new_chat'
    with patch('docurandom.storage.app_data_dir', return_value=data):
        assert reserve_thumbnail_recovery_chat(w, p, claim, s['thread_id'], 'fresh-one', proof)
    after = read_thumbnail_ai_state(w)
    assert after['automatic_retry_count'] == 0
    assert after['thread_id'] == 'fresh-one'
    assert after['recovery_chat']['count'] == 1


@pytest.mark.parametrize('status,submitted', [('uncertain', True), ('recovering', True), ('submitted', True), ('complete', True)])
def test_unknown_or_completed_receipt_never_qualifies_for_new_chat(status, submitted):
    assert thumbnail_recovery_chat_remaining_seconds({
        'status': status, 'submitted': submitted, 'thread_id': 'old',
        'detail': 'CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: KHÔNG TẠO ĐƯỢC',
        'updated_at': (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),
    }) is None


@pytest.mark.parametrize('reason', ['HTTP 429', 'quota', 'rate_limited', 'giới hạn', 'captcha', 'chính sách', 'đăng nhập'])
def test_account_or_policy_failure_does_not_open_fresh_chat(reason):
    assert thumbnail_recovery_chat_remaining_seconds({
        'status': 'terminal_no_image', 'submitted': False, 'thread_id': 'old',
        'detail': 'CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: '+reason,
        'updated_at': (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),
    }) is None


def test_one_recovery_request_cannot_open_another_chat_and_both_links_survive(tmp_path):
    w, p, claim, proof, data = setup(tmp_path)
    bridge = data/'chat-bridge/bridge-state.json'
    with patch('docurandom.storage.app_data_dir', return_value=data):
        assert reserve_thumbnail_recovery_chat(w, p, claim, 'old_video_thread', 'fresh-one', proof)
        first = read_thumbnail_ai_state(w)
        first_journal = first['recovery_chat']['receipt_path']
        first.update(status='terminal_no_image', submitted=False,
                     updated_at=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat(),
                     detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: KHÔNG TẠO ĐƯỢC: công cụ lỗi.')
        save(w, first)
        b = json.loads(bridge.read_text())
        b['savedTurns']['thread:fresh-one'] = {'chatUrl': 'https://chatgpt.com/c/6ad114bc-6e10-83ec-8988-3363f072eb80'}
        bridge.write_text(json.dumps(b))
        second = claim_thumbnail_ai_generation(w, p)
        assert second.action == 'blocked'
        assert not reserve_thumbnail_recovery_chat(w, p, second, 'fresh-one', 'fresh-two', proof)
        own_proof = {**proof, 'conversation_id': '6ad114bc-6e10-83ec-8988-3363f072eb80'}
        assert not reserve_thumbnail_recovery_chat(w, p, second, 'fresh-one', 'fresh-two', own_proof)
        after = read_thumbnail_ai_state(w)
        assert after['recovery_chat']['count'] == 1
        assert after['recovery_chat']['origin_thread_id'] == 'old_video_thread'
        assert after['recovery_chat']['receipt_path'] == first_journal
        assert thumbnail_recovery_chat_remaining_seconds(after) is None
        from docurandom.conversation_links import retain_thumbnail_link
        retain_thumbnail_link(w,after,bridge)
    history = json.loads((w.state_dir/'thumbnail-conversations.json').read_text())['records']
    assert {'old_video_thread', 'fresh-one'} <= {r['thread_id'] for r in history}


@pytest.mark.parametrize('wire,wire_request', [(3, thumbnail_canvas_request), (4, thumbnail_canvas_transport_request)])
def test_fresh_chat_reads_legacy_request_but_resets_new_request_to_original_design(tmp_path, wire, wire_request):
    w, p, old, proof, data = setup(tmp_path)
    s = read_thumbnail_ai_state(w)
    s.update(generation_wire_version=wire, canvas_repair={'count': 1})
    save(w, s)
    claim = claim_thumbnail_ai_generation(w, p)
    assert claim.action == 'recover_new_chat'
    assert thumbnail_request_for_claim(claim) == wire_request(claim.prompt)
    exact = {**proof, 'prompt_sha256': digest(wire_request(claim.prompt))}
    with patch('docurandom.storage.app_data_dir', return_value=data):
        assert not reserve_thumbnail_recovery_chat(w, p, claim, s['thread_id'], 'fresh', proof)
        assert reserve_thumbnail_recovery_chat(w, p, claim, s['thread_id'], 'fresh', exact)
    resumed = claim_thumbnail_ai_generation(w, p)
    assert thumbnail_request_for_claim(resumed) == thumbnail_generation_request(claim.prompt)


def test_latest_different_prompt_or_missing_source_keeps_old_checkpoint_and_backs_off(tmp_path):
    w, p, claim, proof, data = setup(tmp_path)
    with patch('docurandom.storage.app_data_dir', return_value=data):
        before = (w.state_dir/'thumbnail-ai.json').read_bytes()
        assert not reserve_thumbnail_recovery_chat(w, p, claim, 'old_video_thread', 'fresh',
                                                   {**proof, 'latest_request_prompt_sha256': '0'*64})
        assert (w.state_dir/'thumbnail-ai.json').read_bytes() == before
        assert record_thumbnail_ai_loading_failure(w, p, claim, 'CHATGPT_CONVERSATION_LOAD_FAILED')
    state = read_thumbnail_ai_state(w)
    assert state['retry_probe_failures'] == 1
    assert 55 <= thumbnail_recovery_chat_remaining_seconds(state) <= 60
    assert claim_thumbnail_ai_generation(w, p).action == 'blocked'


@pytest.mark.parametrize('already_has_image', [False, True])
def test_real_worker_reuses_found_image_or_sends_original_prompt_without_legacy_canvas(tmp_path, already_has_image):
    import io
    from types import SimpleNamespace
    from PIL import Image
    from tests.test_stalled_thumbnail_repair import recovered_app, run_worker
    app, package, state_path = recovered_app(tmp_path)
    s = read_thumbnail_ai_state(app.workspace)
    s.update(status='terminal_no_image', submitted=False, automatic_retry_count=0,
             generation_wire_version=3, canvas_repair={'count':1},
             detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: KHÔNG TẠO ĐƯỢC: lỗi công cụ.')
    save(app.workspace, s)
    claim = claim_thumbnail_ai_generation(app.workspace, package)
    data = tmp_path/'Data'
    bridge = data/'chat-bridge'
    bridge.mkdir(parents=True)
    cid = '6aceaf4f-44cc-83ec-a402-61fadf210822'
    (bridge/'bridge-state.json').write_text(json.dumps({'savedTurns': {
        'thread:exact-old-thread': {'chatUrl':'https://chatgpt.com/c/'+cid}}}))
    proof = {'complete':True, 'terminal_no_image':True, 'prompt_sha256':digest(thumbnail_canvas_request(claim.prompt)),
             'conversation_id':cid, 'image_message_ids':[], 'matched_user_ids':['u'], 'terminal_detail':'KHÔNG TẠO ĐƯỢC'}
    out = io.BytesIO()
    Image.new('RGB', (1280,720), (10,40,90)).save(out, format='PNG')
    generated = SimpleNamespace(data=out.getvalue(), response_id='image', mime_type='image/png')
    calls = []
    class Chat:
        def __init__(self, *args, **kw):
            self.thread_id = kw.get('thread_id') or 'fresh-chat'
            self.response_id = ''
            self._last_existing_image_evidence = {'status':'no-image','source_proof':proof}
        def read_existing_generated_image(self, **kw):
            calls.append(('read',self.thread_id,kw['expected_prompt']))
            return generated if already_has_image else None
        def diagnose_session(self): return {}
        def generate_image(self, prompt, **kw):
            assert not kw.get('attachments')
            assert prompt == thumbnail_generation_request(claim.prompt)
            calls.append(('generate',self.thread_id,prompt))
            kw['on_status']('submitted','accepted')
            return generated
        def send(self, *args, **kw): return 'ĐÃ TẠO'
    with patch('docurandom.storage.app_data_dir',return_value=data), patch('docurandom.ui._thumbnail_terminal_retry_idle_signature',return_value=('idle',)):
        run_worker(app,package,Chat)
    after = read_thumbnail_ai_state(app.workspace)
    assert after['status'] == 'complete'
    assert calls[0] == ('read','exact-old-thread',thumbnail_canvas_request(claim.prompt))
    if already_has_image:
        assert len(calls) == 1
        assert after['thread_id'] == 'exact-old-thread'
    else:
        assert calls[1][0:2] == ('generate','fresh-chat')
        assert after['generation_wire_version'] == 1
        assert after['recovery_chat']['count'] == 1
