from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .donut_mcp import _protect_bytes, _unprotect_bytes
from .storage import app_data_dir


GITHUB_API_ORIGIN = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
PRIVATE_RELEASE_REPOSITORY = "JustHari01/donutbrowser-local"
PRIVATE_DONUT_REPOSITORY = PRIVATE_RELEASE_REPOSITORY
PRIVATE_GITHUB_TOKEN_FILE = "donut-private-github.dat"
MAX_GITHUB_RESPONSE = 2 * 1024 * 1024


def private_github_token_url() -> str:
    """Return GitHub's official pre-filled fine-grained token form."""

    return "https://github.com/settings/personal-access-tokens/new?" + urllib.parse.urlencode(
        {
            "name": "DocuRandom Donut Local",
            "description": "Read private Donut Local releases for DocuRandom Studio",
            "target_name": PRIVATE_DONUT_REPOSITORY.split("/", 1)[0],
            "expires_in": "366",
            "contents": "read",
        }
    )


class PrivateGitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class PrivateGitHubIdentity:
    login: str
    repository: str


def _token_path(path: Path | None = None) -> Path:
    return path or app_data_dir() / "secrets" / PRIVATE_GITHUB_TOKEN_FILE


def normalize_private_github_token(raw: str) -> str:
    token = str(raw or "").strip()
    if (
        not 20 <= len(token) <= 255
        or not token.isascii()
        or re.search(r"\s", token)
    ):
        raise PrivateGitHubError("Mã đăng nhập GitHub không hợp lệ.")
    return token


def save_private_github_token(raw: str, path: Path | None = None) -> None:
    token = normalize_private_github_token(raw)
    target = _token_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(_protect_bytes(token.encode("ascii")))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def load_private_github_token(path: Path | None = None) -> str | None:
    target = _token_path(path)
    try:
        protected = target.read_bytes()
        return normalize_private_github_token(
            _unprotect_bytes(protected).decode("ascii")
        )
    except (OSError, UnicodeError, ValueError, PrivateGitHubError):
        return None


def clear_private_github_token(path: Path | None = None) -> None:
    try:
        _token_path(path).unlink(missing_ok=True)
    except OSError as exc:
        raise PrivateGitHubError(
            "Không thể xóa đăng nhập GitHub private."
        ) from exc


def private_github_request(
    url: str,
    token: str,
    *,
    accept: str = "application/vnd.github+json",
) -> urllib.request.Request:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise PrivateGitHubError("Địa chỉ GitHub private không hợp lệ.")
    return urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {normalize_private_github_token(token)}",
            "User-Agent": "DocuRandomStudio-PrivateUpdater",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Cache-Control": "no-store",
        },
    )


def _read_json_response(response: object) -> dict[str, object]:
    raw = getattr(response, "read")(MAX_GITHUB_RESPONSE + 1)
    if len(raw) > MAX_GITHUB_RESPONSE:
        raise PrivateGitHubError("GitHub trả dữ liệu lớn bất thường.")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PrivateGitHubError("GitHub trả dữ liệu không hợp lệ.") from exc
    if not isinstance(value, dict):
        raise PrivateGitHubError("GitHub trả sai định dạng dữ liệu.")
    return value


def test_private_github_access(
    raw: str,
    *,
    repository: str = PRIVATE_DONUT_REPOSITORY,
    timeout: float = 20.0,
) -> PrivateGitHubIdentity:
    token = normalize_private_github_token(raw)
    try:
        with urllib.request.urlopen(
            private_github_request(f"{GITHUB_API_ORIGIN}/user", token),
            timeout=timeout,
        ) as response:
            user = _read_json_response(response)
        with urllib.request.urlopen(
            private_github_request(
                f"{GITHUB_API_ORIGIN}/repos/{repository}", token
            ),
            timeout=timeout,
        ) as response:
            repo = _read_json_response(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            message = "GitHub từ chối đăng nhập hoặc mã chưa có quyền đọc kho release private."
        elif exc.code == 404:
            message = "Tài khoản GitHub chưa được cấp quyền vào kho release private."
        else:
            message = f"GitHub trả lỗi HTTP {exc.code}."
        raise PrivateGitHubError(message) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise PrivateGitHubError("Không kết nối được GitHub để kiểm tra đăng nhập.") from exc
    login = str(user.get("login") or "").strip()
    full_name = str(repo.get("full_name") or "").strip()
    if not login or full_name.casefold() != repository.casefold():
        raise PrivateGitHubError("GitHub chưa xác nhận được kho release private.")
    return PrivateGitHubIdentity(login=login, repository=full_name)
