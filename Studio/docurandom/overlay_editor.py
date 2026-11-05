from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageTk

from .overlay_timing import normalize_timing_mode, timing_summary, validate_timing_values
from .ui_scale import proportional_pane_position
from .ui_theme import (
    BACKGROUND,
    BORDER,
    INPUT,
    MUTED,
    PANEL,
    PANEL_ALT,
    SUCCESS,
    TEXT,
)


_IMAGE_EXTENSIONS = frozenset(
    {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)
_KEY_MODE_OPTIONS = {
    "Tự động · xanh / đen / trong suốt": "auto",
    "Phông xanh": "green",
    "Phông đen": "black",
    "Không xóa phông": "none",
}
_CTA_TIMING_OPTIONS = {
    "Phát một lần": "once",
    "Toàn bộ video": "full",
    "Đoạn đầu video": "start",
    "Đoạn cuối video": "end",
    "Khoảng tùy chọn": "custom",
}
_LOGO_TIMING_OPTIONS = {
    label: value
    for label, value in _CTA_TIMING_OPTIONS.items()
    if value != "once"
}
_ANIMATED_CTA_EXTENSIONS = frozenset(
    {".avi", ".gif", ".mkv", ".mov", ".mp4", ".webm"}
)
_CTA_PREVIEW_FPS = 8
_CTA_PREVIEW_MAX_WIDTH = 480


@dataclass(frozen=True)
class OverlayEditorState:
    cta_enabled: bool = False
    cta_path: str = ""
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
    logo_path: str = ""
    logo_x_percent: float = 97.0
    logo_y_percent: float = 4.0
    logo_width_percent: float = 10.0
    logo_opacity: float = 0.85
    logo_timing_mode: str = "full"
    logo_start_seconds: float = 0.0
    logo_end_seconds: float = 8.0
    logo_duration_seconds: float = 6.0


def _creation_flags() -> int:
    if not hasattr(subprocess, "CREATE_NO_WINDOW"):
        return 0
    return subprocess.CREATE_NO_WINDOW


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    filename = "segoeuib.ttf" if bold else "segoeui.ttf"
    path = Path("C:/Windows/Fonts") / filename
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default()


def _video_frame(path: Path, ffmpeg_path: Path | None) -> Image.Image:
    if ffmpeg_path is None or not ffmpeg_path.is_file():
        raise RuntimeError("Cần FFmpeg để xem trước tệp video.")
    completed: subprocess.CompletedProcess[bytes] | None = None
    for seek in ("0.250", "0.000"):
        completed = subprocess.run(
            [
                str(ffmpeg_path),
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
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "Không đọc được khung hình video.")
    with Image.open(io.BytesIO(completed.stdout)) as opened:
        return ImageOps.exif_transpose(opened).convert("RGBA")


def load_preview_frame(path: Path, ffmpeg_path: Path | None) -> Image.Image:
    if path.suffix.casefold() in _IMAGE_EXTENSIONS:
        with Image.open(path) as opened:
            return ImageOps.exif_transpose(opened).convert("RGBA")
    return _video_frame(path, ffmpeg_path)


def animated_preview_command(
    path: Path,
    ffmpeg_path: Path | None,
    key_mode: str,
    *,
    fps: int = _CTA_PREVIEW_FPS,
    max_width: int = _CTA_PREVIEW_MAX_WIDTH,
) -> tuple[list[str], tuple[int, int], str]:
    """Build a full-length, low-resolution CTA stream for the native preview."""

    if ffmpeg_path is None or not ffmpeg_path.is_file():
        raise RuntimeError("Cần FFmpeg để chạy xem trước CTA chuyển động.")
    first = _video_frame(path, ffmpeg_path)
    effective = (
        detect_background_key(first)
        if str(key_mode or "auto").casefold() == "auto"
        else str(key_mode or "none").casefold()
    )
    target_width = max(2, min(int(max_width), first.width))
    target_height = max(2, round(first.height * target_width / max(1, first.width)))
    filters: list[str] = [f"fps={max(1, int(fps))}"]
    if effective == "green":
        filters.extend(
            (
                "chromakey=color=0x00FF00:similarity=0.17:blend=0.07",
                "despill=type=green:mix=0.25",
            )
        )
    elif effective == "black":
        filters.append("colorkey=color=0x000000:similarity=0.10:blend=0.06")
    filters.extend(
        (
            f"scale=w={target_width}:h={target_height}:flags=lanczos",
            "format=rgba",
        )
    )
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-an",
        "-sn",
        "-vf",
        ",".join(filters),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "pipe:1",
    ]
    return command, (target_width, target_height), effective


def compose_video_preview(
    source: Image.Image,
    size: tuple[int, int],
    framing: str,
) -> Image.Image:
    """Match the fixed foreground and blurred canvas used by the renderer."""

    rgba = source.convert("RGBA")
    base = Image.new("RGBA", rgba.size, (10, 12, 16, 255))
    flattened = Image.alpha_composite(base, rgba).convert("RGB")
    if framing != "blur":
        return ImageOps.fit(flattened, size, method=Image.Resampling.LANCZOS)
    background = ImageOps.fit(flattened, size, method=Image.Resampling.LANCZOS)
    background = background.filter(
        ImageFilter.GaussianBlur(max(6.0, max(size) * 0.018))
    )
    background = ImageEnhance.Brightness(background).enhance(0.58)
    foreground_size = (
        max(2, round(size[0] * 0.90)),
        max(2, round(size[1] * 0.90)),
    )
    foreground = ImageOps.fit(
        flattened,
        foreground_size,
        method=Image.Resampling.LANCZOS,
    )
    background.paste(
        foreground,
        (
            (size[0] - foreground.width) // 2,
            (size[1] - foreground.height) // 2,
        ),
    )
    return background


def fit_aspect_size(
    container_size: tuple[int, int],
    content_size: tuple[int, int],
    *,
    inset: int = 0,
) -> tuple[int, int]:
    """Fit content inside a resizable panel while preserving its aspect ratio."""

    available_width = max(2, int(container_size[0]) - max(0, inset) * 2)
    available_height = max(2, int(container_size[1]) - max(0, inset) * 2)
    content_width = max(1, int(content_size[0]))
    content_height = max(1, int(content_size[1]))
    aspect = content_width / content_height
    if available_width / available_height > aspect:
        height = available_height
        width = max(2, round(height * aspect))
    else:
        width = available_width
        height = max(2, round(width / aspect))
    return width, height


def remove_green_screen(image: Image.Image) -> Image.Image:
    """Remove vivid green and suppress spill for the lightweight UI preview."""

    rgba = image.convert("RGBA")
    keyed: list[tuple[int, int, int, int]] = []
    pixels = (
        rgba.get_flattened_data()
        if hasattr(rgba, "get_flattened_data")
        else rgba.getdata()
    )
    for red, green, blue, alpha in pixels:
        dominance = green - max(red, blue)
        brightness = green - 70
        strength = max(0.0, min(1.0, min(dominance / 95.0, brightness / 120.0)))
        new_alpha = round(alpha * (1.0 - strength))
        if strength > 0:
            green = min(green, max(red, blue) + round(18 * (1.0 - strength)))
        keyed.append((red, green, blue, new_alpha))
    rgba.putdata(keyed)
    return rgba


def remove_black_screen(image: Image.Image) -> Image.Image:
    """Turn a near-black background transparent with a soft antialiased edge."""

    rgba = image.convert("RGBA")
    keyed: list[tuple[int, int, int, int]] = []
    pixels = (
        rgba.get_flattened_data()
        if hasattr(rgba, "get_flattened_data")
        else rgba.getdata()
    )
    for red, green, blue, alpha in pixels:
        brightness = max(red, green, blue)
        strength = max(0.0, min(1.0, (62.0 - brightness) / 54.0))
        keyed.append((red, green, blue, round(alpha * (1.0 - strength))))
    rgba.putdata(keyed)
    return rgba


def detect_background_key(image: Image.Image) -> str:
    """Classify a CTA border as green, black, transparent, or unkeyed."""

    sample = image.convert("RGBA").resize((64, 64), Image.Resampling.BILINEAR)
    pixels: list[tuple[int, int, int, int]] = []
    for y in range(64):
        for x in range(64):
            if x < 8 or x >= 56 or y < 8 or y >= 56:
                pixels.append(sample.getpixel((x, y)))
    if not pixels:
        return "none"
    transparent = sum(alpha < 32 for _red, _green, _blue, alpha in pixels)
    green = sum(
        alpha >= 32 and value_green > 85 and value_green - max(red, blue) > 45
        for red, value_green, blue, alpha in pixels
    )
    black = sum(
        alpha >= 32 and max(red, value_green, blue) < 48
        for red, value_green, blue, alpha in pixels
    )
    total = len(pixels)
    if transparent / total >= 0.45:
        return "none"
    if green / total >= 0.45:
        return "green"
    if black / total >= 0.45:
        return "black"
    return "none"


def remove_keyed_background(image: Image.Image, mode: str) -> tuple[Image.Image, str]:
    selected = str(mode or "auto").casefold()
    effective = detect_background_key(image) if selected == "auto" else selected
    if effective == "green":
        return remove_green_screen(image), effective
    if effective == "black":
        return remove_black_screen(image), effective
    return image.convert("RGBA"), "none"


def _placeholder(size: tuple[int, int], label: str, fill: str) -> Image.Image:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (2, 2, size[0] - 3, size[1] - 3),
        radius=max(8, min(size) // 5),
        fill=fill,
        outline="white",
        width=2,
    )
    bounds = draw.textbbox((0, 0), label, font=_font(max(12, min(size) // 4), bold=True))
    draw.text(
        (
            (size[0] - (bounds[2] - bounds[0])) / 2,
            (size[1] - (bounds[3] - bounds[1])) / 2 - bounds[1],
        ),
        label,
        font=_font(max(12, min(size) // 4), bold=True),
        fill="white",
    )
    return image


class VideoOverlayEditorDialog:
    def __init__(
        self,
        parent: tk.Misc,
        *,
        initial: OverlayEditorState,
        video_size: tuple[int, int],
        background_path: Path | None,
        ffmpeg_path: Path | None,
        framing: str,
        on_apply: Callable[[OverlayEditorState], None],
    ) -> None:
        self.parent = parent
        self.on_apply = on_apply
        self.video_size = video_size
        self.background_path = background_path
        self.ffmpeg_path = ffmpeg_path
        self.framing = framing
        self.window = tk.Toplevel(parent)
        self.window.title("Bố cục CTA & logo trong video")
        self.window.configure(background=BACKGROUND)
        self._configure_window_geometry()
        self.window.transient(parent.winfo_toplevel())
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _event: self.close())
        self.window.bind("<Control-Return>", lambda _event: self._apply())

        self.cta_enabled = tk.BooleanVar(value=initial.cta_enabled)
        self.cta_path = tk.StringVar(value=initial.cta_path)
        self.cta_chroma_key = tk.BooleanVar(value=initial.cta_chroma_key)
        initial_key_label = next(
            (
                label
                for label, value in _KEY_MODE_OPTIONS.items()
                if value == initial.cta_key_mode
            ),
            next(iter(_KEY_MODE_OPTIONS)),
        )
        self.cta_key_mode = tk.StringVar(value=initial_key_label)
        self.cta_x = tk.DoubleVar(value=initial.cta_x_percent)
        self.cta_y = tk.DoubleVar(value=initial.cta_y_percent)
        self.cta_width = tk.DoubleVar(value=initial.cta_width_percent)
        self.cta_timing_mode = tk.StringVar(
            value=next(
                (
                    label
                    for label, value in _CTA_TIMING_OPTIONS.items()
                    if value == normalize_timing_mode(
                        initial.cta_timing_mode,
                        allow_once=True,
                    )
                ),
                next(iter(_CTA_TIMING_OPTIONS)),
            )
        )
        self.cta_start = tk.DoubleVar(value=initial.cta_start_seconds)
        self.cta_end = tk.DoubleVar(value=initial.cta_end_seconds)
        self.cta_duration = tk.DoubleVar(value=initial.cta_duration_seconds)
        self.logo_enabled = tk.BooleanVar(value=initial.logo_enabled)
        self.logo_path = tk.StringVar(value=initial.logo_path)
        self.logo_x = tk.DoubleVar(value=initial.logo_x_percent)
        self.logo_y = tk.DoubleVar(value=initial.logo_y_percent)
        self.logo_width = tk.DoubleVar(value=initial.logo_width_percent)
        self.logo_opacity = tk.DoubleVar(value=initial.logo_opacity * 100.0)
        self.logo_timing_mode = tk.StringVar(
            value=next(
                (
                    label
                    for label, value in _LOGO_TIMING_OPTIONS.items()
                    if value == normalize_timing_mode(
                        initial.logo_timing_mode,
                        allow_once=False,
                    )
                ),
                next(iter(_LOGO_TIMING_OPTIONS)),
            )
        )
        self.logo_start = tk.DoubleVar(value=initial.logo_start_seconds)
        self.logo_end = tk.DoubleVar(value=initial.logo_end_seconds)
        self.logo_duration = tk.DoubleVar(value=initial.logo_duration_seconds)
        self.status = tk.StringVar(
            value="Kéo trực tiếp CTA hoặc logo trên khung hình; thanh trượt sẽ cập nhật theo."
        )
        self._redraw_after_id: str | None = None
        self._background_photo: ImageTk.PhotoImage | None = None
        self._overlay_photos: dict[str, ImageTk.PhotoImage] = {}
        self._overlay_boxes: dict[str, tuple[float, float, float, float]] = {}
        self._drag_kind = ""
        self._drag_offset = (0.0, 0.0)
        self.canvas_width, self.canvas_height = fit_aspect_size(
            (640, 400),
            self.video_size,
        )
        self._preview_source: Image.Image | None = None
        self._preview_layout_after_id: str | None = None
        self._pending_preview_size = (self.canvas_width, self.canvas_height)
        self._pane_layout_after_id: str | None = None
        self._cta_preview_generation = 0
        self._cta_preview_stop_event = threading.Event()
        self._cta_preview_play_event = threading.Event()
        self._cta_preview_play_event.set()
        self._cta_preview_queue: queue.Queue[tuple[object, ...]] = queue.Queue(maxsize=3)
        self._cta_preview_frame: Image.Image | None = None
        self._cta_preview_signature = ""
        self._cta_preview_poll_after_id: str | None = None
        self.cta_preview_playing = tk.BooleanVar(value=True)
        self.cta_preview_button_text = tk.StringVar(value="Tạm dừng CTA")
        self.cta_preview_progress = tk.DoubleVar(value=0.0)
        self.cta_preview_clock = tk.StringVar(value="CTA tĩnh/chưa chọn")
        self.cta_timing_summary = tk.StringVar()
        self.logo_timing_summary = tk.StringVar()
        self._timing_frames: dict[str, dict[str, ttk.Frame]] = {}
        self._pane_ratio = 0.67
        self._pane_min_preview = 500
        self._pane_min_controls = 330

        self._configure_styles()
        self._build()
        for variable in (
            self.cta_enabled,
            self.cta_path,
            self.cta_chroma_key,
            self.cta_key_mode,
            self.cta_x,
            self.cta_y,
            self.cta_width,
            self.cta_timing_mode,
            self.cta_start,
            self.cta_end,
            self.cta_duration,
            self.logo_enabled,
            self.logo_path,
            self.logo_x,
            self.logo_y,
            self.logo_width,
            self.logo_opacity,
            self.logo_timing_mode,
            self.logo_start,
            self.logo_end,
            self.logo_duration,
        ):
            variable.trace_add("write", self._request_redraw)
        self.cta_path.trace_add("write", self._cta_preview_source_changed)
        self.cta_key_mode.trace_add("write", self._cta_preview_source_changed)
        for variable in (
            self.cta_timing_mode,
            self.cta_start,
            self.cta_end,
            self.cta_duration,
            self.logo_timing_mode,
            self.logo_start,
            self.logo_end,
            self.logo_duration,
        ):
            variable.trace_add("write", self._timing_changed)
        self._timing_changed()
        self._draw_background()
        self._redraw()
        self._cta_preview_source_changed()
        self._poll_cta_preview()
        self.window.grab_set()
        self.window.after_idle(self._apply_pane_layout)
        self.window.after_idle(self.apply_button.focus_set)

    def _configure_window_geometry(self) -> None:
        screen_width = max(960, int(self.window.winfo_screenwidth()))
        screen_height = max(680, int(self.window.winfo_screenheight()))
        width = min(screen_width - 40, max(960, round(screen_width * 0.86)))
        height = min(screen_height - 72, max(620, round(screen_height * 0.82)))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.window.minsize(min(width, 920), min(height, 600))

    def _configure_styles(self) -> None:
        style = ttk.Style(self.window)
        style.configure("OverlayRoot.TFrame", background=BACKGROUND)
        style.configure("OverlayPanel.TFrame", background=PANEL)
        style.configure("OverlayCard.TFrame", background=PANEL_ALT)
        style.configure(
            "OverlayTitle.TLabel",
            background=PANEL,
            foreground=TEXT,
            font=("Segoe UI Semibold", 15),
        )
        style.configure(
            "OverlaySection.TLabel",
            background=PANEL,
            foreground=TEXT,
            font=("Segoe UI Semibold", 10),
        )
        style.configure(
            "OverlayMuted.TLabel",
            background=PANEL,
            foreground=MUTED,
            font=("Segoe UI", 9),
        )
        style.configure(
            "OverlayCardMuted.TLabel",
            background=PANEL_ALT,
            foreground=MUTED,
            font=("Segoe UI", 9),
        )
        style.configure(
            "OverlayStatus.TLabel",
            background=PANEL_ALT,
            foreground=SUCCESS,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Overlay.Horizontal.TScale",
            background=PANEL,
            troughcolor=INPUT,
            bordercolor=BORDER,
            lightcolor=TEXT,
            darkcolor=TEXT,
        )

    def _build(self) -> None:
        shell = ttk.Frame(
            self.window,
            style="OverlayRoot.TFrame",
            padding=(16, 14, 16, 16),
        )
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell, style="OverlayPanel.TFrame", padding=(14, 12))
        header.pack(fill="x")
        ttk.Label(
            header,
            text="XEM TRƯỚC CTA & LOGO",
            style="OverlayTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            header,
            text=(
                "Khung xem trước giữ đúng tỷ lệ video. Vị trí và kích thước này sẽ được "
                "dùng cho mọi lần render của workspace."
            ),
            style="OverlayMuted.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        self.body_pane = tk.PanedWindow(
            shell,
            orient="horizontal",
            background=BORDER,
            borderwidth=0,
            relief="flat",
            sashwidth=6,
            sashrelief="flat",
            showhandle=False,
            opaqueresize=False,
        )
        self.body_pane.pack(fill="both", expand=True, pady=(10, 0))
        preview = ttk.Frame(
            self.body_pane,
            style="OverlayCard.TFrame",
            padding=12,
        )
        controls = ttk.Frame(
            self.body_pane,
            style="OverlayPanel.TFrame",
            padding=(12, 0, 0, 0),
        )
        self.body_pane.add(
            preview,
            minsize=self._pane_min_preview,
            stretch="always",
        )
        self.body_pane.add(
            controls,
            minsize=self._pane_min_controls,
            stretch="always",
        )
        self.body_pane.bind("<Configure>", self._schedule_pane_layout, add="+")
        self.body_pane.bind("<ButtonRelease-1>", self._remember_pane_ratio, add="+")

        preview_header = ttk.Frame(preview, style="OverlayCard.TFrame")
        preview_header.pack(fill="x", pady=(0, 9))
        ttk.Label(
            preview_header,
            text=f"KHUNG VIDEO · {self.video_size[0]}×{self.video_size[1]}",
            style="OverlayCardMuted.TLabel",
        ).pack(side="left")
        ttk.Label(
            preview_header,
            text="Kéo CTA/logo trực tiếp trên video",
            style="OverlayCardMuted.TLabel",
        ).pack(side="right")
        self.preview_stage = tk.Frame(
            preview,
            background=BACKGROUND,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        self.preview_stage.pack(fill="both", expand=True)
        self.preview_stage.bind("<Configure>", self._preview_stage_configured)
        self.canvas = tk.Canvas(
            self.preview_stage,
            width=self.canvas_width,
            height=self.canvas_height,
            background="#080808",
            borderwidth=0,
            highlightthickness=0,
        )
        self.canvas.place(relx=0.5, rely=0.5, anchor="center")
        self.canvas.tag_bind("cta", "<ButtonPress-1>", lambda event: self._start_drag("cta", event))
        self.canvas.tag_bind("logo", "<ButtonPress-1>", lambda event: self._start_drag("logo", event))
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._stop_drag)
        playback = ttk.Frame(preview, style="OverlayCard.TFrame")
        playback.pack(fill="x", pady=(8, 0))
        self.cta_preview_button = ttk.Button(
            playback,
            textvariable=self.cta_preview_button_text,
            style="Secondary.TButton",
            command=self._toggle_cta_preview,
            state="disabled",
        )
        self.cta_preview_button.pack(side="left")
        ttk.Progressbar(
            playback,
            variable=self.cta_preview_progress,
            maximum=100.0,
            mode="determinate",
        ).pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Label(
            playback,
            textvariable=self.cta_preview_clock,
            style="OverlayCardMuted.TLabel",
            width=22,
            anchor="e",
        ).pack(side="right")
        ttk.Label(
            preview,
            textvariable=self.status,
            style="OverlayStatus.TLabel",
            wraplength=760,
            justify="left",
        ).pack(
            anchor="w", pady=(8, 0)
        )

        notebook = ttk.Notebook(controls)
        notebook.pack(fill="both", expand=True)
        cta_tab = ttk.Frame(notebook, style="OverlayPanel.TFrame", padding=12)
        logo_tab = ttk.Frame(notebook, style="OverlayPanel.TFrame", padding=12)
        notebook.add(cta_tab, text="CTA chuyển động")
        notebook.add(logo_tab, text="Logo kênh")
        self._build_cta_controls(cta_tab)
        self._build_logo_controls(logo_tab)

        footer = ttk.Frame(
            shell,
            style="OverlayPanel.TFrame",
            padding=(12, 10),
        )
        footer.pack(fill="x")
        ttk.Label(
            footer,
            text="Esc để hủy · Ctrl+Enter để áp dụng",
            style="OverlayMuted.TLabel",
        ).pack(side="left")
        ttk.Button(
            footer,
            text="Hủy",
            style="Secondary.TButton",
            command=self.close,
        ).pack(side="right")
        self.apply_button = ttk.Button(
            footer,
            text="Áp dụng cho workspace",
            style="Accent.TButton",
            command=self._apply,
        )
        self.apply_button.pack(
            side="right", padx=(0, 8)
        )

    def _schedule_pane_layout(self, _event: object | None = None) -> None:
        if self._pane_layout_after_id is not None:
            try:
                self.window.after_cancel(self._pane_layout_after_id)
            except tk.TclError:
                pass
        self._pane_layout_after_id = self.window.after_idle(self._apply_pane_layout)

    def _apply_pane_layout(self) -> None:
        self._pane_layout_after_id = None
        try:
            extent = self.body_pane.winfo_width()
            if extent <= 10:
                return
            position = proportional_pane_position(
                extent,
                self._pane_ratio,
                minimum_primary=self._pane_min_preview,
                minimum_secondary=self._pane_min_controls,
            )
            self.body_pane.sash_place(0, position, 0)
        except tk.TclError:
            pass

    def _remember_pane_ratio(self, _event: object | None = None) -> None:
        try:
            extent = self.body_pane.winfo_width()
            position = self.body_pane.sash_coord(0)[0]
        except tk.TclError:
            return
        if extent > 10:
            self._pane_ratio = max(0.40, min(0.80, position / extent))

    def _preview_stage_configured(self, event: tk.Event) -> None:
        size = fit_aspect_size(
            (max(2, int(event.width)), max(2, int(event.height))),
            self.video_size,
            inset=10,
        )
        self._pending_preview_size = size
        if size == (self.canvas_width, self.canvas_height):
            return
        if self._preview_layout_after_id is not None:
            try:
                self.window.after_cancel(self._preview_layout_after_id)
            except tk.TclError:
                pass
        self._preview_layout_after_id = self.window.after(
            80,
            self._apply_preview_layout,
        )

    def _apply_preview_layout(self) -> None:
        self._preview_layout_after_id = None
        width, height = self._pending_preview_size
        if width == self.canvas_width and height == self.canvas_height:
            return
        self.canvas_width = width
        self.canvas_height = height
        try:
            self.canvas.place_configure(width=width, height=height)
        except tk.TclError:
            return
        self._draw_background()
        self._redraw()

    def _path_controls(
        self,
        parent: ttk.Frame,
        variable: tk.StringVar,
        *,
        kind: str,
    ) -> None:
        row = ttk.Frame(parent, style="OverlayPanel.TFrame")
        row.pack(fill="x", pady=(7, 0))
        ttk.Entry(row, textvariable=variable, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            row,
            text="Tải CTA…" if kind == "cta" else "Tải logo…",
            style="Secondary.TButton",
            command=lambda: self._choose_file(kind),
        ).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(
            row,
            text="Xóa",
            style="Secondary.TButton",
            command=lambda: self._clear_file(kind),
        ).pack(
            side="left", padx=(4, 0)
        )

    def _slider(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.DoubleVar,
        minimum: float,
        maximum: float,
        *,
        suffix: str = "%",
    ) -> None:
        row = ttk.Frame(parent, style="OverlayPanel.TFrame")
        row.pack(fill="x", pady=(12, 0))
        value_label = ttk.Label(
            row,
            width=8,
            anchor="e",
            style="OverlayMuted.TLabel",
        )
        value_label.pack(side="right")
        ttk.Label(
            row,
            text=label,
            style="OverlaySection.TLabel",
        ).pack(side="left")

        def update_label(*_args: object) -> None:
            value_label.configure(text=f"{float(variable.get()):.0f}{suffix}")

        variable.trace_add("write", update_label)
        update_label()
        ttk.Scale(
            parent,
            style="Overlay.Horizontal.TScale",
            from_=minimum,
            to=maximum,
            variable=variable,
        ).pack(
            fill="x", pady=(4, 0)
        )

    def _timing_spinbox_row(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.DoubleVar,
    ) -> ttk.Frame:
        row = ttk.Frame(parent, style="OverlayPanel.TFrame")
        ttk.Label(
            row,
            text=label,
            style="OverlaySection.TLabel",
        ).pack(side="left")
        ttk.Spinbox(
            row,
            style="Donut.TSpinbox",
            from_=0.0,
            to=86400.0,
            increment=0.5,
            textvariable=variable,
            width=9,
        ).pack(side="right")
        return row

    def _build_timing_controls(self, parent: ttk.Frame, *, kind: str) -> None:
        is_cta = kind == "cta"
        options = _CTA_TIMING_OPTIONS if is_cta else _LOGO_TIMING_OPTIONS
        mode_var = self.cta_timing_mode if is_cta else self.logo_timing_mode
        start_var = self.cta_start if is_cta else self.logo_start
        end_var = self.cta_end if is_cta else self.logo_end
        duration_var = self.cta_duration if is_cta else self.logo_duration
        ttk.Label(
            parent,
            text="THỜI GIAN XUẤT HIỆN",
            style="OverlaySection.TLabel",
        ).pack(anchor="w", pady=(14, 4))
        combo = ttk.Combobox(
            parent,
            style="Donut.TCombobox",
            state="readonly",
            textvariable=mode_var,
            values=tuple(options),
        )
        combo.pack(fill="x")
        field_host = ttk.Frame(parent, style="OverlayPanel.TFrame")
        field_host.pack(fill="x")
        frames = {
            "start": self._timing_spinbox_row(
                field_host,
                "Bắt đầu từ giây",
                start_var,
            ),
            "end": self._timing_spinbox_row(
                field_host,
                "Kết thúc ở giây",
                end_var,
            ),
            "duration": self._timing_spinbox_row(
                field_host,
                "Thời lượng hiển thị",
                duration_var,
            ),
        }
        self._timing_frames[kind] = frames
        ttk.Label(
            parent,
            textvariable=(self.cta_timing_summary if is_cta else self.logo_timing_summary),
            style="OverlayMuted.TLabel",
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

    def _build_cta_controls(self, parent: ttk.Frame) -> None:
        ttk.Checkbutton(parent, text="Chèn CTA vào video", variable=self.cta_enabled).pack(anchor="w")
        ttk.Label(
            parent,
            text="Tệp CTA: MP4, WebM, MOV, GIF hoặc ảnh",
            style="OverlayMuted.TLabel",
        ).pack(anchor="w", pady=(10, 0))
        self._path_controls(parent, self.cta_path, kind="cta")
        ttk.Label(
            parent,
            text="Xử lý phông CTA",
            style="OverlaySection.TLabel",
        ).pack(anchor="w", pady=(10, 4))
        self.cta_key_combo = ttk.Combobox(
            parent,
            style="Donut.TCombobox",
            state="readonly",
            textvariable=self.cta_key_mode,
            values=tuple(_KEY_MODE_OPTIONS),
        )
        self.cta_key_combo.pack(fill="x")
        ttk.Label(
            parent,
            text="auto = tự nhận nền xanh, nền đen hoặc nền trong suốt",
            style="OverlayMuted.TLabel",
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))
        self._build_timing_controls(parent, kind="cta")
        self._slider(parent, "Vị trí ngang", self.cta_x, 0, 100)
        self._slider(parent, "Vị trí dọc", self.cta_y, 0, 100)
        self._slider(parent, "Kích thước", self.cta_width, 5, 60)
        ttk.Label(
            parent,
            text=(
                "Khung bên trái chạy toàn bộ video CTA rồi lặp lại. Đây là preview nhẹ; "
                "video xuất cuối dùng tệp gốc và đúng khoảng thời gian đã chọn."
            ),
            style="OverlayMuted.TLabel",
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(14, 0))

    def _build_logo_controls(self, parent: ttk.Frame) -> None:
        ttk.Checkbutton(parent, text="Chèn logo vào video", variable=self.logo_enabled).pack(anchor="w")
        ttk.Label(
            parent,
            text="Tệp logo: ưu tiên PNG nền trong suốt",
            style="OverlayMuted.TLabel",
        ).pack(anchor="w", pady=(10, 0))
        self._path_controls(parent, self.logo_path, kind="logo")
        self._build_timing_controls(parent, kind="logo")
        self._slider(parent, "Vị trí ngang", self.logo_x, 0, 100)
        self._slider(parent, "Vị trí dọc", self.logo_y, 0, 100)
        self._slider(parent, "Kích thước", self.logo_width, 2, 40)
        self._slider(parent, "Độ rõ", self.logo_opacity, 0, 100)
        ttk.Label(
            parent,
            text=(
                "0% là trong suốt hoàn toàn; 100% là giữ nguyên logo. Thiết lập thời gian, "
                "vị trí, kích thước và độ rõ đều được áp dụng vào video xuất."
            ),
            style="OverlayMuted.TLabel",
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(14, 0))

    def _choose_file(self, kind: str) -> None:
        if kind == "cta":
            title = "Chọn CTA chuyển động"
            filetypes = [
                ("CTA video/ảnh", "*.mp4 *.webm *.mov *.mkv *.avi *.gif *.png *.jpg *.jpeg *.webp"),
                ("Tất cả tệp", "*.*"),
            ]
            variable = self.cta_path
        else:
            title = "Chọn logo kênh"
            filetypes = [
                ("Ảnh logo", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"),
                ("Tất cả tệp", "*.*"),
            ]
            variable = self.logo_path
        current = Path(variable.get()).expanduser()
        selected = filedialog.askopenfilename(
            parent=self.window,
            title=title,
            initialdir=str(current.parent) if current.is_file() else None,
            filetypes=filetypes,
        )
        if selected:
            variable.set(selected)
            if kind == "cta":
                self.cta_enabled.set(True)
            else:
                self.logo_enabled.set(True)

    def _clear_file(self, kind: str) -> None:
        if kind == "cta":
            self.cta_path.set("")
            self.cta_enabled.set(False)
        else:
            self.logo_path.set("")
            self.logo_enabled.set(False)

    def _timing_changed(self, *_args: object) -> None:
        for kind, options, mode_var, summary_var, start_var, end_var, duration_var in (
            (
                "cta",
                _CTA_TIMING_OPTIONS,
                self.cta_timing_mode,
                self.cta_timing_summary,
                self.cta_start,
                self.cta_end,
                self.cta_duration,
            ),
            (
                "logo",
                _LOGO_TIMING_OPTIONS,
                self.logo_timing_mode,
                self.logo_timing_summary,
                self.logo_start,
                self.logo_end,
                self.logo_duration,
            ),
        ):
            frames = self._timing_frames.get(kind)
            if frames is None:
                continue
            mode = options.get(mode_var.get(), "once" if kind == "cta" else "full")
            for frame in frames.values():
                frame.pack_forget()
            visible = (
                ("start",)
                if mode == "once"
                else ("duration",)
                if mode in {"start", "end"}
                else ("start", "end")
                if mode == "custom"
                else ()
            )
            for name in visible:
                frames[name].pack(fill="x", pady=(8, 0))
            try:
                summary_var.set(
                    timing_summary(
                        mode,
                        start_seconds=float(start_var.get()),
                        end_seconds=float(end_var.get()),
                        duration_seconds=float(duration_var.get()),
                        allow_once=kind == "cta",
                    )
                )
            except (TypeError, ValueError, tk.TclError):
                summary_var.set("Hãy nhập thời gian hợp lệ.")

    @staticmethod
    def _preview_clock_text(seconds: float) -> str:
        total = max(0, round(float(seconds)))
        minutes, remainder = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{remainder:02d}"
        return f"{minutes:02d}:{remainder:02d}"

    def _toggle_cta_preview(self) -> None:
        playing = not bool(self.cta_preview_playing.get())
        self.cta_preview_playing.set(playing)
        self.cta_preview_button_text.set("Tạm dừng CTA" if playing else "Phát CTA")
        if playing:
            self._cta_preview_play_event.set()
        else:
            self._cta_preview_play_event.clear()

    def _cta_preview_source_changed(self, *_args: object) -> None:
        path_text = self.cta_path.get().strip()
        key_mode = _KEY_MODE_OPTIONS.get(self.cta_key_mode.get(), "auto")
        signature = f"{path_text}|{key_mode}"
        if signature == self._cta_preview_signature:
            return
        self._cta_preview_signature = signature
        self._cta_preview_generation += 1
        generation = self._cta_preview_generation
        self._cta_preview_stop_event.set()
        self._cta_preview_stop_event = threading.Event()
        stop_event = self._cta_preview_stop_event
        self._cta_preview_frame = None
        self.cta_preview_progress.set(0.0)
        while True:
            try:
                self._cta_preview_queue.get_nowait()
            except queue.Empty:
                break
        path = Path(path_text).expanduser().resolve(strict=False) if path_text else None
        if (
            path is None
            or not path.is_file()
            or path.suffix.casefold() not in _ANIMATED_CTA_EXTENSIONS
        ):
            self.cta_preview_button.configure(state="disabled")
            self.cta_preview_clock.set("CTA tĩnh/chưa chọn")
            self._request_redraw()
            return
        self.cta_preview_button.configure(state="normal")
        self.cta_preview_playing.set(True)
        self.cta_preview_button_text.set("Tạm dừng CTA")
        self._cta_preview_play_event.set()
        self.cta_preview_clock.set("Đang nạp toàn bộ CTA…")
        self.status.set("Đang mở luồng xem trước CTA; cửa sổ vẫn có thể thao tác bình thường.")
        threading.Thread(
            target=self._cta_preview_worker,
            args=(generation, path, key_mode, stop_event),
            daemon=True,
            name=f"CTA-preview-{generation}",
        ).start()

    def _preview_duration(self, path: Path) -> float:
        if self.ffmpeg_path is None:
            return 0.0
        probe_name = "ffprobe.exe" if self.ffmpeg_path.suffix.casefold() == ".exe" else "ffprobe"
        ffprobe_path = self.ffmpeg_path.with_name(probe_name)
        if not ffprobe_path.is_file():
            return 0.0
        completed = subprocess.run(
            [
                str(ffprobe_path),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
            creationflags=_creation_flags(),
            text=True,
        )
        try:
            return max(0.0, float(completed.stdout.strip()))
        except (TypeError, ValueError):
            return 0.0

    def _put_cta_preview_event(
        self,
        event: tuple[object, ...],
        stop_event: threading.Event,
    ) -> bool:
        while not stop_event.is_set():
            try:
                self._cta_preview_queue.put(event, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def _cta_preview_worker(
        self,
        generation: int,
        path: Path,
        key_mode: str,
        stop_event: threading.Event,
    ) -> None:
        process: subprocess.Popen[bytes] | None = None
        try:
            command, size, effective = animated_preview_command(
                path,
                self.ffmpeg_path,
                key_mode,
            )
            duration = self._preview_duration(path)
            frame_size = size[0] * size[1] * 4
            while not stop_event.is_set():
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=_creation_flags(),
                )
                if process.stdout is None:
                    raise RuntimeError("Không mở được luồng hình CTA.")
                frame_index = 0
                while not stop_event.is_set():
                    if not self._cta_preview_play_event.wait(timeout=0.2):
                        continue
                    payload = process.stdout.read(frame_size)
                    if len(payload) != frame_size:
                        break
                    frame = Image.frombytes("RGBA", size, payload)
                    if not self._put_cta_preview_event(
                        (
                            "frame",
                            generation,
                            frame,
                            frame_index,
                            duration,
                            effective,
                        ),
                        stop_event,
                    ):
                        return
                    frame_index += 1
                if stop_event.is_set():
                    return
                return_code = process.wait(timeout=5)
                if frame_index == 0 or return_code != 0:
                    detail = ""
                    if process.stderr is not None:
                        detail = process.stderr.read().decode("utf-8", errors="replace").strip()
                    raise RuntimeError(detail or "Không phát được video CTA.")
                self._put_cta_preview_event(("loop", generation), stop_event)
        except Exception as exc:
            self._put_cta_preview_event(("error", generation, str(exc)), stop_event)
        finally:
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass

    def _poll_cta_preview(self) -> None:
        self._cta_preview_poll_after_id = None
        if not self.window.winfo_exists():
            return
        if self.cta_preview_playing.get():
            try:
                event = self._cta_preview_queue.get_nowait()
            except queue.Empty:
                event = None
            if event is not None and int(event[1]) == self._cta_preview_generation:
                if event[0] == "frame":
                    _kind, _generation, frame, frame_index, duration, effective = event
                    assert isinstance(frame, Image.Image)
                    self._cta_preview_frame = frame
                    elapsed = float(frame_index) / _CTA_PREVIEW_FPS
                    if float(duration) > 0:
                        self.cta_preview_progress.set(
                            min(100.0, elapsed * 100.0 / float(duration))
                        )
                        self.cta_preview_clock.set(
                            f"{self._preview_clock_text(elapsed)} / "
                            f"{self._preview_clock_text(float(duration))}"
                        )
                    else:
                        self.cta_preview_clock.set(
                            f"Đang phát · {self._preview_clock_text(elapsed)}"
                        )
                    labels = {
                        "green": "đã xóa nền xanh",
                        "black": "đã xóa nền đen",
                        "none": "giữ nền gốc/trong suốt",
                    }
                    self.status.set(
                        f"CTA đang chạy toàn bộ rồi lặp lại · {labels.get(str(effective), effective)}."
                    )
                    self._request_redraw()
                elif event[0] == "loop":
                    self.cta_preview_progress.set(0.0)
                elif event[0] == "error":
                    self.cta_preview_button.configure(state="disabled")
                    self.cta_preview_clock.set("Không chạy được CTA")
                    self.status.set(f"Preview CTA lỗi: {event[2]}")
        self._cta_preview_poll_after_id = self.window.after(125, self._poll_cta_preview)

    def _draw_background(self) -> None:
        if self._preview_source is None:
            try:
                if self.background_path is None or not self.background_path.is_file():
                    raise FileNotFoundError
                self._preview_source = load_preview_frame(
                    self.background_path,
                    self.ffmpeg_path,
                ).convert("RGB")
            except Exception:
                source = Image.new("RGB", (960, 540), "#171717")
                draw = ImageDraw.Draw(source)
                for y in range(source.height):
                    shade = 20 + round(22 * y / max(1, source.height - 1))
                    draw.line(
                        (0, y, source.width, y),
                        fill=(shade, shade, shade + 4),
                    )
                label = "KHUNG VIDEO"
                bounds = draw.textbbox((0, 0), label, font=_font(28, bold=True))
                draw.text(
                    (
                        (source.width - (bounds[2] - bounds[0])) / 2,
                        source.height / 2,
                    ),
                    label,
                    font=_font(28, bold=True),
                    fill="#777777",
                )
                self._preview_source = source
        background = compose_video_preview(
            self._preview_source,
            (self.canvas_width, self.canvas_height),
            self.framing,
        )
        self._background_photo = ImageTk.PhotoImage(background, master=self.window)
        self.canvas.delete("background")
        self.canvas.create_image(0, 0, image=self._background_photo, anchor="nw", tags=("background",))
        self.canvas.tag_lower("background")

    def _request_redraw(self, *_args: object) -> None:
        if self._redraw_after_id is not None:
            return
        self._redraw_after_id = self.window.after_idle(self._redraw)

    def _overlay_image(self, kind: str, width_percent: float) -> Image.Image:
        path_text = self.cta_path.get() if kind == "cta" else self.logo_path.get()
        target_width = max(24, round(self.canvas_width * width_percent / 100.0))
        try:
            path = Path(path_text).expanduser().resolve(strict=False)
            if not path.is_file():
                raise FileNotFoundError
            image = (
                self._cta_preview_frame.copy()
                if kind == "cta" and self._cta_preview_frame is not None
                else load_preview_frame(path, self.ffmpeg_path)
            )
            key_mode = _KEY_MODE_OPTIONS.get(self.cta_key_mode.get(), "auto")
            if (
                kind == "cta"
                and self._cta_preview_frame is None
                and key_mode != "none"
            ):
                image, effective = remove_keyed_background(
                    image,
                    key_mode,
                )
                labels = {
                    "green": "đã nhận nền xanh",
                    "black": "đã nhận nền đen",
                    "none": "nền trong suốt/không cần xóa",
                }
                self.status.set(
                    f"CTA: {labels.get(effective, effective)} · kéo trực tiếp để đổi vị trí."
                )
        except Exception:
            ratio = 3.8 if kind == "cta" else 1.0
            return _placeholder(
                (target_width, max(24, round(target_width / ratio))),
                "CTA" if kind == "cta" else "LOGO",
                "#dc2626" if kind == "cta" else "#27272a",
            )
        target_height = max(2, round(target_width * image.height / max(1, image.width)))
        return image.resize((target_width, target_height), Image.Resampling.LANCZOS)

    def _draw_overlay(self, kind: str) -> None:
        enabled = self.cta_enabled.get() if kind == "cta" else self.logo_enabled.get()
        self.canvas.delete(kind)
        self._overlay_boxes.pop(kind, None)
        self._overlay_photos.pop(kind, None)
        if not enabled:
            return
        width_percent = self.cta_width.get() if kind == "cta" else self.logo_width.get()
        x_percent = self.cta_x.get() if kind == "cta" else self.logo_x.get()
        y_percent = self.cta_y.get() if kind == "cta" else self.logo_y.get()
        image = self._overlay_image(kind, width_percent)
        if kind == "logo":
            alpha = max(0.0, min(1.0, self.logo_opacity.get() / 100.0))
            image.putalpha(image.getchannel("A").point(lambda value: round(value * alpha)))
        x = max(0.0, self.canvas_width - image.width) * max(0.0, min(100.0, x_percent)) / 100.0
        y = max(0.0, self.canvas_height - image.height) * max(0.0, min(100.0, y_percent)) / 100.0
        photo = ImageTk.PhotoImage(image, master=self.window)
        self._overlay_photos[kind] = photo
        self.canvas.create_image(x, y, image=photo, anchor="nw", tags=(kind, "overlay"))
        self._overlay_boxes[kind] = (x, y, image.width, image.height)

    def _redraw(self) -> None:
        self._redraw_after_id = None
        self._draw_overlay("cta")
        self._draw_overlay("logo")

    def _start_drag(self, kind: str, event: tk.Event) -> None:
        box = self._overlay_boxes.get(kind)
        if box is None:
            return
        self._drag_kind = kind
        self._drag_offset = (event.x - box[0], event.y - box[1])
        self.canvas.tag_raise(kind)

    def _drag(self, event: tk.Event) -> None:
        if not self._drag_kind:
            return
        box = self._overlay_boxes.get(self._drag_kind)
        if box is None:
            return
        _old_x, _old_y, width, height = box
        x = max(0.0, min(self.canvas_width - width, event.x - self._drag_offset[0]))
        y = max(0.0, min(self.canvas_height - height, event.y - self._drag_offset[1]))
        x_percent = 0.0 if self.canvas_width <= width else x * 100.0 / (self.canvas_width - width)
        y_percent = 0.0 if self.canvas_height <= height else y * 100.0 / (self.canvas_height - height)
        if self._drag_kind == "cta":
            self.cta_x.set(x_percent)
            self.cta_y.set(y_percent)
        else:
            self.logo_x.set(x_percent)
            self.logo_y.set(y_percent)

    def _stop_drag(self, _event: tk.Event | None = None) -> None:
        self._drag_kind = ""

    def _apply(self) -> None:
        cta_path = Path(self.cta_path.get()).expanduser().resolve(strict=False) if self.cta_path.get().strip() else None
        logo_path = Path(self.logo_path.get()).expanduser().resolve(strict=False) if self.logo_path.get().strip() else None
        if self.cta_enabled.get() and (cta_path is None or not cta_path.is_file()):
            messagebox.showerror("Chưa có CTA", "Hãy chọn tệp CTA hợp lệ hoặc tắt CTA.", parent=self.window)
            return
        if self.logo_enabled.get() and (logo_path is None or not logo_path.is_file()):
            messagebox.showerror("Chưa có logo", "Hãy chọn ảnh logo hợp lệ hoặc tắt logo.", parent=self.window)
            return
        try:
            cta_timing_mode, cta_start, cta_end, cta_duration = validate_timing_values(
                _CTA_TIMING_OPTIONS.get(self.cta_timing_mode.get(), "once"),
                start_seconds=float(self.cta_start.get()),
                end_seconds=float(self.cta_end.get()),
                duration_seconds=float(self.cta_duration.get()),
                allow_once=True,
            )
            logo_timing_mode, logo_start, logo_end, logo_duration = validate_timing_values(
                _LOGO_TIMING_OPTIONS.get(self.logo_timing_mode.get(), "full"),
                start_seconds=float(self.logo_start.get()),
                end_seconds=float(self.logo_end.get()),
                duration_seconds=float(self.logo_duration.get()),
                allow_once=False,
            )
        except (TypeError, ValueError, tk.TclError) as exc:
            messagebox.showerror(
                "Thời gian xuất hiện chưa hợp lệ",
                str(exc),
                parent=self.window,
            )
            return
        state = OverlayEditorState(
            cta_enabled=bool(self.cta_enabled.get()),
            cta_path=str(cta_path) if cta_path is not None else "",
            cta_chroma_key=(
                _KEY_MODE_OPTIONS.get(self.cta_key_mode.get(), "auto") != "none"
            ),
            cta_key_mode=_KEY_MODE_OPTIONS.get(self.cta_key_mode.get(), "auto"),
            cta_x_percent=max(0.0, min(100.0, float(self.cta_x.get()))),
            cta_y_percent=max(0.0, min(100.0, float(self.cta_y.get()))),
            cta_width_percent=max(5.0, min(60.0, float(self.cta_width.get()))),
            cta_timing_mode=cta_timing_mode,
            cta_start_seconds=cta_start,
            cta_end_seconds=cta_end,
            cta_duration_seconds=cta_duration,
            logo_enabled=bool(self.logo_enabled.get()),
            logo_path=str(logo_path) if logo_path is not None else "",
            logo_x_percent=max(0.0, min(100.0, float(self.logo_x.get()))),
            logo_y_percent=max(0.0, min(100.0, float(self.logo_y.get()))),
            logo_width_percent=max(2.0, min(40.0, float(self.logo_width.get()))),
            logo_opacity=max(0.0, min(1.0, float(self.logo_opacity.get()) / 100.0)),
            logo_timing_mode=logo_timing_mode,
            logo_start_seconds=logo_start,
            logo_end_seconds=logo_end,
            logo_duration_seconds=logo_duration,
        )
        try:
            self.on_apply(state)
        except Exception as exc:
            messagebox.showerror(
                "Không lưu được CTA/logo",
                str(exc),
                parent=self.window,
            )
            return
        self.close()

    def close(self) -> None:
        self._cta_preview_generation += 1
        self._cta_preview_stop_event.set()
        self._cta_preview_play_event.set()
        for attribute in (
            "_redraw_after_id",
            "_preview_layout_after_id",
            "_pane_layout_after_id",
            "_cta_preview_poll_after_id",
        ):
            after_id = getattr(self, attribute, None)
            if after_id is None:
                continue
            try:
                self.window.after_cancel(after_id)
            except tk.TclError:
                pass
            setattr(self, attribute, None)
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        try:
            self.window.destroy()
        except tk.TclError:
            pass
