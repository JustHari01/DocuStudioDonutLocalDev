from __future__ import annotations

from pathlib import Path
import tempfile

from docurandom.donut_bridge_installer import install_bridge_for_all_profiles


class _FakeClient:
    tools = frozenset(
        {
            "list_profiles",
            "list_extensions",
            "list_extension_groups",
            "add_extension",
            "update_extension",
            "create_extension_group",
            "add_extension_to_group",
            "assign_extension_group_to_profile",
        }
    )

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.extensions: list[dict[str, object]] = [
            {
                "id": "bridge-id",
                "name": "DocuRandom Donut Bridge",
                "version": "1.0.0",
                "updated_at": 10,
            }
        ]
        self.groups: list[dict[str, object]] = [
            {
                "id": "existing-group",
                "name": "Tiện ích đang dùng",
                "extension_ids": ["existing-extension"],
            }
        ]
        self.profiles: list[dict[str, object]] = [
            {
                "id": "profile-existing",
                "name": "Kênh cũ",
                "browser": "wayfern",
                "extension_group_id": "existing-group",
            },
            {
                "id": "profile-new",
                "name": "Kênh mới",
                "browser": "wayfern",
                "extension_group_id": None,
                "process_id": 123,
            },
            {
                "id": "profile-firefox",
                "name": "Camoufox",
                "browser": "camoufox",
                "extension_group_id": None,
            },
        ]

    def require_docurandom_local(self) -> dict[str, object]:
        return {"serverInfo": {"name": "donut-browser-local"}}

    def list_tool_names(self) -> frozenset[str]:
        return self.tools

    def call_tool_json(
        self, name: str, arguments: dict[str, object] | None = None
    ) -> object:
        payload = arguments or {}
        self.calls.append((name, payload))
        if name == "list_extensions":
            return self.extensions
        if name == "list_extension_groups":
            return self.groups
        if name == "list_profiles":
            return self.profiles
        if name == "update_extension":
            return {
                "id": "bridge-id",
                "name": "DocuRandom Donut Bridge",
                "version": "1.1.14",
            }
        if name == "create_extension_group":
            group = {
                "id": "docurandom-group",
                "name": "DocuRandom Automation",
                "extension_ids": [],
            }
            self.groups.append(group)
            return group
        if name == "add_extension_to_group":
            return {
                "id": payload["group_id"],
                "extension_ids": ["bridge-id"],
            }
        if name == "assign_extension_group_to_profile":
            return {
                "id": payload["profile_id"],
                "extension_group_id": payload["extension_group_id"],
            }
        raise AssertionError(name)


def test_installs_once_preserves_existing_groups_and_assigns_unassigned_profiles() -> None:
    with tempfile.TemporaryDirectory(prefix="DocuRandomBridgeInstall_") as temp:
        source = Path(temp)
        (source / "manifest.json").write_text("{}", encoding="utf-8")
        client = _FakeClient()
        progress: list[tuple[float, str]] = []

        result = install_bridge_for_all_profiles(
            client, source, progress=lambda value, detail: progress.append((value, detail))
        )

    names = [name for name, _arguments in client.calls]
    group_adds = [
        arguments
        for name, arguments in client.calls
        if name == "add_extension_to_group"
    ]
    assignments = [
        arguments
        for name, arguments in client.calls
        if name == "assign_extension_group_to_profile"
    ]
    assert names.count("update_extension") == 1
    assert "update_extension_group" not in names
    assert {item["group_id"] for item in group_adds} == {
        "existing-group",
        "docurandom-group",
    }
    assert assignments == [
        {
            "profile_id": "profile-new",
            "extension_group_id": "docurandom-group",
        }
    ]
    assert result.compatible_profiles == 2
    assert result.assigned_profiles == 1
    assert result.preserved_groups == 1
    assert result.running_profile_names == ("Kênh mới",)
    assert result.skipped_profile_names == ("Camoufox",)
    assert progress[-1][0] == 100.0


def test_repeated_install_is_idempotent_when_bridge_is_already_assigned() -> None:
    with tempfile.TemporaryDirectory(prefix="DocuRandomBridgeInstall_") as temp:
        source = Path(temp)
        (source / "manifest.json").write_text("{}", encoding="utf-8")
        client = _FakeClient()
        client.groups[0]["extension_ids"] = ["existing-extension", "bridge-id"]
        client.profiles = [client.profiles[0]]

        result = install_bridge_for_all_profiles(client, source)

    names = [name for name, _arguments in client.calls]
    assert "create_extension_group" not in names
    assert "add_extension_to_group" not in names
    assert "assign_extension_group_to_profile" not in names
    assert result.compatible_profiles == 1
    assert result.assigned_profiles == 0
