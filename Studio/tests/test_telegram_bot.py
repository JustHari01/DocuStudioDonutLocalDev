from __future__ import annotations

import io
import json
import time
import urllib.error
from unittest.mock import patch

import pytest

from docurandom.telegram_bot import (
    BOT_COMMANDS,
    TelegramBotService,
    TelegramBotClient,
    TelegramBotError,
    TelegramBotSettings,
    TelegramPollingConflict,
    format_telegram_notification,
    split_telegram_message,
    telegram_main_keyboard,
    telegram_remove_keyboard,
    validate_bot_token,
)


VALID_TOKEN = "123456789:" + "A" * 35


class _Response:
    def __init__(self, payload: object) -> None:
        self._body = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self._body.read()


class _BinaryResponse:
    def __init__(self, payload: bytes) -> None:
        self._body = io.BytesIO(payload)

    def __enter__(self) -> "_BinaryResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


def test_bot_token_validation_rejects_incomplete_values() -> None:
    assert validate_bot_token(VALID_TOKEN) == VALID_TOKEN
    with pytest.raises(TelegramBotError):
        validate_bot_token("123:short")


def test_long_messages_split_without_losing_text() -> None:
    text = ("Một dòng thông báo\n" * 500).strip()
    chunks = split_telegram_message(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= 3900 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_client_uses_bot_api_and_parses_success_result() -> None:
    with patch(
        "docurandom.telegram_bot.request.urlopen",
        return_value=_Response(
            {"ok": True, "result": {"id": 7, "is_bot": True, "username": "YTJPbot"}}
        ),
    ) as opener:
        identity = TelegramBotClient(VALID_TOKEN).get_me()

    assert identity["username"] == "YTJPbot"
    sent_request = opener.call_args.args[0]
    assert sent_request.full_url.endswith("/getMe")


def test_get_updates_identifies_another_active_bot_poller() -> None:
    body = io.BytesIO(
        json.dumps(
            {
                "ok": False,
                "description": (
                    "Conflict: terminated by other getUpdates request; "
                    "make sure that only one bot instance is running"
                ),
            }
        ).encode("utf-8")
    )
    conflict = urllib.error.HTTPError(
        "https://api.telegram.org/getUpdates",
        409,
        "Conflict",
        {},
        body,
    )
    with patch(
        "docurandom.telegram_bot.request.urlopen",
        side_effect=conflict,
    ):
        with pytest.raises(TelegramPollingConflict):
            TelegramBotClient(VALID_TOKEN).get_updates(offset=None, timeout=0)


def test_polling_conflict_keeps_outbound_notifications_working() -> None:
    sent: list[str] = []

    class _ConflictedClient:
        def __init__(self, _token: str, *, timeout: float = 12.0) -> None:
            self.timeout = timeout

        def get_me(self) -> dict[str, object]:
            return {"username": "YTJPbot"}

        def set_my_commands(self) -> None:
            return None

        def get_updates(
            self,
            *,
            offset: int | None,
            timeout: int,
        ) -> list[dict[str, object]]:
            raise TelegramPollingConflict("another getUpdates consumer")

        def send_message(
            self,
            _chat_id: int,
            text: str,
            **_kwargs: object,
        ) -> None:
            sent.append(text)

    settings = TelegramBotSettings(
        enabled=True,
        token=VALID_TOKEN,
        chat_id=12345,
        bot_username="YTJPbot",
        notify_status=True,
    )
    with (
        patch("docurandom.telegram_bot.load_telegram_settings", return_value=settings),
        patch("docurandom.telegram_bot.save_telegram_settings"),
        patch("docurandom.telegram_bot.TelegramBotClient", _ConflictedClient),
    ):
        service = TelegramBotService(lambda _command: "ok")
        try:
            service.notify("status", "Thông báo vẫn gửi được.")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not any(
                "Thông báo vẫn gửi được" in item for item in sent
            ):
                time.sleep(0.01)
            assert service.state().status == "send_only"
            assert any("Thông báo vẫn gửi được" in item for item in sent)
        finally:
            service.stop()


def test_client_can_send_html_notification() -> None:
    with patch(
        "docurandom.telegram_bot.request.urlopen",
        return_value=_Response({"ok": True, "result": {"message_id": 8}}),
    ) as opener:
        TelegramBotClient(VALID_TOKEN).send_message(
            12345,
            "<b>Trạng thái</b>",
            parse_mode="HTML",
        )

    payload = json.loads(opener.call_args.args[0].data.decode("utf-8"))
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == "<b>Trạng thái</b>"


def test_notification_formatter_is_readable_and_escapes_dynamic_data() -> None:
    formatted = format_telegram_notification(
        "error",
        "[Kênh <Nhật>] Công đoạn: VOICEVOX\n"
        "Ca: daily-15-05\n"
        "Chi tiết: Thiếu WAV & SRT\n"
        "Xử lý: Đã chuyển sang Skipped.",
    )

    assert "⚠️ <b>CẢNH BÁO</b>" in formatted
    assert "<code>Kênh &lt;Nhật&gt;</code>" in formatted
    assert "<code>daily-15-05</code>" in formatted
    assert "<blockquote>Thiếu WAV &amp; SRT</blockquote>" in formatted


def test_thumbnail_status_does_not_get_mislabeled_as_flow() -> None:
    formatted = format_telegram_notification(
        "status", "[DaiTa4] Công đoạn: Thumbnail\nChi tiết: Không tạo ảnh mới từ đầu."
    )
    assert "<b>THUMBNAIL</b>" in formatted
    assert "<b>FLOW</b>" not in formatted
    upload = format_telegram_notification(
        "status", "[DaiTa4] Công đoạn: Upload YouTube\nChi tiết: Đang xác minh thumbnail."
    )
    assert "<b>YOUTUBE</b>" in upload


def test_notification_formatter_uses_copy_block_for_video_link() -> None:
    formatted = format_telegram_notification(
        "status",
        "[Kênh Nhật] Video: Tin mới\nLink: https://youtu.be/abc?x=1&y=2",
    )

    assert "<pre>https://youtu.be/abc?x=1&amp;y=2</pre>" in formatted
    assert len(formatted) <= 3900


def test_bot_registers_command_suggestions_and_quick_keyboard() -> None:
    with patch(
        "docurandom.telegram_bot.request.urlopen",
        return_value=_Response({"ok": True, "result": True}),
    ) as opener:
        TelegramBotClient(VALID_TOKEN).set_my_commands()

    sent_request = opener.call_args.args[0]
    payload = json.loads(sent_request.data.decode("utf-8"))
    assert sent_request.full_url.endswith("/setMyCommands")
    assert payload["scope"] == {"type": "all_private_chats"}
    assert payload["commands"] == [
        {"command": command, "description": description}
        for command, description in BOT_COMMANDS
    ]
    keyboard = telegram_main_keyboard()
    assert keyboard["is_persistent"] is False
    assert any(button["text"] == "/status" for row in keyboard["keyboard"] for button in row)
    assert any(button["text"] == "/hide_menu" for row in keyboard["keyboard"] for button in row)
    assert any(command == "flow_restart" for command, _description in BOT_COMMANDS)
    assert any(button["text"] == "/flow_restart" for row in keyboard["keyboard"] for button in row)
    assert telegram_remove_keyboard() == {"remove_keyboard": True}


def test_client_downloads_telegram_file_with_hard_size_limit() -> None:
    payload = b"image-bytes"
    responses = [
        _Response(
            {
                "ok": True,
                "result": {
                    "file_id": "file-1",
                    "file_size": len(payload),
                    "file_path": "photos/demo.jpg",
                },
            }
        ),
        _BinaryResponse(payload),
    ]
    with patch(
        "docurandom.telegram_bot.request.urlopen",
        side_effect=responses,
    ) as opener:
        result = TelegramBotClient(VALID_TOKEN).download_file("file-1")

    assert result == payload
    assert "/file/bot" in opener.call_args_list[1].args[0].full_url
    assert opener.call_args_list[1].args[0].full_url.endswith("/photos/demo.jpg")


def test_client_sends_thumbnail_preview_as_real_photo() -> None:
    with patch(
        "docurandom.telegram_bot.request.urlopen",
        return_value=_Response({"ok": True, "result": {"message_id": 9}}),
    ) as opener:
        TelegramBotClient(VALID_TOKEN).send_photo(
            12345,
            b"jpeg-payload",
            filename="preview.jpg",
            caption="Thumbnail preview",
        )

    sent_request = opener.call_args.args[0]
    assert sent_request.full_url.endswith("/sendPhoto")
    assert sent_request.headers["Content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="chat_id"' in sent_request.data
    assert b"12345" in sent_request.data
    assert b'filename="preview.jpg"' in sent_request.data
    assert b"jpeg-payload" in sent_request.data


def test_largest_photo_variant_is_selected_for_thumbnail() -> None:
    candidate = TelegramBotService._attachment_candidate(
        {
            "photo": [
                {"file_id": "small", "file_unique_id": "a", "file_size": 20, "width": 90, "height": 90},
                {"file_id": "large", "file_unique_id": "b", "file_size": 200, "width": 1280, "height": 720},
            ]
        }
    )

    assert candidate is not None
    assert candidate[0] == "large"
    assert candidate[2] == "thumbnail.jpg"
