from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Callable

from .donut_mcp import DonutMcpClient, DonutMcpError


logger = logging.getLogger(__name__)

BRIDGE_EXTENSION_NAME = "DocuRandom Donut Bridge"
BRIDGE_EXTENSION_GROUP_NAME = "DocuRandom Automation"
_REQUIRED_TOOLS = frozenset(
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


class DonutBridgeInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class DonutBridgeInstallResult:
    extension_id: str
    extension_version: str
    compatible_profiles: int
    assigned_profiles: int
    preserved_groups: int
    updated_groups: int
    running_profile_names: tuple[str, ...]
    skipped_profile_names: tuple[str, ...]


ProgressCallback = Callable[[float, str], None]


def _report(callback: ProgressCallback | None, percent: float, detail: str) -> None:
    if callback is not None:
        callback(max(0.0, min(100.0, float(percent))), detail)


def _object_list(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise DonutBridgeInstallError(f"Donut không trả {label} đúng định dạng.")
    return [item for item in value if isinstance(item, dict)]


def _identifier(value: object) -> str:
    return str(value or "").strip()


def install_bridge_for_all_profiles(
    client: DonutMcpClient,
    extension_dir: Path,
    *,
    progress: ProgressCallback | None = None,
) -> DonutBridgeInstallResult:
    """Install/update the bundled Bridge and assign it without replacing groups.

    Donut profiles may have one extension group.  Profiles that already have a
    group keep it; the Bridge is added to that group's membership.  Profiles
    without a group share a dedicated DocuRandom group.  No cookie, proxy,
    fingerprint or raw profile file is modified.
    """

    source = extension_dir.expanduser().resolve(strict=False)
    if not source.is_dir() or not (source / "manifest.json").is_file():
        raise DonutBridgeInstallError(
            "Bản tool hiện tại thiếu gói DocuRandom Donut Bridge hợp lệ."
        )

    _report(progress, 3.0, "Đang xác minh Donut Local và gói Browser Bridge…")
    try:
        client.require_docurandom_local()
        tools = client.list_tool_names()
    except DonutMcpError as exc:
        raise DonutBridgeInstallError(str(exc)) from exc
    missing = sorted(_REQUIRED_TOOLS - tools)
    if missing:
        raise DonutBridgeInstallError(
            "Donut Local đang chạy nhưng thiếu công cụ quản lý tiện ích: "
            + ", ".join(missing)
            + ". Hãy cập nhật Donut Local rồi thử lại."
        )

    _report(progress, 12.0, "Đang đọc tiện ích, nhóm tiện ích và toàn bộ profile…")
    try:
        extensions = _object_list(client.call_tool_json("list_extensions"), "danh sách tiện ích")
        groups = _object_list(
            client.call_tool_json("list_extension_groups"), "danh sách nhóm tiện ích"
        )
        profiles = _object_list(client.call_tool_json("list_profiles"), "danh sách profile")
    except DonutMcpError as exc:
        raise DonutBridgeInstallError(str(exc)) from exc

    matching = [
        item
        for item in extensions
        if _identifier(item.get("name")).casefold() == BRIDGE_EXTENSION_NAME.casefold()
    ]
    matching.sort(
        key=lambda item: int(item.get("updated_at") or 0),
        reverse=True,
    )

    _report(progress, 24.0, "Đang cài/cập nhật một bản Bridge dùng chung…")
    try:
        if matching:
            extension = client.call_tool_json(
                "update_extension",
                {
                    "extension_id": _identifier(matching[0].get("id")),
                    "name": BRIDGE_EXTENSION_NAME,
                    "path": str(source),
                    "link": False,
                },
            )
        else:
            extension = client.call_tool_json(
                "add_extension",
                {
                    "path": str(source),
                    "name": BRIDGE_EXTENSION_NAME,
                    "link": False,
                },
            )
    except DonutMcpError as exc:
        raise DonutBridgeInstallError(f"Donut không cài được Browser Bridge: {exc}") from exc
    if not isinstance(extension, dict):
        raise DonutBridgeInstallError("Donut không xác nhận gói Browser Bridge vừa cài.")
    extension_id = _identifier(extension.get("id"))
    if not extension_id:
        raise DonutBridgeInstallError("Donut trả Browser Bridge nhưng thiếu mã tiện ích.")
    extension_version = _identifier(extension.get("version"))

    groups_by_id = {
        _identifier(item.get("id")): item
        for item in groups
        if _identifier(item.get("id"))
    }
    compatible = [
        item
        for item in profiles
        if _identifier(item.get("browser")).casefold() == "wayfern"
    ]
    skipped = tuple(
        _identifier(item.get("name")) or _identifier(item.get("id"))
        for item in profiles
        if _identifier(item.get("browser")).casefold() != "wayfern"
    )
    if not compatible:
        raise DonutBridgeInstallError(
            "Không có profile Wayfern tương thích để cài Browser Bridge."
        )

    required_group_ids = {
        _identifier(item.get("extension_group_id"))
        for item in compatible
        if _identifier(item.get("extension_group_id")) in groups_by_id
    }
    unassigned = [
        item
        for item in compatible
        if _identifier(item.get("extension_group_id")) not in groups_by_id
    ]

    shared_group: dict[str, object] | None = next(
        (
            item
            for item in groups
            if _identifier(item.get("name")).casefold()
            == BRIDGE_EXTENSION_GROUP_NAME.casefold()
        ),
        None,
    )
    if unassigned and shared_group is None:
        _report(progress, 34.0, "Đang tạo nhóm tiện ích DocuRandom dùng chung…")
        try:
            created = client.call_tool_json(
                "create_extension_group", {"name": BRIDGE_EXTENSION_GROUP_NAME}
            )
        except DonutMcpError as exc:
            raise DonutBridgeInstallError(f"Không tạo được nhóm Browser Bridge: {exc}") from exc
        if not isinstance(created, dict) or not _identifier(created.get("id")):
            raise DonutBridgeInstallError("Donut không xác nhận nhóm Browser Bridge vừa tạo.")
        shared_group = created
        groups_by_id[_identifier(created.get("id"))] = created

    if shared_group is not None and unassigned:
        required_group_ids.add(_identifier(shared_group.get("id")))

    preserved_groups = 0
    updated_groups = 0
    ordered_group_ids = sorted(group_id for group_id in required_group_ids if group_id)
    for index, group_id in enumerate(ordered_group_ids, start=1):
        group = groups_by_id.get(group_id, {})
        members = {
            _identifier(value)
            for value in (group.get("extension_ids") or [])
            if _identifier(value)
        }
        if members:
            preserved_groups += 1
        if extension_id not in members:
            _report(
                progress,
                38.0 + (22.0 * index / max(1, len(ordered_group_ids))),
                f"Đang thêm Bridge vào nhóm tiện ích {index}/{len(ordered_group_ids)}…",
            )
            try:
                client.call_tool_json(
                    "add_extension_to_group",
                    {"group_id": group_id, "extension_id": extension_id},
                )
            except DonutMcpError as exc:
                raise DonutBridgeInstallError(
                    f"Không thêm được Bridge vào nhóm tiện ích hiện có: {exc}"
                ) from exc
            updated_groups += 1

    assigned_profiles = 0
    shared_group_id = _identifier(shared_group.get("id")) if shared_group else ""
    for index, profile in enumerate(unassigned, start=1):
        profile_id = _identifier(profile.get("id"))
        if not profile_id or not shared_group_id:
            continue
        _report(
            progress,
            62.0 + (32.0 * index / max(1, len(unassigned))),
            f"Đang gán Bridge cho profile {index}/{len(unassigned)}…",
        )
        try:
            client.call_tool_json(
                "assign_extension_group_to_profile",
                {"profile_id": profile_id, "extension_group_id": shared_group_id},
            )
        except DonutMcpError as exc:
            profile_name = _identifier(profile.get("name")) or profile_id
            raise DonutBridgeInstallError(
                f"Không gán được Bridge cho profile '{profile_name}': {exc}"
            ) from exc
        assigned_profiles += 1

    running_names = tuple(
        _identifier(item.get("name")) or _identifier(item.get("id"))
        for item in compatible
        if item.get("process_id")
    )
    _report(progress, 100.0, "Đã cài/cập nhật Bridge cho toàn bộ profile tương thích.")
    logger.info(
        "donut_bridge_install_complete extension=%s version=%s profiles=%s assigned=%s groups=%s",
        extension_id,
        extension_version or "unknown",
        len(compatible),
        assigned_profiles,
        len(required_group_ids),
    )
    return DonutBridgeInstallResult(
        extension_id=extension_id,
        extension_version=extension_version,
        compatible_profiles=len(compatible),
        assigned_profiles=assigned_profiles,
        preserved_groups=preserved_groups,
        updated_groups=updated_groups,
        running_profile_names=running_names,
        skipped_profile_names=skipped,
    )
