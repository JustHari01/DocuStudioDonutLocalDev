from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from send2trash import send2trash

from . import __version__
from .storage import (
    app_data_dir,
    atomic_write_json,
    atomic_write_text,
    default_workspaces_dir,
    previous_managed_data_dir,
)


WORKSPACE_VERSION = 2
WORKSPACE_FOLDER_NAME = "DocuRandom Workspaces"
REGISTRY_FILENAME = "workspaces.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def workspace_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_value).strip("-").lower()
    return slug[:60] or "workspace"


def resolve_workspace_selectors(
    workspaces: list[Workspace] | tuple[Workspace, ...],
    selector: str,
) -> tuple[Workspace, ...]:
    """Resolve Telegram-friendly 1-based indexes, IDs, or exact names."""

    available = tuple(workspaces)
    raw = str(selector or "").strip()
    if not raw or not available:
        return ()
    if raw.casefold() in {"all", "tat ca", "tất cả"}:
        return available

    whole = raw.casefold()
    exact_whole = tuple(
        workspace
        for workspace in available
        if workspace.workspace_id.casefold() == whole
        or workspace.name.casefold() == whole
    )
    if len(exact_whole) == 1:
        return exact_whole

    tokens = [item for item in re.split(r"[\s,;]+", raw) if item]
    resolved: list[Workspace] = []
    seen: set[str] = set()
    for token in tokens:
        match: Workspace | None = None
        if token.isdigit():
            index = int(token)
            if 1 <= index <= len(available):
                match = available[index - 1]
        if match is None:
            query = token.casefold()
            exact = [
                workspace
                for workspace in available
                if workspace.workspace_id.casefold() == query
                or workspace.name.casefold() == query
            ]
            if len(exact) == 1:
                match = exact[0]
            else:
                partial = [
                    workspace
                    for workspace in available
                    if query in workspace.name.casefold()
                ]
                if len(partial) == 1:
                    match = partial[0]
        if match is None:
            return ()
        if match.workspace_id not in seen:
            seen.add(match.workspace_id)
            resolved.append(match)
    return tuple(resolved)


def resolve_workspace_selector_report(
    workspaces: list[Workspace] | tuple[Workspace, ...],
    selector: str,
) -> tuple[tuple[Workspace, ...], tuple[str, ...]]:
    """Resolve every token while preserving the names/indexes that do not exist."""

    available = tuple(workspaces)
    raw = str(selector or "").strip()
    if not raw:
        return (), ("(chưa ghi workspace)",)
    if raw.casefold() in {"all", "tat ca", "tất cả"}:
        return available, () if available else ("all",)

    whole = raw.casefold()
    exact_whole = tuple(
        workspace
        for workspace in available
        if workspace.workspace_id.casefold() == whole
        or workspace.name.casefold() == whole
    )
    if len(exact_whole) == 1:
        return exact_whole, ()

    resolved: list[Workspace] = []
    missing: list[str] = []
    seen: set[str] = set()
    for token in (item for item in re.split(r"[\s,;]+", raw) if item):
        match: Workspace | None = None
        if token.isdigit():
            index = int(token)
            if 1 <= index <= len(available):
                match = available[index - 1]
        if match is None:
            query = token.casefold()
            exact = [
                workspace
                for workspace in available
                if workspace.workspace_id.casefold() == query
                or workspace.name.casefold() == query
            ]
            partial = [
                workspace for workspace in available if query in workspace.name.casefold()
            ]
            if len(exact) == 1:
                match = exact[0]
            elif len(partial) == 1:
                match = partial[0]
        if match is None:
            missing.append(token)
        elif match.workspace_id not in seen:
            seen.add(match.workspace_id)
            resolved.append(match)
    return tuple(resolved), tuple(missing)


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    name: str
    root_dir: str
    created_at: str
    updated_at: str
    category: str = "Video YouTube"
    folder_id: str = ""

    @property
    def root(self) -> Path:
        return Path(self.root_dir).expanduser().resolve(strict=False)

    @property
    def media_dir(self) -> Path:
        return self.root / "Media"

    @property
    def output_dir(self) -> Path:
        return self.root / "Output"

    @property
    def uploaded_dir(self) -> Path:
        return self.root / "Uploaded"

    @property
    def prompt_dir(self) -> Path:
        return self.root / "Prompts"

    @property
    def voicevox_dir(self) -> Path:
        return self.root / "VOICEVOX"

    @property
    def state_dir(self) -> Path:
        return self.root / ".docurandom"

    @property
    def settings_path(self) -> Path:
        return self.state_dir / "settings.json"

    @property
    def history_path(self) -> Path:
        return self.state_dir / "render_history.json"

    @property
    def flow_history_path(self) -> Path:
        return self.state_dir / "flow_history.json"

    @property
    def content_history_path(self) -> Path:
        return self.state_dir / "content_history.json"

    @property
    def content_history_text_path(self) -> Path:
        return self.root / "CHECKLIST_NOI_DUNG_DA_LAM.txt"

    @property
    def pipeline_checkpoint_path(self) -> Path:
        return self.state_dir / "pipeline_checkpoint.json"

    @property
    def metadata_path(self) -> Path:
        return self.state_dir / "workspace.json"

    def ensure_layout(self) -> None:
        for directory in (
            self.root,
            self.media_dir,
            self.output_dir,
            self.uploaded_dir,
            self.prompt_dir,
            self.voicevox_dir,
            self.state_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        marker = self.root / "WORKSPACE.txt"
        if not marker.exists():
            marker.write_text(
                "DocuRandom Studio Workspace\n\n"
                "Media: ảnh/video tải từ Flow và media dựng.\n"
                "Output: video và gói nội dung cuối cùng.\n"
                "Uploaded: toàn bộ dữ liệu các phiên đã đăng YouTube.\n"
                "Prompts: master prompt và prompt dự án.\n"
                "VOICEVOX: WAV, SRT và backup giọng đọc.\n",
                encoding="utf-8-sig",
            )
        atomic_write_json(self.metadata_path, asdict(self))
        # Every workspace owns an independent content checklist. It lives
        # outside disposable Media/Output folders and therefore survives the
        # archive-and-clean cycle after a confirmed YouTube upload.
        from .content_history import ContentHistoryStore

        ContentHistoryStore(
            self.content_history_path,
            self.content_history_text_path,
        ).ensure_files()

    def default_settings(self) -> dict[str, object]:
        return {
            # New workspaces already belong to this build. Existing workspaces
            # without this marker are paused once after an application update.
            "last_started_app_version": __version__,
            "source_dir": str(self.media_dir),
            "output_dir": str(self.output_dir),
            "voicevox_backup_dir": str(self.voicevox_dir),
            "chatgpt_web_model": "ChatGPT Web · Theo model đang chọn trên Web",
            "content_dedup_enabled": True,
            "flow_enabled": True,
            "flow_model": "harbor_seal",
            "flow_aspect": "landscape",
            # New workspaces start with the managed daily schedule armed.
            "schedule_armed": True,
            "schedule_resume_after_manual_continue": True,
        }


@dataclass(frozen=True)
class WorkspaceFolder:
    folder_id: str
    name: str
    created_at: str
    updated_at: str
    parent_id: str = ""


@dataclass(frozen=True)
class WorkspaceRelocationResult:
    """Result of moving the complete workspace library to a new parent."""

    destination_root: Path
    workspaces: tuple[Workspace, ...]
    retained_sources: tuple[Path, ...] = ()


def _resolved_workspace_parent(value: Path | str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Nơi lưu workspace phải là một đường dẫn tuyệt đối.")
    return candidate.resolve(strict=False)


def _is_within(path: Path, parent: Path) -> bool:
    """Return whether *path* is equal to or below *parent* safely on Windows."""

    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


class WorkspaceStore:
    def __init__(
        self,
        registry_path: Path | None = None,
        default_root: Path | None = None,
    ) -> None:
        uses_default_registry = registry_path is None
        self.registry_path = registry_path or app_data_dir() / REGISTRY_FILENAME
        self.default_root = _resolved_workspace_parent(
            default_root or default_workspaces_dir()
        )
        # The selected library root is persisted in the workspace registry, so
        # a restart does not silently fall back to the old default directory.
        # Explicit roots are used by tests and migration callers and must win.
        if default_root is None:
            persisted = self._read_persisted_workspace_root()
            if persisted is not None:
                self.default_root = persisted
        if uses_default_registry and default_root is None:
            self.import_previous_managed_workspaces()

    @property
    def backup_path(self) -> Path:
        return self.registry_path.with_name("workspaces.backup.json")

    @staticmethod
    def _valid_raw(value: object) -> dict[str, object] | None:
        if isinstance(value, dict) and isinstance(value.get("workspaces"), list):
            return value
        return None

    def _read_registry(self, path: Path) -> dict[str, object] | None:
        try:
            return self._valid_raw(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def _read_persisted_workspace_root(self) -> Path | None:
        for path in (self.registry_path, self.backup_path):
            raw = self._read_registry(path)
            if raw is None:
                continue
            value = raw.get("workspace_root")
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                return _resolved_workspace_parent(value)
            except (OSError, ValueError):
                continue
        return None

    def _load_raw(self) -> dict[str, object]:
        primary = self._read_registry(self.registry_path)
        backup = self._read_registry(self.backup_path)
        if primary is not None:
            primary_items = primary.get("workspaces", [])
            backup_items = backup.get("workspaces", []) if backup is not None else []
            primary_folders = primary.get("folders", [])
            backup_folders = backup.get("folders", []) if backup is not None else []
            # An unexpected empty primary after replacement must not hide the
            # durable backup. Intentional deletions update both files together.
            if (
                primary_items
                or primary_folders
                or primary.get("removed_workspaces")
                or (not backup_items and not backup_folders)
            ):
                return primary
        if backup is not None:
            try:
                atomic_write_json(self.registry_path, backup)
            except OSError:
                pass
            return backup
        return {
            "version": WORKSPACE_VERSION,
            "last_opened": "",
            "folders": [],
            "workspaces": [],
            "workspace_root": str(self.default_root),
        }

    def _save_raw(self, value: dict[str, object]) -> None:
        value["version"] = WORKSPACE_VERSION
        value["workspace_root"] = str(self.default_root)
        # Commit the durable copy first. If Windows is restarted between the
        # two replacements, the next startup can still reconstruct the list.
        atomic_write_json(self.backup_path, value)
        atomic_write_json(self.registry_path, value)

    @staticmethod
    def _removal_keys(raw: dict[str, object]) -> tuple[set[str], set[str]]:
        entries = raw.get("removed_workspaces", [])
        ids: set[str] = set()
        roots: set[str] = set()
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            workspace_id = str(entry.get("workspace_id", ""))
            root = str(entry.get("root_dir", ""))
            if workspace_id:
                ids.add(workspace_id)
            if root:
                roots.add(os.path.normcase(str(Path(root).resolve(strict=False))))
        return ids, roots

    def import_previous_managed_workspaces(self) -> int:
        """Merge valid workspaces from the old managed root without moving data."""

        previous_data = previous_managed_data_dir()
        if previous_data is None:
            return 0
        checked_marker = self.registry_path.with_name(
            ".previous-managed-workspaces-imported"
        )
        if checked_marker.exists():
            return 0
        source_path = previous_data / REGISTRY_FILENAME
        if os.path.normcase(str(source_path.resolve(strict=False))) == os.path.normcase(
            str(self.registry_path.resolve(strict=False))
        ):
            return 0
        source = self._read_registry(source_path)
        if source is None:
            return 0

        target = self._load_raw()
        removed_ids, removed_roots = self._removal_keys(target)
        target_items = [
            dict(item)
            for item in target.get("workspaces", [])
            if isinstance(item, dict)
        ]
        known_ids: set[str] = set()
        known_roots: set[str] = set()
        known_folder_ids = {
            str(item.get("folder_id", ""))
            for item in target.get("folders", [])
            if isinstance(item, dict) and item.get("folder_id")
        }
        for item in target_items:
            parsed = self._from_dict(item)
            if parsed is None:
                continue
            known_ids.add(parsed.workspace_id)
            known_roots.add(os.path.normcase(str(parsed.root)))

        imported = 0
        for value in source.get("workspaces", []):
            parsed = self._from_dict(value)
            if parsed is None:
                continue
            normalized_root = os.path.normcase(str(parsed.root))
            if (
                parsed.workspace_id in known_ids
                or normalized_root in known_roots
                or parsed.workspace_id in removed_ids
                or normalized_root in removed_roots
            ):
                continue
            migrated = dict(value)
            if parsed.folder_id and parsed.folder_id not in known_folder_ids:
                # A folder from the old registry can be absent in the new
                # registry. Keep its workspace visible at the library root.
                migrated["folder_id"] = ""
            target_items.append(migrated)
            known_ids.add(parsed.workspace_id)
            known_roots.add(normalized_root)
            imported += 1

        if not imported:
            atomic_write_text(
                checked_marker,
                "Previous managed workspace registry checked.\n",
                encoding="ascii",
            )
            return 0

        snapshot_path = self.registry_path.with_name(
            "workspaces.before-legacy-import.json"
        )
        if not snapshot_path.exists():
            atomic_write_json(snapshot_path, target)
        target["workspaces"] = target_items
        # Keep the current home, folders, and last-opened workspace. The old
        # registry contributes only workspace entries that are not present.
        self._save_raw(target)
        atomic_write_text(
            checked_marker,
            f"Imported {imported} previous managed workspace(s).\n",
            encoding="ascii",
        )
        return imported

    def recover_unregistered(self) -> int:
        """Re-register workspace folders that remain on disk after registry loss."""

        try:
            candidates = tuple(self.default_root.iterdir())
        except OSError:
            return 0
        raw = self._load_raw()
        items = [item for item in raw["workspaces"] if isinstance(item, dict)]  # type: ignore[index]
        removed_ids, removed_roots = self._removal_keys(raw)
        known_roots = {
            os.path.normcase(str(parsed.root))
            for item in items
            if (parsed := self._from_dict(item)) is not None
        }
        recovered = 0
        for root in candidates:
            normalized_root = os.path.normcase(str(root.resolve(strict=False)))
            if (
                not root.is_dir()
                or normalized_root in known_roots
                or normalized_root in removed_roots
            ):
                continue
            metadata = root / ".docurandom" / "workspace.json"
            workspace = None
            if metadata.is_file():
                try:
                    workspace = self._from_dict(
                        json.loads(metadata.read_text(encoding="utf-8"))
                    )
                except Exception:
                    workspace = None
            if workspace is None and (root / "WORKSPACE.txt").is_file() and (root / ".docurandom").is_dir():
                try:
                    modified = datetime.fromtimestamp(root.stat().st_mtime, timezone.utc).isoformat()
                except OSError:
                    modified = _now_iso()
                workspace = Workspace(
                    workspace_id=uuid.uuid5(uuid.NAMESPACE_URL, str(root.resolve(strict=False))).hex,
                    name=root.name,
                    root_dir=str(root.resolve(strict=False)),
                    created_at=modified,
                    updated_at=modified,
                )
            if workspace is None:
                continue
            if workspace.workspace_id in removed_ids:
                continue
            if workspace.name.strip().casefold() == "dự án hiện tại".casefold():
                # This legacy placeholder is deliberately removed from the
                # launcher. Keep its folder on disk; do not silently restore it
                # after its registry entry has been removed.
                continue
            # Metadata travels with the workspace folder. If the whole data
            # directory was moved to another drive, the stored absolute path
            # must follow the folder we have actually discovered.
            actual_root = str(root.resolve(strict=False))
            if os.path.normcase(str(workspace.root)) != os.path.normcase(actual_root):
                workspace = replace(workspace, root_dir=actual_root, updated_at=_now_iso())
            workspace.ensure_layout()
            items.append(asdict(workspace))
            known_roots.add(os.path.normcase(str(workspace.root)))
            recovered += 1
        if recovered:
            raw["workspaces"] = items
            if not raw.get("last_opened"):
                last_recovered = self._from_dict(items[-1])
                if last_recovered is not None:
                    raw["last_opened"] = last_recovered.workspace_id
            self._save_raw(raw)
        return recovered

    @staticmethod
    def _from_dict(value: object) -> Workspace | None:
        if not isinstance(value, dict):
            return None
        try:
            workspace = Workspace(
                workspace_id=str(value["workspace_id"]),
                name=str(value["name"]),
                root_dir=str(value["root_dir"]),
                created_at=str(value["created_at"]),
                updated_at=str(value["updated_at"]),
                category=str(value.get("category", "Video YouTube")),
                folder_id=str(value.get("folder_id", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return workspace if workspace.workspace_id and workspace.name else None

    @staticmethod
    def _folder_from_dict(value: object) -> WorkspaceFolder | None:
        if not isinstance(value, dict):
            return None
        try:
            folder = WorkspaceFolder(
                folder_id=str(value["folder_id"]),
                name=str(value["name"]),
                created_at=str(value["created_at"]),
                updated_at=str(value["updated_at"]),
                parent_id=str(value.get("parent_id", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return folder if folder.folder_id and folder.name else None

    @staticmethod
    def _clean_folder_name(name: str) -> str:
        clean_name = " ".join(name.strip().split())
        if not clean_name:
            raise ValueError("Hãy nhập tên thư mục chủ đề.")
        if len(clean_name) > 100:
            raise ValueError("Tên thư mục chủ đề tối đa 100 ký tự.")
        return clean_name

    def list_folders(self) -> list[WorkspaceFolder]:
        raw = self._load_raw()
        values = raw.get("folders", [])
        folders = (
            [self._folder_from_dict(item) for item in values]
            if isinstance(values, list)
            else []
        )
        return sorted(
            (item for item in folders if item is not None),
            key=lambda item: (item.name.casefold(), item.created_at),
        )

    @staticmethod
    def _folder_map(
        folders: list[WorkspaceFolder],
    ) -> dict[str, WorkspaceFolder]:
        return {folder.folder_id: folder for folder in folders}

    def folder_descendant_ids(self, folder_id: str) -> set[str]:
        """Return every virtual descendant without touching workspace folders on disk."""

        children: dict[str, list[str]] = {}
        for folder in self.list_folders():
            children.setdefault(folder.parent_id, []).append(folder.folder_id)
        descendants: set[str] = set()
        pending = list(children.get(folder_id, ()))
        while pending:
            child_id = pending.pop()
            if child_id in descendants:
                continue
            descendants.add(child_id)
            pending.extend(children.get(child_id, ()))
        return descendants

    def folder_path(self, folder_id: str) -> tuple[WorkspaceFolder, ...]:
        """Return a root-to-leaf breadcrumb, tolerating broken legacy parents."""

        folders = self._folder_map(self.list_folders())
        current = folders.get(folder_id)
        result: list[WorkspaceFolder] = []
        visited: set[str] = set()
        while current is not None and current.folder_id not in visited:
            visited.add(current.folder_id)
            result.append(current)
            current = folders.get(current.parent_id)
        result.reverse()
        return tuple(result)

    @staticmethod
    def _ensure_unique_folder_name(
        folders: list[WorkspaceFolder],
        clean_name: str,
        parent_id: str,
        *,
        ignore_id: str = "",
    ) -> None:
        if any(
            item.parent_id == parent_id
            and item.name.casefold() == clean_name.casefold()
            and item.folder_id != ignore_id
            for item in folders
        ):
            raise ValueError("Đã có thư mục cùng cấp mang tên này.")

    def create_folder(self, name: str, parent_id: str = "") -> WorkspaceFolder:
        clean_name = self._clean_folder_name(name)
        raw = self._load_raw()
        existing = self.list_folders()
        if parent_id and parent_id not in self._folder_map(existing):
            raise ValueError("Thư mục cha không còn tồn tại.")
        self._ensure_unique_folder_name(existing, clean_name, parent_id)
        now = _now_iso()
        folder = WorkspaceFolder(
            folder_id=uuid.uuid4().hex,
            name=clean_name,
            created_at=now,
            updated_at=now,
            parent_id=parent_id,
        )
        values = raw.get("folders", [])
        items = (
            [item for item in values if isinstance(item, dict)]
            if isinstance(values, list)
            else []
        )
        items.append(asdict(folder))
        raw["folders"] = items
        self._save_raw(raw)
        return folder

    def rename_folder(self, folder_id: str, name: str) -> WorkspaceFolder:
        clean_name = self._clean_folder_name(name)
        raw = self._load_raw()
        existing = self.list_folders()
        current = next(
            (item for item in existing if item.folder_id == folder_id),
            None,
        )
        if current is None:
            raise ValueError("Thư mục chủ đề không còn tồn tại.")
        self._ensure_unique_folder_name(
            existing,
            clean_name,
            current.parent_id,
            ignore_id=folder_id,
        )
        values = raw.get("folders", [])
        items: list[dict[str, object]] = []
        renamed: WorkspaceFolder | None = None
        for value in values if isinstance(values, list) else []:
            parsed = self._folder_from_dict(value)
            if parsed is not None and parsed.folder_id == folder_id:
                renamed = replace(parsed, name=clean_name, updated_at=_now_iso())
                items.append(asdict(renamed))
            elif isinstance(value, dict):
                items.append(value)
        if renamed is None:
            raise ValueError("Thư mục chủ đề không còn tồn tại.")
        raw["folders"] = items
        self._save_raw(raw)
        return renamed

    def move_folder(self, folder_id: str, parent_id: str = "") -> WorkspaceFolder:
        """Move one virtual folder while preventing self-parenting and cycles."""

        folders = self.list_folders()
        folder_map = self._folder_map(folders)
        current = folder_map.get(folder_id)
        if current is None:
            raise ValueError("Thư mục chủ đề không còn tồn tại.")
        if parent_id and parent_id not in folder_map:
            raise ValueError("Thư mục cha không còn tồn tại.")
        if parent_id == folder_id or parent_id in self.folder_descendant_ids(folder_id):
            raise ValueError("Không thể đặt thư mục vào chính nó hoặc thư mục con của nó.")
        self._ensure_unique_folder_name(
            folders,
            current.name,
            parent_id,
            ignore_id=folder_id,
        )
        if current.parent_id == parent_id:
            return current

        raw = self._load_raw()
        values = raw.get("folders", [])
        items: list[dict[str, object]] = []
        moved: WorkspaceFolder | None = None
        for value in values if isinstance(values, list) else []:
            parsed = self._folder_from_dict(value)
            if parsed is not None and parsed.folder_id == folder_id:
                moved = replace(parsed, parent_id=parent_id, updated_at=_now_iso())
                items.append(asdict(moved))
            elif isinstance(value, dict):
                items.append(value)
        if moved is None:
            raise ValueError("Thư mục chủ đề không còn tồn tại.")
        raw["folders"] = items
        self._save_raw(raw)
        return moved

    def assign_to_folder(
        self,
        workspace_ids: list[str] | tuple[str, ...] | set[str],
        folder_id: str,
    ) -> list[Workspace]:
        selected = {str(value) for value in workspace_ids if str(value)}
        if not selected:
            return []
        if folder_id and not any(
            item.folder_id == folder_id for item in self.list_folders()
        ):
            raise ValueError("Thư mục chủ đề không còn tồn tại.")
        raw = self._load_raw()
        items: list[dict[str, object]] = []
        updated: list[Workspace] = []
        now = _now_iso()
        found: set[str] = set()
        for value in raw["workspaces"]:  # type: ignore[index]
            parsed = self._from_dict(value)
            if parsed is not None and parsed.workspace_id in selected:
                changed = replace(parsed, folder_id=folder_id, updated_at=now)
                items.append(asdict(changed))
                updated.append(changed)
                found.add(parsed.workspace_id)
            elif isinstance(value, dict):
                items.append(value)
        missing = selected - found
        if missing:
            raise ValueError("Có workspace không còn tồn tại; hãy làm mới danh sách.")
        raw["workspaces"] = items
        self._save_raw(raw)
        for workspace in updated:
            workspace.ensure_layout()
        return updated

    def delete_folder(self, folder_id: str) -> WorkspaceFolder:
        raw = self._load_raw()
        values = raw.get("folders", [])
        parsed_folders = [
            parsed
            for value in (values if isinstance(values, list) else [])
            if (parsed := self._folder_from_dict(value)) is not None
        ]
        removed = next(
            (folder for folder in parsed_folders if folder.folder_id == folder_id),
            None,
        )
        if removed is None:
            raise ValueError("Thư mục chủ đề không còn tồn tại.")
        folders: list[dict[str, object]] = []
        reserved_names = {
            folder.name.casefold()
            for folder in parsed_folders
            if folder.folder_id != folder_id
            and folder.parent_id == removed.parent_id
        }

        def reparented_name(name: str) -> str:
            if name.casefold() not in reserved_names:
                reserved_names.add(name.casefold())
                return name
            index = 2
            while f"{name} ({index})".casefold() in reserved_names:
                index += 1
            unique = f"{name} ({index})"
            reserved_names.add(unique.casefold())
            return unique

        for value in values if isinstance(values, list) else []:
            parsed = self._folder_from_dict(value)
            if parsed is not None and parsed.folder_id == folder_id:
                continue
            elif parsed is not None and parsed.parent_id == folder_id:
                folders.append(
                    asdict(
                        replace(
                            parsed,
                            name=reparented_name(parsed.name),
                            parent_id=removed.parent_id,
                            updated_at=_now_iso(),
                        )
                    )
                )
            elif isinstance(value, dict):
                folders.append(value)
        now = _now_iso()
        workspaces: list[dict[str, object]] = []
        changed: list[Workspace] = []
        for value in raw["workspaces"]:  # type: ignore[index]
            parsed = self._from_dict(value)
            if parsed is not None and parsed.folder_id == folder_id:
                reassigned = replace(
                    parsed,
                    folder_id=removed.parent_id,
                    updated_at=now,
                )
                workspaces.append(asdict(reassigned))
                changed.append(reassigned)
            elif isinstance(value, dict):
                workspaces.append(value)
        raw["folders"] = folders
        raw["workspaces"] = workspaces
        self._save_raw(raw)
        for workspace in changed:
            workspace.ensure_layout()
        return removed

    def list(self) -> list[Workspace]:
        raw = self._load_raw()
        workspaces = [self._from_dict(item) for item in raw["workspaces"]]  # type: ignore[index]
        return sorted(
            (item for item in workspaces if item is not None),
            key=lambda item: item.updated_at,
            reverse=True,
        )

    def get(self, workspace_id: str) -> Workspace | None:
        return next(
            (item for item in self.list() if item.workspace_id == workspace_id),
            None,
        )

    @staticmethod
    def _rewrite_moved_json_paths(
        workspace_root: Path,
        old_root: Path,
        new_root: Path,
    ) -> None:
        """Update operational absolute paths stored in durable JSON state.

        Workspace files are intentionally copied byte-for-byte first. Only the
        private state directory is rewritten, keeping user media, prompts and
        rendered artifacts untouched while making resume checkpoints point to
        their new location.
        """

        state_dir = workspace_root / ".docurandom"
        if not state_dir.is_dir():
            return
        old_values = {
            str(old_root),
            old_root.as_posix(),
            str(old_root).replace("\\", "\\\\"),
        }
        new_value = str(new_root)
        escaped_new_value = new_value.replace("\\", "\\\\")
        for path in state_dir.rglob("*.json"):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            updated = text
            for old_value in old_values:
                replacement = (
                    escaped_new_value
                    if "\\\\" in old_value
                    else new_value
                )
                updated = updated.replace(old_value, replacement)
                updated = updated.replace(
                    old_value.replace("/", "\\"),
                    replacement,
                )
            if updated != text:
                path.write_text(updated, encoding="utf-8")

    def relocate_all(
        self,
        destination_root: Path,
        *,
        trash_sources: bool = True,
    ) -> WorkspaceRelocationResult:
        """Move every registered workspace under one new parent directory.

        The operation is copy-then-commit: destination copies are validated
        before the registry changes, and the old folders are sent to the
        Windows Recycle Bin only after the new registry is durable. A failed
        copy therefore leaves the active workspace paths untouched.
        """

        destination = _resolved_workspace_parent(destination_root)
        current_root = self.default_root.resolve(strict=False)
        workspaces = tuple(self.list())
        if destination == current_root and all(
            _is_within(workspace.root, destination) for workspace in workspaces
        ):
            return WorkspaceRelocationResult(destination, workspaces)

        sources: list[tuple[Workspace, Path, Path]] = []
        destination_names: set[str] = set()
        for workspace in workspaces:
            source = workspace.root.resolve(strict=False)
            if not source.is_dir():
                raise FileNotFoundError(
                    f"Không tìm thấy dữ liệu workspace “{workspace.name}” tại:\n{source}"
                )
            # Moving a parent into one of its own workspaces (or moving a
            # workspace parent above an existing source) would copy into itself
            # and can corrupt a large library, so fail closed.
            if _is_within(destination, source) or _is_within(source, destination):
                raise ValueError(
                    "Nơi lưu mới không được nằm trong hoặc bao phủ thư mục workspace hiện tại."
                )
            name = source.name
            folded = os.path.normcase(name)
            if folded in destination_names:
                raise ValueError(
                    f"Các workspace có trùng tên thư mục vật lý “{name}”; không thể gom an toàn."
                )
            destination_names.add(folded)
            target = destination / name
            if target.exists():
                raise FileExistsError(
                    f"Thư mục đích đã có dữ liệu: {target}\n"
                    "Hãy chọn một thư mục trống hoặc đổi tên thư mục trùng."
                )
            sources.append((workspace, source, target))

        destination.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        moved_workspaces: list[Workspace] = []
        try:
            for workspace, source, target in sources:
                shutil.copytree(source, target, symlinks=True)
                copied.append(target)
                if not (target / ".docurandom" / "workspace.json").is_file():
                    raise OSError(
                        f"Bản sao workspace “{workspace.name}” thiếu checkpoint metadata."
                    )
                moved = replace(
                    workspace,
                    root_dir=str(target.resolve(strict=False)),
                    updated_at=_now_iso(),
                )
                self._rewrite_moved_json_paths(target, source, target)
                moved.ensure_layout()
                moved_workspaces.append(moved)

            raw = self._load_raw()
            by_id = {item.workspace_id: item for item in moved_workspaces}
            updated_items: list[dict[str, object]] = []
            for value in raw["workspaces"]:  # type: ignore[index]
                parsed = self._from_dict(value)
                replacement = by_id.get(parsed.workspace_id) if parsed else None
                if replacement is not None:
                    updated_items.append(asdict(replacement))
                elif isinstance(value, dict):
                    updated_items.append(value)
            raw["workspaces"] = updated_items
            self.default_root = destination
            self._save_raw(raw)
        except Exception:
            self.default_root = current_root
            for target in reversed(copied):
                try:
                    shutil.rmtree(target)
                except OSError:
                    pass
            raise

        retained: list[Path] = []
        if trash_sources:
            for _workspace, source, _target in sources:
                try:
                    send2trash(str(source))
                except Exception:
                    retained.append(source)

        return WorkspaceRelocationResult(
            destination_root=destination,
            workspaces=tuple(moved_workspaces),
            retained_sources=tuple(retained),
        )

    def create(
        self,
        name: str,
        *,
        parent_dir: Path | None = None,
        category: str = "Video YouTube",
    ) -> Workspace:
        clean_name = " ".join(name.strip().split())
        if not clean_name:
            raise ValueError("Hãy nhập tên workspace.")
        if len(clean_name) > 100:
            raise ValueError("Tên workspace tối đa 100 ký tự.")

        parent = (parent_dir or self.default_root).expanduser().resolve(strict=False)
        parent.mkdir(parents=True, exist_ok=True)
        base = workspace_slug(clean_name)
        root = parent / base
        suffix = 2
        # Never merge a new workspace into any pre-existing directory. This is
        # the isolation guarantee users rely on when switching video formats.
        while root.exists():
            root = parent / f"{base}-{suffix}"
            suffix += 1

        now = _now_iso()
        workspace = Workspace(
            workspace_id=uuid.uuid4().hex,
            name=clean_name,
            root_dir=str(root),
            created_at=now,
            updated_at=now,
            category=category.strip() or "Video YouTube",
        )
        workspace.ensure_layout()
        if not workspace.settings_path.exists():
            atomic_write_json(workspace.settings_path, workspace.default_settings())

        raw = self._load_raw()
        items = [item for item in raw["workspaces"] if isinstance(item, dict)]  # type: ignore[index]
        items.append(asdict(workspace))
        raw["workspaces"] = items
        raw["last_opened"] = workspace.workspace_id
        self._save_raw(raw)
        return workspace

    def touch(self, workspace: Workspace) -> Workspace:
        """Update a registered workspace; stale sessions must never register it."""
        raw = self._load_raw()
        items = []
        updated: Workspace | None = None
        for value in raw["workspaces"]:  # type: ignore[index]
            parsed = self._from_dict(value)
            if parsed is not None and parsed.workspace_id == workspace.workspace_id:
                # Keep registry-authoritative names, folders and relocated paths.
                updated = replace(parsed, updated_at=_now_iso())
                items.append(asdict(updated))
            elif isinstance(value, dict):
                items.append(value)
        if updated is None:
            raise ValueError("Workspace không còn tồn tại; phiên cũ không được lưu lại.")
        raw["workspaces"] = items
        raw["last_opened"] = updated.workspace_id
        self._save_raw(raw)
        updated.ensure_layout()
        return updated

    def rename(self, workspace_id: str, name: str) -> Workspace:
        clean_name = " ".join(name.strip().split())
        if not clean_name:
            raise ValueError("Tên workspace không được để trống.")
        if len(clean_name) > 100:
            raise ValueError("Tên workspace tối đa 100 ký tự.")

        raw = self._load_raw()
        items: list[dict[str, object]] = []
        renamed: Workspace | None = None
        for value in raw["workspaces"]:  # type: ignore[index]
            parsed = self._from_dict(value)
            if parsed is not None and parsed.workspace_id == workspace_id:
                renamed = replace(parsed, name=clean_name, updated_at=_now_iso())
                items.append(asdict(renamed))
            elif isinstance(value, dict):
                items.append(value)
        if renamed is None:
            raise ValueError("Workspace không còn tồn tại.")
        raw["workspaces"] = items
        self._save_raw(raw)
        # The physical folder deliberately keeps its stable slug. Renaming it
        # would invalidate saved source/output paths and running backups.
        renamed.ensure_layout()
        return renamed

    def unregister(self, workspace_id: str) -> Workspace:
        """Remove one workspace from the registry without touching its files."""
        raw = self._load_raw()
        items: list[dict[str, object]] = []
        removed: Workspace | None = None
        remaining: list[Workspace] = []
        for value in raw["workspaces"]:  # type: ignore[index]
            parsed = self._from_dict(value)
            if parsed is not None and parsed.workspace_id == workspace_id:
                removed = parsed
                continue
            if isinstance(value, dict):
                items.append(value)
            if parsed is not None:
                remaining.append(parsed)
        if removed is None:
            raise ValueError("Workspace không còn tồn tại.")
        raw["workspaces"] = items
        # Disk discovery and legacy migration must distinguish explicit removal
        # from a lost registry. Keep this record in both durable registry copies.
        entries = raw.get("removed_workspaces", [])
        removals = [
            entry for entry in (entries if isinstance(entries, list) else [])
            if isinstance(entry, dict) and entry.get("workspace_id") != workspace_id
        ]
        removals.append({
            "workspace_id": removed.workspace_id,
            "root_dir": str(removed.root),
            "removed_at": _now_iso(),
        })
        raw["removed_workspaces"] = removals
        if str(raw.get("last_opened", "")) == workspace_id:
            raw["last_opened"] = (
                max(remaining, key=lambda item: item.updated_at).workspace_id
                if remaining
                else ""
            )
        self._save_raw(raw)
        return removed

    def unregister_named(self, name: str) -> tuple[Workspace, ...]:
        """Remove matching workspaces from the library without deleting files."""

        clean_name = " ".join(str(name or "").strip().split()).casefold()
        if not clean_name:
            return ()
        matches = tuple(
            workspace
            for workspace in self.list()
            if " ".join(workspace.name.strip().split()).casefold() == clean_name
        )
        removed: list[Workspace] = []
        for workspace in matches:
            try:
                removed.append(self.unregister(workspace.workspace_id))
            except ValueError:
                continue
        return tuple(removed)

    def last_opened(self) -> Workspace | None:
        raw = self._load_raw()
        workspace_id = str(raw.get("last_opened", ""))
        return self.get(workspace_id) if workspace_id else None

    def migrate_legacy(self, legacy_settings_path: Path) -> Workspace | None:
        if self.list() or not legacy_settings_path.is_file():
            return None
        try:
            legacy = json.loads(legacy_settings_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(legacy, dict) or not legacy:
            return None
        workspace = self.create("Workspace đã nhập")
        migrated = {**legacy, **workspace.default_settings()}
        atomic_write_json(workspace.settings_path, migrated)
        return workspace
