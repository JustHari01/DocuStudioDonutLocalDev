from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path

from docurandom.voicevox_qa import (
    audit_voicevox_audio,
    select_voicevox_qa_lines,
    voicevox_transcript_similarity,
)


def _write_wave(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * 400)


class VoicevoxQaTests(unittest.TestCase):
    def test_risk_selection_always_keeps_pronunciation_patches(self) -> None:
        lines = tuple(f"通常の文章です。" for _ in range(30))
        selected = select_voicevox_qa_lines(
            lines,
            pronunciation_indexes=(7, 18, 29),
            limit=4,
        )
        self.assertTrue({7, 18, 29}.issubset(selected))
        self.assertLessEqual(len(selected), 4)

    def test_similarity_ignores_spacing_and_punctuation(self) -> None:
        self.assertEqual(
            voicevox_transcript_similarity("日本の技術です。", " 日本の技術です "),
            1.0,
        )

    def test_selective_qa_writes_report_and_reuses_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomVoiceQa_") as temp:
            root = Path(temp)
            parts = root / "parts"
            parts.mkdir()
            _write_wave(parts / "0001.wav")
            _write_wave(parts / "0002.wav")
            checkpoint = parts / "checkpoint.json"
            checkpoint.write_text("{}", encoding="utf-8")
            metadata = root / "voice.voicevox.json"
            metadata.write_text(
                json.dumps({"parts_checkpoint": str(checkpoint)}),
                encoding="utf-8",
            )
            recognized = {
                "0001.wav": "最初の文です。",
                "0002.wav": "まったく違う読みです。",
            }

            result = audit_voicevox_audio(
                metadata_path=metadata,
                lines=("最初の文です。", "CNCは二百回です。"),
                pronunciation_indexes=(2,),
                recognizer=lambda path: recognized[path.name],
            )

            self.assertEqual(result.status, "warning")
            self.assertEqual(result.warning_indexes, (2,))
            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["checked_indexes"], [1, 2])
            reused = audit_voicevox_audio(
                metadata_path=metadata,
                lines=("最初の文です。", "CNCは二百回です。"),
                pronunciation_indexes=(2,),
                recognizer=lambda _path: self.fail("checkpoint must avoid ASR"),
            )
            self.assertTrue(reused.reused)


if __name__ == "__main__":
    unittest.main()
