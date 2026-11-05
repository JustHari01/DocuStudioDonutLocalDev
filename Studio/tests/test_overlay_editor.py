import subprocess
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from docurandom.ffmpeg_manager import find_tools
from docurandom.overlay_editor import (
    animated_preview_command,
    compose_video_preview,
    detect_background_key,
    fit_aspect_size,
    remove_keyed_background,
)


def test_preview_matches_blurred_background_and_smaller_foreground_frame() -> None:
    source = Image.new("RGB", (640, 360), (220, 40, 30))

    preview = compose_video_preview(source, (320, 180), "blur")

    assert preview.size == (320, 180)
    assert preview.getpixel((160, 90))[0] > preview.getpixel((3, 3))[0]


def test_cta_background_detection_handles_green_black_and_alpha() -> None:
    green = Image.new("RGBA", (160, 80), (0, 255, 0, 255))
    black = Image.new("RGBA", (160, 80), (0, 0, 0, 255))
    transparent = Image.new("RGBA", (160, 80), (255, 255, 255, 0))

    assert detect_background_key(green) == "green"
    assert detect_background_key(black) == "black"
    assert detect_background_key(transparent) == "none"


def test_auto_key_makes_green_and_black_borders_transparent() -> None:
    for color, expected in (((0, 255, 0, 255), "green"), ((0, 0, 0, 255), "black")):
        image = Image.new("RGBA", (120, 60), color)
        for x in range(35, 85):
            for y in range(20, 40):
                image.putpixel((x, y), (255, 255, 255, 255))

        keyed, detected = remove_keyed_background(image, "auto")

        assert detected == expected
        assert keyed.getpixel((0, 0))[3] == 0
        assert keyed.getpixel((60, 30))[3] == 255


def test_preview_canvas_scales_without_changing_video_ratio() -> None:
    assert fit_aspect_size((900, 600), (1920, 1080), inset=10) == (880, 495)
    assert fit_aspect_size((540, 900), (1080, 1920), inset=10) == (495, 880)
    assert fit_aspect_size((620, 620), (1080, 1080), inset=10) == (600, 600)


@pytest.mark.skipif(find_tools() is None, reason="FFmpeg is required")
def test_animated_preview_command_streams_the_full_cta_without_time_cutoff() -> None:
    tools = find_tools()
    assert tools is not None
    with tempfile.TemporaryDirectory(prefix="DocuRandomCtaPreview_") as temp:
        video = Path(temp) / "cta.mp4"
        subprocess.run(
            [
                str(tools.ffmpeg),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x00FF00:s=160x90:r=8:d=1.5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ],
            check=True,
        )

        command, size, effective = animated_preview_command(
            video,
            tools.ffmpeg,
            "auto",
        )

        assert "-t" not in command
        assert size == (160, 90)
        assert effective == "green"
