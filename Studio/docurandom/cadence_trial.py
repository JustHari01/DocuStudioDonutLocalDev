"""Operator-approved temporary cadence; never overwrites a later manual edit."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path

from .storage import atomic_write_json
from .workspace_scheduler import (
    VIETNAM_TIMEZONE, format_vietnam_datetime, parse_vietnam_datetime,
)


def signature(shifts: list[dict]) -> list[dict]:
    return [{**{k: s.get(k) for k in (
        "shift_id", "workflow_enabled", "upload_enabled", "workflow_daily", "upload_daily")},
        **{k: parse_vietnam_datetime(s[k]).strftime("%H:%M")
           for k in ("workflow_at", "upload_at")}} for s in shifts]


def start_trial(state_dir: Path, config: dict, *, now: datetime, days=28, clock="13:30") -> dict:
    path = state_dir / "cadence-trial.json"
    if path.exists():
        raise ValueError("A trial record already exists; review it before replacing")
    current = now.astimezone(VIETNAM_TIMEZONE)
    hour, minute = map(int, clock.split(":"))
    first = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if first <= current:
        first += timedelta(days=1)
    if days < 1:
        raise ValueError("Trial needs at least one day")
    original = config["schedule_shifts"]
    active = str(config.get("schedule_active_shift_id", ""))
    source = next((s for s in original if s["shift_id"] == active), original[0])
    # Preserve the active production and its receipt. Its next Public is the
    # first trial slot; subsequent productions follow the existing A->B->C lane.
    shift = {**source, "workflow_at": format_vietnam_datetime(first - timedelta(days=1)),
             "upload_at": format_vietnam_datetime(first), "workflow_daily": True,
             "upload_daily": True, "workflow_enabled": True, "upload_enabled": True}
    last = first + timedelta(days=days - 1)
    record = {"schema_version": 1, "status": "active", "timezone": "Asia/Saigon",
              "created_at": current.isoformat(), "first_public": first.isoformat(),
              "last_public": last.isoformat(), "days": days, "clock": clock,
              "ends_at": (last + timedelta(days=1)).replace(hour=0, minute=0).isoformat(),
              "original_shifts": original, "trial_signature": signature([shift]),
              "authorization": "DaiTa14: 1 video/ngay luc 13:30 trong 28 ngay",
              "baseline_directory": "analytics", "restore_policy": "restore unless manually edited"}
    # Record before mutating settings, so interruption cannot lose the original.
    atomic_write_json(path, record)
    result = {**config, "schedule_shifts": [shift]}
    for key, value in shift.items():
        if key != "shift_id": result["schedule_" + key] = value
    return result


def reconcile_trial(state_dir: Path, config: dict, *, now: datetime) -> dict:
    path = state_dir / "cadence-trial.json"
    if not path.is_file():
        return config
    record = json.loads(path.read_text(encoding="utf-8"))
    if record["status"] != "active":
        return config
    if signature(config["schedule_shifts"]) != record["trial_signature"]:
        record.update(status="operator_override", resolved_at=now.isoformat())
        atomic_write_json(path, record)
        return config
    current = now.astimezone(VIETNAM_TIMEZONE)
    if current < datetime.fromisoformat(record["ends_at"]):
        return config
    restored = []
    for source in record["original_shifts"]:
        shift = dict(source)
        for kind in ("workflow", "upload"):
            old = parse_vietnam_datetime(source[kind + "_at"])
            future = current.replace(hour=old.hour, minute=old.minute, second=0, microsecond=0)
            if future <= current: future += timedelta(days=1)
            shift[kind + "_at"] = format_vietnam_datetime(future)
        restored.append(shift)
    result = {**config, "schedule_shifts": restored}
    for key, value in restored[0].items():
        if key != "shift_id": result["schedule_" + key] = value
    # Settings commit precedes completion marker: a crash can safely repeat it.
    atomic_write_json(state_dir / "settings.json", result)
    record.update(status="completed", resolved_at=current.isoformat(), report_pending=True)
    atomic_write_json(path, record)
    return result
