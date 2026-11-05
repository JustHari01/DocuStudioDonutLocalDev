from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from .models import RenderPlan


APP_FOLDER = "DocuRandomStudio"
HOME_ENVIRONMENT = "DOCURANDOM_HOME"
HOME_REGISTRY_KEY = r"Software\DocuRandomStudio"
HOME_REGISTRY_VALUE = "Home"
_PATH_LOCKS_GUARD = RLock()
_PATH_LOCKS: dict[str, RLock] = {}
_ATOMIC_REPLACE_RETRIES = 12
_ATOMIC_REPLACE_MAX_DELAY = 1.0


def _lock_for_path(path: Path) -> RLock:
    """Return the process-wide lock shared by stores for the same file."""
    key = os.path.normcase(str(path.expanduser().resolve(strict=False)))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _safe_managed_root(value: str | Path | None) -> Path | None:
    if value is None or not str(value).strip():
        return None
    try:
        path = Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if not path.is_absolute() or path == Path(path.anchor):
        return None
    return path


def _registry_home() -> Path | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, HOME_REGISTRY_KEY) as key:
            value, value_type = winreg.QueryValueEx(key, HOME_REGISTRY_VALUE)
        if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ}:
            return None
        return _safe_managed_root(os.path.expandvars(str(value)))
    except (OSError, ImportError):
        return None


def configured_home_dir() -> Path | None:
    """Return the user-selected top-level DocuRandom folder, if configured."""

    configured = _safe_managed_root(os.environ.get(HOME_ENVIRONMENT))
    if configured is not None:
        return configured
    configured = _registry_home()
    if configured is not None:
        os.environ.setdefault(HOME_ENVIRONMENT, str(configured))
        return configured
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve(strict=False).parent
        # New single-folder installations keep the replaceable application in
        # <chosen>/App and persistent data beside it in <chosen>/Data.
        if executable_dir.name.casefold() == "app":
            derived = _safe_managed_root(executable_dir.parent)
            if derived is not None:
                os.environ.setdefault(HOME_ENVIRONMENT, str(derived))
                return derived
    return None


def legacy_app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        root = Path(base)
    else:
        root = Path.home() / "AppData" / "Local"
    path = root / APP_FOLDER
    path.mkdir(parents=True, exist_ok=True)
    return path


def previous_managed_data_dir() -> Path | None:
    """Find the prior managed data folder used before the home was renamed.

    Older installed releases used ``Documents\\DocuRandom`` as the managed
    root.  The current installer uses ``Documents\\DocuRandom Studio``.  A
    separately selected managed root must remain authoritative, so only import
    from the known old location when it carries both installer markers and a
    readable workspace registry.
    """

    current_home = configured_home_dir()
    if current_home is None:
        return None
    try:
        previous_home = (Path.home() / "Documents" / "DocuRandom").resolve(
            strict=False
        )
        current_home = current_home.resolve(strict=False)
        previous_key = os.path.normcase(str(previous_home))
        current_key = os.path.normcase(str(current_home))
        if previous_key == current_key or previous_key.startswith(
            current_key.rstrip("\\/") + os.sep
        ) or current_key.startswith(previous_key.rstrip("\\/") + os.sep):
            return None
        marker = previous_home / ".docurandom-home"
        version_marker = previous_home / ".docurandom-installed-version"
        previous_data = previous_home / "Data"
        registry = previous_data / "workspaces.json"
        if marker.read_text(encoding="utf-8").strip() != "DocuRandom Studio managed folder":
            return None
        if not version_marker.read_text(encoding="utf-8").strip():
            return None
        registry_value = json.loads(registry.read_text(encoding="utf-8"))
        if not isinstance(registry_value, dict) or not isinstance(
            registry_value.get("workspaces"), list
        ):
            return None
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return None
    return previous_data


def app_data_dir() -> Path:
    home = configured_home_dir()
    path = home / "Data" if home is not None else legacy_app_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_workspaces_dir() -> Path:
    home = configured_home_dir()
    if home is not None:
        path = home / "Workspaces"
    else:
        path = Path.home() / "Videos" / "DocuRandom Workspaces"
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrate_legacy_user_data(data: Path | None = None) -> Path:
    """Copy irreplaceable legacy state into the managed Data folder once.

    Runtime updates replace only the ``App`` directory.  Older builds may have
    stored the workspace registry and the isolated ChatGPT profile in
    ``LOCALAPPDATA`` before the user selected a managed root.  Copying only
    missing destinations keeps the newer state authoritative while preventing
    an update/root migration from looking like a data reset.
    """

    target = (data or app_data_dir()).resolve(strict=False)
    try:
        source = legacy_app_data_dir().resolve(strict=False)
    except (OSError, RuntimeError):
        return target
    if os.path.normcase(str(source)) == os.path.normcase(str(target)):
        return target
    marker = target / ".legacy-data-migrated"
    target.mkdir(parents=True, exist_ok=True)
    try:
        for filename in ("settings.json", "render_history.json", "workspaces.json"):
            source_file = source / filename
            target_file = target / filename
            if source_file.is_file() and not target_file.exists():
                shutil.copy2(source_file, target_file)
        for dirname in ("chat-bridge", "chatgpt-web", "secrets"):
            source_dir = source / dirname
            target_dir = target / dirname
            if not source_dir.is_dir():
                continue
            # Migration is intentionally merge-only.  A selected managed root
            # may already contain newer ChatGPT/Telegram state, while one
            # individual file (notably the DPAPI Telegram bot secret) still
            # lives in the pre-installer LocalAppData directory.  Copying only
            # missing files preserves the managed copy and makes the operation
            # safe to repeat even after an older build wrote the marker.
            for source_file in source_dir.rglob("*"):
                if not source_file.is_file():
                    continue
                relative = source_file.relative_to(source_dir)
                target_file = target_dir / relative
                if target_file.exists():
                    continue
                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, target_file)
        marker.write_text("legacy user data checked\n", encoding="ascii")
    except OSError:
        # Migration is best-effort and never makes the application unusable.
        # A later startup retries because the marker wasn't committed.
        pass
    return target


def configure_managed_environment() -> Path:
    """Route temporary/runtime output into the selected DocuRandom folder."""

    data = app_data_dir()
    if configured_home_dir() is not None:
        migrate_legacy_user_data(data)
    temporary = data / "Temp"
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = str(temporary)
    os.environ["TEMP"] = str(temporary)
    os.environ["FLOW_OUTPUT_DIR"] = str(data / "Flow")
    # npm is only used when DocuRandom has to prepare its private Codex CLI.
    # Keep its potentially large download cache out of the Windows profile.
    os.environ["NPM_CONFIG_CACHE"] = str(data / "npm-cache")
    return data


def _replace_with_retry(temporary: Path, destination: Path) -> None:
    """Replace a managed file even when Windows briefly holds its target open.

    Multiple workspace sessions can persist the shared ChatGPT profile at the
    same time.  ``os.replace`` is atomic, but Windows returns ``WinError 5``
    when another thread/process has the destination open.  Serialize writes
    in this process and retry the short-lived sharing violation instead of
    failing a workspace startup.
    """

    for attempt in range(_ATOMIC_REPLACE_RETRIES):
        try:
            os.replace(temporary, destination)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            retryable = isinstance(exc, PermissionError) or winerror == 5
            if not retryable or attempt >= _ATOMIC_REPLACE_RETRIES - 1:
                raise
            # Keep the first retries quick, then give a bridge/antivirus time
            # to release the handle without blocking the UI for long.
            time.sleep(min(0.05 * (2**attempt), _ATOMIC_REPLACE_MAX_DELAY))
    raise RuntimeError("Không thể thay thế tệp tạm an toàn.")


def _atomic_write(path: Path, value: str, *, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The path lock is shared by every component in this process, including
    # separate workspace sessions writing the same ChatGPT profile.
    with _lock_for_path(path):
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(value, encoding=encoding)
            _replace_with_retry(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # A failed/locked temporary file is harmless and can be
                # cleaned on the next startup; never mask the real write error.
                pass


def atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write(
        path,
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def atomic_write_text(path: Path, value: str, *, encoding: str = "utf-8-sig") -> None:
    _atomic_write(path, value, encoding=encoding)


class HistoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_data_dir() / "render_history.json"
        # Multiple queues/components may construct their own HistoryStore for
        # the same path. An instance-local lock would still allow lost updates.
        self._lock = _lock_for_path(self.path)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "renders": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("renders"), list):
                raise ValueError("invalid history")
            return value
        except Exception:
            backup = self.path.with_suffix(".corrupt.json")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return {"version": 1, "renders": []}

    def used_seeds(self) -> set[str]:
        with self._lock:
            return {
                str(item.get("seed"))
                for item in self._load()["renders"]
                if item.get("seed")
            }

    def contains_signature(self, signature: str) -> bool:
        with self._lock:
            return any(
                item.get("manifest_signature") == signature
                for item in self._load()["renders"]
            )

    def contains_output_hash(self, output_hash: str) -> bool:
        if not output_hash:
            return False
        with self._lock:
            return any(
                item.get("output_sha256") == output_hash
                for item in self._load()["renders"]
            )

    def contains_visual_hash(self, visual_hash: str) -> bool:
        if not visual_hash:
            return False
        with self._lock:
            return any(
                item.get("visual_sha256") == visual_hash
                for item in self._load()["renders"]
            )

    def used_grain_seeds(self) -> set[int]:
        with self._lock:
            return {
                int(item["grain_seed"])
                for item in self._load()["renders"]
                if item.get("grain_seed") is not None
            }

    @staticmethod
    def _record(plan: RenderPlan) -> dict[str, Any]:
        return {
            "render_id": plan.render_id,
            "seed": plan.seed,
            "created_at": plan.created_at,
            "manifest_signature": plan.manifest_signature,
            "grain_seed": plan.grain_seed,
            "output_file": plan.output_file,
            "output_sha256": plan.output_sha256,
            "visual_sha256": plan.visual_sha256,
        }

    def add(self, plan: RenderPlan) -> None:
        with self._lock:
            history = self._load()
            history["renders"].append(self._record(plan))
            atomic_write_json(self.path, history)

    def try_add_unique(self, plan: RenderPlan) -> tuple[bool, str]:
        """Atomically reject a duplicate identity/hash or append the render."""
        with self._lock:
            history = self._load()
            renders = history["renders"]
            checks = (
                ("seed", plan.seed, "seed"),
                ("grain_seed", plan.grain_seed, "grain"),
                ("manifest_signature", plan.manifest_signature, "recipe"),
                ("output_sha256", plan.output_sha256, "output"),
                ("visual_sha256", plan.visual_sha256, "visual"),
            )
            for key, value, reason in checks:
                if value not in (None, "") and any(
                    item.get(key) == value for item in renders
                ):
                    return False, reason
            renders.append(self._record(plan))
            atomic_write_json(self.path, history)
            return True, ""


def save_manifest(plan: RenderPlan, video_path: Path) -> Path:
    manifest_path = video_path.with_suffix(".docurandom.json")
    atomic_write_json(manifest_path, plan.to_dict())
    return manifest_path


def sha256_file(
    path: Path,
    chunk_size: int = 1024 * 1024,
    cancel_callback: Callable[[], bool] | None = None,
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            if cancel_callback and cancel_callback():
                raise InterruptedError("hash cancelled")
            digest.update(chunk)
    return digest.hexdigest()
