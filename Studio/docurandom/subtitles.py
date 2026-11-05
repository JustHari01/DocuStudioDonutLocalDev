from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


DEFAULT_LINE_UNITS = 52  # roughly 26 full-width Japanese glyphs
DEFAULT_MAX_LINES = 2
DEFAULT_FOREGROUND_SCALE = 0.90
SUBTITLE_PANEL_HEIGHT_RATIO = 0.20


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    text: str


def _display_units(text: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        for character in text
    )


def _clean_text(text: str) -> str:
    # Ignore ASS override tags when measuring/splitting a render-ready SRT;
    # they are styling metadata, not spoken characters.
    text = re.sub(r"\{\\[^}]*\}", "", text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return " ".join(line for line in lines if line).strip()


def _take_prefix(text: str, limit: int) -> tuple[str, str]:
    if _display_units(text) <= limit:
        return text.strip(), ""
    used = 0
    hard_index = 0
    preferred_index = 0
    preferred_marks = "。！？!?、，,；;：: 　"
    for index, character in enumerate(text, 1):
        used += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        if used > limit:
            break
        hard_index = index
        if character in preferred_marks and used >= round(limit * 0.55):
            preferred_index = index
    split_at = preferred_index or hard_index or 1
    head = text[:split_at].strip()
    tail = text[split_at:].strip()
    return head, tail


def split_subtitle_text(
    text: str,
    *,
    line_units: int = DEFAULT_LINE_UNITS,
    max_lines: int = DEFAULT_MAX_LINES,
) -> list[str]:
    """Split narration into cues that are each at most two safe-width lines."""

    remaining = _clean_text(text)
    if not remaining:
        return []
    cue_limit = max(8, int(line_units)) * max(1, int(max_lines))
    chunks: list[str] = []
    while remaining:
        chunk, remaining = _take_prefix(remaining, cue_limit)
        lines: list[str] = []
        line_remaining = chunk
        while line_remaining and len(lines) < max_lines:
            line, line_remaining = _take_prefix(line_remaining, line_units)
            lines.append(line)
        if line_remaining:
            # This only occurs with unusual combining/control characters. Keep
            # every character by placing the remainder back at the front.
            remaining = f"{line_remaining}{remaining}"
        chunks.append("\n".join(lines))
    return chunks


def split_timed_cue(
    text: str,
    start: float,
    end: float,
    *,
    line_units: int = DEFAULT_LINE_UNITS,
) -> list[SubtitleCue]:
    chunks = split_subtitle_text(text, line_units=line_units, max_lines=2)
    if not chunks:
        return []
    duration = max(0.001, float(end) - float(start))
    weights = [max(1, _display_units(chunk.replace("\n", ""))) for chunk in chunks]
    total_weight = sum(weights)
    cues: list[SubtitleCue] = []
    cursor = float(start)
    for index, (chunk, weight) in enumerate(zip(chunks, weights, strict=True)):
        cue_end = (
            float(end)
            if index == len(chunks) - 1
            else cursor + duration * weight / total_weight
        )
        cues.append(SubtitleCue(cursor, max(cursor + 0.001, cue_end), chunk))
        cursor = cue_end
    return cues


def format_srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(float(seconds) * 1000.0))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def write_srt(
    cues: list[SubtitleCue],
    destination: Path,
    *,
    override_tag: str = "",
) -> None:
    blocks = [
        f"{index}\n{format_srt_timestamp(cue.start)} --> "
        f"{format_srt_timestamp(cue.end)}\n{override_tag}{cue.text}"
        for index, cue in enumerate(cues, 1)
    ]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")


_TIMING = re.compile(
    r"^(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _timestamp_seconds(parts: tuple[str, str, str, str]) -> float:
    hours, minutes, seconds, milliseconds = (int(value) for value in parts)
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000.0


def read_srt(path: Path, *, line_units: int = DEFAULT_LINE_UNITS) -> list[SubtitleCue]:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[SubtitleCue] = []
    for block in re.split(r"\n{2,}", text.strip()):
        lines = block.splitlines()
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        match = _TIMING.match(lines[timing_index].strip())
        if not match:
            continue
        body = _clean_text("\n".join(lines[timing_index + 1 :]))
        if not body:
            continue
        start = _timestamp_seconds(match.groups()[:4])
        end = _timestamp_seconds(match.groups()[4:])
        cues.extend(
            split_timed_cue(
                body,
                start,
                max(start + 0.001, end),
                line_units=line_units,
            )
        )
    return cues


def subtitle_line_units(width: int, height: int) -> int:
    font_size = max(24, round(height * 0.050))
    usable_width = max(1, round(width * 0.85))
    full_width_glyphs = max(8, usable_width // font_size)
    return max(16, min(DEFAULT_LINE_UNITS, full_width_glyphs * 2))


def prepare_srt_for_render(
    source: Path,
    destination: Path,
    *,
    line_units: int = DEFAULT_LINE_UNITS,
) -> int:
    cues = read_srt(source, line_units=line_units)
    if not cues:
        raise ValueError("Tệp subtitle không có cue SRT hợp lệ.")
    # libass applies this edge-blur tag to the outline while keeping the
    # white glyph fill readable. It avoids a harsh, razor-sharp sticker look.
    write_srt(cues, destination, override_tag=r"{\be1}")
    return len(cues)


def _ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(float(seconds) * 100.0))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def _ass_safe_text(text: str) -> str:
    # Subtitle text is user/AI supplied. Neutralize ASS control characters so
    # dialogue cannot inject style overrides, while preserving visible text.
    return (
        text.replace("\\", "＼")
        .replace("{", "｛")
        .replace("}", "｝")
        .replace("\n", r"\N")
    )


def _subtitle_style_metrics(width: int, height: int) -> tuple[int, int, int, int]:
    # Yu Gothic's Japanese glyph metrics match the approved 1080p reference
    # at about 80 px. Scale from output height for equivalent 720p/4K framing.
    font_size = max(24, round(height * 0.074))
    outline = max(2, round(height * 0.005))
    horizontal_margin = max(32, round(width * 0.075))
    bottom_margin = max(42, round(height * 0.080))
    return font_size, outline, horizontal_margin, bottom_margin


def subtitle_panel_geometry(
    width: int,
    height: int,
    *,
    foreground_scale: float = DEFAULT_FOREGROUND_SCALE,
) -> tuple[int, int, int, int]:
    """Return the panel box fitted to the centred foreground picture."""

    width = max(1, int(width))
    height = max(1, int(height))
    scale = max(0.10, min(1.0, float(foreground_scale)))
    foreground_width = max(1, round(width * scale))
    foreground_height = max(1, round(height * scale))
    foreground_x = (width - foreground_width) // 2
    foreground_y = (height - foreground_height) // 2
    panel_height = max(1, round(height * SUBTITLE_PANEL_HEIGHT_RATIO))
    panel_height = min(panel_height, foreground_height)
    panel_y = foreground_y + foreground_height - panel_height
    return foreground_x, panel_y, foreground_width, panel_height


def prepare_ass_for_render(
    source: Path,
    destination: Path,
    *,
    width: int,
    height: int,
    line_units: int = DEFAULT_LINE_UNITS,
    foreground_scale: float = DEFAULT_FOREGROUND_SCALE,
) -> int:
    """Convert SRT to a render-sized ASS file with deterministic typography.

    libass gives plain SRT an implicit 384x288 script canvas. Passing pixel
    values calculated for a 1080p output through ``force_style`` therefore
    enlarged font, outline, and margins several times. An explicit PlayRes
    makes every style value use the actual output coordinate system.
    """

    width = max(1, int(width))
    height = max(1, int(height))
    cues = read_srt(source, line_units=line_units)
    if not cues:
        raise ValueError("Tệp subtitle không có cue SRT hợp lệ.")
    font_size, outline, horizontal_margin, bottom_margin = _subtitle_style_metrics(
        width, height
    )
    panel_x, panel_y, panel_width, panel_height = subtitle_panel_geometry(
        width,
        height,
        foreground_scale=foreground_scale,
    )
    subtitle_x = panel_x + panel_width // 2
    subtitle_y = panel_y + panel_height // 2
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Yu Gothic,"
        f"{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},0,5,"
        f"{horizontal_margin},{horizontal_margin},{bottom_margin},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    dialogue = "\n".join(
        "Dialogue: 0,"
        f"{_ass_timestamp(cue.start)},{_ass_timestamp(max(cue.start + 0.01, cue.end))},"
        f"Default,,0,0,0,,{{\\an5\\pos({subtitle_x},{subtitle_y})\\be1}}"
        f"{_ass_safe_text(cue.text)}"
        for cue in cues
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(f"{header}{dialogue}\n", encoding="utf-8-sig")
    return len(cues)


def subtitle_force_style(width: int, height: int) -> str:
    # Scaling these values from the output height keeps 720p/1080p/vertical
    # exports visually equivalent. The translucent panel itself is drawn by
    # the renderer; BorderStyle=1 keeps the black treatment as a clean outline
    # instead of creating an opaque box behind every individual line.
    font_size, outline, horizontal_margin, bottom_margin = _subtitle_style_metrics(
        width, height
    )
    return ",".join(
        (
            "FontName=Yu Gothic",
            f"FontSize={font_size}",
            "Bold=-1",
            "PrimaryColour=&H00FFFFFF",
            "OutlineColour=&H00000000",
            "BackColour=&H00000000",
            "BorderStyle=1",
            f"Outline={outline}",
            "Shadow=0",
            "Alignment=5",
            f"MarginL={horizontal_margin}",
            f"MarginR={horizontal_margin}",
            f"MarginV={bottom_margin}",
        )
    )
