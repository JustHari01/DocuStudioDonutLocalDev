from __future__ import annotations

import http.client
import http.server
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from docurandom.content_ai import (
    ChatGPTWebChat,
    ContentAICancelled,
    ContentAIError,
    ChatGPTPreSubmitRejected,
)


@contextmanager
def local_bridge(
    *,
    mode: str = "stream",
    connection_close: bool = False,
    thread_id: str | None = None,
    response_id: str = "",
):
    """Exercise real HTTP transport without opening ChatGPT or using an account."""

    state = SimpleNamespace(
        requests=[],
        ready=threading.Event(),
        release=threading.Event(),
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state.requests.append(json.loads(body))
            if mode == 'pre-submit-rejected':
                raw=json.dumps({'error':{'message':'This workspace has an interrupted ChatGPT turn; recover its DOM before sending another prompt'}}).encode()
                self.send_response(409)
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers();self.wfile.write(raw);self.wfile.flush()
                self.close_connection=True
                return
            if mode == "headers":
                state.ready.set()
                state.release.wait(5)
                self.close_connection = True
                return
            self.send_response(200)
            if connection_close:
                self.send_header("Connection", "close")
            if mode == "body":
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b'{"output_text":')
            else:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                event = {"type": "response.status", "stage": "audit", "message": "test"}
                if mode == "complete":
                    event = {
                        "type": "response.completed",
                        "response": {"id": "resp_test", "output_text": "Done"},
                    }
                raw = json.dumps(event).encode() + b"\n"
                self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
                if mode in ("complete", "eof"):
                    self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            state.ready.set()
            if mode not in ("complete", "eof"):
                state.release.wait(5)
            self.close_connection = True

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.02), daemon=True
    )
    server_thread.start()
    chat = ChatGPTWebChat(
        f"http://127.0.0.1:{server.server_port}",
        "chatgpt-web/local-test",
        Path.cwd(),
        "t" * 48,
        thread_id=thread_id,
        response_id=response_id,
    )
    try:
        yield chat, state
    finally:
        chat.cancel()
        state.release.set()
        server.shutdown()
        server.server_close()
        server_thread.join(3)


class ChatGPTWebTransportTests(unittest.TestCase):
    def test_pre_submit_409_does_not_recover_or_resend_another_turn(self):
        with local_bridge(mode='pre-submit-rejected') as (chat,state):
            with patch.object(chat,'recover_pending') as recover:
                with self.assertRaises(ChatGPTPreSubmitRejected):chat.send('JSON repair',output_mode='verified_json')
                recover.assert_not_called()
            self.assertEqual(len(state.requests),1)

    @staticmethod
    def start_reader(chat, cancel_event=None, *, streaming=True, on_status=None):
        results = []
        errors = []

        def read() -> None:
            try:
                results.append(
                    chat.send(
                        "local test only",
                        cancel_event=cancel_event,
                        on_status=(on_status or (lambda *_: None)) if streaming else None,
                    )
                )
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=read, name="AuditTransportTestReader", daemon=True)
        worker.start()
        return worker, results, errors

    def test_cancel_between_chunk_parse_and_peek_does_not_close_response(self) -> None:
        # This is the exact interleaving from the customer's NoneType.peek
        # traceback. The old connection.close() implementation fails here.
        for use_event in (False, True):
            for connection_close in (False, True):
                with self.subTest(use_event=use_event, connection_close=connection_close):
                    at_peek = threading.Event()
                    resume_read = threading.Event()
                    interrupted = threading.Event()
                    cancel_event = threading.Event() if use_event else None
                    responses = []
                    original_get_chunk = http.client.HTTPResponse._get_chunk_left

                    def pause_before_peek(response):
                        size = original_get_chunk(response)
                        if size and threading.current_thread().name == "AuditTransportTestReader":
                            responses.append(response)
                            at_peek.set()
                            resume_read.wait(5)
                        return size

                    with local_bridge(connection_close=connection_close) as (chat, state):
                        original_interrupt = chat._cancel_request

                        def track_interrupt(request):
                            original_interrupt(request)
                            interrupted.set()

                        with (
                            patch.object(http.client.HTTPResponse, "_get_chunk_left", pause_before_peek),
                            patch.object(chat, "_cancel_request", track_interrupt),
                        ):
                            worker, results, errors = self.start_reader(chat, cancel_event)
                            try:
                                self.assertTrue(at_peek.wait(3))
                                if use_event:
                                    cancel_event.set()
                                else:
                                    chat.cancel()
                                self.assertTrue(interrupted.wait(3))
                                self.assertIsNotNone(responses[0].fp)
                            finally:
                                resume_read.set()
                                worker.join(3)
                            self.assertFalse(worker.is_alive())
                            self.assertEqual(results, [])
                            self.assertEqual(len(errors), 1)
                            self.assertIsInstance(errors[0], ContentAICancelled)
                            self.assertEqual(len(state.requests), 1)
                            self.assertEqual(chat.response_id, "")
                            self.assertIsNone(chat._active_request)
                            self.assertIsNone(responses[0].fp)  # reader owns cleanup

    def test_cancel_unblocks_headers_and_nonstream_body_without_retry(self) -> None:
        for mode in ("headers", "body", "stream"):
            with self.subTest(mode=mode), local_bridge(mode=mode) as (chat, state):
                worker, results, errors = self.start_reader(chat, streaming=mode != "body")
                self.assertTrue(state.ready.wait(3))
                chat.cancel()
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results, [])
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], ContentAICancelled)
                self.assertEqual(len(state.requests), 1)

    def test_uncancelled_premature_eof_stays_an_error(self) -> None:
        with local_bridge(mode="eof") as (chat, state):
            with self.assertRaises(ContentAIError) as caught:
                chat.send("test", on_status=lambda *_: None)
            self.assertNotIsInstance(caught.exception, ContentAICancelled)
            self.assertEqual(len(state.requests), 1)
            self.assertEqual(chat.response_id, "")

    def test_accepted_turn_error_recovers_output_without_resending_prompt(self) -> None:
        error_event = {
            "type": "error",
            "error": {
                "message": (
                    "ChatGPT đã chuyển khỏi đúng cuộc hội thoại đang xử lý; "
                    "checkpoint vẫn được giữ, không gửi lại prompt."
                )
            },
        }

        class _StreamingResponse:
            status = 200

            def __init__(self) -> None:
                self.lines = [(json.dumps(error_event, ensure_ascii=False) + "\n").encode("utf-8")]

            def readline(self, _limit: int) -> bytes:
                return self.lines.pop(0) if self.lines else b""

        class _StreamingConnection:
            def __init__(self, *_args, **_kwargs) -> None:
                self.request_count = 0

            def request(self, *_args, **_kwargs) -> None:
                self.request_count += 1

            def getresponse(self):
                return _StreamingResponse()

            def close(self) -> None:
                return None

        with (
            tempfile.TemporaryDirectory(prefix="DocuRandomWebAcceptedError_") as temp,
            patch(
                "docurandom.content_ai.http.client.HTTPConnection",
                _StreamingConnection,
            ),
        ):
            chat = ChatGPTWebChat(
                "http://127.0.0.1:17841",
                "chatgpt-web/high",
                Path(temp),
                "t" * 48,
            )
            with patch.object(chat, "recover_pending", return_value="Recovered output") as recover:
                self.assertEqual(
                    chat.send("accepted prompt", on_status=lambda *_: None),
                    "Recovered output",
                )
            recover.assert_called_once()

    def test_cancelled_event_before_send_creates_no_request(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        with local_bridge() as (chat, state):
            with self.assertRaises(ContentAICancelled):
                chat.send("test", cancel_event=cancel_event)
            self.assertEqual(state.requests, [])

    def test_completed_response_is_preserved_and_chat_can_be_reused(self) -> None:
        with local_bridge(mode="complete") as (chat, state):
            self.assertEqual(chat.send("one", on_status=lambda *_: None), "Done")
            chat.cancel()  # idle cancellation must not poison the next request
            self.assertEqual(chat.send("two", on_status=lambda *_: None), "Done")
            self.assertEqual(chat.response_id, "resp_test")
            self.assertEqual(len(state.requests), 2)
            self.assertEqual(state.requests[1]["previous_response_id"], "resp_test")

    def test_video_session_reuses_supplied_thread_and_stage_contract(self) -> None:
        with local_bridge(
            mode="complete",
            thread_id="video-thread-20260920",
            response_id="resp_thumbnail",
        ) as (chat, state):
            self.assertEqual(chat.thread_id, "video-thread-20260920")
            self.assertEqual(
                chat.send(
                    "audit",
                    output_mode="voicevox_audit_json",
                    on_status=lambda *_: None,
                ),
                "Done",
            )

        self.assertEqual(state.requests[0]["prompt_cache_key"], "video-thread-20260920")
        self.assertEqual(state.requests[0]["metadata"]["thread_id"], "video-thread-20260920")
        self.assertEqual(state.requests[0]["previous_response_id"], "resp_thumbnail")
        self.assertEqual(state.requests[0]["docurandom_output"], "voicevox_audit_json")

    def test_late_watcher_cannot_cancel_next_request(self) -> None:
        with local_bridge(mode="complete") as (chat, _state):
            requests = []
            original_getresponse = http.client.HTTPConnection.getresponse

            def capture_request(connection):
                requests.append(chat._active_request)
                if len(requests) == 2:
                    chat._cancel_request(requests[0])
                return original_getresponse(connection)

            with patch.object(http.client.HTTPConnection, "getresponse", capture_request):
                self.assertEqual(chat.send("one", on_status=lambda *_: None), "Done")
                self.assertEqual(chat.send("two", on_status=lambda *_: None), "Done")
            self.assertFalse(requests[1].cancelled.is_set())

    def test_callback_programming_error_is_not_swallowed(self) -> None:
        def broken_callback(*_args):
            raise AttributeError("callback bug")

        with local_bridge() as (chat, _state):
            with self.assertRaisesRegex(AttributeError, "callback bug"):
                chat.send("test", on_status=broken_callback)


if __name__ == "__main__":
    unittest.main()
