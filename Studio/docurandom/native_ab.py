"""Official title-only A/B protocol. Never substitutes sequential title changes."""
import hashlib,json,re,time
from pathlib import Path
from .channel_learning import load_policy,now_iso
from .storage import atomic_write_json
from .studio_inspection import StudioInspector


def click(cdp,node):
    box=cdp.command('DOM.getBoxModel',{'backendNodeId':node['backendDOMNodeId']},timeout=5)['model']['content']
    x,y=sum(box[0::2])/4,sum(box[1::2])/4
    for typ in ('mousePressed','mouseReleased'):
        cdp.command('Input.dispatchMouseEvent',{'type':typ,'x':x,'y':y,'button':'left','clickCount':1},timeout=5)


def nodes(cdp):return cdp.command('Accessibility.getFullAXTree',timeout=5).get('nodes',[])


def exact(ns,labels,roles=('button',)):
    found=[n for n in ns if not n.get('ignored') and n.get('role',{}).get('value')in roles
           and str(n.get('name',{}).get('value','')).strip()in labels]
    if len(found)!=1:raise ValueError('Native A/B control not unambiguously available: '+str(labels))
    return found[0]


def run_native_ab(workspace,admission,proposal,*,endpoint,policy,coordinator=None,cancel=None):
    if not admission.get('eligible'):return {'status':'ineligible','admission':admission,'no_mutation':True}
    if not policy.get('native_ab_auto')or policy.get('analytics_paused'):return {'status':'paused','no_mutation':True}
    vid=admission['video_id'];titles=proposal['titles']
    if len(titles)!=3 or len(set(titles))!=3 or any(not isinstance(t,str)or not 1<=len(t)<=100 for t in titles):
        raise ValueError('Exactly three distinct valid Japanese titles required')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',vid):raise ValueError('Exact video ID required')
    root=workspace.state_dir/'analytics'/policy['channel_id']/'experiments';root.mkdir(parents=True,exist_ok=True)
    path=root/(vid+'.json');old=json.loads(path.read_text(encoding='utf-8'))if path.exists()else{}
    if old.get('status')in {'submitted','uncertain','running','completed'}:
        return old  # inspect an existing official test; never starts a second test after restart.
    for p in root.glob('*.json'):
        if p!=path and json.loads(p.read_text(encoding='utf-8')).get('status')in {'submitted','uncertain','running'}:
            raise ValueError('This channel already owns an experiment')
    record={'schema_version':1,'channel_id':policy['channel_id'],'video_id':vid,'mode':'title-only',
            'titles':titles,'baseline_title':titles[0],'evidence':admission,'reason':proposal['reason'],
            'status':'reserved','created_at':now_iso(),'proposal_chat_url':proposal.get('conversation_url','')}
    atomic_write_json(path,record)
    with StudioInspector(endpoint,policy['profile_id'],policy['channel_id'],coordinator=coordinator,cancel=cancel)as inspector:
        with inspector.page(f'https://studio.youtube.com/video/{vid}/edit')as(cdp,page,ns):
            current=[n for n in ns if n.get('role',{}).get('value')=='textbox'
                     and n.get('value',{}).get('value')==titles[0]]
            if len(current)!=1:
                record.update(status='operator_override',no_mutation=True);atomic_write_json(path,record);return record
            # An edit page alone does not prove profile ownership: only IDs from
            # the inspected channel's saved Content list may reach this stage.
            listing=json.loads((root.parent/'video-details/content-list.json').read_text(encoding='utf-8'))
            if vid not in {v['video_id']for v in listing['videos']}or listing['channel_id']!=policy['channel_id']:
                raise ValueError('A/B channel/video provenance mismatch')
            click(cdp,exact(ns,('Thử nghiệm A/B','A/B Testing','A/B testing','A/Bテスト')))
            if inspector.cancel.wait(1):raise InterruptedError('A/B cancelled before submission')
            ns=nodes(cdp)
            click(cdp,exact(ns,('Chỉ tiêu đề','Title only','タイトルのみ'),roles=('radio','button')))
            if inspector.cancel.wait(.5):raise InterruptedError('A/B cancelled before submission')
            ns=nodes(cdp)
            fields=[n for n in ns if n.get('role',{}).get('value')=='textbox'and not n.get('ignored')]
            # Exclude the unchanged Details title outside the modal.
            dialog_ids={n['nodeId']for n in ns if n.get('role',{}).get('value')=='dialog'}
            parents={n['nodeId']:n.get('parentId')for n in ns}
            def in_dialog(node):
                key=node['nodeId']
                for _ in range(30):
                    if key in dialog_ids:return True
                    key=parents.get(key)
                    if not key:return False
                return False
            fields=[n for n in fields if in_dialog(n)]
            if len(fields)!=3 or fields[0].get('value',{}).get('value')!=titles[0]:
                raise ValueError('Native A/B title fields/baseline not verified')
            for field,title in zip(fields[1:],titles[1:]):
                cdp.command('DOM.focus',{'backendNodeId':field['backendDOMNodeId']},timeout=5)
                cdp.command('Input.dispatchKeyEvent',{'type':'keyDown','key':'a','code':'KeyA','modifiers':2,'windowsVirtualKeyCode':65},timeout=5)
                cdp.command('Input.dispatchKeyEvent',{'type':'keyUp','key':'a','code':'KeyA','modifiers':2,'windowsVirtualKeyCode':65},timeout=5)
                cdp.command('Input.insertText',{'text':title},timeout=5)
            ns=nodes(cdp);actual={n.get('value',{}).get('value')for n in ns if n.get('role',{}).get('value')=='textbox'and in_dialog(n)}
            if not set(titles)<=actual:raise ValueError('A/B draft text readback mismatch')
            fresh=load_policy(workspace.state_dir)
            if not fresh.get('native_ab_auto')or fresh.get('analytics_paused'):return {'status':'paused_before_commit','no_mutation':True}
            done=exact(ns,('Xong','Hoàn tất','Done','完了'))
            record.update(status='submitted',submitted_at=now_iso());atomic_write_json(path,record)
            try:
                click(cdp,done)
                if inspector.cancel.wait(2):raise InterruptedError('A/B receipt pending')
                text='\n'.join(str(n.get('name',{}).get('value',''))for n in nodes(cdp))
                if not re.search(r'thử nghiệm.*đang|đang.*thử nghiệm|test.*in progress|running|テスト.*実施中',text,re.I):
                    raise ValueError('Official test start not confirmed; retain receipt')
                record.update(status='running',official_readback=text[:8000]);atomic_write_json(path,record)
            except Exception as exc:
                record.update(status='uncertain',detail=str(exc));atomic_write_json(path,record);raise
    return record


def optimize_eligible(workspace,*,endpoint,policy,coordinator=None,cancel=None,pool=None):
    root=workspace.state_dir/'analytics'/policy['channel_id'];summary=root/'video-details/summary.json'
    if not summary.exists()or policy.get('analytics_paused'):return
    report=json.loads(summary.read_text(encoding='utf-8'));candidates=[r for r in report['admissions']if r['eligible']]
    if not candidates:return {'status':'no_eligible_video','no_mutation':True}
    from .quality_chat import ask_json
    from .channel_learning import ChannelMemory
    admission=candidates[0];vid=admission['video_id'];page=json.loads((root/f'video-details/{vid}-edit.json').read_text(encoding='utf-8'))
    title_fields=[r['value']for r in page.get('form_fields',[])if any(w in r['label'].casefold()for w in ('tiêu đề','title','タイトル'))]
    if len(title_fields)!=1:raise ValueError('Exact A/B baseline title unavailable')
    baseline=title_fields[0]
    proposal=ask_json(root/'experiment-proposals'/vid,
        '[DOCURANDOM_STAGE=native-ab-title-proposal]\nPrepare two Japanese title variants for an official simultaneous title-only test. '
        'Keep all facts, subject, countries, numbers, Master topic and thumbnail promise. Do not change any existing video. '
        'Return JSON only {"variants":[string,string],"reason":string,"uncertain":boolean}. Each title <=100 characters. '
        'Do not infer winners from small or assisted traffic. Baseline: '+baseline+'\nEvidence and channel memory:\n'+ChannelMemory(workspace.state_dir,policy['channel_id']).context(),
        cancel=cancel,pool=pool)
    if proposal.get('uncertain')or len(proposal.get('variants',[]))!=2:return {'status':'proposal_uncertain','no_mutation':True}
    if not policy.get('native_ab_auto'):
        return {'status':'proposal_ready','video_id':vid,'no_mutation':True,'proposal':proposal}
    return run_native_ab(workspace,admission,dict(proposal,titles=[baseline]+proposal['variants']),endpoint=endpoint,
                         policy=policy,coordinator=coordinator,cancel=cancel)
