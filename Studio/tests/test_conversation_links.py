import json
from docurandom.conversation_links import saved_conversation_url,retain_thumbnail_link
from docurandom.workspace import Workspace

def test_url_survives_pruned_bridge_map_and_is_bound_to_package(tmp_path):
    bridge=tmp_path/'bridge-state.json';bridge.write_text('{"savedTurns":{}}')
    url='https://chatgpt.com/c/6aceaf4f-44cc-83ec-a402-61fadf210822'
    bridge.with_name('conversation-links.jsonl').write_text(json.dumps({'thread_id':'thumbnail-thread','url':url})+'\n')
    assert saved_conversation_url(bridge,'thumbnail-thread')==url
    assert saved_conversation_url(bridge,'other-video')==''
    workspace=Workspace('w','Demo',str(tmp_path/'workspace'),'','');workspace.ensure_layout()
    state={'thread_id':'thumbnail-thread','package_path':'video.txt','package_sha256':'video-hash','prompt_sha256':'prompt-hash','updated_at':'now'}
    retain_thumbnail_link(workspace,state,bridge);retain_thumbnail_link(workspace,state,bridge)
    records=json.loads((workspace.state_dir/'thumbnail-conversations.json').read_text())['records']
    assert len(records)==1 and records[0]['package_sha256']=='video-hash'
    assert state['conversation_url']==url
