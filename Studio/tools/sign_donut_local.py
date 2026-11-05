from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign a Donut Local component manifest")
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", type=int, default=1)
    parser.add_argument("--upstream-tag", required=True)
    parser.add_argument("--asset", type=Path, required=True)
    parser.add_argument("--source-asset", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version) or args.revision < 1:
        parser.error("invalid stable version/revision")
    asset = args.asset.resolve(strict=True)
    source_asset = args.source_asset.resolve(strict=True)
    value = {
        "schema": 1,
        "component": "donut-local",
        "version": args.version,
        "revision": args.revision,
        "local_version": f"{args.version}.{args.revision}",
        "upstream_tag": args.upstream_tag,
        "upstream_source": "https://github.com/zhom/donutbrowser",
        "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "asset": {
            "name": asset.name,
            "size": asset.stat().st_size,
            "sha256": sha256(asset),
        },
        "source_asset": {
            "name": source_asset.name,
            "size": source_asset.stat().st_size,
            "sha256": sha256(source_asset),
        },
        "entitlement_policy": "upstream-real-account",
    }
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    private_key = serialization.load_pem_private_key(
        args.private_key.resolve(strict=True).read_bytes(),
        password=None,
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise TypeError("Release signing key must be Ed25519")
    args.manifest.write_bytes(payload)
    args.signature.write_bytes(private_key.sign(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
