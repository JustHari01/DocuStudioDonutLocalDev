"""Finite, durable leases for reading one checkpointed ChatGPT response."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from .storage import atomic_write_json

DOM_RECOVERY_DELAYS = (2 * 60, 5 * 60, 15 * 60)


def has_reserved_dom_response_recovery(
    state_dir: Path, *, workspace_id: str, thread_id: str, pending_text: str,
) -> bool:
    """Keep submitted-only mode on later transport retries and after restart."""
    key = hashlib.sha256(json.dumps(
        [workspace_id, thread_id, pending_text], ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    try:
        saved = json.loads((state_dir / "chatgpt-dom-recovery.json").read_text(encoding="utf-8-sig"))
        return bool(isinstance(saved, dict) and saved.get("key") == key
                    and saved.get("mode") == "recover-submitted-only"
                    and type(saved.get("attempts")) is int and saved["attempts"] > 0)
    except (OSError, ValueError):
        return False


def reserve_dom_response_recovery(
    state_dir: Path,
    *,
    workspace_id: str,
    thread_id: str,
    pending_text: str,
    failed_at: str,
    now: datetime | None = None,
) -> str:
    """Return reserved/wait/exhausted/blocked; never send or clear a turn.

    The lease is written before the UI starts a read-only response recovery.
    Its key remains stable when the same failure is reported after a restart.
    """
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None or not workspace_id or not thread_id or not pending_text:
        return "blocked"
    clock = clock.astimezone(timezone.utc)
    key = hashlib.sha256(json.dumps(
        [workspace_id, thread_id, pending_text], ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    path = state_dir / "chatgpt-dom-recovery.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(saved, dict):
            return "blocked"
    except FileNotFoundError:
        saved = {}
    except (OSError, ValueError):
        return "blocked"
    try:
        if saved.get("key") == key:
            count = saved["attempts"]
            if type(count) is not int or count < 0:
                return "blocked"
            if count >= len(DOM_RECOVERY_DELAYS):
                return "exhausted"
            due = datetime.fromisoformat(saved["next_probe_at"])
        else:
            count = 0
            due = datetime.fromisoformat(failed_at.replace("Z", "+00:00"))
            due += timedelta(seconds=DOM_RECOVERY_DELAYS[0])
        if due.tzinfo is None:
            return "blocked"
        if clock < due:
            return "wait"
        atomic_write_json(path, {
            "version": 2, "workspace_id": workspace_id, "thread_id": thread_id,
            "key": key, "attempts": count + 1,
            "reserved_at": clock.isoformat(),
            "next_probe_at": (clock + timedelta(seconds=DOM_RECOVERY_DELAYS[min(count + 1, len(DOM_RECOVERY_DELAYS) - 1)])).isoformat(),
            "mode": "recover-submitted-only",
        })
    except (OSError, TypeError, ValueError, KeyError, OverflowError):
        return "blocked"
    return "reserved"
