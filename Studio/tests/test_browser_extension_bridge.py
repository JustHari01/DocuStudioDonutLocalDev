from __future__ import annotations

import hashlib
import base64
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

import pytest

from docurandom.browser_extension_bridge import (
    BROWSER_EXTENSION_ID,
    BrowserExtensionBridge,
    BrowserExtensionBridgeError,
)


def test_bundled_extension_exposes_background_youtube_contract() -> None:
    project_root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (project_root / "donut-extension" / "manifest.json").read_text(encoding="utf-8")
    )
    worker_name = manifest["background"]["service_worker"]
    background = (project_root / "donut-extension" / worker_name).read_text(encoding="utf-8")

    assert manifest["version"] == "1.1.43"
    assert manifest["key"]
    public_key = base64.b64decode(manifest["key"])
    digest = hashlib.sha256(public_key).digest()[:16]
    extension_id = "".join(
        chr(ord("a") + (byte >> 4)) + chr(ord("a") + (byte & 15))
        for byte in digest
    )
    assert extension_id == BROWSER_EXTENSION_ID
    assert "youtube-set-text" in background
    assert "youtube-native-edit-v2" in background
    assert "youtube-tags-v1" in background
    assert "youtube-tags-commit-v2" in background
    assert "youtube-upload-completion-v3" in background
    assert 'command === "youtube_set_tags"' in background
    assert 'document.execCommand("insertText", false, value)' in background
    assert "youtube-click" in background
    assert "youtube-query" in background
    assert "youtube-query-active-v2" in background
    assert "youtube-active-controls-v2" in background
    assert "const laidOut = output.filter" in background
    assert "const renderedText = (root)" in background
    assert "node.shadowRoot" in background
    assert "selectorMatches.filter(visible).at(-1)" in background
    assert 'container.querySelectorAll("ytcp-chip")' in background
    assert "youtube-attached-video" in background
    assert "youtube-resume-interrupted-upload-v1" in background
    assert 'command === "youtube_resume_interrupted_upload"' in background
    assert "youtube-content-state-v1" in background
    assert 'command === "youtube_content_state"' in background
    assert "youtube-checkview-range-v1" in background
    assert 'command === "youtube_checkview_select_60"' in background
    assert "youtube-checkview-content-v1" in background
    assert 'command === "youtube_checkview_content_page"' in background
    assert "CHECKVIEW_CONTENT_NOT_READY" in background
    assert "CHECKVIEW_NEXT_PAGE_NOT_CONFIRMED" in background
    assert "thumbnailMatch" in background
    assert "vi(?:_[A-Za-z0-9]+)?" in background
    assert "if (!visible(control)) continue;" in background
    assert "bridge-keepalive" in background
    assert "client_ping" in background
    assert "docurandom_pair" in (
        project_root / "donut-extension" / "popup.js"
    ).read_text(encoding="utf-8")
    assert "restore_pair" in background
    assert "pairingInProgress" in background
    assert 'command === "page_echo"' not in background
    assert "params.active === true" in background


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_checkview_next_uses_two_stable_observations_and_single_click() -> None:
    worker = Path(__file__).resolve().parents[1] / "donut-extension" / "background-v1143.js"
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('async function checkviewContentPageInTab(');
const end = source.indexOf('\nasync function execute(', start);
if (start < 0 || end < 0) throw new Error('checkview function not found');
let page = 0;
let clicks = 0;
const button = {
  nodeType: 1, tagName: 'BUTTON', isConnected: true, className: '',
  get disabled() { return page !== 0; },
  getAttribute(name) { return name === 'aria-label' ? 'Next page' : null; },
  hasAttribute() { return false; },
  getClientRects() { return [1]; },
  click() { clicks += 1; page = 1; },
};
const document = {
  scrollingElement: { scrollTop: 0, scrollHeight: 0, clientHeight: 100 },
  querySelectorAll(selector) {
    if (selector === '*') {
      const videoId = page === 0 ? 'first01' : 'second02';
      return [{
        nodeType: 1, tagName: 'YTCP-VIDEO-ROW', isConnected: true,
        className: '', childNodes: [], scrollHeight: 0, clientHeight: 0,
        parentElement: null,
        getAttribute(name) { return name === 'data-video-id' ? videoId : null; },
        getRootNode() { return null; },
      }];
    }
    if (selector.includes('button')) return [button];
    return [];
  },
};
const context = {
  document, setTimeout, Date,
  Node: { ELEMENT_NODE: 1, TEXT_NODE: 3, DOCUMENT_FRAGMENT_NODE: 11 },
  getComputedStyle() { return { display: 'block', visibility: 'visible', overflowY: 'auto' }; },
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
context.checkviewContentPageInTab('next', 'first01').then((result) => {
  if (clicks !== 1 || result.transitioned !== true ||
      result.rows[0].videoId !== 'second02') {
    throw new Error(JSON.stringify({ clicks, result }));
  }
}).catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, str(worker)],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_checkview_next_empty_script_reply_is_not_reexecuted() -> None:
    worker = Path(__file__).resolve().parents[1] / "donut-extension" / "background-v1143.js"
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('async function executeInTab(');
const end = source.indexOf('\n// Return stable, ID-scoped rows', start);
if (start < 0 || end < 0) throw new Error('executeInTab function not found');
let calls = 0;
const context = {
  chrome: { scripting: { executeScript: async () => { calls += 1; return [{ result: null }]; } } },
  checkviewContentPageInTab: async () => {},
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
context.executeInTab(77, context.checkviewContentPageInTab, ['next'], 'ISOLATED')
  .then(() => { throw new Error('empty reply was accepted'); })
  .catch((error) => {
    if (calls !== 1 || !String(error).includes('SCRIPT_EXECUTION_RETURNED_NO_RESULT')) {
      console.error(error, calls);
      process.exitCode = 1;
    }
  });
"""
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, str(worker)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_bridge_rejects_non_loopback_listener() -> None:
    with pytest.raises(BrowserExtensionBridgeError):
        BrowserExtensionBridge(host="0.0.0.0", port=0)


def test_pair_store_never_persists_plaintext_secret() -> None:
    with tempfile.TemporaryDirectory(prefix="DocuRandomBrowserBridge_") as temp:
        pairs_path = Path(temp) / "pairs.json"
        bridge = BrowserExtensionBridge(port=0, pairs_path=pairs_path)
        secret = "local-secret-that-must-not-be-written"
        bridge._pairs["client_abcdefgh"] = {
            "profile_id": "profile_abcdefgh",
            "secret_hash": bridge._digest(secret),
            "paired_at": "now",
        }
        bridge._save_pairs()

        raw = pairs_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        assert secret not in raw
        assert payload["pairs"]["client_abcdefgh"]["secret_hash"] == hashlib.sha256(
            secret.encode("utf-8")
        ).hexdigest()


def test_status_is_disconnected_before_profile_extension_connects() -> None:
    with tempfile.TemporaryDirectory(prefix="DocuRandomBrowserBridge_") as temp:
        bridge = BrowserExtensionBridge(port=0, pairs_path=Path(temp) / "pairs.json")
        status = bridge.status("profile_abcdefgh")
        assert not status.connected
        assert status.profile_id == "profile_abcdefgh"


def test_share_file_serves_only_tokenized_loopback_url() -> None:
    with tempfile.TemporaryDirectory(prefix="DocuRandomBrowserBridge_") as temp:
        root = Path(temp)
        source = root / "demo video.mp4"
        source.write_bytes(b"docurandom-demo")
        bridge = BrowserExtensionBridge(
            port=0,
            file_port=0,
            pairs_path=root / "pairs.json",
        )
        try:
            url = bridge.share_file(source)
            assert url.startswith("http://127.0.0.1:")
            assert str(source) not in url
            with urllib.request.urlopen(url, timeout=3) as response:
                assert response.read() == b"docurandom-demo"
                assert response.headers["Access-Control-Allow-Origin"] == "*"
        finally:
            bridge.stop()
