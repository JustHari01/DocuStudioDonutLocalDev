from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import __version__
from .storage import app_data_dir, atomic_write_json, configured_home_dir


UPDATE_SCHEMA = 1
UPDATE_OWNER = "JustHari01"
UPDATE_REPOSITORY = "DocuRandom-DN"
UPDATE_REPOSITORY_FULL_NAME = f"{UPDATE_OWNER}/{UPDATE_REPOSITORY}"
UPDATE_PUBLIC_KEY_B64 = "Yno9dsGM6aM72qU2iC3lGtdVcovTBHFVArR2KQd6D9U="
UPDATE_MANIFEST_ASSET = "update-manifest.json"
UPDATE_SIGNATURE_ASSET = "update-manifest.sig"
UPDATE_PORTABLE_ASSET = "DocuRandomStudio-portable.zip"
STAGE_RECEIPT_NAME = "staged-receipt.json"
INSTALL_RESULT_NAME = "install-result.json"
APP_EXECUTABLE = "DocuRandomStudio.exe"
UPDATER_EXECUTABLE = "DocuRandomUpdater.exe"
VERSION_MARKER = ".docurandom-version"
MAX_RELEASE_RESPONSE = 2 * 1024 * 1024
MAX_TAG_REFERENCE_RESPONSE = 512 * 1024
MAX_MANIFEST_SIZE = 64 * 1024
MAX_UPDATE_SIZE = 2 * 1024 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
TRANSIENT_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
HTTP_RETRY_DELAYS = (0.5, 1.5)
LEGACY_TOKEN_FILE_NAME = "github-update-token.dpapi"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(RuntimeError):
    pass


class UpdateCancelled(UpdateError):
    pass


@dataclass(frozen=True)
class UpdateAsset:
    asset_id: int
    name: str
    api_url: str
    size: int
    digest: str


@dataclass(frozen=True)
class UpdateRelease:
    version: str
    tag: str
    notes: str
    published_at: str
    manifest: UpdateAsset
    signature: UpdateAsset
    portable: UpdateAsset


@dataclass(frozen=True)
class VerifiedManifest:
    version: str
    asset_name: str
    asset_size: int
    asset_sha256: str
    published_at: str


@dataclass(frozen=True)
class UpdateInstallResult:
    status: str
    target_version: str
    detail: str
    method: str
    updated_at: float


def update_root() -> Path:
    return app_data_dir() / "updates"


def consume_latest_install_result() -> UpdateInstallResult | None:
    """Return and acknowledge the newest terminal updater result.

    The external updater runs after the Tk app has exited.  Persisting its
    terminal result is the only reliable way for the next app process to tell
    the user whether the replacement actually committed.
    """

    root = update_root()
    try:
        resolved_root = root.resolve(strict=True)
        candidates = tuple(root.iterdir())
    except OSError:
        return None
    results: list[tuple[float, Path, UpdateInstallResult]] = []
    for version_dir in candidates:
        if not version_dir.is_dir() or not SEMVER_RE.fullmatch(version_dir.name):
            continue
        result_path = version_dir / INSTALL_RESULT_NAME
        try:
            resolved_dir = version_dir.resolve(strict=True)
            if resolved_dir.parent != resolved_root:
                continue
            value = json.loads(result_path.read_text(encoding="utf-8"))
            status = str(value["status"])
            target_version = str(value["target_version"])
            updated_at = float(value["updated_at"])
            if (
                value.get("schema") != 1
                or status not in {"success", "failed"}
                or target_version != version_dir.name
            ):
                continue
            result = UpdateInstallResult(
                status=status,
                target_version=target_version,
                detail=str(value.get("detail") or "")[:4000],
                method=str(value.get("method") or "")[:80],
                updated_at=updated_at,
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        results.append((updated_at, result_path, result))
    if not results:
        return None
    _updated_at, path, result = max(results, key=lambda item: item[0])
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return result


def _purge_legacy_update_credential() -> None:
    """Remove the obsolete credential path; the shared login lives elsewhere."""

    try:
        (update_root() / LEGACY_TOKEN_FILE_NAME).unlink(missing_ok=True)
    except OSError:
        # A locked obsolete file must not stop the current DPAPI-backed login.
        pass


def _read_json_response(response: object, *, limit: int) -> object:
    raw = getattr(response, "read")(limit + 1)
    if len(raw) > limit:
        raise UpdateError("Phản hồi cập nhật lớn bất thường.")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("Máy chủ trả về dữ liệu cập nhật không hợp lệ.") from exc
    return value


def _public_update_request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    accept: str = "application/vnd.github+json",
) -> urllib.request.Request:
    parsed = urllib.parse.urlsplit(url)
    allowed_prefix = f"/repos/{UPDATE_REPOSITORY_FULL_NAME}/releases"
    tag_reference_path = (
        f"/repos/{UPDATE_REPOSITORY_FULL_NAME}/git/matching-refs/"
        "tags/docurandom-v"
    )
    if (
        parsed.scheme != "https"
        or str(parsed.hostname or "").casefold() != "api.github.com"
        or not (
            parsed.path == allowed_prefix
            or parsed.path.startswith(allowed_prefix + "/")
            or parsed.path == tag_reference_path
        )
    ):
        raise UpdateError("Địa chỉ kênh cập nhật công khai không hợp lệ.")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "DocuRandomStudio-PublicUpdater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    request.method = method
    request.data = data
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    return request


class _PublicUpdateRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow only GitHub's HTTPS hosts while downloading public assets."""

    _DOWNLOAD_HOSTS = {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        parsed = urllib.parse.urlsplit(newurl)
        host = str(parsed.hostname or "").casefold()
        if parsed.scheme != "https" or host not in self._DOWNLOAD_HOSTS:
            raise UpdateError("GitHub chuyển hướng bản cập nhật tới địa chỉ không an toàn.")
        redirected.remove_header("Authorization")
        return redirected


def _open_update_request(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> object:
    opener = urllib.request.build_opener(_PublicUpdateRedirectHandler())
    return opener.open(request, timeout=timeout)


def _urlopen_with_retry(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> object:
    """Retry short-lived update-server failures without hiding permanent errors."""
    attempts = len(HTTP_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            return _open_update_request(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT_HTTP_STATUS or attempt >= attempts - 1:
                raise
            try:
                exc.close()
            except Exception:
                pass
            time.sleep(HTTP_RETRY_DELAYS[attempt])
    raise AssertionError("unreachable")


def _asset_from_json(value: object) -> UpdateAsset | None:
    if not isinstance(value, dict):
        return None
    try:
        asset_id = int(value["id"])
        name = str(value["name"])
        api_url = str(value["url"])
        size = int(value["size"])
    except (KeyError, TypeError, ValueError):
        return None
    digest = str(value.get("digest") or "")
    if digest.startswith("sha256:"):
        digest = digest[7:].casefold()
    else:
        digest = ""
    return UpdateAsset(asset_id, name, api_url, size, digest)


def _release_from_json(value: object, *, expected_tag: str = "") -> UpdateRelease | None:
    if not isinstance(value, dict):
        return None
    if value.get("draft") is True or value.get("prerelease") is True:
        return None
    tag = str(value.get("tag_name") or "")
    if expected_tag and tag != expected_tag:
        return None
    if not tag.startswith("docurandom-v"):
        return None
    version = tag[len("docurandom-v") :]
    if not SEMVER_RE.fullmatch(version):
        return None
    raw_assets = value.get("assets", [])
    if not isinstance(raw_assets, list):
        return None
    assets = {
        asset.name: asset
        for raw in raw_assets
        if (asset := _asset_from_json(raw)) is not None
    }
    if not {
        UPDATE_MANIFEST_ASSET,
        UPDATE_SIGNATURE_ASSET,
        UPDATE_PORTABLE_ASSET,
    }.issubset(assets):
        return None
    return UpdateRelease(
        version=version,
        tag=tag,
        notes=str(value.get("body") or ""),
        published_at=str(value.get("published_at") or ""),
        manifest=assets[UPDATE_MANIFEST_ASSET],
        signature=assets[UPDATE_SIGNATURE_ASSET],
        portable=assets[UPDATE_PORTABLE_ASSET],
    )


def _read_public_update_json(
    url: str,
    *,
    limit: int,
    timeout: float,
) -> object:
    request = _public_update_request(url)
    try:
        with _urlopen_with_retry(request, timeout=timeout) as response:
            return _read_json_response(response, limit=limit)
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"Máy chủ cập nhật trả về lỗi HTTP {exc.code}.") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError(f"Không thể kiểm tra bản cập nhật: {exc}") from exc


def get_latest_release(*, timeout: float = 20.0) -> UpdateRelease:
    """Resolve the newest DocuRandom release without downloading the full catalog.

    The distribution repository also contains Donut releases.  The old updater
    requested up to 100 complete release records, whose repeated notes and
    asset metadata eventually exceeded its bounded 2 MiB response.  Git refs
    are compact and contain no release bodies, so select the highest semantic
    DocuRandom tag first and then fetch exactly that one release.
    """

    _purge_legacy_update_credential()
    refs_url = (
        f"https://api.github.com/repos/{UPDATE_REPOSITORY_FULL_NAME}/"
        "git/matching-refs/tags/docurandom-v"
    )
    payload = _read_public_update_json(
        refs_url,
        limit=MAX_TAG_REFERENCE_RESPONSE,
        timeout=timeout,
    )
    if not isinstance(payload, list):
        raise UpdateError("Máy chủ trả về danh sách phiên bản cập nhật không hợp lệ.")
    versions: list[tuple[tuple[int, int, int], str]] = []
    prefix = "refs/tags/docurandom-v"
    for value in payload:
        if not isinstance(value, dict):
            continue
        ref = str(value.get("ref") or "")
        if not ref.startswith(prefix):
            continue
        version = ref[len(prefix) :]
        if not SEMVER_RE.fullmatch(version):
            continue
        versions.append((version_tuple(version), f"docurandom-v{version}"))
    if not versions:
        raise UpdateError("Kênh public chưa có phiên bản DocuRandom Studio hợp lệ.")
    _version_key, tag = max(versions, key=lambda item: item[0])
    release_url = (
        f"https://api.github.com/repos/{UPDATE_REPOSITORY_FULL_NAME}/"
        f"releases/tags/{urllib.parse.quote(tag, safe='')}"
    )
    release_payload = _read_public_update_json(
        release_url,
        limit=MAX_RELEASE_RESPONSE,
        timeout=timeout,
    )
    release = _release_from_json(release_payload, expected_tag=tag)
    if release is None:
        raise UpdateError(
            "Bản DocuRandom Studio mới nhất chưa có đủ gói cập nhật đã ký."
        )
    return release


def _download_asset_bytes(
    asset: UpdateAsset,
    *,
    limit: int,
    timeout: float = 30.0,
) -> bytes:
    request = _public_update_request(
        asset.api_url,
        accept="application/octet-stream",
    )
    try:
        with _urlopen_with_retry(request, timeout=timeout) as response:
            raw = response.read(limit + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError(f"Không thể tải {asset.name}: {exc}") from exc
    if len(raw) > limit:
        raise UpdateError(f"{asset.name} lớn bất thường.")
    return raw


def verify_release_manifest(release: UpdateRelease) -> VerifiedManifest:
    manifest_bytes = _download_asset_bytes(
        release.manifest,
        limit=MAX_MANIFEST_SIZE,
    )
    signature = _download_asset_bytes(release.signature, limit=1024)
    try:
        public_key_raw = base64.b64decode(UPDATE_PUBLIC_KEY_B64, validate=True)
        Ed25519PublicKey.from_public_bytes(public_key_raw).verify(
            signature,
            manifest_bytes,
        )
    except (ValueError, InvalidSignature) as exc:
        raise UpdateError("Chữ ký release không hợp lệ; đã từ chối cập nhật.") from exc
    try:
        value = json.loads(manifest_bytes.decode("utf-8"))
        asset = value["asset"]
        verified = VerifiedManifest(
            version=str(value["version"]),
            asset_name=str(asset["name"]),
            asset_size=int(asset["size"]),
            asset_sha256=str(asset["sha256"]).casefold(),
            published_at=str(value.get("published_at") or ""),
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("Manifest cập nhật không hợp lệ.") from exc
    if value.get("schema") != UPDATE_SCHEMA:
        raise UpdateError("Manifest dùng phiên bản schema không được hỗ trợ.")
    if not SEMVER_RE.fullmatch(verified.version) or verified.version != release.version:
        raise UpdateError("Phiên bản manifest không khớp release.")
    if verified.asset_name != UPDATE_PORTABLE_ASSET:
        raise UpdateError("Manifest trỏ tới tên gói không hợp lệ.")
    if not 1 <= verified.asset_size <= MAX_UPDATE_SIZE:
        raise UpdateError("Kích thước gói cập nhật không hợp lệ.")
    if verified.asset_size != release.portable.size:
        raise UpdateError("Kích thước gói trên máy chủ không khớp manifest.")
    if not SHA256_RE.fullmatch(verified.asset_sha256):
        raise UpdateError("SHA-256 trong manifest không hợp lệ.")
    if release.portable.digest and release.portable.digest != verified.asset_sha256:
        raise UpdateError("Digest máy chủ không khớp manifest đã ký.")
    return verified


def version_tuple(value: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(value)
    if not match:
        raise UpdateError(f"Phiên bản không hợp lệ: {value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def update_is_newer(version: str, current: str = __version__) -> bool:
    return version_tuple(version) > version_tuple(current)


def download_portable_update(
    release: UpdateRelease,
    manifest: VerifiedManifest,
    *,
    cancel_event: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    root = update_root() / release.version
    root.mkdir(parents=True, exist_ok=True)
    destination = root / UPDATE_PORTABLE_ASSET
    temporary = root / f".{UPDATE_PORTABLE_ASSET}.{secrets.token_hex(6)}.part"
    if destination.is_file():
        try:
            if destination.stat().st_size == manifest.asset_size:
                cached_digest = hashlib.sha256()
                cached_size = 0
                with destination.open("rb") as cached:
                    while chunk := cached.read(DOWNLOAD_CHUNK_SIZE):
                        if cancel_event is not None and cancel_event.is_set():
                            raise UpdateCancelled("Đã hủy xác minh bản cập nhật đã tải.")
                        cached_digest.update(chunk)
                        cached_size += len(chunk)
                        if progress is not None:
                            progress(cached_size, manifest.asset_size)
                if cached_digest.hexdigest().casefold() == manifest.asset_sha256:
                    return destination
        except OSError:
            pass
    request = _public_update_request(
        release.portable.api_url,
        accept="application/octet-stream",
    )
    digest = hashlib.sha256()
    received = 0
    try:
        with _urlopen_with_retry(request, timeout=120.0) as response, temporary.open("wb") as output:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise UpdateCancelled("Đã hủy tải bản cập nhật.")
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                received += len(chunk)
                if received > manifest.asset_size:
                    raise UpdateError("Gói tải về lớn hơn manifest đã ký.")
                digest.update(chunk)
                output.write(chunk)
                if progress is not None:
                    progress(received, manifest.asset_size)
        if received != manifest.asset_size:
            raise UpdateError("Gói cập nhật tải chưa đủ dữ liệu.")
        if digest.hexdigest().casefold() != manifest.asset_sha256:
            raise UpdateError("SHA-256 gói cập nhật không khớp; đã từ chối cài đặt.")
        os.replace(temporary, destination)
        return destination
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_zip_member(member: zipfile.ZipInfo) -> PurePosixPath:
    name = member.filename.replace("\\", "/")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise UpdateError(f"Gói cập nhật chứa đường dẫn nguy hiểm: {member.filename}")
    mode = member.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise UpdateError("Gói cập nhật không được chứa symbolic link.")
    return path


def cached_portable_update_exists(
    version: str,
    manifest: VerifiedManifest,
) -> bool:
    """Return whether a complete-size archive is already on disk.

    This is only a UI hint. ``download_portable_update`` still hashes every
    byte against the signed manifest before the archive may be used.
    """

    if manifest.version != version:
        return False
    candidate = update_root() / version / UPDATE_PORTABLE_ASSET
    try:
        return candidate.is_file() and candidate.stat().st_size == manifest.asset_size
    except OSError:
        return False


def _find_staged_app(stage_parent: Path) -> Path | None:
    try:
        resolved_parent = stage_parent.resolve(strict=True)
        candidates = [
            executable.parent.resolve(strict=True)
            for executable in stage_parent.rglob(APP_EXECUTABLE)
            if executable.is_file()
        ]
    except OSError:
        return None
    if len(candidates) != 1:
        return None
    app_root = candidates[0]
    if resolved_parent not in app_root.parents:
        return None
    return app_root


def find_reusable_staged_update(
    version: str,
    manifest: VerifiedManifest,
) -> Path | None:
    """Recover a previously verified and fully extracted portable update."""

    if manifest.version != version:
        return None
    root = update_root() / version
    receipt_path = root / STAGE_RECEIPT_NAME
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            return None
        if (
            receipt.get("schema") != 1
            or receipt.get("version") != version
            or receipt.get("asset_size") != manifest.asset_size
            or receipt.get("asset_sha256") != manifest.asset_sha256
        ):
            return None
        if not cached_portable_update_exists(version, manifest):
            return None
        app_root = _find_staged_app(root / "staged")
        if app_root is None:
            return None
        if not (app_root / UPDATER_EXECUTABLE).is_file():
            return None
        if (app_root / VERSION_MARKER).read_text(encoding="ascii").strip() != version:
            return None
        return app_root
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None


def stage_portable_update(
    zip_path: Path,
    version: str,
    manifest: VerifiedManifest | None = None,
) -> Path:
    if not update_is_newer(version):
        raise UpdateError("Bản tải về không mới hơn bản đang chạy.")
    root = update_root() / version
    if manifest is not None:
        reusable = find_reusable_staged_update(version, manifest)
        if reusable is not None:
            return reusable
    stage_parent = root / "staged"
    receipt_path = root / STAGE_RECEIPT_NAME
    try:
        receipt_path.unlink(missing_ok=True)
    except OSError:
        pass
    if stage_parent.exists():
        resolved = stage_parent.resolve()
        if resolved.parent != root.resolve():
            raise UpdateError("Thư mục staging cập nhật không an toàn.")
        shutil.rmtree(resolved)
    stage_parent.mkdir(parents=True)
    total = 0
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            _safe_zip_member(member)
            total += max(0, member.file_size)
            if total > MAX_UPDATE_SIZE * 2:
                raise UpdateError("Gói cập nhật giải nén lớn bất thường.")
        archive.extractall(stage_parent)
    app_root = _find_staged_app(stage_parent)
    if app_root is None:
        raise UpdateError("Không xác định được thư mục ứng dụng trong gói cập nhật.")
    if manifest is not None:
        if manifest.version != version:
            raise UpdateError("Phiên bản staging không khớp manifest đã ký.")
        atomic_write_json(
            receipt_path,
            {
                "schema": 1,
                "version": version,
                "asset_size": manifest.asset_size,
                "asset_sha256": manifest.asset_sha256,
                "created_at": time.time(),
            },
        )
    return app_root


def updater_executable() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    candidate = Path(sys.executable).resolve().parent / UPDATER_EXECUTABLE
    return candidate if candidate.is_file() else None


def create_update_plan(staged_app: Path, version: str) -> tuple[Path, str]:
    if not getattr(sys, "frozen", False):
        raise UpdateError("Chỉ cài cập nhật tự động từ bản portable đã đóng gói.")
    install_dir = Path(sys.executable).resolve().parent
    root = update_root() / version
    staged_root = staged_app.resolve(strict=True)
    helper = staged_root / UPDATER_EXECUTABLE
    if not helper.is_file():
        raise UpdateError("Bản cập nhật portable thiếu DocuRandomUpdater.exe.")
    if not (staged_root / APP_EXECUTABLE).is_file():
        raise UpdateError("Bản cập nhật portable thiếu ứng dụng chính.")
    try:
        staged_version = (staged_root / VERSION_MARKER).read_text(encoding="ascii").strip()
    except OSError as exc:
        raise UpdateError("Bản cập nhật portable thiếu dấu phiên bản.") from exc
    if staged_version != version:
        raise UpdateError("Dấu phiên bản trong bản cập nhật không khớp.")
    helper_copy = root / UPDATER_EXECUTABLE
    # Use the helper shipped in the signed target build. This lets a release
    # repair its own installation path instead of being constrained forever by
    # the updater implementation bundled with the previous version.
    shutil.copy2(helper, helper_copy)
    health_token = secrets.token_urlsafe(32)
    plan_path = root / "install-plan.json"
    health_path = root / "health.json"
    atomic_write_json(
        plan_path,
        {
            "schema": 1,
            "version": version,
            "current_pid": os.getpid(),
            "install_dir": str(install_dir),
            "staged_dir": str(staged_app.resolve()),
            "executable_name": "DocuRandomStudio.exe",
            "health_path": str(health_path),
            "health_token": health_token,
            "source_version": __version__,
        },
    )
    return plan_path, health_token


def launch_update_helper(plan_path: Path) -> None:
    helper = plan_path.parent / UPDATER_EXECUTABLE
    if not helper.is_file():
        raise UpdateError("Thiếu tiến trình cài cập nhật đã xác minh.")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    environment = os.environ.copy()
    managed_home = configured_home_dir()
    if managed_home is not None:
        environment["DOCURANDOM_HOME"] = str(managed_home)
    subprocess.Popen(
        [str(helper), "--plan", str(plan_path.resolve())],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        close_fds=True,
        creationflags=creationflags,
    )


def prune_old_update_payloads(current: str = __version__) -> None:
    """Remove completed payload directories older than the running build."""

    root = update_root()
    try:
        resolved_root = root.resolve(strict=True)
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir() or not SEMVER_RE.fullmatch(entry.name):
            continue
        try:
            if version_tuple(entry.name) >= version_tuple(current):
                continue
            resolved = entry.resolve(strict=True)
            if resolved.parent != resolved_root:
                continue
            shutil.rmtree(resolved)
        except (OSError, UpdateError):
            continue


def mark_update_healthy_from_argv(argv: list[str] | None = None) -> bool:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--update-health" not in args:
        return False
    index = args.index("--update-health")
    if index + 2 >= len(args):
        return False
    path = Path(args[index + 1]).expanduser().resolve()
    token = args[index + 2]
    expected_root = update_root().resolve()
    if expected_root not in path.parents or len(token) < 32:
        return False
    atomic_write_json(
        path,
        {"ok": True, "version": __version__, "token": token, "pid": os.getpid()},
    )
    return True
