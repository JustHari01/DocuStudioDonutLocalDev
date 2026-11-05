from __future__ import annotations

import base64
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from .donut_mcp import (
    DonutMcpClient,
    DonutMcpEndpoint,
    DonutMcpError,
    load_donut_mcp_endpoint,
    save_donut_mcp_endpoint,
)
from .storage import app_data_dir, atomic_write_json, configured_home_dir
from .updater import UPDATE_PUBLIC_KEY_B64
from .private_github import (
    PRIVATE_DONUT_REPOSITORY,
    PrivateGitHubError,
    load_private_github_token,
    private_github_request,
)


logger = logging.getLogger(__name__)

DONUT_DISTRIBUTION_REPOSITORY = PRIVATE_DONUT_REPOSITORY
DONUT_RELEASE_API = (
    "https://api.github.com/repos/"
    f"{DONUT_DISTRIBUTION_REPOSITORY}/releases"
)
DONUT_SOURCE_URL = "https://github.com/zhom/donutbrowser"
DONUT_INSTALLER_REPOSITORY = "JustHari01/DocuRandom-DN"
DONUT_INSTALLER_RELEASE_API = (
    f"https://api.github.com/repos/{DONUT_INSTALLER_REPOSITORY}/releases"
)
DONUT_DEFAULT_MCP_PORT = 51080
DONUT_DEFAULT_API_PORT = 10108
DONUT_VAULT_PASSWORD = b"donutbrowser-api-vault-password"
DONUT_LOCAL_SERVER_NAME = "donut-browser-local"
DONUT_REQUIRED_LOCAL_TOOLS = frozenset(
    {
        "list_profiles",
        "run_profile",
        "navigate",
        "evaluate_javascript",
        "get_debug_connection",
        "cdp_command",
    }
)
MANAGED_MANIFEST = "donut-local-manifest.json"
MANAGED_SIGNATURE = "donut-local-manifest.sig"
INSTALLER_MANIFEST = "donut-installer.json"
SOURCE_MANIFEST = "donut-source.json"
SOURCE_BUNDLE = "donutbrowser-local.bundle"
DONUT_LOCAL_UNINSTALL_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\DocuRandom-DN"
)
DONUT_LOCAL_EXECUTABLE = "donutbrowser.exe"
DONUT_LOCAL_ORIGIN = "https://github.com/JustHari01/donutbrowser-local.git"
DONUT_LOCAL_UPSTREAM = "https://github.com/zhom/donutbrowser.git"
INSTALL_RECEIPT = "DOCURANDOM_INSTALL.json"
MAX_CATALOG_SIZE = 4 * 1024 * 1024
MAX_MANIFEST_SIZE = 64 * 1024
MAX_DONUT_ARCHIVE_SIZE = 2 * 1024 * 1024 * 1024
ProgressCallback = Callable[[float, str], None]


class DonutManagedError(RuntimeError):
    pass


@dataclass(frozen=True)
class DonutRelease:
    version: str
    revision: int
    tag: str
    notes: str
    archive_name: str
    archive_url: str
    archive_size: int
    manifest_url: str
    signature_url: str
    sha256: str = ""
    upstream_tag: str = ""

    @property
    def version_key(self) -> str:
        return f"{self.version}-r{self.revision}"

    @property
    def display_version(self) -> str:
        return f"{self.version} r{self.revision}"


@dataclass(frozen=True)
class DonutInstall:
    version: str
    revision: int
    root: Path
    executable: Path

    @property
    def version_key(self) -> str:
        return f"{self.version}-r{self.revision}" if self.revision > 0 else self.version

    @property
    def display_version(self) -> str:
        return f"{self.version} r{self.revision}" if self.revision > 0 else self.version


@dataclass(frozen=True)
class DonutInstallerPayload:
    version: str
    path: Path
    size: int
    sha256: str

    @property
    def display_version(self) -> str:
        return self.version

    @property
    def notes(self) -> str:
        return "Installer Donut Local được đóng kèm trong DocuRandom Studio."


@dataclass(frozen=True)
class DonutInstallerRelease:
    version: str
    tag: str
    notes: str
    asset_name: str
    asset_url: str
    asset_size: int
    sha256: str

    @property
    def display_version(self) -> str:
        return self.version


@dataclass(frozen=True)
class DonutSourcePayload:
    commit: str
    branch: str
    origin: str
    upstream: str
    path: Path
    size: int
    sha256: str


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value or "").strip())
    if match is None:
        raise DonutManagedError(f"Phiên bản Donut không hợp lệ: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _release_key(value: DonutRelease | DonutInstall) -> tuple[int, int, int, int]:
    return (*_version_tuple(value.version), int(value.revision))


class _PrivateGitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow signed release redirects without leaking the GitHub credential."""

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
            raise DonutManagedError("GitHub chuyển hướng gói Donut tới địa chỉ không an toàn.")
        if host != "api.github.com":
            redirected.remove_header("Authorization")
        return redirected


def _open_private_github(
    url: str,
    token: str,
    *,
    timeout: float,
    accept: str,
) -> object:
    request = private_github_request(url, token, accept=accept)
    opener = urllib.request.build_opener(_PrivateGitHubRedirectHandler())
    return opener.open(request, timeout=timeout)


def _request_bytes(
    url: str,
    *,
    token: str,
    timeout: float = 45.0,
    limit: int = MAX_CATALOG_SIZE,
    accept: str = "application/vnd.github+json",
) -> bytes:
    try:
        with _open_private_github(
            url,
            token,
            timeout=timeout,
            accept=accept,
        ) as response:
            payload = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            message = "Đăng nhập GitHub cho Donut Local đã hết hạn hoặc chưa đủ quyền."
        elif exc.code == 404:
            message = "Tài khoản GitHub chưa được cấp quyền vào Donut Local private."
        else:
            message = f"GitHub trả lỗi HTTP {exc.code} khi tải Donut Local."
        raise DonutManagedError(message) from exc
    except PrivateGitHubError as exc:
        raise DonutManagedError(str(exc)) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise DonutManagedError("Không kết nối được máy chủ cập nhật Donut Local.") from exc
    if len(payload) > limit:
        raise DonutManagedError("Máy chủ trả dữ liệu Donut Local lớn bất thường.")
    return payload


def _request_public_github_bytes(
    url: str,
    *,
    timeout: float = 45.0,
    limit: int = MAX_CATALOG_SIZE,
    accept: str = "application/vnd.github+json",
) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise DonutManagedError("Địa chỉ cập nhật Donut Local không an toàn.")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "DocuRandomStudio-DonutUpdater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = urllib.request.build_opener(_PrivateGitHubRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 429}:
            message = "GitHub đang giới hạn lượt kiểm tra cập nhật Donut Local."
        elif exc.code == 404:
            message = "Chưa tìm thấy kênh Release DocuRandom-DN trên GitHub."
        else:
            message = f"GitHub trả lỗi HTTP {exc.code} khi kiểm tra Donut Local."
        raise DonutManagedError(message) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise DonutManagedError(
            "Không kết nối được kênh cập nhật Donut Local trên GitHub."
        ) from exc
    if len(payload) > limit:
        raise DonutManagedError("GitHub trả dữ liệu cập nhật Donut Local lớn bất thường.")
    return payload


def parse_installer_releases(payload: bytes | str) -> DonutInstallerRelease:
    """Select the newest stable DocuRandom-DN NSIS asset with GitHub SHA-256."""

    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise DonutManagedError("GitHub trả danh sách installer Donut Local không hợp lệ.") from exc
    releases = value if isinstance(value, list) else [value]
    candidates: list[DonutInstallerRelease] = []
    for raw in releases:
        if (
            not isinstance(raw, dict)
            or bool(raw.get("draft"))
            or bool(raw.get("prerelease"))
        ):
            continue
        tag = str(raw.get("tag_name") or "").strip()
        for asset in raw.get("assets") or ():
            if not isinstance(asset, dict) or str(asset.get("state") or "") != "uploaded":
                continue
            name = str(asset.get("name") or "").strip()
            match = re.fullmatch(
                r"DocuRandom-DN_(\d+\.\d+\.\d+)_x64-setup\.exe",
                name,
                flags=re.IGNORECASE,
            )
            if match is None:
                continue
            version = match.group(1)
            try:
                size = int(asset.get("size") or 0)
            except (TypeError, ValueError):
                continue
            digest_value = str(asset.get("digest") or "").strip().casefold()
            digest_match = re.fullmatch(r"sha256:([0-9a-f]{64})", digest_value)
            url = str(asset.get("url") or "").strip()
            expected_prefix = (
                f"https://api.github.com/repos/{DONUT_INSTALLER_REPOSITORY}/"
                "releases/assets/"
            )
            if (
                digest_match is None
                or not 1 <= size <= MAX_DONUT_ARCHIVE_SIZE
                or not url.startswith(expected_prefix)
            ):
                continue
            candidates.append(
                DonutInstallerRelease(
                    version=version,
                    tag=tag,
                    notes=str(raw.get("body") or "").strip(),
                    asset_name=name,
                    asset_url=url,
                    asset_size=size,
                    sha256=digest_match.group(1),
                )
            )
    if not candidates:
        raise DonutManagedError(
            "Chưa có Release ổn định chứa installer DocuRandom-DN_X.Y.Z_x64-setup.exe "
            "và digest SHA-256 trên GitHub."
        )
    newest_version = max(_version_tuple(item.version) for item in candidates)
    newest = [item for item in candidates if _version_tuple(item.version) == newest_version]
    identities = {(item.asset_size, item.sha256) for item in newest}
    if len(identities) != 1:
        raise DonutManagedError(
            "GitHub có nhiều installer khác nhau cho cùng phiên bản Donut Local mới nhất."
        )
    return newest[0]


def latest_installer_release() -> DonutInstallerRelease:
    payload = _request_public_github_bytes(
        f"{DONUT_INSTALLER_RELEASE_API}?per_page=100&page=1"
    )
    return parse_installer_releases(payload)


def _release_asset(release: dict[str, object], name: str) -> tuple[str, int] | None:
    for raw in release.get("assets") or ():
        if not isinstance(raw, dict) or str(raw.get("name") or "").casefold() != name.casefold():
            continue
        url = str(raw.get("url") or "").strip()
        try:
            size = int(raw.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if url:
            return url, size
    return None


def parse_release_payload(payload: bytes | str) -> DonutRelease:
    """Select the newest immutable Donut Local release from the private catalog."""

    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise DonutManagedError("Máy chủ trả danh sách Donut Local không hợp lệ.") from exc
    releases = value if isinstance(value, list) else [value]
    candidates: list[DonutRelease] = []
    for raw in releases:
        # Component releases are deliberately marked prerelease on GitHub so
        # they never replace the main DocuRandom app in /releases/latest.
        # Their strict tag, required assets and signed manifest form the
        # independent stable channel for Donut Local.
        if not isinstance(raw, dict) or bool(raw.get("draft")):
            continue
        tag = str(raw.get("tag_name") or "").strip()
        match = re.fullmatch(r"donut-local-v(\d+\.\d+\.\d+)-r([1-9]\d*)", tag)
        if match is None:
            continue
        version = match.group(1)
        revision = int(match.group(2))
        archive_name = f"DonutLocal_{version}_r{revision}_x64-portable.zip"
        archive = _release_asset(raw, archive_name)
        manifest = _release_asset(raw, MANAGED_MANIFEST)
        signature = _release_asset(raw, MANAGED_SIGNATURE)
        if archive is None or manifest is None or signature is None:
            continue
        candidates.append(
            DonutRelease(
                version=version,
                revision=revision,
                tag=tag,
                notes=str(raw.get("body") or "").strip(),
                archive_name=archive_name,
                archive_url=archive[0],
                archive_size=archive[1],
                manifest_url=manifest[0],
                signature_url=signature[0],
            )
        )
    if not candidates:
        raise DonutManagedError("Chưa có bản Donut Local ổn định đầy đủ trên máy chủ.")
    return max(candidates, key=_release_key)


def _verify_manifest_bytes(
    release: DonutRelease,
    manifest_bytes: bytes,
    signature: bytes,
) -> DonutRelease:
    try:
        public_key_raw = base64.b64decode(UPDATE_PUBLIC_KEY_B64, validate=True)
        Ed25519PublicKey.from_public_bytes(public_key_raw).verify(signature, manifest_bytes)
    except (ValueError, InvalidSignature) as exc:
        raise DonutManagedError(
            "Chữ ký Donut Local không hợp lệ; đã từ chối cài đặt."
        ) from exc
    try:
        value = json.loads(manifest_bytes.decode("utf-8"))
        asset = value["asset"]
        version = str(value["version"])
        revision = int(value["revision"])
        asset_name = str(asset["name"])
        asset_size = int(asset["size"])
        digest = str(asset["sha256"]).casefold()
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DonutManagedError("Manifest Donut Local không hợp lệ.") from exc
    if value.get("schema") != 1 or value.get("component") != "donut-local":
        raise DonutManagedError("Manifest không phải thành phần Donut Local được hỗ trợ.")
    if version != release.version or revision != release.revision:
        raise DonutManagedError("Phiên bản Donut Local trong manifest không khớp release.")
    if str(value.get("local_version") or "") != f"{version}.{revision}":
        raise DonutManagedError("Mã phiên bản Donut Local trong manifest không hợp lệ.")
    if asset_name != release.archive_name:
        raise DonutManagedError("Manifest Donut Local trỏ tới sai tên gói.")
    if not 1 <= asset_size <= MAX_DONUT_ARCHIVE_SIZE:
        raise DonutManagedError("Kích thước Donut Local trong manifest không hợp lệ.")
    if release.archive_size and asset_size != release.archive_size:
        raise DonutManagedError("Kích thước Donut Local trên máy chủ không khớp manifest.")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise DonutManagedError("SHA-256 Donut Local trong manifest không hợp lệ.")
    if value.get("entitlement_policy") != "upstream-real-account":
        raise DonutManagedError("Chính sách tài khoản Donut Local không hợp lệ.")
    upstream_tag = str(value.get("upstream_tag") or "").strip()
    if upstream_tag != f"v{version}":
        raise DonutManagedError("Tag nguồn Donut chính thức không khớp phiên bản Local.")
    return replace(
        release,
        archive_size=asset_size,
        sha256=digest,
        upstream_tag=upstream_tag,
    )


def verify_local_release(release: DonutRelease, *, token: str) -> DonutRelease:
    manifest = _request_bytes(
        release.manifest_url,
        token=token,
        limit=MAX_MANIFEST_SIZE,
        accept="application/octet-stream",
    )
    signature = _request_bytes(
        release.signature_url,
        token=token,
        limit=1024,
        accept="application/octet-stream",
    )
    return _verify_manifest_bytes(release, manifest, signature)


def latest_local_release(*, token: str | None = None) -> DonutRelease:
    credential = token or load_private_github_token()
    if not credential:
        raise DonutManagedError(
            "Chưa đăng nhập GitHub cho Donut Local Developer. "
            "Hãy mở cài đặt Donut và lưu mã truy cập private."
        )
    releases: list[object] = []
    for page in range(1, 6):
        payload = _request_bytes(
            f"{DONUT_RELEASE_API}?per_page=100&page={page}",
            token=credential,
        )
        try:
            batch = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DonutManagedError("Máy chủ trả danh sách Donut Local không hợp lệ.") from exc
        if not isinstance(batch, list):
            raise DonutManagedError("Máy chủ trả danh sách Donut Local không hợp lệ.")
        releases.extend(batch)
        if len(batch) < 100:
            break
    return verify_local_release(
        parse_release_payload(json.dumps(releases, ensure_ascii=False)),
        token=credential,
    )


def managed_root() -> Path:
    home = configured_home_dir()
    root = (
        home / "Components" / "DonutLocal"
        if home is not None
        else app_data_dir() / "components" / "DonutLocal"
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def official_data_root() -> Path:
    portable_root = os.environ.get("DOCURANDOM_DONUT_DATA_ROOT", "").strip()
    if portable_root:
        try:
            resolved = Path(portable_root).expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise DonutManagedError(
                "Không xác định được thư mục dữ liệu Donut portable."
            ) from exc
        if not resolved.is_absolute() or resolved == Path(resolved.anchor):
            raise DonutManagedError(
                "Thư mục dữ liệu Donut portable phải là đường dẫn tuyệt đối an toàn."
            )
        return resolved
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "DonutBrowser"


def bundled_component_root() -> Path:
    runtime_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return runtime_root / "donut-managed"


def bundled_installer_root() -> Path:
    runtime_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return runtime_root / "donut-installer"


def bundled_source_root() -> Path:
    runtime_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return runtime_root / "donut-source"


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def bundled_release(root: Path | None = None) -> tuple[DonutRelease, Path] | None:
    package_root = root or bundled_component_root()
    manifest_path = package_root / MANAGED_MANIFEST
    signature_path = package_root / MANAGED_SIGNATURE
    try:
        manifest_bytes = manifest_path.read_bytes()
        signature = signature_path.read_bytes()
        value = json.loads(manifest_bytes.decode("utf-8"))
        version = str(value["version"])
        revision = int(value["revision"])
        archive_name = str(value["asset"]["name"])
        archive_size = int(value["asset"]["size"])
    except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    release = DonutRelease(
        version=version,
        revision=revision,
        tag=f"donut-local-v{version}-r{revision}",
        notes="Bản Donut Local đóng kèm DocuRandom Studio.",
        archive_name=archive_name,
        archive_url="",
        archive_size=archive_size,
        manifest_url="",
        signature_url="",
    )
    try:
        release = _verify_manifest_bytes(release, manifest_bytes, signature)
    except DonutManagedError:
        return None
    archive = package_root / release.archive_name
    if not archive.is_file() or archive.stat().st_size != release.archive_size:
        return None
    return release, archive


def bundled_installer(root: Path | None = None) -> DonutInstallerPayload | None:
    package_root = root or bundled_installer_root()
    value = _read_json(package_root / INSTALLER_MANIFEST)
    try:
        asset = value["asset"]
        version = str(value["version"])
        name = str(asset["name"])
        size = int(asset["size"])
        digest = str(asset["sha256"]).casefold()
        _version_tuple(version)
    except (KeyError, TypeError, ValueError, DonutManagedError):
        return None
    if (
        value.get("schema") != 1
        or value.get("component") != "donut-local-installer"
        or not re.fullmatch(
            rf"DocuRandom-DN_{re.escape(version)}_x64-setup\.exe",
            name,
            flags=re.IGNORECASE,
        )
        or not 1 <= size <= MAX_DONUT_ARCHIVE_SIZE
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        return None
    installer = package_root / name
    if not installer.is_file() or installer.stat().st_size != size:
        return None
    if _sha256(installer) != digest:
        return None
    return DonutInstallerPayload(version, installer, size, digest)


def bundled_source(root: Path | None = None) -> DonutSourcePayload | None:
    package_root = root or bundled_source_root()
    value = _read_json(package_root / SOURCE_MANIFEST)
    try:
        asset = value["asset"]
        commit = str(value["commit"]).casefold()
        branch = str(value["branch"])
        origin = str(value["origin"])
        upstream = str(value["upstream"])
        name = str(asset["name"])
        size = int(asset["size"])
        digest = str(asset["sha256"]).casefold()
    except (KeyError, TypeError, ValueError):
        return None
    if (
        value.get("schema") != 1
        or value.get("component") != "donut-local-source"
        or not re.fullmatch(r"[0-9a-f]{40}", commit)
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
        or origin != DONUT_LOCAL_ORIGIN
        or upstream != DONUT_LOCAL_UPSTREAM
        or name != SOURCE_BUNDLE
        or not 1 <= size <= MAX_DONUT_ARCHIVE_SIZE
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        return None
    bundle = package_root / name
    if not bundle.is_file() or bundle.stat().st_size != size:
        return None
    if _sha256(bundle) != digest:
        return None
    return DonutSourcePayload(commit, branch, origin, upstream, bundle, size, digest)


def installed_local_app() -> DonutInstall | None:
    """Return only the installed DocuRandom-DN app registered for this user."""

    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, DONUT_LOCAL_UNINSTALL_KEY) as key:
            version = str(winreg.QueryValueEx(key, "DisplayVersion")[0]).strip()
            raw_location = os.path.expandvars(
                str(winreg.QueryValueEx(key, "InstallLocation")[0]).strip()
            )
        _version_tuple(version)
        location = Path(raw_location.strip().strip('"')).resolve(strict=False)
        executable = (location / DONUT_LOCAL_EXECUTABLE).resolve(strict=False)
        executable.relative_to(location)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, DonutManagedError):
        return None
    if not executable.is_file() or executable.name.casefold() != DONUT_LOCAL_EXECUTABLE:
        return None
    return DonutInstall(version, 0, location, executable)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination_resolved = destination.resolve(strict=False)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            relative = Path(member.filename.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                raise DonutManagedError("Gói Donut Local chứa đường dẫn không an toàn.")
            target = (destination / relative).resolve(strict=False)
            try:
                target.relative_to(destination_resolved)
            except ValueError as exc:
                raise DonutManagedError(
                    "Gói Donut Local cố ghi ra ngoài thư mục Components."
                ) from exc
        bundle.extractall(destination)


def _find_donut_executable(root: Path) -> Path:
    candidates = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.name.casefold() == "donutlocal.exe"
    ]
    if len(candidates) != 1:
        raise DonutManagedError(
            "Không xác định được duy nhất DonutLocal.exe trong gói đã ký."
        )
    return candidates[0]


def _write_install_receipt(
    target: Path,
    release: DonutRelease,
    executable: Path,
) -> None:
    atomic_write_json(
        target / INSTALL_RECEIPT,
        {
            "schema": 1,
            "component": "donut-local",
            "version": release.version,
            "revision": release.revision,
            "archive_sha256": release.sha256,
            "executable": str(executable.relative_to(target)),
            "executable_size": executable.stat().st_size,
            "executable_sha256": _sha256(executable),
        },
    )


def _validated_install_receipt(
    target: Path,
    *,
    version: str,
    revision: int,
    archive_sha256: str = "",
) -> DonutInstall | None:
    receipt = _read_json(target / INSTALL_RECEIPT)
    try:
        if (
            receipt.get("schema") != 1
            or receipt.get("component") != "donut-local"
            or str(receipt.get("version") or "") != version
            or int(receipt.get("revision") or 0) != revision
        ):
            return None
        if archive_sha256 and (
            str(receipt.get("archive_sha256") or "").casefold()
            != archive_sha256.casefold()
        ):
            return None
        relative = str(receipt.get("executable") or "").strip()
        executable = (target / relative).resolve(strict=False)
        executable.relative_to(target.resolve(strict=False))
        if executable.name.casefold() != "donutlocal.exe" or not executable.is_file():
            return None
        if executable.stat().st_size != int(receipt.get("executable_size") or -1):
            return None
        if _sha256(executable) != str(receipt.get("executable_sha256") or "").casefold():
            return None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return DonutInstall(version, revision, executable.parent, executable)


def _server_name(info: dict[str, object]) -> str:
    server = info.get("serverInfo")
    return str(server.get("name") or "").strip() if isinstance(server, dict) else ""


class DonutManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or managed_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def versions_root(self) -> Path:
        return self.root / "versions"

    @property
    def current_file(self) -> Path:
        return self.root / "current.json"

    def current_install(self) -> DonutInstall | None:
        value = _read_json(self.current_file)
        version = str(value.get("version") or "").strip()
        relative = str(value.get("executable") or "").strip()
        try:
            revision = int(value.get("revision") or 0)
            _version_tuple(version)
            if revision < 1 or not relative:
                return None
            executable = (self.root / relative).resolve(strict=False)
            executable.relative_to(self.root.resolve(strict=False))
        except (DonutManagedError, OSError, RuntimeError, TypeError, ValueError):
            return None
        if not executable.is_file() or executable.name.casefold() != "donutlocal.exe":
            return None
        target = self.versions_root / f"{version}-r{revision}"
        install = _validated_install_receipt(
            target,
            version=version,
            revision=revision,
            archive_sha256=str(value.get("sha256") or ""),
        )
        if install is None or install.executable != executable:
            return None
        return install

    def installed_app(self) -> DonutInstall | None:
        """Return the component managed by DocuRandom, not the legacy NSIS app."""

        return self.current_install()

    def manage_action_text(self) -> str:
        return (
            "CẬP NHẬT DONUT LOCAL"
            if self.current_install() is not None
            else "CÀI ĐẶT DONUT LOCAL"
        )

    @property
    def source_repo_root(self) -> Path:
        return self.root / "source" / "donutbrowser-local"

    @staticmethod
    def _run_git(
        arguments: list[str],
        *,
        cwd: Path | None = None,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        git = shutil.which("git")
        if not git:
            raise DonutManagedError(
                "Máy chưa có Git nên chưa thể bung/cập nhật source Donut Local."
            )
        result = subprocess.run(
            [git, *arguments],
            cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if result.returncode and not allow_failure:
            detail = result.stdout.strip()[-1600:]
            raise DonutManagedError(
                "Git không thể chuẩn bị source Donut Local."
                + (f" Chi tiết: {detail}" if detail else "")
            )
        return result

    def _validate_source_repo(
        self,
        root: Path,
        payload: DonutSourcePayload,
    ) -> None:
        required = (
            root / ".git",
            root / "UPDATE_DOCURANDOM.cmd",
            root / "scripts" / "update-build-docurandom.ps1",
            root / "scripts" / "verify-local-contract.mjs",
            root / "src-tauri" / "tauri.conf.json",
        )
        if not all(path.exists() for path in required):
            raise DonutManagedError(
                "Source Donut Local thiếu repo Git, script cập nhật hoặc bản vá build."
            )
        status = self._run_git(
            ["status", "--porcelain=v1", "--untracked-files=normal"], cwd=root
        ).stdout.strip()
        if status:
            raise DonutManagedError(
                "Source Donut Local đang có thay đổi chưa lưu; tool dừng để không ghi đè."
            )
        origin = self._run_git(["remote", "get-url", "origin"], cwd=root).stdout.strip()
        upstream = self._run_git(["remote", "get-url", "upstream"], cwd=root).stdout.strip()
        if origin != payload.origin or upstream != payload.upstream:
            raise DonutManagedError(
                "Remote source Donut Local không đúng kho đã khóa; tool từ chối chạy script."
            )
        related = self._run_git(
            ["merge-base", "--is-ancestor", payload.commit, "HEAD"],
            cwd=root,
            allow_failure=True,
        )
        reverse_related = self._run_git(
            ["merge-base", "--is-ancestor", "HEAD", payload.commit],
            cwd=root,
            allow_failure=True,
        )
        if related.returncode and reverse_related.returncode:
            raise DonutManagedError(
                "Source Donut Local trên máy không cùng lịch sử với source đã nhúng."
            )

    def ensure_source_repo(
        self,
        package_root: Path | None = None,
        *,
        progress: ProgressCallback | None = None,
    ) -> Path:
        payload = bundled_source(package_root)
        if payload is None:
            raise DonutManagedError(
                "Bản DocuRandom Studio này không chứa source Donut Local đầy đủ."
            )
        target = self.source_repo_root
        if target.exists():
            self._validate_source_repo(target, payload)
            return target
        if progress is not None:
            progress(4.0, "Đang bung source Donut Local và lịch sử Git đã nhúng…")
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging = parent / f".{target.name}.{uuid.uuid4().hex}.staging"
        try:
            self._run_git(
                [
                    "clone",
                    "--branch",
                    payload.branch,
                    "--single-branch",
                    str(payload.path),
                    str(staging),
                ]
            )
            self._run_git(["remote", "set-url", "origin", payload.origin], cwd=staging)
            self._run_git(["remote", "add", "upstream", payload.upstream], cwd=staging)
            head = self._run_git(["rev-parse", "HEAD"], cwd=staging).stdout.strip().casefold()
            if head != payload.commit:
                raise DonutManagedError(
                    "Commit source sau khi bung không khớp manifest đã nhúng."
                )
            self._validate_source_repo(staging, payload)
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return target

    @staticmethod
    def _stop_installed_app(install: DonutInstall) -> None:
        profile_ids: list[str] = []
        profile_pids: list[int] = []
        try:
            endpoint = DonutManager.discover_endpoint()
            with DonutMcpClient(endpoint, timeout=12.0) as client:
                profiles = client.list_profiles()
                profile_ids = [profile.profile_id for profile in profiles]
                profile_pids = [
                    int(profile.process_id)
                    for profile in profiles
                    if profile.process_id is not None and int(profile.process_id) > 0
                ]
                if profile_ids:
                    client.call_tool(
                        "batch_stop_profiles", {"profile_ids": profile_ids}
                    )
        except (DonutManagedError, DonutMcpError, OSError, ValueError) as exc:
            logger.warning(
                "donut_update_graceful_profile_stop_failed error=%s",
                type(exc).__name__,
            )
        if profile_ids:
            time.sleep(1.5)
        environment = os.environ.copy()
        environment["DOCURANDOM_DONUT_EXE"] = str(
            install.executable.resolve(strict=False)
        )
        environment["DOCURANDOM_DONUT_PROFILE_PIDS"] = ",".join(
            str(pid) for pid in sorted(set(profile_pids))
        )
        environment["DOCURANDOM_DONUT_DATA"] = str(
            official_data_root().resolve(strict=False)
        )
        script = (
            "$target=[IO.Path]::GetFullPath($env:DOCURANDOM_DONUT_EXE);"
            "$data=[IO.Path]::GetFullPath($env:DOCURANDOM_DONUT_DATA).TrimEnd('\\')+'\\';"
            "$ids=@(($env:DOCURANDOM_DONUT_PROFILE_PIDS -split ',')|"
            "Where-Object{$_ -match '^[1-9][0-9]*$'});"
            "foreach($id in $ids){$p=Get-CimInstance Win32_Process -Filter "
            "\"ProcessId=$id\" -ErrorAction SilentlyContinue;"
            "if($p -and (($p.ExecutablePath -and ([IO.Path]::GetFullPath($p.ExecutablePath)."
            "StartsWith($data,[StringComparison]::OrdinalIgnoreCase))) -or "
            "($p.CommandLine -and $p.CommandLine.IndexOf($data,[StringComparison]::"
            "OrdinalIgnoreCase) -ge 0))){& $env:SystemRoot\\System32\\taskkill.exe "
            "/PID $id /T /F 2>$null|Out-Null}};"
            "Get-CimInstance Win32_Process -Filter \"Name='donutbrowser.exe'\" "
            "-ErrorAction SilentlyContinue|Where-Object{$_.ExecutablePath-and"
            "([IO.Path]::GetFullPath($_.ExecutablePath)-ieq$target)}|"
            "ForEach-Object{& $env:SystemRoot\\System32\\taskkill.exe "
            "/PID $_.ProcessId /T /F 2>$null|Out-Null};"
            "Start-Sleep -Milliseconds 700;"
            "$remaining=@(Get-CimInstance Win32_Process -Filter "
            "\"Name='donutbrowser.exe'\" -ErrorAction SilentlyContinue|"
            "Where-Object{$_.ExecutablePath-and([IO.Path]::GetFullPath($_.ExecutablePath)"
            "-ieq$target)});"
            "foreach($id in $ids){$p=Get-CimInstance Win32_Process -Filter "
            "\"ProcessId=$id\" -ErrorAction SilentlyContinue;"
            "if($p -and (($p.ExecutablePath -and ([IO.Path]::GetFullPath($p.ExecutablePath)."
            "StartsWith($data,[StringComparison]::OrdinalIgnoreCase))) -or "
            "($p.CommandLine -and $p.CommandLine.IndexOf($data,[StringComparison]::"
            "OrdinalIgnoreCase) -ge 0))){$remaining+=@($p)}};"
            "if($remaining.Count -gt 0){exit 37};exit 0"
        )
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        try:
            result = subprocess.run(
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DonutManagedError(
                "Không thể đóng an toàn Donut Local trước khi cập nhật; installer chưa được chạy."
            ) from exc
        if result.returncode != 0:
            raise DonutManagedError(
                "Donut Local hoặc một profile vẫn đang chạy; installer chưa được chạy."
            )

    def _download_release_installer(
        self,
        release: DonutInstallerRelease,
        *,
        progress: ProgressCallback | None,
        cancel_event: threading.Event | None,
    ) -> Path:
        downloads = self.root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        destination = downloads / release.asset_name
        if (
            destination.is_file()
            and destination.stat().st_size == release.asset_size
            and _sha256(destination) == release.sha256
        ):
            return destination
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        request = urllib.request.Request(
            release.asset_url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "DocuRandomStudio-DonutUpdater",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        opener = urllib.request.build_opener(_PrivateGitHubRedirectHandler())
        try:
            with opener.open(request, timeout=90) as response, temporary.open("wb") as output:
                received = 0
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise DonutManagedError(
                            "Đã hủy tải Donut Local; installer chưa được chạy."
                        )
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > release.asset_size:
                        raise DonutManagedError(
                            "Installer Donut Local tải về lớn hơn kích thước GitHub công bố."
                        )
                    output.write(chunk)
                    if progress is not None:
                        progress(
                            5.0 + received / release.asset_size * 82.0,
                            f"Đang tải Donut Local {release.version} · "
                            f"{received // 1048576}/{max(1, release.asset_size // 1048576)} MB",
                        )
            if received != release.asset_size or _sha256(temporary) != release.sha256:
                raise DonutManagedError(
                    "Installer tải về không khớp digest SHA-256 của GitHub; đã từ chối cài."
                )
            os.replace(temporary, destination)
            return destination
        except urllib.error.HTTPError as exc:
            raise DonutManagedError(
                f"GitHub trả lỗi HTTP {exc.code} khi tải installer Donut Local."
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise DonutManagedError(
                "Mạng bị lỗi khi tải installer Donut Local; installer chưa được chạy."
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def install_latest_release(
        self,
        *,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> DonutInstall:
        if progress is not None:
            progress(1.0, "Đang kiểm tra Release Donut Local mới nhất trên GitHub…")
        release = latest_installer_release()
        current = installed_local_app()
        if current is not None and (
            _version_tuple(current.version) >= _version_tuple(release.version)
        ):
            if progress is not None:
                progress(100.0, f"Donut Local {current.version} đã là bản mới nhất.")
            return current
        installer = self._download_release_installer(
            release,
            progress=progress,
            cancel_event=cancel_event,
        )
        if _sha256(installer) != release.sha256:
            raise DonutManagedError(
                "Installer Donut Local trong bộ nhớ đệm sai SHA-256; đã từ chối cài."
            )
        if current is not None:
            if progress is not None:
                progress(89.0, "Đang đóng toàn bộ profile và Donut Local cũ…")
            self._stop_installed_app(current)
            time.sleep(0.8)
        if progress is not None:
            progress(93.0, f"Đang cài tự động Donut Local {release.version}…")
        installed = self._run_nsis_installer(
            installer,
            version=release.version,
            silent=True,
        )
        try:
            installer.unlink(missing_ok=True)
        except OSError:
            logger.warning("donut_installer_cache_cleanup_failed path=%s", installer)
        if progress is not None:
            progress(96.0, f"Đã cài Donut Local {installed.version}; đang mở lại MCP…")
        return installed
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )

    @staticmethod
    def _run_nsis_installer(
        path: Path,
        *,
        version: str,
        silent: bool,
    ) -> DonutInstall:
        if not path.is_file():
            raise DonutManagedError("Installer Donut Local không còn tồn tại.")
        command = [str(path), *( ["/S"] if silent else [] )]
        try:
            result = subprocess.run(
                command,
                cwd=str(path.parent),
                stdin=subprocess.DEVNULL,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if silent else 0
                ),
                timeout=20 * 60,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DonutManagedError(
                "Installer Donut Local chạy quá 20 phút nên tool đã dừng chờ."
            ) from exc
        if result.returncode:
            raise DonutManagedError(
                f"Installer Donut Local kết thúc với mã lỗi {result.returncode}; "
                "không chạy lại để tránh cài lặp."
            )
        installed = installed_local_app()
        if installed is None or _version_tuple(installed.version) < _version_tuple(version):
            raise DonutManagedError(
                "Installer đã đóng nhưng Windows chưa xác nhận đúng phiên bản Donut Local. "
                "Tool dừng tại đây và không tự chạy installer lần hai."
            )
        return installed

    def install_embedded_app(
        self,
        *,
        progress: ProgressCallback | None = None,
    ) -> DonutInstall:
        payload = bundled_installer()
        if payload is None:
            raise DonutManagedError(
                "Bản DocuRandom Studio này không chứa installer Donut Local hợp lệ."
            )
        if _sha256(payload.path) != payload.sha256:
            raise DonutManagedError(
                "Installer Donut Local đã nhúng không khớp SHA-256; đã từ chối chạy."
            )
        if progress is not None:
            progress(10.0, f"Đang mở trình cài Donut Local {payload.version}…")
        return self._run_nsis_installer(payload.path, version=payload.version, silent=False)

    def update_from_source(
        self,
        *,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        timeout: float = 3 * 60 * 60,
    ) -> DonutInstall:
        source = self.ensure_source_repo(progress=progress)
        missing = [name for name in ("node", "cargo") if not shutil.which(name)]
        if missing:
            raise DonutManagedError(
                "Máy thiếu công cụ build "
                + ", ".join(missing)
                + ". Source đã được bung đầy đủ nhưng chưa thể build bản cập nhật."
            )
        state_path = (
            Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            / "DocuRandomDNUpdater"
            / "last-build.json"
        )
        before = _read_json(state_path)
        log_root = state_path.parent / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        automation_log = log_root / "docurandom-studio.log"
        script_path = source / "scripts" / "update-build-docurandom.ps1"
        command = (
            "function Start-Process { param([Parameter(ValueFromRemainingArguments=$true)]$Rest) };"
            "& $env:DOCURANDOM_DN_UPDATE_SCRIPT"
        )
        environment = os.environ.copy()
        environment["DOCURANDOM_DN_UPDATE_SCRIPT"] = str(script_path)
        if progress is not None:
            progress(
                12.0,
                "Đang kiểm tra Donut chính thức, merge bản vá MCP và build installer…",
            )
        with automation_log.open("wb") as output:
            process = subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                cwd=str(source),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    process.terminate()
                    raise DonutManagedError(
                        "Đã dừng cập nhật Donut Local; installer chưa được chạy."
                    )
                if time.monotonic() >= deadline:
                    process.terminate()
                    raise DonutManagedError(
                        "Build Donut Local quá thời gian cho phép; installer chưa được chạy."
                    )
                time.sleep(0.5)
        if process.returncode:
            try:
                detail = automation_log.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()[-1800:]
            except OSError:
                detail = ""
            raise DonutManagedError(
                f"Pipeline UPDATE_DOCURANDOM thất bại (mã {process.returncode})."
                + (f" Chi tiết cuối: {detail}" if detail else "")
            )
        state = _read_json(state_path)
        installer_text = str(state.get("installer") or "").strip()
        digest = str(state.get("sha256") or "").strip().casefold()
        if not installer_text or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DonutManagedError(
                "Pipeline build xong nhưng không trả về installer và SHA-256 hợp lệ."
            )
        installer = Path(installer_text).resolve(strict=False)
        output_root = (
            Path(os.environ.get("USERPROFILE") or Path.home())
            / "Downloads"
            / "DocuRandom-DN-builds"
        ).resolve(strict=False)
        try:
            installer.relative_to(output_root)
        except ValueError as exc:
            raise DonutManagedError(
                "Pipeline trả installer nằm ngoài thư mục build đã khóa."
            ) from exc
        if (
            installer.suffix.casefold() != ".exe"
            or not installer.is_file()
            or _sha256(installer) != digest
        ):
            raise DonutManagedError(
                "Installer vừa build không tồn tại hoặc không khớp SHA-256; đã từ chối cài."
            )
        try:
            config = json.loads(
                (source / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
            )
            version = str(config["version"])
            _version_tuple(version)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, DonutManagedError) as exc:
            raise DonutManagedError(
                "Không xác định được phiên bản của installer vừa build."
            ) from exc
        current = installed_local_app()
        build_changed = (
            str(before.get("sha256") or "").casefold() != digest
            or str(before.get("sourceCommit") or "").casefold()
            != str(state.get("sourceCommit") or "").casefold()
        )
        if current is not None and not build_changed and _version_tuple(current.version) >= _version_tuple(version):
            if progress is not None:
                progress(94.0, f"Donut Local {current.version} đã là bản mới nhất.")
            return current
        if current is not None:
            self._stop_installed_app(current)
            time.sleep(0.8)
        if progress is not None:
            progress(92.0, f"Đang cài tự động Donut Local {version} vừa build…")
        return self._run_nsis_installer(installer, version=version, silent=True)

    def available_update(self) -> DonutRelease | None:
        """Return the newer signed private component without downloading it."""

        release = latest_local_release()
        current = self.current_install()
        if current is None:
            return release
        return (
            release
            if _release_key(release) > _release_key(current)
            else None
        )

    def _write_current_install(
        self,
        install: DonutInstall,
        **metadata: object,
    ) -> None:
        payload: dict[str, object] = {
            "version": install.version,
            "revision": install.revision,
            "executable": str(install.executable.relative_to(self.root)),
            "source": DONUT_SOURCE_URL,
            "installed_at": time.time(),
        }
        payload.update(metadata)
        atomic_write_json(self.current_file, payload)

    def _commit_install(
        self,
        release: DonutRelease,
        archive: Path,
        *,
        preserve_versions: set[str] | None = None,
    ) -> DonutInstall:
        if archive.stat().st_size != release.archive_size or _sha256(archive) != release.sha256:
            raise DonutManagedError(
                "Gói Donut Local không khớp manifest đã ký; đã từ chối cài."
            )
        self.versions_root.mkdir(parents=True, exist_ok=True)
        target = self.versions_root / release.version_key
        install: DonutInstall
        if target.exists():
            existing_install = _validated_install_receipt(
                target,
                version=release.version,
                revision=release.revision,
                archive_sha256=release.sha256,
            )
            if existing_install is None:
                try:
                    target.resolve(strict=False).relative_to(
                        self.versions_root.resolve(strict=False)
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    raise DonutManagedError(
                        "Thư mục Donut Local lỗi nằm ngoài vùng Components."
                    ) from exc
                shutil.rmtree(target)
            else:
                install = existing_install
        if not target.exists():
            staging = self.versions_root / f".{release.version_key}.{uuid.uuid4().hex}.staging"
            staging.mkdir(parents=True, exist_ok=False)
            try:
                _safe_extract_zip(archive, staging)
                staged_executable = _find_donut_executable(staging)
                _write_install_receipt(staging, release, staged_executable)
                os.replace(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
            executable = _find_donut_executable(target)
            install = DonutInstall(
                release.version, release.revision, executable.parent, executable
            )
        self._write_current_install(
            install,
            local_tag=release.tag,
            upstream_tag=release.upstream_tag,
            sha256=release.sha256,
        )
        keep = {install.version_key, *(preserve_versions or set())}
        self._prune_versions(keep=keep)
        self._prune_downloads(keep=release.archive_name)
        return install

    def _prune_versions(self, *, keep: set[str]) -> None:
        if not self.versions_root.is_dir():
            return
        candidates: list[tuple[tuple[int, int, int, int], Path]] = []
        for path in self.versions_root.iterdir():
            if not path.is_dir() or path.name in keep or path.name.startswith("."):
                continue
            match = re.fullmatch(r"(\d+\.\d+\.\d+)-r([1-9]\d*)", path.name)
            if match is None:
                continue
            candidates.append(((*_version_tuple(match.group(1)), int(match.group(2))), path))
        candidates.sort(reverse=True)
        backup_slots = max(0, 2 - len(keep))
        for _version, path in candidates[backup_slots:]:
            try:
                path.resolve(strict=False).relative_to(self.versions_root.resolve(strict=False))
                shutil.rmtree(path)
            except (OSError, RuntimeError, ValueError):
                logger.warning("donut_local_component_prune_failed path=%s", path)

    def _prune_downloads(self, *, keep: str) -> None:
        downloads = self.root / "downloads"
        if not downloads.is_dir():
            return
        for path in downloads.glob("DonutLocal_*_x64-portable.zip"):
            if path.name.casefold() == keep.casefold():
                continue
            try:
                path.resolve(strict=False).relative_to(downloads.resolve(strict=False))
                path.unlink(missing_ok=True)
            except (OSError, RuntimeError, ValueError):
                logger.warning("donut_local_download_prune_failed path=%s", path)

    def _download_archive(
        self,
        release: DonutRelease,
        *,
        token: str,
        progress: ProgressCallback | None,
        cancel_event: threading.Event | None,
    ) -> Path:
        downloads = self.root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        destination = downloads / release.archive_name
        if (
            destination.is_file()
            and destination.stat().st_size == release.archive_size
            and _sha256(destination) == release.sha256
        ):
            return destination
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with _open_private_github(
                release.archive_url,
                token,
                timeout=90,
                accept="application/octet-stream",
            ) as response, temporary.open("wb") as output:
                received = 0
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise DonutManagedError(
                            "Đã hủy tải Donut Local; bản đang dùng được giữ nguyên."
                        )
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > release.archive_size:
                        raise DonutManagedError("Gói Donut Local tải về lớn hơn manifest.")
                    output.write(chunk)
                    if progress is not None:
                        progress(
                            received / release.archive_size * 88.0,
                            f"Đang tải Donut Local {release.display_version} · "
                            f"{received // 1048576}/{release.archive_size // 1048576} MB",
                        )
            if received != release.archive_size or _sha256(temporary) != release.sha256:
                raise DonutManagedError(
                    "Gói Donut Local tải về không khớp manifest đã ký."
                )
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    def ensure_latest(
        self,
        *,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        allow_network: bool = True,
        package_root: Path | None = None,
    ) -> DonutInstall:
        with self._lock:
            current = self.current_install()
            packaged = bundled_release(package_root)
            private_token = load_private_github_token()
            remote: DonutRelease | None = None
            network_error: Exception | None = None
            if allow_network:
                try:
                    if progress is not None:
                        progress(1.0, "Đang kiểm tra bản Donut Local mới đã ký…")
                    remote = latest_local_release(token=private_token)
                except Exception as exc:
                    network_error = exc
                    logger.warning("donut_local_release_check_failed error=%s", type(exc).__name__)

            chosen: DonutRelease | None = remote
            local_archive: Path | None = None
            if packaged is not None:
                packaged_release, packaged_archive = packaged
                if chosen is None or _release_key(packaged_release) > _release_key(chosen):
                    chosen = packaged_release
                    local_archive = packaged_archive
                elif _release_key(chosen) == _release_key(packaged_release):
                    local_archive = packaged_archive

            if current is not None and (chosen is None or _release_key(current) >= _release_key(chosen)):
                if progress is not None:
                    progress(100.0, f"Donut Local {current.display_version} đã sẵn sàng.")
                return current
            if chosen is None:
                detail = f" Chi tiết: {network_error}" if network_error else ""
                raise DonutManagedError(
                    "Chưa có gói Donut Local đã ký đóng kèm và chưa tải được bản mới." + detail
                )
            if local_archive is None:
                if not chosen.archive_url:
                    raise DonutManagedError("Gói Donut Local đóng kèm bị thiếu.")
                local_archive = self._download_archive(
                    chosen,
                    token=private_token or "",
                    progress=progress,
                    cancel_event=cancel_event,
                )
            if progress is not None:
                progress(92.0, f"Đang cài Donut Local {chosen.display_version} nguyên tử…")
            preserve = {current.version_key} if current is not None else set()
            install = self._commit_install(
                chosen,
                local_archive,
                preserve_versions=preserve,
            )
            if progress is not None:
                progress(100.0, f"Donut Local {install.display_version} đã sẵn sàng.")
            return install

    @staticmethod
    def enable_mcp() -> Path:
        settings_file = official_data_root() / "settings" / "app_settings.json"
        settings = _read_json(settings_file)
        settings["mcp_enabled"] = True
        settings.setdefault("mcp_port", DONUT_DEFAULT_MCP_PORT)
        settings["api_enabled"] = True
        settings.setdefault("api_port", DONUT_DEFAULT_API_PORT)
        atomic_write_json(settings_file, settings)
        return settings_file

    @staticmethod
    def _decrypt_mcp_token(path: Path) -> str:
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise DonutManagedError("Donut chưa tạo khóa MCP.") from exc
        if len(payload) < 7 or payload[:5] != b"DBMCP" or payload[5] != 2:
            raise DonutManagedError("Định dạng khóa MCP Donut chưa được hỗ trợ.")
        offset = 6
        salt_length = payload[offset]
        offset += 1
        salt_text = payload[offset : offset + salt_length]
        offset += salt_length
        if offset + 16 > len(payload):
            raise DonutManagedError("Khóa MCP Donut bị thiếu dữ liệu.")
        nonce = payload[offset : offset + 12]
        offset += 12
        ciphertext_length = int.from_bytes(payload[offset : offset + 4], "little")
        offset += 4
        ciphertext = payload[offset : offset + ciphertext_length]
        if len(ciphertext) != ciphertext_length:
            raise DonutManagedError("Khóa MCP Donut bị cắt ngắn.")
        key_materials: list[bytes] = []
        try:
            install_key = path.with_name("vault.key").read_bytes()
        except OSError:
            install_key = b""
        if len(install_key) == 32:
            # Current Donut builds seal local tokens with the installation's
            # random vault.key. Keep the legacy password below for older files
            # that Donut has not migrated yet.
            key_materials.append(install_key)
        key_materials.append(DONUT_VAULT_PASSWORD)
        token = ""
        try:
            padding = b"=" * (-len(salt_text) % 4)
            salt = base64.b64decode(salt_text + padding, validate=True)
            for material in key_materials:
                try:
                    key = Argon2id(
                        salt=salt,
                        length=32,
                        iterations=2,
                        lanes=1,
                        memory_cost=19456,
                    ).derive(material)
                    candidate = AESGCM(key).decrypt(
                        nonce, ciphertext, None
                    ).decode("utf-8")
                except Exception:
                    continue
                if re.fullmatch(r"[A-Za-z0-9_-]{16,}", candidate):
                    token = candidate
                    break
        except Exception as exc:
            raise DonutManagedError(
                "Không giải mã được khóa MCP của Donut; có thể định dạng đã thay đổi."
            ) from exc
        if not token:
            raise DonutManagedError("Khóa MCP Donut sau giải mã không hợp lệ.")
        return token

    @classmethod
    def discover_endpoint(cls) -> DonutMcpEndpoint:
        settings_root = official_data_root() / "settings"
        settings = _read_json(settings_root / "app_settings.json")
        try:
            port = int(settings.get("mcp_port") or DONUT_DEFAULT_MCP_PORT)
        except (TypeError, ValueError) as exc:
            raise DonutManagedError("Cổng MCP trong Donut không hợp lệ.") from exc
        token = cls._decrypt_mcp_token(settings_root / "mcp_token.dat")
        endpoint = DonutMcpEndpoint.parse(f"http://127.0.0.1:{port}/mcp/{token}")
        save_donut_mcp_endpoint(endpoint.value)
        return endpoint

    @staticmethod
    def launch(install: DonutInstall) -> subprocess.Popen[bytes]:
        environment = os.environ.copy()
        shared = official_data_root()
        environment["DONUTBROWSER_DATA_DIR"] = str(shared)
        environment["DONUTBROWSER_CACHE_DIR"] = str(shared)
        environment["DOCURANDOM_MANAGED_DONUT"] = "1"
        return subprocess.Popen(
            [str(install.executable)],
            cwd=str(install.executable.parent),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )

    def stop_managed_local(self) -> None:
        """Stop only DonutLocal.exe processes installed under this manager root."""

        environment = os.environ.copy()
        environment["DOCURANDOM_DONUT_LOCAL_ROOT"] = str(self.root.resolve(strict=False))
        script = (
            "$root=[IO.Path]::GetFullPath($env:DOCURANDOM_DONUT_LOCAL_ROOT);"
            "$items=Get-CimInstance Win32_Process -Filter \"Name='DonutLocal.exe'\" "
            "-ErrorAction SilentlyContinue;"
            "foreach($p in $items){if($p.ExecutablePath){"
            "$path=[IO.Path]::GetFullPath($p.ExecutablePath);"
            "if($path.StartsWith($root,[StringComparison]::OrdinalIgnoreCase)){"
            "$null=Invoke-CimMethod -InputObject $p -MethodName Terminate "
            "-ErrorAction SilentlyContinue}}}"
        )
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                env=environment,
                capture_output=True,
                timeout=20,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DonutManagedError("Không thể đóng Donut Local cũ để cập nhật.") from exc

    @staticmethod
    def _require_local_server(info: dict[str, object]) -> None:
        name = _server_name(info)
        if name != DONUT_LOCAL_SERVER_NAME:
            if name:
                raise DonutManagedError(
                    "MCP đang thuộc Donut chính thức, không phải Donut Local. "
                    "Hãy đóng cửa sổ Donut chính thức một lần rồi bấm lại; profile vẫn được giữ nguyên."
                )
            raise DonutManagedError("MCP không xác nhận được danh tính Donut Local.")

    @staticmethod
    def _require_local_tools(names: frozenset[str]) -> None:
        missing = sorted(DONUT_REQUIRED_LOCAL_TOOLS - names)
        if missing:
            raise DonutManagedError(
                "Donut Local vừa mở không có đủ Developer Tools bắt buộc: "
                + ", ".join(missing)
                + ". Tool đã từ chối dùng bản này."
            )

    def _wait_for_local_mcp(
        self,
        process: subprocess.Popen[bytes],
        *,
        cancel_event: threading.Event | None,
        timeout: float,
    ) -> tuple[DonutMcpEndpoint, dict[str, object]]:
        deadline = time.monotonic() + max(10.0, timeout)
        last_error = ""
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise DonutManagedError(
                    "Đã dừng chờ Donut Local; ứng dụng vẫn có thể đang mở."
                )
            if process.poll() is not None:
                raise DonutManagedError(
                    "Donut Local đã đóng trước khi MCP sẵn sàng. Nếu Donut chính thức đang mở, "
                    "hãy đóng nó rồi bấm lại."
                )
            try:
                endpoint = self.discover_endpoint()
                client = DonutMcpClient(endpoint, timeout=8.0)
                try:
                    info = client.connect()
                    self._require_local_tools(client.list_tool_names())
                finally:
                    client.close()
                self._require_local_server(info)
                return endpoint, info
            except (DonutManagedError, DonutMcpError) as exc:
                last_error = str(exc)
                time.sleep(1.0)
        raise DonutManagedError(
            "Đã mở Donut Local nhưng MCP chưa sẵn sàng. Hãy hoàn tất đăng nhập/chấp nhận "
            "điều khoản rồi bấm Kiểm tra lại."
            + (f" Chi tiết cuối: {last_error}" if last_error else "")
        )

    def _restore_previous_after_failed_update(
        self,
        previous: DonutInstall,
        failed: DonutInstall,
        failure: DonutManagedError,
        *,
        progress: ProgressCallback | None,
        timeout: float,
    ) -> None:
        if progress is not None:
            progress(
                98.0,
                f"Bản mới không khởi động được · đang phục hồi Donut Local "
                f"{previous.display_version}…",
            )
        try:
            self.stop_managed_local()
            time.sleep(0.5)
            self._write_current_install(
                previous,
                rollback_from=failed.version_key,
                rollback_reason=str(failure),
                restored_at=time.time(),
            )
            process = self.launch(previous)
            self._wait_for_local_mcp(
                process,
                cancel_event=None,
                timeout=min(45.0, max(10.0, timeout)),
            )
        except (DonutManagedError, OSError) as rollback_error:
            raise DonutManagedError(
                f"Donut Local {failed.display_version} không khởi động được và bản "
                f"{previous.display_version} cũng chưa phục hồi được. "
                f"Chi tiết bản mới: {failure}. Chi tiết phục hồi: {rollback_error}"
            ) from failure
        raise DonutManagedError(
            f"Donut Local {failed.display_version} không khởi động được. Tool đã tự phục hồi "
            f"và mở lại bản {previous.display_version}; dữ liệu/profile vẫn được giữ nguyên. "
            f"Chi tiết: {failure}"
        ) from failure

    def install_launch_and_connect(
        self,
        *,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        timeout: float = 75.0,
    ) -> tuple[DonutInstall, DonutMcpEndpoint, dict[str, object]]:
        previous = self.current_install()
        install = self.ensure_latest(
            progress=progress,
            cancel_event=cancel_event,
        )
        changed = previous is None or _release_key(previous) != _release_key(install)
        self.enable_mcp()
        if changed:
            if progress is not None:
                progress(94.0, "Đang đóng Donut Local cũ trước khi mở bản mới…")
            legacy = installed_local_app()
            if legacy is not None:
                self._stop_installed_app(legacy)
            self.stop_managed_local()
            time.sleep(0.5)
        else:
            try:
                endpoint = self.discover_endpoint()
                client = DonutMcpClient(endpoint, timeout=8.0)
                try:
                    info = client.connect()
                    self._require_local_tools(client.list_tool_names())
                finally:
                    client.close()
                self._require_local_server(info)
                return install, endpoint, info
            except DonutManagedError as exc:
                if "Donut chính thức" in str(exc):
                    raise
            except DonutMcpError:
                pass
        if progress is not None:
            progress(96.0, "Đang mở Donut Local và chờ MCP…")
        try:
            process = self.launch(install)
            endpoint, info = self._wait_for_local_mcp(
                process,
                cancel_event=cancel_event,
                timeout=timeout,
            )
            return install, endpoint, info
        except OSError as exc:
            failure = DonutManagedError(
                f"Không mở được Donut Local {install.display_version}: {exc}"
            )
        except DonutManagedError as exc:
            failure = exc
        if cancel_event is not None and cancel_event.is_set():
            raise failure
        if "Donut chính thức" in str(failure):
            raise failure
        if changed and previous is not None:
            self._restore_previous_after_failed_update(
                previous,
                install,
                failure,
                progress=progress,
                timeout=timeout,
            )
        raise failure


def resolve_donut_mcp_endpoint() -> DonutMcpEndpoint | None:
    """Prefer Donut's live token/port and retain the encrypted saved fallback."""

    try:
        return DonutManager.discover_endpoint()
    except DonutManagedError:
        return load_donut_mcp_endpoint()
