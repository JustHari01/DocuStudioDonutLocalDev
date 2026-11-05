from docurandom.ui_scale import proportional_pane_position, responsive_window_metrics


def test_responsive_window_fits_small_screen():
    metrics = responsive_window_metrics(1366, 768)

    assert metrics.width <= 1366 - 32
    assert metrics.height <= 768 - 64
    assert metrics.minimum_width <= metrics.width
    assert metrics.minimum_height <= metrics.height
    assert 0.86 <= metrics.font_scale <= 1.16


def test_responsive_window_grows_but_remains_bounded_on_4k():
    metrics = responsive_window_metrics(3840, 2160)

    assert metrics.width == round(1360 * 1.20)
    assert metrics.height == round(940 * 1.20)
    assert metrics.font_scale == 1.16
    assert metrics.x > 0
    assert metrics.y > 0


def test_proportional_pane_position_scales_and_respects_both_minimums():
    assert proportional_pane_position(
        1300,
        5 / 13,
        minimum_primary=350,
        minimum_secondary=480,
    ) == 500
    assert proportional_pane_position(
        900,
        0.8,
        minimum_primary=350,
        minimum_secondary=480,
    ) == 420
    assert proportional_pane_position(
        900,
        0.1,
        minimum_primary=350,
        minimum_secondary=480,
    ) == 350
