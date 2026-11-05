from __future__ import annotations

from dataclasses import dataclass


TIMING_MODES = frozenset({"once", "full", "start", "end", "custom"})


@dataclass(frozen=True)
class OverlayWindow:
    start_seconds: float
    end_seconds: float | None
    loop_media: bool


def normalize_timing_mode(value: str, *, allow_once: bool) -> str:
    mode = str(value or "").strip().casefold()
    allowed = TIMING_MODES if allow_once else TIMING_MODES - {"once"}
    if mode in allowed:
        return mode
    return "once" if allow_once else "full"


def validate_timing_values(
    mode: str,
    *,
    start_seconds: float,
    end_seconds: float,
    duration_seconds: float,
    allow_once: bool,
) -> tuple[str, float, float, float]:
    normalized = normalize_timing_mode(mode, allow_once=allow_once)
    start = float(start_seconds)
    end = float(end_seconds)
    duration = float(duration_seconds)
    if start < 0:
        raise ValueError("Thời điểm bắt đầu phải từ 0 giây trở lên.")
    if end < 0:
        raise ValueError("Thời điểm kết thúc phải từ 0 giây trở lên.")
    if duration <= 0:
        raise ValueError("Thời lượng xuất hiện phải lớn hơn 0 giây.")
    if normalized == "custom" and end <= start:
        raise ValueError("Thời điểm kết thúc phải lớn hơn thời điểm bắt đầu.")
    return normalized, start, end, duration


def resolve_overlay_window(
    mode: str,
    *,
    start_seconds: float,
    end_seconds: float,
    duration_seconds: float,
    total_duration: float,
    allow_once: bool,
) -> OverlayWindow:
    normalized, start, end, duration = validate_timing_values(
        mode,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        duration_seconds=duration_seconds,
        allow_once=allow_once,
    )
    total = max(0.0, float(total_duration))
    if normalized == "full":
        return OverlayWindow(0.0, None, True)
    if normalized == "once":
        return OverlayWindow(start, None, False)
    if normalized == "start":
        return OverlayWindow(0.0, min(total, duration), True)
    if normalized == "end":
        return OverlayWindow(max(0.0, total - duration), total, True)
    return OverlayWindow(min(start, total), min(end, total), True)


def timing_summary(
    mode: str,
    *,
    start_seconds: float,
    end_seconds: float,
    duration_seconds: float,
    allow_once: bool,
) -> str:
    normalized = normalize_timing_mode(mode, allow_once=allow_once)
    if normalized == "full":
        return "Hiện trong toàn bộ video"
    if normalized == "once":
        return f"Phát một lần từ {_seconds_text(start_seconds)}"
    if normalized == "start":
        return f"Hiện {_seconds_text(duration_seconds)} đầu video"
    if normalized == "end":
        return f"Hiện {_seconds_text(duration_seconds)} cuối video"
    return f"Hiện từ {_seconds_text(start_seconds)} đến {_seconds_text(end_seconds)}"


def _seconds_text(value: float) -> str:
    seconds = max(0.0, float(value))
    if seconds.is_integer():
        return f"{int(seconds)} giây"
    return f"{seconds:.1f} giây"
