param(
    [Parameter(Mandatory = $true)]
    [string]$TargetExe
)

$ErrorActionPreference = "Stop"

$PortableHome = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$ExpectedTarget = [IO.Path]::GetFullPath(
    (Join-Path $PortableHome "Build\DonutBrowser-target\debug\donutbrowser.exe")
)
$TargetPath = [IO.Path]::GetFullPath($TargetExe)
if (-not [StringComparer]::OrdinalIgnoreCase.Equals($TargetPath, $ExpectedTarget)) {
    throw "Donut update target is outside the portable build path: $TargetPath"
}

$TargetDirectory = Split-Path -Parent $TargetPath
$StagedExe = Join-Path $TargetDirectory "donutbrowser-next.exe"
$StagedHashFile = "$StagedExe.sha256"
$SwapExe = Join-Path $TargetDirectory "donutbrowser-swap.exe"
$RollbackExe = Join-Path $TargetDirectory "donutbrowser-rollback.exe"
if (-not (Test-Path -LiteralPath $StagedExe -PathType Leaf) -or
    -not (Test-Path -LiteralPath $StagedHashFile -PathType Leaf)) {
    return
}

try {
    $ExpectedHash = (Get-Content -LiteralPath $StagedHashFile -Raw).Trim().ToUpperInvariant()
    if ($ExpectedHash -notmatch "^[0-9A-F]{64}$") {
        throw "Staged Donut build has an invalid SHA-256 marker."
    }
    $StagedHash = (Get-FileHash -LiteralPath $StagedExe -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($StagedHash -ne $ExpectedHash) {
        throw "Staged Donut build failed its SHA-256 check."
    }

    $RunningDonut = Get-CimInstance -ClassName Win32_Process -Filter "Name = 'donutbrowser.exe'" |
        Where-Object {
            $_.ExecutablePath -and
            [StringComparer]::OrdinalIgnoreCase.Equals(
                [IO.Path]::GetFullPath($_.ExecutablePath),
                $TargetPath
            )
        }
    if ($RunningDonut) {
        Write-Host "Donut update is queued; the current Donut process is still using the portable executable."
        return
    }

    if (Test-Path -LiteralPath $RollbackExe) {
        if (-not (Test-Path -LiteralPath $TargetPath)) {
            Move-Item -LiteralPath $RollbackExe -Destination $TargetPath
            Write-Warning "Restored the previous Donut executable after an interrupted update; the staged update remains queued."
            return
        }
        $CurrentHash = (Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($CurrentHash -eq $ExpectedHash) {
            Remove-Item -LiteralPath $RollbackExe
            Remove-Item -LiteralPath $StagedExe
            Remove-Item -LiteralPath $StagedHashFile
            Write-Host "The staged Donut dev build was already installed."
            return
        }
        throw "An interrupted Donut executable update needs recovery; existing files were preserved."
    }
    if (Test-Path -LiteralPath $SwapExe) {
        Remove-Item -LiteralPath $SwapExe
    }
    if (-not (Test-Path -LiteralPath $TargetPath -PathType Leaf)) {
        throw "Current Donut executable is missing: $TargetPath"
    }

    $BackupDirectory = Join-Path $PortableHome "Backups"
    if (-not (Test-Path -LiteralPath $BackupDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $BackupDirectory | Out-Null
    }
    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $BackupExe = Join-Path $BackupDirectory "donutbrowser-before-stop-race-fix-$Stamp.exe"

    Copy-Item -LiteralPath $TargetPath -Destination $BackupExe
    Copy-Item -LiteralPath $StagedExe -Destination $SwapExe
    if ((Get-FileHash -LiteralPath $SwapExe -Algorithm SHA256).Hash.ToUpperInvariant() -ne $ExpectedHash) {
        throw "Temporary Donut executable failed its SHA-256 check."
    }

    Move-Item -LiteralPath $TargetPath -Destination $RollbackExe
    try {
        Move-Item -LiteralPath $SwapExe -Destination $TargetPath
        if ((Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash.ToUpperInvariant() -ne $ExpectedHash) {
            throw "Installed Donut executable failed its SHA-256 check."
        }
    }
    catch {
        if (Test-Path -LiteralPath $TargetPath) {
            $FailedExe = Join-Path $BackupDirectory "donutbrowser-failed-stop-race-fix-$Stamp.exe"
            Move-Item -LiteralPath $TargetPath -Destination $FailedExe
        }
        Move-Item -LiteralPath $RollbackExe -Destination $TargetPath
        throw
    }

    Remove-Item -LiteralPath $RollbackExe
    Remove-Item -LiteralPath $StagedExe
    Remove-Item -LiteralPath $StagedHashFile
    Write-Host "Installed the staged Donut dev build. Previous executable saved to $BackupExe"
}
catch {
    if ((Test-Path -LiteralPath $SwapExe) -and -not (Test-Path -LiteralPath $RollbackExe)) {
        Remove-Item -LiteralPath $SwapExe -ErrorAction SilentlyContinue
    }
    Write-Warning "Could not apply the staged Donut build; the current build remains available. $($_.Exception.Message)"
}
