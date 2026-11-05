from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_local_patch_is_integration_only_and_keeps_real_entitlements():
    source = (ROOT / "tools" / "patch_donut_local.py").read_text(encoding="utf-8")

    assert 'config["productName"] = "Donut Local"' in source
    assert '"donut-browser-local"' in source
    assert '"entitlement_policy": "upstream-real-account"' in source
    assert "can_use_browser_automation" not in source
    assert "has_active_paid_subscription" not in source
    assert "developer_tools" not in source


def test_github_builder_uses_private_source_and_shared_private_release_channel():
    source = (ROOT / ".github" / "workflows" / "donut-local.yml").read_text(
        encoding="utf-8"
    )

    assert "JustHari01/donutbrowser-local" in source
    assert "verify_private_donut_source.py" in source
    assert "DISTRIBUTION_REPO_TOKEN" in source
    assert "corresponding-source.zip" in source
    assert "donut-local-manifest.sig" in source
    assert "--prerelease" in source
    assert "--no-bundle" in source
    assert "--features developer-tools" in source
    assert "JustHari01/DocuRandomStudio-Releases" not in source
