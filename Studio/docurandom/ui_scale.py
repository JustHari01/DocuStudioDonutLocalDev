from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WindowMetrics:
    width: int
    height: int
    minimum_width: int
    minimum_height: int
    x: int
    y: int
    font_scale: float


def responsive_window_metrics(screen_width: int, screen_height: int) -> WindowMetrics:
    """Return a proportional initial window that still fits small displays."""

    safe_width = max(900, int(screen_width))
    safe_height = max(680, int(screen_height))
    ratio = min(safe_width / 1920.0, safe_height / 1080.0)
    layout_scale = max(0.82, min(1.20, ratio))
    width = min(safe_width - 32, max(900, round(1360 * layout_scale)))
    height = min(safe_height - 64, max(680, round(940 * layout_scale)))
    minimum_width = min(width, max(820, round(980 * layout_scale)))
    minimum_height = min(height, max(620, round(700 * layout_scale)))
    x = max(0, (safe_width - width) // 2)
    y = max(0, (safe_height - height) // 2)
    return WindowMetrics(
        width=width,
        height=height,
        minimum_width=minimum_width,
        minimum_height=minimum_height,
        x=x,
        y=y,
        font_scale=max(0.86, min(1.16, layout_scale)),
    )


def configure_responsive_window(root: object) -> WindowMetrics:
    """Apply scaling once per Tk root and refresh only its target geometry."""

    screen_width = int(root.winfo_screenwidth())  # type: ignore[attr-defined]
    screen_height = int(root.winfo_screenheight())  # type: ignore[attr-defined]
    metrics = responsive_window_metrics(screen_width, screen_height)
    if not getattr(root, "_docurandom_scaling_configured", False):
        try:
            current = float(root.tk.call("tk", "scaling"))  # type: ignore[attr-defined]
            root.tk.call("tk", "scaling", current * metrics.font_scale)  # type: ignore[attr-defined]
        except Exception:
            pass
        setattr(root, "_docurandom_scaling_configured", True)
    root.geometry(f"{metrics.width}x{metrics.height}+{metrics.x}+{metrics.y}")  # type: ignore[attr-defined]
    root.minsize(metrics.minimum_width, metrics.minimum_height)  # type: ignore[attr-defined]
    setattr(root, "_docurandom_ui_scale", metrics.font_scale)
    return metrics


def proportional_pane_position(
    extent: int,
    ratio: float,
    *,
    minimum_primary: int = 0,
    minimum_secondary: int = 0,
) -> int:
    """Return a sash position that preserves a ratio without crushing a pane."""

    available = max(1, int(extent))
    desired = round(available * max(0.0, min(1.0, float(ratio))))
    lower = max(0, min(int(minimum_primary), available))
    upper = max(lower, available - max(0, int(minimum_secondary)))
    return max(lower, min(upper, desired))
