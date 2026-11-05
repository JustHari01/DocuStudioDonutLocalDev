from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from docurandom.content_ai import (
    CHATGPT_WEB_DEFAULT_MODEL_LABEL,
    ChatGPTWebChat,
    check_chatgpt_web_bridge,
    resolve_chatgpt_web_model,
)
from docurandom.thumbnail_ai import (
    claim_thumbnail_ai_generation,
    is_thumbnail_confirmation,
    thumbnail_confirmation_request,
    thumbnail_generation_request,
    update_thumbnail_ai_state,
)
from docurandom.thumbnail_manager import install_workspace_thumbnail
from docurandom.workspace import Workspace


def _workspace(root: Path) -> Workspace:
    metadata_path = root / ".docurandom" / "workspace.json"
    if metadata_path.is_file():
        raw = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        return Workspace(**raw)
    now = datetime.now(timezone.utc).isoformat()
    workspace = Workspace(
        "live-thumbnail-smoke-2.3.75",
        "Live Thumbnail Smoke 2.3.75",
        str(root),
        now,
        now,
    )
    workspace.ensure_layout()
    return workspace


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Run one real ChatGPT Web thumbnail round-trip without UI control."
    )
    parser.add_argument("package", type=Path)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("artifacts/live-thumbnail-smoke-2.3.75"),
    )
    args = parser.parse_args()

    package = args.package.expanduser().resolve(strict=True)
    workspace = _workspace(args.output_root.expanduser().resolve())
    claim = claim_thumbnail_ai_generation(workspace, package)
    if claim.action == "reuse" and claim.existing_path is not None:
        print(
            json.dumps(
                {
                    "result": "reused",
                    "thumbnail": str(claim.existing_path),
                    "detail": claim.detail,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if claim.action != "start":
        print(
            json.dumps(
                {"result": "blocked", "detail": claim.detail},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    status = check_chatgpt_web_bridge(auto_start=True, timeout=15.0)
    if not status.operational:
        update_thumbnail_ai_state(
            workspace,
            package,
            claim,
            status="preflight_error",
            detail=status.detail,
            source="live-smoke",
        )
        print(
            json.dumps(
                {
                    "result": "preflight_error",
                    "bridge_version": status.bridge_version,
                    "detail": status.detail,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 3

    submitted = False

    def report(phase: str):
        def callback(stage: str, message: str) -> None:
            nonlocal submitted
            if stage == "submitted":
                submitted = True
                update_thumbnail_ai_state(
                    workspace,
                    package,
                    claim,
                    status="submitted",
                    detail="ChatGPT đã nhận đúng một yêu cầu tạo ảnh thật.",
                    source="live-smoke",
                )
            print(
                json.dumps(
                    {"event": "status", "phase": phase, "stage": stage, "message": message},
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return callback

    model = resolve_chatgpt_web_model(
        CHATGPT_WEB_DEFAULT_MODEL_LABEL,
        status.available_models,
    )
    chat = ChatGPTWebChat(
        status.endpoint,
        model,
        workspace.root,
        status.access_token,
    )
    try:
        generated = chat.generate_image(
            thumbnail_generation_request(claim.prompt),
            on_status=report("generate"),
        )
        update_thumbnail_ai_state(
            workspace,
            package,
            claim,
            status="image_received",
            detail=f"Đã nhận ảnh thật {generated.width}x{generated.height} từ bridge.",
            response_id=generated.response_id,
            source="live-smoke",
        )
        confirmation = chat.send(
            thumbnail_confirmation_request(),
            on_status=report("confirm"),
            expected_exact="ĐÃ TẠO",
        )
        if not is_thumbnail_confirmation(confirmation):
            raise RuntimeError(f"ChatGPT xác nhận không đạt: {confirmation}")
        installed = install_workspace_thumbnail(workspace, generated.data)
        update_thumbnail_ai_state(
            workspace,
            package,
            claim,
            status="complete",
            detail=f"Đã tạo ảnh thật và xác nhận {confirmation}.",
            thumbnail_path=installed.path,
            response_id=generated.response_id,
            source="live-smoke",
        )
        print(
            json.dumps(
                {
                    "result": "success",
                    "bridge_version": status.bridge_version,
                    "model": model,
                    "confirmation": confirmation,
                    "width": installed.inspection.width,
                    "height": installed.inspection.height,
                    "size": installed.inspection.size,
                    "sha256": installed.inspection.sha256,
                    "thumbnail": str(installed.path),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    except Exception as exc:
        update_thumbnail_ai_state(
            workspace,
            package,
            claim,
            status="uncertain" if submitted else "preflight_error",
            detail=str(exc),
            response_id=chat.response_id,
            source="live-smoke",
            submitted=submitted,
        )
        print(
            json.dumps(
                {
                    "result": "uncertain" if submitted else "preflight_error",
                    "detail": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
