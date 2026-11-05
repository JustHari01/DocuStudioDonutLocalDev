import threading
from unittest.mock import Mock, patch

import pytest

from docurandom.content_ai import ChatGPTWebChat, ContentAICancelled, ContentAIError
from docurandom.chatgpt_turn_queue import QueuedChatGPTWebChat
from docurandom.ui import _SharedChatGPTTurnPool, _chatgpt_rate_limit_cooldown_seconds


def chat(tmp_path, pool, **kwargs):
    return QueuedChatGPTWebChat('http://127.0.0.1:17841', 'chatgpt-web/high',
                               tmp_path, 'fixture-' + 'x' * 48, turn_pool=pool, **kwargs)


def test_next_workspace_precedes_same_workflows_next_continue(tmp_path):
    pool = _SharedChatGPTTurnPool(1)
    first_started, other_queued = threading.Event(), threading.Event()
    order, failures = [], []
    first = chat(tmp_path, pool)
    second = chat(tmp_path, pool, on_wait=lambda *_: other_queued.set())

    def send(_chat, prompt, **kwargs):
        order.append(prompt)
        assert pool.active_count == 1
        if prompt == 'A1':
            first_started.set()
            assert other_queued.wait(2)
        return prompt + '-response'

    def run_first():
        try:
            first.send('A1')
            first.send('A2')
        except Exception as exc: failures.append(exc)

    with patch.object(ChatGPTWebChat, 'send', send):
        thread = threading.Thread(target=run_first, daemon=True)
        thread.start()
        assert first_started.wait(2)
        assert second.send('B') == 'B-response'
        thread.join(2)
    assert not thread.is_alive() and not failures
    assert order == ['A1', 'B', 'A2']
    assert pool.active_count == 0


def test_transport_recovery_inside_send_keeps_one_lease(tmp_path):
    pool = _SharedChatGPTTurnPool(1)
    acquired = Mock()
    client = chat(tmp_path, pool, on_acquired=acquired)
    client.thread_id = 'same-accepted-thread'

    def send(client, *_args, **_kwargs):
        return client.recover_pending(pending_prompt='exact accepted prompt')

    def recover(client, **kwargs):
        assert pool.active_count == 1
        assert kwargs['pending_prompt'] == 'exact accepted prompt'
        return 'existing answer'

    with patch.object(ChatGPTWebChat, 'send', send), patch.object(ChatGPTWebChat, 'recover_pending', recover):
        assert client.send('TIẾP') == 'existing answer'
    assert acquired.call_count == 1
    assert client.thread_id == 'same-accepted-thread'
    assert isinstance(client, ChatGPTWebChat)
    assert pool.active_count == 0


def test_error_marks_account_backoff_before_releasing_slot(tmp_path):
    pool = _SharedChatGPTTurnPool(1)
    failure = ContentAIError('HTTP 429')

    def failed(exc):
        assert pool.active_count == 1 and exc is failure
        pool.mark_rate_limited(cooldown_seconds=300)

    client = chat(tmp_path, pool, on_failure=failed)
    with patch.object(ChatGPTWebChat, 'recover_pending', side_effect=failure):
        with pytest.raises(ContentAIError): client.recover_pending()
    assert pool.active_count == 0 and pool.current_limit == 0


def test_cancel_while_queued_never_touches_browser_or_releases_another_owner(tmp_path):
    pool = _SharedChatGPTTurnPool(1)
    assert pool.acquire()
    cancel = threading.Event(); cancel.set()
    client = chat(tmp_path, pool)
    with patch.object(ChatGPTWebChat, 'send') as send:
        with pytest.raises(ContentAICancelled): client.send('TIẾP', cancel_event=cancel)
        send.assert_not_called()
    assert pool.active_count == 1
    pool.release()


def test_callbacks_cannot_leak_a_queue_slot(tmp_path):
    pool = _SharedChatGPTTurnPool(1)
    client = chat(tmp_path, pool, on_acquired=Mock(side_effect=ValueError('callback failed')))
    with patch.object(ChatGPTWebChat, 'send') as send:
        with pytest.raises(ValueError): client.send('TIẾP')
        send.assert_not_called()
    assert pool.active_count == 0


def test_without_pool_preserves_base_transport(tmp_path):
    client = chat(tmp_path, None)
    with patch.object(ChatGPTWebChat, 'send', return_value='response') as send:
        assert client.send('TIẾP', output_mode='text') == 'response'
        send.assert_called_once_with('TIẾP', output_mode='text')


@pytest.mark.parametrize('header,expected', [('1200',1200), ('60',300), ('0',300), ('999999999999',300)])
def test_provider_retry_header_is_honored_with_bounds(header, expected):
    assert _chatgpt_rate_limit_cooldown_seconds('[connection=rate-limited;http=429;retry-after-seconds='+header+']') == expected
