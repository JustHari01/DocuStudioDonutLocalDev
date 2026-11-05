from __future__ import annotations

from queue import Empty

from docurandom.ui import _CoalescingEventQueue, _SharedTkEventPump


class _FakeRoot:
    def __init__(self) -> None:
        self.next_id = 0
        self.after_calls: list[tuple[int, object]] = []
        self.cancelled: list[object] = []
        self.callback_errors: list[BaseException] = []

    def after(self, delay: int, callback: object) -> str:
        self.next_id += 1
        identifier = f"after-{self.next_id}"
        self.after_calls.append((delay, callback))
        return identifier

    def after_cancel(self, identifier: object) -> None:
        self.cancelled.append(identifier)

    def report_callback_exception(self, _exc_type: object, exc: BaseException, _tb: object) -> None:
        self.callback_errors.append(exc)


class _FakeApp:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[dict[str, object]] = []

    def _process_events(self, **kwargs: object) -> int:
        self.calls.append(kwargs)
        return int(kwargs["max_events"])


def test_progress_events_are_coalesced_but_terminal_events_remain_fifo() -> None:
    events = _CoalescingEventQueue()

    events.put(("content_stream", 7, "partial-1"))
    events.put(("content_stream", 7, "partial-2"))
    events.put(("content_done", 7, "done"))

    assert events.qsize() == 2
    assert events.get_nowait() == ("content_stream", 7, "partial-2")
    assert events.get_nowait() == ("content_done", 7, "done")
    try:
        events.get_nowait()
    except Empty:
        pass
    else:
        raise AssertionError("queue unexpectedly contained another event")


def test_progress_for_different_jobs_is_not_overwritten() -> None:
    events = _CoalescingEventQueue()
    events.put(("job_progress", "job-a", "render", 0.2, "A"))
    events.put(("job_progress", "job-b", "render", 0.8, "B"))
    events.put(("job_progress", "job-a", "render", 0.4, "A latest"))

    assert events.qsize() == 2
    assert events.get_nowait() == ("job_progress", "job-a", "render", 0.4, "A latest")
    assert events.get_nowait() == ("job_progress", "job-b", "render", 0.8, "B")


def test_shared_tk_event_pump_schedules_one_callback_and_services_all_apps() -> None:
    root = _FakeRoot()
    pump = _SharedTkEventPump(
        root,
        interval_ms=20,
        per_app_budget=3,
        global_budget=9,
        time_budget_ms=1000,
    )
    apps = [_FakeApp(), _FakeApp(), _FakeApp()]

    for app in apps:
        pump.register(app)

    assert len(root.after_calls) == 1
    pump._tick()

    assert len(root.after_calls) == 2
    assert all(len(app.calls) == 1 for app in apps)
    assert all(app.calls[0]["reschedule"] is False for app in apps)
    assert all(app.calls[0]["max_events"] == 3 for app in apps)
    assert root.callback_errors == []


def test_shared_tk_event_pump_rotates_and_unregisters_cleanly() -> None:
    root = _FakeRoot()
    pump = _SharedTkEventPump(root, time_budget_ms=1000)
    apps = [_FakeApp(), _FakeApp()]
    for app in apps:
        pump.register(app)

    pump._tick()
    pump._tick()
    assert [len(app.calls) for app in apps] == [2, 2]

    pump.unregister(apps[0])
    pump.unregister(apps[1])
    assert root.cancelled
    assert pump._after_id is None
