from __future__ import annotations

import threading
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor

from docurandom.batch import AdaptiveRenderPool, ProgressAggregator, TaskCancelled


class AdaptiveRenderPoolTests(unittest.TestCase):
    def test_ten_workspaces_share_limit_and_all_finish_with_correct_results(self) -> None:
        for limit in (1, 2, 4):
            with self.subTest(limit=limit):
                pool = AdaptiveRenderPool(max_workers=4, active_limit=limit)
                release = threading.Event()
                saturated = threading.Event()
                lock = threading.Lock()
                active = 0
                peak = 0

                def make_task(index: int):
                    def task(_cancel: threading.Event) -> int:
                        nonlocal active, peak
                        with lock:
                            active += 1
                            peak = max(peak, active)
                            if active == limit:
                                saturated.set()
                        try:
                            self.assertTrue(release.wait(5))
                            return index
                        finally:
                            with lock:
                                active -= 1
                    return task

                try:
                    handles = [
                        pool.submit(f"workspace-{index}", make_task(index))
                        for index in range(10)
                    ]
                    self.assertTrue(saturated.wait(3))
                    self.assertEqual(pool.queue_snapshot(), (limit, limit, 10 - limit))
                    release.set()
                    self.assertEqual(
                        [handle.future.result(5) for handle in handles], list(range(10))
                    )
                    self.assertEqual(peak, limit)
                    self.assertEqual(pool.queue_snapshot(), (limit, 0, 0))
                finally:
                    release.set()
                    pool.shutdown()

    def test_queue_snapshot_counts_waiting_jobs_across_shared_pool(self) -> None:
        pool = AdaptiveRenderPool(max_workers=4, active_limit=1)
        release = threading.Event()
        started = threading.Event()

        def task(_cancel: threading.Event) -> None:
            started.set()
            release.wait(5)

        try:
            handles = [pool.submit(f"workspace-{index}", task) for index in range(3)]
            self.assertTrue(started.wait(2))
            self.assertEqual(pool.queue_snapshot(), (1, 1, 2))
            self.assertEqual(pool.update_limit(2), 2)
            for _ in range(30):
                if pool.queue_snapshot()[1] == 2:
                    break
                threading.Event().wait(0.02)
            self.assertEqual(pool.queue_snapshot(), (2, 2, 1))
            release.set()
            for handle in handles:
                handle.future.result(5)
        finally:
            release.set()
            pool.shutdown()

    def test_forget_releases_only_terminal_handles(self) -> None:
        pool = AdaptiveRenderPool(max_workers=1, active_limit=1)
        gate = threading.Event()
        started = threading.Event()

        def task(_cancel: threading.Event) -> str:
            started.set()
            self.assertTrue(gate.wait(3))
            return "done"

        try:
            handle = pool.submit("forget-me", task)
            self.assertTrue(started.wait(2))
            self.assertFalse(pool.forget("forget-me"))
            gate.set()
            self.assertEqual(handle.future.result(3), "done")
            self.assertTrue(pool.forget("forget-me"))
            self.assertIsNone(pool.get_handle("forget-me"))
            self.assertFalse(pool.forget("forget-me"))
        finally:
            gate.set()
            pool.shutdown(wait=True)

    def test_tasks_overlap_without_exceeding_active_limit(self) -> None:
        pool = AdaptiveRenderPool(max_workers=4, active_limit=2)
        gate = threading.Event()
        two_started = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0
        starts = 0

        def task(_cancel: threading.Event) -> str:
            nonlocal active, peak, starts
            with lock:
                active += 1
                starts += 1
                peak = max(peak, active)
                if starts >= 2:
                    two_started.set()
            self.assertTrue(gate.wait(5))
            with lock:
                active -= 1
            return "done"

        try:
            handles = [pool.submit(f"job-{index}", task) for index in range(3)]
            self.assertTrue(two_started.wait(3))
            self.assertEqual(pool.active_count, 2)
            with lock:
                self.assertEqual(starts, 2)
            gate.set()
            self.assertEqual([handle.future.result(5) for handle in handles], ["done"] * 3)
            self.assertEqual(peak, 2)
        finally:
            gate.set()
            pool.shutdown()

    def test_cancel_one_running_task_does_not_cancel_another(self) -> None:
        pool = AdaptiveRenderPool(max_workers=2, active_limit=2)
        first_started = threading.Event()
        second_started = threading.Event()
        release_second = threading.Event()

        def first(cancel: threading.Event) -> str:
            first_started.set()
            while not cancel.wait(0.05):
                pass
            raise TaskCancelled()

        def second(_cancel: threading.Event) -> str:
            second_started.set()
            self.assertTrue(release_second.wait(5))
            return "kept"

        try:
            first_handle = pool.submit("first", first)
            second_handle = pool.submit("second", second)
            self.assertTrue(first_started.wait(3))
            self.assertTrue(second_started.wait(3))
            self.assertTrue(pool.cancel("first"))
            release_second.set()
            with self.assertRaises((TaskCancelled, CancelledError)):
                first_handle.future.result(5)
            self.assertEqual(second_handle.future.result(5), "kept")
        finally:
            release_second.set()
            pool.shutdown()

    def test_runtime_limit_can_open_another_slot(self) -> None:
        pool = AdaptiveRenderPool(max_workers=3, active_limit=1)
        gate = threading.Event()
        two_started = threading.Event()
        lock = threading.Lock()
        starts = 0

        def task(_cancel: threading.Event) -> None:
            nonlocal starts
            with lock:
                starts += 1
                if starts >= 2:
                    two_started.set()
            gate.wait(5)

        try:
            handles = [pool.submit(f"job-{index}", task) for index in range(2)]
            with lock:
                self.assertLessEqual(starts, 1)
            pool.update_limit(2)
            self.assertTrue(two_started.wait(3))
            gate.set()
            for handle in handles:
                handle.future.result(5)
        finally:
            gate.set()
            pool.shutdown()

    def test_concurrent_duplicate_submit_publishes_exactly_one_handle(self) -> None:
        pool = AdaptiveRenderPool(max_workers=2, active_limit=2)
        caller_gate = threading.Event()

        def submit_same_id() -> object:
            self.assertTrue(caller_gate.wait(5))
            try:
                return pool.submit("same-job", lambda _cancel: "done")
            except ValueError:
                return "duplicate"

        try:
            with ThreadPoolExecutor(max_workers=12) as callers:
                outcomes = [callers.submit(submit_same_id) for _ in range(12)]
                caller_gate.set()
                values = [outcome.result(5) for outcome in outcomes]
            handles = [value for value in values if value != "duplicate"]
            self.assertEqual(len(handles), 1)
            self.assertEqual(handles[0].future.result(5), "done")
        finally:
            caller_gate.set()
            pool.shutdown()

    def test_waiting_callback_can_cancel_published_job(self) -> None:
        pool = AdaptiveRenderPool(max_workers=1, active_limit=1)
        cancel_results: list[bool] = []
        task_called = threading.Event()

        def state_callback(job_id: str, state: str) -> None:
            if state == "waiting":
                cancel_results.append(pool.cancel(job_id))

        def task(_cancel: threading.Event) -> None:
            task_called.set()

        try:
            handle = pool.submit("cancel-from-callback", task, state_callback=state_callback)
            with self.assertRaises((TaskCancelled, CancelledError)):
                handle.future.result(5)
            self.assertEqual(cancel_results, [True])
            self.assertFalse(task_called.is_set())
            self.assertEqual(pool.active_count, 0)
        finally:
            pool.shutdown()

    def test_observer_callback_failure_does_not_leak_slot(self) -> None:
        pool = AdaptiveRenderPool(max_workers=1, active_limit=1)

        def broken_callback(_job_id: str, _state: str) -> None:
            raise RuntimeError("observer failed")

        try:
            handle = pool.submit(
                "callback-error",
                lambda _cancel: "rendered",
                state_callback=broken_callback,
            )
            self.assertEqual(handle.future.result(5), "rendered")
            self.assertEqual(pool.active_count, 0)
        finally:
            pool.shutdown()

    def test_cancel_waiting_job_never_invokes_task(self) -> None:
        pool = AdaptiveRenderPool(max_workers=2, active_limit=1)
        release_running = threading.Event()
        running_started = threading.Event()
        waiting_seen = threading.Event()
        waiting_task_called = threading.Event()
        states: list[str] = []

        def running(_cancel: threading.Event) -> None:
            running_started.set()
            self.assertTrue(release_running.wait(5))

        def waiting(_cancel: threading.Event) -> None:
            waiting_task_called.set()

        def observe(job_id: str, state: str) -> None:
            if job_id == "waiting":
                states.append(state)
                if state == "waiting":
                    waiting_seen.set()

        try:
            running_handle = pool.submit("running", running)
            self.assertTrue(running_started.wait(3))
            waiting_handle = pool.submit("waiting", waiting, state_callback=observe)
            self.assertTrue(waiting_seen.wait(3))
            self.assertTrue(pool.cancel("waiting"))
            with self.assertRaises((TaskCancelled, CancelledError)):
                waiting_handle.future.result(5)
            self.assertFalse(waiting_task_called.is_set())
            if not waiting_handle.future.cancelled():
                self.assertIn("cancelled", states)
            release_running.set()
            running_handle.future.result(5)
        finally:
            release_running.set()
            pool.shutdown()

    def test_lower_limit_blocks_new_start_until_active_count_drops(self) -> None:
        pool = AdaptiveRenderPool(max_workers=4, active_limit=3)
        releases = [threading.Event() for _ in range(4)]
        starts = [threading.Event() for _ in range(4)]

        def make_task(index: int):
            def task(_cancel: threading.Event) -> int:
                starts[index].set()
                self.assertTrue(releases[index].wait(5))
                return index

            return task

        try:
            handles = [pool.submit(f"job-{index}", make_task(index)) for index in range(3)]
            self.assertTrue(all(start.wait(3) for start in starts[:3]))
            self.assertEqual(pool.update_limit(1), 1)
            fourth = pool.submit("job-3", make_task(3))

            releases[0].set()
            releases[1].set()
            self.assertEqual(handles[0].future.result(5), 0)
            self.assertEqual(handles[1].future.result(5), 1)
            self.assertEqual(pool.active_count, 1)
            self.assertFalse(starts[3].is_set())

            releases[2].set()
            self.assertEqual(handles[2].future.result(5), 2)
            self.assertTrue(starts[3].wait(3))
            releases[3].set()
            self.assertEqual(fourth.future.result(5), 3)
        finally:
            for release in releases:
                release.set()
            pool.shutdown()

    def test_shutdown_wait_false_can_later_be_joined(self) -> None:
        pool = AdaptiveRenderPool(max_workers=1, active_limit=1)
        started = threading.Event()
        saw_cancel = threading.Event()
        allow_exit = threading.Event()
        joined = threading.Event()

        def task(cancel: threading.Event) -> None:
            started.set()
            self.assertTrue(cancel.wait(5))
            saw_cancel.set()
            self.assertTrue(allow_exit.wait(5))

        handle = pool.submit("slow-cleanup", task)
        self.assertTrue(started.wait(3))
        pool.shutdown(wait=False)
        self.assertTrue(saw_cancel.wait(3))

        def join_pool() -> None:
            pool.shutdown(wait=True)
            joined.set()

        join_thread = threading.Thread(target=join_pool, name="PoolJoinTest")
        join_thread.start()
        self.assertFalse(joined.wait(0.2))
        allow_exit.set()
        self.assertTrue(joined.wait(3))
        join_thread.join(3)
        handle.future.result(3)


class ProgressAggregatorTests(unittest.TestCase):
    def test_progress_is_per_job_monotonic_and_averaged(self) -> None:
        progress = ProgressAggregator()
        for job_id in ("a", "b", "c"):
            progress.add(job_id)
        progress.update("a", 0.5)
        progress.update("b", 1.0)
        overall = progress.update("c", 0.25)
        self.assertAlmostEqual(overall, (0.5 + 1.0 + 0.25) / 3)
        progress.update("a", 0.1)
        self.assertEqual(progress.snapshot()["a"], 0.5)

    def test_explicit_retry_can_reset_one_terminal_job(self) -> None:
        progress = ProgressAggregator()
        progress.add("failed")
        progress.add("complete")
        progress.finish("failed")
        progress.finish("complete")

        overall = progress.reset("failed")

        self.assertEqual(progress.snapshot()["failed"], 0.0)
        self.assertAlmostEqual(overall, 0.5)


if __name__ == "__main__":
    unittest.main()
