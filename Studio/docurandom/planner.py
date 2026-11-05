from __future__ import annotations

import hashlib
import json
import math
import random
import secrets
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Iterable, Sequence

from . import __version__
from .models import (
    MediaInfo,
    RenderPlan,
    RenderSettings,
    ScenePlan,
    TransitionPlan,
)


# FFmpeg 8.1.2 exposes 58 native xfade transitions. squeezev is intentionally
# excluded because that filter crashes the bundled Windows build.
TRANSITION_FAMILIES: dict[str, tuple[str, ...]] = {
    "fade": (
        "fade",
        "dissolve",
        "fadeblack",
        "fadewhite",
        "fadegrays",
        "fadefast",
        "fadeslow",
        "hblur",
    ),
    "wipe": (
        "wipeleft",
        "wiperight",
        "wipeup",
        "wipedown",
        "wipetl",
        "wipetr",
        "wipebl",
        "wipebr",
    ),
    "slide": ("slideleft", "slideright", "slideup", "slidedown"),
    "smooth": ("smoothleft", "smoothright", "smoothup", "smoothdown"),
    "cover": (
        "coverleft",
        "coverright",
        "coverup",
        "coverdown",
        "revealleft",
        "revealright",
        "revealup",
        "revealdown",
    ),
    "shape": (
        "circlecrop",
        "rectcrop",
        "distance",
        "radial",
        "circleopen",
        "circleclose",
        "vertopen",
        "vertclose",
        "horzopen",
        "horzclose",
    ),
    "slice": (
        "diagtl",
        "diagtr",
        "diagbl",
        "diagbr",
        "hlslice",
        "hrslice",
        "vuslice",
        "vdslice",
    ),
    "wind": ("hlwind", "hrwind", "vuwind", "vdwind"),
    "creative": ("pixelize", "squeezeh", "zoomin"),
}

TRANSITION_PACKS: dict[str, tuple[str, ...]] = {
    # A broad but calm default for long-form viewing.  It keeps 42 familiar
    # fades, wipes, slides, smooth moves, reveals and aperture transitions,
    # while excluding pixel/squeeze/wind/slice effects that feel abrupt when
    # repeated across a documentary timeline.
    "documentary": (
        *TRANSITION_FAMILIES["fade"],
        *TRANSITION_FAMILIES["wipe"],
        *TRANSITION_FAMILIES["slide"],
        *TRANSITION_FAMILIES["smooth"],
        *TRANSITION_FAMILIES["cover"],
        *TRANSITION_FAMILIES["shape"],
    ),
    "popular": (
        *TRANSITION_FAMILIES["fade"],
        *TRANSITION_FAMILIES["wipe"],
        *TRANSITION_FAMILIES["slide"],
        *TRANSITION_FAMILIES["smooth"],
        *TRANSITION_FAMILIES["cover"],
        "circleopen",
        "circleclose",
        "radial",
        "zoomin",
    ),
    "cinematic": (
        *TRANSITION_FAMILIES["fade"],
        *TRANSITION_FAMILIES["smooth"],
        *TRANSITION_FAMILIES["cover"],
        "circleopen",
        "circleclose",
        "vertopen",
        "vertclose",
        "horzopen",
        "horzclose",
        "radial",
        "distance",
    ),
    "dynamic": (
        *TRANSITION_FAMILIES["wipe"],
        *TRANSITION_FAMILIES["slide"],
        *TRANSITION_FAMILIES["slice"],
        *TRANSITION_FAMILIES["wind"],
        *TRANSITION_FAMILIES["creative"],
        *TRANSITION_FAMILIES["cover"],
    ),
    "all": tuple(
        transition
        for family in TRANSITION_FAMILIES.values()
        for transition in family
    ),
}
TRANSITIONS = TRANSITION_PACKS["all"]
TRANSITION_TO_FAMILY = {
    transition: family
    for family, transitions in TRANSITION_FAMILIES.items()
    for transition in transitions
}

MOTIONS = (
    "push_center",
    "pull_center",
    "pan_left",
    "pan_right",
    "pan_up",
    "pan_down",
    "diagonal_tl_br",
    "diagonal_tr_bl",
    "diagonal_bl_tr",
    "diagonal_br_tl",
)

EFFECT_POOLS: dict[str, tuple[str, ...]] = {
    "low": (
        "clean_cinema",
        "warm_documentary",
        "cool_reportage",
        "muted_film",
        "medium_contrast",
        "soft_focus",
        "vibrant_travel",
        "faded_print",
    ),
    "medium": (
        "clean_cinema",
        "warm_documentary",
        "cool_reportage",
        "muted_film",
        "medium_contrast",
        "soft_focus",
        "vibrant_travel",
        "faded_print",
        "vintage_curve",
        "sepia_archive",
        "monochrome_news",
        "noir_contrast",
        "teal_orange",
        "lens_documentary",
    ),
    "high": (
        "clean_cinema",
        "warm_documentary",
        "cool_reportage",
        "muted_film",
        "medium_contrast",
        "soft_focus",
        "vibrant_travel",
        "faded_print",
        "vintage_curve",
        "sepia_archive",
        "monochrome_news",
        "noir_contrast",
        "teal_orange",
        "lens_documentary",
        "cross_process",
        "archival_warmth",
        "silver_retention",
        "soft_newsprint",
        "neutral_film",
        "bleach_bypass",
        "broadcast_documentary",
    ),
    "maximum": (
        "warm_documentary",
        "cool_reportage",
        "vintage_curve",
        "sepia_archive",
        "monochrome_news",
        "noir_contrast",
        "teal_orange",
        "lens_documentary",
        "cross_process",
        "vhs_chroma",
        "motion_trail",
        "pixel_pulse",
        "vibrant_travel",
        "faded_print",
    ),
}

STYLE_PROFILES: dict[str, dict[str, tuple[float, float]]] = {
    "Tài liệu ấm": {
        "brightness": (-0.018, 0.014),
        "contrast": (1.025, 1.105),
        "saturation": (0.88, 1.08),
        "gamma": (0.97, 1.035),
        "red": (1.018, 1.055),
        "green": (0.995, 1.018),
        "blue": (0.945, 0.992),
    },
    "Phóng sự lạnh": {
        "brightness": (-0.02, 0.012),
        "contrast": (1.035, 1.12),
        "saturation": (0.78, 0.98),
        "gamma": (0.965, 1.03),
        "red": (0.955, 0.995),
        "green": (0.995, 1.02),
        "blue": (1.015, 1.06),
    },
    "Kho lưu trữ": {
        "brightness": (-0.026, 0.018),
        "contrast": (1.04, 1.16),
        "saturation": (0.03, 0.34),
        "gamma": (0.94, 1.04),
        "red": (1.015, 1.065),
        "green": (0.985, 1.025),
        "blue": (0.91, 0.975),
    },
    "Điện ảnh trung tính": {
        "brightness": (-0.015, 0.012),
        "contrast": (1.018, 1.09),
        "saturation": (0.86, 1.02),
        "gamma": (0.975, 1.025),
        "red": (0.985, 1.025),
        "green": (0.99, 1.018),
        "blue": (0.975, 1.025),
    },
    "Phim cũ": {
        "brightness": (-0.022, 0.02),
        "contrast": (1.03, 1.14),
        "saturation": (0.48, 0.78),
        "gamma": (0.95, 1.04),
        "red": (1.035, 1.085),
        "green": (0.99, 1.035),
        "blue": (0.89, 0.96),
    },
    "Bản in phai màu": {
        "brightness": (0.0, 0.025),
        "contrast": (0.94, 1.035),
        "saturation": (0.66, 0.9),
        "gamma": (1.0, 1.065),
        "red": (1.005, 1.045),
        "green": (0.99, 1.02),
        "blue": (0.95, 1.005),
    },
    "Du lịch rực rỡ": {
        "brightness": (-0.008, 0.018),
        "contrast": (1.02, 1.09),
        "saturation": (1.02, 1.18),
        "gamma": (0.985, 1.025),
        "red": (1.0, 1.04),
        "green": (1.0, 1.035),
        "blue": (0.98, 1.025),
    },
}

INTENSITY = {
    "light": {
        "zoom": (0.025, 0.055),
        "grain": (2, 4),
        "vignette": (6.8, 8.8),
        "flicker": (0.0007, 0.0018),
        "sharpen": (0.10, 0.28),
        "speed": (0.97, 1.03),
    },
    "medium": {
        "zoom": (0.045, 0.085),
        "grain": (3, 7),
        "vignette": (5.3, 7.2),
        "flicker": (0.0012, 0.0030),
        "sharpen": (0.18, 0.42),
        "speed": (0.93, 1.08),
    },
    "strong": {
        "zoom": (0.065, 0.115),
        "grain": (5, 10),
        "vignette": (4.2, 6.0),
        "flicker": (0.0022, 0.0046),
        "sharpen": (0.28, 0.58),
        "speed": (0.86, 1.15),
    },
}


def _round6(value: float) -> float:
    return round(value, 6)


def _motion_coordinates(motion: str) -> tuple[float, float, float, float]:
    mapping = {
        "push_center": (0.5, 0.5, 0.5, 0.5),
        "pull_center": (0.5, 0.5, 0.5, 0.5),
        "pan_left": (1.0, 0.0, 0.5, 0.5),
        "pan_right": (0.0, 1.0, 0.5, 0.5),
        "pan_up": (0.5, 0.5, 1.0, 0.0),
        "pan_down": (0.5, 0.5, 0.0, 1.0),
        "diagonal_tl_br": (0.0, 1.0, 0.0, 1.0),
        "diagonal_tr_bl": (1.0, 0.0, 0.0, 1.0),
        "diagonal_bl_tr": (0.0, 1.0, 1.0, 0.0),
        "diagonal_br_tl": (1.0, 0.0, 1.0, 0.0),
    }
    return mapping[motion]


def _shuffle_sequence(
    values: Sequence[str],
    count: int,
    rng: random.Random,
) -> list[str]:
    if not values or count <= 0:
        return []
    result: list[str] = []
    last: str | None = None
    while len(result) < count:
        bag = list(dict.fromkeys(values))
        rng.shuffle(bag)
        if last is not None and len(bag) > 1 and bag[0] == last:
            swap_index = next(index for index, item in enumerate(bag) if item != last)
            bag[0], bag[swap_index] = bag[swap_index], bag[0]
        take = min(len(bag), count - len(result))
        result.extend(bag[:take])
        last = result[-1]
    return result


def _transition_sequence(
    values: Sequence[str],
    count: int,
    rng: random.Random,
) -> list[str]:
    """Shuffle-bag transitions with a two-family cooldown where possible."""
    unique_values = list(dict.fromkeys(values))
    result: list[str] = []
    recent_families: list[str] = []
    last_name: str | None = None
    bag: list[str] = []
    while len(result) < count:
        if not bag:
            bag = list(unique_values)
            rng.shuffle(bag)
        candidates = [
            value
            for value in bag
            if value != last_name
            and TRANSITION_TO_FAMILY.get(value, value) not in recent_families[-2:]
        ]
        if not candidates:
            candidates = [value for value in bag if value != last_name] or list(bag)
        chosen = rng.choice(candidates)
        bag.remove(chosen)
        result.append(chosen)
        last_name = chosen
        recent_families.append(TRANSITION_TO_FAMILY.get(chosen, chosen))
    return result


def _humanized_media_order(
    media: Sequence[MediaInfo],
    rng: random.Random,
) -> list[MediaInfo]:
    """Shuffle media without imposing a fixed image/video alternation pattern.

    Selection is weighted by the number of items remaining. A small randomized
    run limit prevents avoidable long streaks when another type is available,
    but never creates a repeating image/image/video template.
    """
    if len(media) < 2:
        return list(media)

    buckets: dict[str, list[MediaInfo]] = {}
    for item in media:
        buckets.setdefault(item.media_type, []).append(item)
    for bucket in buckets.values():
        rng.shuffle(bucket)
    if len(buckets) < 2:
        result = list(media)
        rng.shuffle(result)
        return result

    result: list[MediaInfo] = []
    last_type: str | None = None
    run_length = 0
    run_limit = rng.randint(1, 3)
    while any(buckets.values()):
        choices = [kind for kind, values in buckets.items() if values]
        if last_type in choices and run_length >= run_limit and len(choices) > 1:
            choices.remove(last_type)

        weights: list[float] = []
        for kind in choices:
            weight = float(len(buckets[kind]))
            if kind == last_type:
                weight *= rng.uniform(0.35, 0.78)
            else:
                weight *= rng.uniform(0.90, 1.25)
            weights.append(weight)
        chosen_type = rng.choices(choices, weights=weights, k=1)[0]
        result.append(buckets[chosen_type].pop())
        if chosen_type == last_type:
            run_length += 1
        else:
            last_type = chosen_type
            run_length = 1
            run_limit = rng.randint(1, 3)
    return result


def _allocate_frames(
    total: int,
    weights: Sequence[float],
    minimum: int,
) -> list[int]:
    count = len(weights)
    if count == 0:
        return []
    if total < count:
        raise ValueError(
            "Audio quá ngắn: không đủ dù chỉ một frame cho mỗi media. "
            "Hãy dùng audio dài hơn hoặc bớt media."
        )
    effective_minimum = minimum if total >= minimum * count else 1
    remaining = total - effective_minimum * count
    weight_sum = sum(max(weight, 0.001) for weight in weights)
    raw = [remaining * max(weight, 0.001) / weight_sum for weight in weights]
    extras = [math.floor(value) for value in raw]
    leftover = remaining - sum(extras)
    order = sorted(
        range(count),
        key=lambda index: (raw[index] - extras[index], weights[index]),
        reverse=True,
    )
    for index in order[:leftover]:
        extras[index] += 1
    result = [effective_minimum + extra for extra in extras]
    assert sum(result) == total
    return result


def _settings_dict(settings: RenderSettings) -> dict[str, object]:
    value = asdict(settings)
    value["source_dir"] = str(settings.source_dir)
    value["output_dir"] = str(settings.output_dir)
    value["audio_path"] = str(settings.audio_path) if settings.audio_path else ""
    value["subtitle_path"] = (
        str(settings.subtitle_path) if settings.subtitle_path else ""
    )
    return value


def _effect_parameters(
    effect_name: str,
    rng: random.Random,
    intensity: dict[str, tuple[float, float] | tuple[int, int]],
) -> dict[str, object]:
    curve_map = {
        "vintage_curve": "vintage",
        "cross_process": "cross_process",
        "medium_contrast": "medium_contrast",
        "noir_contrast": "strong_contrast",
        "faded_print": "lighter",
        "silver_retention": "medium_contrast",
        "soft_newsprint": "lighter",
        "neutral_film": "medium_contrast",
        "bleach_bypass": "medium_contrast",
    }
    hue_shift = rng.uniform(-5.5, 5.5)
    vibrance = rng.uniform(-0.12, 0.28)
    chroma_shift = rng.choice((-2, -1, 1, 2))
    lens_k1 = rng.uniform(-0.025, -0.008)
    lens_k2 = rng.uniform(0.001, 0.007)
    mix = rng.uniform(0.35, 0.85)
    if effect_name in {"monochrome_news", "noir_contrast", "sepia_archive"}:
        vibrance = -1.0
    if effect_name == "vhs_chroma":
        chroma_shift = rng.choice((-3, -2, 2, 3))
    if effect_name != "lens_documentary":
        lens_k1 = 0.0
        lens_k2 = 0.0
    return {
        "curve_preset": curve_map.get(effect_name, "none"),
        "hue_shift": _round6(hue_shift),
        "vibrance": _round6(vibrance),
        "chroma_shift": chroma_shift,
        "lens_k1": _round6(lens_k1),
        "lens_k2": _round6(lens_k2),
        "effect_mix": _round6(mix),
    }


def build_render_plan(
    media: Iterable[MediaInfo],
    settings: RenderSettings,
    *,
    seed: str | None = None,
) -> RenderPlan:
    media_list = list(media)
    if not media_list:
        raise ValueError("Cần ít nhất một ảnh hoặc video hợp lệ để render.")
    if settings.duration_min <= 0 or settings.duration_max < settings.duration_min:
        raise ValueError("Khoảng thời lượng mỗi media không hợp lệ.")
    if settings.fps <= 0:
        raise ValueError("FPS phải lớn hơn 0.")
    if settings.audio_path and settings.audio_duration <= 0:
        raise ValueError("Không xác định được thời lượng audio đã chọn.")

    seed = seed or secrets.token_hex(16)
    rng = random.Random(int(seed, 16))
    if settings.randomize_order:
        media_list = _humanized_media_order(media_list, rng)
    render_id = uuid.uuid4().hex[:12]
    # Colour is selected per scene, not once for the whole render.  A single
    # global grade made long videos look as though one documentary LUT had
    # been placed over everything.  The shuffled sequence keeps adjacent
    # scenes distinct while remaining reproducible from the render seed.
    style = ("Giữ màu nguồn · bố cục an toàn" if settings.framing in {"vertical-blur", "vertical-smart"}
             else "Ngẫu nhiên từng cảnh")
    intensity = INTENSITY.get(settings.intensity, INTENSITY["medium"])
    count = len(media_list)

    transition_names = _transition_sequence(
        TRANSITION_PACKS.get(settings.transition_pack, TRANSITION_PACKS["all"]),
        max(0, count - 1),
        rng,
    )
    transition_min_frames = max(1, math.ceil(settings.transition_min * settings.fps))
    transition_max_frames = max(
        transition_min_frames,
        math.floor(settings.transition_max * settings.fps),
    )
    raw_transition_frames = [
        rng.randint(transition_min_frames, transition_max_frames)
        for _ in range(max(0, count - 1))
    ]

    if settings.audio_path:
        target_frames = math.ceil(settings.audio_duration * settings.fps)
        weights: list[float] = []
        for item in media_list:
            base = 1.0 if settings.framing in {"vertical-blur", "vertical-smart"} else rng.uniform(0.86, 1.14)
            if item.media_type == "video" and settings.framing not in {"vertical-blur", "vertical-smart"}:
                duration_bias = min(1.45, max(0.8, item.duration_seconds / 6.0))
                base *= 0.85 + 0.15 * duration_bias
            weights.append(base)
        contribution_frames = _allocate_frames(
            target_frames,
            weights,
            minimum=max(1, round(settings.fps * 0.42)),
        )
        transition_frames: list[int] = []
        cumulative = contribution_frames[0]
        for index, requested in enumerate(raw_transition_frames):
            maximum = max(1, cumulative)
            chosen = min(requested, maximum)
            transition_frames.append(chosen)
            cumulative += contribution_frames[index + 1]
        scene_frames = [contribution_frames[0]] + [
            contribution_frames[index] + transition_frames[index - 1]
            for index in range(1, count)
        ]
        expected_frames = target_frames
    else:
        min_frames = max(2, math.ceil(settings.duration_min * settings.fps))
        max_frames = max(min_frames, math.floor(settings.duration_max * settings.fps))
        scene_frames = [rng.randint(min_frames, max_frames) for _ in media_list]
        transition_frames = []
        for index, requested in enumerate(raw_transition_frames):
            maximum = max(
                1,
                min(scene_frames[index], scene_frames[index + 1]) - 1,
            )
            transition_frames.append(min(requested, maximum))
        expected_frames = sum(scene_frames) - sum(transition_frames)

    transitions = [
        TransitionPlan(
            name=name,
            duration_frames=frames,
            duration_seconds=_round6(frames / settings.fps),
        )
        for name, frames in zip(
            transition_names,
            transition_frames,
            strict=True,
        )
    ]

    motion_names = _shuffle_sequence(MOTIONS, count, rng)
    effect_names = _shuffle_sequence(
        EFFECT_POOLS.get(settings.effect_density, EFFECT_POOLS["high"]),
        count,
        rng,
    )
    color_styles = _shuffle_sequence(tuple(STYLE_PROFILES), count, rng)
    scenes: list[ScenePlan] = []
    for item, duration_frames, motion, effect_name, color_style in zip(
        media_list,
        scene_frames,
        motion_names,
        effect_names,
        color_styles,
        strict=True,
    ):
        style_profile = STYLE_PROFILES[color_style]
        duration_seconds = duration_frames / settings.fps
        zoom_delta = rng.uniform(*intensity["zoom"])
        if motion == "pull_center":
            zoom_start, zoom_end = 1.0 + zoom_delta, 1.002
        elif motion.startswith("pan_") or motion.startswith("diagonal_"):
            base_zoom = 1.0 + zoom_delta
            zoom_start = base_zoom + rng.uniform(-0.007, 0.007)
            zoom_end = base_zoom + rng.uniform(-0.007, 0.007)
        else:
            zoom_start, zoom_end = 1.002, 1.0 + zoom_delta
        x_start, x_end, y_start, y_end = _motion_coordinates(motion)

        source_start = 0.0
        playback_rate = 1.0
        loop_source = False
        if item.media_type == "video":
            if settings.video_mode == "full" and item.duration_seconds > 0:
                proposed = item.duration_seconds / max(duration_seconds, 1 / settings.fps)
                playback_rate = min(1.35, max(0.72, proposed))
            else:
                playback_rate = rng.uniform(*intensity["speed"])
            needed_source = duration_seconds * playback_rate
            loop_source = needed_source > item.duration_seconds
            if settings.video_mode == "start":
                source_start = 0.0
            elif item.duration_seconds > 1 / settings.fps:
                maximum_start = max(0.0, item.duration_seconds - min(needed_source, item.duration_seconds))
                source_start = rng.uniform(0.0, maximum_start) if maximum_start > 0 else 0.0

        effect_parameters = _effect_parameters(effect_name, rng, intensity)
        scenes.append(
            ScenePlan(
                source_path=str(item.path),
                relative_path=item.relative_path,
                duration_frames=duration_frames,
                duration_seconds=_round6(duration_seconds),
                motion=motion,
                zoom_start=_round6(zoom_start),
                zoom_end=_round6(zoom_end),
                x_start=_round6(x_start),
                x_end=_round6(x_end),
                y_start=_round6(y_start),
                y_end=_round6(y_end),
                brightness=_round6(rng.uniform(*style_profile["brightness"])),
                contrast=_round6(rng.uniform(*style_profile["contrast"])),
                saturation=_round6(rng.uniform(*style_profile["saturation"])),
                gamma=_round6(rng.uniform(*style_profile["gamma"])),
                red_gain=_round6(rng.uniform(*style_profile["red"])),
                green_gain=_round6(rng.uniform(*style_profile["green"])),
                blue_gain=_round6(rng.uniform(*style_profile["blue"])),
                sharpen=_round6(rng.uniform(*intensity["sharpen"])),
                media_type=item.media_type,
                source_duration_seconds=_round6(item.duration_seconds),
                source_width=item.width,
                source_height=item.height,
                source_start_seconds=_round6(source_start),
                playback_rate=_round6(playback_rate),
                loop_source=loop_source,
                effect_name=effect_name,
                color_style=color_style,
                **effect_parameters,
            )
        )

    if settings.framing in {"vertical-blur", "vertical-smart"}:
        # Technical footage must retain source colours. Never randomise a
        # material/temperature/evidence cue through the aesthetic LUT bag.
        scenes = [replace(s, brightness=0, contrast=1, saturation=1, gamma=1,
                          red_gain=1, green_gain=1, blue_gain=1, effect_name="clean",
                          curve_preset="none", hue_shift=0, vibrance=0, chroma_shift=0,
                          lens_k1=0, lens_k2=0, effect_mix=0, sharpen=0, color_style="Tự nhiên") for s in scenes]

    calculated_frames = sum(scene.duration_frames for scene in scenes) - sum(
        transition.duration_frames for transition in transitions
    )
    if calculated_frames != expected_frames:
        raise AssertionError(
            f"Timeline frame mismatch: {calculated_frames} != {expected_frames}"
        )
    expected_duration = expected_frames / settings.fps
    fade_in = min(rng.uniform(0.25, 0.7), expected_duration * 0.08)
    fade_out = min(rng.uniform(0.35, 0.9), expected_duration * 0.08)
    preflight_warnings = [
        "Random hiệu ứng chỉ tạo đa dạng thẩm mỹ, không phải chứng nhận tuân thủ "
        "chính sách YouTube hay bản quyền."
    ]

    plan = RenderPlan(
        app_version=__version__,
        render_id=render_id,
        seed=seed,
        created_at=datetime.now(timezone.utc).isoformat(),
        style=style,
        settings=_settings_dict(settings),
        scenes=scenes,
        transitions=transitions,
        grain_seed=rng.randint(1, 2_147_483_647),
        grain_strength=float(rng.randint(*intensity["grain"])),
        vignette_divisor=_round6(rng.uniform(*intensity["vignette"])),
        flicker_amount=_round6(rng.uniform(*intensity["flicker"])),
        flicker_frequency=_round6(rng.uniform(0.65, 1.85)),
        flicker_phase=_round6(rng.uniform(0.0, math.tau)),
        fade_in_seconds=_round6(fade_in),
        fade_out_seconds=_round6(fade_out),
        expected_duration=_round6(expected_duration),
        warnings=preflight_warnings,
    )

    visual_recipe = {
        "app_version": plan.app_version,
        "style": plan.style,
        "frame": {
            "width": settings.width,
            "height": settings.height,
            "fps": settings.fps,
            "framing": settings.framing,
            "expected_frames": expected_frames,
        },
        "scenes": [asdict(scene) for scene in plan.scenes],
        "transitions": [asdict(transition) for transition in plan.transitions],
        "grain_seed": plan.grain_seed,
        "grain_strength": plan.grain_strength,
        "vignette_divisor": plan.vignette_divisor,
        "flicker_amount": plan.flicker_amount,
        "flicker_frequency": plan.flicker_frequency,
        "flicker_phase": plan.flicker_phase,
        "fade_in_seconds": plan.fade_in_seconds,
        "fade_out_seconds": plan.fade_out_seconds,
        "expected_duration": plan.expected_duration,
    }
    canonical = json.dumps(visual_recipe, ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )
    plan.manifest_signature = hashlib.sha256(canonical).hexdigest()
    return plan
