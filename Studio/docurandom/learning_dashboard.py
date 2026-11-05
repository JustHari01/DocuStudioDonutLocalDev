"""Read-only local reports with independent pause controls for analytics."""
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from .channel_learning import load_policy,ChannelMemory
from .storage import atomic_write_json


def summary(workspace):
    policy=load_policy(workspace.state_dir)
    if not policy.get('enabled'):return 'Kênh này chưa bật bộ học thử nghiệm.'
    memory=ChannelMemory(workspace.state_dir,policy['channel_id'])
    lines=[f'Workspace: {workspace.name}',f'Channel ID: {policy["channel_id"]}',
           'Analytics: '+('TẠM DỪNG'if policy.get('analytics_paused')else'ĐANG BẬT'),
           'Thu: 00:45/06:45/12:45/18:45 · Ngày: 20:15 · Tuần: thứ Hai 20:30 (giờ VN)',
           f'Bộ nhớ: {memory.root / "CHANNEL_MEMORY.md"}',
           f'Snapshots gần nhất: {len(memory.snapshots(300))} · Bài học: {len(memory.lessons())}']
    for name in ['cursor.json','summary.json']:
        p=memory.root/'video-details'/name
        if p.exists():
            d=json.loads(p.read_text(encoding='utf-8'));lines.append(name+': '+json.dumps({k:v for k,v in d.items()if k not in {'completed','pending','admissions'}},ensure_ascii=False))
            if 'pending'in d:lines.append(f'Còn {len(d["pending"])} báo cáo chi tiết đang chờ.')
            if 'admissions'in d:
                lines.append('A/B: '+str(sum(r['eligible']for r in d['admissions']))+' video đủ điều kiện đã xác minh.')
                for r in d['admissions'][:4]:lines.append('  '+r['video_id']+': '+', '.join(r['reasons']))
    for kind in ['daily','weekly']:
        found=sorted(memory.root.glob(kind+'-*.json'))
        reports=[p for p in found if not p.stem.endswith('-input')]
        if reports:
            d=json.loads(reports[-1].read_text(encoding='utf-8'));lines.extend(['',reports[-1].name, d.get('conversation_url',''),str(d.get('result',{}).get('summary',''))])
    lines.append('\nCác đề xuất là giả thuyết có phạm vi; thiếu số liệu không phải số 0. Không có bảo đảm tăng view.')
    acceptance=workspace.state_dir/'workflow-acceptance.json'
    if acceptance.exists():
        d=json.loads(acceptance.read_text(encoding='utf-8'))
        lines.extend(['','Nghiệm thu chuỗi thực tế: '+d['status'],json.dumps(d.get('evidence',{}),ensure_ascii=False,indent=2)])
    return '\n'.join(lines)


def show(parent,workspace):
    window=tk.Toplevel(parent);window.title('Số liệu và bộ nhớ · '+workspace.name);window.geometry('940x650')
    bar=ttk.Frame(window);bar.pack(fill='x',padx=12,pady=8)
    text=tk.Text(window,wrap='word');text.pack(fill='both',expand=True,padx=12,pady=8)
    def refresh():
        text.configure(state='normal');text.delete('1.0','end');text.insert('1.0',summary(workspace));text.configure(state='disabled')
    def pause():
        policy=load_policy(workspace.state_dir);policy['analytics_paused']=not policy.get('analytics_paused',False)
        atomic_write_json(workspace.state_dir/'learning-policy.json',policy);refresh()
    ttk.Button(bar,text='Cập nhật báo cáo',command=refresh).pack(side='left')
    ttk.Button(bar,text='Tạm dừng / tiếp tục analytics',command=pause).pack(side='left',padx=8)
    refresh();return window
