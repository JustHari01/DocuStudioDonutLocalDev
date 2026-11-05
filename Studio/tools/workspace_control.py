"""Run an existing workspace command locally, without a Telegram message or UI automation."""
import argparse,json,os,sys,urllib.request,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from docurandom.storage import app_data_dir

parser=argparse.ArgumentParser()
parser.add_argument('command',choices=['status','errors','continue','skip'])
parser.add_argument('workspace')
parser.add_argument('--request-id',default=None)
parser.add_argument('--expected-thread',default=None)
args=parser.parse_args()
data=app_data_dir()
descriptor=json.loads((data/'runtime/workspace-control.json').read_text(encoding='utf-8'))
workspaces=json.loads((data/'workspaces.json').read_text(encoding='utf-8-sig'))['workspaces']
matches=[w for w in workspaces if args.workspace in (w['workspace_id'],w['name'])]
if len(matches)!=1:raise SystemExit('Choose one exact workspace name or ID.')
body={'command':args.command,'workspace_id':matches[0]['workspace_id'],'request_id':args.request_id or uuid.uuid4().hex}
if args.expected_thread:body['expected_thread_id']=args.expected_thread
request=urllib.request.Request(descriptor['endpoint']+'/v1/workspace/command',
    data=json.dumps(body).encode(),
    headers={'Authorization':'Bearer '+descriptor['token'],'Content-Type':'application/json'})
with urllib.request.urlopen(request,timeout=25) as response:value=json.load(response)
print(value['response'])
