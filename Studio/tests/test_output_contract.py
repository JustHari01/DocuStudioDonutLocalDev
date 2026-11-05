from dataclasses import replace
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from docurandom import output_contract as contract
from docurandom.content_ai import (ChatTurn, ContentArtifacts, ContentAIError, MasterPromptRunner,
                                   _master_message_matches, format_content_package)
from docurandom.research_upgrade import load_master
from tests.test_content_ai import FakeChat


def sample():
    master=load_master('G-13')
    identity=contract.identity(master,'a'*32,2)
    title='【技術比較】工作機械の記録を確かめる'
    voice='同じ条件で記録を確かめます。\n測定方法をそろえて比べます。'
    thumb=('VÀNG: 日本の技術\nCYAN: 記録を比較\nĐỎ: 条件は同じ!?\nMICRO 1: 条件\nMICRO 2: 記録\n'
           'first line 「日本の技術」 in yellow; second line 「記録を比較」 in cyan; third line 「条件は同じ!?」 in red')
    artifacts=ContentArtifacts(title,'測定方法を比較します。\n00:00 条件',thumb,voice,
        '\n'.join('A: '+line for line in voice.splitlines()),('Factory inspection in Japan.','Closeup of measurement in Japan.'),'測定',('工作機械',))
    context={'domain':'G-13','references':[],'factual_claims':[]}
    text=('[[production-contract]]'+json.dumps(identity)+'[[end-production-contract]]'
          '[[tieu-de-duoc-chon]]'+title+'[[end-tieu-de-duoc-chon]]'
          '[[story-context]]'+json.dumps(context)+'[[end-story-context]]')
    turns=[ChatTurn('user',master),ChatTurn('assistant','受領しました。'),ChatTurn('user','BẮT ĐẦU'),ChatTurn('assistant',text)]
    return master,identity,artifacts,turns


@pytest.mark.parametrize('field,value',[('request_id','b'*32),('domain','G-10'),('master_sha256','0'*64),
                                       ('expected_images',3),('schema_version',True)])
def test_identity_is_owned_by_tool_not_the_model(field,value):
    _,expected,artifacts,turns=sample();bad=dict(expected);bad[field]=value
    turns.append(ChatTurn('assistant','[[production-contract]]'+json.dumps(bad)+'[[end-production-contract]]'))
    assert 'identity' in contract.problems(expected,artifacts,turns)


@pytest.mark.parametrize('field,value,key',[
    ('title','別の動画','title'),('voicevox_script','確認します。'*15,'voice'),
    ('voicevox_script','[narration here]','voice'),('image_prompts',('one',),'images'),
    ('seo_tags',(),'seo_tags'),('description','01:00 導入','description'),
    ('thumbnail_prompt','VÀNG: 日本の技術','thumbnail')])
def test_shape_gates_do_not_accept_done_or_a_near_complete_package(field,value,key):
    _,expected,artifacts,turns=sample()
    assert key in contract.problems(expected,replace(artifacts,**{field:value}),turns)


def test_story_domain_and_unbacked_verified_claim_are_not_accepted():
    _,expected,artifacts,turns=sample()
    for context in [{'domain':'G-10','references':[],'factual_claims':[]},
                    {'domain':'G-13','references':[],'factual_claims':[{'claim':'made up','status':'verified'}]}]:
        changed=turns+[ChatTurn('assistant','[[story-context]]'+json.dumps(context)+'[[end-story-context]]')]
        assert 'story_context' in contract.problems(expected,artifacts,changed)


def test_repair_cannot_reselect_the_winner_to_hide_a_title_mismatch():
    _,expected,artifacts,turns=sample()
    turns += [ChatTurn('user','KHÔI PHỤC PHẦN NỘI DUNG CÒN THIẾU — DOCURANDOM\n'+contract.REPAIR_HEADING),
              ChatTurn('assistant','[[tieu-de-duoc-chon]]別の動画[[end-tieu-de-duoc-chon]]')]
    assert 'title' in contract.problems(expected,replace(artifacts,title='別の動画'),turns)


def test_machine_repair_preserves_good_data_and_spends_one_persistent_attempt(tmp_path):
    master,expected,artifacts,turns=sample()
    # The provider fixes the identity but accidentally changes a valid description.
    wrong_packet=dict(expected);wrong_packet['request_id']='b'*32
    turns[-1]=ChatTurn('assistant',turns[-1].text.replace(json.dumps(expected),json.dumps(wrong_packet)))
    reply='[[production-contract]]'+json.dumps(expected)+'[[end-production-contract]]\n'
    reply+=format_content_package(replace(artifacts,description='別の記述\n00:00 条件'))
    runner=MasterPromptRunner(FakeChat([reply]),output_dir=tmp_path,project_name='video',minutes=1)
    runner.expected_images=2
    runner.output_contract_id=expected['request_id'];runner.turns=list(turns)
    result=runner._enforce_output_contract(artifacts,master,threading.Event(),on_turn=None,on_partial=None,on_status=None)
    assert result.description==artifacts.description and result.voicevox_script==artifacts.voicevox_script
    assert runner.content_repair_attempts==1
    stored=json.loads(runner.checkpoint_path.read_text(encoding='utf-8-sig'))
    assert stored['content_repair_attempts']==1 and stored['output_contract_id']==expected['request_id']


def test_exhausted_budget_never_sends_and_remains_exhausted_after_restart(tmp_path):
    master,expected,artifacts,turns=sample()
    bad=replace(artifacts,voicevox_script='確認します。'*15)
    runner=MasterPromptRunner(FakeChat([]),output_dir=tmp_path,project_name='video',minutes=1)
    runner.expected_images=2
    runner.output_contract_id=expected['request_id'];runner.turns=list(turns);runner.content_repair_attempts=3
    runner.master_prompt_sha256=expected['master_sha256'];runner._save_checkpoint()
    for i in range(2):
        if i:
            runner=MasterPromptRunner(FakeChat([]),output_dir=tmp_path,project_name='video',minutes=1)
            runner._load_checkpoint(master)
        with pytest.raises(ContentAIError,match='hết ngân sách'):
            runner._enforce_output_contract(bad,master,threading.Event(),on_turn=None,on_partial=None,on_status=None)
        assert runner.chat.prompts==[] and runner.content_repair_attempts==3


def test_legacy_contract_and_explicit_analytics_suffix_preserve_protocol():
    assert contract.identity('OLD MASTER','',2) is None
    assert contract.problems(None,Mock(),[])=={}
    master='EXACT MASTER\n'
    data='\n\n[DOCURANDOM_CHANNEL_CONTEXT_DATA]\nold evidence\n[/DOCURANDOM_CHANNEL_CONTEXT_DATA]'
    assert _master_message_matches(master+data,master)
    assert not _master_message_matches(master+'anything else'+data,master)
    assert not _master_message_matches(master+data+'extra instructions',master)
    assert not _master_message_matches('A DIFFERENT MASTER'+data,master)


def test_micro_labels_are_not_duplicated_by_translation_and_repeated_design_text():
    from docurandom.thumbnail_design import extract_micro_labels
    prompt=('CHỮ THUMBNAIL\nMICRO 1: 固定中\nMICRO 2: 取り外し後\n'
            'DỊCH NGHĨA\nMicro 1: Khi đang kẹp.\nMicro 2: Sau khi tháo.\n'
            'BỐ CỤC\nPROMPT THUMBNAIL HOÀN CHỈNH\n'
            'MICRO 1: 「固定中」 placed close to the object.\nMICRO 2: 「取り外し後」 on the right.')
    assert extract_micro_labels(prompt)==['固定中','取り外し後']
    with pytest.raises(ValueError,match='conflicting'):
        extract_micro_labels(prompt.replace('「固定中」 placed','「別の文字」 placed'))


def test_changed_master_cannot_make_a_saved_turn_start_a_new_request(tmp_path):
    import hashlib
    old=MasterPromptRunner(FakeChat([]),output_dir=tmp_path,project_name='video',minutes=1)
    old.master_prompt_sha256=hashlib.sha256(b'ORIGINAL').hexdigest()
    old.turns=[ChatTurn('user','ORIGINAL')];old._save_checkpoint()
    resumed=MasterPromptRunner(FakeChat([]),output_dir=tmp_path,project_name='video',minutes=1)
    with pytest.raises(ContentAIError,match='Master đã đổi'):
        resumed.run('CHANGED')
    assert resumed.chat.prompts==[]
