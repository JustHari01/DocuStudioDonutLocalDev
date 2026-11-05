from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile
import os

from docurandom.donut_manager import (
    DONUT_DISTRIBUTION_REPOSITORY,
    DONUT_SOURCE_URL,
    MANAGED_MANIFEST,
    MANAGED_SIGNATURE,
    _request_bytes,
    _open_private_github,
    _sha256,
    _verify_manifest_bytes,
    latest_local_release,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage a signed Donut Local portable package")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    token = os.environ.get("DOCURANDOM_DONUT_GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DOCURANDOM_DONUT_GITHUB_TOKEN is required to stage the private Donut component"
        )
    release = latest_local_release(token=token)
    output = args.output.resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)

    manifest = _request_bytes(
        release.manifest_url,
        token=token,
        limit=64 * 1024,
        accept="application/octet-stream",
    )
    signature = _request_bytes(
        release.signature_url,
        token=token,
        limit=1024,
        accept="application/octet-stream",
    )
    release = _verify_manifest_bytes(release, manifest, signature)
    archive = output / release.archive_name
    if (
        not archive.is_file()
        or archive.stat().st_size != release.archive_size
        or _sha256(archive) != release.sha256
    ):
        with tempfile.NamedTemporaryFile(prefix="donut-local-", suffix=".zip", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            with _open_private_github(
                release.archive_url,
                token,
                timeout=120,
                accept="application/octet-stream",
            ) as response, temporary.open("wb") as target:
                shutil.copyfileobj(response, target, length=1024 * 1024)
            if (
                temporary.stat().st_size != release.archive_size
                or _sha256(temporary) != release.sha256
            ):
                raise RuntimeError("Signed Donut Local archive verification failed")
            shutil.copy2(temporary, archive)
        finally:
            temporary.unlink(missing_ok=True)
    for stale in output.glob("DonutLocal_*_x64-portable.zip"):
        if stale.name.casefold() != archive.name.casefold():
            stale.unlink(missing_ok=True)

    (output / MANAGED_MANIFEST).write_bytes(manifest)
    (output / MANAGED_SIGNATURE).write_bytes(signature)
    source_asset = f"DonutLocal_{release.version}_r{release.revision}-corresponding-source.zip"
    source_url = (
        f"https://github.com/{DONUT_DISTRIBUTION_REPOSITORY}/releases/download/"
        f"{release.tag}/{source_asset}"
    )
    (output / "SOURCE.txt").write_text(
        "Donut Local derivative build\n"
        f"Upstream source: {DONUT_SOURCE_URL}/tree/{release.upstream_tag}\n"
        f"Corresponding source: {source_url}\n"
        "License: AGPL-3.0\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "version": release.version,
                "revision": release.revision,
                "tag": release.tag,
                "archive": release.archive_name,
                "sha256": release.sha256,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
