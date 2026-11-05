from datetime import datetime

import pytest

from docurandom.workspace_scheduler import (
    DEFAULT_DAILY_CHAIN_VERSION,
    ScheduleShift,
    VIETNAM_TIMEZONE,
    default_daily_chain_needs_upgrade,
    default_daily_schedule_shifts,
    evaluate_schedule,
    format_vietnam_datetime,
    load_schedule_shifts,
    next_daily_occurrence,
    parse_remote_schedule_spec,
    parse_vietnam_datetime,
    prepare_schedule_shift_for_start,
    schedule_occurrence_id,
    schedule_value_display,
    schedule_shift_from_mapping,
    schedule_shift_to_dict,
    validate_schedule_shifts,
)


def test_vietnam_schedule_accepts_friendly_and_iso_inputs() -> None:
    friendly = parse_vietnam_datetime("28/08/2026 19:30")
    iso = parse_vietnam_datetime("2026-08-28 19:30")

    assert friendly == iso
    assert friendly.utcoffset().total_seconds() == 7 * 3600
    assert format_vietnam_datetime(friendly) == "28/08/2026 19:30"


def test_daily_schedule_display_hides_date_but_once_keeps_it() -> None:
    value = "28/08/2026 19:30"

    assert schedule_value_display(value, daily=True) == "19:30"
    assert schedule_value_display(value, daily=False) == value


def test_schedule_is_due_only_once_for_the_same_occurrence() -> None:
    now = datetime(2026, 8, 28, 20, 0, tzinfo=VIETNAM_TIMEZONE)
    first = evaluate_schedule(
        kind="workflow",
        enabled=True,
        value="28/08/2026 19:30",
        now=now,
    )

    assert first.due
    assert first.occurrence_id == schedule_occurrence_id(
        "workflow", parse_vietnam_datetime("28/08/2026 19:30")
    )

    claimed = evaluate_schedule(
        kind="workflow",
        enabled=True,
        value="28/08/2026 19:30",
        claimed_for=first.occurrence_id,
        now=now,
    )
    assert claimed.state == "claimed"
    assert not claimed.due


def test_invalid_schedule_is_reported_without_becoming_due() -> None:
    result = evaluate_schedule(
        kind="youtube-upload",
        enabled=True,
        value="19 giờ tối mai",
    )

    assert result.state == "invalid"
    assert "28/08/2026 19:30" in result.detail


def test_daily_schedule_skips_missed_days_and_returns_one_future_occurrence() -> None:
    scheduled = parse_vietnam_datetime("25/08/2026 19:30")
    now = datetime(2026, 8, 28, 20, 0, tzinfo=VIETNAM_TIMEZONE)

    next_at = next_daily_occurrence(scheduled, now=now)

    assert format_vietnam_datetime(next_at) == "29/08/2026 19:30"


def test_remote_schedule_accepts_time_only_and_vietnamese_daily_mode() -> None:
    now = datetime(2026, 8, 28, 20, 0, tzinfo=VIETNAM_TIMEZONE)

    result = parse_remote_schedule_spec("08:30 hằng ngày", now=now)

    assert result.daily
    assert format_vietnam_datetime(result.scheduled_at) == "29/08/2026 08:30"


def test_remote_schedule_rejects_past_one_time_but_advances_daily() -> None:
    now = datetime(2026, 8, 28, 20, 0, tzinfo=VIETNAM_TIMEZONE)

    with pytest.raises(ValueError, match="đã qua"):
        parse_remote_schedule_spec("28/08/2026 19:30 once", now=now)

    daily = parse_remote_schedule_spec("25/08/2026 19:30 daily", now=now)
    assert format_vietnam_datetime(daily.scheduled_at) == "29/08/2026 19:30"


def test_multiple_schedule_shifts_round_trip_and_migrate_legacy_pair() -> None:
    legacy = load_schedule_shifts(
        {
            "schedule_workflow_enabled": True,
            "schedule_workflow_at": "29/08/2026 01:00",
            "schedule_workflow_daily": True,
            "schedule_upload_enabled": True,
            "schedule_upload_at": "29/08/2026 05:00",
            "schedule_upload_daily": True,
        },
        fallback_workflow_at="29/08/2026 02:00",
        fallback_upload_at="29/08/2026 06:00",
        now=datetime(2026, 9, 10, 12, 0, tzinfo=VIETNAM_TIMEZONE),
    )

    assert len(legacy) == 3
    assert [(shift.workflow_at, shift.upload_at) for shift in legacy] == [
        ("29/08/2026 04:30", "29/08/2026 08:30"),
        ("11/09/2026 08:30", "11/09/2026 13:30"),
        ("10/09/2026 13:30", "11/09/2026 04:30"),
    ]
    restored = schedule_shift_from_mapping(schedule_shift_to_dict(legacy[0]), index=1)
    assert restored == legacy[0]


def test_default_daily_chain_uploads_then_starts_next_video_after_confirmation() -> None:
    now = datetime(2026, 8, 29, 12, 0, tzinfo=VIETNAM_TIMEZONE)

    shifts = default_daily_schedule_shifts(now=now)

    assert [shift.shift_id for shift in shifts] == [
        "daily-05-10",
        "daily-10-15",
        "daily-15-05",
    ]
    assert [(shift.workflow_at, shift.upload_at) for shift in shifts] == [
        ("30/08/2026 04:30", "30/08/2026 08:30"),
        ("30/08/2026 08:30", "30/08/2026 13:30"),
        ("29/08/2026 13:30", "30/08/2026 04:30"),
    ]
    assert all(shift.workflow_daily and shift.upload_daily for shift in shifts)
    validate_schedule_shifts(shifts)


def test_starting_daily_chain_after_overnight_workflow_keeps_upload_next_morning() -> None:
    now = datetime(2026, 8, 29, 20, 0, tzinfo=VIETNAM_TIMEZONE)
    shift = ScheduleShift(
        "daily-15-05",
        "29/08/2026 13:00",
        "30/08/2026 04:30",
    )

    prepared = prepare_schedule_shift_for_start(shift, now=now)

    assert prepared.workflow_at == "30/08/2026 13:00"
    assert prepared.upload_at == "31/08/2026 04:30"
    validate_schedule_shifts([prepared])


def test_starting_daily_chain_accepts_same_date_overnight_clock_pair() -> None:
    now = datetime(2026, 8, 29, 12, 0, tzinfo=VIETNAM_TIMEZONE)
    shift = ScheduleShift(
        "daily-15-05",
        "29/08/2026 13:00",
        "29/08/2026 04:30",
    )

    prepared = prepare_schedule_shift_for_start(shift, now=now)

    assert prepared.workflow_at == "29/08/2026 13:00"
    assert prepared.upload_at == "30/08/2026 04:30"
    validate_schedule_shifts([prepared])


def test_untouched_disabled_schedule_migrates_to_armed_chain_once() -> None:
    now = datetime(2026, 8, 29, 12, 0, tzinfo=VIETNAM_TIMEZONE)
    config = {
        "schedule_shifts": [
            {
                "shift_id": "shift-1",
                "workflow_at": "29/08/2026 13:00",
                "upload_at": "29/08/2026 18:00",
                "workflow_enabled": False,
                "upload_enabled": False,
                "workflow_daily": False,
                "upload_daily": False,
            }
        ]
    }

    migrated = load_schedule_shifts(
        config,
        fallback_workflow_at="29/08/2026 13:00",
        fallback_upload_at="29/08/2026 18:00",
        now=now,
    )
    preserved = load_schedule_shifts(
        {**config, "schedule_default_chain_version": DEFAULT_DAILY_CHAIN_VERSION},
        fallback_workflow_at="29/08/2026 13:00",
        fallback_upload_at="29/08/2026 18:00",
        now=now,
    )

    assert len(migrated) == 3
    assert len(preserved) == 1
    assert not preserved[0].workflow_enabled


def test_v1_default_chain_migrates_in_place_and_preserves_claims() -> None:
    config = {
        "schedule_default_chain_version": 1,
        "schedule_shifts": [
            {
                "shift_id": "daily-05-10",
                "workflow_at": "30/08/2026 05:00",
                "upload_at": "29/08/2026 10:00",
                "workflow_daily": True,
                "upload_daily": True,
                "workflow_claimed_for": "workflow:2026-08-29T05:00+07:00",
            },
            {
                "shift_id": "daily-10-15",
                "workflow_at": "29/08/2026 10:00",
                "upload_at": "29/08/2026 15:00",
                "workflow_daily": True,
                "upload_daily": True,
            },
            {
                "shift_id": "daily-15-05",
                "workflow_at": "29/08/2026 15:00",
                "upload_at": "30/08/2026 05:00",
                "workflow_daily": True,
                "upload_daily": True,
            },
        ],
    }

    assert default_daily_chain_needs_upgrade(config)
    upgraded = load_schedule_shifts(
        config,
        fallback_workflow_at="29/08/2026 13:00",
        fallback_upload_at="29/08/2026 18:00",
    )

    assert [(shift.workflow_at, shift.upload_at) for shift in upgraded] == [
        ("30/08/2026 04:30", "29/08/2026 08:30"),
        ("29/08/2026 08:30", "29/08/2026 13:30"),
        ("29/08/2026 13:30", "30/08/2026 04:30"),
    ]
    assert upgraded[0].workflow_claimed_for.endswith("05:00+07:00")


def test_v2_default_chain_migrates_upload_clocks_and_preserves_live_state() -> None:
    config = {
        "schedule_default_chain_version": 2,
        "schedule_shifts": [
            {
                "shift_id": "daily-05-10",
                "workflow_at": "30/08/2026 05:30",
                "upload_at": "30/08/2026 10:00",
                "workflow_daily": True,
                "upload_daily": True,
                "upload_claimed_for": "youtube-upload:old",
            },
            {
                "shift_id": "daily-10-15",
                "workflow_at": "30/08/2026 10:30",
                "upload_at": "30/08/2026 15:00",
                "workflow_daily": True,
                "upload_daily": True,
            },
            {
                "shift_id": "daily-15-05",
                "workflow_at": "30/08/2026 15:30",
                "upload_at": "31/08/2026 05:00",
                "workflow_daily": True,
                "upload_daily": True,
            },
        ],
    }

    upgraded = load_schedule_shifts(
        config,
        fallback_workflow_at="30/08/2026 04:30",
        fallback_upload_at="30/08/2026 10:00",
    )

    assert [(shift.workflow_at, shift.upload_at) for shift in upgraded] == [
        ("30/08/2026 04:30", "30/08/2026 08:30"),
        ("30/08/2026 08:30", "30/08/2026 13:30"),
        ("30/08/2026 13:30", "31/08/2026 04:30"),
    ]
    assert upgraded[0].upload_claimed_for == "youtube-upload:old"


def test_custom_schedule_is_replaced_by_managed_chain_upgrade() -> None:
    config = {
        "schedule_default_chain_version": 1,
        "schedule_shifts": [
            {
                "shift_id": "daily-05-10",
                "workflow_at": "29/08/2026 06:15",
                "upload_at": "29/08/2026 10:00",
            }
        ],
    }

    assert default_daily_chain_needs_upgrade(config)
    shifts = load_schedule_shifts(
        config,
        fallback_workflow_at="29/08/2026 13:00",
        fallback_upload_at="29/08/2026 18:00",
        now=datetime(2026, 9, 10, 12, 0, tzinfo=VIETNAM_TIMEZONE),
    )
    assert [(shift.workflow_at, shift.upload_at) for shift in shifts] == [
        ("29/08/2026 04:30", "29/08/2026 08:30"),
        ("11/09/2026 08:30", "11/09/2026 13:30"),
        ("10/09/2026 13:30", "11/09/2026 04:30"),
    ]


def test_v3_managed_chain_is_migrated_to_new_upload_clocks() -> None:
    config = {
        "schedule_default_chain_version": 3,
        "schedule_shifts": [
            {
                "shift_id": "daily-05-10",
                "workflow_at": "11/09/2026 04:30",
                "upload_at": "11/09/2026 10:00",
            },
            {
                "shift_id": "daily-10-15",
                "workflow_at": "11/09/2026 10:00",
                "upload_at": "11/09/2026 13:00",
            },
            {
                "shift_id": "daily-15-05",
                "workflow_at": "10/09/2026 13:00",
                "upload_at": "11/09/2026 04:30",
            },
        ],
    }

    assert default_daily_chain_needs_upgrade(config)
    upgraded = load_schedule_shifts(
        config,
        fallback_workflow_at="11/09/2026 04:30",
        fallback_upload_at="11/09/2026 10:00",
        now=datetime(2026, 9, 10, 12, 0, tzinfo=VIETNAM_TIMEZONE),
    )

    assert [(shift.workflow_at, shift.upload_at) for shift in upgraded] == [
        ("11/09/2026 04:30", "11/09/2026 08:30"),
        ("11/09/2026 08:30", "11/09/2026 13:30"),
        ("10/09/2026 13:30", "11/09/2026 04:30"),
    ]


def test_active_custom_shift_beyond_three_slots_keeps_its_id() -> None:
    config = {
        "schedule_default_chain_version": 3,
        "schedule_active_shift_id": "custom-4",
        "schedule_shifts": [
            {"shift_id": "custom-1", "workflow_at": "11/09/2026 01:00", "upload_at": "11/09/2026 03:00"},
            {"shift_id": "custom-2", "workflow_at": "11/09/2026 04:00", "upload_at": "11/09/2026 07:00"},
            {"shift_id": "custom-3", "workflow_at": "11/09/2026 08:00", "upload_at": "11/09/2026 11:00"},
            {
                "shift_id": "custom-4",
                "workflow_at": "11/09/2026 12:00",
                "upload_at": "11/09/2026 15:00",
                "upload_claimed_for": "youtube-upload:active",
            },
        ],
    }

    upgraded = load_schedule_shifts(
        config,
        fallback_workflow_at="11/09/2026 04:30",
        fallback_upload_at="11/09/2026 08:30",
    )

    assert [shift.shift_id for shift in upgraded] == [
        "custom-1",
        "custom-2",
        "custom-4",
    ]
    assert upgraded[-1].upload_claimed_for == "youtube-upload:active"


def test_multiple_schedule_shifts_reject_overlapping_workspace_sessions() -> None:
    shifts = (
        ScheduleShift("shift-1", "29/08/2026 01:00", "29/08/2026 05:00"),
        ScheduleShift("shift-2", "29/08/2026 04:00", "29/08/2026 10:00"),
    )

    with pytest.raises(ValueError, match="chưa tới giờ đăng"):
        validate_schedule_shifts(shifts)

    validate_schedule_shifts(
        (
            shifts[0],
            ScheduleShift("shift-2", "29/08/2026 06:00", "29/08/2026 10:00"),
            ScheduleShift("shift-3", "29/08/2026 11:00", "29/08/2026 15:00"),
        )
    )
