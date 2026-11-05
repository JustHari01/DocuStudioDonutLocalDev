"""Exercise production input commands against a real, isolated Chromium target."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import urllib.request

import pytest

from docurandom.browser_input import BrowserInput
from docurandom.google_task_policy import GoogleTaskPending


def test_real_browser_click_unicode_shortcut_and_overlay(tmp_path):
    executable = os.environ.get('DOCURANDOM_TEST_ELECTRON')
    if not executable or not Path(executable).is_file():
        pytest.skip('Set DOCURANDOM_TEST_ELECTRON to an Electron executable for the isolated runtime check')
    fixture = Path(__file__).parent / 'fixtures/browser-input-runtime.cjs'
    replies = queue.Queue()
    with (tmp_path / 'electron-stderr.log').open('w', encoding='utf-8') as errors:
        process = subprocess.Popen([executable, str(fixture), str(tmp_path / 'isolated-profile')],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                   encoding='utf-8', text=True, bufsize=1)
        def reader():
            for line in process.stdout:
                if not line.strip(): continue
                try: replies.put(json.loads(line))
                except ValueError: replies.put({'error': 'Unexpected fixture output', 'line': line[:300]})
        threading.Thread(target=reader, daemon=True).start()
        class Connection:
            identity = 0
            def command(self, method, params=None):
                self.identity += 1
                request = urllib.request.Request(ready['endpoint'], method='POST',
                    data=json.dumps({'id': self.identity, 'method': method, 'params': params or {}}).encode(),
                    headers={'Authorization': 'Bearer ' + ready['token'], 'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=15) as reply:
                    response = json.load(reply)
                assert response.get('id') == self.identity, response
                assert not response.get('error'), response
                return response['result']
        cdp = None
        try:
            ready = replies.get(timeout=30)
            assert ready.get('ready'), ready
            cdp = Connection()
            def guard():
                url = cdp.command('Page.getFrameTree')['frameTree']['frame']['url']
                assert url.startswith('data:text/html;charset=utf-8,'), 'Fixture navigated away'
            action = BrowserInput(cdp, guard)
            action.click_input('#title')
            action.press('a', 'KeyA', 65, 2)
            action.insert_text('日本の技術 · Tiếng Việt')
            action.press('Tab', 'Tab', 9)
            result = cdp.command('fixture.snapshot')
            assert result['value'] == '日本の技術 · Tiếng Việt'
            events = result['events']
            assert any(e['type'] == 'mousedown' and e['target'] == 'title' for e in events)
            assert any(e['type'] == 'input' and e['target'] == 'title' for e in events)
            assert any(e['type'] == 'keydown' and e['key'] == 'Tab' for e in events)
            cdp.command('fixture.overlay')
            with pytest.raises(GoogleTaskPending, match='che'):
                action.click_input('#title')
            assert cdp.command('fixture.snapshot')['value'] == result['value']
        finally:
            if cdp is not None:
                try: cdp.command('fixture.quit')
                except Exception: pass
            process.stdin.close()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate(); process.wait(timeout=10)
            process.stdout.close()
