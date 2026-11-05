import threading
import pytest
from unittest.mock import patch
from docurandom.youtube_upload import ProfileUploadCoordinator


def test_waiting_google_gets_next_idle_slot_without_stopping_active_monitor():
    c=ProfileUploadCoordinator();active_cancel=threading.Event()
    active=c.try_acquire('one',active_cancel)
    waiting_cancel=threading.Event()
    assert c.try_acquire('google',waiting_cancel,job_kind='google-workspace',priority=-50,wait_key='google:w')is None
    assert not active_cancel.is_set()
    active.__exit__(None,None,None)
    assert c.try_acquire('another-monitor',threading.Event())is None
    lease=c.try_acquire('google',waiting_cancel,job_kind='google-workspace',priority=-50,wait_key='google:w')
    assert lease is not None
    lease.__exit__(None,None,None)
    assert c.try_acquire('another-monitor',threading.Event())is not None


def test_idle_reservation_does_not_block_upload_and_upload_can_preempt_google():
    c=ProfileUploadCoordinator();monitor=c.try_acquire('monitor',threading.Event())
    cancel=threading.Event()
    assert c.try_acquire('google',cancel,wait_key='google:w')is None
    monitor.__exit__(None,None,None)
    upload=c.acquire('upload',threading.Event())
    assert c.try_acquire('google',cancel,wait_key='google:w')is None
    upload.__exit__(None,None,None)
    google=c.try_acquire('google',cancel,wait_key='google:w')
    assert google is not None
    # An actual uploader requests cancellation of lower-priority work, then waits
    # for its lease to release; it never takes the profile while Google is active.
    acquired=threading.Event();uploader_cancel=threading.Event()
    def worker():
        with c.acquire('upload',uploader_cancel):acquired.set()
    thread=threading.Thread(target=worker);thread.start()
    assert cancel.wait(1)and not acquired.is_set()
    google.__exit__(None,None,None);thread.join(2)
    assert acquired.is_set()and not thread.is_alive()


def test_abandoned_waiter_expires_without_blocking_all_monitors():
    c=ProfileUploadCoordinator()
    with patch('docurandom.youtube_upload.time.monotonic',return_value=100):
        active=c.try_acquire('monitor',threading.Event())
        assert c.try_acquire('google',threading.Event(),wait_key='google:w')is None
        active.__exit__(None,None,None)
    with patch('docurandom.youtube_upload.time.monotonic',return_value=131):
        assert c.try_acquire('monitor',threading.Event())is not None


def test_waiter_can_be_cancelled_immediately_and_does_not_grow_on_poll():
    c=ProfileUploadCoordinator();active=c.try_acquire('monitor',threading.Event())
    for _ in range(10):assert c.try_acquire('google',threading.Event(),wait_key='google:w')is None
    assert len(c._idle_waiters)==1
    c.cancel_idle_waiter('google:w');active.__exit__(None,None,None)
    assert c.try_acquire('monitor',threading.Event())is not None


def test_waiter_heartbeat_preserves_order_and_uses_current_cancellation_event():
    c=ProfileUploadCoordinator();active=c.try_acquire('monitor',threading.Event())
    assert c.try_acquire('first',threading.Event(),wait_key='first')is None
    assert c.try_acquire('second',threading.Event(),wait_key='second')is None
    current=threading.Event()
    assert c.try_acquire('first',current,wait_key='first')is None
    active.__exit__(None,None,None)
    assert c.try_acquire('second',threading.Event(),wait_key='second')is None
    lease=c.try_acquire('first',current,wait_key='first')
    assert c._active['first'].cancel_event is current
    lease.__exit__(None,None,None)
    assert c.try_acquire('second',threading.Event(),wait_key='second')is not None


@pytest.mark.parametrize('kind,should_yield',[('monitor',True),('metadata-recovery',False),('upload',False)])
def test_long_idle_wait_only_requests_cooperative_monitor_yield(kind,should_yield):
    c=ProfileUploadCoordinator();active_cancel=threading.Event()
    active=c.try_acquire('busy',active_cancel)if kind=='monitor'else c.acquire('busy',active_cancel,job_kind=kind)
    try:
        for clock in [100,120,140,160,180,200,219]:
            with patch('docurandom.youtube_upload.time.monotonic',return_value=clock):
                assert c.try_acquire('google',threading.Event(),wait_key='google:w')is None
            assert not active_cancel.is_set()
        with patch('docurandom.youtube_upload.time.monotonic',return_value=221):
            assert c.try_acquire('google',threading.Event(),wait_key='google:w')is None
        assert active_cancel.is_set()is should_yield
    finally:active.__exit__(None,None,None)
