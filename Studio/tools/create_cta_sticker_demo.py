from __future__ import annotations

import argparse
import math
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


FPS = 30
DURATION = 6.0
GREEN = (0, 255, 0, 255)
CANVAS = (920, 240)
SCALE = 3


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def smoothstep(value: float) -> float:
    value = clamp(value)
    return value * value * (3.0 - 2.0 * value)


def ease_out_back(value: float) -> float:
    value = clamp(value)
    c1 = 1.70158
    c3 = c1 + 1.0
    return 1.0 + c3 * (value - 1.0) ** 3 + c1 * (value - 1.0) ** 2


@lru_cache(maxsize=8)
def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "segoeuib.ttf" if bold else "segoeui.ttf"
    path = Path("C:/Windows/Fonts") / name
    return ImageFont.truetype(str(path), size * SCALE)


def rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int, fill, outline=None, width=1):
    draw.rounded_rectangle(
        tuple(v * SCALE for v in box),
        radius=radius * SCALE,
        fill=fill,
        outline=outline,
        width=width * SCALE,
    )


def text_center(draw: ImageDraw.ImageDraw, box, label: str, font, fill):
    x1, y1, x2, y2 = (v * SCALE for v in box)
    bounds = draw.textbbox((0, 0), label, font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    draw.text(((x1 + x2 - width) / 2, (y1 + y2 - height) / 2 - bounds[1]), label, font=font, fill=fill)


def draw_thumb(draw: ImageDraw.ImageDraw, cx: int, cy: int, color, scale: float = 1.0):
    offsets = [
        (-17, 15),
        (-17, -8),
        (-5, -8),
        (2, -27),
        (10, -25),
        (10, -10),
        (25, -10),
        (29, -4),
        (22, 17),
        (-5, 17),
    ]
    points = [
        (int((cx + dx * scale) * SCALE), int((cy + dy * scale) * SCALE))
        for dx, dy in offsets
    ]
    draw.polygon(points, fill=color)
    draw.rounded_rectangle(
        (
            int((cx - 31 * scale) * SCALE),
            int((cy - 9 * scale) * SCALE),
            int((cx - 20 * scale) * SCALE),
            int((cy + 18 * scale) * SCALE),
        ),
        radius=max(1, int(3 * scale * SCALE)),
        fill=color,
    )


def draw_bell(draw: ImageDraw.ImageDraw, cx: int, cy: int, color, ring: float):
    angle = math.sin(ring * math.pi * 5.0) * (1.0 - ring) * 0.32 if ring < 1 else 0.0
    bell = Image.new("RGBA", (90 * SCALE, 90 * SCALE), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bell)
    bd.pieslice((20 * SCALE, 14 * SCALE, 70 * SCALE, 70 * SCALE), 180, 360, fill=color)
    bd.rectangle((20 * SCALE, 41 * SCALE, 70 * SCALE, 62 * SCALE), fill=color)
    bd.rounded_rectangle((14 * SCALE, 60 * SCALE, 76 * SCALE, 69 * SCALE), radius=4 * SCALE, fill=color)
    bd.ellipse((39 * SCALE, 68 * SCALE, 51 * SCALE, 80 * SCALE), fill=color)
    rotated = bell.rotate(math.degrees(angle), resample=Image.Resampling.BICUBIC, center=(45 * SCALE, 48 * SCALE))
    draw._image.alpha_composite(rotated, ((cx - 45) * SCALE, (cy - 45) * SCALE))


def draw_cursor(draw: ImageDraw.ImageDraw, x: float, y: float, pressed: bool):
    offset = 3 if pressed else 0
    x = int(x + offset)
    y = int(y + offset)
    points = [(x, y), (x + 2, y + 39), (x + 12, y + 29), (x + 21, y + 49), (x + 31, y + 44), (x + 21, y + 25), (x + 36, y + 23)]
    scaled = [(px * SCALE, py * SCALE) for px, py in points]
    draw.polygon(scaled, fill=(20, 24, 32, 255))
    inner = [(x + 3, y + 4), (x + 5, y + 31), (x + 13, y + 23), (x + 22, y + 42), (x + 26, y + 40), (x + 17, y + 20), (x + 29, y + 19)]
    draw.polygon([(px * SCALE, py * SCALE) for px, py in inner], fill=(255, 255, 255, 255))


def draw_sparkles(draw: ImageDraw.ImageDraw, cx: int, cy: int, progress: float, color):
    if not 0.0 < progress < 1.0:
        return
    radius = 26 + 42 * progress
    alpha = int(255 * (1.0 - progress))
    for i in range(8):
        angle = i * math.pi / 4
        x = (cx + math.cos(angle) * radius) * SCALE
        y = (cy + math.sin(angle) * radius) * SCALE
        r = max(2, int(5 * (1.0 - progress))) * SCALE
        rgba = (*color[:3], alpha)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=rgba)


def draw_frame(t: float) -> Image.Image:
    frame = Image.new("RGBA", (CANVAS[0] * SCALE, CANVAS[1] * SCALE), GREEN)

    appear = ease_out_back((t - 0.35) / 0.55)
    disappear = smoothstep((t - 5.15) / 0.55)
    visibility = clamp(appear) * (1.0 - disappear)
    if visibility <= 0:
        return frame.resize(CANVAS, Image.Resampling.LANCZOS).convert("RGB")

    slide_x = int((-720 * (1.0 - clamp(appear))) - 760 * disappear)
    card_y = 48
    pulse_like = 1.0 + 0.11 * math.sin(clamp((t - 1.43) / 0.26) * math.pi) if 1.43 <= t <= 1.69 else 1.0
    liked = t >= 1.55
    subscribed = t >= 3.0

    sticker = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    shadow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    rounded(sd, (40 + slide_x, card_y + 10, 880 + slide_x, card_y + 154), 42, (0, 0, 0, int(115 * visibility)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(16 * SCALE))
    frame.alpha_composite(shadow)

    draw = ImageDraw.Draw(sticker)
    rounded(draw, (40 + slide_x, card_y, 880 + slide_x, card_y + 144), 42, (250, 251, 253, 255), (255, 255, 255, 255), 2)

    like_box = (62 + slide_x, card_y + 20, 300 + slide_x, card_y + 124)
    if liked:
        rounded(draw, like_box, 32, (37, 99, 235, 255))
        thumb_color = (255, 255, 255, 255)
        label_color = (255, 255, 255, 255)
        like_label = "LIKED"
    else:
        rounded(draw, like_box, 32, (235, 241, 255, 255), (201, 214, 250, 255), 2)
        thumb_color = (37, 99, 235, 255)
        label_color = (29, 78, 216, 255)
        like_label = "LIKE"

    icon_x = int((112 + slide_x) * pulse_like)
    if pulse_like != 1.0:
        icon_x = 112 + slide_x
    draw_thumb(draw, icon_x, card_y + 72, thumb_color, pulse_like)
    draw.text(((151 + slide_x) * SCALE, (card_y + 44) * SCALE), like_label, font=load_font(31, True), fill=label_color)

    sub_box = (320 + slide_x, card_y + 20, 858 + slide_x, card_y + 124)
    if subscribed:
        rounded(draw, sub_box, 32, (31, 41, 55, 255))
        draw_bell(draw, 374 + slide_x, card_y + 72, (255, 255, 255, 255), clamp((t - 3.0) / 0.85))
        draw.text(((424 + slide_x) * SCALE, (card_y + 44) * SCALE), "SUBSCRIBED", font=load_font(31, True), fill=(255, 255, 255, 255))
        draw.ellipse(((785 + slide_x) * SCALE, (card_y + 54) * SCALE, (806 + slide_x) * SCALE, (card_y + 75) * SCALE), fill=(34, 197, 94, 255))
    else:
        rounded(draw, sub_box, 32, (225, 29, 45, 255))
        play = [(365 + slide_x, card_y + 49), (365 + slide_x, card_y + 94), (406 + slide_x, card_y + 72)]
        draw.polygon([(x * SCALE, y * SCALE) for x, y in play], fill=(255, 255, 255, 255))
        draw.text(((432 + slide_x) * SCALE, (card_y + 44) * SCALE), "SUBSCRIBE", font=load_font(31, True), fill=(255, 255, 255, 255))

    like_burst = clamp((t - 1.52) / 0.42)
    sub_burst = clamp((t - 2.98) / 0.42)
    draw_sparkles(draw, 180 + slide_x, card_y + 72, like_burst, (59, 130, 246, 255))
    draw_sparkles(draw, 610 + slide_x, card_y + 72, sub_burst, (239, 68, 68, 255))

    if 0.9 <= t < 2.1:
        cursor_p = smoothstep((t - 0.9) / 0.42)
        cx = 210 + slide_x - 80 * (1.0 - cursor_p)
        cy = card_y + 94 + 50 * (1.0 - cursor_p)
        draw_cursor(draw, cx, cy, 1.43 <= t <= 1.61)
    elif 2.1 <= t < 3.55:
        cursor_p = smoothstep((t - 2.1) / 0.55)
        cx = 655 + slide_x - 330 * (1.0 - cursor_p)
        cy = card_y + 94 + 45 * (1.0 - cursor_p)
        draw_cursor(draw, cx, cy, 2.86 <= t <= 3.05)

    if visibility < 1.0:
        alpha = sticker.getchannel("A").point(lambda p: int(p * visibility))
        sticker.putalpha(alpha)
    frame.alpha_composite(sticker)
    return frame.resize(CANVAS, Image.Resampling.LANCZOS).convert("RGB")


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a green-screen CTA and a composited DocuRandom demo.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    green_video = output_dir / "cta-green-screen.mp4"
    final_video = output_dir / "cta-overlay-demo.mp4"
    preview = output_dir / "cta-overlay-preview.png"

    with tempfile.TemporaryDirectory(prefix="docurandom-cta-") as temp:
        frames = Path(temp)
        for index in range(round(FPS * DURATION)):
            draw_frame(index / FPS).save(frames / f"cta-{index:04d}.png", optimize=True)

        run([
            str(args.ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-framerate", str(FPS), "-i", str(frames / "cta-%04d.png"),
            "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "4",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(green_video),
        ])

    filter_graph = (
        "[1:v]chromakey=0x00FF00:0.17:0.07,"
        "despill=type=green:mix=0.25,format=rgba,scale=460:-2[cta];"
        "[0:v][cta]overlay=x=72:y=72:shortest=1:format=auto[v]"
    )
    run([
        str(args.ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", "0.5", "-t", str(DURATION), "-i", str(args.source.resolve()),
        "-i", str(green_video),
        "-filter_complex", filter_graph,
        "-map", "[v]", "-map", "0:a?", "-t", str(DURATION),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final_video),
    ])
    run([
        str(args.ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", "3.15", "-i", str(final_video), "-frames:v", "1", str(preview),
    ])
    print(final_video)


if __name__ == "__main__":
    main()
