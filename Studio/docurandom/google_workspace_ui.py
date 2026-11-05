"""Per-workspace controls shown in the workspace selection menu."""
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .google_workspace import APPS, config, read, root, save_config, preview
from .google_workspace import now


def status_text(state):
    labels={'running':'Đang làm','queued':'Đang đợi profile','recovering':'Đang đọc lại biên nhận',
            'complete':'Hoàn tất','attention':'Cần kiểm tra biên nhận','auth_required':'Chờ đăng nhập ứng dụng',
            'account_required':'Chờ chọn tài khoản','permission_required':'Chờ cấp quyền truy cập',
            'verification_required':'Chờ người dùng xác minh','yielded':'Đã nhường profile'}
    labels.update(waiting_window='Chờ khung giờ đã chọn',daily_limit='Đã đủ lượt mới hôm nay')
    labels['cached']='Đã có kết quả được lưu'
    text=labels.get(state.get('status'),'Chưa có lượt chạy')+' · '+str(state.get('detail',''))
    if state.get('current_app') in APPS:
        text+='\nỨng dụng: '+APPS[state['current_app']][0]
        text+=' · Bước '+str(state.get('current_step',0))+'/'+str(state.get('step_count',0))
    if state.get('waiting_since'):
        from datetime import datetime
        try:
            minutes=max(0,int((now()-datetime.fromisoformat(state['waiting_since'])).total_seconds()//60))
            text+=f'\nĐã chờ profile: {minutes} phút'
        except (ValueError,TypeError):pass
    if state.get('server_not_before'):
        text+='\nMáy chủ yêu cầu chờ đến: '+state['server_not_before']
    elif state.get('next_run_at') and state.get('status') not in {'account_required','auth_required','permission_required','verification_required','attention'}:
        text+='\nLượt kiểm tra kế: '+state['next_run_at']
    return text


def settings_dialog(parent, workspace, service=None, on_saved=None):
    c=config(workspace)
    dialog=tk.Toplevel(parent);dialog.title('Google · '+workspace.name)
    dialog.geometry('900x720');dialog.minsize(760,620)
    panel=ttk.Frame(dialog,padding=16);panel.pack(fill='both',expand=True)
    enabled=tk.BooleanVar(dialog,c['enabled']);create=tk.BooleanVar(dialog,c['create_files'])
    upload=tk.BooleanVar(dialog,c['upload_thumbnail'])
    interval=tk.StringVar(dialog,str(c['interval_minutes']));limit=tk.StringVar(dialog,str(c['apps_per_cycle']))
    daily=tk.StringVar(dialog,str(c['daily_cycle_limit']));minutes=tk.StringVar(dialog,str(c['max_cycle_minutes']))
    start=tk.StringVar(dialog,c['active_start']);end=tk.StringVar(dialog,c['active_end'])
    ttk.Label(panel,text='TƯƠNG TÁC GOOGLE · '+workspace.name,font=('Segoe UI Semibold',14)).pack(anchor='w')
    ttk.Label(panel,text='Dùng đúng profile Donut đã chọn trong workspace. Tự nhường khi upload/Public cần profile.',wraplength=810).pack(anchor='w',pady=(5,12))
    notebook=ttk.Notebook(panel);notebook.pack(fill='both',expand=True)
    schedule=ttk.Frame(notebook,padding=12);apps_panel=ttk.Frame(notebook,padding=12);monitor=ttk.Frame(notebook,padding=12)
    notebook.add(schedule,text='Lịch & giới hạn');notebook.add(apps_panel,text='Ứng dụng & tác vụ');notebook.add(monitor,text='Trạng thái & kế hoạch')
    ttk.Checkbutton(schedule,text='Bật chạy Google định kỳ cho workspace này',variable=enabled).pack(anchor='w')
    line=ttk.Frame(schedule);line.pack(fill='x',pady=10)
    ttk.Label(line,text='Chu kỳ (phút):').pack(side='left');ttk.Entry(line,textvariable=interval,width=8).pack(side='left',padx=8)
    ttk.Label(line,text='Ứng dụng mỗi lượt:').pack(side='left');ttk.Spinbox(line,from_=1,to=5,textvariable=limit,width=5).pack(side='left',padx=8)
    bounds=ttk.Frame(schedule);bounds.pack(fill='x',pady=10)
    ttk.Label(bounds,text='Lượt mới tối đa/ngày:').pack(side='left');ttk.Spinbox(bounds,from_=1,to=24,textvariable=daily,width=5).pack(side='left',padx=8)
    ttk.Label(bounds,text='Phút tối đa mỗi phiên:').pack(side='left');ttk.Spinbox(bounds,from_=1,to=60,textvariable=minutes,width=5).pack(side='left',padx=8)
    hours=ttk.Frame(schedule);hours.pack(fill='x',pady=10)
    ttk.Label(hours,text='Khung giờ Việt Nam:').pack(side='left');ttk.Entry(hours,textvariable=start,width=8).pack(side='left',padx=8)
    ttk.Label(hours,text='đến').pack(side='left');ttk.Entry(hours,textvariable=end,width=8).pack(side='left',padx=8)
    ttk.Label(schedule,text='Giờ bắt đầu bằng giờ kết thúc = cả ngày. Khung qua nửa đêm thuộc ngày bắt đầu.',wraplength=760).pack(anchor='w')
    days=ttk.Frame(schedule);days.pack(fill='x',pady=12)
    day_vars={}
    for i,label in enumerate(['T2','T3','T4','T5','T6','T7','CN']):
        day_vars[i]=tk.BooleanVar(dialog,i in c['weekdays'])
        ttk.Checkbutton(days,text=label,variable=day_vars[i]).pack(side='left',padx=(0,12))
    ttk.Label(schedule,text='Giới hạn áp dụng cả khi bấm chạy một lượt. Phục hồi dùng lượt cũ, không cấp thêm lượt mới. Hết thời gian phiên sẽ giữ checkpoint ở điểm có thể dừng; không cắt ngang thao tác ghi chưa rõ kết quả.',wraplength=760).pack(anchor='w',pady=12)
    ttk.Label(apps_panel,text='Ứng dụng được luân phiên:').pack(anchor='w',pady=(4,7))
    apps=ttk.Frame(apps_panel);apps.pack(fill='x')
    app_vars={}
    for i,(app,(label,_))in enumerate(APPS.items()):
        var=tk.BooleanVar(dialog,app in c['apps']);app_vars[app]=var
        ttk.Checkbutton(apps,text=label,variable=var).grid(row=i//3,column=i%3,sticky='w',padx=(0,18),pady=2)
    for i in range(3):apps.columnconfigure(i,weight=1)
    ttk.Checkbutton(apps_panel,text='Tạo tối đa 1 file/ngày cho mỗi ứng dụng Docs / Sheets / Slides / Forms được chọn',variable=create).pack(anchor='w',pady=(12,3))
    ttk.Checkbutton(apps_panel,text='Drive: tải thumbnail hiện tại của workspace (mỗi ảnh chỉ một lần)',variable=upload).pack(anchor='w')
    ttk.Label(apps_panel,text='Từ khóa / địa chỉ tìm kiếm (mỗi dòng một mục):').pack(anchor='w',pady=(10,3))
    queries=tk.Text(apps_panel,height=3,wrap='word');queries.pack(fill='x');queries.insert('1.0','\n'.join(c['queries']))
    status=tk.StringVar(dialog);ttk.Label(monitor,textvariable=status,wraplength=790).pack(anchor='w',pady=8)
    result=tk.Text(monitor,height=6,wrap='word');result.pack(fill='x')
    details=tk.Text(monitor,height=10,wrap='word');details.pack(fill='both',expand=True,pady=(8,0))
    events=queue.Queue()
    def show_details(text):
        details.configure(state='normal');details.delete('1.0','end');details.insert('1.0',text);details.configure(state='disabled')
    def refresh():
        if not dialog.winfo_exists():return
        while not events.empty():
            text=events.get_nowait();show_details(text);check_button.configure(state='normal'if service else'disabled')
        s=read(root(workspace)/'state.json')
        status.set(status_text(s))
        lines=[' · '.join(str(r.get(k,''))for k in ['app','action','title','artifact_url','url']).strip(' ·')for r in s.get('results',[])]
        text='\n'.join(lines)
        if result.get('1.0','end-1c')!=text:
            result.configure(state='normal');result.delete('1.0','end');result.insert('1.0',text);result.configure(state='disabled')
        dialog.after(2000,refresh)
    def collect():
        return {'enabled':enabled.get(),'interval_minutes':int(interval.get()),
                'apps_per_cycle':int(limit.get()),'apps':[a for a,v in app_vars.items()if v.get()],
                'queries':[v.strip()for v in queries.get('1.0','end').splitlines()if v.strip()],
                'create_files':create.get(),'upload_thumbnail':upload.get(),
                'daily_cycle_limit':int(daily.get()),'max_cycle_minutes':int(minutes.get()),
                'active_start':start.get().strip(),'active_end':end.get().strip(),
                'weekdays':[d for d,v in day_vars.items()if v.get()]}
    def save(cancel_disabled=True):
        try:
            saved=save_config(workspace,collect())
            if service and cancel_disabled and not enabled.get():service.cancel_workspace(workspace.workspace_id)
            if on_saved:on_saved(saved)
            return True
        except Exception as exc:messagebox.showerror('Chưa lưu cấu hình Google',str(exc),parent=dialog);return False
    def run_now():
        if save(cancel_disabled=False) and service:
            service.run_now(workspace)
            notebook.select(monitor)
            status.set(status_text(read(root(workspace)/'state.json')))
    def show_preview():
        try:
            p=preview(workspace,collect());lines=['Kế hoạch đang giữ checkpoint'if p['retained_plan']else'Kế hoạch lượt mới (chưa gửi)']
            if p['gate']:lines.append(p['gate']['detail']+' · '+p['gate']['next_run_at'])
            if p['blocked_until_user_action']:lines.append('Lượt hiện có đang chờ xử lý; xem trạng thái ở trên.')
            labels={'browse':'Mở/đọc trang','create':'Tạo file','upload':'Tải thumbnail'}
            for i,step in enumerate(p['steps'],1):
                lines.append(f"{i}. {step['label']} · {labels.get(step['action'],step['action'])} · {step['title']} · {step['receipt_status']}")
            show_details('\n'.join(lines));notebook.select(monitor)
        except Exception as exc:messagebox.showerror('Chưa đọc được kế hoạch',str(exc),parent=dialog)
    def check_connection():
        notebook.select(monitor);check_button.configure(state='disabled');show_details('Đang kiểm tra extension đã nối; không mở profile hoặc tạo file…')
        def worker():
            try:
                report=service.diagnostics(workspace);connection=report['connection']
                lines=['Extension đang kết nối · '+str(connection.get('extension_version',''))if connection['connected']else'Profile/extension chưa kết nối; không tự mở profile.']
                lines.append('Kết nối extension không xác nhận đăng nhập vào từng ứng dụng Google.')
                for app in report['apps']:
                    proof=app['last_completed_google_task']
                    lines.append(app['label']+': '+('đã hoàn tất lượt '+str(proof.get('action'))+' · '+str(proof.get('at',''))if proof else'chưa có kết quả thực tế được lưu'))
                events.put('\n'.join(lines))
            except Exception as exc:events.put('Chưa xác minh được kết nối: '+str(exc))
        threading.Thread(target=worker,daemon=True,name='GoogleReadOnlyDiagnostics').start()
    buttons=ttk.Frame(panel);buttons.pack(fill='x',pady=(8,0))
    ttk.Button(buttons,text='Lưu thiết lập',command=save).pack(side='right')
    ttk.Button(buttons,text='Chạy một lượt ngay',command=run_now,state='normal'if service else'disabled').pack(side='right',padx=8)
    ttk.Button(buttons,text='Xem kế hoạch',command=show_preview).pack(side='left')
    check_button=ttk.Button(buttons,text='Kiểm tra kết nối',command=check_connection,state='normal'if service else'disabled');check_button.pack(side='left',padx=6)
    def open_results():
        root(workspace).mkdir(parents=True,exist_ok=True);__import__('os').startfile(root(workspace))
    ttk.Button(monitor,text='Mở thư mục kết quả',command=open_results).pack(anchor='w',pady=(5,0))
    refresh()
    return dialog


def add_workspace_controls(parent, workspace, service=None):
    row=ttk.Frame(parent,style='PanelAlt.TFrame');row.pack(fill='x',pady=(5,8))
    value=tk.BooleanVar(parent,config(workspace)['enabled'])
    def toggle():
        try:
            save_config(workspace,{'enabled':value.get()})
            if service and not value.get():service.cancel_workspace(workspace.workspace_id)
        except Exception as exc:value.set(not value.get());messagebox.showerror('Google',str(exc),parent=parent)
    ttk.Checkbutton(row,text='Google định kỳ',variable=value,command=toggle,style='WorkspaceSelect.TCheckbutton').pack(side='left')
    ttk.Button(row,text='Thiết lập Google',style='Secondary.TButton',command=lambda:settings_dialog(parent,workspace,service,on_saved=lambda c:value.set(c['enabled']))).pack(side='right')
    return row
