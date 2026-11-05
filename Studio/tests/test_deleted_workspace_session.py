from unittest.mock import Mock

import pytest

from docurandom.ui import DocuRandomApp, _retire_deleted_workspace_session
from docurandom.workspace import WorkspaceStore


def idle_app():
    app = Mock(closed=False, scanning=False, audio_preflight_active=False, voicevox_audit_busy=False)
    app.has_update_blocking_tasks.return_value = False
    return app


def test_delete_retires_only_target_session_and_no_shared_resources():
    target, sibling = idle_app(), idle_app()
    sessions = {"delete": target, "keep": sibling}

    assert _retire_deleted_workspace_session(sessions, "delete") is target

    assert sessions == {"keep": sibling}
    target.begin_managed_shutdown.assert_called_once_with()
    target.finalize_managed_shutdown.assert_called_once_with()
    target.root_frame.destroy.assert_called_once_with()
    assert sibling.mock_calls == []
    assert _retire_deleted_workspace_session(sessions, "delete") is None


@pytest.mark.parametrize("busy_kind", ["worker", "scan", "audio", "audit"])
def test_worker_started_during_confirmation_cannot_be_deleted(busy_kind):
    app = idle_app()
    if busy_kind == "worker":
        app.has_update_blocking_tasks.return_value = True
    else:
        setattr(app, {"scan": "scanning", "audio": "audio_preflight_active", "audit": "voicevox_audit_busy"}[busy_kind], True)
    sessions = {"busy": app}

    with pytest.raises(ValueError, match="Workspace đang có tác vụ"):
        _retire_deleted_workspace_session(sessions, "busy")

    assert sessions["busy"] is app
    app.begin_managed_shutdown.assert_not_called()
    app.root_frame.destroy.assert_not_called()


def test_stale_config_callback_cannot_create_any_deleted_workspace_files(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "workspaces")
    workspace = store.create("1")
    workspace.root.rename(tmp_path / "recycle-copy")
    store.unregister(workspace.workspace_id)
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.closed, app.workspace, app.workspace_store = False, workspace, store
    app._persist_master_prompt_draft = Mock()
    before = store.registry_path.read_bytes()

    app._save_config()

    app._persist_master_prompt_draft.assert_not_called()
    assert not workspace.root.exists()
    assert store.registry_path.read_bytes() == before


def test_closed_session_never_saves_even_without_workspace():
    app = DocuRandomApp.__new__(DocuRandomApp)
    app.closed = True
    app._persist_master_prompt_draft = Mock()
    app._save_config()
    app._persist_master_prompt_draft.assert_not_called()
