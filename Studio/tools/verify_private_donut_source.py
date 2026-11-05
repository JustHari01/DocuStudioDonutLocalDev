from __future__ import annotations

import argparse
from pathlib import Path


def require_text(path: Path, *needles: str) -> None:
    value = path.read_text(encoding="utf-8")
    missing = [needle for needle in needles if needle not in value]
    if missing:
        raise RuntimeError(f"{path} is missing required capability markers: {missing}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the private Donut Local integration before build"
    )
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    require_text(
        source / "src-tauri" / "src" / "mcp_server.rs",
        'const SERVER_NAME: &str = "donut-browser-local";',
        "crate::developer_tools_enabled() || CLOUD_AUTH.has_active_paid_subscription().await",
        'name: "get_debug_connection".to_string()',
        'name: "cdp_command".to_string()',
        'name: "evaluate_javascript".to_string()',
        'name: "navigate".to_string()',
    )
    require_text(
        source / "src-tauri" / "src" / "settings_manager.rs",
        "settings.api_enabled = true;",
        "settings.mcp_enabled = true;",
    )
    require_text(
        source / "src-tauri" / "tauri.conf.json",
        '"productName": "DocuRandom-DN"',
    )
    require_text(
        source / "src-tauri" / "src" / "lib.rs",
        'cfg!(feature = "developer-tools")',
    )
    print("Private Donut Local MCP/REST/CDP capability markers verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
