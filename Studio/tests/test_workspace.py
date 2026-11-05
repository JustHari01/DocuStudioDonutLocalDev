import json
from pathlib import Path
from unittest import mock

import pytest

from docurandom import __version__
from docurandom.workspace import (
    WorkspaceStore,
    resolve_workspace_selectors,
    resolve_workspace_selector_report,
    workspace_slug,
)


def test_workspace_slug_is_windows_safe():
    assert workspace_slug(" Tin tức Nhật Bản 2026! ") == "tin-tuc-nhat-ban-2026"
    assert workspace_slug("***") == "workspace"


def test_create_workspace_builds_isolated_layout(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    first = store.create("Tin Nhật")
    second = store.create("Tin Nhật")

    assert first.workspace_id != second.workspace_id
    assert first.root != second.root
    for workspace in (first, second):
        assert workspace.media_dir.is_dir()
        assert workspace.output_dir.is_dir()
        assert workspace.prompt_dir.is_dir()
        assert workspace.voicevox_dir.is_dir()
        settings = json.loads(workspace.settings_path.read_text(encoding="utf-8"))
        assert settings["source_dir"] == str(workspace.media_dir)
        assert settings["output_dir"] == str(workspace.output_dir)
        assert settings["flow_model"] == "harbor_seal"
        assert settings["content_dedup_enabled"] is True
        assert settings["last_started_app_version"] == __version__
        assert workspace.content_history_path.parent == workspace.state_dir
        assert workspace.content_history_text_path.parent == workspace.root
        assert workspace.content_history_path.is_file()
        assert workspace.content_history_text_path.is_file()
        assert "CHECKLIST NỘI DUNG" in workspace.content_history_text_path.read_text(
            encoding="utf-8-sig"
        )


def test_create_workspace_never_reuses_unregistered_existing_folder(tmp_path):
    parent = tmp_path / "projects"
    occupied = parent / "tin-nhat"
    occupied.mkdir(parents=True)
    (occupied / "user-file.txt").write_text("keep", encoding="utf-8")
    store = WorkspaceStore(tmp_path / "registry.json", parent)

    workspace = store.create("Tin Nhật")

    assert workspace.root == parent / "tin-nhat-2"
    assert (occupied / "user-file.txt").read_text(encoding="utf-8") == "keep"


def test_workspace_selectors_accept_multiple_numbers_names_and_all(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    first = store.create("Tin Nhật")
    second = store.create("Máy công nghiệp")
    third = store.create("Ô tô")
    workspaces = store.list()

    assert resolve_workspace_selectors(workspaces, "1 2 3") == tuple(workspaces)
    assert resolve_workspace_selectors(workspaces, "1, 3") == (
        workspaces[0],
        workspaces[2],
    )
    assert resolve_workspace_selectors(workspaces, second.name) == (second,)
    assert resolve_workspace_selectors(workspaces, "all") == tuple(workspaces)
    assert resolve_workspace_selectors(workspaces, "1 99") == ()

    found, missing = resolve_workspace_selector_report(workspaces, "1 99 3")
    assert found == (workspaces[0], workspaces[2])
    assert missing == ("99",)

    found, missing = resolve_workspace_selector_report(workspaces, "")
    assert found == ()
    assert missing == ("(chưa ghi workspace)",)


def test_workspace_touch_and_last_opened(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    first = store.create("One")
    second = store.create("Two")
    touched = store.touch(first)

    assert touched.updated_at >= first.updated_at
    assert store.last_opened().workspace_id == first.workspace_id
    assert {item.workspace_id for item in store.list()} == {
        first.workspace_id,
        second.workspace_id,
    }


def test_default_store_merges_previous_managed_workspaces_and_keeps_current_state(
    tmp_path
):
    target_data = tmp_path / "current" / "Data"
    current_root = tmp_path / "current" / "Workspaces"
    previous_data = tmp_path / "previous" / "Data"
    previous_root = tmp_path / "Videos" / "old-project"
    previous_root.mkdir(parents=True)
    (previous_root / "WORKSPACE.txt").write_text("legacy project", encoding="utf-8")

    current_store = WorkspaceStore(target_data / "workspaces.json", current_root)
    active = [current_store.create(f"DaiTa{index}") for index in range(1, 4)]
    current_store.touch(active[-1])

    previous_data.mkdir(parents=True)
    previous_entry = {
        "workspace_id": "legacy-project-id",
        "name": "Dự án hiện tại",
        "root_dir": str(previous_root),
        "created_at": "2026-08-21T12:54:31+00:00",
        "updated_at": "2026-08-21T12:56:51+00:00",
        "category": "Video YouTube",
    }
    (previous_data / "workspaces.json").write_text(
        json.dumps(
            {
                "version": 1,
                "last_opened": "legacy-project-id",
                "workspaces": [previous_entry],
            }
        ),
        encoding="utf-8",
    )

    with (
        mock.patch("docurandom.workspace.app_data_dir", return_value=target_data),
        mock.patch("docurandom.workspace.default_workspaces_dir", return_value=current_root),
        mock.patch(
            "docurandom.workspace.previous_managed_data_dir",
            return_value=previous_data,
        ),
    ):
        restored_store = WorkspaceStore()

    restored = restored_store.list()
    assert {item.name for item in restored} == {
        "DaiTa1",
        "DaiTa2",
        "DaiTa3",
        "Dự án hiện tại",
    }
    assert restored_store.last_opened().workspace_id == active[-1].workspace_id
    assert next(item for item in restored if item.name == "Dự án hiện tại").root == previous_root.resolve()
    assert previous_root.joinpath("WORKSPACE.txt").read_text(encoding="utf-8") == "legacy project"

    backup = json.loads((target_data / "workspaces.backup.json").read_text(encoding="utf-8"))
    snapshot = json.loads(
        (target_data / "workspaces.before-legacy-import.json").read_text(encoding="utf-8")
    )
    assert len(backup["workspaces"]) == 4
    assert len(snapshot["workspaces"]) == 3
    assert (
        target_data / ".previous-managed-workspaces-imported"
    ).read_text(encoding="ascii") == "Imported 1 previous managed workspace(s).\n"

    with (
        mock.patch("docurandom.workspace.app_data_dir", return_value=target_data),
        mock.patch("docurandom.workspace.default_workspaces_dir", return_value=current_root),
        mock.patch(
            "docurandom.workspace.previous_managed_data_dir",
            return_value=previous_data,
        ),
    ):
        second_start = WorkspaceStore()
    assert len(second_start.list()) == 4


def test_rename_changes_display_name_but_keeps_stable_folder_and_settings(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    workspace = store.create("Tin Nhật")
    original_root = workspace.root

    renamed = store.rename(workspace.workspace_id, "  Tin Nhật Bản 2026  ")

    assert renamed.name == "Tin Nhật Bản 2026"
    assert renamed.root == original_root
    assert renamed.settings_path.is_file()
    assert store.get(workspace.workspace_id).name == "Tin Nhật Bản 2026"


def test_unregister_updates_last_opened_and_never_deletes_files(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    first = store.create("One")
    second = store.create("Two")
    marker = second.root / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    removed = store.unregister(second.workspace_id)

    assert removed.workspace_id == second.workspace_id
    assert store.get(second.workspace_id) is None
    assert store.last_opened().workspace_id == first.workspace_id
    assert marker.read_text(encoding="utf-8") == "keep"


def test_stale_touch_cannot_restore_deleted_workspace_or_recreate_folder(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    workspace = store.create("1")
    workspace.root.rename(tmp_path / "recycle-copy")
    store.unregister(workspace.workspace_id)
    before = store.registry_path.read_bytes(), store.backup_path.read_bytes()

    with pytest.raises(ValueError, match="Workspace không còn tồn tại"):
        store.touch(workspace)

    assert not workspace.root.exists()
    assert store.get(workspace.workspace_id) is None
    assert (store.registry_path.read_bytes(), store.backup_path.read_bytes()) == before


def test_touch_from_old_session_preserves_registry_rename_and_folder(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    workspace = store.create("Old")
    folder = store.create_folder("Topic")
    store.rename(workspace.workspace_id, "New")
    store.assign_to_folder([workspace.workspace_id], folder.folder_id)

    touched = store.touch(workspace)

    assert touched.name == "New" and touched.folder_id == folder.folder_id
    assert json.loads(touched.metadata_path.read_text(encoding="utf-8"))["name"] == "New"


def test_restart_does_not_recover_explicitly_removed_workspace(tmp_path):
    registry, projects = tmp_path / "registry.json", tmp_path / "projects"
    store = WorkspaceStore(registry, projects)
    workspace = store.create("1")
    store.unregister(workspace.workspace_id)

    restarted = WorkspaceStore(registry, projects)
    assert restarted.recover_unregistered() == 0
    assert restarted.list() == []
    assert workspace.root.exists()
    # A leftover folder with missing metadata must not gain a new identity.
    workspace.metadata_path.unlink()
    assert restarted.recover_unregistered() == 0


def test_deleted_workspace_in_old_backup_does_not_override_intentional_empty_list(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    workspace = store.create("1")
    old_backup = store.backup_path.read_bytes()
    store.unregister(workspace.workspace_id)
    store.backup_path.write_bytes(old_backup)

    assert store.list() == []


def test_legacy_import_does_not_restore_explicitly_removed_workspace(tmp_path):
    old_data = tmp_path / "old-data"
    old_data.mkdir()
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    workspace = store.create("1")
    (old_data / "workspaces.json").write_bytes(store.registry_path.read_bytes())
    store.unregister(workspace.workspace_id)
    with mock.patch("docurandom.workspace.previous_managed_data_dir", return_value=old_data):
        assert store.import_previous_managed_workspaces() == 0
    assert store.get(workspace.workspace_id) is None


def test_user_can_create_new_workspace_with_same_name_after_delete(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    old = store.create("1")
    old.root.rename(tmp_path / "recycle-copy")
    store.unregister(old.workspace_id)

    new = store.create("1")

    assert new.workspace_id != old.workspace_id
    assert store.list() == [new]
    assert store.touch(new).workspace_id == new.workspace_id


def test_topic_folders_group_multiple_workspaces_without_moving_files(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    first = store.create("Ô tô Nhật")
    second = store.create("Máy công nghiệp")
    roots = {first.workspace_id: first.root, second.workspace_id: second.root}
    folder = store.create_folder("Công nghiệp Nhật Bản")

    grouped = store.assign_to_folder(
        {first.workspace_id, second.workspace_id},
        folder.folder_id,
    )

    assert {item.workspace_id for item in grouped} == {
        first.workspace_id,
        second.workspace_id,
    }
    assert all(item.folder_id == folder.folder_id for item in grouped)
    assert store.get(first.workspace_id).root == roots[first.workspace_id]
    assert store.get(second.workspace_id).root == roots[second.workspace_id]
    assert first.metadata_path.is_file()
    metadata = json.loads(first.metadata_path.read_text(encoding="utf-8"))
    assert metadata["folder_id"] == folder.folder_id


def test_delete_topic_folder_only_ungroups_and_keeps_workspace_data(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    workspace = store.create("Tin Nhật")
    marker = workspace.media_dir / "keep.jpg"
    marker.write_bytes(b"image")
    folder = store.create_folder("Tin tức")
    store.assign_to_folder([workspace.workspace_id], folder.folder_id)

    removed = store.delete_folder(folder.folder_id)

    assert removed.name == "Tin tức"
    assert store.list_folders() == []
    assert store.get(workspace.workspace_id).folder_id == ""
    assert marker.read_bytes() == b"image"


def test_topic_folder_names_are_unique_and_can_be_renamed(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    folder = store.create_folder("Ô tô")

    renamed = store.rename_folder(folder.folder_id, "Công nghiệp")

    assert renamed.name == "Công nghiệp"
    assert store.list_folders()[0].name == "Công nghiệp"
    try:
        store.create_folder("  công NGHIỆP ")
    except ValueError as exc:
        assert "Đã có" in str(exc)
    else:
        raise AssertionError("Duplicate topic folder should be rejected")


def test_nested_topic_folders_keep_breadcrumbs_and_allow_names_in_other_branches(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    japan = store.create_folder("Nhật Bản")
    news = store.create_folder("Tin tức", japan.folder_id)
    other = store.create_folder("Quốc tế")
    other_news = store.create_folder("Tin tức", other.folder_id)

    assert news.parent_id == japan.folder_id
    assert other_news.parent_id == other.folder_id
    assert [item.name for item in store.folder_path(news.folder_id)] == [
        "Nhật Bản",
        "Tin tức",
    ]
    assert store.folder_descendant_ids(japan.folder_id) == {news.folder_id}

    try:
        store.create_folder("  TIN TỨC ", japan.folder_id)
    except ValueError as exc:
        assert "cùng cấp" in str(exc)
    else:
        raise AssertionError("Duplicate sibling folder should be rejected")


def test_move_topic_folder_rejects_cycles(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    parent = store.create_folder("Cha")
    child = store.create_folder("Con", parent.folder_id)
    leaf = store.create_folder("Cháu", child.folder_id)

    moved = store.move_folder(leaf.folder_id, parent.folder_id)
    assert moved.parent_id == parent.folder_id

    for invalid_parent in (parent.folder_id, child.folder_id):
        try:
            store.move_folder(parent.folder_id, invalid_parent)
        except ValueError as exc:
            assert "chính nó" in str(exc)
        else:
            raise AssertionError("Folder cycle should be rejected")


def test_delete_nested_folder_reparents_children_and_direct_workspaces(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    workspace = store.create("Tin Nhật")
    marker = workspace.output_dir / "keep.mp4"
    marker.write_bytes(b"video")
    parent = store.create_folder("Nhật Bản")
    branch = store.create_folder("Tin tức", parent.folder_id)
    child = store.create_folder("Kinh tế", branch.folder_id)
    store.assign_to_folder([workspace.workspace_id], branch.folder_id)

    store.delete_folder(branch.folder_id)

    folders = {item.folder_id: item for item in store.list_folders()}
    assert folders[child.folder_id].parent_id == parent.folder_id
    assert store.get(workspace.workspace_id).folder_id == parent.folder_id
    assert marker.read_bytes() == b"video"


def test_delete_nested_folder_keeps_reparented_sibling_names_unique(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    parent = store.create_folder("Nhật Bản")
    branch = store.create_folder("Tin tức", parent.folder_id)
    existing = store.create_folder("Kinh tế", parent.folder_id)
    child = store.create_folder("Kinh tế", branch.folder_id)

    store.delete_folder(branch.folder_id)

    folders = {item.folder_id: item for item in store.list_folders()}
    assert folders[existing.folder_id].name == "Kinh tế"
    assert folders[child.folder_id].name == "Kinh tế (2)"
    assert folders[child.folder_id].parent_id == parent.folder_id


def test_legacy_flat_folder_defaults_to_root_level(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "last_opened": "",
                "folders": [
                    {
                        "folder_id": "legacy-folder",
                        "name": "Dữ liệu cũ",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
                "workspaces": [],
            }
        ),
        encoding="utf-8",
    )

    folders = WorkspaceStore(registry, tmp_path / "projects").list_folders()

    assert len(folders) == 1
    assert folders[0].parent_id == ""


def test_migrate_legacy_preserves_preferences_but_uses_workspace_paths(tmp_path):
    legacy = tmp_path / "settings.json"
    legacy.write_text(
        json.dumps({"fps": "60", "source_dir": "old", "output_dir": "old"}),
        encoding="utf-8",
    )
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    workspace = store.migrate_legacy(legacy)

    assert workspace is not None
    settings = json.loads(workspace.settings_path.read_text(encoding="utf-8"))
    assert settings["fps"] == "60"
    assert settings["source_dir"] == str(workspace.media_dir)
    assert settings["output_dir"] == str(workspace.output_dir)


def test_registry_backup_restores_an_unexpected_empty_primary(tmp_path):
    registry = tmp_path / "registry.json"
    store = WorkspaceStore(registry, tmp_path / "projects")
    workspace = store.create("Tin Nhật")

    registry.write_text(
        json.dumps({"version": 1, "last_opened": "", "workspaces": []}),
        encoding="utf-8",
    )

    restored = WorkspaceStore(registry, tmp_path / "projects")
    assert restored.get(workspace.workspace_id) is not None
    assert json.loads(registry.read_text(encoding="utf-8"))["workspaces"]


def test_registry_backup_restores_topic_folders_without_workspaces(tmp_path):
    registry = tmp_path / "registry.json"
    store = WorkspaceStore(registry, tmp_path / "projects")
    folder = store.create_folder("Chủ đề Nhật Bản")

    registry.write_text(
        json.dumps({"version": 1, "last_opened": "", "workspaces": []}),
        encoding="utf-8",
    )

    restored = WorkspaceStore(registry, tmp_path / "projects")
    assert restored.list_folders() == [folder]


def test_recover_unregistered_uses_workspace_metadata_on_disk(tmp_path):
    registry = tmp_path / "registry.json"
    projects = tmp_path / "projects"
    store = WorkspaceStore(registry, projects)
    workspace = store.create("Tin Nhật")
    registry.unlink()
    store.backup_path.unlink()

    recovered_store = WorkspaceStore(registry, projects)
    assert recovered_store.recover_unregistered() == 1
    recovered = recovered_store.get(workspace.workspace_id)

    assert recovered is not None
    assert recovered.root == workspace.root
    assert workspace.metadata_path.is_file()


def test_relocate_all_moves_workspace_data_and_persists_new_library_root(tmp_path):
    registry = tmp_path / "registry.json"
    source_root = tmp_path / "projects"
    destination_root = tmp_path / "workspace-library"
    store = WorkspaceStore(registry, source_root)
    workspace = store.create("Tin Nhật")
    marker = workspace.output_dir / "video.mp4"
    marker.write_bytes(b"video")
    old_path = str(marker.resolve())
    workspace.pipeline_checkpoint_path.write_text(
        json.dumps({"video_path": old_path}),
        encoding="utf-8",
    )

    result = store.relocate_all(destination_root, trash_sources=False)

    moved = result.workspaces[0]
    assert moved.root == destination_root / workspace.root.name
    assert (moved.output_dir / "video.mp4").read_bytes() == b"video"
    assert json.loads(moved.pipeline_checkpoint_path.read_text(encoding="utf-8"))[
        "video_path"
    ] == str(moved.output_dir / "video.mp4")
    assert workspace.root.is_dir()
    assert store.default_root == destination_root.resolve()

    restarted = WorkspaceStore(registry)
    assert restarted.default_root == destination_root.resolve()
    restored = restarted.get(workspace.workspace_id)
    assert restored is not None
    assert restored.root == moved.root


def test_relocate_all_rejects_destination_inside_existing_workspace(tmp_path):
    store = WorkspaceStore(tmp_path / "registry.json", tmp_path / "projects")
    workspace = store.create("Tin Nhật")
    invalid_destination = workspace.root / "nested-library"

    try:
        store.relocate_all(invalid_destination, trash_sources=False)
    except ValueError as exc:
        assert "không được nằm trong" in str(exc)
    else:
        raise AssertionError("Workspace relocation must reject nested destinations")

    assert workspace.root.is_dir()
    assert store.get(workspace.workspace_id).root == workspace.root
