$ErrorActionPreference = "Stop"
$PortableHome = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$DonutExe = Join-Path $PortableHome "Components\DonutLocal-0.36.0\donutbrowser.exe"
$DonutRoot = Join-Path $PortableHome "DonutBrowser"
$DonutData = Join-Path $DonutRoot "Data"
$DonutCache = Join-Path $DonutRoot "Cache"
$DonutEphemeral = Join-Path $DonutRoot "Ephemeral"
$PortableTemp = Join-Path $PortableHome "Temp"
$logDirectory = Join-Path $PortableTemp "DonutOnly"
$launchLog = Join-Path $logDirectory "launcher.log"

function Show-DonutError([string]$Message) {
    try {
        [IO.File]::AppendAllText($launchLog, "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ERROR $Message`r`n", [Text.UTF8Encoding]::new($false))
    } catch {}
    try {
        $shell = New-Object -ComObject WScript.Shell
        $null = $shell.Popup($Message, 0, "Donut Local", 0x10)
    } catch {}
}

try {
    New-Item -ItemType Directory -Path $PortableTemp, $logDirectory -Force | Out-Null
    if (-not (Test-Path -LiteralPath $DonutExe -PathType Leaf)) {
        throw "Thiếu bản Donut local: $DonutExe"
    }
    # Preserve the original profiles, engine path and Windows encryption context.
    if (-not (Test-Path -LiteralPath $DonutData -PathType Container)) {
        throw "Không thấy Data cũ. Dừng để tránh tạo nhầm kho profile mới: $DonutData"
    }
    New-Item -ItemType Directory -Path $DonutCache, $DonutEphemeral -Force | Out-Null
    & (Join-Path $PortableHome "Initialize-DonutWebViewData.ps1") -DonutRoot $DonutRoot
    $env:TEMP = $PortableTemp
    $env:TMP = $PortableTemp
    $env:DONUTBROWSER_DATA_ROOT = $DonutRoot
    $env:DONUTBROWSER_DATA_DIR = $DonutData
    $env:DONUTBROWSER_CACHE_DIR = $DonutCache
    $env:DONUTBROWSER_EPHEMERAL_ROOT = $DonutEphemeral
    # The native binary embeds the UI; no development web server is needed.
    $donutProcess = Start-Process -FilePath $DonutExe -WorkingDirectory (Split-Path -Parent $DonutExe) -WindowStyle Hidden -PassThru
    [IO.File]::AppendAllText($launchLog, "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] INFO Donut Local started (PID $($donutProcess.Id)); original Data: $DonutData`r`n", [Text.UTF8Encoding]::new($false))
    $donutProcess.WaitForExit()
} catch {
    Show-DonutError $_.Exception.Message
}