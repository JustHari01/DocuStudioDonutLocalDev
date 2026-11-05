from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .ffmpeg_manager import FFmpegTools, ensure_tools
from .models import MediaInfo, RenderPlan, RenderSettings, ScenePlan
from .overlay_timing import resolve_overlay_window, timing_summary


# In the blur framing mode the source is duplicated: the cover-sized copy is
# softened into the background and a slightly smaller, cover-framed copy is
# centred as a fixed picture.  The small margin prevents a hard edge after
# the Flow right-edge crop while retaining the chosen 16:9 canvas.
BLUR_FOREGROUND_SCALE = 0.90
BLUR_BACKGROUND_OVERSCAN = 1.32
BLUR_BACKGROUND_OSCILLATION = 0.22
from .render_checkpoint import update_render_checkpoint_state
from .runtime_profile import (
    RenderRuntimeProfile,
    detect_runtime_profile,
    encoder_args,
    ffmpeg_thread_args,
)
from .storage import atomic_write_json, sha256_file
from .subtitles import (
    prepare_ass_for_render,
    subtitle_force_style,
    subtitle_line_units,
    subtitle_panel_geometry,
)


ProgressCallback = Callable[[str, float, str], None]
LogCallback = Callable[[str], None]
MAX_MEDIA_PER_RENDER = 500
MAX_IMAGES_PER_RENDER = MAX_MEDIA_PER_RENDER  # v1 compatibility
MAX_OUTPUT_NAME_LENGTH = 120
_INVALID_OUTPUT_NAME_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_OUTPUT_PATH_LOCK = Lock()
_RESERVED_OUTPUT_PATHS: set[str] = set()
_SCENE_CHECKPOINT_VERSION = 4
_STATIC_OVERLAY_EXTENSIONS = frozenset(
    {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)


class RenderCancelled(Exception):
    pass


@dataclass(frozen=True)
class RenderResult:
    video_path: Path
    output_sha256: str
    visual_sha256: str
    duration: float
    codec: str


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0
    )


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_width, target_height = size
    scale = max(target_width / image.width, target_height / image.height)
    resized = image.resize(
        (
            max(target_width, round(image.width * scale)),
            max(target_height, round(image.height * scale)),
        ),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - target_width) // 2)
    top = max(0, (resized.height - target_height) // 2)
    return resized.crop((left, top, left + target_width, top + target_height))


def _contain(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    copy = image.copy()
    copy.thumbnail(size, Image.Resampling.LANCZOS)
    return copy


def normalize_image(
    source: Path,
    destination: Path,
    size: tuple[int, int],
    framing: str,
) -> None:
    with Image.open(source) as opened:
        transposed = ImageOps.exif_transpose(opened)
        if transposed.mode in ("RGBA", "LA") or "transparency" in transposed.info:
            rgba = transposed.convert("RGBA")
            base = Image.new("RGBA", rgba.size, (10, 12, 16, 255))
            image = Image.alpha_composite(base, rgba).convert("RGB")
        else:
            image = transposed.convert("RGB")

        if framing in {"blur", "vertical-blur", "vertical-smart"}:
            background = _cover(image, size)
            blur_radius = max(12.0, max(size) * 0.018)
            background = background.filter(ImageFilter.GaussianBlur(blur_radius))
            background = ImageEnhance.Brightness(background).enhance(0.58)
            foreground = (_cover if framing == "blur" else _contain)(
                image,
                (
                    max(2, round(size[0] * BLUR_FOREGROUND_SCALE)),
                    max(2, round(size[1] * BLUR_FOREGROUND_SCALE)),
                ),
            )
            left = (size[0] - foreground.width) // 2
            top = (size[1] - foreground.height) // 2
            background.paste(foreground, (left, top))
            normalized = background
        else:
            normalized = _cover(image, size)

        destination.parent.mkdir(parents=True, exist_ok=True)
        normalized.save(destination, "JPEG", quality=95, subsampling=0, optimize=False)


def _fmt(value: float) -> str:
    return f"{value:.9f}"


def _image_motion_filter(
    scene: ScenePlan,
    *,
    width: int,
    height: int,
    fps: int,
) -> str:
    """Animate image pixels inside a fixed-size viewport.

    The filter always returns exactly ``width`` by ``height``.  It can zoom or
    pan the picture, but the outer foreground rectangle never changes size or
    position on the final canvas.
    """

    denominator = max(1, scene.duration_frames - 1)
    z_delta = scene.zoom_end - scene.zoom_start
    x_delta = scene.x_end - scene.x_start
    y_delta = scene.y_end - scene.y_start
    z_expression = (
        f"{_fmt(scene.zoom_start)}+({_fmt(z_delta)})*min(on/{denominator},1)"
    )
    x_factor = f"{_fmt(scene.x_start)}+({_fmt(x_delta)})*min(on/{denominator},1)"
    y_factor = f"{_fmt(scene.y_start)}+({_fmt(y_delta)})*min(on/{denominator},1)"
    return (
        f"zoompan=z='{z_expression}':"
        f"x='(iw-iw/zoom)*({x_factor})':"
        f"y='(ih-ih/zoom)*({y_factor})':"
        f"d=1:s={width}x{height}:fps={fps}"
    )


def _blur_background_variation(scene: ScenePlan) -> tuple[float, float, float, float]:
    """Derive a stable but scene-specific motion rhythm for the blurred layer."""

    identity = "|".join(
        (
            scene.source_path,
            scene.motion,
            _fmt(scene.zoom_start),
            _fmt(scene.zoom_end),
            scene.effect_name,
            _fmt(scene.hue_shift),
        )
    ).encode("utf-8", errors="surrogatepass")
    bits = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big")
    x_cycles = 0.85 + ((bits >> 0) & 0xFF) / 255.0 * 0.75
    y_cycles = 0.90 + ((bits >> 8) & 0xFF) / 255.0 * 0.80
    x_phase = ((bits >> 16) & 0xFF) / 255.0 * 2.0 * math.pi
    y_phase = ((bits >> 24) & 0xFF) / 255.0 * 2.0 * math.pi
    return x_cycles, y_cycles, x_phase, y_phase


def _effect_filters(scene: ScenePlan) -> list[str]:
    filters = [
        (
            f"eq=brightness={_fmt(scene.brightness)}:"
            f"contrast={_fmt(scene.contrast)}:"
            f"saturation={_fmt(scene.saturation)}:"
            f"gamma={_fmt(scene.gamma)}"
        ),
        (
            f"colorchannelmixer=rr={_fmt(scene.red_gain)}:"
            f"gg={_fmt(scene.green_gain)}:bb={_fmt(scene.blue_gain)}"
        ),
    ]
    name = scene.effect_name
    if name == "warm_documentary":
        filters.append(
            "colorbalance=rs=0.025:bs=-0.018:rm=0.012:bm=-0.010:"
            "rh=0.018:bh=-0.012:pl=1"
        )
    elif name == "cool_reportage":
        filters.append(
            "colorbalance=rs=-0.018:bs=0.025:rm=-0.010:bm=0.012:"
            "rh=-0.012:bh=0.018:pl=1"
        )
    elif name == "muted_film":
        filters.extend(("hue=s=0.820", "curves=preset=medium_contrast"))
    elif name == "medium_contrast":
        filters.append("curves=preset=medium_contrast")
    elif name == "soft_focus":
        filters.append("gblur=sigma=0.450:steps=1")
    elif name == "vibrant_travel":
        filters.append(f"vibrance=intensity={_fmt(max(0.12, scene.vibrance))}")
    elif name == "faded_print":
        filters.extend(("curves=preset=lighter", "hue=s=0.880"))
    elif name == "archival_warmth":
        filters.extend(
            (
                "colorbalance=rs=0.030:bs=-0.022:rm=0.012:bm=-0.010:pl=1",
                "hue=s=0.900",
            )
        )
    elif name == "silver_retention":
        filters.extend(("hue=s=0.640", "curves=preset=medium_contrast"))
    elif name == "soft_newsprint":
        filters.extend(
            (
                "hue=s=0.780",
                "curves=preset=lighter",
                "gblur=sigma=0.220:steps=1",
            )
        )
    elif name == "neutral_film":
        filters.extend(
            (
                "curves=preset=medium_contrast",
                "colorbalance=rs=0.008:bs=-0.006:bh=0.008:pl=1",
            )
        )
    elif name == "bleach_bypass":
        filters.extend(("hue=s=0.720", "curves=preset=medium_contrast"))
    elif name == "broadcast_documentary":
        filters.append(
            "colorbalance=rs=0.012:bs=-0.008:rm=-0.006:bm=0.010:"
            "rh=-0.008:bh=0.014:pl=1"
        )
    elif name == "vintage_curve":
        filters.append("curves=preset=vintage")
    elif name == "sepia_archive":
        filters.append("colorize=hue=32:saturation=0.22:lightness=0.48:mix=0.85")
    elif name == "monochrome_news":
        filters.extend(("hue=s=0", "curves=preset=medium_contrast"))
    elif name == "noir_contrast":
        filters.extend(("hue=s=0", "curves=preset=strong_contrast"))
    elif name == "teal_orange":
        filters.append(
            "colorbalance=bs=0.035:rs=-0.018:bm=0.010:rm=0.006:"
            "rh=0.035:bh=-0.022:pl=1"
        )
    elif name == "lens_documentary":
        filters.append(
            f"lenscorrection=cx=0.5:cy=0.5:k1={_fmt(scene.lens_k1)}:"
            f"k2={_fmt(scene.lens_k2)}:i=bilinear:fc=black"
        )
    elif name == "cross_process":
        filters.append("curves=preset=cross_process")
    elif name == "vhs_chroma":
        shift = scene.chroma_shift or 2
        filters.append(f"chromashift=cbh={shift}:crh={-shift}:edge=smear")
    elif name == "motion_trail":
        filters.append("tmix=frames=2:weights='1 0.12':scale=0")
    elif name == "pixel_pulse":
        pulse_duration = min(0.28, max(0.10, scene.duration_seconds * 0.08))
        pulse_start = max(0.05, scene.duration_seconds * scene.effect_mix * 0.75)
        pulse_end = min(scene.duration_seconds - 0.02, pulse_start + pulse_duration)
        if pulse_end > pulse_start:
            filters.append(
                "pixelize=w=4:h=4:mode=avg:"
                f"enable='between(t,{_fmt(pulse_start)},{_fmt(pulse_end)})'"
            )
    if scene.curve_preset != "none" and name not in {
        "medium_contrast",
        "faded_print",
        "vintage_curve",
        "noir_contrast",
        "cross_process",
        "silver_retention",
        "soft_newsprint",
        "neutral_film",
        "bleach_bypass",
    }:
        filters.append(f"curves=preset={scene.curve_preset}")
    if name not in {"monochrome_news", "noir_contrast", "sepia_archive"}:
        if abs(scene.hue_shift) >= 1.0:
            filters.append(f"hue=h={_fmt(scene.hue_shift)}")
        if abs(scene.vibrance) >= 0.08 and name != "vibrant_travel":
            filters.append(f"vibrance=intensity={_fmt(scene.vibrance)}")
    filters.append(f"unsharp=5:5:{_fmt(scene.sharpen)}:5:5:0.0")
    return filters


def build_scene_filter_graph(scene: ScenePlan, settings: RenderSettings) -> str:
    if (settings.framing == "vertical-smart" and scene.source_width > 0
            and scene.source_height > 0
            and abs(scene.source_width / scene.source_height - settings.width / settings.height) < 0.01):
        # Matching aspect needs no duplicated backdrop. Uncertain/portrait
        # framing conservatively retains every source pixel on a blur canvas.
        settings = replace(settings, framing="fill")
    prefix: list[str] = []
    if scene.media_type == "video":
        prefix.extend(
            [
                f"setpts=(PTS-STARTPTS)/{_fmt(scene.playback_rate)}",
                f"fps={settings.fps}:start_time=0:round=near",
                f"tpad=stop_mode=clone:stop_duration={_fmt(scene.duration_seconds + 0.25)}",
                f"trim=end_frame={scene.duration_frames}",
                "setpts=N",
            ]
        )
    if settings.framing in {"blur", "vertical-blur", "vertical-smart"}:
        common = ",".join(prefix)
        source_filters = f"{common}," if common else ""
        background_width = max(2, round(settings.width * BLUR_BACKGROUND_OVERSCAN))
        background_height = max(2, round(settings.height * BLUR_BACKGROUND_OVERSCAN))
        duration = _fmt(max(0.10, scene.duration_seconds))
        background_progress = f"min(t/{duration},1)"
        background_base_x = (
            f"{_fmt(scene.x_start)}+({_fmt(scene.x_end - scene.x_start)})*"
            f"{background_progress}"
        )
        background_base_y = (
            f"{_fmt(scene.y_start)}+({_fmt(scene.y_end - scene.y_start)})*"
            f"{background_progress}"
        )
        # The blurred duplicate is decorative, so it can move much more than
        # the readable foreground. A full oscillation plus the planned pan
        # makes motion visible even for centre-push scenes and still images.
        background_wave = f"2*PI*t/{duration}"
        x_cycles, y_cycles, x_phase, y_phase = _blur_background_variation(scene)
        oscillation = _fmt(BLUR_BACKGROUND_OSCILLATION)
        background_x = (
            f"max(0,min(1,({background_base_x})+{oscillation}*"
            f"sin({_fmt(x_cycles)}*({background_wave})+{_fmt(x_phase)})))"
        )
        background_y = (
            f"max(0,min(1,({background_base_y})+{oscillation}*"
            f"cos({_fmt(y_cycles)}*({background_wave})+{_fmt(y_phase)})))"
        )
        # Never run zoompan after compositing: that would move the frame and
        # background together.  Still images may move inside the fixed-size
        # foreground branch below; source video keeps its natural motion.
        composite_chain = ",".join(
            [
                f"trim=end_frame={scene.duration_frames}",
                "setsar=1",
                f"settb=expr=1/{settings.fps}",
                "setpts=N",
                "scale=in_range=auto:out_range=tv",
                "format=yuv420p",
            ]
        )
        blur_sigma = max(18, round(max(settings.width, settings.height) * 0.018))
        foreground_width = max(2, round(settings.width * BLUR_FOREGROUND_SCALE))
        foreground_height = max(2, round(settings.height * BLUR_FOREGROUND_SCALE))
        foreground_filters = [
            f"scale={foreground_width}:{foreground_height}:"
            + ("force_original_aspect_ratio=increase:flags=lanczos"
               if settings.framing == "blur" else
               "force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos"),
        ]
        if settings.framing == "blur":
            foreground_filters.append(f"crop={foreground_width}:{foreground_height}")
        if scene.media_type != "video" and settings.framing == "blur":
            foreground_filters.append(
                _image_motion_filter(
                    scene,
                    width=foreground_width,
                    height=foreground_height,
                    fps=settings.fps,
                )
            )
        # Random grading belongs to the framed media only.  The blurred
        # backdrop remains a neutral decorative layer and subtitle rendering
        # happens later, after all scene/timeline effects.
        foreground_filters.extend(_effect_filters(scene))
        foreground_filters.extend(
            (
                "setsar=1",
                f"settb=expr=1/{settings.fps}",
                "setpts=N",
                "format=yuv420p",
            )
        )
        foreground_chain = ",".join(foreground_filters)
        return (
            f"[0:v]{source_filters}split=2[bg][fg];\n"
            f"[bg]scale={background_width}:{background_height}:"
            "force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={settings.width}:{settings.height}:"
            f"x='(in_w-out_w)*({background_x})':"
            f"y='(in_h-out_h)*({background_y})',"
            f"gblur=sigma={blur_sigma}[bg2];\n"
            f"[fg]{foreground_chain}[fg2];\n"
            f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2:shortest=1,{composite_chain}[vout]"
        )
    if scene.media_type == "video":
        prefix.extend(
            [
                f"scale={settings.width}:{settings.height}:"
                "force_original_aspect_ratio=increase:flags=lanczos",
                f"crop={settings.width}:{settings.height}",
            ]
        )

    # Fill framing uses the full canvas as its fixed viewport.  Still-image
    # pixels may move inside it; source video keeps only its natural motion.
    if scene.media_type != "video":
        prefix.extend(
            [
                f"fps={settings.fps}:start_time=0:round=near",
                f"scale={settings.width}:{settings.height}:"
                "force_original_aspect_ratio=increase:flags=lanczos",
                f"crop={settings.width}:{settings.height}",
                _image_motion_filter(
                    scene,
                    width=settings.width,
                    height=settings.height,
                    fps=settings.fps,
                ),
            ]
        )
    filters = [
        *prefix,
        *_effect_filters(scene),
        f"trim=end_frame={scene.duration_frames}",
        "setsar=1",
        f"settb=expr=1/{settings.fps}",
        "setpts=N",
        "scale=in_range=auto:out_range=tv",
        "format=yuv420p",
    ]
    return f"[0:v]{','.join(filters)}[vout]"


def build_filter_graph(
    plan: RenderPlan,
    settings: RenderSettings,
    *,
    subtitle_filename: str = "",
) -> str:
    filters: list[str] = []
    for index in range(len(plan.scenes)):
        filters.append(
            f"[{index}:v]fps={settings.fps},setsar=1,"
            f"settb=expr=1/{settings.fps},setpts=N,format=yuv420p[scene{index}]"
        )

    current_label = "scene0"
    current_frames = plan.scenes[0].duration_frames
    for index, transition in enumerate(plan.transitions, start=1):
        offset_frames = current_frames - transition.duration_frames
        if offset_frames < 0:
            raise ValueError("Transition dài hơn timeline hiện tại.")
        output_label = f"mix{index}"
        filters.append(
            f"[{current_label}][scene{index}]"
            f"xfade=transition={transition.name}:"
            f"duration={_fmt(transition.duration_frames / settings.fps)}:"
            f"offset={_fmt(offset_frames / settings.fps)}[{output_label}]"
        )
        current_label = output_label
        current_frames += (
            plan.scenes[index].duration_frames - transition.duration_frames
        )

    expected_frames = round(plan.expected_duration * settings.fps)
    if current_frames != expected_frames:
        raise ValueError(
            f"Timeline không khớp frame: {current_frames} != {expected_frames}"
        )
    fade_out_start = max(0.0, plan.expected_duration - plan.fade_out_seconds)
    finishing_filters = [
        f"trim=end_frame={expected_frames}",
        f"settb=expr=1/{settings.fps}",
        "setpts=N",
        f"vignette=angle=PI/{_fmt(plan.vignette_divisor)}:eval=frame",
        f"noise=alls={round(plan.grain_strength)}:allf=t+u:all_seed={plan.grain_seed}",
        (
            f"eq=brightness='{_fmt(plan.flicker_amount)}*"
            f"sin(2*PI*t*{_fmt(plan.flicker_frequency)}+{_fmt(plan.flicker_phase)})':"
            "eval=frame"
        ),
        f"fade=t=in:st=0:d={_fmt(plan.fade_in_seconds)}",
        f"fade=t=out:st={_fmt(fade_out_start)}:d={_fmt(plan.fade_out_seconds)}",
    ]
    if subtitle_filename:
        foreground_scale = (
            BLUR_FOREGROUND_SCALE if settings.framing == "blur" else 1.0
        )
        panel_x, panel_y, panel_width, panel_height = subtitle_panel_geometry(
            settings.width,
            settings.height,
            foreground_scale=foreground_scale,
        )
        finishing_filters.append(
            "drawbox="
            f"x={panel_x}:y={panel_y}:w={panel_width}:h={panel_height}:"
            "color=black@0.42:t=fill"
        )
        style = subtitle_force_style(settings.width, settings.height)
        finishing_filters.append(
            f"subtitles=filename='{subtitle_filename}':force_style='{style}'"
        )
    filters.append(f"[{current_label}]{','.join(finishing_filters)}[finished]")

    next_input_index = len(plan.scenes)
    audio_input_index: int | None = None
    if settings.audio_path:
        audio_input_index = next_input_index
        next_input_index += 1
    cta_input_index: int | None = None
    if settings.cta_enabled and settings.cta_path is not None:
        cta_input_index = next_input_index
        next_input_index += 1
    logo_input_index: int | None = None
    if settings.logo_enabled and settings.logo_path is not None:
        logo_input_index = next_input_index

    overlay_label = "finished"
    if cta_input_index is not None:
        cta_window = resolve_overlay_window(
            settings.cta_timing_mode,
            start_seconds=settings.cta_start_seconds,
            end_seconds=settings.cta_end_seconds,
            duration_seconds=settings.cta_duration_seconds,
            total_duration=plan.expected_duration,
            allow_once=True,
        )
        cta_width = max(
            2,
            round(settings.width * max(1.0, min(100.0, settings.cta_width_percent)) / 200.0)
            * 2,
        )
        cta_chain = [
            f"setpts=PTS-STARTPTS+{_fmt(cta_window.start_seconds)}/TB"
        ]
        key_mode = str(settings.cta_key_mode or "auto").casefold()
        if settings.cta_chroma_key and key_mode == "black":
            cta_chain.append(
                "colorkey=color=0x000000:similarity=0.10:blend=0.06"
            )
        elif settings.cta_chroma_key and key_mode in {"auto", "green"}:
            cta_chain.extend(
                (
                    "chromakey=color=0x00FF00:similarity=0.17:blend=0.07",
                    "despill=type=green:mix=0.25",
                )
            )
        cta_chain.extend(
            (
                f"scale=w={cta_width}:h=-2:flags=lanczos",
                "format=rgba",
            )
        )
        filters.append(f"[{cta_input_index}:v]{','.join(cta_chain)}[cta]")
        cta_enable = (
            ""
            if cta_window.end_seconds is None
            else (
                ":enable='between(t,"
                f"{_fmt(cta_window.start_seconds)},{_fmt(cta_window.end_seconds)})'"
            )
        )
        filters.append(
            f"[{overlay_label}][cta]overlay="
            f"x=(W-w)*{_fmt(clamp_percent(settings.cta_x_percent) / 100.0)}:"
            f"y=(H-h)*{_fmt(clamp_percent(settings.cta_y_percent) / 100.0)}"
            f"{cta_enable}:eof_action=pass:repeatlast=0:shortest=0[withcta]"
        )
        overlay_label = "withcta"
    if logo_input_index is not None:
        logo_window = resolve_overlay_window(
            settings.logo_timing_mode,
            start_seconds=settings.logo_start_seconds,
            end_seconds=settings.logo_end_seconds,
            duration_seconds=settings.logo_duration_seconds,
            total_duration=plan.expected_duration,
            allow_once=False,
        )
        logo_width = max(
            2,
            round(settings.width * max(1.0, min(100.0, settings.logo_width_percent)) / 200.0)
            * 2,
        )
        filters.append(
            f"[{logo_input_index}:v]setpts=PTS-STARTPTS+{_fmt(logo_window.start_seconds)}/TB,"
            f"scale=w={logo_width}:h=-2:flags=lanczos,format=rgba,"
            f"colorchannelmixer=aa={_fmt(max(0.0, min(1.0, settings.logo_opacity)))}[logo]"
        )
        logo_enable = (
            ""
            if logo_window.end_seconds is None
            else (
                ":enable='between(t,"
                f"{_fmt(logo_window.start_seconds)},{_fmt(logo_window.end_seconds)})'"
            )
        )
        filters.append(
            f"[{overlay_label}][logo]overlay="
            f"x=(W-w)*{_fmt(clamp_percent(settings.logo_x_percent) / 100.0)}:"
            f"y=(H-h)*{_fmt(clamp_percent(settings.logo_y_percent) / 100.0)}"
            f"{logo_enable}:eof_action=pass:repeatlast=0:shortest=0[withlogo]"
        )
        overlay_label = "withlogo"
    filters.append(
        f"[{overlay_label}]setparams=range=tv,format=yuv420p[vout]"
    )

    if settings.audio_path:
        assert audio_input_index is not None
        target_samples = expected_frames * 48_000 // settings.fps
        audio_filters = [
            "aresample=48000:async=1:first_pts=0",
            "aformat=sample_rates=48000:channel_layouts=stereo",
            "asetpts=N/SR/TB",
            f"apad=whole_len={target_samples}",
            f"atrim=end_sample={target_samples}",
            f"volume={_fmt(settings.audio_volume)}",
        ]
        fade_duration = min(
            max(0.0, settings.audio_fade_seconds),
            plan.expected_duration * 0.25,
        )
        if fade_duration > 0:
            fade_start = max(0.0, plan.expected_duration - fade_duration)
            audio_filters.extend(
                [
                    f"afade=t=in:st=0:d={_fmt(fade_duration)}",
                    f"afade=t=out:st={_fmt(fade_start)}:d={_fmt(fade_duration)}",
                ]
            )
        audio_filters.append("asetpts=N/SR/TB")
        filters.append(
            f"[{audio_input_index}:a:0]{','.join(audio_filters)}[aout]"
        )
    return ";\n".join(filters)


def clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _resolved_overlay_path(
    *,
    enabled: bool,
    path: Path | None,
    label: str,
) -> Path | None:
    if not enabled:
        return None
    if path is None:
        raise ValueError(f"Đã bật {label} nhưng chưa chọn tệp.")
    resolved = path.expanduser().resolve(strict=False)
    if not resolved.is_file():
        raise ValueError(f"Không tìm thấy tệp {label}: {resolved}")
    return resolved


def _detect_cta_key_mode(ffmpeg: Path, path: Path) -> str:
    """Inspect the CTA border and choose green, black, or no keying."""

    try:
        if path.suffix.casefold() in _STATIC_OVERLAY_EXTENSIONS:
            with Image.open(path) as opened:
                frame = ImageOps.exif_transpose(opened).convert("RGBA")
        else:
            completed: subprocess.CompletedProcess[bytes] | None = None
            for seek in ("0.250", "0.000"):
                completed = subprocess.run(
                    [
                        str(ffmpeg),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-ss",
                        seek,
                        "-i",
                        str(path),
                        "-frames:v",
                        "1",
                        "-f",
                        "image2pipe",
                        "-vcodec",
                        "png",
                        "pipe:1",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=20,
                    creationflags=_creation_flags(),
                )
                if completed.returncode == 0 and completed.stdout:
                    break
            assert completed is not None
            if completed.returncode != 0 or not completed.stdout:
                return "green"
            with Image.open(io.BytesIO(completed.stdout)) as opened:
                frame = opened.convert("RGBA")
        sample = frame.resize((64, 64), Image.Resampling.BILINEAR)
        border = [
            sample.getpixel((x, y))
            for y in range(64)
            for x in range(64)
            if x < 8 or x >= 56 or y < 8 or y >= 56
        ]
    except Exception:
        return "green"
    if not border:
        return "green"
    total = len(border)
    transparent = sum(alpha < 32 for _red, _green, _blue, alpha in border)
    green = sum(
        alpha >= 32 and value_green > 85 and value_green - max(red, blue) > 45
        for red, value_green, blue, alpha in border
    )
    black = sum(
        alpha >= 32 and max(red, value_green, blue) < 48
        for red, value_green, blue, alpha in border
    )
    if transparent / total >= 0.45:
        return "none"
    if green / total >= 0.45:
        return "green"
    if black / total >= 0.45:
        return "black"
    return "none"


def normalize_output_name(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        return ""
    if name.casefold().endswith(".mp4"):
        name = name[:-4].rstrip()
    if not name:
        raise ValueError("Tên video không được chỉ chứa phần mở rộng .mp4.")
    if len(name) > MAX_OUTPUT_NAME_LENGTH:
        raise ValueError(
            f"Tên video tối đa {MAX_OUTPUT_NAME_LENGTH} ký tự."
        )
    if name.endswith((" ", ".")):
        raise ValueError("Tên video không được kết thúc bằng dấu chấm hoặc khoảng trắng.")
    if any(character in _INVALID_OUTPUT_NAME_CHARS or ord(character) < 32 for character in name):
        raise ValueError('Tên video không được chứa các ký tự: < > : " / \\ | ? *')
    reserved_stem = name.split(".", 1)[0].rstrip(" .").upper()
    if reserved_stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f'"{name}" là tên dành riêng của Windows. Hãy chọn tên khác.')
    if name in {".", ".."}:
        raise ValueError("Tên video không hợp lệ.")
    return name


def numbered_output_name(base_name: str, index: int, total: int) -> str:
    normalized = normalize_output_name(base_name)
    if not normalized or total <= 1:
        return normalized
    if index < 1 or index > total:
        raise ValueError("Chỉ số phiên bản video không hợp lệ.")
    return f"{normalized}_{index}"


def _output_path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _unique_output_path(settings: RenderSettings, render_id: str) -> Path:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    custom_name = normalize_output_name(settings.output_name)
    if custom_name:
        base = custom_name
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"DocuRandom_{timestamp}_{render_id}"
    with _OUTPUT_PATH_LOCK:
        candidate = settings.output_dir / f"{base}.mp4"
        counter = 2
        while candidate.exists() or _output_path_key(candidate) in _RESERVED_OUTPUT_PATHS:
            candidate = settings.output_dir / f"{base}_{counter}.mp4"
            counter += 1
        _RESERVED_OUTPUT_PATHS.add(_output_path_key(candidate))
        return candidate


def _release_output_path(path: Path) -> None:
    with _OUTPUT_PATH_LOCK:
        _RESERVED_OUTPUT_PATHS.discard(_output_path_key(path))


def _verify_output(
    tools: FFmpegTools,
    path: Path,
    settings: RenderSettings,
    expected_duration: float,
    expected_frames: int,
) -> tuple[float, str]:
    command = [
        str(tools.ffprobe),
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,pix_fmt,color_range,"
        "sample_rate,channels,nb_frames,duration:format=duration",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
        creationflags=_creation_flags(),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"FFprobe không đọc được video: {completed.stderr.strip()}")
    try:
        data = json.loads(completed.stdout)
        streams = data["streams"]
        video_stream = next(
            stream for stream in streams if stream.get("codec_type") == "video"
        )
        audio_streams = [
            stream for stream in streams if stream.get("codec_type") == "audio"
        ]
        duration = float(data["format"]["duration"])
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Video đầu ra thiếu thông tin hình ảnh hợp lệ.") from exc

    if int(video_stream.get("width", 0)) != settings.width or int(
        video_stream.get("height", 0)
    ) != settings.height:
        raise RuntimeError("Video đầu ra sai độ phân giải.")
    if video_stream.get("codec_name") != "h264":
        raise RuntimeError("Video đầu ra không dùng codec H.264.")
    if video_stream.get("pix_fmt") != "yuv420p":
        raise RuntimeError(
            f"Video đầu ra sai định dạng pixel ({video_stream.get('pix_fmt')})."
        )
    reported_frames = str(video_stream.get("nb_frames") or "")
    if reported_frames.isdigit() and abs(int(reported_frames) - expected_frames) > 1:
        raise RuntimeError(
            f"Video đầu ra sai số frame ({reported_frames} thay vì {expected_frames})."
        )
    if settings.audio_path:
        if not audio_streams:
            raise RuntimeError("Video đầu ra bị thiếu audio đã chọn.")
        audio_stream = audio_streams[0]
        if audio_stream.get("codec_name") != "aac":
            raise RuntimeError("Audio đầu ra không dùng codec AAC.")
        if int(audio_stream.get("sample_rate") or 0) != 48_000:
            raise RuntimeError("Audio đầu ra không ở sample rate 48 kHz.")
    tolerance = max(0.18, 3 / settings.fps)
    if abs(duration - expected_duration) > tolerance:
        raise RuntimeError(
            f"Thời lượng video sai lệch quá mức ({duration:.3f}s thay vì "
            f"{expected_duration:.3f}s)."
        )
    return duration, str(video_stream.get("codec_name", "h264"))


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _run_ffmpeg(
    command: list[str],
    *,
    cwd: Path,
    expected_duration: float,
    cancel: Event,
    progress_callback: Callable[[float], None],
    log: LogCallback,
    label: str,
) -> None:
    process_done = Event()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=_creation_flags(),
    )

    def watch_for_cancel() -> None:
        while not process_done.wait(0.1):
            if cancel.is_set():
                _terminate_process(process)
                return

    Thread(
        target=watch_for_cancel,
        daemon=True,
        name=f"{label}CancelWatcher",
    ).start()
    last_lines: list[str] = []
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            if cancel.is_set():
                _terminate_process(process)
                raise RenderCancelled()
            line = raw_line.strip()
            if not line:
                continue
            last_lines.append(line)
            last_lines = last_lines[-35:]
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    microseconds = int(line.split("=", 1)[1])
                    ratio = microseconds / 1_000_000 / max(expected_duration, 0.001)
                    progress_callback(min(0.995, max(0.0, ratio)))
                except ValueError:
                    pass
            elif not any(
                line.startswith(prefix)
                for prefix in (
                    "frame=",
                    "fps=",
                    "bitrate=",
                    "total_size=",
                    "out_time=",
                    "dup_frames=",
                    "drop_frames=",
                    "speed=",
                    "progress=",
                    "stream_",
                )
            ):
                log(line)
        process.stdout.close()
        return_code = process.wait()
        process_done.set()
        if cancel.is_set():
            raise RenderCancelled()
        if return_code != 0:
            detail = "\n".join(last_lines[-15:])
            raise RuntimeError(f"FFmpeg thất bại ở bước {label}.\n{detail}")
        progress_callback(1.0)
    finally:
        process_done.set()
        if process.poll() is None:
            _terminate_process(process)


def _filter_complex_file_args(path: str | Path) -> list[str]:
    """Load a complex filtergraph from a file on current FFmpeg releases.

    FFmpeg 9 removed the deprecated ``-filter_complex_script`` spelling.
    The generic option-file form remains compatible with the FFmpeg 8 and 9
    runtimes shipped by DocuRandom Studio.
    """

    return ["-/filter_complex", str(path)]


def _scene_checkpoint_signature(
    item: MediaInfo,
    scene: ScenePlan,
    settings: RenderSettings,
) -> str:
    try:
        source_stat = item.path.stat()
        source_identity: dict[str, object] = {
            "path": str(item.path.resolve(strict=False)),
            "size": source_stat.st_size,
            "mtime_ns": source_stat.st_mtime_ns,
        }
    except OSError:
        source_identity = {
            "path": str(item.path.resolve(strict=False)),
            "missing": True,
        }
    payload = {
        "version": _SCENE_CHECKPOINT_VERSION,
        "source": source_identity,
        "scene": asdict(scene),
        "frame": {
            "width": settings.width,
            "height": settings.height,
            "fps": settings.fps,
            "framing": settings.framing,
        },
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _scene_clip_is_valid(
    tools: FFmpegTools,
    clip_path: Path,
    checkpoint_path: Path,
    signature: str,
    settings: RenderSettings,
    scene: ScenePlan,
    *,
    require_checkpoint: bool = True,
) -> bool:
    if not clip_path.is_file() or clip_path.stat().st_size < 1024:
        return False
    if require_checkpoint:
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if (
            not isinstance(checkpoint, dict)
            or checkpoint.get("version") != _SCENE_CHECKPOINT_VERSION
            or checkpoint.get("signature") != signature
        ):
            return False
    completed = subprocess.run(
        [
            str(tools.ffprobe),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,pix_fmt,nb_frames,duration:format=duration",
            "-of",
            "json",
            str(clip_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        creationflags=_creation_flags(),
    )
    if completed.returncode != 0:
        return False
    try:
        value = json.loads(completed.stdout)
        streams = value.get("streams", [])
        stream = next(
            item for item in streams if item.get("codec_type") == "video"
        )
        duration = float(
            stream.get("duration")
            or value.get("format", {}).get("duration")
            or 0.0
        )
    except (StopIteration, TypeError, ValueError, json.JSONDecodeError):
        return False
    if (
        stream.get("codec_name") != "h264"
        or stream.get("pix_fmt") != "yuv420p"
        or int(stream.get("width", 0)) != settings.width
        or int(stream.get("height", 0)) != settings.height
    ):
        return False
    frames = str(stream.get("nb_frames") or "")
    if frames.isdigit() and abs(int(frames) - scene.duration_frames) > 1:
        return False
    tolerance = max(0.20, 3 / max(1, settings.fps))
    return abs(duration - scene.duration_seconds) <= tolerance


def _visual_sha256(tools: FFmpegTools, path: Path, cancel: Event) -> str:
    command = [
        str(tools.ffmpeg),
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "hash",
        "-hash",
        "sha256",
        "pipe:1",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_creation_flags(),
    )
    started = time.monotonic()
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if cancel.is_set():
                _terminate_process(process)
                raise RenderCancelled()
            if time.monotonic() - started > 3600:
                _terminate_process(process)
                raise RuntimeError("Tạo dấu vân tay hình ảnh quá thời gian cho phép.")
    if process.returncode != 0:
        raise RuntimeError(
            f"Không thể tạo dấu vân tay hình ảnh: {stderr.strip()}"
        )
    for line in stdout.splitlines():
        if line.upper().startswith("SHA256="):
            value = line.split("=", 1)[1].strip().casefold()
            if len(value) == 64 and all(
                character in "0123456789abcdef" for character in value
            ):
                return value
    raise RuntimeError("FFmpeg không trả về dấu vân tay hình ảnh hợp lệ.")


def render_video(
    media: list[MediaInfo],
    settings: RenderSettings,
    plan: RenderPlan,
    *,
    progress_callback: ProgressCallback | None = None,
    log_callback: LogCallback | None = None,
    cancel_event: Event | None = None,
    tools: FFmpegTools | None = None,
    runtime_profile: RenderRuntimeProfile | None = None,
    checkpoint_dir: Path | None = None,
) -> RenderResult:
    progress = progress_callback or (lambda _stage, _value, _message: None)
    log = log_callback or (lambda _message: None)
    cancel = cancel_event or Event()
    part_path: Path | None = None
    render_succeeded = False
    completed_scenes = 0
    if not media:
        raise ValueError("Cần ít nhất một ảnh hoặc video hợp lệ để render.")
    if len(media) > MAX_MEDIA_PER_RENDER:
        raise ValueError(
            f"Một lượt hỗ trợ tối đa {MAX_MEDIA_PER_RENDER} media. Hãy chia "
            "thành nhiều tác vụ."
        )
    if len(media) != len(plan.scenes):
        raise ValueError("Số media không khớp với render plan.")
    media_by_path = {str(item.path): item for item in media}
    ordered_media: list[MediaInfo] = []
    for scene in plan.scenes:
        item = media_by_path.get(scene.source_path)
        if item is None:
            raise ValueError(
                f"Không tìm thấy media trong render plan: {scene.relative_path}"
            )
        ordered_media.append(item)
    cta_path = _resolved_overlay_path(
        enabled=settings.cta_enabled,
        path=settings.cta_path,
        label="CTA",
    )
    logo_path = _resolved_overlay_path(
        enabled=settings.logo_enabled,
        path=settings.logo_path,
        label="logo",
    )
    if logo_path is not None and logo_path.suffix.casefold() not in _STATIC_OVERLAY_EXTENSIONS:
        raise ValueError("Logo phải là ảnh PNG, JPG, WEBP, BMP hoặc TIFF.")
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    persistent_checkpoint = checkpoint_dir is not None
    if checkpoint_dir is not None:
        output_root = settings.output_dir.resolve(strict=False)
        work_dir = checkpoint_dir.expanduser().resolve(strict=False)
        if not work_dir.is_relative_to(output_root) or work_dir == output_root:
            raise ValueError("Checkpoint render phải nằm trong thư mục xuất video.")
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        work_dir = Path(
            tempfile.mkdtemp(prefix=".DocuRandom_work_", dir=str(settings.output_dir))
        )

    try:
        if cancel.is_set():
            raise RenderCancelled()
        progress("tools", 0.0, "Đang kiểm tra FFmpeg/FFprobe…")

        def download_progress(current: int, total: int) -> None:
            ratio = current / total if total else 0.0
            progress("tools", min(0.99, ratio), "Đang tải FFmpeg lần đầu…")

        if tools is None:
            try:
                tools = ensure_tools(
                    status_callback=lambda message: progress("tools", 0.0, message),
                    download_callback=download_progress,
                    cancel_event=cancel,
                )
            except RuntimeError:
                if cancel.is_set():
                    raise RenderCancelled() from None
                raise
        else:
            progress("tools", 1.0, "FFmpeg/FFprobe đã sẵn sàng.")

        if (
            cta_path is not None
            and settings.cta_chroma_key
            and str(settings.cta_key_mode or "auto").casefold() == "auto"
        ):
            detected_key_mode = _detect_cta_key_mode(tools.ffmpeg, cta_path)
            settings = replace(settings, cta_key_mode=detected_key_mode)
            key_labels = {
                "green": "phông xanh",
                "black": "phông đen",
                "none": "nền trong suốt/không cần xóa",
            }
            log(
                "CTA tự nhận diện: "
                f"{key_labels.get(detected_key_mode, detected_key_mode)}"
            )

        if runtime_profile is None:
            runtime_profile = detect_runtime_profile(tools.ffmpeg)
        if persistent_checkpoint:
            update_render_checkpoint_state(
                work_dir,
                status="running",
                completed_scenes=0,
                total_scenes=len(ordered_media),
                detail="Đang kiểm tra các cảnh đã hoàn tất để tiếp tục.",
            )
        log(
            f"Hiệu suất: {runtime_profile.encoder_label} · "
            f"{runtime_profile.threads_per_job} luồng/tác vụ · "
            f"khuyến nghị {runtime_profile.recommended_jobs} tác vụ song song"
        )

        clip_names: list[str] = []
        image_count = sum(item.media_type == "image" for item in ordered_media)
        prepared_images = 0
        progress("prepare", 0.0, "Đang chuẩn bị media…")
        for index, (item, scene) in enumerate(
            zip(ordered_media, plan.scenes, strict=True)
        ):
            if cancel.is_set():
                raise RenderCancelled()
            clip_name = f"clip_{index:04d}.mp4"
            clip_path = work_dir / clip_name
            scene_checkpoint_path = work_dir / f"clip_{index:04d}.checkpoint.json"
            scene_signature = _scene_checkpoint_signature(item, scene, settings)
            if persistent_checkpoint and _scene_clip_is_valid(
                tools,
                clip_path,
                scene_checkpoint_path,
                scene_signature,
                settings,
                scene,
            ):
                if item.media_type == "image":
                    prepared_images += 1
                    progress(
                        "prepare",
                        prepared_images / max(1, image_count),
                        f"Đã phục hồi ảnh {prepared_images}/{image_count} từ checkpoint",
                    )
                completed_scenes += 1
                progress(
                    "scenes",
                    (index + 1) / len(ordered_media),
                    f"Đã phục hồi media {index + 1}/{len(ordered_media)} từ checkpoint…",
                )
                log(
                    f"Media {index + 1}/{len(ordered_media)} · phục hồi clip đã kiểm tra, "
                    "không render lại"
                )
                update_render_checkpoint_state(
                    work_dir,
                    status="running",
                    completed_scenes=completed_scenes,
                    total_scenes=len(ordered_media),
                    detail=f"Đã phục hồi media {index + 1}/{len(ordered_media)}.",
                )
                clip_names.append(clip_name)
                continue
            graph_name = f"scene_{index:04d}.ffscript"
            graph_path = work_dir / graph_name
            graph_path.write_text(
                build_scene_filter_graph(scene, settings),
                encoding="utf-8",
            )
            command = [
                str(tools.ffmpeg),
                "-hide_banner",
                "-y",
                *ffmpeg_thread_args(runtime_profile, complex_filter=True),
            ]
            if item.media_type == "image":
                normalized_name = f"still_{index:04d}.jpg"
                if settings.framing in {"vertical-blur", "vertical-smart"}:
                    # Preserve original aspect until the scene graph fits it.
                    # Pre-covering to 16:9 would permanently lose portrait edges.
                    with Image.open(item.path) as original:
                        prepared = ImageOps.exif_transpose(original).convert("RGB")
                        prepared.thumbnail((max(settings.width, settings.height),) * 2, Image.Resampling.LANCZOS)
                        prepared.save(work_dir / normalized_name, quality=95)
                else:
                    normalize_image(
                    item.path,
                    work_dir / normalized_name,
                    (settings.width, settings.height),
                    "fill" if settings.framing == "blur" else settings.framing,
                    )
                prepared_images += 1
                progress(
                    "prepare",
                    prepared_images / max(1, image_count),
                    f"Đã chuẩn hóa ảnh {prepared_images}/{image_count}",
                )
                command.extend(
                    [
                        "-loop",
                        "1",
                        "-framerate",
                        str(settings.fps),
                        "-i",
                        normalized_name,
                    ]
                )
            else:
                if scene.loop_source:
                    command.extend(["-stream_loop", "-1"])
                if scene.source_start_seconds > 0:
                    command.extend(["-ss", _fmt(scene.source_start_seconds)])
                command.extend(["-i", str(item.path)])

            command.extend(
                [
                    *_filter_complex_file_args(graph_name),
                    "-map",
                    "[vout]",
                    "-an",
                    "-frames:v",
                    str(scene.duration_frames),
                    "-fps_mode:v",
                    "cfr",
                    *encoder_args(runtime_profile, "scene"),
                    "-color_range",
                    "tv",
                    "-color_primaries",
                    "bt709",
                    "-color_trc",
                    "bt709",
                    "-colorspace",
                    "bt709",
                    "-progress",
                    "pipe:1",
                    "-nostats",
                    "-loglevel",
                    "error",
                    clip_name,
                ]
            )
            log(
                f"Media {index + 1}/{len(media)} · {item.media_type.upper()} · "
                f"{scene.color_style} · {scene.effect_name} · "
                f"{scene.duration_seconds:.2f}s"
            )
            _run_ffmpeg(
                command,
                cwd=work_dir,
                expected_duration=scene.duration_seconds,
                cancel=cancel,
                progress_callback=lambda ratio, current=index: progress(
                    "scenes",
                    (current + ratio) / len(media),
                    f"Đang xử lý media {current + 1}/{len(media)}…",
                ),
                log=log,
                label=f"media {index + 1}",
            )
            if not _scene_clip_is_valid(
                tools,
                clip_path,
                scene_checkpoint_path,
                scene_signature,
                settings,
                scene,
                require_checkpoint=False,
            ):
                raise RuntimeError(
                    f"Clip media {index + 1} tạo xong nhưng không vượt qua kiểm tra."
                )
            atomic_write_json(
                scene_checkpoint_path,
                {
                    "version": _SCENE_CHECKPOINT_VERSION,
                    "signature": scene_signature,
                    "clip": clip_name,
                    "frames": scene.duration_frames,
                },
            )
            completed_scenes += 1
            if persistent_checkpoint:
                update_render_checkpoint_state(
                    work_dir,
                    status="running",
                    completed_scenes=completed_scenes,
                    total_scenes=len(ordered_media),
                    detail=f"Đã hoàn tất media {index + 1}/{len(ordered_media)}.",
                )
            clip_names.append(clip_name)

        subtitle_filename = ""
        if settings.burn_subtitles and settings.subtitle_path is not None:
            subtitle_source = settings.subtitle_path.expanduser().resolve(strict=False)
            if not subtitle_source.is_file():
                raise ValueError(f"Không tìm thấy tệp subtitle: {subtitle_source}")
            subtitle_filename = "subtitles_render.ass"
            cue_count = prepare_ass_for_render(
                subtitle_source,
                work_dir / subtitle_filename,
                width=settings.width,
                height=settings.height,
                line_units=subtitle_line_units(settings.width, settings.height),
                foreground_scale=(
                    BLUR_FOREGROUND_SCALE if settings.framing == "blur" else 1.0
                ),
            )
            progress(
                "subtitles",
                1.0,
                f"Đã căn subtitle 2 dòng an toàn · {cue_count} cue.",
            )
            log(
                f"Subtitle: {subtitle_source.name} · {cue_count} cue · "
                "chữ trắng, viền đen, nền tối mờ"
            )

        graph_path = work_dir / "timeline.ffscript"
        graph_path.write_text(
            build_filter_graph(
                plan,
                settings,
                subtitle_filename=subtitle_filename,
            ),
            encoding="utf-8",
        )
        output_path = _unique_output_path(settings, plan.render_id)
        part_path = output_path.with_name(f".{output_path.stem}.part.mp4")
        command = [
            str(tools.ffmpeg),
            "-hide_banner",
            "-y",
            *ffmpeg_thread_args(runtime_profile, complex_filter=True),
        ]
        for clip_name in clip_names:
            command.extend(["-i", clip_name])
        if settings.audio_path:
            command.extend(["-i", str(settings.audio_path)])
        if cta_path is not None:
            cta_window = resolve_overlay_window(
                settings.cta_timing_mode,
                start_seconds=settings.cta_start_seconds,
                end_seconds=settings.cta_end_seconds,
                duration_seconds=settings.cta_duration_seconds,
                total_duration=plan.expected_duration,
                allow_once=True,
            )
            if cta_path.suffix.casefold() in _STATIC_OVERLAY_EXTENSIONS:
                command.extend(
                    ["-loop", "1", "-framerate", str(settings.fps)]
                )
            elif cta_window.loop_media:
                command.extend(["-stream_loop", "-1"])
            command.extend(["-i", str(cta_path)])
            log(
                f"CTA: {cta_path.name} · rộng {settings.cta_width_percent:.1f}% · "
                + timing_summary(
                    settings.cta_timing_mode,
                    start_seconds=settings.cta_start_seconds,
                    end_seconds=settings.cta_end_seconds,
                    duration_seconds=settings.cta_duration_seconds,
                    allow_once=True,
                ).lower()
            )
        if logo_path is not None:
            command.extend(
                [
                    "-loop",
                    "1",
                    "-framerate",
                    str(settings.fps),
                    "-i",
                    str(logo_path),
                ]
            )
            log(
                f"Logo: {logo_path.name} · rộng {settings.logo_width_percent:.1f}% · "
                f"độ rõ {settings.logo_opacity * 100:.0f}% · "
                + timing_summary(
                    settings.logo_timing_mode,
                    start_seconds=settings.logo_start_seconds,
                    end_seconds=settings.logo_end_seconds,
                    duration_seconds=settings.logo_duration_seconds,
                    allow_once=False,
                ).lower()
            )
        expected_frames = round(plan.expected_duration * settings.fps)
        command.extend(
            [
                *_filter_complex_file_args(graph_path.name),
                "-map",
                "[vout]",
            ]
        )
        if settings.audio_path:
            command.extend(
                [
                    "-map",
                    "[aout]",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ar",
                    "48000",
                ]
            )
        else:
            command.append("-an")
        command.extend(
            [
                "-frames:v",
                str(expected_frames),
                "-fps_mode:v",
                "cfr",
                *encoder_args(runtime_profile, "final"),
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
                "-colorspace",
                "bt709",
                "-color_range",
                "tv",
                "-metadata",
                f"comment=DocuRandom {plan.render_id} {plan.manifest_signature}",
                "-progress",
                "pipe:1",
                "-nostats",
                "-loglevel",
                "error",
                str(part_path),
            ]
        )
        if os.name == "nt" and len(subprocess.list2cmdline(command)) >= 30_000:
            raise RuntimeError("Dòng lệnh timeline vượt giới hạn Windows.")

        log(f"Render ID: {plan.render_id}")
        log(f"Phong cách tổng: {plan.style}")
        log(
            f"Timeline: {len(media)} media · {len(plan.transitions)} chuyển cảnh · "
            f"{plan.expected_duration:.3f}s"
        )
        progress("render", 0.0, "Đang ghép timeline và audio…")
        if persistent_checkpoint:
            update_render_checkpoint_state(
                work_dir,
                status="timeline",
                completed_scenes=completed_scenes,
                total_scenes=len(ordered_media),
                detail="Toàn bộ clip đã sẵn sàng; đang ghép timeline cuối.",
            )
        _run_ffmpeg(
            command,
            cwd=work_dir,
            expected_duration=plan.expected_duration,
            cancel=cancel,
            progress_callback=lambda ratio: progress(
                "render", ratio, "Đang ghép timeline và audio…"
            ),
            log=log,
            label="timeline cuối",
        )
        if not part_path.exists() or part_path.stat().st_size < 1024:
            raise RuntimeError("FFmpeg không tạo được video đầu ra hợp lệ.")

        progress("verify", 0.15, "Đang kiểm tra video và audio đầu ra…")
        duration, codec = _verify_output(
            tools,
            part_path,
            settings,
            plan.expected_duration,
            expected_frames,
        )
        progress("verify", 0.48, "Đang tạo dấu vân tay hình ảnh…")
        visual_hash = _visual_sha256(tools, part_path, cancel)
        progress("verify", 0.80, "Đang tạo dấu vân tay tệp video…")
        try:
            output_hash = sha256_file(part_path, cancel_callback=cancel.is_set)
        except InterruptedError:
            raise RenderCancelled() from None
        os.replace(part_path, output_path)
        part_path = None
        progress("verify", 1.0, "Video đã hoàn tất và được kiểm tra.")
        render_succeeded = True
        return RenderResult(
            video_path=output_path,
            output_sha256=output_hash,
            visual_sha256=visual_hash,
            duration=duration,
            codec=codec,
        )
    except RenderCancelled:
        if persistent_checkpoint:
            update_render_checkpoint_state(
                work_dir,
                status="paused",
                completed_scenes=completed_scenes,
                total_scenes=len(ordered_media),
                detail="Đã tạm dừng; clip hoàn tất được giữ để tiếp tục.",
            )
        raise
    except Exception as exc:
        if persistent_checkpoint:
            update_render_checkpoint_state(
                work_dir,
                status="error",
                completed_scenes=completed_scenes,
                total_scenes=len(ordered_media),
                detail=str(exc)[:2000],
            )
        raise
    finally:
        if "output_path" in locals():
            _release_output_path(output_path)
        if part_path:
            try:
                part_path.unlink(missing_ok=True)
            except OSError:
                pass
        if not persistent_checkpoint or render_succeeded:
            parent = work_dir.parent
            shutil.rmtree(work_dir, ignore_errors=True)
            if persistent_checkpoint:
                try:
                    parent.rmdir()
                except OSError:
                    pass
