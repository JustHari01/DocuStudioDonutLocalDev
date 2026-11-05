from __future__ import annotations

from docurandom.features import DONUT_YOUTUBE_ENABLED


def test_donut_youtube_integration_is_enabled_for_this_release() -> None:
    assert DONUT_YOUTUBE_ENABLED is True
