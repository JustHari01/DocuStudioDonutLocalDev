param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,
    [Parameter(Mandatory = $true)]
    [string]$PortableZip,
    [string]$InstallerExe = "",
    [string]$Notes = "Bản cập nhật DocuRandom Studio $Version",
    [string]$Python = "",
    [string]$PrivateKey = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $Python) {
    $Python = Join-Path $ProjectRoot ".build-venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Không tìm thấy Python build: $Python"
}
$PreviousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $ProjectRoot
$ExpectedVersion = (& $Python -c "from docurandom import __version__; print(__version__)" 2>$null)
if ($ExpectedVersion -ne $Version) {
    $env:PYTHONPATH = $PreviousPythonPath
    throw "Version source ($ExpectedVersion) không khớp version phát hành ($Version)."
}
$SourceZip = (Resolve-Path -LiteralPath $PortableZip).Path
if (-not $PrivateKey) {
    $PrivateKey = Join-Path $env:LOCALAPPDATA "DocuRandomStudio\release-signing\ed25519-private.pem"
}
if (-not (Test-Path -LiteralPath $PrivateKey -PathType Leaf)) {
    throw "Thiếu khóa ký release offline: $PrivateKey"
}
$Gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $Gh) {
    throw "Chưa có GitHub CLI chính thức (gh)."
}
& $Gh.Source auth status
if ($LASTEXITCODE -ne 0) {
    throw "GitHub CLI chưa đăng nhập."
}

$ArtifactsRoot = Join-Path $ProjectRoot "artifacts"
$ReleaseStage = Join-Path $ArtifactsRoot "release-$Version"
if (Test-Path -LiteralPath $ReleaseStage) {
    $ResolvedStage = (Resolve-Path -LiteralPath $ReleaseStage).Path
    if ((Split-Path -Parent $ResolvedStage) -ne $ArtifactsRoot) {
        throw "Từ chối xóa staging ngoài artifacts."
    }
    Remove-Item -LiteralPath $ResolvedStage -Recurse -Force
}
New-Item -ItemType Directory -Path $ReleaseStage -Force | Out-Null
$CanonicalZip = Join-Path $ReleaseStage "DocuRandomStudio-portable.zip"
$Manifest = Join-Path $ReleaseStage "update-manifest.json"
$Signature = Join-Path $ReleaseStage "update-manifest.sig"
Copy-Item -LiteralPath $SourceZip -Destination $CanonicalZip
& $Python (Join-Path $ProjectRoot "tools\sign_update.py") `
    --version $Version `
    --asset $CanonicalZip `
    --private-key $PrivateKey `
    --manifest $Manifest `
    --signature $Signature
if ($LASTEXITCODE -ne 0) {
    throw "Không thể ký manifest cập nhật."
}

$Repo = "JustHari01/DocuRandom-DN"
$Tag = "docurandom-v$Version"
$ReleaseAssets = @($CanonicalZip, $Manifest, $Signature)
if ($InstallerExe) {
    $ResolvedInstaller = (Resolve-Path -LiteralPath $InstallerExe).Path
    $CanonicalInstaller = Join-Path $ReleaseStage "DocuRandomStudio.exe"
    Copy-Item -LiteralPath $ResolvedInstaller -Destination $CanonicalInstaller
    $ReleaseAssets += $CanonicalInstaller
}
& $Gh.Source release view $Tag --repo $Repo *> $null
if ($LASTEXITCODE -eq 0) {
    $env:PYTHONPATH = $PreviousPythonPath
    throw "Release $Tag đã tồn tại; từ chối ghi đè asset đã phát hành."
}
& $Gh.Source release create $Tag @ReleaseAssets `
    --repo $Repo --target main --title "DocuRandom Studio $Version" --notes $Notes
if ($LASTEXITCODE -ne 0) {
    $env:PYTHONPATH = $PreviousPythonPath
    throw "GitHub từ chối tạo hoặc tải asset release."
}
$env:PYTHONPATH = $PreviousPythonPath
Write-Host "Đã phát hành kênh cập nhật công khai $Tag với chữ ký Ed25519." -ForegroundColor Green
