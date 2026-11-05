from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Condition, Event, RLock
from typing import Callable, Generic, TypeVar


ResultT = TypeVar("ResultT")
TaskCallable = Callable[[Event], ResultT]
StateCallback = Callable[[str, str], None]


class TaskCancelled(Exception):
    """Raised when a queued task is cancelled before it obtains a render slot."""


@dataclass(frozen=True)
class TaskHandle(Generic[ResultT]):
    job_id: str
    cancel_event: Event
    future: Future[ResultT]


class AdaptiveRenderPool:
    """Persistent worker pool with a runtime-adjustable active render limit.

    The executor keeps a small fixed number of threads.  The condition-controlled
    slot limit is what balances CPU/RAM/GPU use and can be updated after hardware
    probing without rebuilding the executor or losing queued jobs.
    """

    def __init__(self, *, max_workers: int = 4, active_limit: int = 1) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.max_workers = max_workers
        self._limit = max(1, min(max_workers, active_limit))
        self._condition = Condition(RLock())
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="DocuRandomJob",
        )
        self._handles: dict[str, TaskHandle[object]] = {}
        self._active = 0
        self._closed = False

    @property
    def active_limit(self) -> int:
        with self._condition:
            return self._limit

    @property
    def active_count(self) -> int:
        with self._condition:
            return self._active

    def queue_snapshot(self) -> tuple[int, int, int]:
        """Return (limit, active, waiting) from one consistent lock snapshot."""

        with self._condition:
            unfinished = sum(
                1 for handle in self._handles.values() if not handle.future.done()
            )
            waiting = max(0, unfinished - self._active)
            return self._limit, self._active, waiting

    def update_limit(self, value: int) -> int:
        with self._condition:
            self._limit = max(1, min(self.max_workers, int(value)))
            self._condition.notify_all()
            return self._limit

    def submit(
        self,
        job_id: str,
        task: TaskCallable[ResultT],
        *,
        state_callback: StateCallback | None = None,
    ) -> TaskHandle[ResultT]:
        callback = state_callback or (lambda _job_id, _state: None)
        cancel_event = Event()

        def notify(state: str) -> None:
            # State reporting is an observer hook. A broken UI/log callback must
            # not leak an active slot or prevent the render callable from running.
            try:
                callback(job_id, state)
            except Exception:
                pass

        def run() -> ResultT:
            notify("waiting")
            with self._condition:
                while self._active >= self._limit and not cancel_event.is_set():
                    self._condition.wait(timeout=0.15)
                if cancel_event.is_set():
                    notify("cancelled")
                    raise TaskCancelled()
                self._active += 1
            try:
                notify("running")
                return task(cancel_event)
            finally:
                with self._condition:
                    self._active = max(0, self._active - 1)
                    self._condition.notify_all()

        # Keep duplicate detection, executor submission and handle publication in
        # one critical section. Otherwise two caller threads can both pass the
        # duplicate check, or a worker callback can try to cancel a job whose
        # handle has not been published yet.
        with self._condition:
            if self._closed:
                raise RuntimeError("render pool is closed")
            if job_id in self._handles:
                raise ValueError(f"duplicate job id: {job_id}")
            future: Future[ResultT] = self._executor.submit(run)
            handle: TaskHandle[ResultT] = TaskHandle(job_id, cancel_event, future)
            self._handles[job_id] = handle  # type: ignore[assignment]
        return handle

    def get_handle(self, job_id: str) -> TaskHandle[object] | None:
        with self._condition:
            return self._handles.get(job_id)

    def cancel(self, job_id: str) -> bool:
        with self._condition:
            handle = self._handles.get(job_id)
            if handle is None or handle.future.done():
                return False
            handle.cancel_event.set()
            handle.future.cancel()
            self._condition.notify_all()
            return True

    def cancel_all(self) -> int:
        cancelled = 0
        with self._condition:
            handles = list(self._handles.values())
            for handle in handles:
                if handle.future.done():
                    continue
                handle.cancel_event.set()
                handle.future.cancel()
                cancelled += 1
            self._condition.notify_all()
        return cancelled

    def unfinished_job_ids(self) -> list[str]:
        with self._condition:
            return [
                job_id
                for job_id, handle in self._handles.items()
                if not handle.future.done()
            ]

    def forget(self, job_id: str) -> bool:
        """Release a terminal handle so long-running UI sessions stay bounded."""

        with self._condition:
            handle = self._handles.get(job_id)
            if handle is None or not handle.future.done():
                return False
            del self._handles[job_id]
            return True

    def shutdown(self, *, wait: bool = True) -> None:
        with self._condition:
            if not self._closed:
                self._closed = True
                for handle in self._handles.values():
                    if not handle.future.done():
                        handle.cancel_event.set()
                        handle.future.cancel()
                self._condition.notify_all()
        # ThreadPoolExecutor.shutdown() is intentionally safe to call again. In
        # particular, shutdown(wait=False) followed later by shutdown(wait=True)
        # must still provide a real join point for application exit.
        self._executor.shutdown(wait=wait, cancel_futures=True)


class ProgressAggregator:
    """Thread-safe, per-job monotonic progress and aggregate calculation."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._values: dict[str, float] = {}

    def add(self, job_id: str) -> None:
        with self._lock:
            self._values.setdefault(job_id, 0.0)

    def reset(self, job_id: str, value: float = 0.0) -> float:
        """Reset one terminal job before explicitly retrying that same job."""

        bounded = max(0.0, min(1.0, float(value)))
        with self._lock:
            self._values[job_id] = bounded
            return self.overall_locked()

    def update(self, job_id: str, value: float) -> float:
        bounded = max(0.0, min(1.0, float(value)))
        with self._lock:
            previous = self._values.get(job_id, 0.0)
            self._values[job_id] = max(previous, bounded)
            return self.overall_locked()

    def finish(self, job_id: str) -> float:
        return self.update(job_id, 1.0)

    def overall(self) -> float:
        with self._lock:
            return self.overall_locked()

    def overall_locked(self) -> float:
        if not self._values:
            return 0.0
        return sum(self._values.values()) / len(self._values)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._values)
