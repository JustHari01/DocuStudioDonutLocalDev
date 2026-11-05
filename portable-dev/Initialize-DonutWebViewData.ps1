param(
    [Parameter(Mandatory = $true)]
    [string]$DonutRoot
)

$ErrorActionPreference = "Stop"

$portableWebViewRoot = Join-Path $DonutRoot "WebView2"
$portableWebViewProfile = Join-Path $portableWebViewRoot "EBWebView"
$legacyWebViewData = Join-Path $env:LOCALAPPDATA "com.donutbrowser\EBWebView"
$migrationMarker = Join-Path $portableWebViewRoot ".localappdata-migration-complete"
New-Item -ItemType Directory -Path $portableWebViewRoot, $portableWebViewProfile -Force | Out-Null

$migrationCompleted = Test-Path -LiteralPath $migrationMarker -PathType Leaf
$legacyDataHasFiles = Test-Path -LiteralPath (Join-Path $legacyWebViewData "Local State") -PathType Leaf

if (-not $migrationCompleted -and $legacyDataHasFiles) {
    $legacyPathForMatch = [System.IO.Path]::GetFullPath($legacyWebViewData).TrimEnd('\')
    $activeWebViewProcesses = @(
        Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -and
                $_.CommandLine.IndexOf($legacyPathForMatch, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
            }
    )
    if ($activeWebViewProcesses.Count -gt 0) {
        throw "Không thể chuyển dữ liệu WebView2 khi Donut còn đang mở. Hãy đóng Donut rồi chạy lại launcher."
    }

    & "$env:SystemRoot\System32\robocopy.exe" `
        $legacyWebViewData `
        $portableWebViewProfile `
        /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ /FFT /IS /IT /NFL /NDL /NJH /NJS /NP *> $null
    if ($LASTEXITCODE -ge 8) {
        throw "Không sao chép được dữ liệu WebView2 sang ổ portable (robocopy exit $LASTEXITCODE). Dữ liệu nguồn vẫn được giữ nguyên tại $legacyWebViewData."
    }
    [System.IO.File]::WriteAllText(
        $migrationMarker,
        "Copied from $legacyWebViewData on $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss').`r`n",
        [System.Text.UTF8Encoding]::new($false)
    )
}

# WEBVIEW2_USER_DATA_FOLDER places the embedded app's cookies, DOM storage,
# permissions and caches beside the portable Donut profiles on the SSD.
$env:WEBVIEW2_USER_DATA_FOLDER = [System.IO.Path]::GetFullPath($portableWebViewRoot)
