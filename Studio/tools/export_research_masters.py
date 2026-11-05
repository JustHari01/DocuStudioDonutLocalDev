"""Export standalone UTF-8 Masters; never assign a production workspace."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from docurandom.research_upgrade import DOMAIN_PROFILES,VERSION,assets_root,load_master,master_manifest
from docurandom.content_ai import read_master_prompt,expected_image_prompt_count

def export(destination: Path, *, replace_index=False):
    catalog=json.loads((assets_root()/'manifest.json').read_text(encoding='utf-8'))
    destination.mkdir(parents=True,exist_ok=True)
    rows=[]
    # Validate the complete export before writing any file.
    for domain,profile in DOMAIN_PROFILES.items():
        text=load_master(domain)
        entry=catalog['masters'][domain]
        slug=profile['export_slug']
        if not re.fullmatch(r'[A-Za-z0-9 -]+',slug):raise ValueError('Unsafe export slug')
        path=destination/f'{domain} - {slug} - V{VERSION}.txt'
        data=(assets_root()/entry['file']).read_bytes()
        if path.exists()and path.read_bytes()!=data:
            raise FileExistsError('Preserve edited Master; choose a new destination: '+str(path))
        if expected_image_prompt_count(text,10)!=20:raise ValueError('Image cadence changed')
        rows.append(dict(domain=domain,topic=profile['name'],file=path.name,version=VERSION,sha256=entry['sha256']))
    guide='''# Master riêng theo chủ đề — 20 file hoàn chỉnh

Mỗi TXT chứa đầy đủ quy trình và định hướng một ngách. Không cần nạp thêm
Master gốc, protocol hay module. Bạn tự chọn file cho từng workspace.

Trong Studio **2.4.324 trở lên**: mở đúng workspace → **Nạp TXT** → chọn file.
Hoặc dùng **Chọn Master theo ngách…**. Lựa chọn áp dụng cho video mới.
Để lượt đang dở hoàn tất trước khi đổi; không thay Master rồi nối checkpoint cũ.
Nạp TXT chuyển sang lựa chọn thủ công để Master thư viện không ghi đè file đã chọn.

| Mã | Chủ đề | File TXT |
|---|---|---|
'''
    for row in rows:guide+=f'| {row["domain"]} | {row["topic"]} | [{row["file"]}](<{(destination/row["file"]).as_posix()}>) |\n'
    guide+='''
Sáu mã G và sáu file đã có giữ nguyên bytes/hash. Mười bốn mã R chỉ là mã thư
viện theo A6, B2–B6, C1–C3, H3-01–H3-05 trong báo cáo; không đổi nghĩa G cũ.
Đây là 20 cụm nghiên cứu, không phải 20 thị trường độc lập hay 20 hướng đã chứng
minh tăng view. Các family đề tài bên trong giúp luân phiên video cùng một ngách.

Tool tiếp tục điều khiển kiểm ảnh/giọng, render, upload và lịch Public. Master
không thay model, lịch, dữ liệu profile hoặc cho phép bỏ cổng đủ nguyên liệu.
Bộ nhớ học chỉ dùng đúng kênh/ngách khi có; không đổi dữ liệu thiếu thành zero.

Bạn có thể sao chép một Master sang tên file riêng để tự chỉnh. Giữ các dòng
định danh và marker nếu vẫn dùng giao thức của tool. Không chỉ đổi tên file
rồi coi một kịch bản CNC là Master của tài chính hoặc văn hóa.

Chỉ nạp TXT, không nạp DANH-SACH.json hoặc file hướng dẫn này vào ô Master.
'''
    index={'version':VERSION,'required_studio':'2.4.324','masters':rows,'standalone_files':True,'workspace_assignments_changed':False}
    metadata={'HUONG-DAN.md':guide,'DANH-SACH.json':json.dumps(index,ensure_ascii=False,indent=2)+'\n'}
    for name,value in metadata.items():
        p=destination/name
        if p.exists()and p.read_text(encoding='utf-8')!=value and not replace_index:
            raise FileExistsError('Generated index exists; back it up before --replace-index')
    for row in rows:
        path=destination/row['file']
        if not path.exists():path.write_bytes((assets_root()/catalog['masters'][row['domain']]['file']).read_bytes())
        loaded=read_master_prompt(path)
        assert master_manifest(loaded)['domain']==row['domain']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']
    for name,value in metadata.items():(destination/name).write_text(value,encoding='utf-8')
    return index

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--destination',type=Path,default=ROOT.parent/'MasterPrompts'/VERSION)
    parser.add_argument('--replace-index',action='store_true')
    args=parser.parse_args()
    result=export(args.destination,replace_index=args.replace_index)
    print(json.dumps({'destination':str(args.destination),'masters':len(result['masters']),'workspace_assignments_changed':False},ensure_ascii=False))
