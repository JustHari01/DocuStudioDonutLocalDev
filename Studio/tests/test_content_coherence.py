from dataclasses import replace
from docurandom.content_ai import (ContentArtifacts,ChatTurn,_extract_blocks,extract_content_artifacts,
    _artifact_problems,_fill_missing_content_artifacts,_content_repair_request,artifacts_complete,
    format_content_package,content_package_problems)
from docurandom.content_quality import thumbnail_prompt_problem,artifact_field_problems

def cotton():
    return ContentArtifacts('【ウズベキスタンの反応】日本製カード機','ウズベキスタンの紡績工場。',
        'Create a documentary thumbnail in an Uzbekistan cotton factory.',
        'ウズベキスタンの紡績工場で綿の繊維長を測定します。','',
        tuple('Uzbekistan cotton carding factory photograph '+str(i)for i in range(4)))

def test_prose_marker_cannot_capture_a_later_complete_block():
    text='Không thể khôi phục [[prompt-thumbnail]] và [[prompt-hinh-anh]].\n\n[[prompt-thumbnail]]Create the actual image[[end-prompt-thumbnail]]'
    assert _extract_blocks(text,'prompt-thumbnail')==['Create the actual image']
    turns=[ChatTurn('user','BẮT ĐẦU'),ChatTurn('assistant',text)]
    assert extract_content_artifacts(turns).thumbnail_prompt=='Create the actual image'

def test_same_length_foreign_image_batch_is_not_complete_and_can_be_replaced():
    good=cotton();bad=replace(good,thumbnail_prompt='Create a Costa Rican PET tray factory thumbnail.',
        image_prompts=tuple('Costa Rica PET thermoforming factory '+str(i)for i in range(4)))
    assert set(_artifact_problems(bad))=={'thumbnail','images'}
    assert not artifacts_complete(bad,1,expected_images=4)
    repaired=_fill_missing_content_artifacts(bad,good)
    assert repaired.title==bad.title and repaired.voicevox_script==bad.voicevox_script
    assert repaired.image_prompts==good.image_prompts
    assert artifacts_complete(repaired,1,expected_images=4)

def test_repair_carries_locked_story_even_when_provider_lost_the_chat():
    bad=replace(cotton(),thumbnail_prompt='Tôi chưa thể khôi phục chính xác hai khối [[prompt-thumbnail]]',image_prompts=())
    prompt=_content_repair_request(bad,4)
    assert bad.title in prompt and bad.voicevox_script in prompt
    assert '[[prompt-thumbnail]]prompt thumbnail[[end-prompt-thumbnail]]'in prompt
    assert thumbnail_prompt_problem(bad.thumbnail_prompt)

def test_generic_images_and_actual_comparisons_do_not_trigger_country_guard():
    a=cotton()
    assert not artifact_field_problems(a.title,a.description,a.voicevox_script,a.thumbnail_prompt,tuple('A Japanese industrial machine'for _ in range(4)))
    assert not artifact_field_problems(a.title,a.description,a.voicevox_script,a.thumbnail_prompt,tuple('Uzbekistan factory compares a Japanese machine to Costa Rica results'for _ in range(4)))

def test_upload_and_public_can_inspect_the_same_broken_package(tmp_path):
    package=tmp_path/'video.txt';a=replace(cotton(),image_prompts=tuple('Costa Rica PET tray line '+str(i)for i in range(4)))
    package.write_text(format_content_package(a),encoding='utf-8')
    assert 'images'in content_package_problems(package)

def test_quality_migration_repair_is_spent_once_and_stays_spent_after_restart(tmp_path):
    import threading
    import pytest
    from docurandom.content_ai import MasterPromptRunner,ContentAIError
    class Chat:
        thread_id='saved-video-thread'
        def send(self,*args,**kwargs):return 'repair response'
    bad=replace(cotton(),image_prompts=tuple('Costa Rica PET tray '+str(i)for i in range(4)))
    runner=MasterPromptRunner(Chat(),output_dir=tmp_path,project_name='video',minutes=1)
    runner.turns=[ChatTurn('user','MASTER'),ChatTurn('assistant','ĐÃ NẠP'),ChatTurn('user','BẮT ĐẦU'),ChatTurn('assistant','saved content')]
    runner.content_repair_attempts=3
    runner._request_content_repair(bad,cancel_event=threading.Event(),on_turn=None,on_partial=None,on_status=None)
    assert runner.content_repair_attempts==4
    restored=MasterPromptRunner(Chat(),output_dir=tmp_path,project_name='video',minutes=1)
    assert restored._load_checkpoint('MASTER')is not None
    assert restored.content_repair_attempts==4
    with pytest.raises(ContentAIError,match='hết số lần'):
        restored._request_content_repair(bad,cancel_event=threading.Event(),on_turn=None,on_partial=None,on_status=None)

def test_broken_legacy_run_repairs_only_visual_fields_in_same_chat(tmp_path):
    from docurandom.content_ai import MasterPromptRunner,CONTENT_REPAIR_PREFIX,IMAGE_COMPLETION_QUESTION
    good=cotton();bad=replace(good,thumbnail_prompt='Create Costa Rica tray factory.',
        image_prompts=tuple('Costa Rica PET tray factory '+str(i)for i in range(4)))
    normal=('[[tieu-de-duoc-chon]]'+good.title+'[[end-tieu-de-duoc-chon]]\n'
        '[[mo-ta]]'+good.description+'[[end-mo-ta]]\n[[part-1]]'+good.voicevox_script+'[[end-part-1]]')
    def visuals(a):return ('[[prompt-thumbnail]]'+a.thumbnail_prompt+'[[end-prompt-thumbnail]]\n[[prompt-hinh-anh]]\n'+
        '\n'.join('Image '+str(i)+'. '+p for i,p in enumerate(a.image_prompts,1))+'\n[[end-prompt-hinh-anh]]')
    class Chat:
        thread_id='same-video-chat'
        def __init__(self):self.responses=['CHƯA XONG',visuals(good),'ĐÃ XONG',format_content_package(good)];self.sent=[]
        def send(self,prompt,**kw):self.sent.append(prompt);return self.responses.pop(0)
    chat=Chat();seed=MasterPromptRunner(chat,output_dir=tmp_path,project_name='video',minutes=1,max_turns=4)
    seed.turns=[ChatTurn('user','MASTER'),ChatTurn('assistant','ĐÃ NẠP'),ChatTurn('user','BẮT ĐẦU'),ChatTurn('assistant',normal)]
    for _ in range(3):seed.turns += [ChatTurn('user','TIẾP'),ChatTurn('assistant','Đang tiếp tục.')]
    seed.turns += [ChatTurn('user',CONTENT_REPAIR_PREFIX),ChatTurn('assistant','Tôi chưa có [[prompt-thumbnail]] và [[prompt-hinh-anh]].'),
        ChatTurn('user',CONTENT_REPAIR_PREFIX),ChatTurn('assistant',visuals(bad)),ChatTurn('user',IMAGE_COMPLETION_QUESTION)]
    seed.content_repair_attempts=3;seed.pending_turn_transport_started=False;seed._save_checkpoint()
    result=MasterPromptRunner(chat,output_dir=tmp_path,project_name='video',minutes=1,max_turns=4).run('MASTER')
    assert result.artifacts.title==good.title and result.artifacts.voicevox_script==good.voicevox_script
    assert result.artifacts.image_prompts==good.image_prompts
    repairs=[p for p in chat.sent if p.startswith(CONTENT_REPAIR_PREFIX)]
    assert len(repairs)==1 and good.title in repairs[0]and good.voicevox_script in repairs[0]

def test_real_upload_and_public_gates_reject_cross_topic_materials(tmp_path):
    import pytest
    from PIL import Image
    from docurandom.youtube_upload import WorkspaceUploadSources,parse_content_package,_assert_complete_upload_assets,YouTubeUploadAssetError,YouTubeUploadError
    from docurandom.publication_assets import publication_package
    archive=tmp_path/'archive';archive.mkdir();package=archive/'video.txt'
    bad=replace(cotton(),seo_tags=('cotton','Japan'),image_prompts=tuple('Costa Rica PET tray '+str(i)for i in range(4)))
    package.write_text(format_content_package(bad),encoding='utf-8')
    thumb=archive/'thumb.png';Image.new('RGB',(1280,720)).save(thumb)
    sources=WorkspaceUploadSources(archive/'video.mp4',package,thumb,parse_content_package(package))
    with pytest.raises(YouTubeUploadAssetError,match='CONTENT_PACKAGE_INCONSISTENT'):_assert_complete_upload_assets(sources)
    with pytest.raises(YouTubeUploadError,match='CONTENT_PACKAGE_INCONSISTENT'):
        publication_package({'archive_dir':str(archive),'package_path':str(package)})
