"""One real, receipted ChatGPT thumbnail replacement for the authorized DaiTa14 pilot."""
import json
from pathlib import Path
import sys
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docurandom.workspace import WorkspaceStore
from docurandom.storage import atomic_write_json
from docurandom.content_ai import check_chatgpt_web_bridge, ChatGPTWebChat, resolve_chatgpt_web_model, CHATGPT_WEB_DEFAULT_MODEL_LABEL
from docurandom.thumbnail_ai import claim_thumbnail_ai_generation, read_thumbnail_ai_state, update_thumbnail_ai_state, stage_thumbnail_candidate, thumbnail_request_for_claim
from docurandom.thumbnail_contract import review_image, accepted
from docurandom.thumbnail_manager import install_workspace_thumbnail

w=WorkspaceStore().get('d3071873f0da46118fa34c5c7a8d2cb0')
assert w is not None and w.name=='DaiTa14'
policy_path=w.state_dir/'learning-policy.json';policy=json.loads(policy_path.read_text())
policy['thumbnail_contract']={'line_colors':['yellow','cyan','red'],'width':1280,'height':720,'source':'operator Master snapshot in AI checkpoint'}
atomic_write_json(policy_path,policy)
p=w.output_dir/'DaiTa14.txt'
content=json.loads((w.output_dir/'DaiTa14_AI_CHECKPOINT.json').read_text())['thread_id']
claim=claim_thumbnail_ai_generation(w,p,dedicated_chat=True,content_thread_id=content)
state=read_thumbnail_ai_state(w);print('CLAIM',claim.action,flush=True)
status=check_chatgpt_web_bridge(auto_start=False,timeout=10);assert status.operational
chat=ChatGPTWebChat(status.endpoint,resolve_chatgpt_web_model(CHATGPT_WEB_DEFAULT_MODEL_LABEL,status.available_models),w.root,
                    status.access_token,thread_id=state['thread_id'])
cancel=threading.Event();timer=threading.Timer(600,chat.cancel);timer.daemon=True;timer.start()
try:
    if claim.action=='reuse' and claim.existing_path and accepted(w,p,claim.existing_path):
        print('ALREADY_VERIFIED',claim.existing_path);sys.exit(0)
    if claim.action=='start':
        update_thumbnail_ai_state(w,p,claim,status='submitted',submitted=True,thread_id=chat.thread_id)
        image=chat.generate_image(thumbnail_request_for_claim(claim),cancel_event=cancel,on_status=lambda s,t:print(s,t[:200],flush=True))
        stage_thumbnail_candidate(w,claim,chat.thread_id,image.response_id,image.data)
        update_thumbnail_ai_state(w,p,claim,status='image_received',submitted=True,response_id=image.response_id)
        data=image.data
    else:
        from docurandom.thumbnail_ai import load_thumbnail_candidate
        candidate=load_thumbnail_candidate(w,claim,chat.thread_id)
        if candidate: data=candidate[1]
        else:
            image=chat.read_existing_generated_image(expected_prompt=thumbnail_request_for_claim(claim))
            if image is None:raise RuntimeError('No exact image recovered; no new submission')
            data=image.data
    payload=review_image(w,claim.prompt,data,cancel=cancel)
    installed=install_workspace_thumbnail(w,payload)
    update_thumbnail_ai_state(w,p,claim,status='complete',submitted=True,thumbnail_path=installed.path,
                              detail='Đã đối chiếu Master, kiểm ảnh và chuẩn hóa 1280x720; có biên nhận kiểm chứng.')
    assert accepted(w,p,installed.path)
    print('THUMBNAIL_CONTRACT_PASS',installed.path,installed.inspection.sha256,flush=True)
finally:timer.cancel()
