param(
    [string]$Version = "",
    [string]$PortableDir = "",
    [string]$OutputDir = "",
    [string]$InnoCompiler = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Version) {
    $Version = (& (Join-Path $ProjectRoot ".build-venv\Scripts\python.exe") -c "from docurandom import __version__; print(__version__)")
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Phiên bản installer phải có dạng X.Y.Z."
}
if (-not $PortableDir) {
    $PortableDir = Join-Path $ProjectRoot "release\DocuRandomStudio"
}
if (-not $OutputDir) {
    $OutputDir = Join-Path $ProjectRoot "release"
}
$ResolvedPortable = (Resolve-Path -LiteralPath $PortableDir).Path
$PortableExecutable = Join-Path $ResolvedPortable "DocuRandomStudio.exe"
$PortableMarker = Join-Path $ResolvedPortable ".docurandom-version"
if (-not (Test-Path -LiteralPath $PortableExecutable -PathType Leaf)) {
    throw "Thư mục portable chưa được build đầy đủ: $ResolvedPortable"
}
if (-not (Test-Path -LiteralPath $PortableMarker -PathType Leaf)) {
    throw "Bản portable thiếu dấu phiên bản: $PortableMarker"
}
$MarkerVersion = (Get-Content -LiteralPath $PortableMarker -Raw).Trim()
$RuntimeVersion = ((Get-Item -LiteralPath $PortableExecutable).VersionInfo.FileVersion).Trim()
if ($MarkerVersion -ne $Version -or $RuntimeVersion -notmatch "^$([regex]::Escape($Version))(?:\.|$)") {
    throw "Từ chối tạo launcher: marker ($MarkerVersion), runtime ($RuntimeVersion) và bản phát hành ($Version) không đồng nhất."
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$ResolvedOutput = (Resolve-Path -LiteralPath $OutputDir).Path

if (-not $InnoCompiler) {
    $Candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
    )
    $InnoCompiler = $Candidates | Where-Object {
        $_ -and (Test-Path -LiteralPath $_ -PathType Leaf)
    } | Select-Object -First 1
}
if (-not $InnoCompiler -or -not (Test-Path -LiteralPath $InnoCompiler -PathType Leaf)) {
    throw "Chưa có Inno Setup 6 để tạo file EXE tự chạy."
}

$Script = Join-Path $ProjectRoot "installer\DocuRandomStudio.iss"
& $InnoCompiler `
    "/DAppVersion=$Version" `
    "/DSourceDir=$ResolvedPortable" `
    "/DBuildOutputDir=$ResolvedOutput" `
    $Script
if ($LASTEXITCODE -ne 0) {
    throw "Không thể tạo DocuRandomStudio.exe tự chạy."
}
$Launcher = Join-Path $ResolvedOutput "DocuRandomStudio.exe"
if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
    throw "Inno Setup không tạo file EXE tự chạy dự kiến."
}
$LauncherVersion = ((Get-Item -LiteralPath $Launcher).VersionInfo.FileVersion).Trim()
if ($LauncherVersion -notmatch "^$([regex]::Escape($Version))(?:\.|$)") {
    throw "Launcher tạo xong nhưng phiên bản file không khớp $Version."
}
Write-Host "Đã tạo một file EXE tự chạy:" -ForegroundColor Green
Write-Host $Launcher
