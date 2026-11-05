"""Render a real, licensed portrait excerpt through the production scene renderer."""
from pathlib import Path
import sys,json,hashlib,subprocess
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from docurandom.models import MediaInfo,RenderSettings
from docurandom.planner import build_render_plan
from docurandom.renderer import render_video
from docurandom.runtime_profile import RenderRuntimeProfile,detect_host_resources
from docurandom.ffmpeg_manager import FFmpegTools
from docurandom.scanner import probe_media_file
from docurandom.broll import splice_footage,FootageLedger,frame_fingerprints
from docurandom.broll_runtime import credit_text
from docurandom.storage import app_data_dir,atomic_write_json
from PIL import Image

root=Path(__file__).resolve().parents[1]/'artifacts/daita14-e2e-20261021/real-footage-canary'
root.mkdir(parents=True,exist_ok=True)
tools=FFmpegTools(app_data_dir()/'tools/ffmpeg.exe',app_data_dir()/'tools/ffprobe.exe')
library=app_data_dir()/'Footage/verified-library'
source=json.loads((library/'metal-lathe.source.json').read_text(encoding='utf-8'))
clip=library/source['file'];assert hashlib.sha256(clip.read_bytes()).hexdigest()==source['sha256']
info=probe_media_file(clip,tools.ffprobe)
image=Path('D:/Youtube/daita14/Media/Image_0001.jpg')
with Image.open(image) as im:width,height=im.size
media=[MediaInfo(image,image.name,width,height,'jpg')]
item=MediaInfo(clip,clip.name,int(info['width']),int(info['height']),'mp4','video',float(info['duration']),float(info['fps']),info['codec_name'],bool(info['has_audio']))
settings=RenderSettings(image.parent,root,width=640,height=360,fps=15,duration_min=20,duration_max=20,
    randomize_order=False,framing='vertical-smart',effect_density='none',intensity='light',transition_min=.1,transition_max=.1,
    rights_confirmed=True,flow_right_crop=False,output_name='DaiTa14-real-portrait-canary')
base=build_render_plan(media,settings,seed='caa141');assert base.expected_duration==20
plan,ordered=splice_footage(base,media,[{'start':5,'end':9,'media':item}],settings)
plan.settings['footage_provenance']={'credits':[source],'illustrative_only':True}
result=render_video(ordered,settings,plan,tools=tools,
    runtime_profile=RenderRuntimeProfile(detect_host_resources(),'libx264',False,1,2,'CPU'),log_callback=lambda t:print(t,flush=True))
subprocess.run([str(tools.ffmpeg),'-v','error','-i',str(result.video_path),'-f','null','-'],check=True,timeout=45)
frame=root/'portrait-frame.png'
subprocess.run([str(tools.ffmpeg),'-v','error','-y','-ss','7','-i',str(result.video_path),'-frames:v','1',str(frame)],check=True,timeout=15)
ledger=FootageLedger(root/'ledger');hashes=frame_fingerprints(clip,tools.ffmpeg,0,4)
source.update(provider='local',asset_id='pexels-5846708')
sha=ledger.record(video_id='canary-a',channel_id='DaiTa14-canary',path=clip,source=source,start=0,end=4,hashes=hashes)
duplicate=ledger.duplicate(video_id='canary-b',channel_id='DaiTa14-canary',sha256=sha,provider_id='local:pexels-5846708',start=0,end=4,hashes=hashes)
assert duplicate
credits=credit_text({'credits':[source]});(root/'credits.txt').write_text(credits,encoding='utf-8')
atomic_write_json(root/'proof.json',{'full_decode':'passed','video':str(result.video_path),'frame':str(frame),
    'source':source,'source_probe':info,'used_seconds':4,'total_seconds':20,'rate':1,'loop':False,
    'repeated_source_rejected':duplicate,'production_video_changed':False,'uploaded':False,'credits':credits})
print('REAL_FOOTAGE_CANARY_PASS',result.video_path,flush=True)
