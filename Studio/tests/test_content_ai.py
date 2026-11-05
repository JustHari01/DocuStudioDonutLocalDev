from __future__ import annotations

import dataclasses
import base64
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from docurandom.content_ai import (
    CHATGPT_WEB_BRIDGE_VERSION,
    CHATGPT_WEB_DEFAULT_MODEL_LABEL,
    ChatAttachment,
    ChatGPTWebChat,
    ChatGPTWebStatus,
    ChatTurn,
    CodexCliChat,
    CodexCliStatus,
    ContentAICancelled,
    ContentAIError,
    ContentAISubmissionUncertain,
    ContentArtifacts,
    FINAL_EXPORT_REQUEST,
    MasterPromptRunner,
    artifacts_complete,
    check_chatgpt_web_bridge,
    check_codex_cli,
    extract_content_artifacts,
    ensure_chatgpt_web_codex_profile,
    discover_codex_cli,
    expected_image_prompt_count,
    extract_voicevox_script_from_package,
    format_content_package,
    final_export_reply_in_turns,
    harden_chatgpt_web_profile,
    install_chatgpt_web_launcher,
    install_codex_cli,
    image_prompt_phase_started,
    is_image_prompt_done_confirmation,
    is_image_prompt_not_done_confirmation,
    is_master_prompt_attachment_failure,
    master_prompt_attachment_retry_pending_in_turns,
    is_master_prompt_loaded_confirmation,
    load_completed_content_checkpoint,
    read_master_prompt,
    repair_chatgpt_web_bridge,
    resolve_chatgpt_web_model,
    _chatgpt_web_session_status,
    _chatgpt_web_runtime_status,
    _chatgpt_web_has_active_turns,
    _codex_event_error_message,
    _codex_stderr_error_detail,
    _inspect_chatgpt_web_session,
    _request_chatgpt_web_bridge_shutdown,
    _request_chatgpt_web_bridge_repair,
    stop_all_chatgpt_web_bridges,
    stop_chatgpt_web_bridge_for_update,
    _PREVIOUS_FINAL_EXPORT_REQUEST,
    _validated_chatgpt_conversation_url,
)


class ConversationRecoveryUrlTests(unittest.TestCase):
    def test_provisional_urls_are_rejected_without_releasing_submission(self):
        for suffix in ("local-chatgpt:abc", "local-chatgpt%3Aabc", "LOCAL-CHATGPT%3aabc"):
            with self.subTest(suffix=suffix), self.assertRaises(ContentAIError):
                _validated_chatgpt_conversation_url("https://chatgpt.com/c/" + suffix)
        self.assertEqual(
            _validated_chatgpt_conversation_url("https://chatgpt.com/c/saved-server-chat"),
            "https://chatgpt.com/c/saved-server-chat",
        )

    def test_account_failure_does_not_start_another_dom_recovery(self):
        detail = "CHATGPT_ACCOUNT_LOAD_FAILED: checkpoint vẫn được giữ, không gửi lại prompt."
        for transport in (CodexCliChat, ChatGPTWebChat):
            with self.subTest(transport=transport.__name__):
                self.assertFalse(transport._is_recoverable_submitted_error(detail))


class FakeChat:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.attachments: list[list[ChatAttachment]] = []
        self.thread_id = "thread-test"
        self.cancelled = False

    def send(
        self,
        prompt: str,
        *,
        cancel_event=None,
        on_partial=None,
        on_status=None,
        attachments=(),
    ) -> str:
        if cancel_event is not None and cancel_event.is_set():
            raise ContentAICancelled()
        self.prompts.append(prompt)
        self.attachments.append(list(attachments))
        if not self.responses:
            raise AssertionError("unexpected prompt")
        response = self.responses.pop(0)
        if on_partial is not None:
            on_partial(response)
        if on_status is not None:
            on_status("waiting", "ChatGPT đã nhận yêu cầu")
        return response

    def cancel(self) -> None:
        self.cancelled = True


class FinalExportRequestTests(unittest.TestCase):
    def test_final_export_requests_plain_text_and_recovers_previous_prompt_identity(self) -> None:
        self.assertIn("dạng văn bản thuần", FINAL_EXPORT_REQUEST)
        self.assertIn("Không tạo hoặc đính kèm tệp", FINAL_EXPORT_REQUEST)
        self.assertNotIn("Export cho tôi 1 file", FINAL_EXPORT_REQUEST)
        response = "Document-final.txt\nDocument"
        self.assertEqual(
            final_export_reply_in_turns([
                ChatTurn("user", _PREVIOUS_FINAL_EXPORT_REQUEST),
                ChatTurn("assistant", response),
            ]),
            response,
        )


class CodexErrorDisplayTests(unittest.TestCase):
    def test_turn_failed_prefers_nested_actionable_message(self) -> None:
        self.assertEqual(
            _codex_event_error_message(
                {
                    "type": "turn.failed",
                    "error": {
                        "message": (
                            "Gửi nội dung: ChatGPT không ghi đủ nội dung "
                            "vào ô soạn thảo"
                        )
                    },
                }
            ),
            "Gửi nội dung: ChatGPT không ghi đủ nội dung vào ô soạn thảo",
        )

    def test_stderr_fallback_hides_routine_warnings(self) -> None:
        detail = _codex_stderr_error_detail(
            [
                "2026-08-20 WARN Unknown model chatgpt-web/high",
                "2026-08-20 WARN Shell snapshot not supported yet",
                "fatal: transport closed",
            ]
        )
        self.assertEqual(detail, "fatal: transport closed")

    def test_stream_wrapper_is_removed_from_actionable_bridge_error(self) -> None:
        self.assertEqual(
            _codex_event_error_message(
                {
                    "type": "error",
                    "message": (
                        "stream disconnected before completion: "
                        "Đọc phản hồi: DOM phản hồi ChatGPT đã biến mất"
                    ),
                }
            ),
            "Đọc phản hồi: DOM phản hồi ChatGPT đã biến mất",
        )

    def test_bridge_inactivity_timeout_is_localized_and_keeps_checkpoint_action(self) -> None:
        message = _codex_event_error_message(
            {
                "type": "turn.failed",
                "error": {
                    "message": (
                        "Đọc phản hồi: ChatGPT response stopped making progress "
                        "before the inactivity timeout"
                    )
                },
            }
        )
        self.assertIn("ngừng cập nhật", message)
        self.assertIn("checkpoint", message)
        self.assertIn("tự nối lại", message)
        self.assertIn("không gửi trùng", message)


class ContentArtifactTests(unittest.TestCase):
    def test_extracts_parts_and_export_contract(self) -> None:
        turns = [
            ChatTurn(
                "assistant",
                """
[[chu-de-con-duoc-chon]]
Chủ đề đã chọn
[[end-chu-de-con-duoc-chon]]
[[tieu-de-duoc-chon]]
日本の工作機械
[[end-tieu-de-duoc-chon]]
""",
            ),
            ChatTurn(
                "assistant",
                """
[[part-2]]
PHẦN 2
A: 二行目です。
[[end-part-2]]
""",
            ),
            ChatTurn(
                "assistant",
                """
[[part-1]]
一行目です。

続きです。
[[end-part-1]]
""",
            ),
            ChatTurn(
                "assistant",
                """
[[mo-ta]]
日本語の説明です。
[[end-mo-ta]]
[[prompt-thumbnail]]
PROMPT THUMBNAIL HOÀN CHỈNH: dramatic factory
[[end-prompt-thumbnail]]
[[prompt-hinh-anh]]
Image 2. second scene

Image 1. first scene
[[end-prompt-hinh-anh]]
""",
            ),
        ]
        artifacts = extract_content_artifacts(turns)
        self.assertEqual(artifacts.title, "日本の工作機械")
        self.assertEqual(
            artifacts.voicevox_script,
            "一行目です。\n続きです。\n二行目です。",
        )
        self.assertEqual(
            artifacts.voicevox_export_script,
            "A: 一行目です。\nA: 続きです。\nA: 二行目です。",
        )
        self.assertEqual(artifacts.image_prompts, ("first scene", "second scene"))
        self.assertTrue(artifacts_complete(artifacts, 2))

        package = format_content_package(artifacts)
        self.assertIn("TIÊU ĐỀ\n\n日本の工作機械", package)
        self.assertIn("THOẠI VOICEVOX\n\nA: 一行目です。", package)
        self.assertIn("Image 1. first scene\n\nImage 2. second scene", package)
        self.assertNotIn("PHẦN 2", package)

        self.assertNotIn("CHECKLIST NỘI DUNG ĐÃ LÀM (CHỐNG TRÙNG)", package)

        corrected = extract_content_artifacts(
            [
                *turns,
                ChatTurn(
                    "assistant",
                    "[[part-1]]修正版です。[[end-part-1]]",
                ),
            ]
        )
        self.assertTrue(corrected.voicevox_script.startswith("修正版です。"))

        gapped = extract_content_artifacts(
            [
                ChatTurn(
                    "assistant",
                    (
                        "[[prompt-hinh-anh]]\n"
                        "Image 1. first\n\n"
                        "Image 3. third\n"
                        "[[end-prompt-hinh-anh]]"
                    ),
                )
            ]
        )
        self.assertEqual(gapped.image_prompts, ("first",))

    def test_read_master_prompt_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "master.txt"
            path.write_text("BẮT ĐẦU — 日本語", encoding="utf-8-sig")
            self.assertEqual(read_master_prompt(path), "BẮT ĐẦU — 日本語")

    def test_web_network_error_explains_safe_stop_before_start(self) -> None:
        raw = json.dumps(
            {
                "error": {
                    "message": (
                        "ChatGPT returned a network error while processing this message; "
                        "the Master Prompt may exceed the current model/session limit"
                    )
                }
            }
        ).encode("utf-8")
        message = ChatGPTWebChat._error_message(500, raw)
        self.assertIn("vượt giới hạn", message)
        self.assertIn("dừng trước bước BẮT ĐẦU", message)

    def test_web_too_many_requests_preserves_retry_after_pause(self) -> None:
        raw = json.dumps(
            {
                "error": {
                    "message": (
                        "CHATGPT_RATE_LIMITED: Too many requests. "
                        "You're making requests too quickly."
                    )
                }
            }
        ).encode("utf-8")
        message = ChatGPTWebChat._error_message(500, raw)
        self.assertIn("quá nhiều yêu cầu", message)
        self.assertIn("Retry-After", message)
        self.assertIn("giữ checkpoint", message)
        self.assertIn("giữ nguyên tài khoản/model", message)

    def test_web_renderer_error_preserves_bounded_page_diagnostics(self) -> None:
        raw = json.dumps(
            {
                "error": {
                    "message": (
                        "ChatGPT composer was not available; sign in again or check the page "
                        "(url=https://chatgpt.com/c/example, title=Saved chat, "
                        "visibleComposers=0, readyState=complete, visibility=visible, "
                        "signedOut=false, rateLimited=false)"
                    )
                }
            }
        ).encode("utf-8")
        message = ChatGPTWebChat._error_message(500, raw)
        self.assertIn("Bridge không đọc được giao diện ChatGPT hiện tại", message)
        self.assertIn("visibleComposers=0", message)
        self.assertIn("signedOut=false", message)
        self.assertLessEqual(len(message), 1_000)


class MasterPromptRunnerTests(unittest.TestCase):
    def test_repair_cannot_duplicate_voice_or_undo_later_title_correction(self):
        from docurandom.content_ai import CONTENT_REPAIR_PREFIX, _content_repair_request
        turns = [
            ChatTurn('assistant', '[[part-1]]first[[end-part-1]]\n[[part-2]]second[[end-part-2]]'),
            ChatTurn('user', CONTENT_REPAIR_PREFIX),
            ChatTurn('assistant', 'TIÊU ĐỀ\n\nstale title\n\nTHOẠI VOICEVOX\n\nA: first\nA: second'),
            ChatTurn('assistant', '[[tieu-de-duoc-chon]]corrected title[[end-tieu-de-duoc-chon]]'),
        ]
        artifacts = extract_content_artifacts(turns)
        self.assertEqual(artifacts.voicevox_script, 'first\nsecond')
        self.assertEqual(artifacts.title, 'corrected title')
        self.assertNotIn('[[part-1]]', _content_repair_request(artifacts, 20))
        turns[2] = ChatTurn('assistant', '[[part-1]]first\nsecond[[end-part-1]]')
        self.assertEqual(extract_content_artifacts(turns).voicevox_script, 'first\nsecond')

    def test_done_marker_requests_missing_package_instead_of_blind_continue(self):
        repaired = ("[[tieu-de-duoc-chon]]title[[end-tieu-de-duoc-chon]]\n"
                    "[[mo-ta]]description[[end-mo-ta]]\n"
                    "[[prompt-thumbnail]]thumbnail[[end-prompt-thumbnail]]\n"
                    "[[part-1]]voice[[end-part-1]]\n"
                    "[[prompt-image-1]]Image 1. image[[end-prompt-image-1]]")
        for exhausted in ("[[DONE]]", "Đã hết nội dung của lượt hiện tại. Không còn marker [[CONTINUE]]."):
            with self.subTest(exhausted=exhausted), tempfile.TemporaryDirectory() as temp:
                chat = FakeChat(["Đã nhận", "ĐÃ NẠP", exhausted, repaired, "ĐÃ XONG", repaired])
                runner = MasterPromptRunner(chat, output_dir=Path(temp), project_name="video", minutes=1)
                result = runner.run("MASTER")
                self.assertNotIn("TIẾP", chat.prompts)
                self.assertTrue(chat.prompts[3].startswith("KHÔI PHỤC PHẦN NỘI DUNG CÒN THIẾU"))
                self.assertEqual(result.artifacts.title, "title")
                self.assertEqual(result.artifacts.image_prompts, ("image",))

    def test_content_repair_limit_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            chat = FakeChat(["Đã nhận", "ĐÃ NẠP"] + ["[[DONE]]"] * 4)
            runner = MasterPromptRunner(chat, output_dir=Path(temp), project_name="video", minutes=1)
            with self.assertRaisesRegex(ContentAIError, "hết số lần khôi phục"):
                runner.run("MASTER")
            self.assertNotIn("TIẾP", chat.prompts)
            self.assertEqual(json.loads(runner.checkpoint_path.read_text(encoding='utf-8'))['content_repair_attempts'], 3)
            resumed_chat = FakeChat([])
            resumed = MasterPromptRunner(resumed_chat, output_dir=Path(temp), project_name="video", minutes=1)
            with self.assertRaisesRegex(ContentAIError, "hết số lần khôi phục"):
                resumed.run("MASTER")
            self.assertEqual(resumed_chat.prompts, [])

    def test_plain_package_reply_to_repair_is_recovered_from_transcript(self):
        from docurandom.content_ai import CONTENT_REPAIR_PREFIX
        package = "TIÊU ĐỀ\n\ntitle\n\nMÔ TẢ\n\ndescription\n\nPROMPT THUMBNAIL\n\nthumb\n\nTHOẠI VOICEVOX\n\nA: voice\n\nPROMPT HÌNH ẢNH\n\nImage 1. image"
        artifacts = extract_content_artifacts([
            ChatTurn('user', CONTENT_REPAIR_PREFIX + '\nrepair'), ChatTurn('assistant', package),
        ])
        self.assertEqual(artifacts.title, 'title')
        self.assertEqual(artifacts.image_prompts, ('image',))
        self.assertTrue(artifacts_complete(artifacts, 1))

    def test_append_turn_preserves_exact_assistant_output_whitespace(self) -> None:
        raw_output = "\n  Exact response with spacing. \t\n\n"
        callbacks: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as temporary:
            runner = MasterPromptRunner(
                FakeChat([]),  # type: ignore[arg-type]
                output_dir=Path(temporary),
                project_name="video",
                minutes=1,
            )
            runner._append_turn("assistant", raw_output, lambda role, text: callbacks.append((role, text)))
            saved = json.loads(runner.checkpoint_path.read_text(encoding="utf-8"))
            transcript = runner.transcript_path.read_text(encoding="utf-8-sig")

        self.assertEqual(runner.turns[0].text, raw_output)
        self.assertEqual(saved["turns"][0]["text"], raw_output)
        self.assertEqual(callbacks, [("assistant", raw_output)])
        self.assertEqual(transcript, f"===== TRỢ LÝ =====\n{raw_output}")

    def test_stops_before_start_when_checklist_is_not_confirmed(self) -> None:
        chat = FakeChat(
            [
                "Đã nhận Master Prompt.",
                "ĐÃ NẠP",
                "Tôi đã đọc checklist.",
                "Tôi hiểu rồi.",
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            runner = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=Path(temporary),
                project_name="video",
                minutes=1,
                title_history=("Nội dung cũ",),
            )
            with self.assertRaisesRegex(ContentAIError, "chưa trả đúng"):
                runner.run("MASTER")

        self.assertEqual(len(chat.prompts), 4)
        self.assertFalse(any(prompt.startswith("BẮT ĐẦU") for prompt in chat.prompts))

    def test_title_history_is_sent_and_duplicate_title_is_replaced_before_flow(self) -> None:
        duplicate_title = "日本のコンビニが社会の命綱になった理由"
        new_title = "日本の町工場が宇宙産業を支える理由"
        duplicate_response = f"""
[[tieu-de-duoc-chon]]{duplicate_title}[[end-tieu-de-duoc-chon]]
[[part-1]]古い内容です。[[end-part-1]]
[[mo-ta]]古い説明です。[[end-mo-ta]]
[[prompt-thumbnail]]old thumbnail[[end-prompt-thumbnail]]
[[prompt-hinh-anh]]
Image 1. old image
[[end-prompt-hinh-anh]]
"""
        corrected_response = f"""
[[tieu-de-duoc-chon]]{new_title}[[end-tieu-de-duoc-chon]]
[[part-1]]新しい内容です。[[end-part-1]]
[[mo-ta]]新しい説明です。[[end-mo-ta]]
[[prompt-thumbnail]]new thumbnail[[end-prompt-thumbnail]]
[[prompt-hinh-anh]]
Image 1. new image
[[end-prompt-hinh-anh]]
"""
        final_export = f"""TIÊU ĐỀ

{new_title}

MÔ TẢ

新しい説明です。

SEO tags

町工場, 宇宙産業

PROMPT THUMBNAIL

new thumbnail

THOẠI VOICEVOX

A: 新しい内容です。

PROMPT HÌNH ẢNH

Image 1. new image
"""
        chat = FakeChat(
            [
                "Đã nhận Master Prompt.",
                "ĐÃ NẠP",
                "Tôi đã đọc checklist.",
                "ĐÃ XONG",
                duplicate_response,
                corrected_response,
                "ĐÃ XONG",
                final_export,
            ]
        )
        statuses: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as temporary:
            result = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=Path(temporary),
                project_name="video",
                minutes=1,
                title_history=(duplicate_title,),
                title_history_total=1,
            ).run(
                "MASTER",
                on_status=lambda stage, message: statuses.append((stage, message)),
            )
            package = result.package_path.read_text(encoding="utf-8-sig")

        self.assertIn("CHECKLIST NỘI DUNG ĐÃ LÀM", chat.prompts[2])
        self.assertIn(duplicate_title, chat.prompts[2])
        self.assertIn("chỉ trả lời \"ĐÃ XONG\"", chat.prompts[3])
        self.assertTrue(chat.prompts[4].startswith("BẮT ĐẦU"))
        self.assertTrue(chat.prompts[5].startswith("CHỐNG TRÙNG"))
        self.assertEqual(result.artifacts.title, new_title)
        self.assertTrue(any(stage == "title-duplicate" for stage, _ in statuses))
        self.assertNotIn("CHECKLIST NỘI DUNG ĐÃ LÀM", package)
        self.assertNotIn(duplicate_title, package)

    def test_expected_images_follow_master_prompt_cadence(self) -> None:
        master = "QUY TẮC: ĐÚNG MỘT ẢNH MỖI 15 GIÂY."
        self.assertEqual(expected_image_prompt_count(master, 15), 60)
        self.assertEqual(expected_image_prompt_count("MASTER cũ", 2.5), 3)

    def test_three_missing_image_prompts_are_allowed_but_four_are_not(self) -> None:
        def artifacts(count: int) -> ContentArtifacts:
            return ContentArtifacts(
                title="title",
                description="description",
                thumbnail_prompt="thumbnail",
                voicevox_script="voice",
                voicevox_export_script="A: voice",
                image_prompts=tuple(f"image {index}" for index in range(count)),
            )

        self.assertTrue(
            artifacts_complete(
                artifacts(57), 15, expected_images=60, allowed_missing_images=3
            )
        )
        self.assertFalse(
            artifacts_complete(
                artifacts(56), 15, expected_images=60, allowed_missing_images=3
            )
        )

    def test_partial_checkpoint_resumes_same_thread_and_next_image(self) -> None:
        master = "Mỗi 30 giây phải có đúng một ảnh."
        first_chat = FakeChat([])
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            first = MasterPromptRunner(
                first_chat,  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            )
            first.expected_images = expected_image_prompt_count(master, 1)
            first.master_prompt_sha256 = hashlib.sha256(
                master.encode("utf-8")
            ).hexdigest()
            first.turns = [
                ChatTurn("user", master),
                ChatTurn("assistant", "Đã nhận."),
                ChatTurn(
                    "user",
                    'Nếu đã đọc toàn bộ Master Prompt thì hãy trả lời "ĐÃ NẠP" và không nói gì thêm.',
                ),
                ChatTurn("assistant", "ĐÃ NẠP"),
                ChatTurn("user", "BẮT ĐẦU\n\nThời lượng yêu cầu: 1 phút."),
                ChatTurn(
                    "assistant",
                    """
[[tieu-de-duoc-chon]]title[[end-tieu-de-duoc-chon]]
[[part-1]]voice[[end-part-1]]
[[mo-ta]]description[[end-mo-ta]]
[[prompt-thumbnail]]thumbnail[[end-prompt-thumbnail]]
[[prompt-hinh-anh]]
Image 1. first image
[[end-prompt-hinh-anh]]
""",
                ),
            ]
            first._save_checkpoint()

            resumed_chat = FakeChat(
                [
                    "CHƯA XONG",
                    "[[prompt-hinh-anh]]\nImage 2. second image\n[[end-prompt-hinh-anh]]",
                    "ĐÃ XONG",
                    "Đã xuất.",
                ]
            )
            resumed_chat.thread_id = "new-thread-placeholder"
            runner = MasterPromptRunner(
                resumed_chat,  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            )
            result = runner.run(master)

        self.assertEqual(resumed_chat.thread_id, "thread-test")
        self.assertNotIn(master, resumed_chat.prompts)
        self.assertEqual(resumed_chat.prompts[1], "TIẾP")
        self.assertEqual(result.artifacts.image_prompts, ("first image", "second image"))

    def test_failed_send_persists_thread_id_for_pending_recovery(self) -> None:
        class FailingChat(FakeChat):
            def send(
                self,
                prompt: str,
                *,
                cancel_event=None,
                on_partial=None,
                on_status=None,
                attachments=(),
            ) -> str:
                self.thread_id = "thread-after-started"
                raise ContentAIError("DOM conversation evidence unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            chat = FailingChat([])
            runner = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            )
            with self.assertRaisesRegex(ContentAIError, "DOM conversation evidence"):
                runner._send(
                    "MASTER",
                    cancel_event=threading.Event(),
                    on_turn=None,
                    on_partial=None,
                )
            checkpoint = json.loads(
                runner.checkpoint_path.read_text(encoding="utf-8")
            )

        self.assertEqual(checkpoint["thread_id"], "thread-after-started")
        self.assertEqual(checkpoint["turns"][-1], {"role": "user", "text": "MASTER"})

    def test_uncertain_submission_checkpoint_never_replays_prompt(self) -> None:
        class UncertainChat(FakeChat):
            def __init__(self) -> None:
                super().__init__([])
                self.thread_id = ""

            def send(
                self,
                prompt: str,
                *,
                cancel_event=None,
                on_partial=None,
                on_status=None,
                attachments=(),
            ) -> str:
                self.prompts.append(prompt)
                raise ContentAISubmissionUncertain(
                    "ChatGPT Web chưa tạo phiên; chưa xác nhận prompt đã được gửi."
                )

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            first_chat = UncertainChat()
            first = MasterPromptRunner(
                first_chat,  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            )
            with self.assertRaises(ContentAISubmissionUncertain):
                first.run("MASTER")
            checkpoint = json.loads(
                first.checkpoint_path.read_text(encoding="utf-8")
            )
            self.assertTrue(checkpoint["submission_uncertain"])

            second_chat = FakeChat(["should never be sent"])
            second = MasterPromptRunner(
                second_chat,  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            )
            with self.assertRaisesRegex(ContentAIError, "không tự gửi lại"):
                second.run("MASTER")

        self.assertEqual(second_chat.prompts, [])

    def test_trailing_user_turn_recovers_dom_before_sending_any_next_prompt(self) -> None:
        master = "MASTER"
        recovered_answer = """
[[tieu-de-duoc-chon]]タイトル[[end-tieu-de-duoc-chon]]
[[part-1]]一行目です。[[end-part-1]]
[[mo-ta]]説明です。[[end-mo-ta]]
[[prompt-thumbnail]]factory[[end-prompt-thumbnail]]
[[prompt-hinh-anh]]
Image 1. first image

Image 2. second image

Image 3. third image
[[end-prompt-hinh-anh]]
"""

        class RecoveringChat(FakeChat):
            def __init__(self) -> None:
                super().__init__(["ĐÃ XONG", "Đã xuất nội dung tổng hợp."])
                self.recovery_calls = 0

            def recover_pending(self, **kwargs) -> str:
                self.recovery_calls += 1
                partial = kwargs.get("on_partial")
                status = kwargs.get("on_status")
                if callable(partial):
                    partial(recovered_answer)
                if callable(status):
                    status("recovered", "Đã thu hồi DOM")
                return recovered_answer

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            seed = MasterPromptRunner(
                FakeChat([]),  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=2.5,
            )
            seed.master_prompt_sha256 = hashlib.sha256(master.encode("utf-8")).hexdigest()
            seed.turns = [
                ChatTurn("user", master),
                ChatTurn("assistant", "Đã nhận."),
                ChatTurn(
                    "user",
                    'Nếu đã đọc toàn bộ Master Prompt thì hãy trả lời "ĐÃ NẠP" và không nói gì thêm.',
                ),
                ChatTurn("assistant", "ĐÃ NẠP"),
                ChatTurn("user", "BẮT ĐẦU\n\nThời lượng yêu cầu: 2.5 phút."),
            ]
            seed._save_checkpoint()

            chat = RecoveringChat()
            runner = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=2.5,
                recover_submitted_only=True,
            )
            result = runner.run(master)

        self.assertEqual(chat.recovery_calls, 1)
        self.assertEqual(len(chat.prompts), 2)
        self.assertIn("Đã hoàn tất promt ảnh chưa?", chat.prompts[0])
        self.assertTrue(chat.prompts[1].startswith("Hãy trình bày trực tiếp"))
        self.assertFalse(any(prompt == "TIẾP" for prompt in chat.prompts))
        self.assertEqual(result.artifacts.image_prompts, ("first image", "second image", "third image"))

    def test_trailing_user_turn_is_resent_once_when_bridge_proves_it_was_not_submitted(self) -> None:
        master = "MASTER"
        content = """
[[tieu-de-duoc-chon]]title[[end-tieu-de-duoc-chon]]
[[part-1]]voice[[end-part-1]]
[[mo-ta]]description[[end-mo-ta]]
[[prompt-thumbnail]]thumbnail[[end-prompt-thumbnail]]
[[prompt-hinh-anh]]
Image 1. first image
[[end-prompt-hinh-anh]]
"""

        class NotSubmittedChat(FakeChat):
            def recover_pending(self, **_kwargs) -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            seed = MasterPromptRunner(
                FakeChat([]),  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            )
            seed.master_prompt_sha256 = hashlib.sha256(master.encode("utf-8")).hexdigest()
            pending = "BẮT ĐẦU\n\nThời lượng yêu cầu: 1 phút."
            seed.turns = [
                ChatTurn("user", master),
                ChatTurn("assistant", "Đã nhận."),
                ChatTurn(
                    "user",
                    'Nếu đã đọc toàn bộ Master Prompt thì hãy trả lời "ĐÃ NẠP" và không nói gì thêm.',
                ),
                ChatTurn("assistant", "ĐÃ NẠP"),
                ChatTurn("user", pending),
            ]
            seed._save_checkpoint()
            chat = NotSubmittedChat([content, "ĐÃ XONG", "Đã xuất nội dung tổng hợp."])
            result = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            ).run(master)

        self.assertEqual(chat.prompts[0], pending)
        self.assertEqual(chat.prompts.count(pending), 1)
        self.assertFalse(any(prompt == "TIẾP" for prompt in chat.prompts))
        self.assertEqual(result.artifacts.image_prompts, ("first image",))

    def test_missing_master_attachment_is_repaired_once_without_resending_old_confirmation(self) -> None:
        master = "MASTER"
        content = """
[[tieu-de-duoc-chon]]title[[end-tieu-de-duoc-chon]]
[[part-1]]voice[[end-part-1]]
[[mo-ta]]description[[end-mo-ta]]
[[prompt-thumbnail]]thumbnail[[end-prompt-thumbnail]]
[[prompt-hinh-anh]]
Image 1. first image
[[end-prompt-hinh-anh]]
"""
        missing_file_reply = (
            "Tệp DocuRandom-Master-Prompt.txt hiện không xuất hiện trong các tệp khả dụng "
            "của cuộc trò chuyện; thư mục tệp đính kèm cũng không có bản sao nào để đọc."
        )

        class AttachmentRepairChat(FakeChat):
            def __init__(self) -> None:
                super().__init__(["Đã nhận Master Prompt.", "ĐÃ NẠP", content, "ĐÃ XONG", content])
                self.recovery_calls = 0

            def recover_pending(self, **_kwargs) -> None:
                self.recovery_calls += 1
                return None

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            seed = MasterPromptRunner(
                FakeChat([]),  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            )
            seed.master_prompt_sha256 = hashlib.sha256(master.encode("utf-8")).hexdigest()
            seed.turns = [
                ChatTurn("user", master),
                ChatTurn("assistant", missing_file_reply),
                ChatTurn(
                    "user",
                    'Nếu đã đọc toàn bộ Master Prompt thì hãy trả lời "ĐÃ NẠP" và không nói gì thêm.',
                ),
            ]
            seed._save_checkpoint()

            chat = AttachmentRepairChat()
            result = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            ).run(master)
            checkpoint = json.loads(
                (output / "video_AI_CHECKPOINT.json").read_text(encoding="utf-8")
            )

        confirmation = 'Nếu đã đọc toàn bộ Master Prompt thì hãy trả lời "ĐÃ NẠP" và không nói gì thêm.'
        self.assertEqual(chat.recovery_calls, 1)
        self.assertEqual(chat.prompts[0], master)
        self.assertEqual(chat.prompts.count(confirmation), 1)
        self.assertEqual(chat.prompts.count(master), 1)
        self.assertEqual(checkpoint["master_attachment_repair_attempts"], 1)
        self.assertEqual(result.artifacts.image_prompts, ("first image",))

    def test_orphaned_attachment_repair_retry_is_identified_as_pre_submit_only(self) -> None:
        master = "MASTER"
        missing_file_reply = (
            "Tệp DocuRandom-Master-Prompt.txt hiện không xuất hiện trong các tệp khả dụng "
            "của cuộc trò chuyện; thư mục tệp đính kèm cũng không có bản sao nào để đọc."
        )
        self.assertTrue(
            master_prompt_attachment_retry_pending_in_turns(
                [
                    ChatTurn("user", master),
                    ChatTurn("assistant", missing_file_reply),
                    ChatTurn("user", master),
                ],
                master,
            )
        )
        self.assertFalse(
            master_prompt_attachment_retry_pending_in_turns(
                [
                    ChatTurn("user", master),
                    ChatTurn("assistant", "Đã nhận Master Prompt."),
                    ChatTurn("user", master),
                ],
                master,
            )
        )

    def test_orphaned_attachment_retry_rotates_to_one_fresh_conversation(self) -> None:
        master = "MASTER"
        missing_file_reply = (
            "Tệp DocuRandom-Master-Prompt.txt hiện không xuất hiện trong các tệp khả dụng "
            "của cuộc trò chuyện; thư mục tệp đính kèm cũng không có bản sao nào để đọc."
        )
        content = """
[[tieu-de-duoc-chon]]title[[end-tieu-de-duoc-chon]]
[[part-1]]voice[[end-part-1]]
[[mo-ta]]description[[end-mo-ta]]
[[prompt-thumbnail]]thumbnail[[end-prompt-thumbnail]]
[[prompt-hinh-anh]]
Image 1. first image
[[end-prompt-hinh-anh]]
"""

        class FreshConversationChat(FakeChat):
            def __init__(self) -> None:
                super().__init__(
                    [
                        "Đã nhận Master Prompt.",
                        "ĐÃ NẠP",
                        content,
                        "ĐÃ XONG",
                        content,
                    ]
                )
                self.recovery_calls = 0
                self.thread_ids_at_send: list[str] = []

            def recover_pending(self, **_kwargs) -> str:
                self.recovery_calls += 1
                raise AssertionError("safe conversation migration must not recover the orphaned thread")

            def send(self, prompt: str, **kwargs) -> str:
                self.thread_ids_at_send.append(self.thread_id)
                if not self.thread_id:
                    # A real Codex CLI emits this after the fresh bridge turn
                    # is accepted; simulate only that transport fact here.
                    self.thread_id = "fresh-thread"
                return super().send(prompt, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            seed_chat = FakeChat([])
            seed_chat.thread_id = "orphaned-thread"
            seed = MasterPromptRunner(
                seed_chat,  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            )
            seed.master_prompt_sha256 = hashlib.sha256(master.encode("utf-8")).hexdigest()
            seed.master_attachment_repair_attempts = 1
            seed.turns = [
                ChatTurn("user", master),
                ChatTurn("assistant", missing_file_reply),
                ChatTurn("user", master),
            ]
            seed._save_checkpoint()

            chat = FreshConversationChat()
            result = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            ).run(master)
            checkpoint = json.loads(
                (output / "video_AI_CHECKPOINT.json").read_text(encoding="utf-8")
            )

        self.assertEqual(chat.recovery_calls, 0)
        self.assertEqual(chat.thread_ids_at_send[0], "")
        self.assertEqual(chat.prompts[0], master)
        self.assertEqual(chat.prompts.count(master), 1)
        self.assertEqual(checkpoint["thread_id"], "fresh-thread")
        self.assertEqual(checkpoint["conversation_recovery_attempts"], 1)
        self.assertNotIn("conversation_recovery_pending", checkpoint)
        self.assertEqual(result.artifacts.image_prompts, ("first image",))

    def test_conversation_migration_marker_fails_closed_after_crash(self) -> None:
        master = "MASTER"
        missing_file_reply = (
            "Tệp đính kèm không xuất hiện trong các tệp khả dụng; không thể đọc tệp."
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            seed = MasterPromptRunner(
                FakeChat([]),  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            )
            seed.master_prompt_sha256 = hashlib.sha256(master.encode("utf-8")).hexdigest()
            seed.turns = [
                ChatTurn("user", master),
                ChatTurn("assistant", missing_file_reply),
            ]
            seed.conversation_recovery_pending = True
            seed._save_checkpoint()
            chat = FakeChat(["should never be sent"])
            with self.assertRaises(ContentAISubmissionUncertain):
                MasterPromptRunner(
                    chat,  # type: ignore[arg-type]
                    output_dir=output,
                    project_name="video",
                    minutes=1,
                ).run(master)

        self.assertEqual(chat.prompts, [])

    def test_repaired_missing_master_attachment_does_not_loop_forever(self) -> None:
        master = "MASTER"
        missing_file_reply = (
            "Tệp đính kèm không xuất hiện trong các tệp khả dụng; không thể đọc tệp."
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            seed = MasterPromptRunner(
                FakeChat([]),  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            )
            seed.master_prompt_sha256 = hashlib.sha256(master.encode("utf-8")).hexdigest()
            seed.master_attachment_repair_attempts = 1
            seed.turns = [ChatTurn("user", master), ChatTurn("assistant", missing_file_reply)]
            seed._save_checkpoint()

            chat = FakeChat([])
            with self.assertRaisesRegex(ContentAIError, "sau một lần tự sửa"):
                MasterPromptRunner(
                    chat,  # type: ignore[arg-type]
                    output_dir=output,
                    project_name="video",
                    minutes=1,
                ).run(master)

        self.assertEqual(chat.prompts, [])

    def test_loaded_confirmation_accepts_only_the_requested_phrase(self) -> None:
        self.assertTrue(is_master_prompt_loaded_confirmation("ĐÃ NẠP"))
        self.assertTrue(is_master_prompt_loaded_confirmation('**“Đã nạp”**'))
        self.assertFalse(is_master_prompt_loaded_confirmation("ĐÃ NẠP, tôi đã sẵn sàng"))

    def test_missing_master_attachment_marker_requires_both_file_and_missing_evidence(self) -> None:
        missing_replies = (
            "Tệp DocuRandom-Master-Prompt.txt hiện không xuất hiện trong các tệp khả dụng; "
            "thư mục tệp đính kèm cũng không có bản sao nào để đọc.",
            "Hiện tôi chưa nhận được tệp đính kèm; hệ thống tệp đang báo không có file nào khả dụng.",
            "Tệp không thực sự khả dụng trong cuộc trò chuyện; danh sách tệp đang trống.",
            "Tệp không hiện diện trong vùng làm việc và tìm kiếm trả về 0 tệp.",
            "The attached file did not appear. The conversation has no files available.",
        )
        for reply in missing_replies:
            with self.subTest(reply=reply):
                self.assertTrue(is_master_prompt_attachment_failure(reply))

        self.assertTrue(
            is_master_prompt_attachment_failure(
                "Tôi sẽ đọc toàn bộ tệp đính kèm.\n"
                "Listed files for complete sequential reading\n"
                "Inspected mounted data files\n"
                "Searched for the referenced master prompt file\n"
                "Called tool"
            )
        )
        self.assertFalse(is_master_prompt_attachment_failure("Tôi sẽ tạo một tệp tổng hợp sau."))
        self.assertFalse(
            is_master_prompt_attachment_failure(
                "Tệp đính kèm đã được đọc đầy đủ và nội dung bắt đầu bằng tiêu đề chính."
            )
        )
        self.assertFalse(
            is_master_prompt_attachment_failure(
                "Tệp đã đọc xong, không có lỗi và nội dung đã được xử lý đầy đủ."
            )
        )

    def test_image_phase_and_done_confirmation_are_detected_from_ai_output(self) -> None:
        self.assertTrue(is_image_prompt_done_confirmation('**"Đã xong"**'))
        self.assertTrue(
            is_image_prompt_done_confirmation("ĐÃ HOÀN TẤT TOÀN BỘ WORKFLOW.")
        )
        self.assertFalse(is_image_prompt_done_confirmation("Đã xong phần một"))
        self.assertTrue(is_image_prompt_not_done_confirmation("CHƯA XONG"))
        self.assertTrue(
            image_prompt_phase_started([
                ChatTurn("assistant", "[[prompt-hinh-anh]]\nImage 1. factory"),
            ])
        )
        self.assertFalse(
            image_prompt_phase_started([
                ChatTurn("user", "Ví dụ Image 1. không được tính từ lời người dùng"),
            ])
        )

    def test_paged_prompt_image_blocks_are_merged_and_first_done_stops(self) -> None:
        responses = [
            "Đã nhận Master Prompt.",
            "ĐÃ NẠP",
            """
[[tieu-de-duoc-chon]]title[[end-tieu-de-duoc-chon]]
[[part-1]]voice[[end-part-1]]
[[mo-ta]]description[[end-mo-ta]]
[[prompt-thumbnail]]thumbnail[[end-prompt-thumbnail]]
[[prompt-image-1]]
IMAGE 1. first

IMAGE 2. second
[[end-prompt-image-1]]
""",
            """
[[prompt-hinh-anh]]
IMAGE 3. third
[[end-prompt-hinh-anh]]
            """,
            "ĐÃ XONG",
            """
Tiêu đề
title

Mô tả
description

Prompt Thumbnail
thumbnail

Thoại VOICEVOX liền mạch
A: voice

Prompt hình ảnh
IMAGE1. first

Image 2. second

Ảnh 3. third
""",
        ]
        chat = FakeChat(responses)
        with tempfile.TemporaryDirectory() as temporary:
            runner = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=Path(temporary),
                project_name="video",
                minutes=3,
            )
            result = runner.run("MASTER")

        self.assertEqual(result.artifacts.image_prompts, ("first", "second", "third"))
        self.assertTrue(chat.prompts[-2].startswith("Đã hoàn tất promt ảnh chưa?"))
        self.assertTrue(chat.prompts[-1].startswith("Hãy trình bày trực tiếp"))
        self.assertEqual(chat.prompts.count("TIẾP"), 1)
        self.assertEqual(len(chat.prompts), 6)

    def test_complete_checkpoint_skips_chat_and_continues_pipeline(self) -> None:
        master_prompt = "MASTER giống hệt"
        completed_turns = [
            {"role": "user", "text": master_prompt},
            {
                "role": "assistant",
                "text": """
[[tieu-de-duoc-chon]]title[[end-tieu-de-duoc-chon]]
[[part-1]]voice[[end-part-1]]
[[mo-ta]]description[[end-mo-ta]]
[[prompt-thumbnail]]thumbnail[[end-prompt-thumbnail]]
[[prompt-image-1]]IMAGE 1. first[[end-prompt-image-1]]
""",
            },
            {"role": "assistant", "text": "ĐÃ XONG"},
        ]
        chat = FakeChat([])
        with tempfile.TemporaryDirectory() as temporary:
            runner = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=Path(temporary),
                project_name="video",
                minutes=1,
            )
            runner.checkpoint_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "project_name": "video",
                        "minutes": 1,
                        "thread_id": "saved-thread",
                        "turns": completed_turns,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            statuses: list[tuple[str, str]] = []
            result = runner.run(
                master_prompt,
                on_status=lambda stage, message: statuses.append((stage, message)),
            )

        self.assertEqual(chat.prompts, [])
        self.assertEqual(result.thread_id, "saved-thread")
        self.assertEqual(result.artifacts.image_prompts, ("first",))
        self.assertEqual(statuses[-1][0], "workflow-recovered")

    def test_completed_checkpoint_can_resume_downstream_without_chatgpt(self) -> None:
        master_prompt = "MASTER giống hệt"
        completed_turns = [
            {"role": "user", "text": master_prompt},
            {
                "role": "assistant",
                "text": """
[[tieu-de-duoc-chon]]title[[end-tieu-de-duoc-chon]]
[[part-1]]voice[[end-part-1]]
[[mo-ta]]description[[end-mo-ta]]
[[prompt-thumbnail]]thumbnail[[end-prompt-thumbnail]]
[[prompt-image-1]]IMAGE 1. first[[end-prompt-image-1]]
""",
            },
            {"role": "assistant", "text": "ĐÃ XONG"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            checkpoint = output / "video_AI_CHECKPOINT.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "project_name": "video",
                        "minutes": 1,
                        "thread_id": "saved-thread",
                        "master_prompt_sha256": hashlib.sha256(
                            master_prompt.encode("utf-8")
                        ).hexdigest(),
                        "turns": completed_turns,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = load_completed_content_checkpoint(
                output_dir=output,
                project_name="video",
                minutes=1,
                master_prompt=master_prompt,
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.thread_id, "saved-thread")
            self.assertEqual(result.artifacts.image_prompts, ("first",))
            self.assertTrue(result.package_path.is_file())

            mismatch = load_completed_content_checkpoint(
                output_dir=output,
                project_name="video",
                minutes=1,
                master_prompt="MASTER khác",
            )
            self.assertIsNone(mismatch)

    def test_completed_checkpoint_recovers_voice_from_existing_package(self) -> None:
        master_prompt = "MASTER giống hệt"
        completed_turns = [
            {"role": "user", "text": master_prompt},
            {
                "role": "assistant",
                "text": """
[[tieu-de-duoc-chon]]title[[end-tieu-de-duoc-chon]]
[[mo-ta]]description[[end-mo-ta]]
[[prompt-thumbnail]]thumbnail[[end-prompt-thumbnail]]
[[prompt-image-1]]IMAGE 1. first[[end-prompt-image-1]]
""",
            },
            {"role": "assistant", "text": "ĐÃ XONG"},
        ]
        package = """TIÊU ĐỀ

title

MÔ TẢ

description

PROMPT THUMBNAIL

thumbnail

THOẠI VOICEVOX

A: 一行目です。
A: 二行目です。

PROMPT HÌNH ẢNH

Image 1. first
"""
        self.assertEqual(
            extract_voicevox_script_from_package(package),
            "一行目です。\n二行目です。",
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "video_AI_CHECKPOINT.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "project_name": "video",
                        "minutes": 1,
                        "thread_id": "saved-thread",
                        "master_prompt_sha256": hashlib.sha256(
                            master_prompt.encode("utf-8")
                        ).hexdigest(),
                        "turns": completed_turns,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (output / "video.txt").write_text(package, encoding="utf-8-sig")

            result = load_completed_content_checkpoint(
                output_dir=output,
                project_name="video",
                minutes=1,
                master_prompt=master_prompt,
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(
                result.artifacts.voicevox_script,
                "一行目です。\n二行目です。",
            )

    def test_automates_continue_and_exports_same_stem(self) -> None:
        responses = [
            "Tôi đã đọc nội dung bạn gửi.",
            "ĐÃ NẠP",
            """
[[chu-de-con-duoc-chon]]Chủ đề[[end-chu-de-con-duoc-chon]]
[[tieu-de-duoc-chon]]タイトル[[end-tieu-de-duoc-chon]]
[[part-1]]一行目です。[[end-part-1]]
""",
            "[[mo-ta]]説明です。[[end-mo-ta]]",
            "[[prompt-thumbnail]]PROMPT THUMBNAIL HOÀN CHỈNH: factory[[end-prompt-thumbnail]]",
            """
[[prompt-hinh-anh]]
Image 1. first image

Image 2. second image

Image 3. third image
[[end-prompt-hinh-anh]]
            """,
            "ĐÃ XONG",
            "Đã xuất nội dung tổng hợp.",
        ]
        chat = FakeChat(responses)
        with tempfile.TemporaryDirectory() as temporary:
            runner = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=Path(temporary),
                project_name="Tên video Nhật",
                minutes=2.5,
            )
            progress: list[float] = []
            statuses: list[tuple[str, str]] = []
            result = runner.run(
                "MASTER",
                cancel_event=threading.Event(),
                on_progress=lambda value, _message: progress.append(value),
                on_status=lambda stage, message: statuses.append((stage, message)),
            )
            self.assertEqual(result.package_path.name, "Tên video Nhật.txt")
            self.assertEqual(result.transcript_path.name, "Tên video Nhật_CHAT.txt")
            self.assertEqual(result.checkpoint_path.name, "Tên video Nhật_AI_CHECKPOINT.json")
            self.assertTrue(result.package_path.is_file())
            self.assertEqual(chat.prompts[0], "MASTER")
            self.assertEqual(
                chat.prompts[1],
                'Nếu đã đọc toàn bộ Master Prompt thì hãy trả lời "ĐÃ NẠP" và không nói gì thêm.',
            )
            self.assertEqual(chat.prompts[2].splitlines()[0], "BẮT ĐẦU")
            self.assertNotIn("Tên dự án", chat.prompts[2])
            self.assertEqual(chat.attachments, [[], [], [], [], [], [], [], []])
            self.assertIn("MASTER", result.turns[0].text)
            self.assertEqual(chat.prompts[3:6], ["TIẾP", "TIẾP", "TIẾP"])
            self.assertEqual(
                chat.prompts[6],
                'Đã hoàn tất promt ảnh chưa? Nếu rồi thì chỉ trả lời "ĐÃ XONG" '
                'và không nói gì thêm. Nếu chưa hoàn tất thì chỉ cần trả lời '
                '"CHƯA XONG" và không giải thích hay nói gì thêm.',
            )
            self.assertTrue(chat.prompts[7].startswith("Hãy trình bày trực tiếp"))
            self.assertEqual(progress[-1], 1.0)
            workflow_status = next(
                item for item in statuses if item[0] == "workflow-complete"
            )
            self.assertIn("lượt nội dung 4/48", workflow_status[1])
            self.assertIn("ĐÃ XONG", workflow_status[1])
            exported = result.package_path.read_text(encoding="utf-8-sig")
            self.assertIn("A: 一行目です。", exported)

    def test_cancel_delegates_to_chat(self) -> None:
        chat = FakeChat([])
        with tempfile.TemporaryDirectory() as temporary:
            runner = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=Path(temporary),
                project_name="video",
                minutes=5,
            )
            runner.cancel()
            self.assertTrue(chat.cancelled)

    def test_stops_before_start_when_master_prompt_is_not_confirmed(self) -> None:
        chat = FakeChat(["Tôi đã nhận nội dung.", "Tôi chưa đọc xong."])
        with tempfile.TemporaryDirectory() as temporary:
            runner = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=Path(temporary),
                project_name="video",
                minutes=5,
            )
            with self.assertRaisesRegex(ContentAIError, "chưa trả đúng"):
                runner.run("MASTER")
        self.assertEqual(len(chat.prompts), 2)
        self.assertNotIn("BẮT ĐẦU", chat.prompts)

    def test_image_phase_checks_after_continue_and_retries_when_not_done(self) -> None:
        chat = FakeChat([
            "Đã nhận Master Prompt.",
            "ĐÃ NẠP",
            """
[[tieu-de-duoc-chon]]title[[end-tieu-de-duoc-chon]]
[[part-1]]voice[[end-part-1]]
[[mo-ta]]description[[end-mo-ta]]
[[prompt-thumbnail]]thumbnail[[end-prompt-thumbnail]]
[[prompt-hinh-anh]]
Image 1. first image
""",
            "[[end-prompt-hinh-anh]]",
            "CHƯA XONG",
            "Đã rà soát xong phần ảnh.",
            "ĐÃ XONG",
            "Đã xuất nội dung tổng hợp.",
        ])
        with tempfile.TemporaryDirectory() as temporary:
            runner = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=Path(temporary),
                project_name="video",
                minutes=1,
            )
            result = runner.run("MASTER")

        question = (
            'Đã hoàn tất promt ảnh chưa? Nếu rồi thì chỉ trả lời "ĐÃ XONG" '
            'và không nói gì thêm. Nếu chưa hoàn tất thì chỉ cần trả lời '
            '"CHƯA XONG" và không giải thích hay nói gì thêm.'
        )
        self.assertEqual(
            chat.prompts[:2],
            [
                "MASTER",
                'Nếu đã đọc toàn bộ Master Prompt thì hãy trả lời "ĐÃ NẠP" và không nói gì thêm.',
            ],
        )
        self.assertTrue(chat.prompts[2].startswith("BẮT ĐẦU"))
        self.assertEqual(chat.prompts[3:7], ["TIẾP", question, "TIẾP", question])
        self.assertTrue(chat.prompts[7].startswith("Hãy trình bày trực tiếp"))
        self.assertEqual(result.artifacts.image_prompts, ("first image",))

    def test_done_confirmation_never_sends_continue_again(self) -> None:
        final_export = """
TIÊU ĐỀ

日本の製造業

MÔ TẢ

Mô tả đầy đủ.

SEO tags

日本製造業, 工作機械, 海外の反応

PROMPT THUMBNAIL

Nhà máy Nhật Bản hiện đại.

THOẠI VOICEVOX

A: 日本の製造業を見てみましょう。

PROMPT HÌNH ẢNH

Image 1. A modern Japanese factory documentary scene.
"""
        chat = FakeChat(
            [
                "Đã nhận Master Prompt.",
                "ĐÃ NẠP",
                """
[[tieu-de-duoc-chon]]title[[end-tieu-de-duoc-chon]]
[[part-1]]voice[[end-part-1]]
[[prompt-hinh-anh]]
Image 1. first image
[[end-prompt-hinh-anh]]
""",
                "Đã viết xong danh sách ảnh.",
                "Đã xong",
                final_export,
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            runner = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=Path(temporary),
                project_name="video",
                minutes=1,
            )
            result = runner.run("MASTER")

        question = (
            'Đã hoàn tất promt ảnh chưa? Nếu rồi thì chỉ trả lời "ĐÃ XONG" '
            'và không nói gì thêm. Nếu chưa hoàn tất thì chỉ cần trả lời '
            '"CHƯA XONG" và không giải thích hay nói gì thêm.'
        )
        self.assertEqual(chat.prompts[3:5], ["TIẾP", question])
        self.assertTrue(chat.prompts[5].startswith("Hãy trình bày trực tiếp"))
        self.assertEqual(chat.prompts.count("TIẾP"), 1)
        self.assertEqual(result.artifacts.title, "日本の製造業")
        self.assertEqual(
            result.artifacts.seo_tags,
            ("日本製造業", "工作機械", "海外の反応"),
        )
        self.assertIn(
            "MÔ TẢ\n\nMô tả đầy đủ.\n\nSEO TAGS\n\n日本製造業, 工作機械, 海外の反応",
            format_content_package(result.artifacts),
        )

    def test_resumed_checkpoint_after_done_goes_directly_to_final_export(self) -> None:
        master = "MASTER"
        question = (
            'Đã hoàn tất promt ảnh chưa? Nếu rồi thì chỉ trả lời "ĐÃ XONG" '
            'và không nói gì thêm. Nếu chưa hoàn tất thì chỉ cần trả lời '
            '"CHƯA XONG" và không giải thích hay nói gì thêm.'
        )
        final_export = """
TIÊU ĐỀ

title

MÔ TẢ

description

PROMPT THUMBNAIL

thumbnail

THOẠI VOICEVOX

A: voice

PROMPT HÌNH ẢNH

Image 1. first image
"""
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            seed_chat = FakeChat([])
            seed = MasterPromptRunner(
                seed_chat,  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            )
            seed.master_prompt_sha256 = hashlib.sha256(master.encode("utf-8")).hexdigest()
            seed.turns = [
                ChatTurn("user", master),
                ChatTurn("assistant", "Đã nhận."),
                ChatTurn(
                    "user",
                    'Nếu đã đọc toàn bộ Master Prompt thì hãy trả lời "ĐÃ NẠP" và không nói gì thêm.',
                ),
                ChatTurn("assistant", "ĐÃ NẠP"),
                ChatTurn("user", "BẮT ĐẦU\n\nThời lượng yêu cầu: 1 phút."),
                ChatTurn(
                    "assistant",
                    "[[prompt-hinh-anh]]\nImage 1. first image\n[[end-prompt-hinh-anh]]",
                ),
                ChatTurn("user", question),
                ChatTurn("assistant", "ĐÃ XONG"),
            ]
            seed._save_checkpoint()

            resumed_chat = FakeChat([final_export])
            resumed = MasterPromptRunner(
                resumed_chat,  # type: ignore[arg-type]
                output_dir=output,
                project_name="video",
                minutes=1,
            )
            result = resumed.run(master)

        self.assertEqual(len(resumed_chat.prompts), 1)
        self.assertTrue(resumed_chat.prompts[0].startswith("Hãy trình bày trực tiếp"))
        self.assertEqual(result.artifacts.image_prompts, ("first image",))

    def test_stops_at_hard_turn_limit_without_sending_one_more_continue(self) -> None:
        chat = FakeChat([
            "Đã nhận nội dung.",
            "ĐÃ NẠP",
            "chưa đủ 1",
            "chưa đủ 2",
            "chưa đủ 3",
            "chưa đủ 4",
            "vẫn thiếu sau sửa 1",
            "vẫn thiếu sau sửa 2",
            "vẫn thiếu sau sửa 3",
        ])
        statuses: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as temporary:
            runner = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=Path(temporary),
                project_name="video",
                minutes=5,
                max_turns=4,
            )
            with self.assertRaisesRegex(ContentAIError, "hết số lần khôi phục"):
                runner.run(
                    "MASTER",
                    on_status=lambda stage, message: statuses.append((stage, message)),
                )
        self.assertEqual(len(chat.prompts), 9)
        self.assertEqual(chat.prompts.count("TIẾP"), 3)
        self.assertTrue(all(p.startswith("KHÔI PHỤC PHẦN NỘI DUNG CÒN THIẾU")
                            for p in chat.prompts[-3:]))
        self.assertEqual(sum(stage == "content-repair" for stage, _ in statuses), 3)

    def test_keeps_extra_image_prompts_instead_of_stopping_workflow(self) -> None:
        chat = FakeChat([
            "Đã nhận nội dung.",
            "ĐÃ NẠP",
            """
[[tieu-de-duoc-chon]]title[[end-tieu-de-duoc-chon]]
[[part-1]]voice[[end-part-1]]
[[mo-ta]]description[[end-mo-ta]]
[[prompt-thumbnail]]thumbnail[[end-prompt-thumbnail]]
[[prompt-hinh-anh]]
Image 1. first

Image 2. extra
[[end-prompt-hinh-anh]]
""",
            "ĐÃ XONG",
            "Đã xuất nội dung tổng hợp.",
        ])
        with tempfile.TemporaryDirectory() as temporary:
            runner = MasterPromptRunner(
                chat,  # type: ignore[arg-type]
                output_dir=Path(temporary),
                project_name="video",
                minutes=1,
            )
            result = runner.run("MASTER")
        self.assertEqual(result.artifacts.image_prompts, ("first", "extra"))
        self.assertEqual(len(chat.prompts), 5)


class CodexPrecheckTests(unittest.TestCase):
    def test_clean_machine_prefers_codex_cli_bundled_with_the_app(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomBundledCodex_") as temp:
            bundle = Path(temp)
            executable = bundle / "codex-cli" / "codex.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"official bundled cli")
            with (
                mock.patch("docurandom.content_ai.sys._MEIPASS", str(bundle), create=True),
                mock.patch.dict("os.environ", {}, clear=True),
            ):
                self.assertEqual(discover_codex_cli(), executable.resolve())

    def test_one_click_installer_uses_official_npm_package_in_app_data(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomCodexInstall_") as temp:
            root = Path(temp)
            installed = root / "codex.exe"
            with (
                mock.patch(
                    "docurandom.content_ai.discover_codex_cli",
                    side_effect=[None, installed],
                ),
                mock.patch(
                    "docurandom.content_ai._discover_npm",
                    return_value=Path("C:/Node/npm.cmd"),
                ),
                mock.patch("docurandom.content_ai.app_data_dir", return_value=root),
                mock.patch("docurandom.content_ai._run_installer") as run_installer,
            ):
                result = install_codex_cli()

            self.assertEqual(result, installed)
            command = run_installer.call_args.args[0]
            self.assertIn("@openai/codex@latest", command)
            self.assertIn(str(root / "codex-cli"), command)

    def test_codex_cli_probe_does_not_read_openai_login(self) -> None:
        fake_path = Path("C:/fake/codex.exe")
        with mock.patch(
            "docurandom.content_ai._run_cli_probe",
            return_value=(0, "codex-cli 1.2.3"),
        ) as probe:
            status = check_codex_cli(fake_path)
        self.assertEqual(
            status,
            CodexCliStatus(
                path=fake_path,
                version="codex-cli 1.2.3",
                logged_in=False,
                login_method="Không yêu cầu",
                detail=(
                    "codex-cli 1.2.3 sẵn sàng làm cầu nối local; "
                    "không đọc đăng nhập hay token Codex."
                ),
            ),
        )
        self.assertTrue(status.ready)
        probe.assert_called_once_with(fake_path, ["--version"], 12.0)

    def test_cli_transport_readiness_depends_on_executable_and_version_only(self) -> None:
        status = CodexCliStatus(
            path=Path("C:/fake/codex.exe"),
            version="codex-cli 1.2.3",
            logged_in=False,
            login_method="Không yêu cầu",
            detail="local transport",
        )
        self.assertTrue(status.ready)

    def test_cli_command_is_read_only_and_resumes_exact_thread(self) -> None:
        chat = CodexCliChat(
            Path("C:/fake/codex.exe"),
            Path("C:/work"),
            access_token="docurandom_bridge_token_0123456789_abcdefghijklmnop",
            endpoint="http://127.0.0.1:17841",
            codex_home=Path("C:/DocuRandom/codex-home"),
        )
        initial = chat._command(Path("C:/temp/out.txt"))
        self.assertIn("read-only", initial)
        self.assertIn("--profile", initial)
        self.assertIn("docurandom-chatgpt-web", initial)
        self.assertIn("--model", initial)
        self.assertNotIn("--ignore-user-config", initial)
        self.assertNotIn("danger-full-access", initial)
        self.assertEqual(chat.codex_home, Path("C:/DocuRandom/codex-home"))

        chat.thread_id = "thread-123"
        resumed = chat._command(Path("C:/temp/out.txt"))
        self.assertIn("resume", resumed)
        self.assertIn("thread-123", resumed)
        self.assertNotIn("--sandbox", resumed)

    def test_cli_recovers_interrupted_dom_before_any_resume_prompt(self) -> None:
        chat = CodexCliChat(
            Path("C:/fake/codex.exe"),
            Path("C:/work"),
            access_token="docurandom_bridge_token_0123456789_abcdefghijklmnop",
            endpoint="http://127.0.0.1:17841",
            codex_home=Path("C:/DocuRandom/codex-home"),
        )
        chat.thread_id = "thread-123"
        requests: list[tuple[str, str, bytes, dict[str, str]]] = []
        connection_timeouts: list[float | None] = []

        class _Response:
            status = 200

            def read(self, _limit: int) -> bytes:
                return json.dumps(
                    {"status": "recovered", "output_text": "Câu trả lời đã thu hồi."},
                    ensure_ascii=False,
                ).encode("utf-8")

        class _Connection:
            def __init__(self, host: str, port: int, timeout: float | None) -> None:
                self.host = host
                self.port = port
                self.timeout = timeout
                connection_timeouts.append(timeout)

            def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
                requests.append((method, path, body, headers))

            def getresponse(self) -> _Response:
                return _Response()

            def close(self) -> None:
                return None

        with (
            mock.patch(
                "docurandom.content_ai.http.client.HTTPConnection",
                side_effect=_Connection,
            ),
            mock.patch.object(chat, "_poll_bridge_activity"),
        ):
            recovered = chat.recover_pending()

        self.assertEqual(recovered, "Câu trả lời đã thu hồi.")
        self.assertEqual(requests[0][0:2], ("POST", "/v1/turns/recover"))
        self.assertEqual(
            json.loads(requests[0][2].decode("utf-8")),
            {"thread_id": "thread-123"},
        )
        self.assertEqual(connection_timeouts, [None])
        self.assertTrue(requests[0][3]["Authorization"].startswith("Bearer "))

    def test_custom_codex_profile_is_isolated_and_uses_rotating_bridge_token(self) -> None:
        token = "docurandom_bridge_token_0123456789_abcdefghijklmnop"
        with tempfile.TemporaryDirectory(prefix="DocuRandomCodexProfile_") as temp:
            root = Path(temp)
            base = root / "config.toml"
            base_content = (
                'model = "user-model"\n'
                '\n[mcp_servers.donut-browser]\n'
                'type = "http"\n'
                'url = "http://127.0.0.1:51080/mcp/test"\n'
                '\n[mcp_servers.node_repl]\n'
                'command = "node_repl.exe"\n'
            )
            base.write_text(base_content, encoding="utf-8")
            profile = ensure_chatgpt_web_codex_profile(
                "http://127.0.0.1:17841",
                token,
                codex_home=root,
            )
            content = profile.read_text(encoding="utf-8")

            self.assertEqual(profile.name, "docurandom-chatgpt-web.config.toml")
            self.assertEqual(base.read_text(encoding="utf-8"), base_content)
            self.assertIn('model = "chatgpt-web/auto"', content)
            self.assertIn('model_reasoning_effort = "none"', content)
            self.assertIn('wire_api = "responses"', content)
            self.assertIn('supports_websockets = false', content)
            self.assertIn('env_key = "DOCURANDOM_CHAT_BRIDGE_TOKEN"', content)
            self.assertIn('[features]\napps = false\nplugins = false', content)
            self.assertIn('[mcp_servers."donut-browser"]\nenabled = false', content)
            self.assertIn('[mcp_servers."node_repl"]\nenabled = false', content)
            self.assertNotIn(token, content)


class ChatGPTWebBridgeTests(unittest.TestCase):
    ACCESS_TOKEN = "docurandom_bridge_token_0123456789_abcdefghijklmnop"

    def test_repair_request_is_authenticated_and_uses_local_endpoint(self) -> None:
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return b'{"status":"repaired","ready":true,"authenticated":true}'

        descriptor = {
            "version": 1,
            "kind": "docurandom-chat-bridge",
            "pid": 456,
            "control": {
                "endpoint": "http://127.0.0.1:17842",
                "token": self.ACCESS_TOKEN,
            },
        }
        with (
            mock.patch("docurandom.content_ai._read_json_object", return_value=descriptor),
            mock.patch("docurandom.content_ai._pid_is_running", return_value=True),
            mock.patch(
                "docurandom.content_ai.urllib.request.urlopen",
                return_value=_Response(),
            ) as urlopen,
        ):
            repaired = _request_chatgpt_web_bridge_repair(timeout=10.0)

        self.assertTrue(repaired and repaired["ready"])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:17842/v1/session/repair")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {self.ACCESS_TOKEN}")

    def test_repair_uses_live_route_without_stopping_or_clearing_login(self) -> None:
        ready = ChatGPTWebStatus(
            launcher_path=Path("C:/portable/chat-bridge/DocuRandom Chat Bridge.exe"),
            installed=True,
            launcher_running=True,
            setup_complete=True,
            authenticated=True,
            runtime_ready=True,
            endpoint="http://127.0.0.1:17841",
            bridge_version=CHATGPT_WEB_BRIDGE_VERSION,
            available_models=("chatgpt-web/high",),
            access_token=self.ACCESS_TOKEN,
        )
        with (
            mock.patch("docurandom.content_ai.install_chatgpt_web_launcher"),
            mock.patch(
                "docurandom.content_ai._chatgpt_web_session_status",
                return_value=(True, True, self.ACCESS_TOKEN, False),
            ),
            mock.patch(
                "docurandom.content_ai._request_chatgpt_web_bridge_repair",
                return_value={"status": "repaired", "ready": True},
            ),
            mock.patch(
                "docurandom.content_ai.check_chatgpt_web_bridge",
                return_value=ready,
            ),
            mock.patch("docurandom.content_ai.stop_all_chatgpt_web_bridges") as stop,
            mock.patch("docurandom.content_ai._clear_chatgpt_web_transient_cache") as clear,
        ):
            result = repair_chatgpt_web_bridge(show=True)

        self.assertIs(result, ready)
        stop.assert_not_called()
        clear.assert_not_called()

    def test_repair_never_force_stops_an_active_old_bridge(self) -> None:
        with (
            mock.patch("docurandom.content_ai.install_chatgpt_web_launcher"),
            mock.patch(
                "docurandom.content_ai._chatgpt_web_session_status",
                return_value=(True, True, self.ACCESS_TOKEN, False),
            ),
            mock.patch(
                "docurandom.content_ai._request_chatgpt_web_bridge_repair",
                return_value=None,
            ),
            mock.patch(
                "docurandom.content_ai._chatgpt_web_has_active_turns",
                return_value=True,
            ),
            mock.patch("docurandom.content_ai.stop_all_chatgpt_web_bridges") as stop,
        ):
            with self.assertRaisesRegex(ContentAIError, "đang làm nội dung"):
                repair_chatgpt_web_bridge(show=True)
        stop.assert_not_called()

    def test_active_turn_probe_uses_health_count_instead_of_capacity_flag(self) -> None:
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return b'{"active_turns":1,"accepting_turns":true}'

        descriptor = {
            "control": {"endpoint": "http://127.0.0.1:17842"},
        }
        with (
            mock.patch("docurandom.content_ai._read_json_object", return_value=descriptor),
            mock.patch(
                "docurandom.content_ai.urllib.request.urlopen",
                return_value=_Response(),
            ),
        ):
            self.assertTrue(_chatgpt_web_has_active_turns())

    def test_update_requests_authenticated_graceful_bridge_shutdown(self) -> None:
        token = self.ACCESS_TOKEN

        class _Response:
            status = 202

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return b'{"status":"shutting-down"}'

        descriptor = {
            "version": 1,
            "kind": "docurandom-chat-bridge",
            "pid": 456,
            "control": {
                "endpoint": "http://127.0.0.1:17842",
                "token": token,
            },
        }
        with (
            mock.patch("docurandom.content_ai._read_json_object", return_value=descriptor),
            mock.patch(
                "docurandom.content_ai._pid_is_running",
                side_effect=[True, False],
            ),
            mock.patch(
                "docurandom.content_ai.urllib.request.urlopen",
                return_value=_Response(),
            ) as urlopen,
        ):
            stopped = _request_chatgpt_web_bridge_shutdown()

        self.assertTrue(stopped)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:17842/v1/session/shutdown")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {token}")

    def test_update_prefers_graceful_shutdown_before_packaged_fallback(self) -> None:
        with (
            mock.patch(
                "docurandom.content_ai._chatgpt_web_session_status",
                return_value=(True, True, self.ACCESS_TOKEN, False),
            ),
            mock.patch(
                "docurandom.content_ai._request_chatgpt_web_bridge_shutdown",
                return_value=True,
            ) as graceful,
            mock.patch(
                "docurandom.content_ai._stop_outdated_chatgpt_web_bridge"
            ) as fallback,
        ):
            self.assertTrue(stop_chatgpt_web_bridge_for_update())

        graceful.assert_called_once_with()
        fallback.assert_not_called()

    def test_full_app_shutdown_closes_current_and_legacy_bridges(self) -> None:
        roots = (Path("C:/managed/chat-bridge"), Path("C:/legacy/chat-bridge"))
        with (
            mock.patch(
                "docurandom.content_ai._known_chatgpt_web_roots",
                return_value=roots,
            ),
            mock.patch(
                "docurandom.content_ai._chatgpt_web_session_status_at",
                side_effect=[
                    (True, True, self.ACCESS_TOKEN, False),
                    (True, True, self.ACCESS_TOKEN, True),
                ],
            ),
            mock.patch(
                "docurandom.content_ai._request_chatgpt_web_bridge_shutdown_at",
                return_value=True,
            ) as graceful,
            mock.patch(
                "docurandom.content_ai._stop_chatgpt_web_bridge_at",
                return_value=True,
            ) as forced,
        ):
            stopped = stop_all_chatgpt_web_bridges(force=True)

        self.assertTrue(stopped)
        graceful.assert_called_once_with(roots[0], 5.0)
        forced.assert_called_once_with(roots[1])

    def test_status_requires_real_session_runtime_and_models(self) -> None:
        status = ChatGPTWebStatus(
            launcher_path=Path("C:/Bridge/Codex Web GPT.exe"),
            installed=True,
            launcher_running=True,
            setup_complete=True,
            authenticated=True,
            runtime_ready=True,
            endpoint="http://127.0.0.1:17841",
            bridge_version="2.1.9",
            available_models=("chatgpt-web/high",),
            access_token=self.ACCESS_TOKEN,
        )
        self.assertTrue(status.ready)
        self.assertEqual(status.login_method, "ChatGPT Web")
        self.assertTrue(status.logged_in)
        self.assertEqual(status.path, status.launcher_path)

        busy = dataclasses.replace(status, session_busy=True)
        self.assertTrue(busy.operational)
        self.assertFalse(busy.ready)

        self.assertFalse(
            ChatGPTWebStatus(
                launcher_path=status.launcher_path,
                installed=True,
                launcher_running=True,
                setup_complete=True,
                authenticated=False,
                runtime_ready=True,
                endpoint=status.endpoint,
                available_models=status.available_models,
            ).ready
        )

    def test_full_parallel_capacity_remains_a_healthy_runtime(self) -> None:
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return json.dumps(
                    {
                        "service": "docurandom-chat-bridge",
                        "status": "ok",
                        "version": CHATGPT_WEB_BRIDGE_VERSION,
                        "accepting_turns": False,
                        "active_turns": 4,
                        "max_parallel_turns": 4,
                    }
                ).encode("utf-8")

        with (
            mock.patch(
                "docurandom.content_ai._read_json_object",
                return_value={
                    "host": "127.0.0.1",
                    "port": 17841,
                    "releaseVersion": CHATGPT_WEB_BRIDGE_VERSION,
                    "availableModels": ["chatgpt-web/high"],
                },
            ),
            mock.patch(
                "docurandom.content_ai.urllib.request.urlopen",
                return_value=_Response(),
            ),
        ):
            ready, endpoint, version, models = _chatgpt_web_runtime_status(2.0)

        self.assertTrue(ready)
        self.assertEqual(endpoint, "http://127.0.0.1:17841")
        self.assertEqual(version, CHATGPT_WEB_BRIDGE_VERSION)
        self.assertEqual(models, ("chatgpt-web/high",))

    def test_default_keeps_web_selection_even_when_higher_routes_exist(self) -> None:
        models = ("chatgpt-web/auto", "chatgpt-web/high", "chatgpt-web/extra-high")
        self.assertEqual(
            resolve_chatgpt_web_model(CHATGPT_WEB_DEFAULT_MODEL_LABEL, models),
            "chatgpt-web/auto",
        )
        with self.assertRaisesRegex(ContentAIError, "không cung cấp"):
            resolve_chatgpt_web_model("chatgpt-web/pro", models)

    def test_explicit_high_is_separate_from_default_follow_web(self) -> None:
        models = ("chatgpt-web/auto", "chatgpt-web/high")
        self.assertEqual(
            resolve_chatgpt_web_model(CHATGPT_WEB_DEFAULT_MODEL_LABEL, models),
            "chatgpt-web/auto",
        )

        # The fixed High route remains authoritative even if an old bridge also
        # advertises the legacy account-default route.
        self.assertEqual(
            resolve_chatgpt_web_model("chatgpt-web/high", models),
            "chatgpt-web/high",
        )

    def test_passive_session_status_never_requests_session_inspection(self) -> None:
        token = self.ACCESS_TOKEN
        requests = []

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return json.dumps(
                    {"authenticated": True, "busy": False, "status": "ready"}
                ).encode("utf-8")

        def open_request(request, *, timeout):
            requests.append((request, timeout))
            return _Response()

        descriptor = {
            "version": 1,
            "kind": "docurandom-chat-bridge",
            "pid": 1234,
            "control": {
                "endpoint": "http://127.0.0.1:57752",
                "token": token,
            },
        }
        with (
            mock.patch(
                "docurandom.content_ai._read_json_object",
                return_value=descriptor,
            ),
            mock.patch("docurandom.content_ai._pid_is_running", return_value=True),
            mock.patch(
                "docurandom.content_ai.urllib.request.urlopen",
                side_effect=open_request,
            ),
        ):
            result = _chatgpt_web_session_status(2.0)

        self.assertEqual(result, (True, True, token, False))
        request, timeout = requests[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.endswith("/v1/session/status"))
        self.assertNotIn("inspect", request.full_url)
        self.assertEqual(timeout, 2.0)

    def test_busy_strong_inspection_falls_back_without_expiring_session(self) -> None:
        descriptor = {
            "version": 1,
            "kind": "docurandom-chat-bridge",
            "pid": 1234,
            "control": {
                "endpoint": "http://127.0.0.1:57752",
                "token": self.ACCESS_TOKEN,
            },
        }
        passive = (True, True, self.ACCESS_TOKEN, True)
        with (
            mock.patch(
                "docurandom.content_ai._read_json_object",
                return_value=descriptor,
            ),
            mock.patch("docurandom.content_ai._pid_is_running", return_value=True),
            mock.patch(
                "docurandom.content_ai.urllib.request.urlopen",
                side_effect=OSError("browser busy"),
            ),
            mock.patch(
                "docurandom.content_ai._chatgpt_web_session_status",
                return_value=passive,
            ) as status,
        ):
            result = _inspect_chatgpt_web_session(8.0)

        self.assertEqual(result, passive)
        status.assert_called_once_with(3.0)

    def test_strong_inspection_requires_normal_saved_chat_mode(self) -> None:
        descriptor = {
            "version": 1,
            "kind": "docurandom-chat-bridge",
            "pid": 1234,
            "control": {
                "endpoint": "http://127.0.0.1:57752",
                "token": self.ACCESS_TOKEN,
            },
        }

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return json.dumps(
                    {
                        "authenticated": True,
                        "saved": True,
                        "temporary": False,
                        "busy": False,
                    }
                ).encode("utf-8")

        with (
            mock.patch(
                "docurandom.content_ai._read_json_object",
                return_value=descriptor,
            ),
            mock.patch("docurandom.content_ai._pid_is_running", return_value=True),
            mock.patch(
                "docurandom.content_ai.urllib.request.urlopen",
                return_value=_Response(),
            ),
        ):
            result = _inspect_chatgpt_web_session(8.0)

        self.assertEqual(result, (True, True, self.ACCESS_TOKEN, False))

    def test_check_auto_replaces_idle_outdated_bridge(self) -> None:
        launcher = Path("C:/portable/chat-bridge/DocuRandom Chat Bridge.exe")
        current_session = (True, True, self.ACCESS_TOKEN, False)
        with (
            mock.patch(
                "docurandom.content_ai.discover_chatgpt_web_launcher",
                return_value=launcher,
            ),
            mock.patch(
                "docurandom.content_ai._read_json_object",
                return_value={"coreSetupComplete": True},
            ),
            mock.patch(
                "docurandom.content_ai._inspect_chatgpt_web_session",
                return_value=current_session,
            ),
            mock.patch(
                "docurandom.content_ai._chatgpt_web_session_status",
                return_value=current_session,
            ),
            mock.patch(
                "docurandom.content_ai._chatgpt_web_runtime_status",
                side_effect=[
                    (
                        True,
                        "http://127.0.0.1:17842",
                        "1.0.5",
                        ("chatgpt-web/auto",),
                    ),
                    (
                        True,
                        "http://127.0.0.1:17841",
                        CHATGPT_WEB_BRIDGE_VERSION,
                        ("chatgpt-web/auto",),
                    ),
                ],
            ),
            mock.patch(
                "docurandom.content_ai._stop_outdated_chatgpt_web_bridge",
                return_value=True,
            ) as stop_bridge,
            mock.patch(
                "docurandom.content_ai.start_chatgpt_web_launcher"
            ) as start_bridge,
            mock.patch("docurandom.content_ai.time.sleep"),
        ):
            status = check_chatgpt_web_bridge(auto_start=True)

        self.assertTrue(status.ready)
        self.assertEqual(status.bridge_version, CHATGPT_WEB_BRIDGE_VERSION)
        stop_bridge.assert_called_once_with()
        start_bridge.assert_called_once_with(show=False)

    def test_strict_check_accepts_live_saved_session_when_dom_inspection_lags(self) -> None:
        launcher = Path("C:/portable/chat-bridge/DocuRandom Chat Bridge.exe")
        state = {
            "coreSetupComplete": True,
            "authenticated": True,
            "chatMode": "saved",
        }
        with (
            mock.patch(
                "docurandom.content_ai.discover_chatgpt_web_launcher",
                return_value=launcher,
            ),
            mock.patch(
                "docurandom.content_ai._read_json_object",
                return_value=state,
            ),
            mock.patch(
                "docurandom.content_ai._inspect_chatgpt_web_session",
                return_value=(True, False, "", False),
            ),
            mock.patch(
                "docurandom.content_ai._chatgpt_web_session_status",
                return_value=(True, True, self.ACCESS_TOKEN, False),
            ) as passive,
            mock.patch(
                "docurandom.content_ai._chatgpt_web_runtime_status",
                return_value=(
                    True,
                    "http://127.0.0.1:17842",
                    CHATGPT_WEB_BRIDGE_VERSION,
                    ("chatgpt-web/high",),
                ),
            ),
        ):
            status = check_chatgpt_web_bridge(auto_start=False)

        self.assertTrue(status.ready)
        self.assertTrue(status.authenticated)
        self.assertIn("tránh báo chưa đăng nhập giả", status.detail)
        passive.assert_called_once()

    def test_strict_check_never_falls_back_for_temporary_chat_mode(self) -> None:
        launcher = Path("C:/portable/chat-bridge/DocuRandom Chat Bridge.exe")
        state = {
            "coreSetupComplete": True,
            "authenticated": True,
            "chatMode": "temporary",
        }
        with (
            mock.patch(
                "docurandom.content_ai.discover_chatgpt_web_launcher",
                return_value=launcher,
            ),
            mock.patch(
                "docurandom.content_ai._read_json_object",
                return_value=state,
            ),
            mock.patch(
                "docurandom.content_ai._inspect_chatgpt_web_session",
                return_value=(True, False, "", False),
            ),
            mock.patch(
                "docurandom.content_ai._chatgpt_web_session_status"
            ) as passive,
            mock.patch(
                "docurandom.content_ai._chatgpt_web_runtime_status",
                return_value=(
                    True,
                    "http://127.0.0.1:17842",
                    CHATGPT_WEB_BRIDGE_VERSION,
                    ("chatgpt-web/high",),
                ),
            ),
        ):
            status = check_chatgpt_web_bridge(auto_start=False)

        self.assertFalse(status.ready)
        self.assertFalse(status.authenticated)
        passive.assert_not_called()

    def test_busy_outdated_bridge_is_not_replaced_mid_turn(self) -> None:
        launcher = Path("C:/portable/chat-bridge/DocuRandom Chat Bridge.exe")
        with (
            mock.patch(
                "docurandom.content_ai.discover_chatgpt_web_launcher",
                return_value=launcher,
            ),
            mock.patch(
                "docurandom.content_ai._read_json_object",
                return_value={"coreSetupComplete": True},
            ),
            mock.patch(
                "docurandom.content_ai._inspect_chatgpt_web_session",
                return_value=(True, True, self.ACCESS_TOKEN, True),
            ),
            mock.patch(
                "docurandom.content_ai._chatgpt_web_runtime_status",
                return_value=(
                    True,
                    "http://127.0.0.1:17842",
                    "1.0.5",
                    ("chatgpt-web/auto",),
                ),
            ),
            mock.patch(
                "docurandom.content_ai._stop_outdated_chatgpt_web_bridge"
            ) as stop_bridge,
        ):
            status = check_chatgpt_web_bridge(auto_start=True)

        self.assertFalse(status.ready)
        self.assertTrue(status.session_busy)
        stop_bridge.assert_not_called()

    def test_direct_bridge_chat_uses_loopback_and_continues_response(self) -> None:
        response_payloads = [
            {
                "id": "resp_first",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "\n  Phản hồi một \n\n"}
                        ],
                    }
                ],
            },
            {
                "id": "resp_second",
                "status": "completed",
                "output_text": "\nPhản hồi hai  \n",
            },
        ]
        connections = []

        class _Response:
            status = 200

            def __init__(self, value: dict[str, object]) -> None:
                self.raw = json.dumps(value, ensure_ascii=False).encode("utf-8")

            def getheader(self, name: str, default: str = "") -> str:
                return str(len(self.raw)) if name.casefold() == "content-length" else default

            def read(self, _limit: int) -> bytes:
                return self.raw

        class _Connection:
            def __init__(self, host: str, port: int, timeout: float) -> None:
                self.host = host
                self.port = port
                self.timeout = timeout
                self.requests = []
                self.closed = False
                self.value = response_payloads[len(connections)]
                connections.append(self)

            def request(self, method, path, body=None, headers=None) -> None:
                self.requests.append((method, path, body, headers))

            def getresponse(self):
                return _Response(self.value)

            def close(self) -> None:
                self.closed = True

        with (
            tempfile.TemporaryDirectory(prefix="DocuRandomWebChat_") as temp,
            mock.patch(
                "docurandom.content_ai.http.client.HTTPConnection",
                _Connection,
            ),
        ):
            chat = ChatGPTWebChat(
                "http://127.0.0.1:17841",
                "chatgpt-web/high",
                Path(temp),
                self.ACCESS_TOKEN,
            )
            self.assertEqual(
                chat.send("Xin chào", expected_exact="ĐÃ TẠO"),
                "\n  Phản hồi một \n\n",
            )
            self.assertEqual(chat.send("TIẾP"), "\nPhản hồi hai  \n")

        self.assertEqual(len(connections), 2)
        for connection in connections:
            self.assertEqual(connection.host, "127.0.0.1")
            self.assertEqual(connection.port, 17841)
            self.assertTrue(connection.closed)
            headers = connection.requests[0][3]
            self.assertEqual(
                headers["Authorization"],
                f"Bearer {self.ACCESS_TOKEN}",
            )
        second_body = json.loads(connections[1].requests[0][2].decode("utf-8"))
        first_body = json.loads(connections[0].requests[0][2].decode("utf-8"))
        self.assertEqual(first_body["docurandom_expected_exact"], "ĐÃ TẠO")
        self.assertEqual(second_body["previous_response_id"], "resp_first")
        self.assertTrue(str(second_body["model"]).startswith("chatgpt-web/"))

    def test_direct_bridge_chat_streams_partial_output_snapshots(self) -> None:
        events = [
            {"type": "response.output_text.delta", "delta": "Xin"},
            {"type": "response.output_text.delta", "delta": " chào"},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_streamed",
                    "output_text": "Xin chào",
                },
            },
        ]

        class _StreamingResponse:
            status = 200

            def __init__(self) -> None:
                self.lines = [
                    (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
                    for event in events
                ]

            def getheader(self, _name: str, default: str = "") -> str:
                return default

            def readline(self, _limit: int) -> bytes:
                return self.lines.pop(0) if self.lines else b""

        class _StreamingConnection:
            def __init__(self, host: str, port: int, timeout: float) -> None:
                self.host = host
                self.port = port
                self.timeout = timeout
                self.request_body = b""

            def request(self, _method, _path, body=None, headers=None) -> None:
                self.request_body = body

            def getresponse(self):
                return _StreamingResponse()

            def close(self) -> None:
                return None

        with (
            tempfile.TemporaryDirectory(prefix="DocuRandomWebStream_") as temp,
            mock.patch(
                "docurandom.content_ai.http.client.HTTPConnection",
                _StreamingConnection,
            ),
        ):
            chat = ChatGPTWebChat(
                "http://127.0.0.1:17841",
                "chatgpt-web/auto",
                Path(temp),
                self.ACCESS_TOKEN,
            )
            partials: list[str] = []
            self.assertEqual(
                chat.send("Xin chào", on_partial=partials.append),
                "Xin chào",
            )

        self.assertEqual(partials, ["Xin", "Xin chào"])
        self.assertEqual(chat.response_id, "resp_streamed")

    def test_direct_bridge_recovery_uses_the_validated_loopback_port(self) -> None:
        connections = []

        class _Response:
            status = 404

            def read(self, _limit: int = -1) -> bytes:
                return b'{"error":{"message":"not found"}}'

        class _Connection:
            def __init__(self, host: str, port: int, timeout: float) -> None:
                connections.append((host, port, timeout))

            def request(self, _method, _path, headers=None) -> None:
                return None

            def getresponse(self):
                return _Response()

            def close(self) -> None:
                return None

        with (
            tempfile.TemporaryDirectory(prefix="DocuRandomWebRecovery_") as temp,
            mock.patch(
                "docurandom.content_ai.http.client.HTTPConnection",
                _Connection,
            ),
            mock.patch(
                "docurandom.content_ai.CHATGPT_WEB_DISCONNECTED_RECOVERY_SECONDS",
                0.01,
            ),
        ):
            chat = ChatGPTWebChat(
                "http://127.0.0.1:17841",
                "chatgpt-web/high",
                Path(temp),
                self.ACCESS_TOKEN,
            )
            recovered = chat._recover_disconnected_response(
                "turn_docurandom_recovery",
                cancel_event=None,
                on_partial=None,
                on_status=None,
            )

        self.assertIsNone(recovered)
        self.assertTrue(connections)
        self.assertEqual(connections[0][0:2], ("127.0.0.1", 17841))

    def test_missing_thumbnail_image_reports_bounded_text_and_keeps_response_id(self) -> None:
        chat = ChatGPTWebChat('http://127.0.0.1:17841', 'chatgpt-web/high', Path('output'), 'test_token_' * 5)
        chat.response_id = 'accepted-thumbnail-response'
        with self.assertRaises(ContentAIError) as raised:
            chat.parse_generated_image_response({'output_text': 'Tiêu đề ' + 'full script watermark sharp ' * 1000})
        self.assertIn('ChatGPT không trả về dữ liệu ảnh thumbnail', str(raised.exception))
        self.assertLess(len(str(raised.exception)), 350)
        self.assertEqual(chat.response_id, 'accepted-thumbnail-response')

    def test_direct_bridge_generates_image_with_status_only_streaming(self) -> None:
        image_bytes = b"real-image-payload"
        statuses: list[tuple[str, str]] = []
        request_bodies: list[dict[str, object]] = []
        events = [
            {
                "type": "response.status",
                "stage": "image-waiting:0",
                "message": "ChatGPT đang tạo thumbnail",
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_image",
                    "status": "completed",
                    "output_text": "Đã tạo ảnh",
                    "generated_image": {
                        "data_base64": base64.b64encode(image_bytes).decode("ascii"),
                        "mime_type": "image/png",
                        "width": 1280,
                        "height": 720,
                        "size": len(image_bytes),
                    },
                },
            },
        ]

        class _StreamingResponse:
            status = 200

            def __init__(self) -> None:
                self.lines = [
                    (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
                    for event in events
                ]

            def getheader(self, _name: str, default: str = "") -> str:
                return default

            def readline(self, _limit: int) -> bytes:
                return self.lines.pop(0) if self.lines else b""

        class _StreamingConnection:
            def __init__(self, _host: str, _port: int, timeout: float) -> None:
                self.timeout = timeout

            def request(self, _method, _path, body=None, headers=None) -> None:
                request_bodies.append(json.loads(body.decode("utf-8")))

            def getresponse(self):
                return _StreamingResponse()

            def close(self) -> None:
                return None

        with (
            tempfile.TemporaryDirectory(prefix="DocuRandomWebImage_") as temp,
            mock.patch(
                "docurandom.content_ai.http.client.HTTPConnection",
                _StreamingConnection,
            ),
        ):
            chat = ChatGPTWebChat(
                "http://127.0.0.1:17841",
                "chatgpt-web/high",
                Path(temp),
                self.ACCESS_TOKEN,
            )
            generated = chat.generate_image(
                "Tạo một thumbnail",
                on_status=lambda stage, message: statuses.append((stage, message)),
            )

        self.assertEqual(generated.data, image_bytes)
        self.assertEqual((generated.width, generated.height), (1280, 720))
        self.assertEqual(generated.mime_type, "image/png")
        self.assertEqual(chat.response_id, "resp_image")
        self.assertEqual(request_bodies[0]["docurandom_output"], "generated_image")
        self.assertTrue(request_bodies[0]["stream"])
        self.assertEqual(statuses, [("image-waiting:0", "ChatGPT đang tạo thumbnail")])

    def test_terminal_recovery_without_image_is_reported_as_terminal(self) -> None:
        events = [
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_refusal",
                    "status": "completed",
                    "output_text": "Không thể tạo ảnh.",
                    "recovery_error": "ChatGPT không tạo được ảnh thumbnail trong lượt này.",
                },
            },
        ]

        class _Response:
            status = 200

            def __init__(self) -> None:
                self.lines = [
                    (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
                    for event in events
                ]

            def getheader(self, _name: str, default: str = "") -> str:
                return default

            def readline(self, _limit: int) -> bytes:
                return self.lines.pop(0) if self.lines else b""

        class _Connection:
            def __init__(self, _host: str, _port: int, timeout: float) -> None:
                self.timeout = timeout

            def request(self, _method, _path, body=None, headers=None) -> None:
                return None

            def getresponse(self):
                return _Response()

            def close(self) -> None:
                return None

        with (
            tempfile.TemporaryDirectory(prefix="DocuRandomWebTerminalImage_") as temp,
            mock.patch(
                "docurandom.content_ai.http.client.HTTPConnection",
                _Connection,
            ),
        ):
            chat = ChatGPTWebChat(
                "http://127.0.0.1:17841",
                "chatgpt-web/high",
                Path(temp),
                self.ACCESS_TOKEN,
            )
            with self.assertRaisesRegex(
                ContentAIError,
                r"CHATGPT_TERMINAL_RESPONSE_NO_IMAGE",
            ):
                chat.generate_image("Tạo thumbnail mới", on_status=lambda *_: None)

    def test_profile_hardening_is_app_scoped_and_disables_external_access(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWebProfile_") as temp:
            root = Path(temp)
            with mock.patch("docurandom.content_ai.app_data_dir", return_value=root):
                harden_chatgpt_web_profile()
                policy_path = root / "chat-bridge" / "bridge-policy.json"
                state = json.loads(policy_path.read_text(encoding="utf-8"))

            self.assertTrue(state["loopback_only"])
            self.assertFalse(state["external_cookie_import"])
            self.assertTrue(state["codex_integration"])
            self.assertFalse(state["telemetry"])

    def test_bundled_portable_bridge_verifies_hash_without_installing(self) -> None:
        payload = b"trusted-test-installer"
        expected_hash = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory(prefix="DocuRandomWebInstall_") as temp:
            root = Path(temp)
            bridge = root / "DocuRandom Chat Bridge.exe"
            bridge.write_bytes(payload)

            with (
                mock.patch(
                    "docurandom.content_ai.discover_chatgpt_web_launcher",
                    return_value=None,
                ),
                mock.patch(
                    "docurandom.content_ai.CHATGPT_WEB_INSTALLER_SHA256",
                    expected_hash,
                ),
                mock.patch(
                    "docurandom.content_ai._bundled_chatgpt_web_installer",
                    return_value=bridge,
                ),
                mock.patch("docurandom.content_ai.subprocess.run") as run,
                mock.patch("docurandom.content_ai.app_data_dir", return_value=root),
            ):
                result = install_chatgpt_web_launcher()

            self.assertEqual(result, bridge)
            run.assert_not_called()

    def test_installer_rejects_modified_bundled_payload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWebInstallBad_") as temp:
            installer = Path(temp) / "bundled-installer.exe"
            installer.write_bytes(b"modified-payload")
            with (
                mock.patch(
                    "docurandom.content_ai.discover_chatgpt_web_launcher",
                    return_value=None,
                ),
                mock.patch(
                    "docurandom.content_ai._bundled_chatgpt_web_installer",
                    return_value=installer,
                ),
                mock.patch(
                    "docurandom.content_ai.CHATGPT_WEB_INSTALLER_SHA256",
                    hashlib.sha256(b"expected-payload").hexdigest(),
                ),
                mock.patch("docurandom.content_ai.subprocess.run") as run,
            ):
                with self.assertRaises(ContentAIError):
                    install_chatgpt_web_launcher()
            run.assert_not_called()

    def test_codex_activity_watchdog_reports_stale_bridge_without_answer_deadline(self) -> None:
        class _Response:
            status = 200

            def read(self, _limit: int = -1) -> bytes:
                return json.dumps(
                    {
                        "active": True,
                        "thread_id": "thread-watchdog",
                        "turn_id": "turn_watchdog_123",
                        "updated_at": 1,
                        "stage": "codex-running",
                        "message": "Đang chờ ChatGPT phản hồi",
                    }
                ).encode("utf-8")

        class _Connection:
            def __init__(self, _host: str, _port: int, timeout: float) -> None:
                self.timeout = timeout

            def request(self, _method, _path, headers=None) -> None:
                return None

            def getresponse(self):
                return _Response()

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory(prefix="DocuRandomWatchdog_") as temp:
            chat = CodexCliChat(
                Path("C:/portable/codex.exe"),
                Path(temp),
                access_token=self.ACCESS_TOKEN,
                endpoint="http://127.0.0.1:1",
            )
            chat.thread_id = "thread-watchdog"
            stop_event = threading.Event()
            stalled: list[tuple[str, str, str]] = []

            def on_stalled(message: str, thread_id: str, turn_id: str) -> None:
                stalled.append((message, thread_id, turn_id))
                stop_event.set()

            with mock.patch(
                "docurandom.content_ai.http.client.HTTPConnection",
                _Connection,
            ):
                activity = threading.Thread(
                    target=chat._poll_bridge_activity,
                    kwargs={
                        "stop_event": stop_event,
                        "on_partial": None,
                        "on_status": None,
                        "on_stalled": on_stalled,
                        "poll_interval": 0.01,
                        "stale_after": 0.05,
                    },
                    daemon=True,
                )
                activity.start()
                activity.join(timeout=1.0)

        self.assertFalse(activity.is_alive())
        self.assertEqual(len(stalled), 1)
        self.assertEqual(stalled[0][1:], ("thread-watchdog", "turn_watchdog_123"))
        self.assertIn("mất heartbeat", stalled[0][0])

    def test_codex_activity_watchdog_ignores_status_clock_for_progress(self) -> None:
        class _Response:
            status = 200

            def __init__(self, status_clock: int) -> None:
                self.status_clock = status_clock

            def read(self, _limit: int = -1) -> bytes:
                return json.dumps(
                    {
                        "active": True,
                        "thread_id": "thread-progress-clock",
                        "turn_id": "turn_progress_123",
                        # This changes on every DOM status poll, but is not
                        # evidence that the answer itself progressed.
                        "updated_at": self.status_clock,
                        "activity_updated_at": 1,
                        "no_response_evidence": False,
                        "stage": "waiting:12",
                        "message": "Đang suy nghĩ",
                    }
                ).encode("utf-8")

        class _Connection:
            status_clock = 0

            def __init__(self, _host: str, _port: int, timeout: float) -> None:
                self.timeout = timeout

            def request(self, _method, _path, headers=None) -> None:
                return None

            def getresponse(self):
                self.__class__.status_clock += 1
                return _Response(self.__class__.status_clock)

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory(prefix="DocuRandomProgressClock_") as temp:
            chat = CodexCliChat(
                Path("C:/portable/codex.exe"),
                Path(temp),
                access_token=self.ACCESS_TOKEN,
                endpoint="http://127.0.0.1:1",
            )
            chat.thread_id = "thread-progress-clock"
            stop_event = threading.Event()
            stalled: list[tuple[str, str, str]] = []

            def on_stalled(message: str, thread_id: str, turn_id: str) -> None:
                stalled.append((message, thread_id, turn_id))
                stop_event.set()

            with mock.patch(
                "docurandom.content_ai.http.client.HTTPConnection",
                _Connection,
            ):
                activity = threading.Thread(
                    target=chat._poll_bridge_activity,
                    kwargs={
                        "stop_event": stop_event,
                        "on_partial": None,
                        "on_status": None,
                        "on_stalled": on_stalled,
                        "poll_interval": 0.01,
                        "stale_after": 0.05,
                    },
                    daemon=True,
                )
                activity.start()
                activity.join(timeout=1.0)

        self.assertFalse(activity.is_alive())
        self.assertEqual(len(stalled), 1)
        self.assertEqual(stalled[0][1:], ("thread-progress-clock", "turn_progress_123"))

    def test_codex_startup_watchdog_reports_missing_thread_without_answer_deadline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomStartupWatchdog_") as temp:
            chat = CodexCliChat(
                Path("C:/portable/codex.exe"),
                Path(temp),
                access_token=self.ACCESS_TOKEN,
                endpoint="http://127.0.0.1:17841",
            )
            stop_event = threading.Event()
            stalled: list[str] = []

            def on_stalled(message: str) -> None:
                stalled.append(message)
                stop_event.set()

            started = time.monotonic() - 1.0
            activity = threading.Thread(
                target=chat._poll_bridge_activity,
                kwargs={
                    "stop_event": stop_event,
                    "on_partial": None,
                    "on_status": None,
                    "on_startup_stalled": on_stalled,
                    "startup_started_at": started,
                    "startup_last_activity": [started],
                    "startup_stale_after": 0.05,
                    "poll_interval": 0.01,
                },
                daemon=True,
            )
            activity.start()
            activity.join(timeout=1.0)

        self.assertFalse(activity.is_alive())
        self.assertEqual(len(stalled), 1)
        self.assertIn("chưa xác nhận prompt", stalled[0])

    def test_codex_startup_watchdog_accepts_bridge_heartbeat_without_cli_thread_event(self) -> None:
        class _Response:
            status = 200

            def __init__(self, updated_at: int) -> None:
                self.updated_at = updated_at

            def read(self, _limit: int = -1) -> bytes:
                return json.dumps(
                    {
                        "active_turns": 1,
                        "activity_updated_at": self.updated_at,
                    }
                ).encode("utf-8")

        class _Connection:
            updated_at = 0

            def __init__(self, _host: str, _port: int, timeout: float) -> None:
                self.timeout = timeout

            def request(self, _method, _path, headers=None) -> None:
                return None

            def getresponse(self):
                self.__class__.updated_at += 1
                return _Response(self.__class__.updated_at)

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory(prefix="DocuRandomStartupHeartbeat_") as temp:
            chat = CodexCliChat(
                Path("C:/portable/codex.exe"),
                Path(temp),
                access_token=self.ACCESS_TOKEN,
                endpoint="http://127.0.0.1:17841",
            )
            stop_event = threading.Event()
            stalled: list[str] = []

            def stop_later() -> None:
                time.sleep(0.12)
                stop_event.set()

            stopper = threading.Thread(target=stop_later, daemon=True)
            stopper.start()
            with mock.patch(
                "docurandom.content_ai.http.client.HTTPConnection",
                _Connection,
            ):
                activity = threading.Thread(
                    target=chat._poll_bridge_activity,
                    kwargs={
                        "stop_event": stop_event,
                        "on_partial": None,
                        "on_status": None,
                        "on_startup_stalled": stalled.append,
                        "startup_started_at": time.monotonic(),
                        "startup_last_activity": [time.monotonic()],
                        "startup_stale_after": 0.05,
                        "poll_interval": 0.01,
                    },
                    daemon=True,
                )
                activity.start()
                activity.join(timeout=1.0)

        self.assertFalse(activity.is_alive())
        self.assertEqual(stalled, [])

    def test_codex_startup_watchdog_does_not_treat_active_slot_as_heartbeat(self) -> None:
        class _Response:
            status = 200

            def read(self, _limit: int = -1) -> bytes:
                return json.dumps(
                    {
                        "active_turns": 1,
                        # The bridge is alive but this turn never advances.
                        "activity_updated_at": 7,
                        "no_response_evidence": False,
                    }
                ).encode("utf-8")

        class _Connection:
            def __init__(self, _host: str, _port: int, timeout: float) -> None:
                self.timeout = timeout

            def request(self, _method, _path, headers=None) -> None:
                return None

            def getresponse(self):
                return _Response()

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory(prefix="DocuRandomStartupStaleHeartbeat_") as temp:
            chat = CodexCliChat(
                Path("C:/portable/codex.exe"),
                Path(temp),
                access_token=self.ACCESS_TOKEN,
                endpoint="http://127.0.0.1:17841",
            )
            stop_event = threading.Event()
            stalled: list[str] = []

            def on_stalled(message: str) -> None:
                stalled.append(message)
                stop_event.set()

            started = time.monotonic() - 1.0
            with mock.patch(
                "docurandom.content_ai.http.client.HTTPConnection",
                _Connection,
            ):
                activity = threading.Thread(
                    target=chat._poll_bridge_activity,
                    kwargs={
                        "stop_event": stop_event,
                        "on_partial": None,
                        "on_status": None,
                        "on_startup_stalled": on_stalled,
                        "startup_started_at": started,
                        "startup_last_activity": [started],
                        "startup_stale_after": 0.05,
                        "poll_interval": 0.01,
                    },
                    daemon=True,
                )
                activity.start()
                activity.join(timeout=1.0)

        self.assertFalse(activity.is_alive())
        self.assertEqual(len(stalled), 1)
        self.assertIn("chưa xác nhận prompt", stalled[0])


if __name__ == "__main__":
    unittest.main()
