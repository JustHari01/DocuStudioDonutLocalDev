import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from docurandom.chatgpt_recovery import has_reserved_dom_response_recovery, reserve_dom_response_recovery
from docurandom.content_ai import ChatTurn, ContentAIError, MasterPromptRunner
from docurandom.ui import DocuRandomApp


def test_dom_recovery_budget_survives_restart_and_waits_between_attempts(tmp_path):
    clock = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)
    kwargs = dict(workspace_id="w", thread_id="t", pending_text="BẮT ĐẦU",
                  failed_at=(clock - timedelta(hours=2)).isoformat())
    assert reserve_dom_response_recovery(tmp_path, now=clock, **kwargs) == "reserved"
    for _ in range(5):
        assert reserve_dom_response_recovery(tmp_path, now=clock, **kwargs) == "wait"
    clock += timedelta(minutes=5)
    assert reserve_dom_response_recovery(tmp_path, now=clock, **kwargs) == "reserved"
    clock += timedelta(minutes=14)
    assert reserve_dom_response_recovery(tmp_path, now=clock, **kwargs) == "wait"
    clock += timedelta(minutes=1)
    assert reserve_dom_response_recovery(tmp_path, now=clock, **kwargs) == "reserved"
    clock += timedelta(days=1)
    assert reserve_dom_response_recovery(tmp_path, now=clock, **kwargs) == "exhausted"
    saved = json.loads((tmp_path / "chatgpt-dom-recovery.json").read_text())
    assert saved["attempts"] == 3
    assert "BẮT ĐẦU" not in json.dumps(saved, ensure_ascii=False)
    assert reserve_dom_response_recovery(tmp_path, now=clock, **{**kwargs, "thread_id": "new"}) == "reserved"


def test_dom_recovery_keeps_corrupt_budget_closed(tmp_path):
    path = tmp_path / "chatgpt-dom-recovery.json"
    path.write_text("malformed")
    assert reserve_dom_response_recovery(tmp_path, workspace_id="w", thread_id="t",
                                         pending_text="p", failed_at="2000-01-01T00:00:00+00:00") == "blocked"
    assert path.read_text() == "malformed"


def test_submitted_only_guard_persists_for_exact_turn_after_transport_failure(tmp_path):
    assert reserve_dom_response_recovery(
        tmp_path, workspace_id="w", thread_id="t", pending_text="prompt",
        failed_at="2000-01-01T00:00:00+00:00",
    ) == "reserved"
    assert has_reserved_dom_response_recovery(tmp_path, workspace_id="w", thread_id="t", pending_text="prompt")
    assert not has_reserved_dom_response_recovery(tmp_path, workspace_id="w", thread_id="t", pending_text="TIẾP")
    assert not has_reserved_dom_response_recovery(tmp_path, workspace_id="other", thread_id="t", pending_text="prompt")


def test_failed_response_probe_cannot_switch_to_generic_resend_timer():
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.content_snapshot = {"recover_submitted_only": True}
    app._require_manual_pipeline_resume = Mock()
    app.root = Mock()
    assert app._schedule_pipeline_auto_resume("chatgpt", "transport disconnected") is False
    app._require_manual_pipeline_resume.assert_called_once()
    app.root.after.assert_not_called()


@pytest.mark.parametrize("result", [None, {"status": "not-submitted"},
                                     {"status": "not-submitted", "safe_to_resend": True}])
def test_submitted_only_recovery_never_resends_or_writes_a_fake_answer(tmp_path, result):
    chat = SimpleNamespace(thread_id="old-thread", response_id="", prompts=[],
                           recover_pending=Mock(return_value=result), send=Mock())
    seed = MasterPromptRunner(chat, output_dir=tmp_path, project_name="video", minutes=1)
    seed.master_prompt_sha256 = hashlib.sha256(b"MASTER").hexdigest()
    seed.turns = [ChatTurn("user", "MASTER"), ChatTurn("assistant", "ĐÃ NẠP"),
                  ChatTurn("user", "BẮT ĐẦU")]
    seed._save_checkpoint()
    before = seed.checkpoint_path.read_bytes()
    runner = MasterPromptRunner(chat, output_dir=tmp_path, project_name="video", minutes=1,
                                recover_submitted_only=True)
    with pytest.raises(ContentAIError, match="không gửi lại prompt"):
        runner.run("MASTER")
    chat.send.assert_not_called()
    chat.recover_pending.assert_called_once()
    assert seed.checkpoint_path.read_bytes() == before


def test_submitted_only_recovery_cannot_create_new_conversation_without_checkpoint(tmp_path):
    chat = SimpleNamespace(thread_id="old-thread", response_id="", send=Mock())
    runner = MasterPromptRunner(chat, output_dir=tmp_path, project_name="video", minutes=1,
                                recover_submitted_only=True)
    with pytest.raises(ContentAIError, match="không gửi lại prompt"):
        runner.run("MASTER")
    chat.send.assert_not_called()
    assert not runner.checkpoint_path.exists()


def dom_app(tmp_path):
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.workspace = SimpleNamespace(state_dir=tmp_path, workspace_id="workspace-10")
    snapshot = {"project_name": "video", "minutes": 1, "master_prompt": "MASTER",
                "workspace_id": "workspace-10", "output_dir": tmp_path}
    app._content_input_snapshot = Mock(return_value=snapshot)
    app._pipeline_checkpoint_path = Mock(return_value=tmp_path / "pipeline.json")
    app._append_content_chat = Mock()
    app._schedule_log_once = Mock()
    app.closed = app.closing_after_cancel = False
    identity = {"workspace_id": "workspace-10", "project_name": "video", "minutes": 1,
                "master_prompt_sha256": hashlib.sha256(b"MASTER").hexdigest()}
    payload = {"project_name": "video", "minutes": 1,
               "master_prompt_sha256": identity["master_prompt_sha256"],
               "thread_id": "saved-thread", "turns": [{"role": "user", "text": "BẮT ĐẦU"}]}
    (tmp_path / "video_AI_CHECKPOINT.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "pipeline.json").write_text(json.dumps({"identity": identity, "stages": {
        "chatgpt": {"status": "error", "detail": "ChatGPT không tạo DOM phản hồi",
                    "updated_at": "2026-01-01T00:00:00+00:00"}}}), encoding="utf-8")
    return app


def test_scheduler_reserves_only_one_submitted_response_recovery(tmp_path):
    app = dom_app(tmp_path)
    assert app._has_safe_pending_chatgpt_turn()
    assert app.content_dom_recovery_only is True
    assert not app._has_safe_pending_chatgpt_turn()
    assert json.loads((tmp_path / "chatgpt-dom-recovery.json").read_text())["attempts"] == 1


@pytest.mark.parametrize("detail", [
    "CHATGPT_CONVERSATION_LOAD_FAILED: giữ checkpoint, không gửi lại prompt",
    "CHATGPT_RECOVERY_PENDING: vẫn chưa đọc được phản hồi",
    "Không có bằng chứng phản hồi của đúng lượt đã gửi; giữ checkpoint, không gửi lại prompt.",
])
def test_transient_load_failures_use_only_the_existing_readonly_budget(tmp_path, detail):
    app = dom_app(tmp_path)
    path = tmp_path / "pipeline.json"
    payload = json.loads(path.read_text())
    payload["stages"]["chatgpt"]["detail"] = detail
    path.write_text(json.dumps(payload))
    assert app._has_safe_pending_chatgpt_turn()
    assert app.content_dom_recovery_only is True
    assert not app._has_safe_pending_chatgpt_turn()
    app.content_snapshot = {"recover_submitted_only": True}
    app._require_manual_pipeline_resume = Mock()
    app.root = Mock()
    assert not app._schedule_pipeline_auto_resume("chatgpt", detail)
    app.root.after.assert_not_called()
    lease = json.loads((tmp_path / "chatgpt-dom-recovery.json").read_text())
    lease["attempts"] = 5
    (tmp_path / "chatgpt-dom-recovery.json").write_text(json.dumps(lease))
    assert not app._has_safe_pending_chatgpt_turn()
    assert json.loads((tmp_path / "chatgpt-dom-recovery.json").read_text())["attempts"] == 5


@pytest.mark.parametrize("change", ["foreign-workspace", "uncertain", "busy"])
def test_scheduler_does_not_unlock_foreign_uncertain_or_busy_turn(tmp_path, change):
    app = dom_app(tmp_path)
    if change == "busy":
        app.content_busy = True
    elif change == "uncertain":
        path = tmp_path / "video_AI_CHECKPOINT.json"
        payload = json.loads(path.read_text())
        payload["submission_uncertain"] = True
        path.write_text(json.dumps(payload))
    else:
        path = tmp_path / "pipeline.json"
        payload = json.loads(path.read_text())
        payload["identity"]["workspace_id"] = "other"
        path.write_text(json.dumps(payload))
    assert not app._has_safe_pending_chatgpt_turn()
    assert not (tmp_path / "chatgpt-dom-recovery.json").exists()
