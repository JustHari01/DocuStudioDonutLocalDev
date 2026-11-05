from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from docurandom.donut_manager import (
    DONUT_VAULT_PASSWORD,
    DonutInstall,
    DonutManagedError,
    DonutManager,
    bundled_installer,
    bundled_release,
    bundled_source,
    parse_installer_releases,
    parse_release_payload,
)


def _catalog_release(version: str, revision: int, *, size: int = 123) -> dict[str, object]:
    return {
        "tag_name": f"donut-local-v{version}-r{revision}",
        "draft": False,
        "prerelease": False,
        "body": f"Donut Local {version} r{revision}",
        "assets": [
            {
                "name": f"DonutLocal_{version}_r{revision}_x64-portable.zip",
                "url": "https://api.github.com/assets/1",
                "browser_download_url": "https://example.test/donut-local.zip",
                "size": size,
            },
            {
                "name": "donut-local-manifest.json",
                "url": "https://api.github.com/assets/2",
                "browser_download_url": "https://example.test/manifest.json",
                "size": 512,
            },
            {
                "name": "donut-local-manifest.sig",
                "url": "https://api.github.com/assets/3",
                "browser_download_url": "https://example.test/manifest.sig",
                "size": 64,
            },
        ],
    }


def _signed_package(root: Path, *, traversal: bool = False) -> tuple[Path, str]:
    version = "0.29.6"
    revision = 1
    archive = root / f"DonutLocal_{version}_r{revision}_x64-portable.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        if traversal:
            bundle.writestr("../outside.txt", "bad")
        bundle.writestr("Donut-Local/DonutLocal.exe", "binary")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {
        "schema": 1,
        "component": "donut-local",
        "version": version,
        "revision": revision,
        "local_version": f"{version}.{revision}",
        "upstream_tag": f"v{version}",
        "asset": {"name": archive.name, "size": archive.stat().st_size, "sha256": digest},
        "entitlement_policy": "upstream-real-account",
    }
    payload = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    private_key = Ed25519PrivateKey.generate()
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    (root / "donut-local-manifest.json").write_bytes(payload)
    (root / "donut-local-manifest.sig").write_bytes(private_key.sign(payload))
    return archive, base64.b64encode(public_raw).decode("ascii")


class DonutManagerTests(unittest.TestCase):
    def test_public_installer_catalog_selects_newest_stable_github_digest(self) -> None:
        def release(version: str, *, prerelease: bool = False) -> dict[str, object]:
            name = f"DocuRandom-DN_{version}_x64-setup.exe"
            return {
                "tag_name": f"v{version}",
                "draft": False,
                "prerelease": prerelease,
                "body": f"Donut Local {version}",
                "assets": [
                    {
                        "state": "uploaded",
                        "name": name,
                        "size": 25_000_000,
                        "digest": "sha256:" + version.replace(".", "")[-1] * 64,
                        "url": (
                            "https://api.github.com/repos/JustHari01/DocuRandom-DN/"
                            f"releases/assets/{version.replace('.', '')}"
                        ),
                    }
                ],
            }

        selected = parse_installer_releases(
            json.dumps(
                [
                    release("0.29.6"),
                    release("0.29.7"),
                    release("9.0.0", prerelease=True),
                ]
            )
        )
        self.assertEqual(selected.version, "0.29.7")
        self.assertEqual(selected.asset_name, "DocuRandom-DN_0.29.7_x64-setup.exe")
        self.assertEqual(selected.sha256, "7" * 64)

    def test_public_installer_catalog_rejects_missing_github_digest(self) -> None:
        payload = [
            {
                "tag_name": "v0.29.7",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "state": "uploaded",
                        "name": "DocuRandom-DN_0.29.7_x64-setup.exe",
                        "size": 25_000_000,
                        "digest": None,
                        "url": (
                            "https://api.github.com/repos/JustHari01/DocuRandom-DN/"
                            "releases/assets/297"
                        ),
                    }
                ],
            }
        ]
        with self.assertRaises(DonutManagedError):
            parse_installer_releases(json.dumps(payload))

    def test_bundled_nsis_installer_requires_exact_manifest_and_hash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutNsis_") as temp:
            root = Path(temp)
            setup = root / "DocuRandom-DN_0.29.6_x64-setup.exe"
            setup.write_bytes(b"verified-nsis")
            digest = hashlib.sha256(setup.read_bytes()).hexdigest()
            (root / "donut-installer.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "component": "donut-local-installer",
                        "version": "0.29.6",
                        "asset": {
                            "name": setup.name,
                            "size": setup.stat().st_size,
                            "sha256": digest,
                        },
                    }
                ),
                encoding="utf-8",
            )
            payload = bundled_installer(root)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload.path, setup)
            setup.write_bytes(b"tampered")
            self.assertIsNone(bundled_installer(root))

    def test_bundled_source_contains_locked_repo_identity_and_git_bundle_hash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutSource_") as temp:
            root = Path(temp)
            bundle = root / "donutbrowser-local.bundle"
            bundle.write_bytes(b"full-git-history")
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            (root / "donut-source.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "component": "donut-local-source",
                        "commit": "a" * 40,
                        "branch": "main",
                        "origin": "https://github.com/JustHari01/donutbrowser-local.git",
                        "upstream": "https://github.com/zhom/donutbrowser.git",
                        "asset": {
                            "name": bundle.name,
                            "size": bundle.stat().st_size,
                            "sha256": digest,
                        },
                    }
                ),
                encoding="utf-8",
            )
            payload = bundled_source(root)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload.commit, "a" * 40)
            self.assertEqual(payload.path, bundle)
            value = json.loads((root / "donut-source.json").read_text(encoding="utf-8"))
            value["upstream"] = "https://example.test/wrong.git"
            (root / "donut-source.json").write_text(json.dumps(value), encoding="utf-8")
            self.assertIsNone(bundled_source(root))

    def test_manage_button_has_only_install_or_update_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutManaged_") as temp:
            manager = DonutManager(Path(temp))
            with mock.patch.object(manager, "current_install", return_value=None):
                self.assertEqual(manager.manage_action_text(), "CÀI ĐẶT DONUT LOCAL")
            with mock.patch.object(
                manager, "current_install", return_value=mock.sentinel.install
            ):
                self.assertEqual(manager.manage_action_text(), "CẬP NHẬT DONUT LOCAL")

    def test_update_check_uses_signed_private_component_revision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutManaged_") as temp:
            manager = DonutManager(Path(temp))
            current = DonutInstall("0.30.0", 1, Path(temp), Path(temp) / "DonutLocal.exe")
            release = mock.Mock(version="0.30.0", revision=2)
            with (
                mock.patch.object(manager, "current_install", return_value=current),
                mock.patch(
                    "docurandom.donut_manager.latest_local_release",
                    return_value=release,
                ) as latest,
            ):
                self.assertIs(manager.available_update(), release)
            latest.assert_called_once_with()

    def test_install_and_connect_uses_managed_signed_package(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutManaged_") as temp:
            manager = DonutManager(Path(temp))
            install = DonutInstall(
                "0.30.0", 2, Path(temp), Path(temp) / "DonutLocal.exe"
            )
            process = mock.Mock()
            endpoint = mock.sentinel.endpoint
            info = {"serverInfo": {"name": "donut-browser-local"}}
            with (
                mock.patch.object(manager, "current_install", return_value=None),
                mock.patch.object(manager, "ensure_latest", return_value=install) as ensure,
                mock.patch.object(manager, "enable_mcp"),
                mock.patch(
                    "docurandom.donut_manager.installed_local_app", return_value=None
                ),
                mock.patch.object(manager, "stop_managed_local"),
                mock.patch.object(manager, "launch", return_value=process),
                mock.patch.object(
                    manager,
                    "_wait_for_local_mcp",
                    return_value=(endpoint, info),
                ),
                mock.patch("docurandom.donut_manager.time.sleep"),
            ):
                result = manager.install_launch_and_connect()
            ensure.assert_called_once()
            self.assertEqual(result, (install, endpoint, info))

    def test_installed_app_stop_executes_verified_process_shutdown_once(self) -> None:
        install = DonutInstall(
            version="0.29.6",
            revision=0,
            root=Path(r"C:\Users\Hari\AppData\Local\Programs\DonutBrowser"),
            executable=Path(
                r"C:\Users\Hari\AppData\Local\Programs\DonutBrowser\donutbrowser.exe"
            ),
        )
        completed = mock.Mock(returncode=0)
        with (
            mock.patch.object(
                DonutManager,
                "discover_endpoint",
                side_effect=DonutManagedError("offline"),
            ),
            mock.patch("docurandom.donut_manager.subprocess.run", return_value=completed) as run,
        ):
            DonutManager._stop_installed_app(install)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[-2], "-Command")
        self.assertIn("taskkill.exe", command[-1])
        self.assertIn("$remaining.Count", command[-1])
        parse_environment = os.environ.copy()
        parse_environment["DOCURANDOM_TEST_POWERSHELL"] = command[-1]
        parsed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$tokens=$null;$errors=$null;"
                "[Management.Automation.Language.Parser]::ParseInput("
                "$env:DOCURANDOM_TEST_POWERSHELL,[ref]$tokens,[ref]$errors)|Out-Null;"
                "if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1}",
            ],
            env=parse_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(parsed.returncode, 0, parsed.stdout + parsed.stderr)

    def test_installed_app_stop_aborts_when_a_donut_process_remains(self) -> None:
        install = DonutInstall("0.29.6", 0, Path("DonutBrowser"), Path("donutbrowser.exe"))
        with (
            mock.patch.object(
                DonutManager,
                "discover_endpoint",
                side_effect=DonutManagedError("offline"),
            ),
            mock.patch(
                "docurandom.donut_manager.subprocess.run",
                return_value=mock.Mock(returncode=37),
            ),
        ):
            with self.assertRaisesRegex(DonutManagedError, "installer chưa được chạy"):
                DonutManager._stop_installed_app(install)

    def test_catalog_selects_newest_complete_local_release(self) -> None:
        payload = json.dumps(
            [
                _catalog_release("0.29.6", 1),
                _catalog_release("0.29.6", 2),
                {**_catalog_release("0.29.7", 1), "prerelease": True},
                {"tag_name": "v9.9.9", "assets": []},
            ]
        )
        release = parse_release_payload(payload)
        self.assertEqual(release.version, "0.29.7")
        self.assertEqual(release.revision, 1)
        self.assertEqual(release.archive_name, "DonutLocal_0.29.7_r1_x64-portable.zip")

    def test_bundled_manifest_requires_valid_signature_and_archive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutPackage_") as temp:
            root = Path(temp)
            archive, public_key = _signed_package(root)
            with mock.patch("docurandom.donut_manager.UPDATE_PUBLIC_KEY_B64", public_key):
                packaged = bundled_release(root)
                self.assertIsNotNone(packaged)
                assert packaged is not None
                self.assertEqual(packaged[1], archive)
                (root / "donut-local-manifest.sig").write_bytes(b"invalid")
                self.assertIsNone(bundled_release(root))

    def test_install_rejects_zip_traversal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutInstall_") as temp:
            root = Path(temp)
            package = root / "package"
            package.mkdir()
            _, public_key = _signed_package(package, traversal=True)
            manager = DonutManager(root / "managed")
            with mock.patch("docurandom.donut_manager.UPDATE_PUBLIC_KEY_B64", public_key):
                with self.assertRaises(DonutManagedError):
                    manager.ensure_latest(allow_network=False, package_root=package)
            self.assertFalse((root / "outside.txt").exists())

    def test_reads_official_version_two_mcp_token(self) -> None:
        salt = b"0123456789abcdef"
        nonce = b"123456789012"
        token = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
        key = Argon2id(
            salt=salt,
            length=32,
            iterations=2,
            lanes=1,
            memory_cost=19456,
        ).derive(DONUT_VAULT_PASSWORD)
        ciphertext = AESGCM(key).encrypt(nonce, token.encode("utf-8"), None)
        salt_text = base64.b64encode(salt).rstrip(b"=")
        payload = (
            b"DBMCP"
            + bytes([2, len(salt_text)])
            + salt_text
            + nonce
            + len(ciphertext).to_bytes(4, "little")
            + ciphertext
        )
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutToken_") as temp:
            path = Path(temp) / "mcp_token.dat"
            path.write_bytes(payload)
            self.assertEqual(DonutManager._decrypt_mcp_token(path), token)

    def test_enabling_mcp_preserves_shared_official_settings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutSettings_") as temp:
            root = Path(temp) / "DonutBrowser"
            settings = root / "settings" / "app_settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text('{"theme":"dark","mcp_enabled":false}', encoding="utf-8")
            with mock.patch("docurandom.donut_manager.official_data_root", return_value=root):
                DonutManager.enable_mcp()
            value = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(value["theme"], "dark")
            self.assertTrue(value["mcp_enabled"])
            self.assertEqual(value["mcp_port"], 51080)
            self.assertTrue(value["api_enabled"])
            self.assertEqual(value["api_port"], 10108)

    def test_only_accepts_local_mcp_identity(self) -> None:
        DonutManager._require_local_server(
            {"serverInfo": {"name": "donut-browser-local"}}
        )
        with self.assertRaises(DonutManagedError):
            DonutManager._require_local_server(
                {"serverInfo": {"name": "donut-browser"}}
            )

    def test_requires_developer_mcp_and_cdp_tools(self) -> None:
        DonutManager._require_local_tools(
            frozenset(
                {
                    "list_profiles",
                    "run_profile",
                    "navigate",
                    "evaluate_javascript",
                    "get_debug_connection",
                    "cdp_command",
                }
            )
        )
        with self.assertRaises(DonutManagedError):
            DonutManager._require_local_tools(
                frozenset({"list_profiles", "run_profile", "navigate"})
            )


if __name__ == "__main__":
    unittest.main()
