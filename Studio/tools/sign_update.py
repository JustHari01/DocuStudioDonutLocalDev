from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and sign a DocuRandom update manifest.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--asset", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--signature", required=True, type=Path)
    args = parser.parse_args()

    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", args.version):
        parser.error("--version must be strict semantic version x.y.z")

    asset = args.asset.resolve(strict=True)
    digest_builder = hashlib.sha256()
    with asset.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest_builder.update(chunk)
    digest = digest_builder.hexdigest()
    value = {
        "schema": 1,
        "version": args.version,
        "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "asset": {
            "name": "DocuRandomStudio-portable.zip",
            "size": asset.stat().st_size,
            "sha256": digest,
        },
    }
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    private_key = serialization.load_pem_private_key(
        args.private_key.resolve(strict=True).read_bytes(),
        password=None,
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise TypeError("Release signing key must be Ed25519.")
    signature = private_key.sign(payload)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_bytes(payload)
    args.signature.write_bytes(signature)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
