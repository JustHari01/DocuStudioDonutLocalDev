from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from docurandom.donut_mcp import DonutMcpClient, DonutMcpEndpoint  # noqa: E402
from docurandom.workspace import Workspace  # noqa: E402
from docurandom.youtube_upload import (  # noqa: E402
    _CdpSession,
    _SemanticCdpController,
    _browser_extension_tab_for_target,
    _cdp_page_target_id,
    _ensure_youtube_browser_bridge,
    _mcp_close_cdp_page_target,
    _mcp_open_minimized_cdp_window_ws_url,
    _mcp_set_cdp_window_state,
    _youtube_metadata_ui_guard,
    YouTubeStudioUploader,
    YouTubeUploadRequest,
    resolve_workspace_upload_sources,
)


def _load_workspace(root: Path) -> Workspace:
    value = json.loads(
        (root / ".docurandom" / "workspace.json").read_text(encoding="utf-8-sig")
    )
    if not isinstance(value, dict):
        raise ValueError("workspace.json không hợp lệ")
    return Workspace(**value)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Repair and live-verify one existing video's custom thumbnail."
    )
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--video-url", required=True)
    parser.add_argument("--thumbnail", type=Path, required=True)
    parser.add_argument("--privacy", default="PUBLIC")
    args = parser.parse_args()

    workspace = _load_workspace(args.workspace.expanduser().resolve(strict=True))
    thumbnail = args.thumbnail.expanduser().resolve(strict=True)
    sources = resolve_workspace_upload_sources(
        workspace,
        thumbnail_path=thumbnail,
    )
    request = YouTubeUploadRequest(
        workspace=workspace,
        endpoint=DonutMcpEndpoint.parse(args.endpoint),
        profile_id=args.profile_id,
        privacy=args.privacy,
        contains_synthetic_media=True,
        video_path=sources.video_path,
        package_path=sources.package_path,
        thumbnail_path=thumbnail,
    )
    client = DonutMcpClient(request.endpoint, timeout=45)
    client.connect()
    client.require_docurandom_local()
    profiles = {profile.profile_id: profile for profile in client.list_profiles()}
    profile = profiles.get(args.profile_id)
    if profile is None:
        raise RuntimeError("Không tìm thấy profile đã chọn.")
    debug = client.get_debug_connection(profile.profile_id)

    def report(percent: float, message: str) -> None:
        print(
            json.dumps(
                {"progress": percent, "message": message},
                ensure_ascii=False,
            ),
            flush=True,
        )

    repair_ws = _mcp_open_minimized_cdp_window_ws_url(
        client,
        profile.profile_id,
        debug,
        "about:blank",
    )
    repair_target = _cdp_page_target_id(repair_ws)
    try:
        _mcp_set_cdp_window_state(
            client,
            profile.profile_id,
            repair_target,
            "parked",
        )
        bridge = _ensure_youtube_browser_bridge(client, profile.profile_id)
        _bridge, tab_id = _browser_extension_tab_for_target(
            client,
            profile.profile_id,
            repair_target,
            bridge=bridge,
        )
        with _CdpSession(repair_ws) as cdp:
            controller = _SemanticCdpController(
                cdp,
                browser_bridge=bridge,
                bridge_profile_id=profile.profile_id,
                bridge_tab_id=tab_id,
                automation_target_id=repair_target,
                window_state=lambda state: _mcp_set_cdp_window_state(
                    client,
                    profile.profile_id,
                    repair_target,
                    state,
                ),
            )
            controller.enable()
            uploader = YouTubeStudioUploader()
            cancel = threading.Event()
            with _youtube_metadata_ui_guard(cancel, report):
                uploader._persist_and_verify_saved_metadata(
                    request,
                    sources,
                    controller,
                    args.video_url,
                    report,
                    cancel,
                )
        print(
            json.dumps(
                {
                    "result": "success",
                    "profile": profile.name,
                    "video_url": args.video_url,
                    "thumbnail": str(thumbnail),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    finally:
        _mcp_close_cdp_page_target(
            client,
            profile.profile_id,
            debug,
            repair_target,
        )


if __name__ == "__main__":
    raise SystemExit(main())
