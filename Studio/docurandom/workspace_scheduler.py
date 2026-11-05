from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEDULE_DISPLAY_FORMAT = "%d/%m/%Y %H:%M"
_SCHEDULE_INPUT_FORMATS = (
    SCHEDULE_DISPLAY_FORMAT,
    "%Y-%m-%d %H:%M",
)

try:
    VIETNAM_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
except ZoneInfoNotFoundError:  # pragma: no cover - Windows runtime normally bundles tzdata.
    VIETNAM_TIMEZONE = timezone(timedelta(hours=7), name="Asia/Ho_Chi_Minh")


class ScheduleValueError(ValueError):
    pass


@dataclass(frozen=True)
class ScheduleEvaluation:
    state: str
    scheduled_at: datetime | None = None
    occurrence_id: str = ""
    detail: str = ""

    @property
    def due(self) -> bool:
        return self.state == "due"


@dataclass(frozen=True)
class RemoteScheduleSpec:
    scheduled_at: datetime
    daily: bool = False


MAX_SCHEDULE_SHIFTS = 12
PUBLICATION_WINDOW = timedelta(hours=2)
# Version 4 changes the managed upload clocks to 04:30, 08:30 and 13:30.
# Bumping this value is intentional: every workspace saved by an older build
# is migrated once on its next start, including workspaces that had customized
# the previous schedule.
DEFAULT_DAILY_CHAIN_VERSION = 4
DEFAULT_DAILY_CHAIN_SLOTS = (
    (4, 30, 8, 30),
    (8, 30, 13, 30),
    (13, 30, 4, 30),
)
_DEFAULT_DAILY_CHAIN_IDS = (
    "daily-05-10",
    "daily-10-15",
    "daily-15-05",
)


@dataclass(frozen=True)
class ScheduleShift:
    """One production/upload pair owned by a workspace.

    Full next-occurrence datetimes are stored instead of clock-only values so a
    production run can safely cross midnight before its upload slot.  Daily
    shifts advance each half independently only after that half has been
    claimed.
    """

    shift_id: str
    workflow_at: str
    upload_at: str
    workflow_enabled: bool = True
    upload_enabled: bool = True
    workflow_daily: bool = True
    upload_daily: bool = True
    workflow_claimed_for: str = ""
    upload_claimed_for: str = ""


def schedule_shift_to_dict(shift: ScheduleShift) -> dict[str, object]:
    return asdict(shift)


def default_daily_schedule_shifts(
    *,
    now: datetime | None = None,
) -> tuple[ScheduleShift, ...]:
    """Create the 04:30→08:30→13:30→04:30 daily chain.

    Every workflow clock is also the preceding shift's upload clock. The
    scheduler processes uploads before workflows, so production starts as soon
    as the preceding upload is fully verified instead of competing with it.
    """

    current = (now or vietnam_now()).astimezone(VIETNAM_TIMEZONE)
    shifts: list[ScheduleShift] = []
    for index, (
        workflow_hour,
        workflow_minute,
        upload_hour,
        upload_minute,
    ) in enumerate(DEFAULT_DAILY_CHAIN_SLOTS):
        workflow_at = current.replace(
            hour=workflow_hour,
            minute=workflow_minute,
            second=0,
            microsecond=0,
        )
        if workflow_at <= current:
            workflow_at += timedelta(days=1)
        upload_at = current.replace(
            hour=upload_hour,
            minute=upload_minute,
            second=0,
            microsecond=0,
        )
        # Pair each workflow with the first upload slot after that workflow.
        # Advancing both clocks independently can otherwise produce a saved
        # pair where the upload is due before its workflow (for example,
        # workflow tomorrow at 08:30 but upload today at 13:30).
        while upload_at <= workflow_at:
            upload_at += timedelta(days=1)
        shifts.append(
            ScheduleShift(
                # Keep the stable IDs so an in-progress video remains bound to
                # its upload slot while the clock is upgraded.
                shift_id=_DEFAULT_DAILY_CHAIN_IDS[index],
                workflow_at=format_vietnam_datetime(workflow_at),
                upload_at=format_vietnam_datetime(upload_at),
            )
        )
    return tuple(shifts)


def _matches_default_chain_v1(shifts: list[ScheduleShift]) -> bool:
    if len(shifts) != len(_DEFAULT_DAILY_CHAIN_IDS):
        return False
    expected_clocks = ((5, 0, 10, 0), (10, 0, 15, 0), (15, 0, 5, 0))
    for shift, shift_id, expected in zip(
        shifts,
        _DEFAULT_DAILY_CHAIN_IDS,
        expected_clocks,
    ):
        if shift.shift_id != shift_id or not shift.workflow_daily or not shift.upload_daily:
            return False
        try:
            workflow_at = parse_vietnam_datetime(shift.workflow_at)
            upload_at = parse_vietnam_datetime(shift.upload_at)
        except ScheduleValueError:
            return False
        if (
            workflow_at.hour,
            workflow_at.minute,
            upload_at.hour,
            upload_at.minute,
        ) != expected:
            return False
    return True


def _matches_default_chain_v2(shifts: list[ScheduleShift]) -> bool:
    if len(shifts) != len(_DEFAULT_DAILY_CHAIN_IDS):
        return False
    expected_clocks = ((5, 30, 10, 0), (10, 30, 15, 0), (15, 30, 5, 0))
    for shift, shift_id, expected in zip(
        shifts,
        _DEFAULT_DAILY_CHAIN_IDS,
        expected_clocks,
    ):
        if shift.shift_id != shift_id or not shift.workflow_daily or not shift.upload_daily:
            return False
        try:
            workflow_at = parse_vietnam_datetime(shift.workflow_at)
            upload_at = parse_vietnam_datetime(shift.upload_at)
        except ScheduleValueError:
            return False
        if (
            workflow_at.hour,
            workflow_at.minute,
            upload_at.hour,
            upload_at.minute,
        ) != expected:
            return False
    return True


def _is_untouched_disabled_shift(shifts: list[ScheduleShift]) -> bool:
    return bool(
        len(shifts) == 1
        and not shifts[0].workflow_enabled
        and not shifts[0].upload_enabled
        and not shifts[0].workflow_claimed_for
        and not shifts[0].upload_claimed_for
    )


def _load_shift_list(config: dict[str, object]) -> list[ScheduleShift]:
    raw = config.get("schedule_shifts")
    loaded: list[ScheduleShift] = []
    if not isinstance(raw, list):
        return loaded
    seen: set[str] = set()
    for index, value in enumerate(raw[:MAX_SCHEDULE_SHIFTS], 1):
        shift = schedule_shift_from_mapping(value, index=index)
        if shift is None:
            continue
        if shift.shift_id in seen:
            shift = ScheduleShift(**{**asdict(shift), "shift_id": f"shift-{index}"})
        seen.add(shift.shift_id)
        loaded.append(shift)
    return loaded


def default_daily_chain_needs_upgrade(config: dict[str, object]) -> bool:
    """Return whether this workspace should adopt the current managed chain.

    The managed schedule is deliberately authoritative across releases.  A
    workspace may have edited the old clocks in the UI, but once a new managed
    chain is shipped those old clocks must be replaced as well; otherwise two
    workspaces on the same installation can silently keep different upload
    windows.
    """

    try:
        stored_version = int(config.get("schedule_default_chain_version", 0) or 0)
    except (TypeError, ValueError):
        stored_version = 0
    if stored_version >= DEFAULT_DAILY_CHAIN_VERSION:
        return False
    return stored_version < DEFAULT_DAILY_CHAIN_VERSION


def _upgrade_default_chain(
    shifts: list[ScheduleShift],
    *,
    now: datetime | None = None,
    active_shift_id: str = "",
) -> tuple[ScheduleShift, ...]:
    """Replace an older/custom chain with the managed clocks.

    Shift IDs, enabled flags and claim markers are retained whenever possible
    so a live checkpoint remains attached to the same workspace session.  If a
    custom schedule had more than three shifts, its active shift is retained in
    the final managed slot instead of being orphaned during migration.
    """

    defaults = list(default_daily_schedule_shifts(now=now))
    if not shifts:
        return tuple(defaults)

    source_indices = list(range(min(len(shifts), len(defaults))))
    active_index = next(
        (
            index
            for index, shift in enumerate(shifts)
            if str(shift.shift_id) == str(active_shift_id or "")
        ),
        None,
    )
    if active_index is not None and active_index >= len(defaults):
        # Keep the active checkpoint alive even when an old custom schedule had
        # a fourth (or later) shift.  The dropped non-active slot is harmless;
        # the active shift is the one that must never disappear.
        source_indices[-1] = active_index

    upgraded: list[ScheduleShift] = []
    for index, default in enumerate(defaults):
        source = shifts[source_indices[index]] if index < len(source_indices) else None
        if source is None:
            upgraded.append(default)
            continue
        workflow_hour, workflow_minute, upload_hour, upload_minute = (
            DEFAULT_DAILY_CHAIN_SLOTS[index]
        )
        try:
            workflow_at = parse_vietnam_datetime(source.workflow_at).replace(
                hour=workflow_hour,
                minute=workflow_minute,
                second=0,
                microsecond=0,
            )
        except ScheduleValueError:
            workflow_at = parse_vietnam_datetime(default.workflow_at)
        try:
            upload_at = parse_vietnam_datetime(source.upload_at).replace(
                hour=upload_hour,
                minute=upload_minute,
                second=0,
                microsecond=0,
            )
        except ScheduleValueError:
            upload_at = parse_vietnam_datetime(default.upload_at)
        upgraded.append(
            ScheduleShift(
                **{
                    **asdict(source),
                    "workflow_at": format_vietnam_datetime(workflow_at),
                    "upload_at": format_vietnam_datetime(upload_at),
                }
            )
        )
    return tuple(upgraded)


def schedule_shift_from_mapping(
    value: object,
    *,
    index: int,
) -> ScheduleShift | None:
    if not isinstance(value, dict):
        return None
    shift_id = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value.get("shift_id") or "")).strip("-")
    if not shift_id:
        shift_id = f"shift-{index}"
    workflow_at = str(value.get("workflow_at") or "").strip()
    upload_at = str(value.get("upload_at") or "").strip()
    if not workflow_at or not upload_at:
        return None
    return ScheduleShift(
        shift_id=shift_id[:64],
        workflow_at=workflow_at,
        upload_at=upload_at,
        workflow_enabled=bool(value.get("workflow_enabled", True)),
        upload_enabled=bool(value.get("upload_enabled", True)),
        workflow_daily=bool(value.get("workflow_daily", value.get("daily", True))),
        upload_daily=bool(value.get("upload_daily", value.get("daily", True))),
        workflow_claimed_for=str(value.get("workflow_claimed_for") or ""),
        upload_claimed_for=str(value.get("upload_claimed_for") or ""),
    )


def load_schedule_shifts(
    config: dict[str, object],
    *,
    fallback_workflow_at: str,
    fallback_upload_at: str,
    now: datetime | None = None,
) -> tuple[ScheduleShift, ...]:
    """Load schedules and migrate every pre-v4 workspace to the managed chain."""

    loaded = _load_shift_list(config)
    needs_default_upgrade = default_daily_chain_needs_upgrade(config)
    if needs_default_upgrade and loaded:
        return _upgrade_default_chain(
            loaded,
            now=now,
            active_shift_id=str(config.get("schedule_active_shift_id") or ""),
        )
    if loaded and not needs_default_upgrade:
        return tuple(loaded)
    legacy_enabled = bool(
        config.get("schedule_workflow_enabled", False)
        or config.get("schedule_upload_enabled", False)
    )
    if needs_default_upgrade and not legacy_enabled:
        return default_daily_schedule_shifts(now=now)
    legacy_shift = ScheduleShift(
        shift_id="shift-1",
        workflow_at=str(config.get("schedule_workflow_at") or fallback_workflow_at),
        upload_at=str(config.get("schedule_upload_at") or fallback_upload_at),
        workflow_enabled=bool(config.get("schedule_workflow_enabled", False)),
        upload_enabled=bool(config.get("schedule_upload_enabled", False)),
        workflow_daily=bool(config.get("schedule_workflow_daily", False)),
        upload_daily=bool(config.get("schedule_upload_daily", False)),
        workflow_claimed_for=str(config.get("schedule_workflow_claimed_for") or ""),
        upload_claimed_for=str(config.get("schedule_upload_claimed_for") or ""),
    )
    return _upgrade_default_chain(
        [legacy_shift],
        now=now,
        active_shift_id=str(config.get("schedule_active_shift_id") or ""),
    )


def validate_schedule_shift(shift: ScheduleShift) -> tuple[datetime, datetime]:
    if not shift.workflow_enabled and not shift.upload_enabled:
        raise ScheduleValueError("Ca phải bật ít nhất workflow hoặc upload YouTube.")
    workflow_at = parse_vietnam_datetime(shift.workflow_at)
    upload_at = parse_vietnam_datetime(shift.upload_at)
    if shift.workflow_enabled and shift.upload_enabled and upload_at <= workflow_at:
        if not (shift.workflow_daily and shift.upload_daily):
            raise ScheduleValueError(
                "Giờ upload của một ca phải sau giờ bắt đầu workflow để video có thời gian render xong."
            )
        # Recurring workflow and upload clocks advance independently. For an
        # overnight shift, the next upload occurrence can be earlier than the
        # next workflow occurrence while still being the correct upcoming
        # publication slot (for example, 04:30 before 13:30). Use the upload
        # after this workflow only for interval validation; never rewrite the
        # separately stored next occurrence.
        while upload_at <= workflow_at:
            upload_at += timedelta(days=1)
    return workflow_at, upload_at


def validate_schedule_shifts(shifts: tuple[ScheduleShift, ...] | list[ScheduleShift]) -> None:
    if not shifts:
        raise ScheduleValueError("Chưa có ca sản xuất/upload nào.")
    if len(shifts) > MAX_SCHEDULE_SHIFTS:
        raise ScheduleValueError(f"Mỗi workspace dùng tối đa {MAX_SCHEDULE_SHIFTS} ca.")
    intervals: list[tuple[datetime, datetime, str]] = []
    for index, shift in enumerate(shifts, 1):
        workflow_at, upload_at = validate_schedule_shift(shift)
        if shift.workflow_enabled and shift.upload_enabled:
            intervals.append((workflow_at, upload_at, f"Ca {index}"))
    intervals.sort(key=lambda item: item[0])
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] < previous[1]:
            raise ScheduleValueError(
                f"{current[2]} bắt đầu khi {previous[2]} chưa tới giờ đăng. "
                "Một workspace chỉ giữ một phiên video; hãy tách giờ để ca trước upload và lưu kho xong."
            )


def vietnam_now() -> datetime:
    return datetime.now(VIETNAM_TIMEZONE)


def format_vietnam_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        localized = value.replace(tzinfo=VIETNAM_TIMEZONE)
    else:
        localized = value.astimezone(VIETNAM_TIMEZONE)
    return localized.strftime(SCHEDULE_DISPLAY_FORMAT)


def schedule_value_display(value: str, *, daily: bool) -> str:
    """Show only the recurring clock; one-time schedules keep their date."""

    parsed = parse_vietnam_datetime(value)
    return parsed.strftime("%H:%M") if daily else format_vietnam_datetime(parsed)


def default_schedule_text(*, hours_from_now: float) -> str:
    value = vietnam_now() + timedelta(hours=max(0.0, hours_from_now))
    value = value.replace(second=0, microsecond=0)
    return format_vietnam_datetime(value)


def parse_vietnam_datetime(value: str) -> datetime:
    cleaned = str(value or "").strip()
    for pattern in _SCHEDULE_INPUT_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, pattern)
        except ValueError:
            continue
        return parsed.replace(tzinfo=VIETNAM_TIMEZONE)
    raise ScheduleValueError(
        "Thời gian chưa đúng. Hãy nhập theo dạng ngày/tháng/năm giờ:phút, "
        "ví dụ 28/08/2026 19:30."
    )


def _ascii_words(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip()).casefold()
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.replace("đ", "d")
    return re.sub(r"\s+", " ", normalized).strip()


def parse_remote_schedule_spec(
    value: str,
    *,
    now: datetime | None = None,
) -> RemoteScheduleSpec:
    """Parse a compact Telegram schedule in Vietnam time.

    Accepted examples are ``08:30 daily``, ``29/08/2026 08:30 once`` and
    ``2026-08-29 08:30``. A time without a date means its next occurrence.
    """

    raw = re.sub(r"\s+", " ", str(value or "").strip())
    normalized = _ascii_words(raw)
    daily = False
    mode_patterns = (
        (r"(?:\s+|^)(daily|hang ngay)$", True),
        (r"(?:\s+|^)(once|mot lan)$", False),
    )
    matched_mode = False
    for pattern, mode in mode_patterns:
        match = re.search(pattern, normalized)
        if match is None:
            continue
        daily = mode
        matched_mode = True
        # The ASCII-normalized and original values have the same number of
        # trailing words for all supported mode labels.
        word_count = len(match.group(1).split())
        raw = " ".join(raw.split()[:-word_count]).strip()
        break
    if not raw:
        raise ScheduleValueError(
            "Thiếu giờ. Ví dụ: 08:30 daily hoặc 29/08/2026 08:30 once."
        )
    current = now or vietnam_now()
    current = current.astimezone(VIETNAM_TIMEZONE)
    if re.fullmatch(r"\d{1,2}:\d{2}", raw):
        try:
            hour_text, minute_text = raw.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError as exc:
            raise ScheduleValueError("Giờ phải nằm trong khoảng 00:00–23:59.") from exc
        scheduled_at = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled_at <= current:
            scheduled_at += timedelta(days=1)
        return RemoteScheduleSpec(scheduled_at=scheduled_at, daily=daily)
    try:
        scheduled_at = parse_vietnam_datetime(raw)
    except ScheduleValueError as exc:
        suffix = " daily" if daily else " once" if matched_mode else ""
        raise ScheduleValueError(
            f"{exc} Trên Telegram cũng có thể dùng 08:30{suffix or ' daily'}."
        ) from exc
    if scheduled_at <= current:
        if daily:
            scheduled_at = next_daily_occurrence(scheduled_at, now=current)
        else:
            raise ScheduleValueError("Mốc một lần đã qua; hãy chọn giờ trong tương lai.")
    return RemoteScheduleSpec(scheduled_at=scheduled_at, daily=daily)


def schedule_occurrence_id(kind: str, scheduled_at: datetime) -> str:
    normalized_kind = str(kind or "schedule").strip().casefold() or "schedule"
    normalized_time = scheduled_at.astimezone(VIETNAM_TIMEZONE).isoformat(timespec="minutes")
    return f"{normalized_kind}:{normalized_time}"


def evaluate_schedule(
    *,
    kind: str,
    enabled: bool,
    value: str,
    claimed_for: str = "",
    now: datetime | None = None,
) -> ScheduleEvaluation:
    if not enabled:
        return ScheduleEvaluation("off")
    try:
        scheduled_at = parse_vietnam_datetime(value)
    except ScheduleValueError as exc:
        return ScheduleEvaluation("invalid", detail=str(exc))
    occurrence_id = schedule_occurrence_id(kind, scheduled_at)
    if occurrence_id == str(claimed_for or ""):
        return ScheduleEvaluation(
            "claimed",
            scheduled_at=scheduled_at,
            occurrence_id=occurrence_id,
            detail="Mốc giờ này đã được kích hoạt trước đó.",
        )
    current = now or vietnam_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=VIETNAM_TIMEZONE)
    else:
        current = current.astimezone(VIETNAM_TIMEZONE)
    return ScheduleEvaluation(
        "due" if current >= scheduled_at else "waiting",
        scheduled_at=scheduled_at,
        occurrence_id=occurrence_id,
    )


def countdown_text(scheduled_at: datetime, *, now: datetime | None = None) -> str:
    current = (now or vietnam_now()).astimezone(VIETNAM_TIMEZONE)
    remaining = int((scheduled_at - current).total_seconds())
    if remaining <= 0:
        return "đã đến giờ"
    days, remainder = divmod(remaining, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = max(1, (remainder + 59) // 60)
    if days:
        return f"còn {days} ngày {hours} giờ"
    if hours:
        return f"còn {hours} giờ {minutes} phút"
    return f"còn {minutes} phút"


def next_daily_occurrence(
    scheduled_at: datetime,
    *,
    now: datetime | None = None,
) -> datetime:
    current = (now or vietnam_now()).astimezone(VIETNAM_TIMEZONE)
    candidate = scheduled_at.astimezone(VIETNAM_TIMEZONE) + timedelta(days=1)
    while candidate <= current:
        candidate += timedelta(days=1)
    return candidate


def prepare_schedule_shift_for_start(
    shift: ScheduleShift,
    *,
    now: datetime | None = None,
) -> ScheduleShift:
    """Advance recurring clocks while keeping each upload after its workflow."""

    current = (now or vietnam_now()).astimezone(VIETNAM_TIMEZONE)
    workflow_at = parse_vietnam_datetime(shift.workflow_at)
    upload_at = parse_vietnam_datetime(shift.upload_at)
    if shift.workflow_enabled and workflow_at <= current:
        if not shift.workflow_daily:
            raise ScheduleValueError("Mốc workflow một lần đã qua.")
        workflow_at = next_daily_occurrence(workflow_at, now=current)
    if shift.upload_enabled and upload_at <= current:
        if not shift.upload_daily:
            raise ScheduleValueError("Mốc upload một lần đã qua.")
        upload_at = next_daily_occurrence(upload_at, now=current)
    if shift.workflow_enabled and shift.upload_enabled and upload_at <= workflow_at:
        if not shift.upload_daily:
            raise ScheduleValueError(
                "Mốc upload một lần phải nằm sau giờ bắt đầu workflow."
            )
        while upload_at <= workflow_at:
            upload_at += timedelta(days=1)

    return ScheduleShift(
        **{
            **asdict(shift),
            "workflow_at": format_vietnam_datetime(workflow_at),
            "upload_at": format_vietnam_datetime(upload_at),
            "workflow_claimed_for": "",
            "upload_claimed_for": "",
        }
    )
