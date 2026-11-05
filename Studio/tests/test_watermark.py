from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from docurandom.models import MediaInfo
from docurandom.scanner import scan_media
from docurandom.watermark import (
    WatermarkRemovalCancelled,
    WatermarkRuntime,
    discover_watermark_runtime,
    prepare_image_watermarks,
)


BUILD_RELEASE = Path(__file__).resolve().parents[1] / "build_release.ps1"


FAKE_REMOVER = """\
from pathlib import Path
import json
import shutil
import sys

args = sys.argv[1:]
source = Path(args[args.index('remove') + 1])
destination = Path(args[args.index('--output') + 1])
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(source, destination)
print(json.dumps({'kind': 'image', 'output': str(destination)}))
"""


class WatermarkIntegrationTests(unittest.TestCase):
    @staticmethod
    def _runtime(root: Path, script: str = FAKE_REMOVER) -> WatermarkRuntime:
        cli = root / "fake_remover.py"
        cli.write_text(script, encoding="utf-8")
        return WatermarkRuntime(
            node_path=Path(sys.executable),
            cli_path=cli,
            root_path=root,
            version="test-1.0",
        )

    @staticmethod
    def _image_info(path: Path) -> MediaInfo:
        with Image.open(path) as image:
            width, height = image.size
        return MediaInfo(
            path=path,
            relative_path=path.name,
            width=width,
            height=height,
            format_name=path.suffix.lstrip(".").upper(),
        )

    def test_prepares_supported_images_without_overwriting_sources_and_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWatermark_") as temp:
            root = Path(temp)
            source = root / "source.png"
            unsupported = root / "source.bmp"
            Image.new("RGB", (64, 64), (40, 80, 120)).save(source)
            Image.new("RGB", (32, 32), (10, 20, 30)).save(unsupported)
            original_bytes = source.read_bytes()
            runtime = self._runtime(root)
            cache = root / "cache"
            media = [self._image_info(source), self._image_info(unsupported)]

            first = prepare_image_watermarks(media, runtime=runtime, cache_dir=cache)

            self.assertEqual(first.processed_count, 1)
            self.assertEqual(first.cache_hits, 0)
            self.assertEqual(first.skipped_count, 1)
            self.assertNotEqual(first.media[0].path, source)
            self.assertEqual(first.media[0].relative_path, source.name)
            self.assertEqual(first.media[1].path, unsupported)
            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertTrue(first.warnings)

            second = prepare_image_watermarks(media, runtime=runtime, cache_dir=cache)

            self.assertEqual(second.processed_count, 0)
            self.assertEqual(second.cache_hits, 1)
            self.assertEqual(second.media[0].path, first.media[0].path)

    def test_individual_failure_is_best_effort_and_keeps_original(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWatermarkFail_") as temp:
            root = Path(temp)
            source = root / "source.jpg"
            Image.new("RGB", (48, 48), (200, 210, 220)).save(source)
            runtime = self._runtime(root, "import sys\nsys.stderr.write('boom')\nsys.exit(4)\n")

            result = prepare_image_watermarks(
                [self._image_info(source)],
                runtime=runtime,
                cache_dir=root / "cache",
            )

            self.assertEqual(result.media[0].path, source)
            self.assertEqual(result.processed_count, 0)
            self.assertEqual(len(result.warnings), 1)
            self.assertIn("dùng ảnh gốc", result.warnings[0])

    def test_large_json_report_is_drained_without_pipe_deadlock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWatermarkReport_") as temp:
            root = Path(temp)
            source = root / "source.jpg"
            Image.new("RGB", (48, 48), (90, 100, 110)).save(source)
            script = """\
from pathlib import Path
import json
import shutil
import sys

args = sys.argv[1:]
source = Path(args[args.index('remove') + 1])
destination = Path(args[args.index('--output') + 1])
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(source, destination)
print(json.dumps({'kind': 'image', 'qualityReport': 'x' * 250000}))
"""

            result = prepare_image_watermarks(
                [self._image_info(source)],
                runtime=self._runtime(root, script),
                cache_dir=root / "cache",
            )

            self.assertEqual(result.processed_count, 1)
            self.assertTrue(result.media[0].path.is_file())

    def test_clean_media_is_visible_and_uses_shared_right_crop_template(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWatermarkCrop_") as temp:
            root = Path(temp)
            source = root / "Image_0022.jpg"
            Image.new("RGB", (1376, 768), (40, 50, 60)).save(source)
            clean_dir = root / "Media" / "_watermark_clean"

            result = prepare_image_watermarks(
                [self._image_info(source)],
                runtime=self._runtime(root),
                cache_dir=clean_dir,
            )

            cleaned = result.media[0].path
            self.assertTrue(cleaned.is_file())
            self.assertEqual(cleaned.parent, clean_dir)
            self.assertIn("__clean_", cleaned.name)
            with Image.open(cleaned) as image:
                self.assertEqual(image.size, (1256, 768))

    def test_flow_template_crop_does_not_require_watermark_engine(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWatermarkTemplateOnly_") as temp:
            root = Path(temp)
            source = root / "flow.jpg"
            Image.new("RGB", (1376, 768), (80, 90, 100)).save(source)
            clean_dir = root / "Media" / "_watermark_clean"

            result = prepare_image_watermarks(
                [self._image_info(source)],
                remove_watermark=False,
                crop_right_template=True,
                cache_dir=clean_dir,
            )

            cleaned = result.media[0].path
            self.assertTrue(cleaned.is_file())
            self.assertEqual(cleaned.parent, clean_dir)
            with Image.open(cleaned) as image:
                self.assertEqual(image.size, (1256, 768))

    def test_recursive_scan_ignores_visible_clean_media_cache(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWatermarkScan_") as temp:
            root = Path(temp)
            Image.new("RGB", (32, 32), "red").save(root / "source.jpg")
            clean = root / "_watermark_clean"
            clean.mkdir()
            Image.new("RGB", (32, 32), "blue").save(clean / "source__clean.jpg")

            media, warnings = scan_media(root, recursive=True)

            self.assertEqual([item.relative_path for item in media], ["source.jpg"])
            self.assertEqual(warnings, [])

    def test_hung_image_times_out_and_batch_keeps_original(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWatermarkTimeout_") as temp:
            root = Path(temp)
            source = root / "source.jpg"
            Image.new("RGB", (48, 48), (20, 30, 40)).save(source)
            runtime = self._runtime(root, "import time\ntime.sleep(30)\n")

            with patch(
                "docurandom.watermark.WATERMARK_IMAGE_TIMEOUT_SECONDS", 0.05
            ):
                result = prepare_image_watermarks(
                    [self._image_info(source)],
                    runtime=runtime,
                    cache_dir=root / "cache",
                )

            self.assertEqual(result.media[0].path, source)
            self.assertIn("quá 5 phút", result.warnings[0])

    def test_cancelled_batch_stops_before_starting_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWatermarkCancel_") as temp:
            root = Path(temp)
            source = root / "source.webp"
            Image.new("RGB", (48, 48), (1, 2, 3)).save(source)
            cancelled = threading.Event()
            cancelled.set()

            with self.assertRaises(WatermarkRemovalCancelled):
                prepare_image_watermarks(
                    [self._image_info(source)],
                    runtime=self._runtime(root),
                    cache_dir=root / "cache",
                    cancel_event=cancelled,
                )

    def test_runtime_discovery_honours_explicit_source_and_node(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWatermarkRuntime_") as temp:
            root = Path(temp) / "watermark-remover-main"
            (root / "bin").mkdir(parents=True)
            (root / "bin" / "gwr.mjs").write_text("", encoding="utf-8")
            (root / "node_modules" / "sharp").mkdir(parents=True)
            (root / "node_modules" / "sharp" / "package.json").write_text(
                "{}", encoding="utf-8"
            )
            (root / "package.json").write_text(
                '{"version": "9.8.7"}', encoding="utf-8"
            )
            node = Path(temp) / "node.exe"
            node.write_bytes(b"fake")

            with patch.dict(
                os.environ,
                {
                    "DOCURANDOM_WATERMARK_ROOT": str(root),
                    "DOCURANDOM_NODE_PATH": str(node),
                },
                clear=False,
            ):
                runtime = discover_watermark_runtime()

            self.assertEqual(runtime.root_path, root.resolve())
            self.assertEqual(runtime.node_path, node.resolve())
            self.assertEqual(runtime.version, "9.8.7")

    def test_portable_build_materializes_and_validates_sharp_runtime(self) -> None:
        source = BUILD_RELEASE.read_text(encoding="utf-8")

        self.assertIn("--config.node-linker=hoisted", source)
        self.assertIn("--config.package-import-method=copy", source)
        self.assertIn(
            'Join-Path $BuiltWatermarkRoot "node_modules\\sharp\\package.json"',
            source,
        )
        self.assertIn("import('sharp')", source)
        self.assertIn("$BuiltWatermarkNode $BuiltWatermarkCli --help", source)


if __name__ == "__main__":
    unittest.main()
