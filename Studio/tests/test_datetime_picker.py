from datetime import datetime

from docurandom.datetime_picker import month_cells
from docurandom.workspace_scheduler import VIETNAM_TIMEZONE


def test_month_cells_are_monday_first_and_always_six_weeks() -> None:
    cells = month_cells(2026, 8)

    assert len(cells) == 42
    assert cells[0][0] == datetime(2026, 7, 27, tzinfo=VIETNAM_TIMEZONE)
    assert cells[0][0].weekday() == 0
    assert cells[-1][0] == datetime(2026, 9, 6, tzinfo=VIETNAM_TIMEZONE)


def test_month_cells_mark_only_days_in_visible_month() -> None:
    cells = month_cells(2026, 8)
    visible = [value.day for value, in_month in cells if in_month]

    assert visible == list(range(1, 32))
