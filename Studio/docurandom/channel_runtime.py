"""Low-priority learning jobs, durable before submission and safe across restart."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import threading

from .channel_learning import ANALYSIS_PROMPT, ChannelMemory, digest, load_policy, now_iso
from .channel_analytics import StudioAnalyticsReader
from .storage import app_data_dir, atomic_write_json

VIETNAM = timezone(timedelta(hours=7))
_LANE = threading.Lock()


def analysis_evidence(rows):
    """Deduplicate repeated UI availability labels; retain every metric and source."""
    latest={}
    for row in rows:latest.setdefault((row.get('video_id','channel'),row.get('report')),row)
    catalogue={};evidence=[]
    for raw in latest.values():
        row={k:v for k,v in raw.items()if k not in {'observations','raw_observations','content_context','channel_id','locale','engagement_provenance'}}
        # Null metrics remain explicitly unavailable in the referenced catalogue.
        row['metrics']={k:v for k,v in row.get('metrics',{}).items()if v is not None}
        url=row.get('source_url','')
        if url.startswith('https://studio.youtube.com/'):
            row['source_path']=url.removeprefix('https://studio.youtube.com');row.pop('source_url')
        availability=row.pop('availability',{})
        key=digest(availability)[:16];catalogue[key]=availability;row['availability_ref']=key
        evidence.append(row)
    fields=sorted({k for item in catalogue.values()for k in item})
    statuses=sorted({v for item in catalogue.values()for v in item.values()})
    packed={'fields':fields,'statuses':statuses,'missing_status':-1,
            'records':{key:[statuses.index(item[k])if k in item else -1 for k in fields]for key,item in catalogue.items()}}
    return evidence,packed


def supplied_evidence_ids(memory,request):
    """Include historical citations actually present in the supplied channel memory."""
    import re
    mentioned=set(re.findall(r'(?<![a-f0-9])[a-f0-9]{64}(?![a-f0-9])',request))
    with memory.connect()as db:known={r[0]for r in db.execute('SELECT id FROM snapshots')}
    return sorted(mentioned & known)


def analysis_request(memory, evidence, catalogue, job_id):
    """Keep full metrics in one immutable attachment when inline history grows."""
    import hashlib
    from .content_ai import ChatAttachment
    prefix = (ANALYSIS_PROMPT + '\nCHANNEL MEMORY:\n' + memory.context()
        + '\nAll reports belong to this channel. source_path is relative to https://studio.youtube.com. '
          'availability_ref resolves through catalogue fields/statuses. Missing data is not zero. '
          'Reports may include assisted traffic. Raw reports remain locally retained.')
    data = json.dumps({'availability_catalogue': catalogue, 'evidence': evidence}, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')
    inline = prefix + '\nAVAILABILITY CATALOGUE:\n' + json.dumps(catalogue,ensure_ascii=False,separators=(',',':'))
    inline += '\nEVIDENCE:\n' + json.dumps(evidence,ensure_ascii=False,separators=(',',':'))
    if len(inline.encode('utf-8')) <= 48 * 1024:
        return inline, ()
    # Attachment identity participates in the saved request. Do not truncate
    # metrics or prior lessons to make a daily request fit the inline budget.
    sha = hashlib.sha256(data).hexdigest()
    name = f'{job_id}-evidence-{sha[:16]}.json.txt'
    path = memory.root / name
    if path.exists() and path.read_bytes() != data:
        raise ValueError('Analytics attachment identity changed')
    path.write_bytes(data)
    request = (prefix + f'\nRead the complete attached JSON {name}; SHA256={sha}. '
        'It contains all metrics and their availability catalogue for the evidence IDs below. '
        'If the attachment is unavailable, report unavailable and do not infer its metrics. '
        'The following list is an identity index, not the metric values.\nEVIDENCE:\n'
        + json.dumps([{'evidence_id':r['evidence_id']} for r in evidence],separators=(',',':')))
    if len(request.encode('utf-8')) > 48 * 1024:
        raise RuntimeError('Channel memory itself needs scoped consolidation; evidence retained')
    return request, (ChatAttachment(name, data, 'text/plain'),)


def analyze(memory: ChannelMemory, *, weekly=False, cancel_event=None, pool=None, progress=None, revision='') -> dict:
    from .content_ai import ChatGPTWebChat, check_chatgpt_web_bridge, resolve_chatgpt_web_model, CHATGPT_WEB_DEFAULT_MODEL_LABEL
    from .conversation_links import saved_conversation_url
    from .quality_chat import exact_text_response
    cancel = cancel_event or threading.Event(); report = progress or (lambda _: None)
    day = datetime.now(VIETNAM).date().isoformat()
    job_id = ("weekly-" if weekly else "daily-") + day
    if revision:
        import re
        if not re.fullmatch(r'[a-z0-9-]{1,32}',revision):raise ValueError('Invalid analysis revision')
        job_id+='-'+revision
    with memory.connect() as db:
        previous = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if (previous and 'ContentAIError: File Master Prompt phải có định dạng .txt hoặc .md.' in str(previous['detail'])
            and 'Read the complete attached JSON ' in previous['request']):
        # This exact error is raised by the local attachment preflight before
        # any HTTP request. Retain that unsent job; use one stable corrected
        # transport identity, never reclassify a remote/ambiguous failure.
        job_id += '-txt-evidence-v1'
        with memory.connect() as db:
            previous = db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if previous and previous["status"] == "complete":
        return json.loads(previous["response"])
    recovering = bool(previous and previous["status"] in {"submitted", "uncertain", "response_received"})
    raw_evidence = memory.snapshots(300)
    # Keep all raw reports in SQLite. Send bounded, explicitly scoped numeric
    # views instead of dropping all but the last three reports on a busy channel.
    evidence,catalogue=analysis_evidence(raw_evidence)
    if not evidence:
        raise RuntimeError("No verified analytics; keep original Master instead of inventing lessons")
    attachments = ()
    if previous:
        request = previous['request']
        if previous['status'] == 'reserved':
            import re, hashlib
            from .content_ai import ChatAttachment
            match = re.search(r'Read the complete attached JSON ([\w.-]+); SHA256=([a-f0-9]{64})',request)
            if match:
                data = (memory.root / match[1]).read_bytes()
                if hashlib.sha256(data).hexdigest() != match[2]:
                    raise ValueError('Saved analytics attachment digest changed')
                attachments = (ChatAttachment(match[1],data,'text/plain'),)
    else:
        request, attachments = analysis_request(memory,evidence,catalogue,job_id)
    if len(request.encode("utf-8")) > 48 * 1024:
        raise RuntimeError("Analysis context exceeds byte budget; preserve evidence for scoped analysis")
    status = check_chatgpt_web_bridge(auto_start=False, timeout=10)
    if not status.operational:
        raise RuntimeError("ChatGPT Bridge unavailable; analytics retained, no request sent")
    job = memory.reserve(job_id, request)
    input_path = memory.root / (job_id + "-input.json")
    if not input_path.is_file():
        marker = "\nEVIDENCE:\n" if "\nEVIDENCE:\n" in request else "\nNEW AND HISTORICAL EVIDENCE:\n"
        supplied = json.loads(request.rsplit(marker, 1)[1])
        atomic_write_json(input_path, {"request_sha256": digest(request), "evidence_ids": [e["evidence_id"] for e in supplied]})
    pinned = json.loads(input_path.read_text(encoding="utf-8"))
    if pinned["request_sha256"] != digest(request):
        raise ValueError("Analysis input receipt mismatch")
    # Historical lessons in CHANNEL MEMORY cite older reports, even when the
    # latest report has replaced them in the numeric projection. They are part
    # of the exact submitted request, not an invention by the returned answer.
    supplied=supplied_evidence_ids(memory,request)
    if sorted(pinned['evidence_ids'])!=supplied:
        pinned.setdefault('report_evidence_ids',pinned['evidence_ids'])
        pinned['evidence_ids']=supplied
        pinned['historical_citation_policy']='known snapshot ID present in the exact submitted request'
        atomic_write_json(input_path,pinned)
    acquired = False
    chat = None
    timer = None
    try:
        if pool is not None:
            acquired = pool.acquire(cancel_event=cancel, label="Channel analytics", on_wait=lambda *_: None)
            if not acquired: raise InterruptedError("Analysis cancelled in queue")
        chat = ChatGPTWebChat(status.endpoint, resolve_chatgpt_web_model(CHATGPT_WEB_DEFAULT_MODEL_LABEL, status.available_models),
                              memory.root, status.access_token, thread_id=job["thread_id"])
        timer = threading.Timer(60 if recovering else 300, chat.cancel)
        timer.daemon = True; timer.start()
        if recovering and previous["response"]:
            response = previous["response"]
        elif recovering:
            response=''  # recovered once from exact source below; never resubmit
        else:
            memory.job_state(job_id, "submitted")
            response = chat.send(request, attachments=attachments, cancel_event=cancel, on_status=lambda _s,t: report(t),output_mode='verified_json')
        response,source_proof=exact_text_response(status,chat.thread_id,request)
        memory.job_state(job_id, "response_received", response=response)
        raw = response.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        memory.apply_analysis(result, snapshot_ids=pinned["evidence_ids"], analysis_id=job_id)
        memory.job_state(job_id, "complete", response=json.dumps(result, ensure_ascii=False))
        atomic_write_json(memory.root / (job_id + ".json"), {
            "channel_id": memory.channel_id, "result": result,
            "thread_id": chat.thread_id, "conversation_url": saved_conversation_url(app_data_dir() / "chat-bridge" / "bridge-state.json", chat.thread_id),
            "memory_version": digest(memory.context()), "created_at": now_iso(),
            "source_proof":source_proof,
        })
        return result
    except Exception as exc:
        if chat is not None:
            memory.job_state(job_id, "uncertain", detail=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if timer: timer.cancel()
        if acquired: pool.release()


def run_cycle(workspace, *, endpoint, coordinator=None, pool=None, cancel_event=None, progress=None, force=False):
    policy = load_policy(workspace.state_dir)
    if not policy.get("enabled") or policy.get('analytics_paused'):
        return
    if not _LANE.acquire(blocking=False):
        return
    report = progress or (lambda _: None)
    try:
        memory = ChannelMemory(workspace.state_dir, policy["channel_id"])
        current = datetime.now(VIETNAM)
        snapshots = memory.snapshots(1)
        anchor = current.replace(hour=(current.hour // 6) * 6, minute=45, second=0, microsecond=0)
        if anchor > current: anchor -= timedelta(hours=6)
        due = not snapshots or datetime.fromisoformat(snapshots[0]["collected_at"]) < anchor
        if due or force:
            for snapshot in StudioAnalyticsReader(endpoint, coordinator).collect(policy["profile_id"], policy["channel_id"], cancel_event=cancel_event, progress=report):
                memory.add_snapshot(snapshot)
        if policy.get('detailed_analytics'):
            from .studio_inspection import collect_video_details
            from .video_analytics import export_details
            cursor=collect_video_details(workspace,endpoint=endpoint,policy=policy,coordinator=coordinator,cancel=cancel_event)
            export_details(workspace,policy)
            if cursor.get('pending'):
                report('Video analytics cursor retained; continuing at next idle check')
        if force or (current.hour, current.minute) >= (20, 15):
            analyze(memory, cancel_event=cancel_event, pool=pool, progress=report)
        if current.weekday() == 0 and (current.hour, current.minute) >= (20, 30):
            analyze(memory, weekly=True, cancel_event=cancel_event, pool=pool, progress=report)
        from .native_ab import optimize_eligible
        optimize_eligible(workspace,endpoint=endpoint,policy=policy,coordinator=coordinator,cancel=cancel_event,pool=pool)
    finally:
        _LANE.release()
