from __future__ import annotations

import os
import tempfile
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from docurandom.models import ImageInfo, RenderSettings
from docurandom.planner import build_render_plan
from docurandom.storage import (
    HOME_ENVIRONMENT,
    HistoryStore,
    app_data_dir,
    atomic_write_text,
    configure_managed_environment,
    configured_home_dir,
    default_workspaces_dir,
    migrate_legacy_user_data,
    previous_managed_data_dir,
)


class HistoryStoreTests(unittest.TestCase):
    def test_previous_managed_data_requires_valid_markers_and_registry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomPreviousHome_") as temp:
            profile = Path(temp)
            previous = profile / "Documents" / "DocuRandom"
            current = profile / "Documents" / "DocuRandom Studio"
            data = previous / "Data"
            data.mkdir(parents=True)
            (previous / ".docurandom-home").write_text(
                "DocuRandom Studio managed folder\n", encoding="utf-8"
            )
            (previous / ".docurandom-installed-version").write_text(
                "2.4.150\n", encoding="ascii"
            )
            (data / "workspaces.json").write_text(
                json.dumps({"workspaces": [{"workspace_id": "old"}]}),
                encoding="utf-8",
            )

            with mock.patch("docurandom.storage.Path.home", return_value=profile), mock.patch(
                "docurandom.storage.configured_home_dir", return_value=current
            ):
                self.assertEqual(previous_managed_data_dir(), data.resolve())

            with mock.patch("docurandom.storage.Path.home", return_value=profile), mock.patch(
                "docurandom.storage.configured_home_dir", return_value=previous
            ):
                self.assertIsNone(previous_managed_data_dir())

            (data / "workspaces.json").unlink()
            with mock.patch("docurandom.storage.Path.home", return_value=profile), mock.patch(
                "docurandom.storage.configured_home_dir", return_value=current
            ):
                self.assertIsNone(previous_managed_data_dir())

    def test_legacy_profile_and_registry_migrate_without_overwriting_new_data(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomMigration_") as temp:
            root = Path(temp)
            source = root / "legacy"
            target = root / "managed" / "Data"
            (source / "chat-bridge" / "profile").mkdir(parents=True)
            (source / "chat-bridge" / "profile" / "Cookies").write_text(
                "signed-in", encoding="utf-8"
            )
            source.mkdir(parents=True, exist_ok=True)
            (source / "workspaces.json").write_text("legacy", encoding="utf-8")
            (source / "secrets").mkdir(parents=True)
            (source / "secrets" / "telegram-bot.bin").write_bytes(b"dpapi")
            target.mkdir(parents=True)
            (target / "workspaces.json").write_text("new", encoding="utf-8")
            (target / ".legacy-data-migrated").write_text("old marker\n", encoding="ascii")

            with mock.patch(
                "docurandom.storage.legacy_app_data_dir", return_value=source
            ):
                migrate_legacy_user_data(target)

            self.assertEqual(
                (target / "workspaces.json").read_text(encoding="utf-8"), "new"
            )
            self.assertEqual(
                (target / "chat-bridge" / "profile" / "Cookies").read_text(
                    encoding="utf-8"
                ),
                "signed-in",
            )
            self.assertEqual(
                (target / "secrets" / "telegram-bot.bin").read_bytes(),
                b"dpapi",
            )
            self.assertTrue((target / ".legacy-data-migrated").is_file())

    def test_selected_home_routes_all_managed_data_and_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomHome_") as temp:
            selected = Path(temp) / "DocuRandom Studio"
            with mock.patch.dict(
                os.environ,
                {HOME_ENVIRONMENT: str(selected)},
                clear=True,
            ):
                self.assertEqual(configured_home_dir(), selected.resolve())
                self.assertEqual(app_data_dir(), selected.resolve() / "Data")
                self.assertEqual(
                    default_workspaces_dir(),
                    selected.resolve() / "Workspaces",
                )
                configure_managed_environment()
                temporary = selected.resolve() / "Data" / "Temp"
                self.assertEqual(Path(os.environ["TMP"]), temporary)
                self.assertEqual(Path(os.environ["TEMP"]), temporary)
                self.assertEqual(
                    Path(os.environ["FLOW_OUTPUT_DIR"]),
                    selected.resolve() / "Data" / "Flow",
                )
                self.assertEqual(
                    Path(os.environ["NPM_CONFIG_CACHE"]),
                    selected.resolve() / "Data" / "npm-cache",
                )
                self.assertTrue(temporary.is_dir())

    def test_atomic_write_text_creates_parent_and_replaces_content(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomText_") as temp:
            path = Path(temp) / "Prompts" / "MASTER_PROMPT_CURRENT.txt"
            atomic_write_text(path, "Master Prompt đầu tiên")
            atomic_write_text(path, "Master Prompt đã cập nhật")

            self.assertEqual(
                path.read_text(encoding="utf-8-sig"),
                "Master Prompt đã cập nhật",
            )
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_atomic_write_text_retries_windows_access_denied(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomTextRetry_") as temp:
            path = Path(temp) / "chatgpt" / "docurandom-chatgpt-web.config.toml"
            real_replace = os.replace
            attempts = {"count": 0}

            def flaky_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
                attempts["count"] += 1
                if attempts["count"] <= 2:
                    raise PermissionError(13, "Access is denied")
                real_replace(source, target)

            with mock.patch("docurandom.storage.os.replace", side_effect=flaky_replace):
                atomic_write_text(path, "model = \"chatgpt-web/high\"", encoding="utf-8")

            self.assertEqual(path.read_text(encoding="utf-8"), 'model = "chatgpt-web/high"')
            self.assertEqual(attempts["count"], 3)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_visual_identity_and_grain_seed_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomHistory_") as temp:
            root = Path(temp)
            image = ImageInfo(root / "1.jpg", "1.jpg", 320, 180, "JPEG")
            settings = RenderSettings(root, root, 320, 180, 24, 1.0, 1.0)
            plan = build_render_plan(
                [image],
                settings,
                seed="13571357135713571357135713571357",
            )
            plan.output_sha256 = "a" * 64
            plan.visual_sha256 = "b" * 64
            history = HistoryStore(root / "history.json")
            history.add(plan)

            self.assertIn(plan.seed, history.used_seeds())
            self.assertIn(plan.grain_seed, history.used_grain_seeds())
            self.assertTrue(history.contains_signature(plan.manifest_signature))
            self.assertTrue(history.contains_output_hash(plan.output_sha256))
            self.assertTrue(history.contains_visual_hash(plan.visual_sha256))

    def test_parallel_unique_commits_do_not_lose_history(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomHistoryParallel_") as temp:
            root = Path(temp)
            image = ImageInfo(root / "1.jpg", "1.jpg", 320, 180, "JPEG")
            settings = RenderSettings(root, root, 320, 180, 24, 1.0, 1.0)
            history = HistoryStore(root / "history.json")
            plans = []
            for index in range(20):
                plan = build_render_plan(
                    [image],
                    settings,
                    seed=f"{index + 1:032x}",
                )
                plan.output_sha256 = f"{index + 101:064x}"
                plan.visual_sha256 = f"{index + 1001:064x}"
                plans.append(plan)

            with ThreadPoolExecutor(max_workers=8) as executor:
                outcomes = list(executor.map(history.try_add_unique, plans))
            self.assertTrue(all(accepted for accepted, _reason in outcomes))
            data = json.loads((root / "history.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data["renders"]), 20)
            accepted, reason = history.try_add_unique(plans[0])
            self.assertFalse(accepted)
            self.assertEqual(reason, "seed")

    def test_separate_store_instances_share_one_file_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomHistoryInstances_") as temp:
            root = Path(temp)
            path = root / "history.json"
            image = ImageInfo(root / "1.jpg", "1.jpg", 320, 180, "JPEG")
            settings = RenderSettings(root, root, 320, 180, 24, 1.0, 1.0)
            plans = []
            for index in range(24):
                plan = build_render_plan(
                    [image],
                    settings,
                    seed=f"{index + 500:032x}",
                )
                plan.output_sha256 = f"{index + 5000:064x}"
                plan.visual_sha256 = f"{index + 9000:064x}"
                plans.append(plan)

            stores = [HistoryStore(path) for _ in plans]
            with ThreadPoolExecutor(max_workers=12) as executor:
                futures = [
                    executor.submit(store.try_add_unique, plan)
                    for store, plan in zip(stores, plans, strict=True)
                ]
                outcomes = [future.result(5) for future in futures]

            self.assertTrue(all(accepted for accepted, _reason in outcomes))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["renders"]), len(plans))

    def test_duplicate_commit_is_atomic_across_store_instances(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomHistoryDuplicate_") as temp:
            root = Path(temp)
            path = root / "history.json"
            image = ImageInfo(root / "1.jpg", "1.jpg", 320, 180, "JPEG")
            settings = RenderSettings(root, root, 320, 180, 24, 1.0, 1.0)
            plan = build_render_plan(
                [image],
                settings,
                seed="abababababababababababababababab",
            )
            plan.output_sha256 = "c" * 64
            plan.visual_sha256 = "d" * 64
            stores = [HistoryStore(path) for _ in range(8)]
            start = threading.Barrier(len(stores))

            def commit(store: HistoryStore) -> tuple[bool, str]:
                start.wait(timeout=5)
                return store.try_add_unique(plan)

            with ThreadPoolExecutor(max_workers=len(stores)) as executor:
                outcomes = list(executor.map(commit, stores))

            self.assertEqual(sum(accepted for accepted, _reason in outcomes), 1)
            self.assertTrue(
                all(reason == "seed" for accepted, reason in outcomes if not accepted)
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["renders"]), 1)


if __name__ == "__main__":
    unittest.main()
