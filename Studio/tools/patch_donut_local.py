from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the safe DocuRandom Donut Local patch")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--upstream-tag", required=True)
    args = parser.parse_args()

    source = args.source.resolve(strict=True)
    config_path = source / "src-tauri" / "tauri.conf.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("productName") != "Donut":
        raise RuntimeError("Unexpected upstream productName; refusing an ambiguous patch")
    # Keep com.donutbrowser and DonutBrowser data paths unchanged so the local
    # build uses exactly the official profile/cookie/proxy/fingerprint store
    # and shares the official single-instance lock.
    config["productName"] = "Donut Local"
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    mcp_path = source / "src-tauri" / "src" / "mcp_server.rs"
    mcp_source = mcp_path.read_text(encoding="utf-8")
    old_name = 'const SERVER_NAME: &str = "donut-browser";'
    if mcp_source.count(old_name) != 1:
        raise RuntimeError("Unexpected upstream MCP server marker; refusing an ambiguous patch")
    mcp_path.write_text(
        mcp_source.replace(old_name, 'const SERVER_NAME: &str = "donut-browser-local";'),
        encoding="utf-8",
    )
    marker = {
        "schema": 1,
        "name": "DocuRandom Donut Local",
        "upstream": "https://github.com/zhom/donutbrowser",
        "upstream_tag": args.upstream_tag,
        "patch_scope": ["productName", "mcpServerName"],
        "entitlement_policy": "upstream-real-account",
    }
    (source / "DOCURANDOM_LOCAL_BUILD.json").write_text(
        json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
