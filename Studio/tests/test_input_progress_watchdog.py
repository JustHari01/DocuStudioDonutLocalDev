import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from docurandom.content_ai import CodexCliChat


class InputProgressWatchdogTests(unittest.TestCase):
    def run_watchdog(self, phase, *, advancing=False):
        stop = threading.Event()
        stalled = []
        calls = []

        class Connection:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                calls.append(len(calls) + 1)
                return self

            status = 200

            def read(self, _limit):
                count = len(calls)
                if (advancing or phase == "waiting") and count >= 8:
                    stop.set()
                return json.dumps({
                    "active": True, "stage": phase + ":" + str(count),
                    "message": "elapsed status " + str(count),
                    "updated_at": count, "activity_updated_at": count if advancing else 1,
                    "turn_id": "turn_exact_123", "no_response_evidence": False,
                }).encode()

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            chat = CodexCliChat(Path("codex.exe"), Path(directory),
                                access_token="A" * 48, endpoint="http://127.0.0.1:1")
            chat.thread_id = "thread_exact"
            with patch("docurandom.content_ai.http.client.HTTPConnection", Connection):
                worker = threading.Thread(target=chat._poll_bridge_activity, kwargs=dict(
                    stop_event=stop, on_partial=None, on_status=None,
                    on_stalled=lambda *args: (stalled.append(args), stop.set()),
                    poll_interval=0.01, stale_after=1, input_progress_stale_after=0.12,
                ))
                worker.start()
                worker.join(2)
                stop.set()
                worker.join(1)
        self.assertFalse(worker.is_alive())
        return stalled, calls

    def test_import_elapsed_heartbeats_cannot_hide_no_progress(self):
        stalled, _ = self.run_watchdog("inline-import")
        self.assertEqual(len(stalled), 1)
        self.assertIn("CHATGPT_INPUT_PROGRESS_STALLED", stalled[0][0])
        self.assertEqual(stalled[0][1:], ("thread_exact", "turn_exact_123"))

    def test_queued_elapsed_heartbeats_cannot_hold_a_workspace_forever(self):
        stalled, _ = self.run_watchdog("trusted-input-queued")
        self.assertEqual(len(stalled), 1)

    def test_verified_import_progress_extends_the_lease(self):
        stalled, calls = self.run_watchdog("inline-import", advancing=True)
        self.assertEqual(stalled, [])
        self.assertGreaterEqual(len(calls), 8)

    def test_model_thinking_is_not_subject_to_the_input_deadline(self):
        stalled, calls = self.run_watchdog("waiting")
        self.assertEqual(stalled, [])
        self.assertGreaterEqual(len(calls), 8)
