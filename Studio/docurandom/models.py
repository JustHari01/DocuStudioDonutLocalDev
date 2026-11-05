from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    relative_path: str
    width: int
    height: int
    format_name: str
    media_type: str = "image"
    duration_seconds: float = 0.0
    fps: float = 0.0
    codec_name: str = ""
    has_audio: bool = False


# Backward-compatible import name for existing integrations and v1 manifests.
ImageInfo = MediaInfo


@dataclass(frozen=True)
class RenderSettings:
    source_dir: Path
    output_dir: Path
    width: int = 1920
    height: int = 1080
    fps: int = 30
    duration_min: float = 3.0
    duration_max: float = 5.5
    intensity: str = "medium"
    framing: str = "blur"
    recursive: bool = False
    audio_path: Path | None = None
    audio_duration: float = 0.0
    audio_identity: str = ""
    audio_volume: float = 1.0
    audio_fade_seconds: float = 0.8
    transition_pack: str = "all"
    effect_density: str = "high"
    video_mode: str = "random"
    transition_min: float = 0.55
    transition_max: float = 1.05
    randomize_order: bool = True
    rights_confirmed: bool = False
    editorial_intent: str = ""
    performance_mode: str = "auto"
    parallel_jobs: int = 1
    video_encoder: str = "libx264"
    hardware_accelerated: bool = False
    threads_per_job: int = 0
    runtime_summary: str = ""
    flow_right_crop: bool = True
    remove_gemini_watermarks: bool = False
    watermark_remover_version: str = ""
    output_name: str = ""
    subtitle_path: Path | None = None
    burn_subtitles: bool = True
    cta_enabled: bool = False
    cta_path: Path | None = None
    cta_chroma_key: bool = True
    cta_key_mode: str = "auto"
    cta_x_percent: float = 3.0
    cta_y_percent: float = 4.0
    cta_width_percent: float = 24.0
    cta_timing_mode: str = "once"
    cta_start_seconds: float = 2.0
    cta_end_seconds: float = 8.0
    cta_duration_seconds: float = 6.0
    logo_enabled: bool = False
    logo_path: Path | None = None
    logo_x_percent: float = 97.0
    logo_y_percent: float = 4.0
    logo_width_percent: float = 10.0
    logo_opacity: float = 0.85
    logo_timing_mode: str = "full"
    logo_start_seconds: float = 0.0
    logo_end_seconds: float = 8.0
    logo_duration_seconds: float = 6.0


@dataclass(frozen=True)
class ScenePlan:
    source_path: str
    relative_path: str
    duration_frames: int
    duration_seconds: float
    motion: str
    zoom_start: float
    zoom_end: float
    x_start: float
    x_end: float
    y_start: float
    y_end: float
    brightness: float
    contrast: float
    saturation: float
    gamma: float
    red_gain: float
    green_gain: float
    blue_gain: float
    sharpen: float
    media_type: str = "image"
    source_duration_seconds: float = 0.0
    source_start_seconds: float = 0.0
    playback_rate: float = 1.0
    loop_source: bool = False
    effect_name: str = "clean"
    curve_preset: str = "none"
    hue_shift: float = 0.0
    vibrance: float = 0.0
    chroma_shift: int = 0
    lens_k1: float = 0.0
    lens_k2: float = 0.0
    effect_mix: float = 0.0
    color_style: str = "Tự nhiên"
    source_width: int = 0
    source_height: int = 0


@dataclass(frozen=True)
class TransitionPlan:
    name: str
    duration_frames: int
    duration_seconds: float


@dataclass
class RenderPlan:
    app_version: str
    render_id: str
    seed: str
    created_at: str
    style: str
    settings: dict[str, Any]
    scenes: list[ScenePlan]
    transitions: list[TransitionPlan]
    grain_seed: int
    grain_strength: float
    vignette_divisor: float
    flicker_amount: float
    flicker_frequency: float
    flicker_phase: float
    fade_in_seconds: float
    fade_out_seconds: float
    expected_duration: float
    manifest_signature: str = ""
    output_file: str = ""
    output_sha256: str = ""
    visual_sha256: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of the render plan.

        Render plans are persisted in checkpoints and manifests.  Most fields
        are already plain JSON values, but settings can contain platform
        ``Path`` objects (for example ``subtitle_path``) when a job is created
        on Windows.  ``json.dumps`` cannot encode ``WindowsPath`` directly,
        which used to abort a client render immediately after watermark
        preparation.  Convert paths recursively so every checkpoint remains
        portable and resumable.
        """

        def json_safe(value: Any) -> Any:
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, dict):
                return {str(key): json_safe(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [json_safe(item) for item in value]
            return value

        return json_safe(asdict(self))
