$ErrorActionPreference = "Stop"

$PortableHome = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$launcherStatePath = Join-Path $PortableHome "Data\runtime\portable-launcher-state.json"
function Write-PortableLauncherState([string]$Status, [string]$Message = "") {
    $record = @{
        supervisorPid = $PID
        status = $Status
        message = $Message
        updatedAt = (Get-Date).ToString("o")
        donutPid = $(if ($donutProcess) { $donutProcess.Id } else { $null })
        studioPid = $(if ($studioProcess) { $studioProcess.Id } else { $null })
    }
    $stateDirectory = Split-Path -Parent $launcherStatePath
    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    $temporaryState = $launcherStatePath + "." + $PID + ".tmp"
    [IO.File]::WriteAllText($temporaryState, (ConvertTo-Json $record), [Text.UTF8Encoding]::new($false))
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            if (Test-Path -LiteralPath $launcherStatePath) {
                [IO.File]::Replace($temporaryState, $launcherStatePath, [NullString]::Value)
            } else { [IO.File]::Move($temporaryState, $launcherStatePath) }
            return
        } catch {
            # A status reader or antivirus can briefly prevent replacement.
            # Status telemetry must never take down browser supervision.
            if ($attempt -eq 19) {
                Write-Warning ("Could not refresh launcher status: " + $_.Exception.Message)
                return
            }
            Start-Sleep -Milliseconds 50
        }
    }
}
$launcherHash = [Security.Cryptography.SHA256]::Create()
try {
    $launcherRootId = ([BitConverter]::ToString($launcherHash.ComputeHash(
        [Text.Encoding]::UTF8.GetBytes($PortableHome.ToUpperInvariant())
    ))).Replace("-", "").Substring(0, 24)
} finally { $launcherHash.Dispose() }
$launcherMutex = [Threading.Mutex]::new($false, ("Local\DocuRandomPortableSupervisor-" + $launcherRootId))
$ownsLauncherMutex = $false
try { $ownsLauncherMutex = $launcherMutex.WaitOne(0) }
catch [Threading.AbandonedMutexException] { $ownsLauncherMutex = $true }
if (-not $ownsLauncherMutex) {
    $launcherMutex.Dispose()
    exit 0
}
trap {
    $startupError = $_.Exception.Message
    try {
        Write-PortableLauncherState "failed" $startupError
        $failureLog = Join-Path $PortableHome "Data\Logs\portable-launcher.log"
        New-Item -ItemType Directory -Path (Split-Path -Parent $failureLog) -Force | Out-Null
        Add-Content -LiteralPath $failureLog -Encoding UTF8 -Value (
            "[" + (Get-Date).ToString("o") + "] Supervisor PID " + $PID + " failed: " + $startupError
        )
    } catch {}
    exit 1
}
Write-PortableLauncherState "starting"
$PortableDriveRoot = [IO.Path]::GetPathRoot($PortableHome)
$RepositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PortableHome "..")).Path
$StudioAppDirectory = Join-Path $PortableHome "App"
$SelectedStudioVersion = [version]"0.0.0"
foreach ($candidateName in @("App", "App-next", "App-autofix", "App-recovery")) {
    $candidateDirectory = Join-Path $PortableHome $candidateName
    $candidateExe = Join-Path $candidateDirectory "DocuRandomStudio.exe"
    if (-not (Test-Path -LiteralPath $candidateExe -PathType Leaf)) {
        continue
    }
    $candidateVersion = [version]"0.0.0"
    $candidateVersionPath = Join-Path $candidateDirectory ".docurandom-version"
    if (Test-Path -LiteralPath $candidateVersionPath -PathType Leaf) {
        try {
            $candidateVersionText = (Get-Content -LiteralPath $candidateVersionPath -Raw).Trim()
            $candidateVersion = [version]$candidateVersionText
        }
        catch {
            # A damaged version marker must not prevent the current portable
            # app from starting; valid newer staged builds still win below.
            $candidateVersion = [version]"0.0.0"
        }
    }
    if ($candidateVersion -ge $SelectedStudioVersion) {
        $SelectedStudioVersion = $candidateVersion
        $StudioAppDirectory = $candidateDirectory
    }
}
$StudioExe = Join-Path $StudioAppDirectory "DocuRandomStudio.exe"
$NodeExe = Join-Path $PortableHome "Runtime\node.exe"
$NextCli = Join-Path $RepositoryRoot "node_modules\next\dist\bin\next"
$DonutExe = Join-Path $PortableHome "Components\DonutLocal-0.36.0\donutbrowser.exe"
$DonutData = Join-Path $PortableHome "DonutBrowser\Data"
$DonutCache = Join-Path $PortableHome "DonutBrowser\Cache"
$DonutEphemeral = Join-Path $PortableHome "DonutBrowser\Ephemeral"
$DonutRoot = Join-Path $PortableHome "DonutBrowser"
$FlowEdgeUserData = Join-Path $PortableHome "FlowEdge\User Data"
$FlowExtension = Join-Path $PortableHome "FlowExtension"
$StagedFlowExtensionDirectory = Join-Path $PortableHome "FlowExtension-next"
$PortableTemp = Join-Path $PortableHome "Temp"

if (Test-Path -LiteralPath $StagedFlowExtensionDirectory -PathType Container) {
    $stagedManifestPath = Join-Path $StagedFlowExtensionDirectory "manifest.json"
    $bundledManifestPath = Join-Path $StudioAppDirectory "_internal\flow-extension\manifest.json"
    if (-not (Test-Path -LiteralPath $stagedManifestPath -PathType Leaf)) {
        throw "Extension Flow đang chờ cập nhật nhưng thiếu manifest.json: $StagedFlowExtensionDirectory"
    }
    if (-not (Test-Path -LiteralPath $bundledManifestPath -PathType Leaf)) {
        throw "Bản Studio được staging không có Flow Agent Extension tương ứng."
    }
    try {
        $stagedExtensionManifest = Get-Content -LiteralPath $stagedManifestPath -Raw | ConvertFrom-Json
        $bundledExtensionManifest = Get-Content -LiteralPath $bundledManifestPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "Không đọc được manifest của extension Flow đang chờ cập nhật."
    }
    $stagedExtensionVersion = [string]$stagedExtensionManifest.version
    if (
        -not $stagedExtensionVersion -or
        $stagedExtensionVersion -ne [string]$bundledExtensionManifest.version
    ) {
        throw "Extension Flow đang chờ cập nhật không khớp phiên bản Studio."
    }

    $currentManifestPath = Join-Path $FlowExtension "manifest.json"
    $stagedExtensionAlreadyActive = $false
    if (Test-Path -LiteralPath $currentManifestPath -PathType Leaf) {
        try {
            $activeExtensionManifest = Get-Content -LiteralPath $currentManifestPath -Raw | ConvertFrom-Json
            $stagedExtensionAlreadyActive = (
                [string]$activeExtensionManifest.version -eq $stagedExtensionVersion -and
                (Test-Path -LiteralPath (Join-Path $FlowExtension "background.js") -PathType Leaf)
            )
        }
        catch {
            $stagedExtensionAlreadyActive = $false
        }
    }

    if (-not $stagedExtensionAlreadyActive) {
    $portableEdgeProcesses = @(
        Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -and
                $_.CommandLine.IndexOf(
                    $FlowEdgeUserData,
                    [StringComparison]::OrdinalIgnoreCase
                ) -ge 0
            }
    )
    if ($portableEdgeProcesses.Count -gt 0) {
        throw "Có phiên Edge portable đang mở. Hãy đóng tool và Edge portable rồi chạy lại launcher để cập nhật extension Flow an toàn."
    }

    $currentExtensionVersion = "invalid"
    if (Test-Path -LiteralPath $currentManifestPath -PathType Leaf) {
        try {
            $currentExtensionManifest = Get-Content -LiteralPath $currentManifestPath -Raw | ConvertFrom-Json
            if ([string]$currentExtensionManifest.version) {
                $currentExtensionVersion = [string]$currentExtensionManifest.version
            }
        }
        catch {
            # Preserve the unreadable extension as a backup, then activate the
            # separately validated extension that matches this Studio build.
            $currentExtensionVersion = "invalid"
        }
    }
    $backupExtensionName = "FlowExtension-before-" +
        $currentExtensionVersion + "-" +
        (Get-Date -Format "yyyyMMdd-HHmmss") + "-" +
        [Guid]::NewGuid().ToString("N").Substring(0, 8)
    $extensionBackupPath = Join-Path (Join-Path $PortableHome "Backups") $backupExtensionName
    $resolvedPortableHome = [IO.Path]::GetFullPath($PortableHome).TrimEnd('\') + '\'
    $resolvedExtensionPath = [IO.Path]::GetFullPath($FlowExtension)
    $resolvedStagedExtensionPath = [IO.Path]::GetFullPath($StagedFlowExtensionDirectory)
    $resolvedExtensionBackupPath = [IO.Path]::GetFullPath($extensionBackupPath)
    $resolvedBackupRoot = [IO.Path]::GetFullPath((Join-Path $PortableHome "Backups"))
    if (
        -not $resolvedExtensionPath.StartsWith($resolvedPortableHome, [StringComparison]::OrdinalIgnoreCase) -or
        -not $resolvedStagedExtensionPath.StartsWith($resolvedPortableHome, [StringComparison]::OrdinalIgnoreCase) -or
        -not $resolvedExtensionBackupPath.StartsWith(
            $resolvedBackupRoot.TrimEnd('\') + '\',
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Từ chối cập nhật extension Flow ngoài thư mục portable."
    }

    New-Item -ItemType Directory -Path (Split-Path -Parent $extensionBackupPath) -Force | Out-Null
    if (Test-Path -LiteralPath $FlowExtension -PathType Container) {
        Copy-Item -LiteralPath $FlowExtension -Destination $extensionBackupPath -Recurse
    }
    try {
        # Keep the registered Edge extension path stable. Copying the staged
        # files into that existing directory also avoids directory-move
        # behavior that nested the source on the removable drive.
        foreach ($stagedItem in Get-ChildItem -LiteralPath $resolvedStagedExtensionPath -Force) {
            $destinationItem = Join-Path $resolvedExtensionPath $stagedItem.Name
            Copy-Item -LiteralPath $stagedItem.FullName -Destination $destinationItem -Recurse -Force
        }
    }
    catch {
        throw
    }

    if (-not (Test-Path -LiteralPath $currentManifestPath -PathType Leaf)) {
        throw "Extension Flow được staging nhưng manifest.json chưa ở thư mục gốc: $currentManifestPath"
    }
    $activatedManifest = Get-Content -LiteralPath $currentManifestPath -Raw | ConvertFrom-Json
    if ([string]$activatedManifest.version -ne $stagedExtensionVersion) {
        throw "Extension Flow sau cập nhật không khớp phiên bản Studio; extension cũ đã được sao lưu tại $extensionBackupPath."
    }
    }
}

$studioFlowManifestPath = Join-Path $StudioAppDirectory "_internal\flow-extension\manifest.json"
$portableFlowManifestPath = Join-Path $FlowExtension "manifest.json"
if (
    -not (Test-Path -LiteralPath $studioFlowManifestPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $portableFlowManifestPath -PathType Leaf)
) {
    throw "Thiếu manifest Flow Agent của bản Studio hoặc thư mục extension portable."
}
$studioFlowManifest = Get-Content -LiteralPath $studioFlowManifestPath -Raw | ConvertFrom-Json
$portableFlowManifest = Get-Content -LiteralPath $portableFlowManifestPath -Raw | ConvertFrom-Json
if ([string]$studioFlowManifest.version -ne [string]$portableFlowManifest.version) {
    throw "Phiên bản extension Flow portable không khớp bản Studio đang chạy; không khởi động để tránh lỗi kết nối."
}

foreach ($required in @($StudioExe, $NodeExe, $DonutExe, (Join-Path $FlowExtension "manifest.json"))) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Thiếu thành phần portable: $required"
    }
}

function Test-LocalPort([int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $client.Connect("127.0.0.1", $Port)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Test-DonutLocalMcpReady {
    $port = 51902
    $settingsPath = Join-Path $DonutData "settings\app_settings.json"
    if (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
        try {
            $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
            $configuredPort = 0
            if ([int]::TryParse([string]$settings.mcp_port, [ref]$configuredPort) -and
                $configuredPort -ge 1 -and $configuredPort -le 65535) {
                $port = $configuredPort
            }
        }
        catch {
            # Keep the documented local default until Donut writes its settings.
        }
    }

    $request = $null
    $response = $null
    $statusCode = 0
    try {
        # An unauthenticated request to the local-dev MCP route must be
        # rejected by its auth middleware (401). A removed-server tombstone
        # answers 410, so a listening port alone is not enough readiness proof.
        $request = [System.Net.HttpWebRequest]::Create(
            "http://127.0.0.1:$port/mcp/docurandom-readiness-probe"
        )
        $request.Method = "GET"
        $request.Timeout = 1000
        $request.ReadWriteTimeout = 1000
        $request.Proxy = $null
        $response = $request.GetResponse()
        $statusCode = [int]$response.StatusCode
    }
    catch [System.Net.WebException] {
        if ($_.Exception.Response) {
            $response = $_.Exception.Response
            $statusCode = [int]$response.StatusCode
        }
    }
    catch {
        $statusCode = 0
    }
    finally {
        if ($response) {
            $response.Close()
        }
    }
    return $statusCode -eq 401
}

function Get-RunningNativeDonut {
    Get-Process -Name donutbrowser -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.Equals($DonutExe, [StringComparison]::OrdinalIgnoreCase) } |
        Select-Object -First 1
}

function Wait-DonutLocalMcpReady {
    param(
        [System.Diagnostics.Process]$DonutProcess,
        [System.Diagnostics.Process]$SupervisorProcess = $null,
        [int]$TimeoutSeconds = 45
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($SupervisorProcess -and $SupervisorProcess.HasExited) {
            return $false
        }
        if ($DonutProcess -and $DonutProcess.HasExited) {
            return $false
        }
        if (Test-DonutLocalMcpReady) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Write-PortableLauncherLog([string]$Message) {
    $logPath = Join-Path $PortableHome "Data\Logs\portable-launcher.log"
    $logDirectory = Split-Path -Parent $logPath
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $timestamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
    Add-Content -LiteralPath $logPath -Value "[$timestamp] $Message" -Encoding UTF8
}

function Get-PortableFlowBackendIds {
    $processes = Get-CimInstance Win32_Process -Filter "Name='DocuRandomStudio.exe'" -ErrorAction SilentlyContinue
    foreach ($process in $processes) {
        if (
            $process.ExecutablePath -and
            [IO.Path]::GetFullPath($process.ExecutablePath).Equals(
                [IO.Path]::GetFullPath($StudioExe),
                [StringComparison]::OrdinalIgnoreCase
            ) -and
            $process.CommandLine -match '(?i)(^|\s)--flow-backend(\s|$)'
        ) {
            [int]$process.ProcessId
        }
    }
}

function Get-PortableEdgeBrowserIds {
    $processes = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" -ErrorAction SilentlyContinue
    foreach ($process in $processes) {
        if (
            $process.CommandLine -and
            $process.CommandLine.IndexOf($FlowEdgeUserData, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $process.CommandLine -notmatch '(?i)(^|\s)--type='
        ) {
            [int]$process.ProcessId
        }
    }
}

function Get-PortableAppProcessIds {
    $appRoot = [IO.Path]::GetFullPath($StudioAppDirectory).TrimEnd('\') + '\'
    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    foreach ($process in $processes) {
        if (-not $process.ExecutablePath) {
            continue
        }
        try {
            $executablePath = [IO.Path]::GetFullPath($process.ExecutablePath)
        }
        catch {
            continue
        }
        if ($executablePath.StartsWith($appRoot, [StringComparison]::OrdinalIgnoreCase)) {
            [int]$process.ProcessId
        }
    }
}

function Stop-PortableAppProcesses([int[]]$ProcessIds) {
    foreach ($processId in @($ProcessIds | Sort-Object -Unique)) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $processId /T /F *> $null
    }
}

function Save-PortableJson([string]$Path, [object]$Value) {
    $json = ConvertTo-Json -InputObject $Value -Depth 100
    $encoding = [Text.UTF8Encoding]::new($false)
    $temporaryPath = $Path + ".portable.tmp"
    [IO.File]::WriteAllText($temporaryPath, $json + [Environment]::NewLine, $encoding)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $backupPath = $Path + ".portable-backup-" + [Guid]::NewGuid().ToString("N") + ".json"
        [IO.File]::Replace($temporaryPath, $Path, $backupPath)
    }
    else {
        [IO.File]::Move($temporaryPath, $Path)
    }
}

function Update-PortableWorkspaceDrivePaths {
    $driveMarker = Join-Path $PortableHome ".portable-drive-root"
    $registryPath = Join-Path $PortableHome "Data\workspaces.json"
    if (-not (Test-Path -LiteralPath $registryPath -PathType Leaf)) {
        return
    }

    if (-not (Test-Path -LiteralPath $driveMarker -PathType Leaf)) {
        [IO.File]::WriteAllText($driveMarker, $PortableDriveRoot, [Text.UTF8Encoding]::new($false))
        return
    }

    $previousDriveRoot = (Get-Content -LiteralPath $driveMarker -Raw).Trim()
    if ($previousDriveRoot -notmatch '^[A-Za-z]:\\$') {
        throw "Không đọc được ký tự ổ đĩa portable đã lưu: $driveMarker"
    }
    if ($previousDriveRoot.Equals($PortableDriveRoot, [StringComparison]::OrdinalIgnoreCase)) {
        return
    }

    $registry = Get-Content -LiteralPath $registryPath -Raw | ConvertFrom-Json
    $changed = $false
    foreach ($workspace in $registry.workspaces) {
        $oldRoot = [string]$workspace.root_dir
        if (-not $oldRoot.StartsWith($previousDriveRoot, [StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        $newRoot = $PortableDriveRoot + $oldRoot.Substring($previousDriveRoot.Length)
        if (-not (Test-Path -LiteralPath $newRoot -PathType Container)) {
            throw "Workspace '$($workspace.name)' không tìm thấy trên ổ portable mới: $newRoot"
        }

        $workspace.root_dir = $newRoot
        $changed = $true
        $metadataPath = Join-Path $newRoot ".docurandom\workspace.json"
        if (Test-Path -LiteralPath $metadataPath -PathType Leaf) {
            $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
            $metadata.root_dir = $newRoot
            Save-PortableJson $metadataPath $metadata
        }

        $settingsPath = Join-Path $newRoot ".docurandom\settings.json"
        if (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
            $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
            foreach ($property in $settings.PSObject.Properties) {
                if ($property.Value -is [string] -and $property.Value.StartsWith(
                    $previousDriveRoot,
                    [StringComparison]::OrdinalIgnoreCase
                )) {
                    $settings.($property.Name) = $PortableDriveRoot + $property.Value.Substring($previousDriveRoot.Length)
                }
            }
            Save-PortableJson $settingsPath $settings
        }
    }

    if ($changed) {
        Save-PortableJson $registryPath $registry
    }
    [IO.File]::WriteAllText($driveMarker, $PortableDriveRoot, [Text.UTF8Encoding]::new($false))
}

function Stop-PortableEdgeBrowsers([int[]]$ProcessIds) {
    $targets = @($ProcessIds | Sort-Object -Unique)
    foreach ($processId in $targets) {
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($process) {
            $process.CloseMainWindow() | Out-Null
        }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(12)
    do {
        $remaining = @(Get-PortableEdgeBrowserIds | Where-Object { $targets -contains $_ })
        if ($remaining.Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 300
    } while ([DateTime]::UtcNow -lt $deadline)

    foreach ($processId in $remaining) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $processId /T /F *> $null
    }
}

if (Test-LocalPort 8001) {
    throw "Flow Agent 8001 đang chạy từ một phiên khác. Hãy đóng phiên đó trước khi mở bộ portable."
}
if (@(Get-PortableEdgeBrowserIds).Count -gt 0) {
    # A managed Studio restart can leave Edge's resident extension alive after
    # its last Flow window closes. Reuse that same registered profile only when
    # the extension version is already active; never replace a live extension.
    if (-not $stagedExtensionAlreadyActive) {
        throw 'Close the previous Flow profile before activating a different extension.'
    }
    Write-PortableLauncherLog ('Reusing the existing managed Flow Edge profile with extension ' + $stagedExtensionVersion + '.')
}

if (-not (Test-Path -LiteralPath $DonutData -PathType Container)) {
    throw "Không thấy Data Donut cũ. Dừng để tránh tạo nhầm kho profile: $DonutData"
}
New-Item -ItemType Directory -Force -Path @(
    $DonutData,
    $DonutCache,
    $DonutEphemeral,
    $FlowEdgeUserData,
    $PortableTemp,
    (Join-Path $PortableHome "Workspaces"),
    (Join-Path $PortableHome "Components")
) | Out-Null

Update-PortableWorkspaceDrivePaths

# All mutable Donut and Studio paths follow the portable folder if its drive
# letter changes. These settings are inherited only by this launcher session.
$env:DOCURANDOM_HOME = $PortableHome
$env:DOCURANDOM_DONUT_DATA_ROOT = $DonutData
$env:DOCURANDOM_FLOW_EDGE_USER_DATA = $FlowEdgeUserData
$env:DOCURANDOM_FLOW_EXTENSION = $FlowExtension
$env:DOCURANDOM_AUTO_OPEN_FLOW = "1"
$env:TEMP = $PortableTemp
$env:TMP = $PortableTemp
$env:DONUT_LOCAL_DEV = "1"
$env:NEXT_PUBLIC_DONUT_LOCAL_DEV = "1"
$env:DONUTBROWSER_DATA_ROOT = $DonutRoot
$env:DONUTBROWSER_DATA_DIR = $DonutData
$env:DONUTBROWSER_CACHE_DIR = $DonutCache
$env:DONUTBROWSER_EPHEMERAL_ROOT = $DonutEphemeral

$nextProcess = $null
$donutProcess = $null
$donutOwnedByLauncher = $false
$studioProcess = $null
$flowBackendIdsBefore = @(Get-PortableFlowBackendIds)
$edgeBrowserIdsBefore = @(Get-PortableEdgeBrowserIds)
$appProcessIdsBefore = @(Get-PortableAppProcessIds)

try {
    Write-PortableLauncherLog "Supervisor PID $PID starting; browser recovery stays active independently of the starter CMD."
    & (Join-Path $PortableHome "Initialize-DonutWebViewData.ps1") -DonutRoot $DonutRoot

    # The local binary embeds its UI; no Next.js development server is needed.

    $donutProcess = Get-RunningNativeDonut
    if ($donutProcess) {
        Write-PortableLauncherLog "Reusing Donut Local PID $($donutProcess.Id) already opened by the Donut-only launcher."
    }
    else {
        $donutProcess = Start-Process `
            -FilePath $DonutExe `
            -WorkingDirectory $RepositoryRoot `
            -WindowStyle Hidden `
            -PassThru
        $donutOwnedByLauncher = $true
    }

    if (-not (Wait-DonutLocalMcpReady -DonutProcess $donutProcess -TimeoutSeconds 45)) {
        if ($donutProcess.HasExited) {
            throw "Donut dev đã đóng trước khi MCP local sẵn sàng. Studio chưa được mở để tránh workflow lỗi tiếp."
        }
        throw "Donut dev chưa mở MCP local-dev sau 45 giây. Studio chưa được mở để tránh workflow lỗi tiếp."
    }

    $studioProcess = Start-Process `
        -FilePath $StudioExe `
        -WorkingDirectory (Split-Path -Parent $StudioExe) `
        -PassThru
    Write-PortableLauncherState "ready"
    Write-PortableLauncherLog "Ready: Studio PID $($studioProcess.Id), Donut PID $($donutProcess.Id)."
    $donutRestartDelaySeconds = 5
    $nextLauncherHeartbeat = (Get-Date).AddSeconds(30)
    while (-not $studioProcess.WaitForExit(1000)) {
        if ((Get-Date) -ge $nextLauncherHeartbeat) {
            Write-PortableLauncherState "ready"
            $nextLauncherHeartbeat = (Get-Date).AddSeconds(30)
        }
        if (-not $donutProcess -or -not $donutProcess.HasExited) {
            continue
        }

        $donutExitCode = "unknown"
        try {
            $donutExitCode = [string]$donutProcess.ExitCode
        }
        catch {
            # The process can disappear between HasExited and ExitCode.
        }
        Write-PortableLauncherLog "Donut dev exited (code=$donutExitCode); restarting the launcher-owned browser process."
        Write-PortableLauncherState "restarting" "Donut exited; retaining Studio and its checkpoints."
        Start-Sleep -Seconds $donutRestartDelaySeconds
        if ($studioProcess.HasExited) {
            break
        }

        try {
            $donutProcess = Start-Process `
                -FilePath $DonutExe `
                -WorkingDirectory $RepositoryRoot `
                -WindowStyle Hidden `
                -PassThru
            $donutOwnedByLauncher = $true
        }
        catch {
            Write-PortableLauncherLog "Donut dev restart could not start: $($_.Exception.Message)"
            $donutRestartDelaySeconds = [Math]::Min(60, $donutRestartDelaySeconds * 2)
            continue
        }

        if (Wait-DonutLocalMcpReady `
            -DonutProcess $donutProcess `
            -SupervisorProcess $studioProcess `
            -TimeoutSeconds 45) {
            Write-PortableLauncherLog "Donut local-dev MCP is ready again."
            Write-PortableLauncherState "ready"
            $donutRestartDelaySeconds = 5
        }
        else {
            Write-PortableLauncherLog "Donut restarted but local-dev MCP is not ready; Studio stays open and its checkpoints remain authoritative."
            $donutRestartDelaySeconds = [Math]::Min(60, $donutRestartDelaySeconds * 2)
        }
    }
}
finally {
    if ($studioProcess -and -not $studioProcess.HasExited) {
        $studioProcess.CloseMainWindow() | Out-Null
        if (-not $studioProcess.WaitForExit(15000)) {
            Stop-Process -Id $studioProcess.Id -Force -ErrorAction SilentlyContinue
        }
    }

    if ($donutOwnedByLauncher -and $donutProcess -and -not $donutProcess.HasExited) {
        $donutProcess.CloseMainWindow() | Out-Null
        if (-not $donutProcess.WaitForExit(15000)) {
            # The native app may be waiting for its quit dialog. Let the user
            # finish closing it so Wayfern can flush cookies/session storage.
            Write-PortableLauncherLog "Donut is awaiting graceful quit; keeping profile processes alive to preserve session data."
        }
    }

    # Close only Edge's dedicated Flow profile created by this launcher. The
    # user's normal Edge profile and its windows remain untouched.
    $baselineEdge = @($edgeBrowserIdsBefore)
    $newEdgeBrowserIds = @(
        Get-PortableEdgeBrowserIds | Where-Object { $baselineEdge -notcontains $_ }
    )
    if ($newEdgeBrowserIds.Count -gt 0) {
        Stop-PortableEdgeBrowsers $newEdgeBrowserIds
    }

    # A forced Studio exit can leave its hidden Chat Bridge and Flow helper
    # processes alive. Only stop processes launched from this portable App.
    $baselineApp = @($appProcessIdsBefore)
    $newAppProcessIds = @(
        Get-PortableAppProcessIds | Where-Object { $baselineApp -notcontains $_ }
    )
    if ($newAppProcessIds.Count -gt 0) {
        Stop-PortableAppProcesses $newAppProcessIds
    }

    if ($nextProcess -and -not $nextProcess.HasExited) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $nextProcess.Id /T /F *> $null
    }

    # Graceful Studio shutdown normally closes this child itself. Clean up only
    # a matching portable backend created during this launcher session, such as
    # after Studio was force-closed or crashed.
    $baseline = @($flowBackendIdsBefore)
    foreach ($processId in @(Get-PortableFlowBackendIds)) {
        if ($baseline -notcontains $processId) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    }
    Write-PortableLauncherState "stopped"
    Write-PortableLauncherLog "Supervisor PID $PID stopped after Studio exit."
    if ($ownsLauncherMutex) { $launcherMutex.ReleaseMutex() }
    $launcherMutex.Dispose()
}
