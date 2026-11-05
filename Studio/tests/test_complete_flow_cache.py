import hashlib
import json
import threading
from unittest.mock import Mock
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from docurandom.flow_agent import FlowAgentManager, FlowAgentError, FlowAgentCancelled, FLOW_REQUIRED_IMAGE_MODEL, load_complete_flow_images


def batch(tmp_path):
    prompts = ["exact first prompt", "exact second prompt"]
    path = tmp_path / "flow.json"
    entries = {}
    for i, p in enumerate(prompts, 1):
        Image.new("RGB", (16, 9), color=(i*50, 0, 0)).save(tmp_path / f"{i}.png")
        entries[str(i)] = {"filename": f"{i}.png", "prompt_sha256": hashlib.sha256(p.encode()).hexdigest(), "media_id": f"saved-{i}"}
    saved = {"status": "complete", "workspace_id": "exact-workspace", "total": 2, "completed": 2, "submitted": 2,
             "signature": hashlib.sha256(json.dumps({"model": FLOW_REQUIRED_IMAGE_MODEL, "aspect": "landscape", "prompts": tuple(prompts)}, ensure_ascii=False, sort_keys=True).encode()).hexdigest(), "entries": entries}
    path.write_text(json.dumps(saved), encoding="utf-8")
    return prompts, path, saved


def test_complete_batch_resumes_with_flow_offline_and_leaves_checkpoint_unchanged(tmp_path):
    prompts, path, _ = batch(tmp_path)
    before = path.read_bytes()
    manager = FlowAgentManager()
    manager.check = Mock(side_effect=AssertionError("A complete local batch must not require Edge or Flow"))
    result = manager.generate_images(prompts, tmp_path, checkpoint_path=path, workspace_id="exact-workspace")
    assert [r.media_id for r in result] == ["saved-1", "saved-2"]
    manager.check.assert_not_called()
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["missing-image", "damaged-image", "changed-prompt", "wrong-prompt-hash", "partial", "outside-path", "missing-count"])
def test_partial_or_changed_batch_never_claims_complete(tmp_path, change):
    prompts, path, saved = batch(tmp_path)
    if change == "missing-image": (tmp_path / "2.png").unlink()
    elif change == "damaged-image": (tmp_path / "2.png").write_bytes(b"damaged image")
    elif change == "changed-prompt": prompts[1] = "different prompt"
    elif change == "wrong-prompt-hash": saved["entries"]["2"]["prompt_sha256"] = "wrong"
    elif change == "partial": saved["status"] = "incomplete"
    elif change == "outside-path": saved["entries"]["2"]["filename"] = "../2.png"
    else: saved.pop("submitted")
    path.write_text(json.dumps(saved), encoding="utf-8")
    before = path.read_bytes()
    assert load_complete_flow_images(prompts, tmp_path, checkpoint_path=path, workspace_id="exact-workspace") is None
    assert path.read_bytes() == before


def test_foreign_workspace_and_cancelled_request_cannot_complete_from_cache(tmp_path):
    prompts, path, _ = batch(tmp_path)
    with pytest.raises(FlowAgentError, match="không khớp workspace"):
        load_complete_flow_images(prompts, tmp_path, checkpoint_path=path, workspace_id="foreign-workspace")
    cancel = threading.Event(); cancel.set()
    with pytest.raises(FlowAgentCancelled):
        load_complete_flow_images(prompts, tmp_path, checkpoint_path=path, cancel_event=cancel)


def test_workspace_worker_bypasses_live_flow_queue_for_verified_local_batch(tmp_path):
    from docurandom.ui import DocuRandomApp
    prompts, path, _ = batch(tmp_path)
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.workspace = SimpleNamespace(media_dir=tmp_path, flow_history_path=path, workspace_id="exact-workspace")
    app.flow_generation_busy = False
    app.content_token = 7
    app.events = Queue()
    app.flow_coordinator = Mock()
    app.flow_coordinator.execute.side_effect = AssertionError("Must bypass the live Flow coordinator")
    app.flow_manager = Mock()
    for name in ("_save_config", "_set_content_progress", "_refresh_flow_controls", "_refresh_content_controls", "_append_exception_log"):
        setattr(app, name, Mock())
    app.flow_progress_var = Mock(); app.flow_percent_var = Mock()
    class InlineThread:
        def __init__(self, target, **_kwargs): self.target = target
        def start(self): self.target()
    with patch("docurandom.ui.threading.Thread", InlineThread):
        app._start_flow_image_generation(SimpleNamespace(artifacts=SimpleNamespace(image_prompts=prompts)))
    app.flow_coordinator.execute.assert_not_called()
    app.flow_manager.check.assert_not_called()
    app._append_exception_log.assert_not_called()
    assert app.events.get_nowait()[0] == "flow_generation_progress"
    done = app.events.get_nowait()
    assert done[0] == "flow_generation_done"
    assert len(done[2]) == 2
    assert app.events.empty()
