import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from docurandom.content_ai import ChatGPTWebStatus
from docurandom.thumbnail_ai import claim_thumbnail_ai_generation, update_thumbnail_ai_state
from docurandom.ui import DocuRandomApp
from docurandom.workspace import Workspace


def ready_status(*, busy=False):
    return ChatGPTWebStatus(
        launcher_path=Path("bridge.exe"), installed=True, launcher_running=True,
        setup_complete=True, authenticated=True, runtime_ready=True,
        session_busy=busy, endpoint="http://127.0.0.1:17841",
        available_models=("chatgpt-web/auto",), access_token="test-bridge-token",
    )


def waiting_app(tmp_path, submitted):
    workspace = Workspace("startup-thumbnail", "DaiTa15", str(tmp_path / "workspace"), "", "")
    workspace.ensure_layout()
    package = workspace.output_dir / "Video.txt"
    package.write_text("TIÊU ĐỀ\nTitle\n\nPROMPT THUMBNAIL\nCreate a factory thumbnail.\n", encoding="utf-8")
    claim = claim_thumbnail_ai_generation(workspace, package)
    update_thumbnail_ai_state(workspace, package, claim, status="submitted" if submitted else "error", submitted=submitted,
                              thread_id="exact-old-thread", response_id="exact-old-response")
    state_path=workspace.state_dir/"thumbnail-ai.json";raw=json.loads(state_path.read_text());raw["isolated_chat"]={"policy_version":1,"prompt_sha256":claim.prompt_sha256,"content_thread_id":"content-thread"};state_path.write_text(json.dumps(raw))
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.workspace = workspace
    app.closed = app.closing_after_cancel = app.thumbnail_ai_busy = False
    app.youtube_thumbnail_auto_ai_var = Mock()
    app.youtube_thumbnail_auto_ai_var.get.return_value = True
    app.content_cli_status = None
    app.codex_checking = True
    app._effective_youtube_thumbnail = Mock(return_value=None)
    app._schedule_thumbnail_ai_retry = Mock()
    app._check_codex_async = Mock()
    app._notify_remote = Mock()
    app._append_log = Mock()
    return app, package


@pytest.mark.parametrize("submitted", [False, True])
@pytest.mark.parametrize("checking", [False, True])
def test_startup_wait_does_not_claim_rewrite_or_warn(tmp_path, submitted, checking):
    app, package = waiting_app(tmp_path, submitted)
    app.codex_checking = checking
    state = app.workspace.state_dir / "thumbnail-ai.json"
    before = state.read_bytes() if state.exists() else None
    with patch("docurandom.ui.claim_thumbnail_ai_generation") as claim, \
         patch("docurandom.ui.update_thumbnail_ai_state") as update, \
         patch("docurandom.ui.threading.Thread") as thread:
        app._start_chatgpt_thumbnail_generation(package)
    claim.assert_not_called()
    update.assert_not_called()
    thread.assert_not_called()
    app._notify_remote.assert_not_called()
    assert (state.read_bytes() if state.exists() else None) == before
    assert app.thumbnail_ai_waiting_for_bridge_package == package.resolve()
    app._schedule_thumbnail_ai_retry.assert_called_once()
    assert app._check_codex_async.call_count == (0 if checking else 1)


@pytest.mark.parametrize("busy", [False, True])
def test_completed_precheck_resumes_waiting_thumbnail_once(tmp_path, busy):
    app, package = waiting_app(tmp_path, True)
    app.thumbnail_ai_waiting_for_bridge_package = package
    app.codex_check_token = 7
    app.content_precheck_pending = False
    app.root = Mock()
    app._apply_codex_status_ui = Mock()
    app._refresh_content_controls = Mock()
    app._cancel_thumbnail_ai_retry = Mock()
    app._start_chatgpt_thumbnail_generation = Mock()
    app._handle_codex_checked(7, "startup", None, 0, ready_status(busy=busy))
    app._cancel_thumbnail_ai_retry.assert_called_once_with()
    assert app.thumbnail_ai_waiting_for_bridge_package is None
    callback = app.root.after_idle.call_args.args[0]
    callback()
    app._start_chatgpt_thumbnail_generation.assert_called_once_with(package)
    app.root.after_idle.reset_mock()
    app._handle_codex_checked(7, "startup", None, 0, ready_status(busy=busy))
    app.root.after_idle.assert_not_called()


@pytest.mark.parametrize('action',['start_checked','recover_new_chat'])
def test_verified_operator_retry_reaches_the_real_worker_setup(tmp_path,action):
    from docurandom.thumbnail_ai import ThumbnailAIClaim
    app, package = waiting_app(tmp_path, False)
    app.content_cli_status = ready_status()
    app.content_snapshot = {}
    class ReachedWorkerSetup(Exception):
        pass
    with patch("docurandom.ui.claim_thumbnail_ai_generation", return_value=ThumbnailAIClaim(action, "prompt", "sha", "package")), \
         patch("docurandom.ui.resolve_chatgpt_web_model", side_effect=ReachedWorkerSetup), \
         pytest.raises(ReachedWorkerSetup):
        app._start_chatgpt_thumbnail_generation(package)
    app._notify_remote.assert_not_called()


def test_failed_precheck_keeps_wait_without_rewriting_checkpoint(tmp_path):
    app, package = waiting_app(tmp_path, True)
    app.thumbnail_ai_waiting_for_bridge_package = package
    app.codex_check_token = 7
    app.content_precheck_pending = False
    app.root = Mock()
    app._apply_codex_status_ui = Mock()
    app._refresh_content_controls = Mock()
    app._cancel_thumbnail_ai_retry = Mock()
    app._handle_codex_checked(7, "startup", None, 0, ChatGPTWebStatus(launcher_path=None))
    app.root.after_idle.assert_not_called()
    app._cancel_thumbnail_ai_retry.assert_not_called()
    assert app.thumbnail_ai_waiting_for_bridge_package == package


def test_recovering_checkpoint_cannot_be_reclaimed_as_unsent_after_restart(tmp_path):
    app, package = waiting_app(tmp_path, True)
    claim = claim_thumbnail_ai_generation(app.workspace, package)
    update_thumbnail_ai_state(app.workspace, package, claim, status="recovering", submitted=True,
                              thread_id="exact-old-thread", response_id="exact-old-response")
    state = app.workspace.state_dir / "thumbnail-ai.json"
    before = state.read_bytes()
    assert claim_thumbnail_ai_generation(app.workspace, package).action == "blocked"
    assert state.read_bytes() == before
    app.content_cli_status = ready_status()
    app._start_chatgpt_thumbnail_generation(package)
    app._notify_remote.assert_not_called()
    app._schedule_thumbnail_ai_retry.assert_called_once()
    assert state.read_bytes() == before


def test_bridge_ready_callback_rearms_negative_thumbnail_cooldown_without_sending(tmp_path):
    app, package = waiting_app(tmp_path, False)
    claim = claim_thumbnail_ai_generation(app.workspace, package)
    update_thumbnail_ai_state(app.workspace, package, claim, status="terminal_no_image", submitted=False,
                              thread_id="exact-old-thread", automatic_retry_count=1,
                              detail="CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: công cụ chưa tạo ảnh")
    state = app.workspace.state_dir / "thumbnail-ai.json"
    before = state.read_bytes()
    app.thumbnail_ai_waiting_for_bridge_package = package
    app.codex_check_token = 7
    app.content_precheck_pending = False
    app.root = Mock()
    app._apply_codex_status_ui = Mock()
    app._refresh_content_controls = Mock()
    app._cancel_thumbnail_ai_retry = Mock()
    with patch("docurandom.ui.threading.Thread") as thread:
        app._handle_codex_checked(7, "startup", None, 0, ready_status())
        callback = app.root.after_idle.call_args.args[0]
        # The production status UI normally stores the freshly checked status.
        app.content_cli_status = ready_status()
        callback()
    app._cancel_thumbnail_ai_retry.assert_called_once()
    app._schedule_thumbnail_ai_retry.assert_called_once()
    assert 50 <= app._schedule_thumbnail_ai_retry.call_args.kwargs["delay_seconds"] <= 60
    app._notify_remote.assert_not_called()
    thread.assert_not_called()
    assert state.read_bytes() == before


@pytest.mark.parametrize("count,detail", [
    (3, "CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: công cụ chưa tạo ảnh"),
    (1, "CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: đã đạt giới hạn quota"),
])
def test_negative_retry_does_not_rearm_when_budget_or_provider_limit_blocks_it(tmp_path, count, detail):
    app, package = waiting_app(tmp_path, False)
    claim = claim_thumbnail_ai_generation(app.workspace, package)
    update_thumbnail_ai_state(app.workspace, package, claim, status="terminal_no_image", submitted=False,
                              thread_id="exact-old-thread", automatic_retry_count=count, detail=detail)
    state = app.workspace.state_dir / "thumbnail-ai.json"
    before = state.read_bytes()
    app.content_cli_status = ready_status()
    app.youtube_thumbnail_status_var = Mock()
    app._maybe_start_deferred_pipeline_voice = Mock()
    with patch("docurandom.ui.threading.Thread") as thread:
        app._start_chatgpt_thumbnail_generation(package)
    if "quota" in detail:
        app._schedule_thumbnail_ai_retry.assert_not_called()
    else:
        app._schedule_thumbnail_ai_retry.assert_called_once()
    thread.assert_not_called()
    assert state.read_bytes() == before
