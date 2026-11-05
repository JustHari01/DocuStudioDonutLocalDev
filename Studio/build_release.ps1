param(
    [string]$Python = "python",
    [switch]$NoBundleFFmpeg,
    [switch]$NoBundleWatermarkRemover,
    [switch]$BundleDonut,
    [string]$DonutInstaller = "",
    [string]$DonutSourceRoot = "",
    [switch]$NoPortableZip,
    [string]$WatermarkRoot = "",
    [string]$Node = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildVenv = Join-Path $ProjectRoot ".build-venv"
$VenvPython = Join-Path $BuildVenv "Scripts\python.exe"
$DistDir = Join-Path $ProjectRoot "dist"
$ReleaseDir = Join-Path $ProjectRoot "release"
$IconPath = Join-Path $ProjectRoot "assets\docurandom.ico"
$WatermarkStage = Join-Path $ProjectRoot ".build-watermark-runtime"
$ChatBridgeRoot = Join-Path $ProjectRoot "third_party\docurandom-chat-bridge"
$ChatBridgeExecutable = Join-Path $ChatBridgeRoot "DocuRandom Chat Bridge.exe"
$ChatBridgeAppAsar = Join-Path $ChatBridgeRoot "resources\app.asar"
$CodexExecutable = Join-Path $ProjectRoot "third_party\docurandom-codex\codex.exe"
$DonutManagedRoot = Join-Path $ProjectRoot "third_party\donut-managed"
$DonutPayloadStage = Join-Path $ProjectRoot ".build-donut-payload"
$DonutInstallerStage = Join-Path $DonutPayloadStage "installer"
$DonutSourceStage = Join-Path $DonutPayloadStage "source"
$FlowExtensionConfig = Join-Path $ProjectRoot "flow-extension\config.js"

function Remove-DonutPayloadStage {
    if (-not (Test-Path -LiteralPath $DonutPayloadStage)) {
        return
    }
    $ResolvedStage = (Resolve-Path -LiteralPath $DonutPayloadStage).Path
    if ((Split-Path -Parent $ResolvedStage) -ne $ProjectRoot) {
        throw "Từ chối xóa Donut staging ngoài project: $ResolvedStage"
    }
    Remove-Item -LiteralPath $ResolvedStage -Recurse -Force
}

function Remove-WatermarkStage {
    if (-not (Test-Path -LiteralPath $WatermarkStage)) {
        return
    }
    $ResolvedStage = (Resolve-Path -LiteralPath $WatermarkStage).Path
    $ResolvedParent = Split-Path -Parent $ResolvedStage
    if ($ResolvedParent -ne $ProjectRoot) {
        throw "Từ chối xóa thư mục staging ngoài project: $ResolvedStage"
    }
    Remove-Item -LiteralPath $ResolvedStage -Recurse -Force
}

function Remove-ReleaseDirectory {
    param([string]$Target, [string]$ExpectedParent)

    if (-not (Test-Path -LiteralPath $Target)) {
        return
    }
    $ResolvedTarget = (Resolve-Path -LiteralPath $Target).Path
    $ResolvedParent = (Resolve-Path -LiteralPath $ExpectedParent).Path
    if ((Split-Path -Parent $ResolvedTarget) -ne $ResolvedParent) {
        throw "Từ chối xóa thư mục ngoài release: $ResolvedTarget"
    }
    for ($Attempt = 1; $Attempt -le 8; $Attempt++) {
        try {
            Remove-Item -LiteralPath $ResolvedTarget -Recurse -Force -ErrorAction Stop
            return
        }
        catch {
            if ($Attempt -eq 8) {
                throw
            }
            # Windows Defender/indexer may briefly retain a freshly closed DLL.
            Start-Sleep -Milliseconds 400
        }
    }
}

function Resolve-NodeExecutable {
    param([string]$RequestedNode)

    $Candidates = @()
    if ($RequestedNode) {
        $Candidates += $RequestedNode
    }
    if ($env:DOCURANDOM_NODE_PATH) {
        $Candidates += $env:DOCURANDOM_NODE_PATH
    }
    $NodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if ($NodeCommand) {
        $Candidates += $NodeCommand.Source
    }
    $Candidates += @(
        (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\nodejs\node.exe"),
        (Join-Path $env:ProgramFiles "nodejs\node.exe")
    )
    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }
    throw "Không tìm thấy node.exe để đóng gói Gemini Watermark Remover. Dùng -Node <đường-dẫn-node.exe>."
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $Python -m venv $BuildVenv
}
# The unpacked extension imports this file from both the background worker and
# popup.  CI creates it as a build-only file, but local builds must do the same
# or the extension fails before it can open its bridge WebSocket.
if (-not (Test-Path -LiteralPath $FlowExtensionConfig -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "flow-extension\config.example.js") `
        -Destination $FlowExtensionConfig
}
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements-build.txt")
& $VenvPython (Join-Path $ProjectRoot "tools\generate_icon.py") $IconPath
$PreviousPythonPathForBuild = $env:PYTHONPATH
$env:PYTHONPATH = $ProjectRoot
$ReleaseVersion = (& $VenvPython -c "from docurandom import __version__; print(__version__)")
$ExpectedBridgeMetadata = @(
    & $VenvPython -c "from docurandom.bridge_integrity import CHAT_BRIDGE_EXECUTABLE_SHA256, CHAT_BRIDGE_APP_ASAR_SHA256; from docurandom.content_ai import CHATGPT_WEB_BRIDGE_VERSION; print(CHAT_BRIDGE_EXECUTABLE_SHA256); print(CHAT_BRIDGE_APP_ASAR_SHA256); print(CHATGPT_WEB_BRIDGE_VERSION)"
)
$env:PYTHONPATH = $PreviousPythonPathForBuild
if ($ExpectedBridgeMetadata.Count -ne 3) {
    throw "Không đọc được metadata đã khóa của DocuRandom Chat Bridge."
}
if ($ReleaseVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "Không đọc được phiên bản runtime để đóng gói."
}
$ChatBridgeExecutableSha256 = ([string]$ExpectedBridgeMetadata[0]).ToUpperInvariant()
$ChatBridgeAppAsarSha256 = ([string]$ExpectedBridgeMetadata[1]).ToUpperInvariant()
$ExpectedChatBridgeVersion = ([string]$ExpectedBridgeMetadata[2]).Trim()
$VersionInfoDir = Join-Path $ProjectRoot "build\version-info"
$AppVersionInfo = Join-Path $VersionInfoDir "DocuRandomStudio.txt"
$UpdaterVersionInfo = Join-Path $VersionInfoDir "DocuRandomUpdater.txt"
& $VenvPython (Join-Path $ProjectRoot "tools\write_version_info.py") `
    --version $ReleaseVersion `
    --internal-name "DocuRandomStudio" `
    --description "DocuRandom Studio" `
    --output $AppVersionInfo
& $VenvPython (Join-Path $ProjectRoot "tools\write_version_info.py") `
    --version $ReleaseVersion `
    --internal-name "DocuRandomUpdater" `
    --description "DocuRandom Studio Updater" `
    --output $UpdaterVersionInfo

if (($DonutInstaller -and -not $DonutSourceRoot) -or
    ($DonutSourceRoot -and -not $DonutInstaller)) {
    throw "Phải truyền đồng thời -DonutInstaller và -DonutSourceRoot."
}
if ($BundleDonut -and $DonutInstaller) {
    throw "Không dùng đồng thời gói Donut portable cũ và installer/source Donut mới."
}
if ($DonutInstaller) {
    $ResolvedDonutInstaller = (Resolve-Path -LiteralPath $DonutInstaller).Path
    $ResolvedDonutSource = (Resolve-Path -LiteralPath $DonutSourceRoot).Path
    $DonutInstallerName = Split-Path -Leaf $ResolvedDonutInstaller
    if ($DonutInstallerName -notmatch '^DocuRandom-DN_(\d+\.\d+\.\d+)_x64-setup\.exe$') {
        throw "Tên installer Donut Local không đúng mẫu DocuRandom-DN_X.Y.Z_x64-setup.exe."
    }
    $DonutVersion = $Matches[1]
    if (-not (Test-Path -LiteralPath (Join-Path $ResolvedDonutSource ".git"))) {
        throw "DonutSourceRoot không phải repo Git đầy đủ: $ResolvedDonutSource"
    }
    foreach ($RequiredSourcePath in @(
        "UPDATE_DOCURANDOM.cmd",
        "scripts\update-build-docurandom.ps1",
        "scripts\verify-local-contract.mjs",
        "src-tauri\tauri.conf.json"
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $ResolvedDonutSource $RequiredSourcePath))) {
            throw "Source Donut Local thiếu: $RequiredSourcePath"
        }
    }
    $DonutStatus = @(& git -C $ResolvedDonutSource status --porcelain=v1 --untracked-files=normal 2>&1)
    if ($LASTEXITCODE -ne 0 -or $DonutStatus.Count -gt 0) {
        throw "Source Donut Local phải là repo sạch trước khi đóng gói."
    }
    $DonutBranch = ([string](& git -C $ResolvedDonutSource branch --show-current)).Trim()
    $DonutCommit = ([string](& git -C $ResolvedDonutSource rev-parse HEAD)).Trim().ToLowerInvariant()
    $DonutOrigin = ([string](& git -C $ResolvedDonutSource remote get-url origin)).Trim()
    $DonutUpstream = ([string](& git -C $ResolvedDonutSource remote get-url upstream)).Trim()
    if ($DonutBranch -notmatch '^[A-Za-z0-9._/-]+$' -or
        $DonutCommit -notmatch '^[0-9a-f]{40}$') {
        throw "Không xác định được branch/commit Donut Local để đóng gói."
    }
    if ($DonutOrigin -ne "https://github.com/JustHari01/donutbrowser-local.git" -or
        $DonutUpstream -ne "https://github.com/zhom/donutbrowser.git") {
        throw "Remote origin/upstream Donut Local không đúng kho đã khóa."
    }
    $DonutConfig = Get-Content -LiteralPath (Join-Path $ResolvedDonutSource "src-tauri\tauri.conf.json") -Raw | ConvertFrom-Json
    if ([string]$DonutConfig.version -ne $DonutVersion) {
        throw "Phiên bản installer ($DonutVersion) không khớp source Donut ($($DonutConfig.version))."
    }

    Remove-DonutPayloadStage
    New-Item -ItemType Directory -Path $DonutInstallerStage -Force | Out-Null
    New-Item -ItemType Directory -Path $DonutSourceStage -Force | Out-Null
    $StagedInstaller = Join-Path $DonutInstallerStage $DonutInstallerName
    Copy-Item -LiteralPath $ResolvedDonutInstaller -Destination $StagedInstaller
    $InstallerHash = (Get-FileHash -LiteralPath $StagedInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
    $InstallerSize = (Get-Item -LiteralPath $StagedInstaller).Length
    [ordered]@{
        schema = 1
        component = "donut-local-installer"
        version = $DonutVersion
        asset = [ordered]@{
            name = $DonutInstallerName
            size = $InstallerSize
            sha256 = $InstallerHash
        }
    } | ConvertTo-Json -Depth 4 | Set-Content `
        -LiteralPath (Join-Path $DonutInstallerStage "donut-installer.json") `
        -Encoding UTF8

    $DonutBundle = Join-Path $DonutSourceStage "donutbrowser-local.bundle"
    & git -C $ResolvedDonutSource bundle create $DonutBundle --all
    if ($LASTEXITCODE -ne 0) {
        throw "Không thể đóng gói lịch sử Git Donut Local."
    }
    & git -C $ResolvedDonutSource bundle verify $DonutBundle | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Git bundle Donut Local vừa tạo không hợp lệ."
    }
    $BundleHash = (Get-FileHash -LiteralPath $DonutBundle -Algorithm SHA256).Hash.ToLowerInvariant()
    $BundleSize = (Get-Item -LiteralPath $DonutBundle).Length
    [ordered]@{
        schema = 1
        component = "donut-local-source"
        commit = $DonutCommit
        branch = $DonutBranch
        origin = $DonutOrigin
        upstream = $DonutUpstream
        asset = [ordered]@{
            name = "donutbrowser-local.bundle"
            size = $BundleSize
            sha256 = $BundleHash
        }
    } | ConvertTo-Json -Depth 4 | Set-Content `
        -LiteralPath (Join-Path $DonutSourceStage "donut-source.json") `
        -Encoding UTF8
}

$PyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--windowed",
    "--onedir",
    "--name", "DocuRandomStudio",
    "--icon", $IconPath,
    "--version-file", $AppVersionInfo,
    "--add-data", "$IconPath;assets",
    "--add-data", "$(Join-Path $ProjectRoot 'flow-extension');flow-extension",
    "--add-data", "$(Join-Path $ProjectRoot 'donut-extension');donut-extension",
    "--distpath", $DistDir,
    "--workpath", (Join-Path $ProjectRoot "build"),
    "--specpath", $ProjectRoot,
    "--hidden-import", "PIL._tkinter_finder",
    "--collect-all", "uvicorn",
    "--collect-all", "websockets",
    "--collect-all", "faster_whisper",
    "--collect-all", "ctranslate2",
    "--collect-all", "av",
    "--collect-all", "tokenizers"
)

if (-not (Test-Path -LiteralPath $ChatBridgeExecutable -PathType Leaf)) {
    throw "Thiếu DocuRandom Chat Bridge: $ChatBridgeExecutable"
}
if (-not (Test-Path -LiteralPath $ChatBridgeAppAsar -PathType Leaf)) {
    throw "Thiếu mã đóng gói DocuRandom Chat Bridge: $ChatBridgeAppAsar"
}
& $VenvPython (Join-Path $ProjectRoot "tools\verify_chat_bridge_package.py") `
    $ChatBridgeAppAsar `
    --expected-version $ExpectedChatBridgeVersion
if ($LASTEXITCODE -ne 0) {
    throw "DocuRandom Chat Bridge đóng gói không khớp phiên bản source."
}
if (-not (Test-Path -LiteralPath $CodexExecutable -PathType Leaf)) {
    throw "Thiếu Codex CLI portable đã xác minh: $CodexExecutable"
}
$ActualChatBridgeExecutableHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ChatBridgeExecutable).Hash
$ActualChatBridgeAppAsarHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ChatBridgeAppAsar).Hash
if ($ActualChatBridgeExecutableHash -ne $ChatBridgeExecutableSha256 -or
    $ActualChatBridgeAppAsarHash -ne $ChatBridgeAppAsarSha256) {
    throw "DocuRandom Chat Bridge không khớp SHA-256 đã khóa."
}
$PyInstallerArgs += @(
    "--add-data", "$ChatBridgeRoot;chat-bridge"
)
$PyInstallerArgs += @(
    "--add-binary", "$CodexExecutable;codex-cli"
)

if ($DonutInstaller) {
    $PyInstallerArgs += @(
        "--add-data", "$DonutInstallerStage;donut-installer",
        "--add-data", "$DonutSourceStage;donut-source"
    )
}

if ($BundleDonut) {
    $PreviousPythonPathForDonut = $env:PYTHONPATH
    $env:PYTHONPATH = $ProjectRoot
    try {
        & $VenvPython (Join-Path $ProjectRoot "tools\stage_donut_component.py") `
            --output $DonutManagedRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Không thể chuẩn bị gói Donut Local đã ký cho bản all-in-one."
        }
    }
    finally {
        $env:PYTHONPATH = $PreviousPythonPathForDonut
    }
    $DonutManifest = Join-Path $DonutManagedRoot "donut-local-manifest.json"
    $DonutSignature = Join-Path $DonutManagedRoot "donut-local-manifest.sig"
    $DonutSource = Join-Path $DonutManagedRoot "SOURCE.txt"
    if (-not (Test-Path -LiteralPath $DonutManifest -PathType Leaf) -or
        -not (Test-Path -LiteralPath $DonutSignature -PathType Leaf) -or
        -not (Test-Path -LiteralPath $DonutSource -PathType Leaf)) {
        throw "Gói Donut Local all-in-one thiếu manifest, chữ ký hoặc thông tin mã nguồn."
    }
    $PyInstallerArgs += @(
        "--add-data", "$DonutManagedRoot;donut-managed"
    )
}

if (-not $NoBundleWatermarkRemover) {
    if (-not $WatermarkRoot) {
        $WatermarkRoot = Join-Path (Split-Path -Parent $ProjectRoot) "watermark-remover-main"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $WatermarkRoot "bin\gwr.mjs") -PathType Leaf)) {
        throw "Không tìm thấy watermark-remover-main hợp lệ tại: $WatermarkRoot"
    }
    $ResolvedNode = Resolve-NodeExecutable -RequestedNode $Node
    $PnpmCommand = Get-Command pnpm -ErrorAction SilentlyContinue
    if (-not $PnpmCommand) {
        throw "Cần pnpm để chuẩn bị runtime Gemini Watermark Remover khi đóng gói."
    }

    Remove-WatermarkStage
    New-Item -ItemType Directory -Path $WatermarkStage -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $WatermarkRoot "bin") -Destination $WatermarkStage -Recurse
    Copy-Item -LiteralPath (Join-Path $WatermarkRoot "src") -Destination $WatermarkStage -Recurse
    # Upstream keeps a large regression corpus under src/assets. The CLI uses
    # embedded alpha maps and the previous verified runtime contains no assets,
    # so shipping sample PNG/MP4 files would only add over 100 MB per update.
    $WatermarkSampleAssets = Join-Path $WatermarkStage "src\assets"
    if (Test-Path -LiteralPath $WatermarkSampleAssets) {
        $ResolvedWatermarkAssets = (Resolve-Path -LiteralPath $WatermarkSampleAssets).Path
        $ExpectedWatermarkAssetsParent = (Resolve-Path -LiteralPath (Join-Path $WatermarkStage "src")).Path
        if ((Split-Path -Parent $ResolvedWatermarkAssets) -ne $ExpectedWatermarkAssetsParent) {
            throw "Từ chối xóa assets watermark ngoài staging: $ResolvedWatermarkAssets"
        }
        Remove-Item -LiteralPath $ResolvedWatermarkAssets -Recurse -Force
    }
    Copy-Item -LiteralPath (Join-Path $WatermarkRoot "LICENSE") -Destination $WatermarkStage
    $SourceWatermarkPackage = Get-Content -LiteralPath (Join-Path $WatermarkRoot "package.json") -Raw | ConvertFrom-Json
    $RuntimePackage = [ordered]@{
        name = "docurandom-watermark-runtime"
        version = [string]$SourceWatermarkPackage.version
        private = $true
        type = "module"
        dependencies = [ordered]@{
            sharp = "0.34.5"
        }
    }
    $RuntimePackage | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $WatermarkStage "package.json") -Encoding UTF8

    $PreviousPath = $env:Path
    $env:Path = "$(Split-Path -Parent $ResolvedNode);$PreviousPath"
    Push-Location $WatermarkStage
    try {
        # PyInstaller does not preserve pnpm's directory junctions reliably.
        # A hoisted, copied layout puts sharp and its native @img packages in
        # real directories under node_modules, so the installed EXE is truly
        # self-contained and never asks the user to run pnpm.
        & $PnpmCommand.Source add `
            --save-prod sharp@0.34.5 `
            --ignore-workspace `
            --allow-build=sharp `
            --config.node-linker=hoisted `
            --config.package-import-method=copy
        if ($LASTEXITCODE -ne 0) {
            throw "Không thể cài dependency runtime của Gemini Watermark Remover."
        }
        & $ResolvedNode -e "import('sharp').then(()=>process.exit(0)).catch(error=>{console.error(error);process.exit(1)})"
        if ($LASTEXITCODE -ne 0) {
            throw "Sharp không thể khởi tạo trong runtime Gemini Watermark Remover."
        }
        & $ResolvedNode (Join-Path $WatermarkStage "bin\gwr.mjs") --help | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Không thể chạy CLI Gemini Watermark Remover đã staging."
        }
    }
    finally {
        Pop-Location
        $env:Path = $PreviousPath
    }
    $PyInstallerArgs += @(
        "--add-data", "$WatermarkStage;watermark-remover",
        "--add-binary", "$ResolvedNode;watermark-node"
    )
}

if (-not $NoBundleFFmpeg) {
    $env:PYTHONPATH = $ProjectRoot
    $ResolvedTools = @()
    for ($Attempt = 1; $Attempt -le 4; $Attempt++) {
        $ResolvedTools = @(
            & $VenvPython -c "from docurandom.ffmpeg_manager import ensure_tools; t=ensure_tools(); print(t.ffmpeg); print(t.ffprobe)"
        )
        if (($LASTEXITCODE -eq 0) -and ($ResolvedTools.Count -ge 2)) {
            break
        }
        if ($Attempt -lt 4) {
            Write-Warning "Máy chủ FFmpeg chưa phản hồi (lần $Attempt/4); đang thử lại."
            Start-Sleep -Seconds (5 * $Attempt)
        }
    }
    if ($ResolvedTools.Count -lt 2) {
        throw "Không thể xác định đường dẫn FFmpeg và FFprobe."
    }
    $FFmpeg = [string]$ResolvedTools[-2]
    $FFprobe = [string]$ResolvedTools[-1]
    if (-not (Test-Path -LiteralPath $FFmpeg) -or -not (Test-Path -LiteralPath $FFprobe)) {
        throw "Không thể chuẩn bị FFmpeg để đóng gói."
    }
    $PyInstallerArgs += @(
        "--add-binary", "$FFmpeg;ffmpeg",
        "--add-binary", "$FFprobe;ffmpeg"
    )
}

$PyInstallerArgs += (Join-Path $ProjectRoot "app.py")
& $VenvPython -m PyInstaller @PyInstallerArgs

$UpdaterArgs = @(
    "--noconfirm",
    "--clean",
    "--windowed",
    "--onefile",
    "--name", "DocuRandomUpdater",
    "--icon", $IconPath,
    "--version-file", $UpdaterVersionInfo,
    "--distpath", $DistDir,
    "--workpath", (Join-Path $ProjectRoot "build\updater"),
    "--specpath", $ProjectRoot,
    (Join-Path $ProjectRoot "updater_app.py")
)
& $VenvPython -m PyInstaller @UpdaterArgs

$PortableDir = Join-Path $DistDir "DocuRandomStudio"
if ($DonutInstaller) {
    $BuiltDonutInstallerRoot = Join-Path $PortableDir "_internal\donut-installer"
    $BuiltDonutSourceRoot = Join-Path $PortableDir "_internal\donut-source"
    $BuiltDonutInstaller = Join-Path $BuiltDonutInstallerRoot $DonutInstallerName
    $BuiltDonutBundle = Join-Path $BuiltDonutSourceRoot "donutbrowser-local.bundle"
    foreach ($RequiredPayload in @(
        $BuiltDonutInstaller,
        (Join-Path $BuiltDonutInstallerRoot "donut-installer.json"),
        $BuiltDonutBundle,
        (Join-Path $BuiltDonutSourceRoot "donut-source.json")
    )) {
        if (-not (Test-Path -LiteralPath $RequiredPayload -PathType Leaf)) {
            throw "Bản PyInstaller thiếu payload Donut Local: $RequiredPayload"
        }
    }
    if ((Get-FileHash -LiteralPath $BuiltDonutInstaller -Algorithm SHA256).Hash.ToLowerInvariant() -ne $InstallerHash -or
        (Get-FileHash -LiteralPath $BuiltDonutBundle -Algorithm SHA256).Hash.ToLowerInvariant() -ne $BundleHash) {
        throw "Payload Donut Local trong bản PyInstaller không khớp SHA-256 staging."
    }
    & git -C $ResolvedDonutSource bundle verify $BuiltDonutBundle | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Git bundle Donut Local trong bản PyInstaller không hợp lệ."
    }
}
if (-not $NoBundleWatermarkRemover) {
    # Validate the materialized PyInstaller output, not only the pre-build
    # staging tree. This catches lost pnpm links before any release is signed.
    $BuiltWatermarkRoot = Join-Path $PortableDir "_internal\watermark-remover"
    $BuiltWatermarkNode = Join-Path $PortableDir "_internal\watermark-node\node.exe"
    $BuiltSharpPackage = Join-Path $BuiltWatermarkRoot "node_modules\sharp\package.json"
    $BuiltWatermarkCli = Join-Path $BuiltWatermarkRoot "bin\gwr.mjs"
    if (-not (Test-Path -LiteralPath $BuiltSharpPackage -PathType Leaf)) {
        throw "Bản portable sau PyInstaller bị thiếu node_modules\sharp; từ chối phát hành."
    }
    if (-not (Test-Path -LiteralPath $BuiltWatermarkNode -PathType Leaf)) {
        throw "Bản portable sau PyInstaller bị thiếu node.exe cho watermark runtime."
    }
    $PreviousPath = $env:Path
    $env:Path = "$(Split-Path -Parent $BuiltWatermarkNode);$PreviousPath"
    Push-Location $BuiltWatermarkRoot
    try {
        & $BuiltWatermarkNode -e "import('sharp').then(()=>process.exit(0)).catch(error=>{console.error(error);process.exit(1)})"
        if ($LASTEXITCODE -ne 0) {
            throw "Sharp không thể khởi tạo từ chính thư mục portable sau PyInstaller."
        }
        & $BuiltWatermarkNode $BuiltWatermarkCli --help | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Gemini Watermark Remover không chạy được từ chính thư mục portable."
        }
    }
    finally {
        Pop-Location
        $env:Path = $PreviousPath
    }
}
Copy-Item -LiteralPath (Join-Path $DistDir "DocuRandomUpdater.exe") -Destination $PortableDir -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "README_VI.md") -Destination $PortableDir -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "HUONG_DAN_YOUTUBE_VI.md") -Destination $PortableDir -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "THIRD_PARTY_NOTICES.md") -Destination $PortableDir -Force
Set-Content -LiteralPath (Join-Path $PortableDir ".docurandom-version") -Value $ReleaseVersion -Encoding Ascii -NoNewline

New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null
$ReleaseBaseName = $null
$ReleaseAppDir = $null
$ReleaseCandidates = @("DocuRandomStudio", "DocuRandomStudio-next")
$ReleaseCandidates += 2..20 | ForEach-Object { "DocuRandomStudio-next$_" }
foreach ($Candidate in $ReleaseCandidates) {
    $CandidatePath = Join-Path $ReleaseDir $Candidate
    if (Test-Path -LiteralPath $CandidatePath) {
        try {
            Remove-ReleaseDirectory -Target $CandidatePath -ExpectedParent $ReleaseDir
        }
        catch {
            Write-Warning "Bản $Candidate đang được sử dụng; thử tên release kế tiếp."
            continue
        }
    }
    $ReleaseBaseName = $Candidate
    $ReleaseAppDir = $CandidatePath
    break
}
if (-not $ReleaseAppDir) {
    throw "Không tìm được tên release trống sau 20 bản đang được sử dụng."
}
if ($ReleaseBaseName -ne "DocuRandomStudio") {
    Write-Warning "Bản release hiện tại đang chạy; xuất bản mới sang $ReleaseBaseName."
}
Copy-Item -LiteralPath $PortableDir -Destination $ReleaseAppDir -Recurse

$ZipPath = Join-Path $ReleaseDir "$ReleaseBaseName-portable.zip"
if (-not $NoPortableZip) {
    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }
    Compress-Archive -Path $ReleaseAppDir -DestinationPath $ZipPath -CompressionLevel Optimal
}

if (-not $NoBundleWatermarkRemover) {
    Remove-WatermarkStage
}
if ($DonutInstaller) {
    Remove-DonutPayloadStage
}

Write-Host ""
Write-Host "Đã tạo runtime để đóng installer:" -ForegroundColor Green
Write-Host (Join-Path $ReleaseAppDir "DocuRandomStudio.exe")
if (-not $NoPortableZip) {
    Write-Host $ZipPath
}
