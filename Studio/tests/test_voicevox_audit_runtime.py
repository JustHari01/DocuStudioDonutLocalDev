from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from docurandom.ui import DocuRandomApp, VoicevoxAuditTimeout
from docurandom.voicevox import VoicevoxKanaError
from docurandom.voicevox_project import (
    VoicevoxStyleOption,
    build_voicevox_project,
    project_boxes,
    voicevox_project_digest,
)


def _query(kana: str) -> dict[str, object]:
    return {
        "accent_phrases": [
            {
                "moras": [
                    {
                        "text": "テ",
                        "consonant": "t",
                        "consonant_length": 0.05,
                        "vowel": "e",
                        "vowel_length": 0.1,
                        "pitch": 5.0,
                    }
                ],
                "accent": 1,
                "pause_mora": None,
                "is_interrogative": False,
            }
        ],
        "speedScale": 1.0,
        "pitchScale": 0.0,
        "intonationScale": 1.0,
        "volumeScale": 1.0,
        "pauseLengthScale": 1.0,
        "prePhonemeLength": 0.1,
        "postPhonemeLength": 0.1,
        "outputSamplingRate": 24000,
        "outputStereo": False,
        "kana": kana,
    }


class VoicevoxAuditRuntimeTests(unittest.TestCase):
    def test_automatic_chatgpt_audit_timeout_does_not_use_passthrough(self) -> None:
        project = build_voicevox_project(
            app_version="0.25.2",
            lines=("技術の確認です。",),
            queries=(_query("ギジュツ'ノカクニンデス"),),
            style=VoicevoxStyleOption("Narrator", "speaker-a", "Normal", 10),
        )
        app = DocuRandomApp.__new__(DocuRandomApp)
        app._send_voicevox_audit_to_chatgpt = Mock(
            side_effect=VoicevoxAuditTimeout("audit timeout")
        )
        client = Mock()
        with (
            patch("docurandom.ui.VOICEVOX_AUDIT_RETRY_DELAY_SECONDS", 0.0),
            tempfile.TemporaryDirectory(prefix="DocuRandomAuditRequired_") as temp,
        ):
            with self.assertRaises(VoicevoxAuditTimeout):
                app._audit_voicevox_project_data(
                    project_data=project,
                    source_path=Path(temp) / "video.vvproj",
                    styles=(VoicevoxStyleOption("Narrator", "speaker-a", "Normal", 10),),
                    client=client,
                    cancel_event=threading.Event(),
                    progress=Mock(),
                )

        # A timeout is uncertain delivery, so retain/recover the same receipt
        # instead of spending two more automatic sends.
        self.assertEqual(app._send_voicevox_audit_to_chatgpt.call_count, 1)
        self.assertFalse(client.audited_audio_query.called)

    def test_chatgpt_audit_timeout_uses_passthrough_without_retry(self) -> None:
        project = build_voicevox_project(
            app_version="0.25.2",
            lines=("技術の確認です。",),
            queries=(_query("ギジュツ'ノカクニンデス"),),
            style=VoicevoxStyleOption("Narrator", "speaker-a", "Normal", 10),
        )
        app = DocuRandomApp.__new__(DocuRandomApp)
        app._send_voicevox_audit_to_chatgpt = Mock(
            side_effect=VoicevoxAuditTimeout("audit timeout")
        )
        client = Mock()
        messages: list[str] = []
        with tempfile.TemporaryDirectory(prefix="DocuRandomAuditTimeout_") as temp:
            updated, result = app._audit_voicevox_project_data(
                project_data=project,
                source_path=Path(temp) / "video.vvproj",
                styles=(VoicevoxStyleOption("Narrator", "speaker-a", "Normal", 10),),
                client=client,
                cancel_event=threading.Event(),
                progress=messages.append,
                allow_safe_fallback=True,
            )

        self.assertTrue(result.used_safe_fallback)
        self.assertEqual(app._send_voicevox_audit_to_chatgpt.call_count, 1)
        self.assertFalse(client.audited_audio_query.called)
        self.assertEqual(project_boxes(updated)[0].style_id, 10)
        self.assertTrue(any("không gửi lại prompt" in message for message in messages))

    def test_invalid_pronunciation_falls_back_only_for_that_box(self) -> None:
        project = build_voicevox_project(
            app_version="0.25.2",
            lines=("技術の確認です。",),
            queries=(_query("ギジュツ'ノカクニンデス"),),
            style=VoicevoxStyleOption("Narrator", "speaker-a", "Normal", 10),
        )
        response = json.dumps(
            {
                "schemaVersion": 1,
                "sourceDigest": voicevox_project_digest(project),
                "presets": [
                    {
                        "id": "documentary",
                        "function": "Đọc rõ câu kỹ thuật",
                        "params": {
                            "speedScale": 1.07,
                            "pitchScale": 0.0,
                            "intonationScale": 1.02,
                            "volumeScale": 1.0,
                            "pauseLengthScale": 1.0,
                            "prePhonemeLength": 0.1,
                            "postPhonemeLength": 0.14,
                        },
                    }
                ],
                "mappings": [
                    {"presetId": "documentary", "styleId": 11, "boxIndexes": [1]}
                ],
                "overrides": [{"boxIndex": 1, "params": {"volumeScale": 1.03}}],
                "pronunciations": [
                    {
                        "boxIndex": 1,
                        "correctedKana": "ギジュツノカクニンデス",
                        "reason": "Đề xuất thiếu accent marker",
                    }
                ],
            },
            ensure_ascii=False,
        )
        app = DocuRandomApp.__new__(DocuRandomApp)
        app._send_voicevox_audit_to_chatgpt = Mock(return_value=response)
        client = Mock()

        def resolve(
            _text: str,
            _style_id: int,
            *,
            corrected_kana: str | None,
            cancel_event: threading.Event,
        ) -> dict[str, object]:
            self.assertFalse(cancel_event.is_set())
            if corrected_kana is not None:
                raise VoicevoxKanaError(
                    "invalid kana",
                    error_name="ACCENT_NOTFOUND",
                )
            return _query("ギジュツ'ノカクニンデス")

        client.audited_audio_query.side_effect = resolve
        messages: list[str] = []
        with tempfile.TemporaryDirectory(prefix="DocuRandomAuditRuntime_") as temp:
            updated, result = app._audit_voicevox_project_data(
                project_data=project,
                source_path=Path(temp) / "video.vvproj",
                styles=(
                    VoicevoxStyleOption("Narrator", "speaker-a", "Normal", 10),
                    VoicevoxStyleOption("Narrator", "speaker-a", "Tense", 11),
                ),
                client=client,
                cancel_event=threading.Event(),
                progress=messages.append,
            )

        box = project_boxes(updated)[0]
        self.assertEqual(box.style_id, 11)
        self.assertEqual(box.query["speedScale"], 1.07)
        self.assertEqual(box.query["volumeScale"], 1.03)
        self.assertEqual(result.pronunciation_count, 0)
        self.assertEqual(result.pronunciation_indexes, ())
        self.assertTrue(any("giữ cách đọc gốc" in message for message in messages))
        self.assertEqual(client.audited_audio_query.call_count, 2)


if __name__ == "__main__":
    unittest.main()
