import hashlib
import json
import threading
from pathlib import Path

import pytest

from docurandom.content_ai import (
    ChatTurn, ContentArtifacts, ContentAIError, MasterPromptRunner, FINAL_EXPORT_REQUEST,
    IMAGE_COMPLETION_QUESTION, CONTENT_REPAIR_PREFIX, extract_content_artifacts,
    merge_exported_artifacts, load_completed_content_checkpoint,
)
from docurandom.content_quality import narration_problem, narration_duration_problem
from docurandom.narration_identity import package_narration, validate_narration_duration, record_narration_duration_repair
from docurandom.voicevox import VoicevoxError


VOICE = "日本の工場では品質の記録を比較しました。\n" * 130
MASTER = "MASTER production rules"


def marked(script=VOICE):
    return ("[[tieu-de-duoc-chon]]正しい動画の題名[[end-tieu-de-duoc-chon]]\n"
            "[[mo-ta]]正しい説明[[end-mo-ta]]\n"
            "[[prompt-thumbnail]]Real thumbnail instructions[[end-prompt-thumbnail]]\n"
            f"[[part-1]]{script}[[end-part-1]]\n"
            "[[prompt-hinh-anh]]\n" + "\n".join(f"Image {n}. real image {n}" for n in range(1, 11)) +
            "\n[[end-prompt-hinh-anh]]")


def test_master_echo_examples_never_become_video_material():
    example = marked('[Nội dung kịch bản PART 1]')
    actual = marked(VOICE)
    turns = [ChatTurn('user', MASTER), ChatTurn('assistant', example),
             ChatTurn('user', 'BẮT ĐẦU\nThời lượng yêu cầu: 10 phút.'), ChatTurn('assistant', actual)]
    assert extract_content_artifacts(turns).voicevox_script == VOICE.strip()
    only_setup = turns[:3] + [ChatTurn('assistant', 'Đã bắt đầu.')]
    assert extract_content_artifacts(only_setup).voicevox_script == ''


@pytest.mark.parametrize('header', ['VOICEVOX', 'A: VOICEVOX', '**Thoại VOICEVOX**'])
def test_final_export_voice_heading_variants_preserve_real_narration(header):
    blank = ContentArtifacts('', '', '', '', '', ())
    result = merge_exported_artifacts(f'{header}\nA: {VOICE}\nPrompt hình ảnh\nImage 1. real', blank, 1)
    assert result.voicevox_script == VOICE.strip()


def test_summary_or_placeholder_cannot_shorten_the_full_canonical_script():
    fallback = extract_content_artifacts([ChatTurn('assistant', marked())])
    for voice in ('短い要約。', '[Nội dung kịch bản PART 1]'):
        result = merge_exported_artifacts('Thoại VOICEVOX\nA: ' + voice, fallback, 10)
        assert result.voicevox_script == VOICE.strip()


@pytest.mark.parametrize('script', ['[Nội dung kịch bản PART 1]', '\n'.join(['本当の文章。'] * 2001)], ids=['placeholder', 'overlong'])
def test_placeholder_and_overlong_scripts_fail_before_engine(script, tmp_path):
    package = tmp_path / 'video.txt'
    package.write_text('THOẠI VOICEVOX\n' + script, encoding='utf-8')
    with pytest.raises(VoicevoxError):
        package_narration(package)


def test_short_actual_audio_is_blocked_and_repair_budget_remains_durable(tmp_path):
    output = tmp_path / 'Output'; output.mkdir()
    state = tmp_path / '.docurandom'; state.mkdir()
    package = output / 'video.txt'; package.write_text('THOẠI VOICEVOX\n' + VOICE, encoding='utf-8')
    ai = output / 'video_AI_CHECKPOINT.json'
    ai.write_text(json.dumps({'project_name': 'video', 'content_repair_attempts': 2}), encoding='utf-8')
    (state / 'pipeline_checkpoint.json').write_text(json.dumps({'identity': {'minutes': 10},
        'stages': {'voicevox': {'artifacts': {'duration_seconds': 16}}}}), encoding='utf-8')
    with pytest.raises(VoicevoxError, match='NARRATION_CONTENT_INCOMPLETE'):
        validate_narration_duration(package)
    record_narration_duration_repair(package, 16, 10, VOICE)
    result = json.loads(ai.read_text(encoding='utf-8'))
    assert result['minimum_narration_characters'] > len(VOICE)
    assert result['content_repair_attempts'] == 2
    assert narration_duration_problem(574, 10) == ''


class Chat:
    thread_id = 'existing-video-chat'

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def send(self, prompt, **kwargs):
        self.sent.append(prompt)
        return self.responses.pop(0)


def seed(output, script):
    runner = MasterPromptRunner(Chat([]), output_dir=output, project_name='video', minutes=10,
                                minimum_characters=1500)
    runner.master_prompt_sha256 = hashlib.sha256(MASTER.encode()).hexdigest()
    runner.turns = [ChatTurn('user', MASTER), ChatTurn('assistant', 'ĐÃ NẠP'),
                   ChatTurn('user', 'BẮT ĐẦU\nThời lượng yêu cầu: 10 phút.'),
                   ChatTurn('assistant', marked(script)),
                   ChatTurn('user', IMAGE_COMPLETION_QUESTION), ChatTurn('assistant', 'ĐÃ XONG'),
                   ChatTurn('user', FINAL_EXPORT_REQUEST),
                   ChatTurn('assistant', 'THOẠI VOICEVOX\nA: ' + script)]
    runner._save_checkpoint()
    return runner


def test_invalid_saved_export_is_repaired_once_in_same_chat_before_package(tmp_path):
    seed(tmp_path, '[Nội dung kịch bản PART 1]')
    chat = Chat([f'[[part-1]]{VOICE}[[end-part-1]]'])
    runner = MasterPromptRunner(chat, output_dir=tmp_path, project_name='video', minutes=10,
                                minimum_characters=1500)
    result = runner.run(MASTER)
    assert len(chat.sent) == 1 and chat.sent[0].startswith(CONTENT_REPAIR_PREFIX)
    assert result.thread_id == 'existing-video-chat'
    assert narration_problem(result.artifacts.voicevox_script, 1500) == ''
    assert package_narration(result.package_path) == VOICE.strip()
    assert load_completed_content_checkpoint(output_dir=tmp_path, project_name='video',
        minutes=10, master_prompt=MASTER, minimum_characters=1500) is not None


def test_exhausted_confirmation_loop_uses_verified_artifacts_without_more_continue(tmp_path):
    runner = seed(tmp_path, VOICE)
    runner.turns = runner.turns[:4]
    for _ in range(47):
        runner.turns += [ChatTurn('user', 'TIẾP'), ChatTurn('assistant', 'CHƯA XONG')]
    runner.turns += [ChatTurn('user', IMAGE_COMPLETION_QUESTION), ChatTurn('assistant', 'CHƯA XONG')]
    runner._save_checkpoint()
    chat = Chat(['Đã xuất bản tổng hợp.'])
    result = MasterPromptRunner(chat, output_dir=tmp_path, project_name='video', minutes=10,
                                minimum_characters=1500).run(MASTER)
    assert chat.sent == [FINAL_EXPORT_REQUEST]
    assert len(result.artifacts.image_prompts) == 10
    assert json.loads(result.checkpoint_path.read_text(encoding='utf-8'))['content_locally_validated'] is True

def test_legacy_terminal_reservation_preserves_chat_and_budget_after_metadata_edit(tmp_path):
    from datetime import datetime, timedelta, timezone
    from tests.test_thumbnail_ai import _workspace, _package
    from docurandom.thumbnail_ai import (claim_thumbnail_ai_generation, update_thumbnail_ai_state,
        reserve_thumbnail_ai_automatic_retry, read_thumbnail_ai_state)
    w = _workspace(tmp_path)
    package = _package(w.output_dir / 'video.txt')
    claim = claim_thumbnail_ai_generation(w, package)
    update_thumbnail_ai_state(w, package, claim, status='terminal_no_image', submitted=False,
        thread_id='same-video-chat', detail='CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: no image')
    state = read_thumbnail_ai_state(w)
    state['updated_at'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    state['automatic_retry_count'] = 1
    path = w.state_dir / 'thumbnail-ai.json'
    path.write_text(json.dumps(state), encoding='utf-8')
    package.write_text(package.read_text(encoding='utf-8-sig').replace('Demo description', 'Edited description'), encoding='utf-8')
    claim = claim_thumbnail_ai_generation(w, package)
    assert claim.action == 'recover_new_chat'
    assert reserve_thumbnail_ai_automatic_retry(w, package, claim,
        previous_thread_id='same-video-chat', new_thread_id='same-video-chat',
        terminal_no_image=True, allow_same_conversation=True)
    after = read_thumbnail_ai_state(w)
    assert after['thread_id'] == 'same-video-chat'
    assert after['automatic_retry_count'] == 2


def test_measured_duration_repairs_have_separate_durable_limit(tmp_path):
    runner = seed(tmp_path, '[Nội dung kịch bản PART 1]')
    runner.content_repair_attempts = 3
    runner.minimum_characters = 3500
    runner.narration_repair_reason = narration_duration_problem(180, 10)
    runner._save_checkpoint()
    args = dict(cancel_event=threading.Event(), on_turn=None, on_partial=None, on_status=None)
    for expected in (1, 2):
        chat = Chat(['[[part-1]]' + VOICE + '[[end-part-1]]'])
        runner = MasterPromptRunner(chat, output_dir=tmp_path, project_name='video', minutes=10)
        loaded = runner._load_checkpoint(MASTER)
        assert loaded is not None
        runner.turns = loaded[0]
        runner._request_content_repair(ContentArtifacts('', '', '', '', '', ()), **args)
        state = json.loads(runner.checkpoint_path.read_text(encoding='utf-8'))
        assert state['content_repair_attempts'] == 3
        assert state['narration_duration_repair_attempts'] == expected
        assert state['narration_repair_reason'].startswith('NARRATION_CONTENT_INCOMPLETE:')
        assert 'Thời lượng yêu cầu: 10 phút' in chat.sent[0]
        assert '3500 ký tự' in chat.sent[0]
    with pytest.raises(ContentAIError, match='hết số lần'):
        runner._request_content_repair(ContentArtifacts('', '', '', '', '', ()), **args)
    assert len(chat.sent) == 1


def test_metadata_gap_cannot_use_measured_duration_budget(tmp_path):
    runner = seed(tmp_path, VOICE)
    runner.content_repair_attempts = 3
    runner.minimum_characters = 1600
    runner.narration_repair_reason = narration_duration_problem(180, 10)
    with pytest.raises(ContentAIError, match='hết số lần'):
        runner._request_content_repair(ContentArtifacts('', '', '', VOICE, '', ()),
            cancel_event=threading.Event(), on_turn=None, on_partial=None, on_status=None)
    assert runner.narration_duration_repair_attempts == 0
