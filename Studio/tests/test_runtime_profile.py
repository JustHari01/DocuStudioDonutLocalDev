from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from docurandom.runtime_profile import (
    GIB,
    HostResources,
    RenderRuntimeProfile,
    choose_h264_encoder,
    detect_host_resources,
    encoder_args,
    load_global_chatgpt_limit,
    load_global_chatgpt_serial,
    load_global_render_limit,
    parse_ffmpeg_encoders,
    recommend_parallelism,
    recommend_chatgpt_parallelism,
    recommend_youtube_upload_parallelism,
    recommend_voicevox_parallelism,
    save_global_chatgpt_limit,
    save_global_chatgpt_serial,
    save_global_render_limit,
)


class RuntimeProfileTests(unittest.TestCase):
    def test_global_chatgpt_queue_defaults_to_serial_and_persists(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomGlobalChatGPT_") as temp:
            path = Path(temp) / "global_chatgpt_queue.json"
            self.assertTrue(load_global_chatgpt_serial(path=path))
            self.assertFalse(save_global_chatgpt_serial(False, path=path))
            self.assertFalse(load_global_chatgpt_serial(path=path))
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(stored["serial_workspaces"])
            path.write_text('{"serial_workspaces": "false"}', encoding="utf-8")
            self.assertTrue(load_global_chatgpt_serial(path=path))

    def test_global_render_limit_persists_outside_workspace_and_clamps(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomGlobalRender_") as temp:
            path = Path(temp) / "global_render_limit.json"
            self.assertEqual(load_global_render_limit(3, path=path), 3)
            self.assertEqual(save_global_render_limit(2, path=path), 2)
            self.assertEqual(load_global_render_limit(4, path=path), 2)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["parallel_videos"], 2)
            path.write_text('{"parallel_videos": 99}', encoding="utf-8")
            self.assertEqual(load_global_render_limit(1, path=path), 12)
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(load_global_render_limit(2, path=path), 2)
            path.write_text('{"parallel_videos": null}', encoding="utf-8")
            self.assertEqual(load_global_render_limit(2, path=path), 2)

    def test_global_chatgpt_limit_persists_and_reads_legacy_boolean(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomGlobalChatGPTLimit_") as temp:
            path = Path(temp) / "global_chatgpt_queue.json"
            self.assertEqual(load_global_chatgpt_limit(path=path), 1)
            self.assertEqual(save_global_chatgpt_limit(6, path=path), 6)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["parallel_sessions"], 6)
            self.assertFalse(stored["serial_workspaces"])
            self.assertEqual(load_global_chatgpt_limit(path=path), 6)
            path.write_text('{"serial_workspaces": true}', encoding="utf-8")
            self.assertEqual(load_global_chatgpt_limit(4, path=path), 1)
            path.write_text('{"serial_workspaces": false}', encoding="utf-8")
            self.assertEqual(load_global_chatgpt_limit(4, path=path), 4)
            path.write_text('{"parallel_sessions": 99}', encoding="utf-8")
            self.assertEqual(load_global_chatgpt_limit(path=path), 20)

    def test_parse_ffmpeg_encoder_table(self) -> None:
        output = """
Encoders:
 V..... = Video
 A..... = Audio
 ------
 V....D h264_nvenc          NVIDIA NVENC H.264 encoder
 V..... h264_qsv            Intel Quick Sync H.264 encoder
 VF...D libx264             libx264 H.264 encoder
 A....D aac                 AAC encoder
"""
        self.assertEqual(
            parse_ffmpeg_encoders(output),
            {"h264_nvenc", "h264_qsv", "libx264", "aac"},
        )

    def test_encoder_selection_uses_priority_and_real_probe(self) -> None:
        attempts: list[str] = []

        def probe(_path: object, encoder: str) -> bool:
            attempts.append(encoder)
            return encoder == "h264_amf"

        encoder, hardware = choose_h264_encoder(
            "ffmpeg",
            {"h264_nvenc", "h264_amf", "h264_qsv", "libx264"},
            probe,
        )
        self.assertEqual(encoder, "h264_amf")
        self.assertTrue(hardware)
        self.assertEqual(attempts, ["h264_nvenc", "h264_amf"])

    def test_encoder_selection_falls_back_to_x264(self) -> None:
        encoder, hardware = choose_h264_encoder(
            "ffmpeg",
            {"h264_nvenc", "libx264"},
            lambda _path, _encoder: False,
        )
        self.assertEqual((encoder, hardware), ("libx264", False))

    def test_22_gib_machine_reserves_memory_and_uses_one_render(self) -> None:
        resources = HostResources(
            logical_cpus=8,
            total_ram_bytes=22 * GIB,
            available_ram_bytes=11 * GIB,
        )
        self.assertEqual(recommend_parallelism(resources), (1, 4))

    def test_low_available_ram_forces_one_job(self) -> None:
        resources = HostResources(
            logical_cpus=32,
            total_ram_bytes=64 * GIB,
            available_ram_bytes=7 * GIB,
        )
        jobs, threads = recommend_parallelism(resources)
        self.assertEqual(jobs, 1)
        self.assertLessEqual(jobs * threads, resources.logical_cpus)

    def test_minecraft_mode_protects_server_tick_budget(self) -> None:
        resources = HostResources(
            logical_cpus=32,
            total_ram_bytes=64 * GIB,
            available_ram_bytes=48 * GIB,
            minecraft_running=True,
            java_process_count=1,
        )
        self.assertEqual(recommend_parallelism(resources), (1, 4))
        self.assertEqual(recommend_voicevox_parallelism(resources), 1)
        self.assertEqual(recommend_youtube_upload_parallelism(resources), 1)
        self.assertEqual(recommend_chatgpt_parallelism(resources), 1)

    def test_pagefile_pressure_reduces_all_heavy_lanes(self) -> None:
        resources = HostResources(
            logical_cpus=32,
            total_ram_bytes=64 * GIB,
            available_ram_bytes=24 * GIB,
            pagefile_total_bytes=64 * GIB,
            pagefile_available_bytes=2 * GIB,
        )
        self.assertTrue(resources.pagefile_pressure)
        self.assertTrue(resources.memory_pressure)
        self.assertEqual(recommend_parallelism(resources), (1, 4))
        self.assertEqual(recommend_voicevox_parallelism(resources), 1)
        self.assertEqual(recommend_youtube_upload_parallelism(resources), 1)
        self.assertEqual(recommend_chatgpt_parallelism(resources), 1)

    def test_voicevox_parallelism_scales_without_unbounded_fanout(self) -> None:
        self.assertEqual(
            recommend_voicevox_parallelism(HostResources(4, 8 * GIB, 5 * GIB)),
            1,
        )
        self.assertEqual(
            recommend_voicevox_parallelism(HostResources(8, 16 * GIB, 8 * GIB)),
            2,
        )
        self.assertEqual(
            recommend_voicevox_parallelism(HostResources(24, 32 * GIB, 20 * GIB)),
            2,
        )

    def test_youtube_upload_parallelism_is_one_for_every_host(self) -> None:
        for resources in (
            HostResources(4, 8 * GIB, 5 * GIB),
            HostResources(10, 24 * GIB, 15 * GIB),
            HostResources(32, 64 * GIB, 48 * GIB),
            HostResources(32, 64 * GIB, 7 * GIB, minecraft_running=True),
        ):
            with self.subTest(resources=resources):
                self.assertEqual(recommend_youtube_upload_parallelism(resources), 1)

    def test_chatgpt_parallelism_caps_account_pressure_at_three(self) -> None:
        self.assertEqual(
            recommend_chatgpt_parallelism(HostResources(2, 8 * GIB, 4 * GIB)),
            1,
        )
        self.assertEqual(
            recommend_chatgpt_parallelism(HostResources(6, 16 * GIB, 8 * GIB)),
            2,
        )
        self.assertEqual(
            recommend_chatgpt_parallelism(HostResources(10, 24 * GIB, 15 * GIB)),
            2,
        )
        self.assertEqual(
            recommend_chatgpt_parallelism(HostResources(14, 32 * GIB, 21 * GIB)),
            3,
        )
        self.assertEqual(
            recommend_chatgpt_parallelism(HostResources(18, 40 * GIB, 28 * GIB)),
            3,
        )
        self.assertEqual(
            recommend_chatgpt_parallelism(HostResources(22, 48 * GIB, 36 * GIB)),
            3,
        )
        self.assertEqual(
            recommend_chatgpt_parallelism(HostResources(32, 64 * GIB, 48 * GIB)),
            3,
        )

    def test_encoder_arguments_preserve_quality_and_thread_cap(self) -> None:
        host = HostResources(8, 22 * GIB, 11 * GIB)
        software = RenderRuntimeProfile(host, "libx264", False, 2, 3, "CPU / x264")
        scene = encoder_args(software, "scene")
        final = encoder_args(software, "final")
        self.assertIn("libx264", final)
        self.assertEqual(final[final.index("-threads") + 1], "3")
        self.assertEqual(scene[scene.index("-crf") + 1], "12")
        self.assertEqual(final[final.index("-crf") + 1], "17")
        self.assertIn("+faststart", final)

        hardware = RenderRuntimeProfile(host, "h264_nvenc", True, 2, 3, "NVIDIA NVENC")
        nvenc = encoder_args(hardware, "final")
        self.assertIn("h264_nvenc", nvenc)
        self.assertIn("p6", nvenc)
        self.assertNotIn("-threads", nvenc)

    def test_host_detection_never_returns_zero_capacity(self) -> None:
        resources = detect_host_resources()
        self.assertGreaterEqual(resources.logical_cpus, 1)
        self.assertGreater(resources.total_ram_bytes, 0)
        self.assertGreater(resources.available_ram_bytes, 0)
        self.assertLessEqual(resources.available_ram_bytes, resources.total_ram_bytes)


if __name__ == "__main__":
    unittest.main()
