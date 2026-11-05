from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from docurandom.voicevox_project import (
    VoicevoxProjectError,
    VoicevoxStyleOption,
    apply_audit_plan,
    build_audit_attachment,
    build_passthrough_audit_plan,
    build_voicevox_project,
    load_voicevox_project,
    parse_audit_plan,
    project_boxes,
    render_items_from_project,
    voicevox_project_digest,
    write_final_project,
    write_project_checkpoint,
)


def _engine_query(kana: str) -> dict[str, object]:
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
                    },
                    {
                        "text": "ス",
                        "consonant": "s",
                        "consonant_length": 0.05,
                        "vowel": "U",
                        "vowel_length": 0.05,
                        "pitch": 0.0,
                    },
                ],
                "accent": 1,
                "pause_mora": {
                    "text": "、",
                    "vowel": "pau",
                    "vowel_length": 0.2,
                    "pitch": 0.0,
                },
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


def _project() -> dict[str, object]:
    return build_voicevox_project(
        app_version="0.25.2",
        lines=("最初の文です。", "技術の確認です。"),
        queries=(_engine_query("サイショ"), _engine_query("ギジュツ")),
        style=VoicevoxStyleOption("Narrator", "speaker-a", "Normal", 10),
    )


def _response(project: dict[str, object], *, style_id: int = 11) -> str:
    return json.dumps(
        {
            "schemaVersion": 1,
            "sourceDigest": voicevox_project_digest(project),
            "presets": [
                {
                    "id": "hook",
                    "function": "Mở đầu chắc",
                    "params": {
                        "speedScale": 1.08,
                        "pitchScale": -0.01,
                        "intonationScale": 1.05,
                        "volumeScale": 1.00,
                        "pauseLengthScale": 0.95,
                        "prePhonemeLength": 0.08,
                        "postPhonemeLength": 0.16,
                    },
                },
                {
                    "id": "normal",
                    "function": "Giải thích rõ",
                    "params": {
                        "speedScale": 1.00,
                        "pitchScale": 0.00,
                        "intonationScale": 0.98,
                        "volumeScale": 1.00,
                        "pauseLengthScale": 1.00,
                        "prePhonemeLength": 0.10,
                        "postPhonemeLength": 0.12,
                    },
                },
            ],
            "mappings": [
                {"presetId": "hook", "styleId": style_id, "boxIndexes": [1]},
                {"presetId": "normal", "styleId": 10, "boxIndexes": [2]},
            ],
            "overrides": [
                {"boxIndex": 2, "params": {"postPhonemeLength": 0.25}}
            ],
            "pronunciations": [
                {
                    "boxIndex": 2,
                    "correctedKana": "ギジュツノカクニンデス",
                    "reason": "Cách đọc kỹ thuật",
                }
            ],
        },
        ensure_ascii=False,
    )


class VoicevoxProjectTests(unittest.TestCase):
    def test_build_project_converts_engine_query_to_vvproj_schema(self) -> None:
        project = _project()
        boxes = project_boxes(project)
        self.assertEqual(len(boxes), 2)
        self.assertEqual(boxes[0].speaker_id, "speaker-a")
        self.assertIn("accentPhrases", boxes[0].query)
        phrase = boxes[0].query["accentPhrases"][0]  # type: ignore[index]
        self.assertIn("pauseMora", phrase)
        self.assertIn("vowelLength", phrase["moras"][0])

    def test_build_project_accepts_voicevox_vowel_only_mora_nulls(self) -> None:
        query = _engine_query("アイ")
        mora = query["accent_phrases"][0]["moras"][0]  # type: ignore[index]
        mora["consonant"] = None  # type: ignore[index]
        mora["consonant_length"] = None  # type: ignore[index]

        project = build_voicevox_project(
            app_version="0.25.2",
            lines=("愛です。",),
            queries=(query,),
            style=VoicevoxStyleOption("Narrator", "speaker-a", "Normal", 10),
        )

        built_mora = project_boxes(project)[0].query["accentPhrases"][0]["moras"][0]  # type: ignore[index]
        self.assertIsNone(built_mora["consonant"])
        self.assertIsNone(built_mora["consonantLength"])

    def test_chat_plan_may_change_style_only_inside_locked_character(self) -> None:
        project = _project()
        plan = parse_audit_plan(
            _response(project),
            project_data=project,
            allowed_style_ids=(10, 11),
        )
        resolver_calls: list[tuple[str, int, str | None]] = []

        def resolver(
            _box_index: int,
            text: str,
            style_id: int,
            kana: str | None,
        ) -> dict[str, object]:
            resolver_calls.append((text, style_id, kana))
            return _engine_query(kana or "REGENERATED")

        updated = apply_audit_plan(project, plan, query_resolver=resolver)
        boxes = project_boxes(updated)
        self.assertEqual([box.style_id for box in boxes], [11, 10])
        self.assertEqual(boxes[0].speaker_id, "speaker-a")
        self.assertEqual(boxes[0].query["speedScale"], 1.08)
        self.assertEqual(boxes[1].query["postPhonemeLength"], 0.25)
        self.assertEqual(len(resolver_calls), 2)
        self.assertIsNone(resolver_calls[0][2])
        self.assertEqual(resolver_calls[1][2], "ギジュツノカクニンデス")

    def test_chat_plan_accepts_literal_control_character_inside_json_string(self) -> None:
        project = _project()
        response = _response(project).replace("Mở đầu chắc", "Mở đầu\nchắc")

        plan = parse_audit_plan(
            response,
            project_data=project,
            allowed_style_ids=(10, 11),
        )

        self.assertEqual(plan.presets[0]["function"], "Mở đầu\nchắc")

    def test_chat_plan_extracts_complete_json_from_surrounding_text(self) -> None:
        project = _project()

        plan = parse_audit_plan(
            "Đây là kết quả:\n" + _response(project) + "\nHoàn tất.",
            project_data=project,
            allowed_style_ids=(10, 11),
        )

        self.assertEqual(set(plan.mapping_by_box), {1, 2})

    def test_safe_passthrough_plan_preserves_original_style_and_sliders(self) -> None:
        project = _project()
        boxes = project_boxes(project)
        second_item = project["talk"]["audioItems"][boxes[1].audio_key]  # type: ignore[index]
        second_item["query"]["speedScale"] = 1.13  # type: ignore[index]
        plan = build_passthrough_audit_plan(
            project,
            allowed_style_ids=(10, 11),
        )

        def unexpected_resolver(
            _box_index: int,
            _text: str,
            _style_id: int,
            _kana: str | None,
        ) -> dict[str, object]:
            self.fail("Passthrough plan must not regenerate an audio query")

        updated = apply_audit_plan(
            project,
            plan,
            query_resolver=unexpected_resolver,
        )
        updated_boxes = project_boxes(updated)
        self.assertEqual([box.style_id for box in updated_boxes], [10, 10])
        self.assertEqual(updated_boxes[0].query["speedScale"], 1.0)
        self.assertEqual(updated_boxes[1].query["speedScale"], 1.13)

    def test_chat_plan_rejects_style_from_another_character(self) -> None:
        project = _project()
        with self.assertRaisesRegex(VoicevoxProjectError, "ngoài nhân vật"):
            parse_audit_plan(
                _response(project, style_id=999),
                project_data=project,
                allowed_style_ids=(10, 11),
            )

    def test_chat_plan_accepts_valid_engine_precision_beyond_ui_slider_step(self) -> None:
        project = _project()
        raw = json.loads(_response(project))
        raw["presets"][0]["params"]["speedScale"] = 1.075
        plan = parse_audit_plan(
                json.dumps(raw, ensure_ascii=False),
                project_data=project,
                allowed_style_ids=(10, 11),
            )
        self.assertEqual(plan.presets[0]["params"]["speedScale"], 1.075)

    def test_attachment_contains_locked_speaker_and_only_supplied_styles(self) -> None:
        project = _project()
        value = json.loads(
            build_audit_attachment(
                project,
                styles=(
                    VoicevoxStyleOption("Narrator", "speaker-a", "Normal", 10),
                    VoicevoxStyleOption("Narrator", "speaker-a", "Tense", 11),
                ),
            ).decode("utf-8")
        )
        self.assertEqual(value["lockedSpeakerUuid"], "speaker-a")
        self.assertEqual(
            [item["styleId"] for item in value["availableStylesForLockedSpeaker"]],
            [10, 11],
        )

    def test_audit_attachment_keeps_pronunciation_evidence_in_compact_form(self) -> None:
        project = _project()
        original_query = project_boxes(project)[0].query
        value = json.loads(
            build_audit_attachment(project, styles=()).decode("utf-8")
        )
        box = value["boxes"][0]
        query = box["currentQuery"]
        original_phrase = original_query["accentPhrases"][0]  # type: ignore[index]
        compact_phrase = query["accentPhrases"][0]

        self.assertEqual(value["sourceDigest"], voicevox_project_digest(project))
        self.assertEqual(box["text"], project_boxes(project)[0].text)
        self.assertEqual(query["kana"], original_query["kana"])
        self.assertEqual(compact_phrase["accent"], original_phrase["accent"])
        self.assertEqual(
            compact_phrase["moras"],
            [
                [mora["text"], mora["pitch"]]
                for mora in original_phrase["moras"]
            ],
        )
        self.assertEqual(
            compact_phrase["pauseMora"],
            [
                original_phrase["pauseMora"]["text"],
                original_phrase["pauseMora"]["pitch"],
            ],
        )
        self.assertEqual(len(compact_phrase["moras"][0]), 2)
        self.assertFalse(compact_phrase["isInterrogative"])
        self.assertNotIn("outputSamplingRate", query)
        original_query_json = json.dumps(original_query, ensure_ascii=False, separators=(",", ":"))
        compact_query_json = json.dumps(query, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(compact_query_json), len(original_query_json))
        self.assertEqual(project_boxes(project)[0].query, original_query)

    def test_final_files_preserve_text_and_render_query_uses_engine_schema(self) -> None:
        project = _project()
        plan = parse_audit_plan(
            _response(project),
            project_data=project,
            allowed_style_ids=(10, 11),
        )
        updated = apply_audit_plan(
            project,
            plan,
            query_resolver=lambda _index, _text, _style, kana: _engine_query(kana or "OK"),
        )
        with tempfile.TemporaryDirectory(prefix="DocuRandomVvproj_") as temp:
            source = Path(temp) / "Video 1.vvproj"
            write_project_checkpoint(source, project)
            result = write_final_project(
                source_path=source,
                project_data=updated,
                plan=plan,
            )
            self.assertEqual(result.box_count, 2)
            self.assertEqual(result.style_ids, (10, 11))
            loaded = load_voicevox_project(result.project_path)
            self.assertEqual(
                result.content_path.read_text(encoding="utf-8").splitlines(),
                [box.text for box in project_boxes(loaded)],
            )
            render_items = render_items_from_project(loaded)
            self.assertIn("accent_phrases", render_items[0][2])
            self.assertIn("vowel_length", render_items[0][2]["accent_phrases"][0]["moras"][0])  # type: ignore[index]
            self.assertIn("speedScale", render_items[0][2])
            self.assertNotIn("speed_scale", render_items[0][2])

    def test_legacy_project_without_pause_scale_is_upgraded_in_memory(self) -> None:
        project = _project()
        item = next(iter(project["talk"]["audioItems"].values()))  # type: ignore[index,union-attr]
        item["query"].pop("pauseLengthScale")  # type: ignore[index,union-attr]
        boxes = project_boxes(project)
        self.assertEqual(boxes[0].query["pauseLengthScale"], 1.0)


if __name__ == "__main__":
    unittest.main()
