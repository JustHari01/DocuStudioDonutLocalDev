from __future__ import annotations

import tempfile
import shutil
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from docurandom.scanner import (
    _remove_exact_duplicates,
    _sha256_file,
    natural_key,
    probe_audio_duration,
    probe_media_file,
    scan_images,
    scan_media,
)


class _CompletedProbe:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int:
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return self.stdout, self.stderr

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class _BlockingProbe:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.started.set()
        if timeout is None:
            self.stopped.wait(5)
        elif not self.stopped.wait(timeout):
            raise subprocess.TimeoutExpired(["ffprobe"], timeout)
        return "", ""

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.stopped.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stopped.set()


class _CancelDuringRead:
    def __init__(self, cancel_event: threading.Event) -> None:
        self.cancel_event = cancel_event
        self.read_count = 0

    def __enter__(self) -> _CancelDuringRead:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        self.read_count += 1
        self.cancel_event.set()
        return b"hash this chunk"


class ScannerTests(unittest.TestCase):
    def test_natural_sort(self) -> None:
        names = ["10.jpg", "2.jpg", "ảnh 11.png", "Ảnh 3.png", "1.jpg"]
        self.assertEqual(
            sorted(names, key=natural_key),
            ["1.jpg", "2.jpg", "10.jpg", "Ảnh 3.png", "ảnh 11.png"],
        )

    def test_scan_orders_and_reports_corrupt_images(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomTest_") as temp:
            root = Path(temp) / "Ảnh có dấu"
            root.mkdir()
            for name, color in (
                ("10.jpg", "red"),
                ("2.png", "green"),
                ("1.jpg", "blue"),
            ):
                Image.new("RGB", (64, 48), color).save(root / name)
            (root / "3.jpg").write_bytes(b"not an image")
            (root / "ignore.txt").write_text("ignored", encoding="utf-8")

            images, warnings = scan_images(root)
            self.assertEqual([item.relative_path for item in images], ["1.jpg", "2.png", "10.jpg"])
            self.assertEqual(len(warnings), 1)
            self.assertIn("3.jpg", warnings[0])

    def test_recursive_scan_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomTest_") as temp:
            root = Path(temp)
            nested = root / "sub"
            nested.mkdir()
            Image.new("RGB", (32, 32), "white").save(nested / "1.jpg")
            self.assertEqual(scan_images(root, recursive=False)[0], [])
            self.assertEqual(len(scan_images(root, recursive=True)[0]), 1)

    def test_exact_duplicate_sources_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDuplicate_") as temp:
            root = Path(temp)
            original = root / "1.jpg"
            duplicate = root / "2.jpg"
            Image.new("RGB", (64, 48), "navy").save(original)
            shutil.copyfile(original, duplicate)
            media, warnings = scan_images(root)
            self.assertEqual([item.relative_path for item in media], ["1.jpg"])
            self.assertEqual(len(warnings), 1)
            self.assertIn("trùng hoàn toàn", warnings[0])

    def test_media_probe_keeps_backward_compatible_result_and_error(self) -> None:
        success = _CompletedProbe(
            '{"streams":['
            '{"codec_type":"video","duration":"2.5","width":1920,'
            '"height":1080,"avg_frame_rate":"30000/1001","codec_name":"h264"},'
            '{"codec_type":"audio"}],"format":{"duration":"2.5"}}'
        )
        with patch("docurandom.scanner.subprocess.Popen", return_value=success):
            info = probe_media_file(Path("clip.mp4"), Path("ffprobe.exe"))
        self.assertEqual(info["width"], 1920)
        self.assertEqual(info["height"], 1080)
        self.assertEqual(info["duration"], 2.5)
        self.assertAlmostEqual(float(info["fps"]), 30000 / 1001)
        self.assertEqual(info["codec_name"], "h264")
        self.assertTrue(info["has_audio"])

        failure = _CompletedProbe("", stderr="bad video", returncode=1)
        with patch("docurandom.scanner.subprocess.Popen", return_value=failure):
            with self.assertRaisesRegex(ValueError, "bad video"):
                probe_media_file(Path("broken.mp4"), Path("ffprobe.exe"))

    def test_scan_cancellation_terminates_active_video_probe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomScanCancel_") as temp:
            root = Path(temp)
            (root / "1.mp4").write_bytes(b"placeholder video")
            process = _BlockingProbe()
            cancelled = threading.Event()
            errors: list[BaseException] = []

            def scan() -> None:
                try:
                    scan_media(
                        root,
                        ffprobe_path=Path("ffprobe.exe"),
                        cancel_event=cancelled,
                    )
                except BaseException as exc:
                    errors.append(exc)

            with patch("docurandom.scanner.subprocess.Popen", return_value=process):
                worker = threading.Thread(target=scan, name="MediaScanCancelTest")
                worker.start()
                self.assertTrue(process.started.wait(2))
                cancelled.set()
                worker.join(1.5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], InterruptedError)
            self.assertTrue(process.terminated)
            self.assertIsNotNone(process.poll())

    def test_pre_cancelled_scan_and_alias_stop_immediately(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomScanPreCancel_") as temp:
            root = Path(temp)
            Image.new("RGB", (32, 32), "navy").save(root / "1.jpg")
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(InterruptedError):
                scan_media(root, cancel_event=cancelled)
            with self.assertRaises(InterruptedError):
                scan_images(root, cancel_event=cancelled)

    def test_hash_checks_cancel_event_between_chunks(self) -> None:
        cancelled = threading.Event()
        reader = _CancelDuringRead(cancelled)
        with patch.object(Path, "open", return_value=reader):
            with self.assertRaises(InterruptedError):
                _sha256_file(Path("large-media.bin"), cancelled)
        self.assertEqual(reader.read_count, 1)

    def test_dedup_does_not_swallow_hash_cancellation_as_os_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDedupCancel_") as temp:
            root = Path(temp)
            first = root / "1.jpg"
            second = root / "2.jpg"
            first.write_bytes(b"same size")
            second.write_bytes(b"different")
            cancelled = threading.Event()
            with patch(
                "docurandom.scanner._sha256_file",
                side_effect=InterruptedError("media scan cancelled"),
            ):
                with self.assertRaises(InterruptedError):
                    _remove_exact_duplicates([first, second], cancelled)

    def test_audio_probe_keeps_backward_compatible_result_and_error(self) -> None:
        success = _CompletedProbe(
            '{"streams":[{"duration":"3.25"}],"format":{"duration":"9.0"}}'
        )
        with patch("docurandom.scanner.subprocess.Popen", return_value=success) as popen:
            self.assertEqual(
                probe_audio_duration(Path("sound.wav"), Path("ffprobe.exe")),
                3.25,
            )
            popen.assert_called_once()

        failure = _CompletedProbe("", stderr="bad audio", returncode=1)
        with patch("docurandom.scanner.subprocess.Popen", return_value=failure):
            with self.assertRaisesRegex(ValueError, "bad audio"):
                probe_audio_duration(Path("broken.wav"), Path("ffprobe.exe"))

    def test_audio_probe_does_not_spawn_when_already_cancelled(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        with patch("docurandom.scanner.subprocess.Popen") as popen:
            with self.assertRaises(InterruptedError):
                probe_audio_duration(
                    Path("sound.wav"),
                    Path("ffprobe.exe"),
                    cancel_event=cancelled,
                )
            popen.assert_not_called()

    def test_audio_probe_cancellation_terminates_running_process(self) -> None:
        process = _BlockingProbe()
        cancelled = threading.Event()
        errors: list[BaseException] = []

        def probe() -> None:
            try:
                probe_audio_duration(
                    Path("slow.wav"),
                    Path("ffprobe.exe"),
                    cancel_event=cancelled,
                    timeout=30,
                )
            except BaseException as exc:
                errors.append(exc)

        with patch("docurandom.scanner.subprocess.Popen", return_value=process):
            worker = threading.Thread(target=probe, name="AudioProbeCancelTest")
            worker.start()
            self.assertTrue(process.started.wait(2))
            cancelled.set()
            worker.join(1.5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], InterruptedError)
        self.assertTrue(process.terminated)
        self.assertIsNotNone(process.poll())

    def test_audio_probe_timeout_reaps_process_and_preserves_exception(self) -> None:
        process = _BlockingProbe()
        with patch("docurandom.scanner.subprocess.Popen", return_value=process):
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                probe_audio_duration(
                    Path("slow.wav"),
                    Path("ffprobe.exe"),
                    timeout=0.03,
                )
        self.assertEqual(raised.exception.timeout, 0.03)
        self.assertTrue(process.terminated)
        self.assertIsNotNone(process.poll())


if __name__ == "__main__":
    unittest.main()
