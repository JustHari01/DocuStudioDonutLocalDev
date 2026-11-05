from __future__ import annotations

import base64
import hashlib
import inspect
import json
import tempfile
import unittest
import zipfile
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from docurandom.updater import (
    UpdateAsset,
    UpdateError,
    UpdateRelease,
    VerifiedManifest,
    _PublicUpdateRedirectHandler,
    _safe_zip_member,
    consume_latest_install_result,
    create_update_plan,
    download_portable_update,
    find_reusable_staged_update,
    get_latest_release,
    launch_update_helper,
    prune_old_update_payloads,
    stage_portable_update,
    update_is_newer,
    verify_release_manifest,
)


class UpdaterTests(unittest.TestCase):
    def test_version_comparison_is_semantic(self) -> None:
        self.assertTrue(update_is_newer("2.10.0", "2.9.9"))
        self.assertFalse(update_is_newer("2.1.0", "2.1.0"))
        with self.assertRaises(UpdateError):
            update_is_newer("latest", "2.1.0")

    def test_manifest_requires_valid_ed25519_signature_and_exact_asset(self) -> None:
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes_raw()
        payload = (
            json.dumps(
                {
                    "schema": 1,
                    "version": "2.2.0",
                    "published_at": "2026-08-15T00:00:00+00:00",
                    "asset": {
                        "name": "DocuRandomStudio-portable.zip",
                        "size": 123,
                        "sha256": "a" * 64,
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        signature = private.sign(payload)
        release = UpdateRelease(
            version="2.2.0",
            tag="v2.2.0",
            notes="",
            published_at="",
            manifest=UpdateAsset(1, "update-manifest.json", "https://api.github.com/a", len(payload), ""),
            signature=UpdateAsset(2, "update-manifest.sig", "https://api.github.com/b", len(signature), ""),
            portable=UpdateAsset(3, "DocuRandomStudio-portable.zip", "https://api.github.com/c", 123, "a" * 64),
        )
        with (
            mock.patch("docurandom.updater.UPDATE_PUBLIC_KEY_B64", base64.b64encode(public).decode()),
            mock.patch("docurandom.updater._download_asset_bytes", side_effect=[payload, signature]),
        ):
            verified = verify_release_manifest(release)
        self.assertEqual(verified.version, "2.2.0")

        with (
            mock.patch("docurandom.updater.UPDATE_PUBLIC_KEY_B64", base64.b64encode(public).decode()),
            mock.patch("docurandom.updater._download_asset_bytes", side_effect=[payload + b"x", signature]),
        ):
            with self.assertRaisesRegex(UpdateError, "Chữ ký"):
                verify_release_manifest(release)

    def test_zip_traversal_and_symlink_are_rejected(self) -> None:
        with self.assertRaises(UpdateError):
            _safe_zip_member(zipfile.ZipInfo("../escape.exe"))
        link = zipfile.ZipInfo("DocuRandomStudio/link")
        link.external_attr = (0o120777 << 16)
        with self.assertRaises(UpdateError):
            _safe_zip_member(link)

    def test_staging_accepts_one_portable_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdateTest_") as temporary:
            root = Path(temporary)
            archive = root / "update.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("DocuRandomStudio/DocuRandomStudio.exe", b"exe")
                output.writestr("DocuRandomStudio/_internal/file.bin", b"data")
            with (
                mock.patch("docurandom.updater.update_root", return_value=root / "updates"),
                mock.patch("docurandom.updater.update_is_newer", return_value=True),
            ):
                staged = stage_portable_update(archive, "2.2.0")
            self.assertTrue((staged / "DocuRandomStudio.exe").is_file())

    def test_manifest_digest_matches_download_contract(self) -> None:
        payload = b"portable"
        verified = VerifiedManifest(
            version="2.2.0",
            asset_name="DocuRandomStudio-portable.zip",
            asset_size=len(payload),
            asset_sha256=hashlib.sha256(payload).hexdigest(),
            published_at="",
        )
        self.assertEqual(verified.asset_size, 8)

    def test_public_update_catalog_is_anonymous_and_selects_tool_assets(self) -> None:
        release_payload = {
            "tag_name": "docurandom-v2.2.7",
            "body": "Sửa tự cập nhật",
            "published_at": "2026-08-20T00:00:00Z",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "id": index,
                    "name": name,
                    "browser_download_url": f"https://github.com/example/releases/download/v2.2.7/{name}",
                    "url": (
                        "https://api.github.com/repos/JustHari01/DocuRandom-DN/"
                        f"releases/assets/{index}"
                    ),
                    "size": size,
                    "digest": digest,
                }
                for index, name, size, digest in (
                    (1, "update-manifest.json", 100, ""),
                    (2, "update-manifest.sig", 64, ""),
                    (3, "DocuRandomStudio-portable.zip", 123, "sha256:" + "a" * 64),
                )
            ],
        }
        refs_payload = [
            {"ref": "refs/tags/docurandom-v2.2.6"},
            {"ref": "refs/tags/donut-v9.9.9"},
            {"ref": "refs/tags/docurandom-v2.2.7"},
        ]

        class _Response:
            def __init__(self, body: object) -> None:
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return json.dumps(self.body).encode("utf-8")

        with mock.patch(
                "docurandom.updater._open_update_request",
                side_effect=[
                    _Response(refs_payload),
                    _Response(release_payload),
                ],
            ) as open_request:
            release = get_latest_release()
        self.assertEqual(release.version, "2.2.7")
        self.assertEqual(release.notes, "Sửa tự cập nhật")
        self.assertEqual(
            release.portable.api_url,
            "https://api.github.com/repos/JustHari01/DocuRandom-DN/releases/assets/3",
        )
        request = open_request.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))
        self.assertNotIn("Cookie", request.headers)
        self.assertIn("/releases/tags/docurandom-v2.2.7", request.full_url)

    def test_public_updater_does_not_accept_token_parameters(self) -> None:
        for function in (
            get_latest_release,
            verify_release_manifest,
            download_portable_update,
        ):
            parameters = inspect.signature(function).parameters
            self.assertNotIn("token", parameters)
            self.assertNotIn("client_id", parameters)

    def test_public_update_catalog_retries_transient_http_500(self) -> None:
        payload = {
            "tag_name": "v2.3.4",
            "body": "Updater tự phục hồi lỗi tạm thời",
            "published_at": "2026-08-21T00:00:00Z",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "id": index,
                    "name": name,
                    "browser_download_url": f"https://github.com/example/{name}",
                    "url": (
                        "https://api.github.com/repos/JustHari01/DocuRandom-DN/"
                        f"releases/assets/{index}"
                    ),
                    "size": size,
                }
                for index, name, size in (
                    (1, "update-manifest.json", 100),
                    (2, "update-manifest.sig", 64),
                    (3, "DocuRandomStudio-portable.zip", 123),
                )
            ],
        }
        payload["tag_name"] = "docurandom-v2.3.4"
        refs_payload = [{"ref": "refs/tags/docurandom-v2.3.4"}]

        class _Response:
            def __init__(self, body: object) -> None:
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read(self, _limit: int = -1) -> bytes:
                return json.dumps(self.body).encode("utf-8")

        transient = urllib.error.HTTPError(
            "https://api.github.com/releases/latest", 500, "Internal Server Error", {}, None
        )
        with (
            mock.patch(
                "docurandom.updater._open_update_request",
                side_effect=[
                    transient,
                    _Response(refs_payload),
                    _Response(payload),
                ],
            ) as open_request,
            mock.patch("docurandom.updater.time.sleep") as sleep,
        ):
            release = get_latest_release()
        self.assertEqual(release.version, "2.3.4")
        self.assertEqual(open_request.call_count, 3)
        sleep.assert_called_once_with(0.5)

    def test_public_update_catalog_requires_no_login_header(self) -> None:
        from docurandom.updater import _public_update_request

        request = _public_update_request(
            "https://api.github.com/repos/JustHari01/DocuRandom-DN/"
            "git/matching-refs/tags/docurandom-v"
        )
        self.assertIsNone(request.get_header("Authorization"))

    def test_public_asset_redirect_never_accepts_github_authorization(self) -> None:
        request = urllib.request.Request(
            "https://api.github.com/repos/JustHari01/DocuRandom-DN/releases/assets/1",
            headers={"Authorization": "Bearer secret"},
        )
        redirected = _PublicUpdateRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://release-assets.githubusercontent.com/private/signed-asset",
        )
        self.assertIsNotNone(redirected)
        assert redirected is not None
        self.assertIsNone(redirected.get_header("Authorization"))

        with self.assertRaisesRegex(UpdateError, "không an toàn"):
            _PublicUpdateRedirectHandler().redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://example.test/stolen",
            )

    def test_verified_cached_portable_is_reused_without_downloading_again(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdateCache_") as temporary:
            root = Path(temporary)
            payload = b"already-downloaded-portable"
            destination = root / "updates" / "2.2.4" / "DocuRandomStudio-portable.zip"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(payload)
            manifest = VerifiedManifest(
                version="2.2.4",
                asset_name="DocuRandomStudio-portable.zip",
                asset_size=len(payload),
                asset_sha256=hashlib.sha256(payload).hexdigest(),
                published_at="",
            )
            release = UpdateRelease(
                version="2.2.4",
                tag="v2.2.4",
                notes="",
                published_at="",
                manifest=UpdateAsset(1, "update-manifest.json", "https://api.github.com/a", 1, ""),
                signature=UpdateAsset(2, "update-manifest.sig", "https://api.github.com/b", 1, ""),
                portable=UpdateAsset(
                    3,
                    "DocuRandomStudio-portable.zip",
                    "https://api.github.com/c",
                    len(payload),
                    hashlib.sha256(payload).hexdigest(),
                ),
            )
            progress: list[tuple[int, int]] = []
            with (
                mock.patch("docurandom.updater.update_root", return_value=root / "updates"),
                mock.patch("docurandom.updater._open_update_request") as open_request,
            ):
                result = download_portable_update(
                    release,
                    manifest,
                    progress=lambda received, total: progress.append((received, total)),
                )
            self.assertEqual(result, destination)
            self.assertEqual(progress[-1], (len(payload), len(payload)))
            open_request.assert_not_called()

    def test_verified_staging_is_reused_without_extracting_again(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdateStage_") as temporary:
            root = Path(temporary) / "updates"
            version_root = root / "2.2.8"
            stage = version_root / "staged" / "DocuRandomStudio"
            stage.mkdir(parents=True)
            payload = b"signed-portable"
            (version_root / "DocuRandomStudio-portable.zip").write_bytes(payload)
            (stage / "DocuRandomStudio.exe").write_bytes(b"app")
            (stage / "DocuRandomUpdater.exe").write_bytes(b"updater")
            (stage / ".docurandom-version").write_text("2.2.8", encoding="ascii")
            manifest = VerifiedManifest(
                version="2.2.8",
                asset_name="DocuRandomStudio-portable.zip",
                asset_size=len(payload),
                asset_sha256=hashlib.sha256(payload).hexdigest(),
                published_at="",
            )
            (version_root / "staged-receipt.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "version": "2.2.8",
                        "asset_size": len(payload),
                        "asset_sha256": manifest.asset_sha256,
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch("docurandom.updater.update_root", return_value=root),
                mock.patch("docurandom.updater.update_is_newer", return_value=True),
            ):
                reusable = find_reusable_staged_update("2.2.8", manifest)
                staged = stage_portable_update(
                    version_root / "DocuRandomStudio-portable.zip",
                    "2.2.8",
                    manifest,
                )
            self.assertEqual(reusable, stage.resolve())
            self.assertEqual(staged, stage.resolve())

    def test_update_plan_uses_helper_from_signed_target_build(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdatePlan_") as temporary:
            root = Path(temporary)
            install = root / "Home" / "App"
            staged = root / "Home" / "Data" / "updates" / "2.2.9" / "staged" / "DocuRandomStudio"
            install.mkdir(parents=True)
            staged.mkdir(parents=True)
            current_executable = install / "DocuRandomStudio.exe"
            current_executable.write_bytes(b"old-app")
            (staged / "DocuRandomStudio.exe").write_bytes(b"new-app")
            (staged / "DocuRandomUpdater.exe").write_bytes(b"new-updater")
            (staged / ".docurandom-version").write_text("2.2.9", encoding="ascii")
            update_root_path = root / "Home" / "Data" / "updates"
            with (
                mock.patch("docurandom.updater.sys.frozen", True, create=True),
                mock.patch("docurandom.updater.sys.executable", str(current_executable)),
                mock.patch("docurandom.updater.update_root", return_value=update_root_path),
            ):
                plan_path, _token = create_update_plan(staged, "2.2.9")
            self.assertEqual(
                (plan_path.parent / "DocuRandomUpdater.exe").read_bytes(),
                b"new-updater",
            )
            plan_value = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertTrue(plan_value["source_version"])

    def test_terminal_install_result_is_reported_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdateResult_") as temporary:
            root = Path(temporary) / "updates"
            version_root = root / "2.4.0"
            version_root.mkdir(parents=True)
            result_path = version_root / "install-result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "status": "failed",
                        "target_version": "2.4.0",
                        "detail": "Windows đang khóa file runtime.",
                        "method": "in-place-fallback",
                        "updated_at": 123.0,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch("docurandom.updater.update_root", return_value=root):
                result = consume_latest_install_result()
                second = consume_latest_install_result()

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.target_version, "2.4.0")
            self.assertFalse(result_path.exists())
            self.assertIsNone(second)

    def test_launch_helper_carries_selected_home_explicitly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdateLaunch_") as temporary:
            root = Path(temporary)
            plan_path = root / "Data" / "updates" / "2.2.9" / "install-plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text("{}", encoding="utf-8")
            (plan_path.parent / "DocuRandomUpdater.exe").write_bytes(b"updater")
            with (
                mock.patch("docurandom.updater.configured_home_dir", return_value=root),
                mock.patch("docurandom.updater.subprocess.Popen") as popen,
            ):
                launch_update_helper(plan_path)
            self.assertEqual(popen.call_args.kwargs["env"]["DOCURANDOM_HOME"], str(root))

    def test_prune_removes_only_older_semver_payloads(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdatePrune_") as temporary:
            root = Path(temporary)
            for name in ("2.3.10", "2.3.13", "notes"):
                (root / name).mkdir()
            with mock.patch("docurandom.updater.update_root", return_value=root):
                prune_old_update_payloads("2.3.13")
            self.assertFalse((root / "2.3.10").exists())
            self.assertTrue((root / "2.3.13").exists())
            self.assertTrue((root / "notes").exists())

    def test_legacy_update_credential_is_deleted_and_never_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdateLegacy_") as temporary:
            root = Path(temporary)
            legacy = root / "github-update-token.dpapi"
            legacy.write_bytes(b"unused-private-credential")
            with mock.patch("docurandom.updater.update_root", return_value=root):
                from docurandom.updater import _purge_legacy_update_credential

                _purge_legacy_update_credential()
            self.assertFalse(legacy.exists())


if __name__ == "__main__":
    unittest.main()
