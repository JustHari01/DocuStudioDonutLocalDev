from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from pathlib import Path

from verify_chat_bridge_package import verify_chat_bridge_package


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pin hashes of the bridge built from the current source tree."
    )
    parser.add_argument("bridge_root", type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docurandom/bridge_integrity.py"),
    )
    args = parser.parse_args()
    bridge = args.bridge_root.resolve(strict=True)
    executable = bridge / "DocuRandom Chat Bridge.exe"
    app_asar = bridge / "resources" / "app.asar"
    if not executable.is_file() or not app_asar.is_file():
        parser.error("bridge root is missing executable or resources/app.asar")
    try:
        verify_chat_bridge_package(app_asar, args.expected_version)
    except ValueError as exc:
        parser.error(str(exc))
    payload = (
        '"""Build-pinned hashes for the bundled DocuRandom Chat Bridge."""\n\n'
        "CHAT_BRIDGE_EXECUTABLE_SHA256 = (\n"
        f'    "{sha256_file(executable)}"\n'
        ")\n"
        "CHAT_BRIDGE_APP_ASAR_SHA256 = (\n"
        f'    "{sha256_file(app_asar)}"\n'
        ")\n"
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as temporary:
            temporary.write(payload)
        os.replace(temporary_name, output)
    finally:
        try:
            Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
