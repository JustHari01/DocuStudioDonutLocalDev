from __future__ import annotations

import argparse
from datetime import datetime
import json
import mimetypes
from pathlib import Path
import sys
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from docurandom.browser_extension_bridge import (  # noqa: E402
    BrowserExtensionBridgeError,
    get_browser_extension_bridge,
)
from docurandom.youtube_upload import (  # noqa: E402
    _DEEP_HELPER,
    _js_click,
    _js_focus_textbox,
)


DEFAULT_TEST_PROFILE = "4df23bcd-b166-4865-9b9e-d6805e11370f"
DEFAULT_VIDEO = Path(r"C:\Users\Hari\Desktop\cc\renderer_motion_smoke.mp4")
MIN_EXTENSION_VERSION = (1, 1, 1)


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except (TypeError, ValueError):
        return ()


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload YouTube demo bằng DocuRandom extension")
    parser.add_argument("--profile-id", default=DEFAULT_TEST_PROFILE)
    parser.add_argument("--video", type=Path, default=DEFAULT_VIDEO)
    parser.add_argument("--wait", type=float, default=600.0)
    args = parser.parse_args()
    video = args.video.expanduser().resolve(strict=True)

    bridge = get_browser_extension_bridge()
    bridge.start()
    print("BRIDGE_READY", flush=True)
    if not bridge.wait_for_profile(args.profile_id, timeout=args.wait):
        print("EXTENSION_TIMEOUT", flush=True)
        return 2
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        status = bridge.status(args.profile_id)
        if status.connected and _version_tuple(status.extension_version) >= MIN_EXTENSION_VERSION:
            break
        print(
            f"WAIT_EXTENSION_UPDATE current={status.extension_version or 'unknown'} required=1.1.1",
            flush=True,
        )
        time.sleep(1)
    else:
        print("EXTENSION_UPDATE_TIMEOUT", flush=True)
        return 4
    print(f"EXTENSION_CONNECTED version={status.extension_version}", flush=True)

    def run(expression: str, *, timeout: float = 30.0):
        return bridge.command(
            args.profile_id,
            "script_eval",
            {"expression": expression},
            timeout=timeout,
        )

    def wait(expression: str, label: str, *, timeout: float = 60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = run(expression)
            if value:
                print(label, flush=True)
                return value
            time.sleep(0.7)
        raise BrowserExtensionBridgeError(f"Hết thời gian: {label}")

    def set_text(selectors: tuple[str, ...], value: str) -> None:
        if not run(_js_focus_textbox(selectors)):
            raise BrowserExtensionBridgeError("Không tìm thấy ô nhập văn bản YouTube.")
        bridge.command(
            args.profile_id,
            "insert_text",
            {"text": value},
            timeout=30,
        )

    try:
        bridge.command(
            args.profile_id,
            "navigate",
            {"url": "https://studio.youtube.com/", "new_tab": True},
            timeout=20,
        )
        state = wait(
            "(()=>document.body?({url:location.href,title:document.title,text:(document.body.innerText||'').slice(0,2000)}):null)()",
            "YOUTUBE_STUDIO_LOADED",
            timeout=90,
        )
        state_text = json.dumps(state, ensure_ascii=False).casefold()
        if "accounts.google.com" in state_text or "sign in" in state_text or "đăng nhập" in state_text:
            raise BrowserExtensionBridgeError("Profile Test chưa đăng nhập YouTube Studio.")

        wait(
            _js_click(
                ("ytcp-button#create-icon", "#create-icon", "button[aria-label*=Create]"),
                ("Create", "Tạo", "作成"),
            ),
            "CREATE_MENU_OPENED",
            timeout=60,
        )
        time.sleep(0.8)
        wait(
            _js_click(
                ("tp-yt-paper-item#text-item-0", "[test-id=upload-beta]"),
                ("Upload videos", "Tải video lên", "動画をアップロード"),
            ),
            "UPLOAD_DIALOG_OPENED",
            timeout=30,
        )
        wait(
            "(()=>{" + _DEEP_HELPER + "return deepAll('input[type=file]').length>0})()",
            "FILE_INPUT_READY",
            timeout=45,
        )

        file_url = bridge.share_file(video, ttl=60 * 60)
        mime = mimetypes.guess_type(video.name)[0] or "video/mp4"
        attached = bridge.command(
            args.profile_id,
            "set_file_url",
            {
                "url": file_url,
                "name": video.name,
                "mime": mime,
                "accept_hint": "video",
            },
            timeout=120,
        )
        print("VIDEO_ATTACHED " + json.dumps(attached, ensure_ascii=False), flush=True)

        wait(
            "(()=>{" + _DEEP_HELPER + "return deepAll('#title-textarea #textbox,[aria-label*=title][contenteditable=true]').length>0})()",
            "DETAILS_READY",
            timeout=90,
        )
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        set_text(
            ("#title-textarea #textbox", "[aria-label*=title][contenteditable=true]"),
            f"DocuRandom Browser Bridge Upload Test · {stamp}",
        )
        set_text(
            ("#description-textarea #textbox", "[aria-label*=description][contenteditable=true]"),
            "Video kiểm tra upload tự động bằng DocuRandom Browser Bridge. Chế độ không công khai.",
        )
        print("METADATA_FILLED", flush=True)

        if not run(
            _js_click(
                (
                    'tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]',
                    '[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]',
                ),
                (
                    "No, it's not made for kids",
                    "Không, nội dung này không dành cho trẻ em",
                    "いいえ、子ども向けではありません",
                ),
            )
        ):
            raise BrowserExtensionBridgeError("Không chọn được mục Không dành cho trẻ em.")

        run(
            _js_click(
                ("ytcp-button#toggle-button", "#toggle-button"),
                ("Show more", "Hiện thêm", "さらに表示"),
            )
        )
        time.sleep(0.5)
        if not run(
            _js_click(
                (
                    'tp-yt-paper-radio-button[name="VIDEO_HAS_ALTERED_CONTENT_YES"]',
                    '[name="VIDEO_HAS_ALTERED_CONTENT_YES"]',
                    "#altered-content-yes",
                ),
            )
        ):
            raise BrowserExtensionBridgeError("Không chọn được khai báo nội dung có sử dụng AI.")
        print("DISCLOSURES_SELECTED", flush=True)

        for index in range(3):
            wait(
                _js_click(
                    ("#next-button", "ytcp-button#next-button"),
                    ("Next", "Tiếp", "次へ"),
                ),
                f"NEXT_STEP_{index + 1}",
                timeout=120 if index == 1 else 60,
            )
            time.sleep(0.8)

        if not run(
            _js_click(
                (
                    'tp-yt-paper-radio-button[name="UNLISTED"]',
                    '[name="UNLISTED"]',
                ),
            )
        ):
            raise BrowserExtensionBridgeError("Không chọn được chế độ Không công khai.")
        print("PRIVACY_UNLISTED_SELECTED", flush=True)

        wait(
            _js_click(
                ("#done-button", "ytcp-button#done-button"),
                ("Save", "Lưu", "保存"),
            ),
            "YOUTUBE_SAVE_ACCEPTED",
            timeout=60 * 30,
        )
        time.sleep(2)
        youtube_url = run(
            "(()=>{" + _DEEP_HELPER + "const links=deepAll('a[href*=youtu],a[href*=youtube.com/watch]');return links.map(x=>x.href).find(Boolean)||''})()"
        )
        print(f"UPLOAD_COMPLETE url={youtube_url or ''}", flush=True)
        return 0
    except (BrowserExtensionBridgeError, OSError) as exc:
        print(f"UPLOAD_ERROR {exc}", flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
