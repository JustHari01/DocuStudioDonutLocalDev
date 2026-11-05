"""Run the real channel collector or a local render canary; never upload a test."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def render_canary(root: Path):
    from PIL import Image
    from docurandom.models import MediaInfo,RenderSettings
    from docurandom.planner import build_render_plan
    from docurandom.renderer import render_video
    from docurandom.ffmpeg_manager import FFmpegTools
    from docurandom.runtime_profile import RenderRuntimeProfile,detect_host_resources
    from docurandom.scanner import probe_media_file
    from docurandom.storage import app_data_dir,atomic_write_json
    out=Path(__file__).resolve().parents[1]/'artifacts'/'channel-learning-20261021'/'daita14-render'
    out.mkdir(parents=True,exist_ok=True)
    tools=FFmpegTools(app_data_dir()/'tools'/'ffmpeg.exe',app_data_dir()/'tools'/'ffprobe.exe')
    portrait=out/'portrait-test.png';image=Image.new('RGB',(100,300),'green')
    image.paste('red',(0,0,100,45));image.paste('blue',(0,255,100,300));image.save(portrait)
    clip=out/'portrait-test.mp4'
    subprocess.run([str(tools.ffmpeg),'-v','error','-y','-loop','1','-i',str(portrait),'-t','5','-r','15','-c:v','libx264','-pix_fmt','yuv420p',str(clip)],check=True,timeout=20)
    source=root/'Media'/'Image_0001.jpg'
    with Image.open(source)as im:width,height=im.size
    media=[MediaInfo(source,source.name,width,height,'jpg'),MediaInfo(clip,clip.name,100,300,'mp4','video',5,15,'h264')]
    settings=RenderSettings(root/'Media',out,width=640,height=360,fps=15,duration_min=3,duration_max=3,
                            framing='vertical-smart',randomize_order=False,effect_density='none',intensity='light',
                            transition_min=0.1,transition_max=0.1,rights_confirmed=True,flow_right_crop=False,output_name='DaiTa14-framing-canary')
    plan=build_render_plan(media,settings,seed='a14ca11')
    profile=RenderRuntimeProfile(detect_host_resources(),'libx264',False,1,2,'CPU')
    result=render_video(media,settings,plan,tools=tools,runtime_profile=profile,log_callback=lambda t:print(t,flush=True))
    subprocess.run([str(tools.ffmpeg),'-v','error','-i',str(result.video_path),'-f','null','-'],check=True,timeout=30)
    still=out/'canary-frame.png'
    subprocess.run([str(tools.ffmpeg),'-v','error','-y','-ss','4','-i',str(result.video_path),'-frames:v','1',str(still)],check=True,timeout=20)
    atomic_write_json(out/'verified.json',{'workspace':'DaiTa14','video':str(result.video_path),'frame':str(still),
                      'full_decode':'passed','original_workspace_data_changed':False,'uploaded':False,
                      'source_media':[str(m.path) for m in media], 'probe':probe_media_file(result.video_path,tools.ffprobe)})
    print(json.dumps({'video':str(result.video_path),'frame':str(still),'verified':True}),flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['collect','analyze','status','render-canary']);p.add_argument('--workspace',type=Path,required=True)
    args=p.parse_args();root=args.workspace.resolve()
    if args.action=='render-canary':render_canary(root);return
    from docurandom.channel_learning import ChannelMemory,load_policy
    policy=load_policy(root/'.docurandom');memory=ChannelMemory(root/'.docurandom',policy['channel_id'])
    if args.action=='status':print(json.dumps({'memory':str(memory.export()),'snapshots':len(memory.snapshots()),'lessons':len(memory.lessons())}));return
    if args.action=='analyze':
        from docurandom.channel_runtime import analyze
        print(json.dumps(analyze(memory,progress=lambda t:print(t,flush=True)),ensure_ascii=False));return
    from docurandom.channel_analytics import StudioAnalyticsReader
    from docurandom.donut_manager import resolve_donut_mcp_endpoint
    for snapshot in StudioAnalyticsReader(resolve_donut_mcp_endpoint()).collect(policy['profile_id'],policy['channel_id'],progress=lambda t:print(t,flush=True)):
        print(memory.add_snapshot(snapshot))


if __name__=='__main__':main()
