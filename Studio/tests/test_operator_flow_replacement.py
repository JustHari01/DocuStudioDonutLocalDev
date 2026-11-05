import hashlib,json
from pathlib import Path
import pytest
from docurandom.flow_agent import _operator_flow_replacement_valid
from docurandom.flow_agent import _legacy_flow_checkpoint_proves_all_prompts_unsent

@pytest.mark.parametrize('change',[None,'used','foreign','edited','path','image','prompt'])
def test_one_use_replacement_is_bound_to_exact_legacy_receipt(tmp_path: Path,change):
 state=tmp_path/'.docurandom';state.mkdir();journal=state/'repair-journal';journal.mkdir()
 old=journal/'old-flow.json';old.write_text('{"unresolved":"original receipt"}')
 cp={'run_id':'run','signature':'signature','entries':{},'operator_project_replacement':{
  'authorization':'replace-missing-project-once','workspace_id':'workspace','run_id':'run','signature':'signature',
  'prompt_sha256s':[hashlib.sha256(b'prompt').hexdigest()], 'retired_checkpoint':str(old),
  'retired_sha256':hashlib.sha256(old.read_bytes()).hexdigest()}}
 if change=='used':cp['operator_project_replacement']['consumed_at']=1
 if change=='foreign':cp['operator_project_replacement']['workspace_id']='another'
 if change=='edited':old.write_text('changed')
 if change=='path':cp['operator_project_replacement']['retired_checkpoint']=str(tmp_path/'outside')
 if change=='image':cp['entries']={'1':{'filename':'retained.png'}}
 if change=='prompt':cp['operator_project_replacement']['prompt_sha256s']=['other']
 assert _operator_flow_replacement_valid(cp,state/'flow_history.json',('prompt',),'workspace')==(change is None)

@pytest.mark.parametrize('status',['not-dispatched','not-submitted','attempting','uncertain','submitted'])
def test_dispatch_journal_distinguishes_untouched_tail_from_uncertain_send(status):
 prompts=['first','untouched']
 cp={'workspace_id':'workspace','run_id':'run','total':2,'submitted':0,'completed':0,'entries':{},'failures':{},
     'dispatch_journal':{'workspace_id':'workspace','run_id':'run','prompts':{
       str(i):{'prompt_sha256':hashlib.sha256(p.encode()).hexdigest(),'status':status if i==1 else 'not-dispatched'}
       for i,p in enumerate(prompts,1)}}}
 assert _legacy_flow_checkpoint_proves_all_prompts_unsent(cp,prompts,'workspace')==(status in ['not-dispatched','not-submitted'])
 cp['dispatch_journal']['run_id']='foreign'
 assert not _legacy_flow_checkpoint_proves_all_prompts_unsent(cp,prompts,'workspace')
