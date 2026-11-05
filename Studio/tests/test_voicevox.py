from __future__ import annotations

import io
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
import wave
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from docurandom.voicevox import (
    VoicevoxCancelled,
    VoicevoxClient,
    VoicevoxEnginePackage,
    VoicevoxEngineController,
    VoicevoxKanaError,
    VoicevoxStyle,
    VoicevoxSynthesisSettings,
    discover_engine_path,
    install_official_engine,
    load_valid_synthesis_result,
    managed_engine_root,
    normalize_endpoint,
    parse_script_lines,
    synthesize_script,
)


def _wav_bytes(frames: int = 400, rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\0\0" * frames)
    return buffer.getvalue()


class _VoicevoxHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    queries: list[dict[str, object]] = []
    slow_synthesis = False
    synthesis_started = threading.Event()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_json(self, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json_error(self, status: int, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/version":
            self._send_json("test-1.0")
            return
        if self.path == "/speakers":
            self._send_json(
                [
                    {
                        "name": "Test Voice",
                        "speaker_uuid": "speaker-uuid",
                        "styles": [
                            {"name": "Normal", "id": 7, "type": "talk"},
                            {"name": "Singer", "id": 6000, "type": "sing"},
                        ],
                        "version": "1",
                    }
                ]
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/audio_query":
            query = urllib.parse.parse_qs(parsed.query)
            self._send_json(
                {
                    "accent_phrases": [],
                    "speedScale": 1.0,
                    "pitchScale": 0.0,
                    "intonationScale": 1.0,
                    "volumeScale": 1.0,
                    "prePhonemeLength": 0.1,
                    "postPhonemeLength": 0.1,
                    "outputSamplingRate": 8000,
                    "outputStereo": False,
                    "kana": query.get("text", [""])[0],
                }
            )
            return
        if parsed.path == "/accent_phrases":
            query = urllib.parse.parse_qs(parsed.query)
            text = query.get("text", [""])[0]
            if "'" not in text:
                self._send_json_error(
                    400,
                    {
                        "detail": {
                            "text": "アクセントを指定していないアクセント句があります",
                            "error_name": "ACCENT_NOTFOUND",
                            "error_args": {"text": text},
                        }
                    },
                )
                return
            self._send_json(
                [
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
                ]
            )
            return
        if parsed.path == "/synthesis":
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            type(self).queries.append(value)
            if type(self).slow_synthesis:
                type(self).synthesis_started.set()
                time.sleep(2.0)
            payload = _wav_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.send_error(404)


class VoicevoxTests(unittest.TestCase):
    def test_managed_engine_uses_selected_docurandom_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomVoicevoxHome_") as temp:
            selected = Path(temp) / "DocuRandom Studio"
            with patch.dict(
                os.environ,
                {"DOCURANDOM_HOME": str(selected)},
                clear=True,
            ):
                self.assertEqual(
                    managed_engine_root(),
                    selected.resolve() / "Data" / "VOICEVOX Engine",
                )

    @staticmethod
    def _test_engine_package(
        source: Path,
        *,
        traversal: bool = False,
    ) -> VoicevoxEnginePackage:
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "engine_manifest.json",
                json.dumps({"command": "run", "version": "test"}),
            )
            archive.writestr("run.exe", b"test-engine")
            if traversal:
                archive.writestr("../escape.txt", b"must-not-escape")
        payload = source.read_bytes()
        return VoicevoxEnginePackage(
            version="test-1.0",
            variant="cpu",
            filename="test-engine.vvpp",
            url=source.as_uri(),
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _VoicevoxHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        _VoicevoxHandler.queries.clear()
        _VoicevoxHandler.slow_synthesis = False
        _VoicevoxHandler.synthesis_started.clear()

    def test_endpoint_is_restricted_to_local_engine(self) -> None:
        self.assertEqual(
            normalize_endpoint("http://localhost:50021/"),
            "http://localhost:50021",
        )
        with self.assertRaises(ValueError):
            normalize_endpoint("https://example.com:50021")

    def test_script_is_line_based_and_bounded(self) -> None:
        self.assertEqual(parse_script_lines(" one\r\n\r\n two "), ["one", "two"])
        with self.assertRaises(ValueError):
            parse_script_lines("\n\n")

    def test_script_accepts_optional_a_dialogue_prefix(self) -> None:
        self.assertEqual(
            parse_script_lines(
                "A: 日本の技術です。\n"
                "A： 次の文章です。\n"
                "Ａ: 全角のラベルです。\n"
                "ラベルなしも読めます。\n"
                "速報: このコロンは内容です。"
            ),
            [
                "日本の技術です。",
                "次の文章です。",
                "全角のラベルです。",
                "ラベルなしも読めます。",
                "速報: このコロンは内容です。",
            ],
        )
        with self.assertRaises(ValueError):
            parse_script_lines("A:\nＡ：")

    def test_script_does_not_turn_export_dividers_into_voice_boxes(self) -> None:
        narration = [f"第{index}の文章です。" for index in range(1, 156)]
        export = "\n".join(f"A: {line}" for line in narration)
        export += "\n━━━━━━━━━━━━━━━━━━━━\nA: ---\n* * *\n___\n═══\n"
        self.assertEqual(parse_script_lines(export), narration)

    def test_script_preserves_spoken_text_and_punctuation(self) -> None:
        self.assertEqual(
            parse_script_lines("A: ━━━技術━━━\nA: 三つの記号---も説明します。\nA: …\nA: 「……」"),
            ["━━━技術━━━", "三つの記号---も説明します。", "…", "「……」"],
        )
        with self.assertRaises(ValueError):
            parse_script_lines("━━━\n---\n* * *")

    def test_client_reads_only_talk_styles(self) -> None:
        client = VoicevoxClient(self.endpoint)
        self.assertEqual(client.version(), "test-1.0")
        styles = client.styles()
        self.assertEqual(len(styles), 1)
        self.assertEqual(styles[0].style_id, 7)
        self.assertIn("Test Voice", styles[0].label)

    def test_invalid_aquestalk_kana_has_a_typed_error(self) -> None:
        client = VoicevoxClient(self.endpoint)
        with self.assertRaises(VoicevoxKanaError) as raised:
            client.audited_audio_query(
                "技術の確認です。",
                7,
                corrected_kana="ギジュツノカクニンデス",
            )
        self.assertEqual(raised.exception.error_name, "ACCENT_NOTFOUND")

    def test_controller_reuses_running_engine_without_owning_it(self) -> None:
        controller = VoicevoxEngineController()
        client, version, started = controller.ensure_running(
            endpoint=self.endpoint,
            engine_path=None,
            use_gpu=True,
            cpu_threads=2,
        )
        self.assertEqual(client.endpoint, self.endpoint)
        self.assertEqual(version, "test-1.0")
        self.assertFalse(started)
        self.assertFalse(controller.owns_process)

    def test_controller_serializes_engine_startup_only(self) -> None:
        controller = VoicevoxEngineController()
        counter_lock = threading.Lock()
        concurrent = 0
        maximum = 0

        def fake_start(**kwargs: object) -> tuple[VoicevoxClient, str, bool]:
            nonlocal concurrent, maximum
            with counter_lock:
                concurrent += 1
                maximum = max(maximum, concurrent)
            time.sleep(0.04)
            with counter_lock:
                concurrent -= 1
            return VoicevoxClient(str(kwargs["endpoint"])), "test", False

        results: list[tuple[VoicevoxClient, str, bool]] = []

        def run_start() -> None:
            results.append(
                controller.ensure_running(
                    endpoint=self.endpoint,
                    engine_path=None,
                    use_gpu=False,
                    cpu_threads=2,
                )
            )

        with patch.object(controller, "_ensure_running_locked", side_effect=fake_start):
            workers = [threading.Thread(target=run_start) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=1.0)

        self.assertEqual(len(results), 2)
        self.assertEqual(maximum, 1)

    def test_synthesize_script_applies_controls_and_combines_lines(self) -> None:
        settings = VoicevoxSynthesisSettings(
            speed_scale=1.25,
            pitch_scale=0.05,
            intonation_scale=1.4,
            volume_scale=0.8,
            line_pause_seconds=0.1,
            output_sampling_rate=8000,
            save_line_files=True,
        )
        style = VoicevoxStyle("Test Voice", "speaker-uuid", "Normal", 7)
        events: list[tuple[int, int, str]] = []
        with tempfile.TemporaryDirectory(prefix="DocuRandomVoicevoxTest_") as temp:
            output = Path(temp) / "voice.wav"
            result = synthesize_script(
                client=VoicevoxClient(self.endpoint),
                script="Dòng một\nDòng hai",
                style=style,
                settings=settings,
                output_path=output,
                progress=lambda done, total, message: events.append(
                    (done, total, message)
                ),
            )
            self.assertTrue(result.audio_path.is_file())
            self.assertTrue(result.subtitle_path.is_file())
            self.assertTrue(result.metadata_path.is_file())
            self.assertIsNotNone(result.line_files_dir)
            self.assertEqual(len(list(result.line_files_dir.glob("*.wav"))), 2)  # type: ignore[union-attr]
            self.assertAlmostEqual(result.duration_seconds, 0.2, places=2)
            with wave.open(str(result.audio_path), "rb") as audio:
                self.assertEqual(audio.getframerate(), 8000)
                self.assertEqual(audio.getnframes(), 1600)
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["line_count"], 2)
            self.assertEqual(metadata["style"]["style_id"], 7)
            self.assertEqual(metadata["subtitle_file"], "voice.srt")
            self.assertEqual(len(metadata["subtitle_cues"]), 2)
            self.assertEqual(
                result.subtitle_path.read_text(encoding="utf-8-sig"),
                (
                    "1\n00:00:00,000 --> 00:00:00,050\nDòng một\n\n"
                    "2\n00:00:00,150 --> 00:00:00,200\nDòng hai\n"
                ),
            )
            self.assertEqual(len(_VoicevoxHandler.queries), 2)
            self.assertEqual(_VoicevoxHandler.queries[0]["speedScale"], 1.25)
            self.assertEqual(_VoicevoxHandler.queries[0]["pauseLengthScale"], 1.0)
            self.assertEqual(events[-1][0:2], (2, 2))
            restored = load_valid_synthesis_result(
                output,
                script="Dòng một\nDòng hai",
                style=style,
                settings=settings,
            )
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.audio_path, result.audio_path)
            self.assertEqual(restored.subtitle_path, result.subtitle_path)

    def test_synthesis_and_subtitles_do_not_include_a_dialogue_prefix(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomVoicevoxPrefix_") as temp:
            output = Path(temp) / "voice.wav"
            result = synthesize_script(
                client=VoicevoxClient(self.endpoint),
                script="A: 日本の技術です。\nラベルなしです。",
                style=VoicevoxStyle("Test Voice", "speaker-uuid", "Normal", 7),
                settings=VoicevoxSynthesisSettings(output_sampling_rate=8000),
                output_path=output,
            )
            self.assertEqual(
                [query["kana"] for query in _VoicevoxHandler.queries],
                ["日本の技術です。", "ラベルなしです。"],
            )
            subtitles = result.subtitle_path.read_text(encoding="utf-8-sig")
            self.assertNotIn("A:", subtitles)
            self.assertIn("日本の技術です。", subtitles)

    def test_synthesize_audited_project_uses_final_queries_without_extra_line_gap(self) -> None:
        settings = VoicevoxSynthesisSettings(
            line_pause_seconds=1.0,
            output_sampling_rate=8000,
        )
        audited_items = [
            (
                "一行目です。",
                7,
                {
                    "accent_phrases": [],
                    "speedScale": 1.08,
                    "pitchScale": -0.01,
                    "intonationScale": 1.05,
                    "volumeScale": 1.0,
                    "pauseLengthScale": 0.95,
                    "prePhonemeLength": 0.08,
                    "postPhonemeLength": 0.16,
                    "outputSamplingRate": 8000,
                    "outputStereo": False,
                    "kana": "イチギョーメデス",
                },
            ),
            (
                "二行目です。",
                8,
                {
                    "accent_phrases": [],
                    "speedScale": 0.96,
                    "pitchScale": 0.0,
                    "intonationScale": 0.92,
                    "volumeScale": 1.0,
                    "pauseLengthScale": 1.1,
                    "prePhonemeLength": 0.1,
                    "postPhonemeLength": 0.24,
                    "outputSamplingRate": 8000,
                    "outputStereo": False,
                    "kana": "ニギョーメデス",
                },
            ),
        ]
        with tempfile.TemporaryDirectory(prefix="DocuRandomAuditedVoice_") as temp:
            output = Path(temp) / "audited.wav"
            result = synthesize_script(
                client=VoicevoxClient(self.endpoint),
                script="一行目です。\n二行目です。",
                style=VoicevoxStyle("Test Voice", "speaker-uuid", "Normal", 7),
                settings=settings,
                output_path=output,
                audited_items=audited_items,
                audited_project_path=Path(temp) / "audited.vvproj",
            )
            self.assertAlmostEqual(result.duration_seconds, 0.1, places=2)
            self.assertEqual(_VoicevoxHandler.queries[0]["speedScale"], 1.08)
            self.assertEqual(_VoicevoxHandler.queries[1]["speedScale"], 0.96)
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            self.assertTrue(metadata["audited_query_digest"])
            self.assertTrue(metadata["audited_project"].endswith("audited.vvproj"))
            self.assertIsNotNone(
                load_valid_synthesis_result(
                    output,
                    script="一行目です。\n二行目です。",
                    style=VoicevoxStyle("Test Voice", "speaker-uuid", "Normal", 7),
                    settings=settings,
                    audited_items=audited_items,
                )
            )
            self.assertIsNone(
                load_valid_synthesis_result(
                    output,
                    script="一行目です。\n二行目です。",
                    style=VoicevoxStyle("Test Voice", "speaker-uuid", "Normal", 7),
                    settings=settings,
                )
            )

    def test_pre_cancelled_synthesis_writes_nothing(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        with tempfile.TemporaryDirectory(prefix="DocuRandomVoicevoxCancel_") as temp:
            output = Path(temp) / "cancelled.wav"
            with self.assertRaises(VoicevoxCancelled):
                synthesize_script(
                    client=VoicevoxClient(self.endpoint),
                    script="Không được tạo",
                    style=VoicevoxStyle("Test", "uuid", "Normal", 7),
                    settings=VoicevoxSynthesisSettings(),
                    output_path=output,
                    cancel_event=cancelled,
                )
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".srt").exists())

    def test_running_http_synthesis_cancels_without_waiting_for_server(self) -> None:
        client = VoicevoxClient(self.endpoint)
        query = client.audio_query("slow", 7)
        cancelled = threading.Event()
        errors: list[BaseException] = []
        _VoicevoxHandler.slow_synthesis = True

        def run() -> None:
            try:
                client.synthesis(query, 7, cancel_event=cancelled)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(_VoicevoxHandler.synthesis_started.wait(timeout=1.0))
        started = time.monotonic()
        cancelled.set()
        worker.join(timeout=0.7)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 0.7)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], VoicevoxCancelled)

    def test_discovers_run_exe_from_voicevox_folder(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomEnginePath_") as temp:
            root = Path(temp)
            executable = root / "vv-engine" / "run.exe"
            executable.parent.mkdir()
            executable.write_bytes(b"placeholder")
            self.assertEqual(discover_engine_path(root), executable.resolve())

    def test_installs_official_vvpp_once_and_rediscovers_managed_engine(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomManagedEngine_") as temp:
            workspace = Path(temp)
            source = workspace / "source.vvpp"
            package = self._test_engine_package(source)
            install_root = workspace / "managed"
            events: list[tuple[float, str]] = []

            installed = install_official_engine(
                use_gpu=False,
                install_root=install_root,
                package=package,
                progress=lambda percent, message: events.append((percent, message)),
            )

            self.assertTrue(installed.is_file())
            self.assertEqual(installed.read_bytes(), b"test-engine")
            self.assertTrue(
                (install_root / "test-1.0" / "cpu" / ".docurandom-engine.json").is_file()
            )
            self.assertEqual(events[-1][0], 100.0)
            self.assertFalse(
                (install_root / "downloads" / package.filename).exists()
            )
            source.unlink()
            self.assertEqual(
                install_official_engine(
                    use_gpu=False,
                    install_root=install_root,
                    package=package,
                ),
                installed,
            )
            with patch(
                "docurandom.voicevox.managed_engine_root",
                return_value=install_root,
            ):
                self.assertEqual(discover_engine_path(), installed)

    def test_engine_install_honours_pre_cancel_without_writing(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        with tempfile.TemporaryDirectory(prefix="DocuRandomManagedCancel_") as temp:
            root = Path(temp)
            with self.assertRaises(VoicevoxCancelled):
                install_official_engine(
                    use_gpu=False,
                    cancel_event=cancelled,
                    install_root=root / "managed",
                    package=VoicevoxEnginePackage(
                        version="test",
                        variant="cpu",
                        filename="unused.vvpp",
                        url="https://example.invalid/unused.vvpp",
                        size=1,
                        sha256="0" * 64,
                    ),
                )
            self.assertFalse((root / "managed").exists())

    def test_engine_install_recovers_fully_downloaded_part_without_network(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomManagedResume_") as temp:
            workspace = Path(temp)
            source = workspace / "source.vvpp"
            package = self._test_engine_package(source)
            install_root = workspace / "managed"
            downloads = install_root / "downloads"
            downloads.mkdir(parents=True)
            part = downloads / f"{package.filename}.part"
            part.write_bytes(source.read_bytes())
            source.unlink()

            installed = install_official_engine(
                use_gpu=False,
                install_root=install_root,
                package=package,
            )

            self.assertTrue(installed.is_file())
            self.assertFalse(part.exists())

    def test_engine_install_rejects_archive_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomManagedUnsafe_") as temp:
            workspace = Path(temp)
            source = workspace / "unsafe.vvpp"
            package = self._test_engine_package(source, traversal=True)
            install_root = workspace / "managed"

            with self.assertRaisesRegex(Exception, "không an toàn"):
                install_official_engine(
                    use_gpu=False,
                    install_root=install_root,
                    package=package,
                )

            self.assertFalse((workspace / "escape.txt").exists())
            self.assertFalse((install_root / "test-1.0" / "cpu").exists())


if __name__ == "__main__":
    unittest.main()
