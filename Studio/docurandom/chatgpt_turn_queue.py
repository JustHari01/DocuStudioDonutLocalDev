"""Yield the shared browser slot between complete checkpointed ChatGPT turns."""
from __future__ import annotations

import threading
from typing import Callable

from .content_ai import ChatGPTWebChat, ContentAICancelled


class QueuedChatGPTWebChat(ChatGPTWebChat):
    """Keep one conversation while giving each send/recovery its own FIFO lease.

    A send's internal transport recovery stays in the same lease. Releasing a
    lease never cancels an accepted browser turn or changes the model/thread.
    """

    def __init__(self, *args, turn_pool=None, queue_label: str = "",
                 on_wait=None, on_acquired: Callable[[], None] | None = None,
                 on_failure: Callable[[Exception], None] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._turn_pool = turn_pool
        self._queue_label = queue_label
        self._queue_wait = on_wait
        self._queue_acquired = on_acquired
        self._queue_failure = on_failure
        self._queue_local = threading.local()

    def _queued_call(self, operation, *args, **kwargs):
        pool = self._turn_pool
        if pool is None or getattr(self._queue_local, "owned", False):
            return operation(*args, **kwargs)
        acquired = pool.acquire(cancel_event=kwargs.get("cancel_event"),
                                label=self._queue_label, on_wait=self._queue_wait)
        if not acquired:
            raise ContentAICancelled("Đã hủy khi đang chờ lượt ChatGPT.")
        self._queue_local.owned = True
        try:
            if self._queue_acquired is not None:
                self._queue_acquired()
            return operation(*args, **kwargs)
        except Exception as exc:
            if self._queue_failure is not None and not isinstance(exc, ContentAICancelled):
                self._queue_failure(exc)
            raise
        finally:
            self._queue_local.owned = False
            pool.release()

    def send(self, *args, **kwargs):
        return self._queued_call(super().send, *args, **kwargs)

    def recover_pending(self, **kwargs):
        return self._queued_call(super().recover_pending, **kwargs)
