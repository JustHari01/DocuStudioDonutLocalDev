from __future__ import annotations

import tempfile
import math
import unittest
from pathlib import Path

from docurandom.models import ImageInfo, MediaInfo, RenderSettings
from docurandom.planner import TRANSITION_PACKS, TRANSITION_TO_FAMILY, build_render_plan


def make_images(count: int, root: Path) -> list[ImageInfo]:
    return [
        ImageInfo(
            path=root / f"{index + 1}.jpg",
            relative_path=f"{index + 1}.jpg",
            width=1920,
            height=1080,
            format_name="JPEG",
        )
        for index in range(count)
    ]


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="DocuRandomPlan_")
        root = Path(self.temp.name)
        self.settings = RenderSettings(
            source_dir=root,
            output_dir=root,
            width=1280,
            height=720,
            fps=30,
            duration_min=3.0,
            duration_max=5.0,
            intensity="medium",
            framing="fill",
        )
        self.images = make_images(12, root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_documentary_transition_pack_is_broad_but_excludes_jarring_families(self) -> None:
        transitions = TRANSITION_PACKS["documentary"]

        self.assertEqual(len(transitions), 42)
        self.assertNotIn("pixelize", transitions)
        self.assertNotIn("squeezeh", transitions)
        self.assertNotIn("hlwind", transitions)
        self.assertNotIn("hlslice", transitions)

    def test_same_seed_reproduces_effect_values(self) -> None:
        seed = "00112233445566778899aabbccddeeff"
        first = build_render_plan(self.images, self.settings, seed=seed)
        second = build_render_plan(self.images, self.settings, seed=seed)
        self.assertEqual(first.seed, second.seed)
        self.assertEqual(first.style, second.style)
        self.assertEqual(first.scenes, second.scenes)
        self.assertEqual(first.transitions, second.transitions)
        self.assertEqual(first.grain_seed, second.grain_seed)
        self.assertEqual(first.manifest_signature, second.manifest_signature)
        self.assertNotEqual(first.render_id, second.render_id)

    def test_colour_grades_are_randomized_per_scene_without_adjacent_repeats(self) -> None:
        plan = build_render_plan(
            self.images,
            self.settings,
            seed="1234567890abcdef1234567890abcdef",
        )
        styles = [scene.color_style for scene in plan.scenes]

        self.assertEqual(plan.style, "Ngẫu nhiên từng cảnh")
        self.assertGreater(len(set(styles)), 1)
        self.assertTrue(all(left != right for left, right in zip(styles, styles[1:])))

    def test_different_seed_changes_visual_recipe(self) -> None:
        first = build_render_plan(
            self.images,
            self.settings,
            seed="00000000000000000000000000000001",
        )
        second = build_render_plan(
            self.images,
            self.settings,
            seed="00000000000000000000000000000002",
        )
        self.assertNotEqual(first.grain_seed, second.grain_seed)
        self.assertNotEqual(first.scenes, second.scenes)
        self.assertNotEqual(first.manifest_signature, second.manifest_signature)

    def test_no_adjacent_transition_or_motion_repeats(self) -> None:
        plan = build_render_plan(
            self.images,
            self.settings,
            seed="abcdefabcdefabcdefabcdefabcdefab",
        )
        transition_names = [item.name for item in plan.transitions]
        motion_names = [item.motion for item in plan.scenes]
        self.assertTrue(all(a != b for a, b in zip(transition_names, transition_names[1:])))
        self.assertTrue(all(a != b for a, b in zip(motion_names, motion_names[1:])))
        transition_families = [TRANSITION_TO_FAMILY[name] for name in transition_names]
        self.assertTrue(
            all(
                family not in transition_families[max(0, index - 2) : index]
                for index, family in enumerate(transition_families)
            )
        )

    def test_duration_is_frame_accurate(self) -> None:
        plan = build_render_plan(
            self.images,
            self.settings,
            seed="11111111111111111111111111111111",
        )
        expected_frames = sum(item.duration_frames for item in plan.scenes) - sum(
            item.duration_frames for item in plan.transitions
        )
        self.assertAlmostEqual(plan.expected_duration, expected_frames / self.settings.fps, places=5)
        for transition in plan.transitions:
            self.assertEqual(
                transition.duration_seconds,
                round(transition.duration_frames / self.settings.fps, 6),
            )

    def test_audio_master_is_exact_and_mixed_order_changes_by_seed(self) -> None:
        root = Path(self.temp.name)
        media: list[MediaInfo] = []
        for index in range(10):
            kind = "video" if index in {1, 4, 5, 8} else "image"
            suffix = ".mp4" if kind == "video" else ".jpg"
            media.append(
                MediaInfo(
                    path=root / f"{index + 1}{suffix}",
                    relative_path=f"{index + 1}{suffix}",
                    width=1920,
                    height=1080,
                    format_name=suffix[1:].upper(),
                    media_type=kind,
                    duration_seconds=4.0 if kind == "video" else 0.0,
                    fps=30.0 if kind == "video" else 0.0,
                )
            )
        settings = RenderSettings(
            source_dir=root,
            output_dir=root,
            fps=30,
            audio_path=root / "audio.wav",
            audio_duration=8.137,
            randomize_order=True,
        )
        plans = [
            build_render_plan(media, settings, seed=f"{seed:032x}")
            for seed in (1, 2, 3)
        ]
        target_frames = math.ceil(settings.audio_duration * settings.fps)
        original_paths = {item.relative_path for item in media}
        orders: list[tuple[str, ...]] = []
        for plan in plans:
            self.assertEqual(
                sum(item.duration_frames for item in plan.scenes)
                - sum(item.duration_frames for item in plan.transitions),
                target_frames,
            )
            order = tuple(item.relative_path for item in plan.scenes)
            self.assertEqual(set(order), original_paths)
            orders.append(order)
        self.assertGreater(len(set(orders)), 1)


if __name__ == "__main__":
    unittest.main()
