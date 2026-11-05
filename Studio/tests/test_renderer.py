from __future__ import annotations

import tempfile
import math
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from docurandom import renderer as renderer_module
from docurandom.ffmpeg_manager import find_tools
from docurandom.models import MediaInfo, RenderSettings
from docurandom.planner import build_render_plan
from docurandom.renderer import (
    _filter_complex_file_args,
    _release_output_path,
    _unique_output_path,
    build_filter_graph,
    build_scene_filter_graph,
    normalize_image,
    normalize_output_name,
    numbered_output_name,
    render_video,
)
from docurandom.render_checkpoint import (
    render_checkpoint_dir,
    render_request_identity,
    save_render_plan_checkpoint,
)
from docurandom.runtime_profile import HostResources, RenderRuntimeProfile
from docurandom.scanner import probe_audio_duration, scan_images, scan_media


class RendererTests(unittest.TestCase):
    @staticmethod
    def _software_profile() -> RenderRuntimeProfile:
        return RenderRuntimeProfile(
            host=HostResources(
                logical_cpus=8,
                total_ram_bytes=16 * 1024**3,
                available_ram_bytes=12 * 1024**3,
            ),
            encoder="libx264",
            hardware_accelerated=False,
            recommended_jobs=2,
            threads_per_job=3,
            encoder_label="CPU / x264",
        )

    def test_filter_graph_contains_unique_finishing_effects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomGraph_") as temp:
            root = Path(temp)
            Image.new("RGB", (100, 80), "navy").save(root / "1.jpg")
            images, _ = scan_images(root)
            settings = RenderSettings(root, root, 640, 360, 24, 1.0, 1.0)
            plan = build_render_plan(
                images,
                settings,
                seed="1234567890abcdef1234567890abcdef",
            )
            graph = build_filter_graph(plan, settings)
            subtitle_graph = build_filter_graph(
                plan, settings, subtitle_filename="subtitles_render.ass"
            )
            scene_graph = build_scene_filter_graph(plan.scenes[0], settings)
            self.assertIn(f"all_seed={plan.grain_seed}", graph)
            self.assertIn("vignette=", graph)
            self.assertIn("zoompan=", scene_graph)
            self.assertIn("scale=845:475", scene_graph)
            self.assertIn("sin(", scene_graph)
            self.assertIn("cos(", scene_graph)
            self.assertIn("*(2*PI*t/", scene_graph)
            self.assertIn("split=2[bg][fg]", scene_graph)
            self.assertIn("overlay=(W-w)/2:(H-h)/2", scene_graph)
            self.assertLess(
                scene_graph.index("eq=brightness="),
                scene_graph.index("[fg2]"),
            )
            self.assertNotIn(
                "overlay=(W-w)/2:(H-h)/2:shortest=1,eq=brightness=",
                scene_graph,
            )
            self.assertIn("[vout]", graph)
            self.assertIn("subtitles=filename='subtitles_render.ass'", subtitle_graph)
            self.assertIn("drawbox=x=32:y=270:w=576:h=72", subtitle_graph)
            self.assertIn("BorderStyle=1", subtitle_graph)
            self.assertLess(subtitle_graph.index("noise="), subtitle_graph.index("drawbox="))
            self.assertLess(subtitle_graph.index("drawbox="), subtitle_graph.index("subtitles="))

    def test_filter_graph_composites_black_cta_and_translucent_logo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomOverlayGraph_") as temp:
            root = Path(temp)
            Image.new("RGB", (100, 80), "navy").save(root / "1.jpg")
            Image.new("RGBA", (120, 60), (255, 255, 255, 180)).save(root / "logo.png")
            (root / "cta.mp4").write_bytes(b"placeholder")
            media, _ = scan_images(root)
            settings = RenderSettings(
                source_dir=root,
                output_dir=root,
                width=640,
                height=360,
                fps=24,
                duration_min=1.0,
                duration_max=1.0,
                cta_enabled=True,
                cta_path=root / "cta.mp4",
                cta_key_mode="black",
                cta_x_percent=25.0,
                cta_y_percent=40.0,
                cta_width_percent=20.0,
                cta_start_seconds=1.5,
                logo_enabled=True,
                logo_path=root / "logo.png",
                logo_x_percent=95.0,
                logo_y_percent=5.0,
                logo_width_percent=12.0,
                logo_opacity=0.6,
            )
            plan = build_render_plan(
                media,
                settings,
                seed="1234567890abcdef1234567890abcdef",
            )

            graph = build_filter_graph(plan, settings)

            self.assertIn("colorkey=color=0x000000", graph)
            self.assertIn("setpts=PTS-STARTPTS+1.500000000/TB", graph)
            self.assertIn("x=(W-w)*0.250000000", graph)
            self.assertIn("colorchannelmixer=aa=0.600000000", graph)
            self.assertIn("[withlogo]setparams=range=tv,format=yuv420p[vout]", graph)

    def test_filter_graph_applies_custom_cta_and_end_logo_windows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomOverlayTiming_") as temp:
            root = Path(temp)
            source = root / "media"
            source.mkdir()
            Image.new("RGB", (100, 80), "navy").save(source / "1.jpg")
            (root / "cta.mp4").write_bytes(b"placeholder")
            Image.new("RGBA", (80, 80), "white").save(root / "logo.png")
            media, _ = scan_images(source)
            settings = RenderSettings(
                source_dir=source,
                output_dir=root,
                duration_min=20.0,
                duration_max=20.0,
                cta_enabled=True,
                cta_path=root / "cta.mp4",
                cta_timing_mode="custom",
                cta_start_seconds=4.0,
                cta_end_seconds=11.0,
                logo_enabled=True,
                logo_path=root / "logo.png",
                logo_timing_mode="end",
                logo_duration_seconds=3.0,
            )
            plan = build_render_plan(
                media,
                settings,
                seed="abcdefabcdefabcdefabcdefabcdefab",
            )

            graph = build_filter_graph(plan, settings)

            self.assertIn("setpts=PTS-STARTPTS+4.000000000/TB", graph)
            self.assertIn("enable='between(t,4.000000000,11.000000000)'", graph)
            self.assertIn("setpts=PTS-STARTPTS+17.000000000/TB", graph)
            self.assertIn("enable='between(t,17.000000000,20.000000000)'", graph)

    def test_image_and_video_foregrounds_keep_fixed_geometry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomFixedFrame_") as temp:
            root = Path(temp)
            settings = RenderSettings(
                source_dir=root,
                output_dir=root,
                width=640,
                height=360,
                fps=30,
                duration_min=5.0,
                duration_max=5.0,
                framing="blur",
            )
            media = [
                MediaInfo(root / "image.jpg", "image.jpg", 1920, 1080, "JPEG"),
                MediaInfo(
                    root / "video.mp4",
                    "video.mp4",
                    1920,
                    1080,
                    "MP4",
                    media_type="video",
                    duration_seconds=10.0,
                    fps=30.0,
                ),
            ]
            plan = build_render_plan(
                media,
                settings,
                seed="fedcfedcfedcfedcfedcfedcfedcfedc",
            )

            image_scene = next(scene for scene in plan.scenes if scene.media_type == "image")
            video_scene = next(scene for scene in plan.scenes if scene.media_type == "video")
            image_graph = build_scene_filter_graph(image_scene, settings)
            video_graph = build_scene_filter_graph(video_scene, settings)
            self.assertIn("zoompan=", image_graph)
            self.assertNotIn("zoompan=", video_graph)
            for graph in (image_graph, video_graph):
                self.assertIn("[bg2][fg2]overlay=(W-w)/2:(H-h)/2", graph)
                self.assertIn("scale=576:324", graph)

            fill_graph = build_scene_filter_graph(
                image_scene,
                RenderSettings(root, root, 640, 360, 30, 5.0, 5.0, framing="fill"),
            )
            self.assertIn("zoompan=", fill_graph)
            self.assertIn("scale=640:360", fill_graph)

    def test_complex_filter_file_uses_current_ffmpeg_option_syntax(self) -> None:
        args = _filter_complex_file_args(Path("scene.ffscript"))

        self.assertEqual(args, ["-/filter_complex", "scene.ffscript"])
        self.assertNotIn("-filter_complex_script", args)

    def test_optional_output_name_is_validated_numbered_and_never_overwritten(self) -> None:
        self.assertEqual(normalize_output_name("  Video_1.mp4  "), "Video_1")
        self.assertEqual(numbered_output_name("Video", 2, 3), "Video_2")
        self.assertEqual(numbered_output_name("", 1, 3), "")
        for invalid in ('bad:name', "CON", "folder/video", "name. "):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    normalize_output_name(invalid)

        with tempfile.TemporaryDirectory(prefix="DocuRandomOutputName_") as temp:
            root = Path(temp)
            settings = RenderSettings(root, root, output_name="Video")
            first = _unique_output_path(settings, "render-a")
            try:
                second = _unique_output_path(settings, "render-b")
                try:
                    self.assertEqual(first.name, "Video.mp4")
                    self.assertEqual(second.name, "Video_2.mp4")
                finally:
                    _release_output_path(second)
            finally:
                _release_output_path(first)

    def test_normalize_blur_and_fill(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomNormalize_") as temp:
            root = Path(temp)
            source = root / "portrait.png"
            Image.new("RGBA", (100, 300), (220, 50, 80, 180)).save(source)
            for framing in ("fill", "blur"):
                target = root / f"{framing}.jpg"
                normalize_image(source, target, (320, 180), framing)
                with Image.open(target) as rendered:
                    self.assertEqual(rendered.size, (320, 180))
                    self.assertEqual(rendered.mode, "RGB")

    @unittest.skipUnless(find_tools(), "FFmpeg integration test requires ffmpeg and ffprobe")
    def test_short_real_render(self) -> None:
        tools = find_tools()
        assert tools is not None
        with tempfile.TemporaryDirectory(prefix="DocuRandomRender_") as temp:
            root = Path(temp)
            source = root / "images"
            output = root / "output"
            source.mkdir()
            for index, color in enumerate(("navy", "maroon", "darkgreen"), start=1):
                Image.new("RGB", (480 + index * 20, 300), color).save(
                    source / f"{index}.jpg"
                )
            subtitle = root / "voice.srt"
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\n日本の技術です。\n",
                encoding="utf-8-sig",
            )
            images, _ = scan_images(source)
            settings = RenderSettings(
                source,
                output,
                320,
                180,
                24,
                1.0,
                1.0,
                "light",
                "fill",
                output_name="Video_1",
                subtitle_path=subtitle,
            )
            plan = build_render_plan(
                images,
                settings,
                seed="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            )
            # Prepared runtime objects are shared by concurrent jobs. The render
            # must neither download tools nor repeat the hardware smoke probe.
            with (
                patch("docurandom.renderer.ensure_tools") as ensure_mock,
                patch("docurandom.renderer.detect_runtime_profile") as detect_mock,
            ):
                result = render_video(
                    images,
                    settings,
                    plan,
                    tools=tools,
                    runtime_profile=self._software_profile(),
                )
            ensure_mock.assert_not_called()
            detect_mock.assert_not_called()
            self.assertTrue(result.video_path.is_file())
            self.assertEqual(result.video_path.name, "Video_1.mp4")
            self.assertGreater(result.video_path.stat().st_size, 1024)
            self.assertEqual(len(result.output_sha256), 64)
            self.assertEqual(len(result.visual_sha256), 64)
            self.assertAlmostEqual(result.duration, plan.expected_duration, delta=0.25)

    @unittest.skipUnless(find_tools(), "FFmpeg integration test requires ffmpeg and ffprobe")
    def test_short_real_render_with_auto_keyed_cta_and_logo(self) -> None:
        tools = find_tools()
        assert tools is not None
        with tempfile.TemporaryDirectory(prefix="DocuRandomOverlayRender_") as temp:
            root = Path(temp)
            source = root / "media"
            output = root / "output"
            source.mkdir()
            Image.new("RGB", (640, 360), "navy").save(source / "scene.jpg")
            logo = root / "logo.png"
            Image.new("RGBA", (120, 60), (255, 255, 255, 190)).save(logo)
            cta = root / "cta-green.mp4"
            subprocess.run(
                [
                    str(tools.ffmpeg),
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=0x00FF00:s=320x90:r=24:d=1",
                    "-vf",
                    "drawbox=x=70:y=20:w=180:h=50:color=white:t=fill",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(cta),
                ],
                check=True,
            )
            media, warnings = scan_media(source, ffprobe_path=tools.ffprobe)
            self.assertEqual(warnings, [])
            settings = RenderSettings(
                source_dir=source,
                output_dir=output,
                width=320,
                height=180,
                fps=24,
                duration_min=2.0,
                duration_max=2.0,
                randomize_order=False,
                output_name="Overlay_Test",
                cta_enabled=True,
                cta_path=cta,
                cta_key_mode="auto",
                cta_width_percent=30.0,
                cta_timing_mode="custom",
                cta_start_seconds=0.25,
                cta_end_seconds=1.5,
                logo_enabled=True,
                logo_path=logo,
                logo_width_percent=12.0,
                logo_opacity=0.65,
                logo_timing_mode="end",
                logo_duration_seconds=0.5,
            )
            plan = build_render_plan(
                media,
                settings,
                seed="facefacefacefacefacefacefaceface",
            )

            result = render_video(
                media,
                settings,
                plan,
                tools=tools,
                runtime_profile=self._software_profile(),
            )

            self.assertTrue(result.video_path.is_file())
            self.assertGreater(result.video_path.stat().st_size, 1024)
            self.assertAlmostEqual(result.duration, 2.0, delta=0.25)

    @unittest.skipUnless(find_tools(), "FFmpeg integration test requires ffmpeg and ffprobe")
    def test_failed_render_resumes_after_last_verified_scene(self) -> None:
        tools = find_tools()
        assert tools is not None
        with tempfile.TemporaryDirectory(prefix="DocuRandomResume_") as temp:
            root = Path(temp)
            source = root / "media"
            output = root / "output"
            source.mkdir()
            Image.new("RGB", (480, 300), "navy").save(source / "1.jpg")
            Image.new("RGB", (480, 300), "maroon").save(source / "2.jpg")
            media, warnings = scan_media(source, ffprobe_path=tools.ffprobe)
            self.assertEqual(warnings, [])
            settings = RenderSettings(
                source_dir=source,
                output_dir=output,
                width=320,
                height=180,
                fps=24,
                duration_min=1.0,
                duration_max=1.0,
                randomize_order=False,
                output_name="Resume_Test",
            )
            plan = build_render_plan(
                media,
                settings,
                seed="abcdefabcdefabcdefabcdefabcdefab",
            )
            identity = render_request_identity(media, settings)
            checkpoint = render_checkpoint_dir(media, settings)
            save_render_plan_checkpoint(
                checkpoint,
                request_identity=identity,
                plan=plan,
            )
            original_run = renderer_module._run_ffmpeg
            first_labels: list[str] = []

            def interrupt_second(*args, **kwargs):
                label = str(kwargs["label"])
                first_labels.append(label)
                if label == "media 2":
                    raise RuntimeError("simulated interruption")
                return original_run(*args, **kwargs)

            with patch(
                "docurandom.renderer._run_ffmpeg",
                side_effect=interrupt_second,
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    render_video(
                        media,
                        settings,
                        plan,
                        tools=tools,
                        runtime_profile=self._software_profile(),
                        checkpoint_dir=checkpoint,
                    )

            first_clip = checkpoint / "clip_0000.mp4"
            self.assertTrue(first_clip.is_file())
            self.assertTrue((checkpoint / "clip_0000.checkpoint.json").is_file())
            first_hash = first_clip.read_bytes()[:1024]
            resumed_labels: list[str] = []

            def record_resume(*args, **kwargs):
                resumed_labels.append(str(kwargs["label"]))
                return original_run(*args, **kwargs)

            with patch(
                "docurandom.renderer._run_ffmpeg",
                side_effect=record_resume,
            ):
                result = render_video(
                    media,
                    settings,
                    plan,
                    tools=tools,
                    runtime_profile=self._software_profile(),
                    checkpoint_dir=checkpoint,
                )

            self.assertTrue(result.video_path.is_file())
            self.assertIn("media 1", first_labels)
            self.assertNotIn("media 1", resumed_labels)
            self.assertIn("media 2", resumed_labels)
            self.assertIn("timeline cuối", resumed_labels)
            self.assertFalse(checkpoint.exists())
            self.assertTrue(first_hash)

    @unittest.skipUnless(find_tools(), "FFmpeg integration test requires ffmpeg and ffprobe")
    def test_mixed_image_video_image_render_matches_audio(self) -> None:
        tools = find_tools()
        assert tools is not None
        with tempfile.TemporaryDirectory(prefix="DocuRandomMixed_") as temp:
            root = Path(temp)
            source = root / "media"
            output = root / "output"
            source.mkdir()
            Image.new("RGB", (420, 280), "navy").save(source / "1.jpg")
            video_path = source / "2.mp4"
            subprocess.run(
                [
                    str(tools.ffmpeg),
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=24:duration=1.3",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(video_path),
                ],
                check=True,
                capture_output=True,
            )
            Image.new("RGB", (280, 420), "maroon").save(source / "3.png")
            audio_path = root / "soundtrack.wav"
            subprocess.run(
                [
                    str(tools.ffmpeg),
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=3.137",
                    "-c:a",
                    "pcm_s16le",
                    str(audio_path),
                ],
                check=True,
                capture_output=True,
            )

            media, warnings = scan_media(source, ffprobe_path=tools.ffprobe)
            self.assertEqual(warnings, [])
            self.assertEqual(
                [item.media_type for item in media],
                ["image", "video", "image"],
            )
            audio_duration = probe_audio_duration(audio_path, tools.ffprobe)
            settings = RenderSettings(
                source_dir=source,
                output_dir=output,
                width=320,
                height=180,
                fps=24,
                duration_min=1.0,
                duration_max=1.0,
                intensity="light",
                framing="fill",
                audio_path=audio_path,
                audio_duration=audio_duration,
                audio_fade_seconds=0.1,
                transition_pack="popular",
                effect_density="low",
                transition_min=0.20,
                transition_max=0.30,
                randomize_order=False,
            )
            plan = build_render_plan(
                media,
                settings,
                seed="12341234123412341234123412341234",
            )
            target_frames = math.ceil(audio_duration * settings.fps)
            self.assertEqual(
                sum(scene.duration_frames for scene in plan.scenes)
                - sum(item.duration_frames for item in plan.transitions),
                target_frames,
            )
            result = render_video(
                media,
                settings,
                plan,
                tools=tools,
                runtime_profile=self._software_profile(),
            )
            self.assertTrue(result.video_path.is_file())
            self.assertAlmostEqual(
                result.duration,
                target_frames / settings.fps,
                delta=0.18,
            )


if __name__ == "__main__":
    unittest.main()
