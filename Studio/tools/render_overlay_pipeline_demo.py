from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from docurandom.ffmpeg_manager import find_tools
from docurandom.models import RenderSettings
from docurandom.planner import build_render_plan
from docurandom.renderer import render_video
from docurandom.runtime_profile import HostResources, RenderRuntimeProfile
from docurandom.scanner import scan_media


def _software_profile() -> RenderRuntimeProfile:
    return RenderRuntimeProfile(
        host=HostResources(
            logical_cpus=8,
            total_ram_bytes=16 * 1024**3,
            available_ram_bytes=8 * 1024**3,
        ),
        encoder="libx264",
        hardware_accelerated=False,
        recommended_jobs=1,
        threads_per_job=4,
        encoder_label="CPU / x264 demo",
    )


def _logo(path: Path) -> None:
    size = 320
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((12, 12, size - 12, size - 12), fill=(255, 255, 255, 238))
    draw.ellipse((32, 32, size - 32, size - 32), fill=(22, 28, 40, 255))
    font_path = Path("C:/Windows/Fonts/segoeuib.ttf")
    font = ImageFont.truetype(str(font_path), 150)
    label = "D"
    bounds = draw.textbbox((0, 0), label, font=font)
    draw.text(
        (
            (size - (bounds[2] - bounds[0])) / 2,
            (size - (bounds[3] - bounds[1])) / 2 - bounds[1],
        ),
        label,
        font=font,
        fill=(255, 255, 255, 255),
    )
    image.save(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--green-cta", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    tools = find_tools()
    if tools is None:
        raise RuntimeError("Không tìm thấy FFmpeg đã đóng gói.")
    root = args.output_dir.resolve()
    media_dir = root / "media"
    output_dir = root / "output"
    media_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    source = media_dir / f"source{args.source.suffix.casefold()}"
    shutil.copy2(args.source.resolve(), source)
    logo = root / "demo-logo.png"
    _logo(logo)

    black_cta = root / "cta-black-screen.mp4"
    filter_graph = (
        "color=c=black:s=920x240:r=30:d=6[bg];"
        "[0:v]chromakey=0x00FF00:0.17:0.07,despill=type=green:mix=0.25[cta];"
        "[bg][cta]overlay=shortest=1,format=yuv420p[v]"
    )
    import subprocess

    subprocess.run(
        [
            str(tools.ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(args.green_cta.resolve()),
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-an",
            "-c:v",
            "libx264",
            "-crf",
            "8",
            "-pix_fmt",
            "yuv420p",
            str(black_cta),
        ],
        check=True,
    )

    media, warnings = scan_media(media_dir, ffprobe_path=tools.ffprobe)
    if warnings or len(media) != 1:
        raise RuntimeError(f"Media demo không hợp lệ: {warnings}")
    settings = RenderSettings(
        source_dir=media_dir,
        output_dir=output_dir,
        width=1280,
        height=720,
        fps=30,
        duration_min=6.0,
        duration_max=6.0,
        framing="blur",
        intensity="light",
        transition_pack="documentary",
        effect_density="low",
        randomize_order=False,
        output_name="CTA_Logo_Pipeline_Demo",
        cta_enabled=True,
        cta_path=black_cta,
        cta_chroma_key=True,
        cta_key_mode="auto",
        cta_x_percent=3.0,
        cta_y_percent=4.0,
        cta_width_percent=28.0,
        cta_start_seconds=0.35,
        logo_enabled=True,
        logo_path=logo,
        logo_x_percent=97.0,
        logo_y_percent=4.0,
        logo_width_percent=8.0,
        logo_opacity=0.58,
    )
    plan = build_render_plan(
        media,
        settings,
        seed="c7a10c7a10c7a10c7a10c7a10c7a10c",
    )
    result = render_video(
        media,
        settings,
        plan,
        tools=tools,
        runtime_profile=_software_profile(),
    )
    preview = root / "pipeline-demo-preview.png"
    subprocess.run(
        [
            str(tools.ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "3.2",
            "-i",
            str(result.video_path),
            "-frames:v",
            "1",
            str(preview),
        ],
        check=True,
    )
    print(result.video_path)


if __name__ == "__main__":
    main()
