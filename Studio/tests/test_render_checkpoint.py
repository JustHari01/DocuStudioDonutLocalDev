from pathlib import Path

from docurandom.models import MediaInfo, RenderSettings
from docurandom.planner import build_render_plan
from docurandom.render_checkpoint import (
    load_render_plan_checkpoint,
    read_render_checkpoint,
    render_checkpoint_dir,
    render_request_identity,
    save_render_plan_checkpoint,
    update_render_checkpoint_state,
)


def _media(path: Path) -> MediaInfo:
    return MediaInfo(
        path=path,
        relative_path=path.name,
        width=640,
        height=360,
        format_name="jpeg",
    )


def test_render_plan_checkpoint_survives_runtime_profile_changes(tmp_path: Path) -> None:
    source = tmp_path / "1.jpg"
    source.write_bytes(b"source-image")
    media = [_media(source)]
    settings = RenderSettings(
        source_dir=tmp_path,
        output_dir=tmp_path / "output",
        output_name="Video_1",
        rights_confirmed=True,
        editorial_intent="test",
    )
    plan = build_render_plan(
        media,
        settings,
        seed="1234567890abcdef1234567890abcdef",
    )
    identity = render_request_identity(media, settings)
    directory = render_checkpoint_dir(media, settings)
    save_render_plan_checkpoint(directory, request_identity=identity, plan=plan)
    update_render_checkpoint_state(
        directory,
        status="error",
        completed_scenes=1,
        total_scenes=1,
        detail="test interruption",
    )

    changed_runtime = RenderSettings(
        **{
            **settings.__dict__,
            "video_encoder": "h264_nvenc",
            "hardware_accelerated": True,
            "threads_per_job": 8,
            "runtime_summary": "different machine",
            "parallel_jobs": 4,
        }
    )
    changed_identity = render_request_identity(media, changed_runtime)
    restored = load_render_plan_checkpoint(
        directory,
        request_identity=changed_identity,
    )

    assert changed_identity == identity
    assert restored is not None
    assert restored.seed == plan.seed
    assert restored.manifest_signature == plan.manifest_signature
    assert restored.scenes == plan.scenes
    document = read_render_checkpoint(directory)
    assert document["status"] == "error"
    assert document["completed_scenes"] == 1


def test_render_identity_changes_when_source_is_replaced(tmp_path: Path) -> None:
    source = tmp_path / "1.jpg"
    source.write_bytes(b"first")
    media = [_media(source)]
    settings = RenderSettings(source_dir=tmp_path, output_dir=tmp_path / "output")
    first = render_request_identity(media, settings)

    source.write_bytes(b"replacement-is-different")
    second = render_request_identity(media, settings)

    assert first != second


def test_render_checkpoint_serializes_windows_paths_in_settings(tmp_path: Path) -> None:
    source = tmp_path / "1.jpg"
    subtitle = tmp_path / "captions.srt"
    source.write_bytes(b"source-image")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nテスト\n", encoding="utf-8")
    media = [_media(source)]
    settings = RenderSettings(
        source_dir=tmp_path,
        output_dir=tmp_path / "output",
        subtitle_path=subtitle,
        burn_subtitles=True,
        rights_confirmed=True,
        editorial_intent="test",
    )
    plan = build_render_plan(
        media,
        settings,
        seed="abcdefabcdefabcdefabcdefabcdefab",
    )
    identity = render_request_identity(media, settings)
    directory = render_checkpoint_dir(media, settings)

    save_render_plan_checkpoint(directory, request_identity=identity, plan=plan)

    document = read_render_checkpoint(directory)
    assert document["plan"]["settings"]["subtitle_path"] == str(subtitle)
    restored = load_render_plan_checkpoint(directory, request_identity=identity)
    assert restored is not None
