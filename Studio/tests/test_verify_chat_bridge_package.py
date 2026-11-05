from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from tools.verify_chat_bridge_package import (
    ChatBridgePackageError,
    inspect_chat_bridge_package,
    read_asar_member,
    verify_chat_bridge_package,
)


def _pickle_string(value: bytes) -> bytes:
    padding = b"\0" * ((-len(value)) % 4)
    payload = struct.pack("<I", len(value)) + value + padding
    return struct.pack("<I", len(payload)) + payload


def _write_asar(path: Path, *, package_version: str, runtime_version: str) -> None:
    members = {
        "package.json": json.dumps(
            {
                "name": "docurandom-chat-bridge",
                "version": package_version,
                "main": "src/main.cjs",
            },
            separators=(",", ":"),
        ).encode(),
        "src/main.cjs": f'const VERSION = "{runtime_version}";\n'.encode(),
    }
    offset = 0
    header = {"files": {}}
    for name, value in members.items():
        node = header["files"]
        parts = name.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {"files": {}})["files"]
        node[parts[-1]] = {"size": len(value), "offset": str(offset)}
        offset += len(value)
    header_pickle = _pickle_string(
        json.dumps(header, separators=(",", ":")).encode("utf-8")
    )
    size_pickle = struct.pack("<II", 4, len(header_pickle))
    path.write_bytes(size_pickle + header_pickle + b"".join(members.values()))


def test_reads_member_and_accepts_matching_bridge(tmp_path: Path) -> None:
    archive = tmp_path / "app.asar"
    _write_asar(archive, package_version="1.0.20", runtime_version="1.0.20")

    assert json.loads(read_asar_member(archive, "package.json"))["version"] == "1.0.20"
    assert inspect_chat_bridge_package(archive) == "1.0.20"
    assert verify_chat_bridge_package(archive, "1.0.20") == "1.0.20"


def test_rejects_runtime_and_package_version_mismatch(tmp_path: Path) -> None:
    archive = tmp_path / "app.asar"
    _write_asar(archive, package_version="1.0.20", runtime_version="1.0.18")

    with pytest.raises(ChatBridgePackageError, match="runtime"):
        inspect_chat_bridge_package(archive)


def test_rejects_stale_packaged_bridge(tmp_path: Path) -> None:
    archive = tmp_path / "app.asar"
    _write_asar(archive, package_version="1.0.18", runtime_version="1.0.18")

    with pytest.raises(ChatBridgePackageError, match="source yêu cầu 1.0.20"):
        verify_chat_bridge_package(archive, "1.0.20")


def test_rejects_truncated_archive(tmp_path: Path) -> None:
    archive = tmp_path / "app.asar"
    archive.write_bytes(struct.pack("<II", 4, 128) + b"{}")

    with pytest.raises(ChatBridgePackageError, match="cắt ngắn"):
        read_asar_member(archive, "package.json")
