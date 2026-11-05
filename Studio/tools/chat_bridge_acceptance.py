"""Controlled, single-chat runtime acceptance for the installed Chat Bridge.

No production workspace thread, video prompt, or VOICEVOX project is changed.
The bridge token is only read from its protected local descriptor, never logged.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
import urllib.request

from docurandom.content_ai import (
    CHATGPT_WEB_AUTO_MODEL,
    ChatAttachment,
    ChatGPTWebChat,
    _chatgpt_web_bridge_control,
)


DIAGNOSTIC_THREAD = "thread_docurandom_exact_output_20260925_01"
TEXT_MARKER = "DOCURANDOM-EXACT-OK-20260926-1.0.84"
FILE_MARKER = "DOCURANDOM-FILE-OK-20260926-1.0.84"


def diagnostic(endpoint: str, token: str) -> dict[str, object]:
    body = json.dumps({"thread_id": DIAGNOSTIC_THREAD}).encode("utf-8")
    request = urllib.request.Request(
        endpoint + "/v1/session/diagnostic",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            payload = json.loads(error.read(1024 * 1024).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise
        message = str((payload.get("error") or {}).get("message") or "")
        if error.code != 404 or message != "ChatGPT conversation context is not available":
            raise
        # A fresh, dedicated acceptance thread has no prior context, which is
        # safe to initialize with the one exact marker turn below.
        return {
            "context_available": False,
            "saved_url": "",
            "current_url": "",
            "pending_submission": False,
            "pending_attachment": False,
            "snapshot": {"assistantCount": 0, "userCount": 0},
            "page_snapshot": {},
        }
    if not isinstance(result, dict):
        raise RuntimeError("Invalid diagnostic response")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("diagnose", "recover", "text", "attachment"))
    args = parser.parse_args()
    control = _chatgpt_web_bridge_control()
    if control is None:
        raise RuntimeError("Installed Chat Bridge is not running")
    endpoint, token = control
    state = diagnostic(endpoint, token)
    page = state.get("page_snapshot") or {}
    snapshot = state.get("snapshot") or {}
    print(json.dumps({
        "saved_url": state.get("saved_url"),
        "current_url": state.get("current_url"),
        "context_available": state.get("context_available", True),
        "pending_submission": state.get("pending_submission"),
        "pending_attachment": state.get("pending_attachment"),
        "assistant_count": snapshot.get("assistantCount") if isinstance(snapshot, dict) else None,
        "user_count": snapshot.get("userCount") if isinstance(snapshot, dict) else None,
        "visible_composers": page.get("visibleComposers") if isinstance(page, dict) else None,
        "rate_limited": page.get("rateLimited") if isinstance(page, dict) else None,
        "signed_out": page.get("signedOut") if isinstance(page, dict) else None,
        "page_ready": page.get("ready") if isinstance(page, dict) else None,
        "page_title": str(page.get("title") or "")[:100] if isinstance(page, dict) else None,
        "page_probe_message": str(page.get("probeError") or "")[:120] if isinstance(page, dict) else None,
        "page_probe_error": bool(state.get("page_snapshot_error")),
    }, ensure_ascii=False))
    if args.mode == "diagnose":
        return 0
    chat = ChatGPTWebChat(
        endpoint,
        CHATGPT_WEB_AUTO_MODEL,
        Path.cwd(),
        token,
        thread_id=DIAGNOSTIC_THREAD,
    )
    if args.mode == "recover":
        if not state.get("pending_submission"):
            print("No submitted turn to recover.")
            return 0
        recovered = chat.recover_pending(
            on_status=lambda stage, _message: print(f"stage={stage}")
        )
        text = recovered if isinstance(recovered, str) else str(recovered or "")
        print(f"recovered_text_length={len(text)} sample={text[:160]!r}")
        return 0 if text else 2
    if state.get("pending_submission") or state.get("pending_attachment"):
        print("Diagnostic chat still has an unresolved turn or attachment; no prompt sent.")
        return 3
    if args.mode == "text":
        result = chat.send(
            f"Chỉ trả lời đúng chuỗi sau, không thêm nội dung: {TEXT_MARKER}",
            on_status=lambda stage, _message: print(f"stage={stage}"),
        )
        print(f"result={result[:160]!r}")
        return 0 if result == TEXT_MARKER else 4
    fixture = b'{"schemaVersion":1,"kind":"VOICEVOX_PROJECT_AUDIT_TEST","digest":"abc123"}'
    result = chat.send(
        f"Đọc tệp kiểm thử nhỏ và chỉ trả lời {FILE_MARKER} nếu có schemaVersion bằng 1.",
        attachments=(ChatAttachment("VOICEVOX-audit-fixture.txt", fixture),),
        on_status=lambda stage, _message: print(f"stage={stage}"),
    )
    print(f"result={result[:160]!r}")
    return 0 if FILE_MARKER in result else 5


if __name__ == "__main__":
    raise SystemExit(main())
