from pathlib import Path


INSTALLER = Path(__file__).resolve().parents[1] / "installer" / "DocuRandomStudio.iss"
BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "build_installer.ps1"


def test_first_install_exposes_one_managed_folder_and_keeps_data_outside_app():
    source = INSTALLER.read_text(encoding="utf-8")

    assert "DefaultDirName={code:GetDefaultBaseDir}" in source
    assert "DisableDirPage=no" in source
    assert 'DestDir: "{app}\\App"' in source
    assert 'Name: "{app}\\Data"' in source
    assert 'Name: "{app}\\Workspaces"' in source
    assert 'ValueName: "Home"; ValueData: "{app}"' in source
    assert 'Filename: "{app}\\App\\DocuRandomStudio.exe"' in source


def test_later_installs_reuse_the_saved_home_and_updates_only_the_app_runtime():
    source = INSTALLER.read_text(encoding="utf-8")

    assert "RegQueryStringValue(HKCU" in source
    assert "ShouldSkipPage" in source
    assert "ShouldDeployRuntime" in source
    assert "{app}\\App\\" in source


def test_installer_repairs_marker_executable_mismatch_without_downgrading():
    source = INSTALLER.read_text(encoding="utf-8")

    assert "GetVersionNumbersString(ExecutablePath, ExecutableVersion)" in source
    assert "MarkerReady and ExecutableReady" in source
    assert "CompareSemanticVersions(ExecutableVersion, EmbeddedVersion) > 0" in source
    assert "CompareSemanticVersions(ExecutableVersion, EmbeddedVersion) <> 0" in source


def test_installer_creates_stable_shortcuts_and_clears_same_version_staging():
    source = INSTALLER.read_text(encoding="utf-8")

    assert 'Name: "{userdesktop}\\DocuRandom Studio"' in source
    assert 'Name: "{userprograms}\\DocuRandom Studio"' in source
    assert 'Filename: "{app}\\App\\DocuRandomStudio.exe"' in source
    assert 'Name: "{app}\\Data\\updates\\{#AppVersion}"' in source
    assert '.docurandom-installed-version' in source


def test_installer_stops_packaged_process_trees_before_replacing_runtime():
    source = INSTALLER.read_text(encoding="utf-8")

    assert "function PrepareToInstall" in source
    assert "taskkill.exe" in source
    assert "'/F /T /IM \"' + ImageName + '\"'" in source
    assert "DocuRandom Chat Bridge.exe" in source
    assert "Sleep(500)" in source


def test_build_refuses_mismatched_marker_runtime_or_launcher_versions():
    source = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "$MarkerVersion -ne $Version" in source
    assert "$RuntimeVersion" in source
    assert "$LauncherVersion" in source
    assert "không đồng nhất" in source
