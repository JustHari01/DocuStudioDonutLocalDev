import hashlib,json
from unittest.mock import Mock,patch
from PIL import Image
import pytest
from docurandom.workspace import Workspace
from docurandom.thumbnail_contract import completed_thumbnail_selection
from docurandom.ui import DocuRandomApp

def prepared(tmp_path):
    w=Workspace('w','Test',str(tmp_path/'w'),'','');w.ensure_layout()
    package=w.output_dir/'Video.txt';package.write_text('exact package',encoding='utf-8')
    image=w.state_dir/'thumbnails/current.png';image.parent.mkdir()
    Image.new('RGB',(1280,720)).save(image)
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    rules={'line_colors':['yellow','cyan','red'],'width':1280,'height':720}
    (w.state_dir/'learning-policy.json').write_text(json.dumps({'schema_version':1,'enabled':True,'channel_id':'UC'+'a'*22,'thumbnail_contract':rules}))
    state={'status':'complete','workspace_id':w.workspace_id,'package_path':str(package),'package_sha256':sha(package),
           'prompt_sha256':hashlib.sha256(b'exact prompt').hexdigest(),'thumbnail_path':str(image),'automatic_retry_count':3}
    (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(state))
    review={'passed':True,'image_sha256':sha(image),'prompt_sha256':state['prompt_sha256'],'contract':rules}
    (w.state_dir/'thumbnail-contract').mkdir()
    (w.state_dir/'thumbnail-contract'/(sha(image)+'.json')).write_text(json.dumps(review))
    return w,package,image,state

def test_exact_completed_selection_restores_without_ai_or_budget_mutation(tmp_path):
    w,p,image,state=prepared(tmp_path);saved=(w.state_dir/'thumbnail-ai.json').read_bytes()
    app=DocuRandomApp.__new__(DocuRandomApp);app.workspace=w;app.last_content_package=p
    app._effective_youtube_thumbnail=Mock(return_value=None);app.youtube_thumbnail_var=Mock()
    app._save_config=Mock();app._refresh_thumbnail_status=Mock()
    with patch('docurandom.thumbnail_ai.extract_complete_thumbnail_prompt',return_value='exact prompt'),patch('docurandom.quality_chat.ask_json',side_effect=AssertionError('No AI')):
        assert not app._automatic_thumbnail_required_but_missing()
    app.youtube_thumbnail_var.set.assert_called_once_with(str(image.resolve()))
    app._save_config.assert_called_once()
    assert (w.state_dir/'thumbnail-ai.json').read_bytes()==saved

@pytest.mark.parametrize('bad',['pending','package','image','prompt','failed-review','other-workspace','external'])
def test_invalid_or_changed_selection_stays_blocked(tmp_path,bad):
    w,p,image,state=prepared(tmp_path)
    if bad=='pending':state['status']='confirmation_pending'
    if bad=='package':p.write_text('changed')
    if bad=='image':image.write_bytes(b'changed bytes')
    if bad=='prompt':state['prompt_sha256']='0'*64
    if bad=='other-workspace':state['workspace_id']='other'
    if bad=='external':state['thumbnail_path']=str(tmp_path/'outside.png')
    if bad=='failed-review':
        review=next((w.state_dir/'thumbnail-contract').glob('*.json'));value=json.loads(review.read_text());value['passed']=False;review.write_text(json.dumps(value))
    (w.state_dir/'thumbnail-ai.json').write_text(json.dumps(state))
    with patch('docurandom.thumbnail_ai.extract_complete_thumbnail_prompt',return_value='exact prompt'):
        assert completed_thumbnail_selection(w,p) is None
