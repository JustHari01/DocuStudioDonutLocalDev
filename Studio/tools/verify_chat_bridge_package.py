from __future__ import annotations

import argparse
import json
import re
import struct
from pathlib import Path


MAX_ASAR_HEADER_SIZE = 16 * 1024 * 1024
MAX_ASAR_METADATA_FILE_SIZE = 2 * 1024 * 1024
SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")
RUNTIME_VERSION_RE = re.compile(
    rb"\bconst\s+VERSION\s*=\s*([\"'])(?P<version>\d+\.\d+\.\d+)\1\s*;"
)


class ChatBridgePackageError(ValueError):
    pass


def _uint32(buffer: bytes, offset: int, label: str) -> int:
    if len(buffer) < offset + 4:
        raise ChatBridgePackageError(f"ASAR thiếu {label}.")
    return struct.unpack_from("<I", buffer, offset)[0]


def read_asar_member(archive: Path, member: str) -> bytes:
    parts = tuple(part for part in member.replace("\\", "/").split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise ChatBridgePackageError("Tên tệp ASAR không hợp lệ.")
    try:
        with archive.open("rb") as source:
            size_pickle = source.read(8)
            if _uint32(size_pickle, 0, "size pickle") != 4:
                raise ChatBridgePackageError("ASAR dùng size pickle không được hỗ trợ.")
            header_size = _uint32(size_pickle, 4, "kích thước header")
            if not 8 <= header_size <= MAX_ASAR_HEADER_SIZE:
                raise ChatBridgePackageError("Kích thước header ASAR không hợp lệ.")
            header_pickle = source.read(header_size)
            if len(header_pickle) != header_size:
                raise ChatBridgePackageError("Header ASAR bị cắt ngắn.")
            payload_size = _uint32(header_pickle, 0, "header payload")
            json_size = _uint32(header_pickle, 4, "header JSON")
            if payload_size + 4 != header_size or json_size > header_size - 8:
                raise ChatBridgePackageError("Cấu trúc header ASAR không hợp lệ.")
            try:
                header = json.loads(header_pickle[8 : 8 + json_size].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ChatBridgePackageError("Header JSON của ASAR không hợp lệ.") from exc

            node: object = header
            for part in parts:
                if not isinstance(node, dict) or not isinstance(node.get("files"), dict):
                    raise ChatBridgePackageError(f"ASAR thiếu tệp {member}.")
                node = node["files"].get(part)
            if not isinstance(node, dict) or node.get("unpacked") is True or "link" in node:
                raise ChatBridgePackageError(f"Tệp {member} trong ASAR không đọc được trực tiếp.")
            size = node.get("size")
            offset = node.get("offset")
            if (
                not isinstance(size, int)
                or not 0 <= size <= MAX_ASAR_METADATA_FILE_SIZE
                or not isinstance(offset, str)
                or not offset.isdecimal()
            ):
                raise ChatBridgePackageError(f"Metadata của {member} trong ASAR không hợp lệ.")
            source.seek(8 + header_size + int(offset))
            value = source.read(size)
            if len(value) != size:
                raise ChatBridgePackageError(f"Tệp {member} trong ASAR bị cắt ngắn.")
            return value
    except OSError as exc:
        raise ChatBridgePackageError(f"Không thể đọc ASAR: {exc}") from exc


def inspect_chat_bridge_package(archive: Path) -> str:
    try:
        package = json.loads(read_asar_member(archive, "package.json").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChatBridgePackageError("package.json trong ASAR không hợp lệ.") from exc
    if not isinstance(package, dict) or package.get("name") != "docurandom-chat-bridge":
        raise ChatBridgePackageError("ASAR không phải DocuRandom Chat Bridge.")
    package_version = package.get("version")
    if not isinstance(package_version, str) or not SEMVER_RE.fullmatch(package_version):
        raise ChatBridgePackageError("Phiên bản package của Chat Bridge không hợp lệ.")
    runtime_source = read_asar_member(archive, "src/main.cjs")
    runtime_match = RUNTIME_VERSION_RE.search(runtime_source)
    if runtime_match is None:
        raise ChatBridgePackageError("Không tìm thấy phiên bản runtime trong Chat Bridge.")
    runtime_version = runtime_match.group("version").decode("ascii")
    if runtime_version != package_version:
        raise ChatBridgePackageError(
            f"Phiên bản runtime ({runtime_version}) không khớp package ({package_version})."
        )
    return package_version


def verify_chat_bridge_package(archive: Path, expected_version: str) -> str:
    if not SEMVER_RE.fullmatch(expected_version):
        raise ChatBridgePackageError("Phiên bản Chat Bridge mong đợi không hợp lệ.")
    packaged_version = inspect_chat_bridge_package(archive)
    if packaged_version != expected_version:
        raise ChatBridgePackageError(
            f"Chat Bridge đóng gói là {packaged_version}, nhưng source yêu cầu {expected_version}."
        )
    return packaged_version


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the version embedded in a packaged DocuRandom Chat Bridge ASAR."
    )
    parser.add_argument("app_asar", type=Path)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    try:
        version = verify_chat_bridge_package(
            args.app_asar.resolve(strict=True),
            args.expected_version,
        )
    except (OSError, ChatBridgePackageError) as exc:
        parser.error(str(exc))
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
