$ErrorActionPreference = 'Stop'
$portableRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$workerScript = Join-Path $portableRoot 'Start-DocuRandomStudio.ps1'
$statePath = Join-Path $portableRoot 'Data\runtime\portable-launcher-state.json'
$logPath = Join-Path $portableRoot 'Data\Logs\portable-launcher.log'
$hash = [Security.Cryptography.SHA256]::Create()
try {
    $rootId = ([BitConverter]::ToString($hash.ComputeHash(
        [Text.Encoding]::UTF8.GetBytes($portableRoot.ToUpperInvariant())
    ))).Replace('-', '').Substring(0, 24)
} finally { $hash.Dispose() }
$bootstrapMutex = [Threading.Mutex]::new($false, ('Local\DocuRandomPortableBootstrap-' + $rootId))
$ownsBootstrap = $false
try {
    try { $ownsBootstrap = $bootstrapMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsBootstrap = $true }
    if (-not $ownsBootstrap) {
        Write-Host 'The portable launcher is already starting.'
        exit 0
    }
    $runningWorker = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -in @('powershell.exe', 'pwsh.exe') -and
            $_.CommandLine -and
            $_.CommandLine.IndexOf($workerScript, [StringComparison]::OrdinalIgnoreCase) -ge 0
        })
    if ($runningWorker.Count -gt 0) {
        Write-Host 'The portable supervisor is already running.'
        exit 0
    }
    # A separate hidden console owns the supervisor. Closing the starter CMD
    # cannot terminate it or disable Donut crash recovery.
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $workerScript + '"'
    $logDirectory = Split-Path -Parent $logPath
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $runStamp = (Get-Date).ToString('yyyyMMdd-HHmmss') + '-' + $PID
    $stdoutLog = Join-Path $logDirectory ('portable-supervisor-' + $runStamp + '.stdout.log')
    $stderrLog = Join-Path $logDirectory ('portable-supervisor-' + $runStamp + '.stderr.log')
    $worker = Start-Process -FilePath $powershell -ArgumentList $arguments `
        -WorkingDirectory $portableRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    Write-Host 'Starting Donut and DocuRandom Studio in the background...'
    $deadline = (Get-Date).AddSeconds(60)
    do {
        $state = $null
        if (Test-Path -LiteralPath $statePath) {
            $stateReader = $null
            try {
                # Allow the supervisor to atomically replace status while read.
                $stateStream = [IO.File]::Open($statePath, [IO.FileMode]::Open,
                    [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
                $stateReader = [IO.StreamReader]::new($stateStream)
                $state = $stateReader.ReadToEnd() | ConvertFrom-Json
            } catch {} finally { if ($stateReader) { $stateReader.Dispose() } }
        }
        if ($state -and [int]$state.supervisorPid -eq $worker.Id) {
            if ($state.status -eq 'ready') {
                Write-Host 'Ready. This CMD can close; the background supervisor stays running.'
                exit 0
            }
            if ($state.status -eq 'failed') { throw ([string]$state.message) }
        }
        if ($worker.HasExited) {
            if ($worker.ExitCode -eq 0) { exit 0 }
            throw ('Portable supervisor exited with code ' + $worker.ExitCode + '. See ' + $logPath)
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    Write-Host ('Startup is still in progress. See ' + $logPath)
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    if ($ownsBootstrap) { $bootstrapMutex.ReleaseMutex() }
    $bootstrapMutex.Dispose()
}
