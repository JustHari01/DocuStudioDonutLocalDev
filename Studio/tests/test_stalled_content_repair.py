import hashlib
import json
import threading
import unicodedata
from unittest.mock import Mock

import pytest

from docurandom.content_ai import (ChatGPTWebChat, ChatTurn, ContentAIError,
    CONTENT_REPAIR_PREFIX, MasterPromptRunner, extract_content_artifacts)
from docurandom.ui import _SharedChatGPTTurnPool
from tests.test_chatgpt_recovery import dom_app

VOICE = "日本の工場では品質の記録と設備の条件を比較しました。" * 100


def digest(text):
    return hashlib.sha256(" ".join(unicodedata.normalize("NFC", text).split()).encode()).hexdigest()


def seed(tmp_path):
    chat = ChatGPTWebChat('http://127.0.0.1:17841', 'chatgpt-web/auto', tmp_path, 't' * 48)
    chat.thread_id = 'same-video-thread'
    runner = MasterPromptRunner(chat, output_dir=tmp_path, project_name='video', minutes=10)
    runner.master_prompt_sha256 = hashlib.sha256(b'MASTER').hexdigest()
    runner.turns = [ChatTurn('user', 'MASTER'), ChatTurn('assistant', 'ĐÃ NẠP'),
        ChatTurn('user', 'BẮT ĐẦU'),
        ChatTurn('assistant', '[[tieu-de-duoc-chon]]日本の設備[[end-tieu-de-duoc-chon]]'),
        ChatTurn('user', 'TIẾP'), ChatTurn('assistant', f'[[part-1]]{VOICE}[[end-part-1]]'),
        ChatTurn('user', 'TIẾP')]
    runner._save_checkpoint()
    proof = {'status': 'recovery-baseline', 'proof': 'stable-completed-baseline',
        'safe_to_resend': False, 'safe_to_request_missing': True,
        'user_sha256': digest(runner.turns[-3].text),
        'assistant_sha256': digest(runner.turns[-2].text),
        'pending_prompt_sha256': digest(runner.turns[-1].text), 'evidence_sha256': 'a' * 64}
    return runner, chat, proof


def test_production_resume_requests_only_missing_material_once_preserving_ambiguous_turn(tmp_path):
    initial, chat, proof = seed(tmp_path)
    original = list(initial.turns)
    chat.recover_pending = Mock(return_value=proof)
    chat.send = Mock(side_effect=ContentAIError('fixture-targeted-followup'))
    resumed = MasterPromptRunner(chat, output_dir=tmp_path, project_name='video', minutes=10,
        recover_submitted_only=True)
    with pytest.raises(ContentAIError, match='fixture-targeted-followup'):
        resumed.run('MASTER')
    assert chat.recover_pending.call_args.kwargs['allow_missing_content_repair'] is True
    assert chat.send.call_count == 1
    prompt = chat.send.call_args.args[0]
    assert prompt.startswith(CONTENT_REPAIR_PREFIX) and prompt != 'TIẾP'
    assert 'không tạo video mới' in prompt and 'Giữ nguyên toàn bộ lời thoại' in prompt
    assert resumed.turns[:-1] == original
    saved = json.loads(resumed.checkpoint_path.read_text())
    assert saved['content_repair_attempts'] == 1
    assert saved['stalled_content_repair']['status'] == 'enqueued'
    assert saved['pending_turn_transport_started'] is False


def test_reserved_repair_survives_restart_and_cancelled_fifo_without_second_reservation(tmp_path):
    runner, chat, proof = seed(tmp_path)
    runner._reserve_stalled_content_repair(extract_content_artifacts(runner.turns), proof)
    request = runner.stalled_content_repair['prompt']
    chat.send = Mock(side_effect=ContentAIError('fixture-fifo-cancel'))
    chat.recover_pending = Mock(side_effect=AssertionError('Already reserved; never reprove/reserve another request'))
    for _ in range(2):
        restored = MasterPromptRunner(chat, output_dir=tmp_path, project_name='video', minutes=10,
            recover_submitted_only=True)
        with pytest.raises(ContentAIError, match='fixture-fifo-cancel'):
            restored.run('MASTER')
        saved = json.loads(restored.checkpoint_path.read_text())
        assert saved['content_repair_attempts'] == 1
        assert saved['turns'][-1]['text'] == request
        assert sum(t['text'] == request for t in saved['turns']) == 1
        assert sum(t['role'] == 'user' and t['text'] == 'TIẾP' for t in saved['turns']) == 2
    assert chat.send.call_count == 2 and all(c.args == (request,) for c in chat.send.call_args_list)
    chat.recover_pending.assert_not_called()


def test_successful_missing_metadata_repair_finishes_package_with_original_narration(tmp_path):
    initial, chat, proof = seed(tmp_path)
    metadata = ('[[mo-ta]]同じ動画の説明[[end-mo-ta]]\n'
        '[[prompt-thumbnail]]Industrial documentary thumbnail[[end-prompt-thumbnail]]\n'
        '[[prompt-hinh-anh]]\n' + '\n'.join(f'Image {i}. matching factory equipment {i}' for i in range(1, 21))
        + '\n[[end-prompt-hinh-anh]]')
    chat.recover_pending = Mock(return_value=proof)
    chat.send = Mock(side_effect=[metadata, 'ĐÃ XONG', metadata])
    resumed = MasterPromptRunner(chat, output_dir=tmp_path, project_name='video', minutes=10,
        recover_submitted_only=True)
    result = resumed.run('MASTER')
    assert result.thread_id == 'same-video-thread'
    assert result.artifacts.voicevox_script == VOICE
    assert result.artifacts.title == '日本の設備'
    assert result.artifacts.description and result.artifacts.thumbnail_prompt and len(result.artifacts.image_prompts) == 20
    assert all(call.args[0] != 'TIẾP' for call in chat.send.call_args_list)
    assert sum(call.args[0].startswith(CONTENT_REPAIR_PREFIX) for call in chat.send.call_args_list) == 1
    assert json.loads(result.checkpoint_path.read_text())['stalled_content_repair']['status'] == 'completed'


@pytest.mark.parametrize('change', ['wrong-reply', 'resend', 'no-proof', 'no-voice', 'budget'])
def test_unknown_receipt_or_incomplete_script_cannot_authorize_followup(tmp_path, change):
    runner, chat, proof = seed(tmp_path)
    if change == 'wrong-reply': proof['assistant_sha256'] = 'f' * 64
    elif change == 'resend': proof['safe_to_resend'] = True
    elif change == 'no-proof': proof['safe_to_request_missing'] = False
    elif change == 'no-voice': runner.turns[-2] = ChatTurn('assistant', 'ĐÃ NẠP')
    elif change == 'budget': runner.content_repair_attempts = 3
    before = runner.checkpoint_path.read_bytes()
    with pytest.raises(ContentAIError, match='Chưa đủ bằng chứng'):
        runner._reserve_stalled_content_repair(extract_content_artifacts(runner.turns), proof)
    assert runner.checkpoint_path.read_bytes() == before
    assert not runner.stalled_content_repair


def test_repair_refuses_topic_or_narration_replacement(tmp_path):
    runner, chat, proof = seed(tmp_path)
    runner._reserve_stalled_content_repair(extract_content_artifacts(runner.turns), proof)
    chat.send = Mock(return_value=f'[[part-1]]別の動画の内容です。{VOICE}[[end-part-1]]')
    with pytest.raises(ContentAIError, match='thay đổi kịch bản'):
        runner._resume_stalled_content_repair(cancel_event=threading.Event(), on_turn=None,
            on_partial=None, on_status=None)
    assert not runner.package_path.exists()


def test_model_account_cooldown_does_not_spend_readonly_probe(tmp_path):
    app = dom_app(tmp_path)
    app.chatgpt_turn_pool = _SharedChatGPTTurnPool(3)
    app.chatgpt_turn_pool.mark_rate_limited(cooldown_seconds=3600)
    assert not app._has_safe_pending_chatgpt_turn()
    assert not (tmp_path / 'chatgpt-dom-recovery.json').exists()


@pytest.mark.parametrize('armed', [True, False])
def test_deferred_public_clock_does_not_permanently_lock_content_recovery(tmp_path, monkeypatch, armed):
    app = dom_app(tmp_path)
    app.schedule_manual_resume_required = True
    app.schedule_manual_resume_reason = 'chatgpt: ChatGPT cần được kiểm tra trước khi tiếp tục.'
    app.schedule_active_stage = 'deferred-upload'
    app.schedule_active_shift_id = 'existing-shift'
    app._schedules_armed = Mock(return_value=armed)
    app._clear_manual_pipeline_resume_barrier = Mock()
    app._scheduled_video_is_ready = Mock(return_value=False)
    app._save_config = Mock()
    app._start_content_pipeline = Mock()
    for name in ('content_busy', 'flow_generation_busy', 'pipeline_voice_busy', 'rendering',
        'scanning', 'audio_preflight_active', 'youtube_upload_busy'):
        setattr(app, name, False)
    monkeypatch.setattr('docurandom.ui.pipeline_resume_summary', lambda _: ('chatgpt', {}))
    app._resume_active_scheduled_pipeline_if_idle()
    assert app._start_content_pipeline.call_count == int(armed)
    assert app._clear_manual_pipeline_resume_barrier.call_count == int(armed)
    assert (tmp_path / 'chatgpt-dom-recovery.json').exists() is armed
