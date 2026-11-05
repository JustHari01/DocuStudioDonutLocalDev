import json
from pathlib import Path
import urllib.request
import urllib.error
from docurandom.workspace_control import LocalWorkspaceControl

def test_authenticated_exact_commands_are_idempotent_and_cleanup_is_instance_owned(tmp_path: Path):
    calls=[]
    path=tmp_path/'control.json'
    service=LocalWorkspaceControl(path,lambda action,workspace:calls.append((action,workspace)) or 'accepted','test')
    descriptor=json.loads(path.read_text())
    def request(value, token=None):
        req=urllib.request.Request(descriptor['endpoint']+'/v1/workspace/command',data=json.dumps(value).encode(),
            headers={'Authorization':'Bearer '+(token or descriptor['token']),'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=3) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as exc:return exc.code,json.load(exc)
    body={'command':'continue','workspace_id':'a'*32,'request_id':'b'*32}
    try:
        assert request(body,'invalid')[0]==401
        assert not calls
        assert request(dict(body,command='upload'))[0]==400
        assert request(dict(body,workspace_id='all'))[0]==400
        assert request(body)[0]==200
        assert request(body)[0]==200
        assert calls==[('continue','a'*32)]
        assert request(dict(body,command='status'))[0]==409
        skip=dict(body,command='skip',request_id='c'*32)
        assert request(skip)[0]==200
        assert request(skip)[0]==200
        assert calls==[('continue','a'*32),('skip','a'*32)]
        descriptor['instance']='replacement-owner'
        path.write_text(json.dumps(descriptor))
    finally:service.stop()
    assert path.exists()

def test_normal_shutdown_removes_descriptor(tmp_path: Path):
    path=tmp_path/'control.json'
    service=LocalWorkspaceControl(path,lambda *_:'ok','test')
    service.stop()
    assert not path.exists()


def test_exact_thread_skip_dispatches_bound_identity_once(tmp_path: Path):
    calls=[]
    path=tmp_path/'control.json'
    service=LocalWorkspaceControl(path,lambda *args:calls.append(args) or 'accepted','test')
    descriptor=json.loads(path.read_text())
    body={'command':'skip','workspace_id':'a'*32,'request_id':'b'*32,
          'expected_thread_id':'thread_docurandom_old_failed_turn'}
    def request(value):
        req=urllib.request.Request(descriptor['endpoint']+'/v1/workspace/command',data=json.dumps(value).encode(),
            headers={'Authorization':'Bearer '+descriptor['token'],'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=3) as response:return response.status
        except urllib.error.HTTPError as exc:return exc.code
    try:
        assert request(dict(body,expected_thread_id=None))==400
        assert request(dict(body,expected_thread_id='bad/path'))==400
        assert request(dict(body,command='continue'))==400
        assert not calls
        assert request(body)==200 and request(body)==200
        assert calls==[('skip','a'*32,'thread_docurandom_old_failed_turn')]
        assert request(dict(body,expected_thread_id='thread_docurandom_replacement'))==409
        assert len(calls)==1
    finally:service.stop()
