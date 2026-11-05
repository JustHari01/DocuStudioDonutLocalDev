"""Scoped video metrics and conservative native A/B admission."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from .channel_analytics import parse_number
from .channel_learning import now_iso
from .storage import atomic_write_json

LABELS={
 'subscribers_net':('Người đăng ký','Subscribers','チャンネル登録者数'),
 'impressions':('Số lượt hiển thị hình thu nhỏ','Impressions','インプレッション数'),
 'ctr_percent':('Tỷ lệ nhấp vào hình thu nhỏ','Impressions click-through rate','インプレッションのクリック率'),
 'views':('Số lượt xem','Views','視聴回数'),
 'watch_hours':('Thời gian xem (giờ)','Watch time (hours)','総再生時間（単位: 時間）'),
 'average_view_duration':('Thời lượng xem trung bình','Average view duration','平均視聴時間'),
 'average_view_percentage':('Tỷ lệ phần trăm đã xem trung bình','Average percentage viewed','平均再生率'),
 'unique_viewers':('Số người xem riêng biệt','Unique viewers','ユニーク視聴者数')}


def conservative_public_time(raw):
    if not raw:return None
    match=re.fullmatch(r'(\d{1,2}) thg (\d{1,2}), (\d{4})',raw)
    if match:
        day,month,year=map(int,match.groups())
        return datetime(year,month,day,23,59,59,tzinfo=timezone(timedelta(hours=7))).isoformat()
    for fmt in ('%b %d, %Y','%Y/%m/%d','%Y年%m月%d日'):
        try:return datetime.strptime(raw,fmt).replace(hour=23,minute=59,second=59,tzinfo=timezone(timedelta(hours=7))).isoformat()
        except ValueError:pass
    return None


def video_snapshot(page,content):
    texts=[r['text']for r in page['observations']if r['role']=='StaticText']
    # Primary KPI strip ends before card tables; avoid unrelated realtime totals.
    stop=next((i for i,t in enumerate(texts)if t in {'Thời gian thực','Realtime','Cách người xem tìm thấy video này',
        'How viewers find this video','Tỷ lệ giữ chân người xem','Audience retention'}),len(texts))
    primary=texts[:stop];locale='vi'if any('lượt'in t or 'ngày'in t for t in texts)else'ja'if any('視聴'in t for t in texts)else'en'
    metrics={};availability={}
    for key,labels in LABELS.items():
        values=[]
        for i,t in enumerate(primary[:-1]):
            if t not in labels:continue
            raw=primary[i+1]
            if raw in labels and i+2<len(primary):raw=primary[i+2]
            if key=='average_view_duration'and re.fullmatch(r'\d+:\d\d(?::\d\d)?',raw):
                value=sum(int(v)*60**n for n,v in enumerate(reversed(raw.split(':'))))
            else:value=parse_number(raw,locale)
            if value is not None:values.append(value)
        unique=set(values);metrics[key]=next(iter(unique))if len(unique)==1 else None
        availability[key]='observed'if len(unique)==1 else'not_exposed_or_ambiguous'
    period=next((t for t in texts if re.fullmatch(r'\d+ ngày qua|Last \d+ days|過去 \d+ 日間|Từ khi xuất bản|Since published',t)),None)
    insufficient=any(re.search(r'không đủ dữ liệu|not enough data|データが不十分',t,re.I)for t in texts)
    for name in ['retention_30s','retention_curve','traffic_sources','search_terms','suggesting_videos','age_gender','geography']:
        availability[name]='insufficient_data_reported'if insufficient else'raw_observations_only_not_structured'
    traffic = {}
    traffic_labels = {
        'browse': ('Browse features', 'Các tính năng duyệt xem', 'ブラウジング機能'),
        'suggested': ('Suggested videos', 'Video đề xuất', '関連動画'),
        'search': ('YouTube search', 'Tìm kiếm trên YouTube', 'YouTube 検索'),
        'external': ('External', 'Bên ngoài', '外部'),
        'advertising': ('YouTube advertising', 'Quảng cáo trên YouTube', 'YouTube 広告'),
    }
    for key, labels in traffic_labels.items():
        observed = set()
        for index, label in enumerate(texts[:-1]):
            if label in labels and re.fullmatch(r'[\d.,\s]+[%％]', texts[index+1]):
                value = parse_number(texts[index+1], locale)
                if value is not None and 0 <= value <= 100: observed.add(value)
        if len(observed) == 1: traffic[key] = {'percent': observed.pop()}
    if traffic: availability['traffic_sources'] = 'partially_observed_percentages'
    published = content.get('published_at')
    age = None
    if published:
        try:
            age = max(0, (datetime.fromisoformat(page['collected_at']) - datetime.fromisoformat(published)).total_seconds() / 3600)
        except (ValueError, TypeError):
            pass
    cohort = None if age is None else ('0-24h' if age < 24 else '24-72h' if age < 72 else '3-7d' if age < 168 else '7-28d' if age < 672 else '28d+')
    return {'channel_id':page['channel_id'],'video_id':page['video_id'],'collected_at':page['collected_at'],
            'video_age_hours':age,'age_cohort':cohort,
            'traffic_sources':traffic,
            'source_url':page['url'],'report':'video-'+page['report'],'report_range':{'label':period,'dates':[t for t in texts if re.search(r'\d.*(?:thg|202\d|年)',t)][:3]},
            'metrics':metrics,'availability':availability,'content_context':content,'locale':locale,
            'raw_observations':page['observations'],'engagement_provenance':'may_include_assisted_engagement'}


def ab_admission(video,reach,*,now=None,active=None,last_trial_at=None,capability=False):
    now=now or datetime.now(timezone.utc);reasons=[]
    if video.get('visibility')not in {'Công khai','Public','公開'}:reasons.append('not_public')
    published=video.get('published_at')
    if not published or now-datetime.fromisoformat(published)<timedelta(hours=48):reasons.append('public_age_not_verified_48h')
    if now-datetime.fromisoformat(reach['collected_at'])>timedelta(hours=24):reasons.append('stale_analytics')
    if reach['report_range'].get('label')not in {'7 ngày qua','Last 7 days','過去 7 日間'}:reasons.append('seven_day_window_not_verified')
    impressions=reach['metrics'].get('impressions')
    if impressions is None or impressions<1000:reasons.append('fewer_than_1000_verified_impressions')
    if active:reasons.append('channel_has_active_experiment')
    if last_trial_at and now-datetime.fromisoformat(last_trial_at)<timedelta(days=28):reasons.append('same_video_cooldown')
    if not capability:reasons.append('native_ab_control_not_confirmed')
    return {'eligible':not reasons,'reasons':reasons,'checked_at':now_iso(),'video_id':video['video_id'],
            'no_mutation':True,'thresholds_are_operator_policy_not_statistical_guarantees':True}


def export_details(workspace,policy):
    from .channel_learning import ChannelMemory
    root=workspace.state_dir/'analytics'/policy['channel_id']/'video-details'
    listing=json.loads((root/'content-list.json').read_text(encoding='utf-8'))
    videos={r['video_id']:dict(r,published_at=conservative_public_time(r.get('published_date_raw')),published_time_precision='day_end_upper_bound') for r in listing.get('videos',[])}
    memory=ChannelMemory(workspace.state_dir,policy['channel_id']);count=0;admissions=[]
    for p in root.glob('*.json'):
        if p.name in {'cursor.json','content-list.json','summary.json'}:continue
        page=json.loads(p.read_text(encoding='utf-8'))
        if page.get('report') == 'edit':
            from .research_upgrade import packaging_history
            packaging_history(workspace, page)
        if 'video_id'not in page or page.get('report')=='edit':continue
        snap=video_snapshot(page,videos.get(page['video_id'],{}));memory.add_snapshot(snap);count+=1
        if page['report']=='reach':
            video=videos.get(page['video_id'],{'video_id':page['video_id']})
            edit=root/(page['video_id']+'-edit.json')
            controls=json.loads(edit.read_text(encoding='utf-8')).get('ab_controls',[])if edit.exists()else[]
            admissions.append(ab_admission(video,snap,capability=bool(controls)))
    result={'exported_reports':count,'video_count':len(videos),'admissions':admissions,'updated_at':now_iso()}
    atomic_write_json(root/'summary.json',result);return result
