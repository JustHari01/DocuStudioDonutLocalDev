from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
import hashlib,threading
import pytest
from docurandom import google_workspace as g
from docurandom import google_drive as module
from docurandom.google_drive import drive_page,exact_file

def test_drive_matching_requires_one_id_and_no_active_transfer():
    row={'id':'a'*25,'text':'thumbnail.png Image'}
    assert exact_file({'items':[row,row]},'thumbnail.png')=='https://drive.google.com/file/d/'+'a'*25+'/view'
    assert exact_file({'items':[row],'upload_in_progress':True},'thumbnail.png')is None
    assert exact_file({'items':[row,{'id':'b'*25,'text':'thumbnail.png'}]},'thumbnail.png')is None
    with pytest.raises(g.GoogleBlocked):drive_page('https://evil.test/my-drive')

def prepared(tmp_path,monkeypatch,*,submitted=False):
    w=SimpleNamespace(state_dir=tmp_path,workspace_id='w',name='Test')
    bridge=Mock();runner=g.GoogleRunner(w,bridge,'profile',client=Mock())
    runner.cancel=SimpleNamespace(wait=lambda _:False,is_set=lambda:False)
    runner.session_id=Mock(return_value='session')
    runner.call=Mock(return_value={'tab_id':1,'window_id':2,'url':'https://drive.google.com/drive/u/0/my-drive','browser_session_id':'session'})
    runner.command=Mock(return_value={})
    runner.snapshot=Mock(return_value={'items':[{'id':'a'*25,'text':'thumbnail.png'}]})
    image=tmp_path/'image.png';image.write_bytes(b'owned exact image')
    step={'app':'drive','action':'upload','file':str(image),'sha256':hashlib.sha256(image.read_bytes()).hexdigest(),'filename':'thumbnail.png'}
    monkeypatch.setattr(g,'resolve_upload_source',lambda *_:image)
    sends=[]
    class Native:
        def __init__(self,*a):pass
        @contextmanager
        def session(self):yield self
        def submit(self,image,filename,before_input):
            before_input();sends.append(filename)
    monkeypatch.setattr(module,'GoogleDriveInput',Native)
    path=g.root(w)/'jobs'/('operation.json')
    if submitted:g.atomic_write_json(path,{'status':'submitted','native_file_input':True,'profile_id':'profile'})
    return runner,step,sends,path

def test_native_input_is_sent_once_and_completion_survives_restart(tmp_path,monkeypatch):
    runner,step,sends,path=prepared(tmp_path,monkeypatch)
    result=runner.run_step(step,'operation');assert result['status']=='complete'
    assert runner.run_step(step,'operation')==result
    assert sends==['thumbnail.png'];assert g.read(path)['native_file_input']is True
    runner.bridge.share_file.assert_not_called()

def test_submitted_native_input_only_observes_existing_result(tmp_path,monkeypatch):
    runner,step,sends,path=prepared(tmp_path,monkeypatch,submitted=True)
    assert runner.run_step(step,'operation')['status']=='complete'
    assert sends==[]
