from __future__ import annotations

import json
import sys
import time
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
from docurandom.storage import atomic_write_json
from docurandom.thumbnail_ai import is_thumbnail_confirmation


RESULT_PATH = PROJECT_ROOT / "artifacts" / "live-exact-confirmation-2.3.75.json"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    existing: dict[str, object] = {}
    try:
        value = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
        existing = value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    if existing.get("status") in {"success", "submitted", "uncertain"}:
        print(json.dumps({"result": "not-repeated", **existing}, ensure_ascii=False))
        return 0 if existing.get("status") == "success" else 2

    status = check_chatgpt_web_bridge(auto_start=True, timeout=15.0)
    if not status.operational:
        atomic_write_json(RESULT_PATH, {"status": "preflight_error", "detail": status.detail})
        print(json.dumps({"result": "preflight_error", "detail": status.detail}, ensure_ascii=False))
        return 3
    model = resolve_chatgpt_web_model(
        CHATGPT_WEB_DEFAULT_MODEL_LABEL,
        status.available_models,
    )
    chat = ChatGPTWebChat(status.endpoint, model, RESULT_PATH.parent, status.access_token)
    submitted = False
    started = time.monotonic()

    def report(stage: str, message: str) -> None:
        nonlocal submitted
        if stage == "submitted" and not submitted:
            submitted = True
            atomic_write_json(
                RESULT_PATH,
                {"status": "submitted", "detail": "Đã gửi đúng một lượt xác nhận."},
            )
        print(
            json.dumps({"event": "status", "stage": stage, "message": message}, ensure_ascii=False),
            flush=True,
        )

    try:
        answer = chat.send(
            "Đây là kiểm thử detector trạng thái của DocuRandom. Chỉ trả lời chính xác: ĐÃ TẠO",
            on_status=report,
            expected_exact="ĐÃ TẠO",
        )
        elapsed = round(time.monotonic() - started, 3)
        if not is_thumbnail_confirmation(answer):
            raise RuntimeError(f"Phản hồi không đúng: {answer}")
        result = {
            "status": "success",
            "answer": answer,
            "elapsed_seconds": elapsed,
            "bridge_version": status.bridge_version,
            "model": model,
        }
        atomic_write_json(RESULT_PATH, result)
        print(json.dumps({"result": "success", **result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        result = {
            "status": "uncertain" if submitted else "preflight_error",
            "detail": str(exc),
        }
        atomic_write_json(RESULT_PATH, result)
        print(json.dumps({"result": result["status"], **result}, ensure_ascii=False))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
