import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from docurandom import research_upgrade as library
from docurandom.channel_learning import production_context
from docurandom.content_ai import (
    ChatTurn, ContentArtifacts, MasterPromptRunner, expected_image_prompt_count,
    extract_voicevox_script_from_package, format_content_package, content_package_problems,
)
from docurandom.thumbnail_design import design_spec
from tests.test_content_ai import FakeChat


DOMAINS = tuple(library.DOMAIN_PROFILES)
OBJECTS = {'G-13': ('工作機械', 'machine tool inspection'), 'G-10': ('鉄道保守', 'railway depot inspection'),
           'G-09': ('電池劣化', 'vehicle battery thermal inspection'), 'G-12': ('半導体素材', 'wafer surface inspection'),
           'G-11': ('物流の引き継ぎ', 'airport baggage handoff'), 'G-16': ('部品の認定', 'bearing supplier qualification')}
OBJECTS.update({key: (row['test_subject'], row['test_scene']) for key, row in library.DOMAIN_PROFILES.items() if key.startswith('R-')})


def workspace(root):
    state = root / '.docurandom'
    state.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(state_dir=state, root_dir=root, name='Test')


@pytest.mark.parametrize('domain', DOMAINS)
def test_master_can_drive_existing_content_transport_and_downstream_parsers(tmp_path, domain):
    """Scripted provider; real runner/receipt/package/voice/thumbnail/image parsing."""
    master = library.load_master(domain)
    assert expected_image_prompt_count(master, 10) == 20
    assert expected_image_prompt_count(master, 2.25) == 5
    assert library.master_manifest(master)['domain'] == domain
    noun, scene = OBJECTS[domain]
    title = f'【技術比較】{noun}で見落とされた条件とは!?'
    voice = '\n'.join(f'記録の第{i}項を確認します。\n条件をそろえて結果を比べます。' for i in range(1, 12))
    headlines = [noun+'を見直す', '記録に残った違い', '同じ条件なのか!?']
    thumb = ('CHỮ THUMBNAIL\n' + '\n'.join(f'{label}: {text}' for label, text in zip(('VÀNG','CYAN','ĐỎ'), headlines))
             + '\nMICRO 1: 条件\nMICRO 2: 記録\nPROMPT THUMBNAIL HOÀN CHỈNH\nCreate exactly 1280x720, 16:9, '+scene+'. '
             + '; '.join(f'{ordinal} line 「{text}」 in {color}' for ordinal, text, color in zip(
                 ('first','second','third'), headlines, ('yellow','cyan','red'))))
    description = noun+'について条件と記録を比べます。技術原理を説明する再構成です。\n00:00 条件をそろえる'
    images = (f'Documentary illustration of {scene}, a wide establishing view in Japan.',
              f'Documentary illustration of {scene}, close view of the relevant physical evidence in Japan.')
    context = {'domain': domain, 'situation': noun, 'references': [], 'factual_claims': [], 'reconstruction': True}
    selection = f'[[chu-de-con-duoc-chon]]{noun}[[end-chu-de-con-duoc-chon]]\n'
    selection += f'[[tieu-de-duoc-chon]]{title}[[end-tieu-de-duoc-chon]]\n'
    selection += '[[story-context]]'+json.dumps(context,ensure_ascii=False)+'[[end-story-context]]\n'
    selection += '[[part-1]]'+voice+'[[end-part-1]]\n[[CONTINUE]]'
    metadata = f'[[mo-ta]]{description}[[end-mo-ta]]\n[[prompt-thumbnail]]{thumb}[[end-prompt-thumbnail]]'
    artifacts = ContentArtifacts(title, description, thumb, voice,
        '\n'.join('A: '+line for line in voice.splitlines()), images, noun, ('技術比較', noun))
    responses = ['受領しました。','ĐÃ NẠP', selection, metadata,
                 '[[prompt-image-1]]Image 1. '+images[0]+'[[end-prompt-image-1]]', 'CHƯA XONG',
                 '[[prompt-image-2]]Image 2. '+images[1]+'[[end-prompt-image-2]]', 'ĐÃ XONG',
                 format_content_package(artifacts)]
    class ContractChat(FakeChat):
        def send(self, prompt, **kwargs):
            if prompt.startswith('BẮT ĐẦU'):
                from docurandom.output_contract import REQUEST_BEGIN, REQUEST_END
                packet=prompt.split(REQUEST_BEGIN+'\n',1)[1].split('\n'+REQUEST_END,1)[0]
                self.responses[0]='[[production-contract]]'+packet+'[[end-production-contract]]\n'+self.responses[0]
            return super().send(prompt, **kwargs)
    chat = ContractChat(responses)
    channel_context='\n\n[DOCURANDOM_CHANNEL_CONTEXT_DATA]\nActual matching-domain history\n[/DOCURANDOM_CHANNEL_CONTEXT_DATA]'
    result = MasterPromptRunner(chat, output_dir=tmp_path/'Output', project_name='video', minutes=1,
                                minimum_characters=150, production_context=channel_context).run(master)
    assert not chat.responses
    assert result.artifacts.title == title and result.artifacts.image_prompts == images
    assert content_package_problems(result.package_path) == {}
    assert extract_voicevox_script_from_package(result.package_path.read_text(encoding='utf-8-sig')) == voice
    assert all(len(line) <= 40 for line in voice.splitlines())
    assert [r['text'] for r in design_spec(result.artifacts.thumbnail_prompt)['headlines']] == headlines
    assert design_spec(result.artifacts.thumbnail_prompt)['micro_labels'] == ['条件','記録']
    w = workspace(tmp_path)
    library.record_story_context(w, result)  # Manual TXT also records context.
    saved = json.loads(next((w.state_dir/'research').glob('*-context.json')).read_text(encoding='utf-8'))
    assert saved['brief']['domain'] == domain
    assert saved['factual_labels'] == 'model_reported_not_independently_verified'
    # A changed next-run selection must not invalidate this completed conversation.
    library.configure_master(w, 'G-11' if domain != 'G-11' else 'G-13')
    recovered_chat = FakeChat([])
    recovered = MasterPromptRunner(recovered_chat, output_dir=tmp_path/'Output', project_name='video', minutes=1,
                                   minimum_characters=150).run(library.master_for_run(w, master, resume=True))
    assert recovered.package_path.read_bytes() == result.package_path.read_bytes()
    assert recovered_chat.prompts == []
    from docurandom.content_ai import load_completed_content_checkpoint
    assert load_completed_content_checkpoint(output_dir=tmp_path/'Output',project_name='video',minutes=1,
                                              master_prompt=master,minimum_characters=150) is not None


def test_selection_does_not_require_analytics_or_modify_existing_run(tmp_path):
    w=workspace(tmp_path)
    for name in ('pipeline_checkpoint.json','deferred_public_queue.json','settings.json'):
        (w.state_dir/name).write_text('{"protected":true}',encoding='utf-8')
    draft=tmp_path/'Prompts/MASTER_PROMPT_CURRENT.txt';draft.parent.mkdir();draft.write_text('OLD MASTER')
    originals={p:p.read_bytes() for p in [draft,*w.state_dir.glob('*.json')]}
    library.configure_master(w,'G-12')
    assert library.master_manifest(library.canonical_master(w))['domain']=='G-12'
    assert all(p.read_bytes()==raw for p,raw in originals.items())
    library.configure_master(w,None)
    assert library.canonical_master(w) is None
    assert library.master_for_run(w,'MANUAL',resume=False)=='MANUAL'


def test_archived_master_is_byte_identical_and_available():
    text=library.load_master('G-13','11.0.0-r1')
    assert hashlib.sha256(text.encode()).hexdigest()=='db6f5c50e6862862e552e5cc9254c4772f0b510d738b630e391fcb9c889b9952'


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_research_ids_keep_machine_and_thumbnail_gates_for_manual_txt(tmp_path, newline):
    from docurandom import output_contract
    from docurandom.thumbnail_contract import contract
    w=workspace(tmp_path)
    for domain in DOMAINS:
        master=library.load_master(domain).replace('\r\n','\n').replace('\n',newline)
        assert library.master_manifest(master)['domain']==domain
        assert output_contract.identity(master,'a'*32,2)['domain']==domain
        draft=tmp_path/'Prompts/MASTER_PROMPT_CURRENT.txt';draft.parent.mkdir(exist_ok=True)
        draft.write_bytes(master.encode())
        assert contract(w.state_dir)['line_colors']==['yellow','cyan','red']


def test_twenty_standalone_masters_cover_distinct_authored_domains():
    import re
    source=library.assets_root()/'source'
    hashes=set()
    assert len(DOMAINS)==20
    assert sum(domain.startswith('R-')for domain in DOMAINS)==14
    for domain in DOMAINS:
        module=(source/(domain.replace('-','')+'.txt')).read_text(encoding='utf-8')
        assert len(re.findall(r'(?m)^\d+\. ',module))==10
        assert domain in module.splitlines()[0]
        assert all(section in module for section in ('VẬT CHỨNG','THUẬT NGỮ','BIÊN NGÁCH'))
        assert '@DOMAIN_' not in library.load_master(domain)
        hashes.add(hashlib.sha256(module.encode()).hexdigest())
    assert len(hashes)==20


def test_corrupt_or_unknown_catalog_cannot_replace_a_resume(tmp_path):
    w=workspace(tmp_path);library.configure_master(w,'G-10')
    with patch.object(library,'load_master',side_effect=ValueError('corrupt catalog')):
        assert library.master_for_run(w,'ORIGINAL',resume=True)=='ORIGINAL'
        with pytest.raises(ValueError,match='corrupt'):
            library.master_for_run(w,'ORIGINAL',resume=False)
    with pytest.raises(ValueError,match='Unknown'):
        library.configure_master(w,'G-99')
    with pytest.raises(ValueError,match='not available'):
        library.load_master('G-10','2099.0')


@pytest.mark.parametrize('damage',['hash','path','domain'])
def test_catalog_tampering_is_rejected(tmp_path,damage):
    src=library.assets_root();target=tmp_path/'library';target.mkdir()
    catalog=json.loads((src/'manifest.json').read_text(encoding='utf-8'))
    entry=catalog['masters']['G-10'];payload=(src/entry['file']).read_bytes()
    (target/entry['file']).write_bytes(payload)
    if damage=='hash':entry['sha256']='0'*64
    if damage=='path':entry['file']='../outside.txt'
    if damage=='domain':
        entry['file']=catalog['masters']['G-13']['file'];entry['sha256']=catalog['masters']['G-13']['sha256']
        (target/entry['file']).write_bytes((src/entry['file']).read_bytes())
    (target/'manifest.json').write_text(json.dumps(catalog),encoding='utf-8')
    with patch.object(library,'assets_root',return_value=target),pytest.raises(ValueError):
        library.load_master('G-10')


def test_channel_lessons_never_cross_editorial_domains(tmp_path):
    w=workspace(tmp_path)
    (w.state_dir/'learning-policy.json').write_text(json.dumps({'schema_version':1,'enabled':True,
        'channel_id':'UCnWW9-QkcA773UV8v0xPqvw','research_upgrade':{'enabled':True,'domain':'G-13'}}),encoding='utf-8')
    with patch('docurandom.channel_learning.ChannelMemory') as memory:
        memory.return_value.context.return_value='ACTUAL G13 CHANNEL MEMORY'
        assert 'ACTUAL G13' in production_context(w,resume=False,domain='G-13')
        memory.reset_mock()
        assert production_context(w,resume=False,domain='G-10')==''
        assert production_context(w,resume=True,domain='G-13')==''
        memory.assert_not_called()


def test_pinned_canary_version_remains_until_explicit_upgrade(tmp_path):
    w=workspace(tmp_path)
    (w.state_dir/'learning-policy.json').write_text(json.dumps({'schema_version':1,'enabled':True,
        'channel_id':'UCnWW9-QkcA773UV8v0xPqvw','research_upgrade':{
            'enabled':True,'domain':'G-13','master_version':'11.0.0-r1'}}),encoding='utf-8')
    assert library.master_manifest(library.canonical_master(w))['version']=='11.0.0-r1'
    library.configure_master(w,'G-13')
    assert library.master_manifest(library.canonical_master(w))['version']=='11.1.0-r1'


def test_context_receipt_uses_last_video_context_and_ignores_next_run_preference(tmp_path):
    w=workspace(tmp_path);package=tmp_path/'package.txt';package.write_text('existing package')
    (w.state_dir/'production-master.json').write_text('broken next-run preference')
    block=lambda domain:'[[story-context]]'+json.dumps({'domain':domain})+'[[end-story-context]]'
    turns=[ChatTurn('user',library.load_master('G-13')),ChatTurn('assistant',block('not-a-video')),
           ChatTurn('user','BẮT ĐẦU'),ChatTurn('assistant',block('G-10')),
           ChatTurn('user','Sửa bối cảnh'),ChatTurn('assistant',block('G-13'))]
    library.record_story_context(w,SimpleNamespace(turns=turns,package_path=package,thread_id='same'))
    receipt=json.loads(next((w.state_dir/'research').glob('*context.json')).read_text(encoding='utf-8'))
    assert receipt['brief']['domain']=='G-13'


def test_native_selector_saves_next_run_without_touching_current_draft(tmp_path):
    import tkinter as tk
    from tkinter import ttk
    root=tk.Tk();root.withdraw()
    w=workspace(tmp_path);draft=tmp_path/'current.txt';draft.write_text('unchanged')
    selected=[]
    try:
        dialog=library.show_master_selector(root,w,selected.append);dialog.withdraw()
        children=dialog.winfo_children()[0].winfo_children()
        combo=next(c for c in children if isinstance(c,ttk.Combobox))
        assert len(combo['values'])==21
        combo.set(next(v for v in combo['values'] if str(v).startswith('R-B3')))
        next(c for c in children if isinstance(c,ttk.Button)).invoke()
        assert library.master_manifest(library.canonical_master(w))['domain']=='R-B3'
        assert draft.read_text()=='unchanged' and 'R-B3' in selected[0]
    finally:
        root.destroy()


@pytest.mark.parametrize('with_workspace', [False, True])
def test_full_native_workspace_window_builds_master_selection_button(tmp_path, monkeypatch, with_workspace):
    """Construct the real window in a fresh interpreter, like actual startup."""
    import subprocess
    import sys
    monkeypatch.setenv('DOCURANDOM_HOME', str(tmp_path/'portable'))
    script='''
import sys
import tkinter as tk
from tkinter import ttk
from docurandom.ui import DocuRandomApp
from docurandom.workspace import Workspace
w=Workspace('fixture','Fixture',sys.argv[1],'2026-10-24','2026-10-24')if sys.argv[2]=='yes'else None
root=tk.Tk();root.withdraw();app=None
try:
    app=DocuRandomApp(root,workspace=w)
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)
    button=next(widget for widget in descendants(root)
                if isinstance(widget,ttk.Button)and widget.cget('text')=='Chọn Master theo ngách…')
    assert str(button.cget('state'))==('normal'if w is not None else'disabled')
finally:
    for identifier in root.tk.call('after','info'):root.after_cancel(identifier)
    root.destroy()
    if app is not None:
        app.render_pool.shutdown(wait=False)
        app.flow_manager.stop()
'''
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path/'workspace'),'yes'if with_workspace else'no'],
                          cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


def test_new_master_thumbnail_gate_does_not_depend_on_analytics_or_next_run_selection(tmp_path):
    from docurandom.thumbnail_contract import contract
    w=workspace(tmp_path)
    library.configure_master(w,'G-10')
    assert contract(w.state_dir)=={}  # Does not change an old video's receipt.
    draft=tmp_path/'Prompts/MASTER_PROMPT_CURRENT.txt';draft.parent.mkdir()
    draft.write_text('LEGACY MASTER',encoding='utf-8')
    assert contract(w.state_dir)=={}
    draft.write_text(library.load_master('G-10'),encoding='utf-8')
    assert contract(w.state_dir)['line_colors']==['yellow','cyan','red']
    library.configure_master(w,None)
    assert contract(w.state_dir)['source']=='active-production-master-11.1.0-r1'
