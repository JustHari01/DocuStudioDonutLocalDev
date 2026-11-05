from pathlib import Path

from tools.write_version_info import render_version_info


BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "build_release.ps1"


def test_version_info_embeds_semver_in_windows_file_resource():
    value = render_version_info(
        "2.3.14",
        internal_name="DocuRandomStudio",
        description="DocuRandom Studio",
    )

    assert "filevers=(2, 3, 14, 0)" in value
    assert "StringStruct(u'FileVersion', u'2.3.14.0')" in value
    assert "StringStruct(u'InternalName', 'DocuRandomStudio')" in value


def test_release_build_passes_version_resources_to_both_windows_executables():
    source = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert 'tools\\write_version_info.py' in source
    assert 'tools\\verify_chat_bridge_package.py' in source
    assert "ExpectedChatBridgeVersion" in source
    assert '"--version-file", $AppVersionInfo' in source
    assert '"--version-file", $UpdaterVersionInfo' in source


def test_release_build_keeps_private_donut_out_of_public_tool_package_by_default():
    source = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "tools\\stage_donut_component.py" in source
    assert '"--add-data", "$DonutManagedRoot;donut-managed"' in source
    assert '"donut-local-manifest.json"' in source
    assert '"donut-local-manifest.sig"' in source
    assert "BundleDonut" in source
    assert "if ($BundleDonut)" in source


def test_release_build_can_embed_donut_nsis_and_full_git_source_without_user_zip():
    source = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "DonutInstaller" in source
    assert "DonutSourceRoot" in source
    assert "donut-installer.json" in source
    assert "donutbrowser-local.bundle" in source
    assert "git -C $ResolvedDonutSource bundle create" in source
    assert '"--add-data", "$DonutInstallerStage;donut-installer"' in source
    assert '"--add-data", "$DonutSourceStage;donut-source"' in source
    assert "NoPortableZip" in source
