from dataclasses import replace
from pathlib import Path
import pytest
from PIL import Image
from docurandom.broll import FootageLedger, dhash, splice_footage
from docurandom.models import MediaInfo,RenderSettings
from docurandom.planner import build_render_plan
from docurandom.renderer import build_scene_filter_graph, normalize_image


def test_portrait_normalization_retains_top_and_bottom(tmp_path):
    image=Image.new('RGB',(100,300),'green')
    image.paste('red',(0,0,100,40));image.paste('blue',(0,260,100,300))
    source=tmp_path/'portrait.png';image.save(source)
    for mode in ('vertical-blur','vertical-smart'):
        out=tmp_path/(mode+'.png');normalize_image(source,out,(320,180),mode)
        with Image.open(out)as result:
            assert result.getpixel((160,15))[0]>180
            assert result.getpixel((160,165))[2]>180


def test_semantic_footage_preserves_duration_and_source_speed(tmp_path):
    media=[MediaInfo(tmp_path/'1.jpg','1.jpg',1920,1080,'jpg'),MediaInfo(tmp_path/'2.jpg','2.jpg',1920,1080,'jpg')]
    settings=RenderSettings(tmp_path,tmp_path,audio_path=tmp_path/'voice.wav',audio_duration=30,randomize_order=False,framing='vertical-smart')
    plan=build_render_plan(media,settings,seed='abc123')
    clip=MediaInfo(tmp_path/'portrait.mp4','portrait.mp4',1080,1920,'mp4','video',12,30,'h264')
    updated,ordered=splice_footage(plan,media,[{'start':4,'end':9,'media':clip}],settings)
    assert sum(s.duration_frames for s in updated.scenes)-sum(t.duration_frames for t in updated.transitions)==900
    assert updated.expected_duration==30 and len(ordered)==len(updated.scenes)
    video=next(s for s in updated.scenes if s.media_type=='video')
    assert video.playback_rate==1 and not video.loop_source
    graph=build_scene_filter_graph(video,settings)
    assert 'force_original_aspect_ratio=decrease' in graph
    assert 'crop=1728:972' not in graph
    with pytest.raises(ValueError):splice_footage(plan,media,[{'start':0,'end':8,'media':clip}],settings)


def test_changed_file_or_provider_ids_do_not_hide_visual_reuse(tmp_path):
    ledger=FootageLedger(tmp_path)
    path=tmp_path/'clip.mp4';path.write_bytes(b'first source')
    source={'provider':'local','asset_id':'one','source_url':'owned:clip','license':'owned','checked_at':'2026-10-21'}
    hashes=['aaaaaaaaaaaaaaaa','bbbbbbbbbbbbbbbb','cccccccccccccccc']
    sha=ledger.record(video_id='A',channel_id='C',path=path,source=source,start=0,end=5,hashes=hashes)
    assert ledger.duplicate(channel_id='C',video_id='B',sha256='reencoded',provider_id='other:two',start=0,end=5,hashes=hashes)
    assert ledger.duplicate(channel_id='C',video_id='B',sha256=sha,provider_id='local:one',start=3,end=7,hashes=[])
    assert not ledger.duplicate(channel_id='C',video_id='A',sha256=sha,provider_id='local:one',start=0,end=5,hashes=hashes)
