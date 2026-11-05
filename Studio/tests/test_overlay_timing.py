import pytest

from docurandom.overlay_timing import (
    resolve_overlay_window,
    timing_summary,
    validate_timing_values,
)


def test_overlay_windows_cover_full_start_end_custom_and_legacy_once() -> None:
    full = resolve_overlay_window(
        "full",
        start_seconds=7,
        end_seconds=9,
        duration_seconds=3,
        total_duration=20,
        allow_once=True,
    )
    start = resolve_overlay_window(
        "start",
        start_seconds=7,
        end_seconds=9,
        duration_seconds=3,
        total_duration=20,
        allow_once=True,
    )
    end = resolve_overlay_window(
        "end",
        start_seconds=7,
        end_seconds=9,
        duration_seconds=3,
        total_duration=20,
        allow_once=False,
    )
    custom = resolve_overlay_window(
        "custom",
        start_seconds=4,
        end_seconds=11,
        duration_seconds=3,
        total_duration=20,
        allow_once=True,
    )
    once = resolve_overlay_window(
        "once",
        start_seconds=2,
        end_seconds=11,
        duration_seconds=3,
        total_duration=20,
        allow_once=True,
    )

    assert (full.start_seconds, full.end_seconds, full.loop_media) == (0, None, True)
    assert (start.start_seconds, start.end_seconds, start.loop_media) == (0, 3, True)
    assert (end.start_seconds, end.end_seconds, end.loop_media) == (17, 20, True)
    assert (custom.start_seconds, custom.end_seconds, custom.loop_media) == (4, 11, True)
    assert (once.start_seconds, once.end_seconds, once.loop_media) == (2, None, False)


def test_custom_timing_rejects_an_end_before_start() -> None:
    with pytest.raises(ValueError, match="kết thúc"):
        validate_timing_values(
            "custom",
            start_seconds=8,
            end_seconds=4,
            duration_seconds=3,
            allow_once=True,
        )


def test_timing_summary_is_human_readable() -> None:
    assert "toàn bộ" in timing_summary(
        "full",
        start_seconds=0,
        end_seconds=0,
        duration_seconds=5,
        allow_once=False,
    ).lower()
    assert "6 giây cuối" in timing_summary(
        "end",
        start_seconds=0,
        end_seconds=0,
        duration_seconds=6,
        allow_once=False,
    ).lower()
