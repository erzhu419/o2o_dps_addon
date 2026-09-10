[CmdletBinding()]
param(
    [string]$SavedVariablesPath,
    [string]$DataRoot,
    [switch]$Status,
    [switch]$Once,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $projectRoot 'offline_data'
}
$resolvedDataRoot = [System.IO.Path]::GetFullPath($DataRoot)
$runtimeDirectory = Join-Path $resolvedDataRoot 'calibration_watch'
$statusPath = Join-Path $runtimeDirectory 'status.json'
$pidPath = Join-Path $runtimeDirectory 'watcher.pid'

if ($Status) {
    if (-not (Test-Path -LiteralPath $statusPath -PathType Leaf)) {
        Write-Output "status=not_started"
        Write-Output "status_file=$statusPath"
        exit 0
    }
    Get-Content -LiteralPath $statusPath -Raw
    exit 0
}

if ([string]::IsNullOrWhiteSpace($SavedVariablesPath)) {
    throw 'SavedVariablesPath is required when starting the watcher.'
}
$sourcePath = (Resolve-Path -LiteralPath $SavedVariablesPath).Path
if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
    throw "BrainOfCat SavedVariables file does not exist: $SavedVariablesPath"
}

New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
if ($Once) {
    $python = (Get-Command py.exe -ErrorAction Stop).Source
    Push-Location -LiteralPath $projectRoot
    try {
        & $python `
            -3 `
            -B `
            -m o2o_dps.calibration_watch `
            --source $sourcePath `
            --data-root $resolvedDataRoot `
            --runtime-directory $runtimeDirectory `
            --once
        $onceExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    exit $onceExitCode
}

if ($Restart -and (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
    $restartPidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    if ($restartPidText -match '^\d+$') {
        $restartProcess = Get-Process -Id ([int]$restartPidText) -ErrorAction SilentlyContinue
        if ($null -ne $restartProcess) {
            Stop-Process -Id ([int]$restartPidText) -ErrorAction Stop
            $restartProcess.WaitForExit(5000) | Out-Null
        }
    }
    Remove-Item -LiteralPath $pidPath -Force
}

if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    $existingPidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    if ($existingPidText -match '^\d+$') {
        $existingProcess = Get-Process -Id ([int]$existingPidText) -ErrorAction SilentlyContinue
        if ($null -ne $existingProcess) {
            Write-Output "status=already_running"
            Write-Output "pid=$existingPidText"
            Write-Output "status_file=$statusPath"
            exit 0
        }
    }
    Remove-Item -LiteralPath $pidPath
}

if ($sourcePath.Contains('"') -or $resolvedDataRoot.Contains('"') -or $runtimeDirectory.Contains('"')) {
    throw 'Paths containing a double quote are not supported by this launcher.'
}

$python = (Get-Command py.exe -ErrorAction Stop).Source
$stdoutPath = Join-Path $runtimeDirectory 'watcher.stdout.log'
$stderrPath = Join-Path $runtimeDirectory 'watcher.stderr.log'
$arguments = @(
    '-3',
    '-B',
    '-m',
    'o2o_dps.calibration_watch',
    '--source',
    ('"{0}"' -f $sourcePath),
    '--data-root',
    ('"{0}"' -f $resolvedDataRoot),
    '--runtime-directory',
    ('"{0}"' -f $runtimeDirectory)
)

$process = Start-Process `
    -FilePath $python `
    -ArgumentList $arguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

for ($attempt = 0; $attempt -lt 50; $attempt++) {
    if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
        break
    }
    if ($process.HasExited) {
        $errorText = if (Test-Path -LiteralPath $stderrPath) {
            (Get-Content -LiteralPath $stderrPath -Raw).Trim()
        } else {
            ''
        }
        throw "Calibration watcher exited during startup (exit $($process.ExitCode)): $errorText"
    }
    Start-Sleep -Milliseconds 100
}

if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
    throw "Calibration watcher did not publish its PID within five seconds. See $stderrPath"
}
$watcherPid = (Get-Content -LiteralPath $pidPath -Raw).Trim()

Write-Output 'status=started'
Write-Output "pid=$watcherPid"
Write-Output "source=$sourcePath"
Write-Output "status_file=$statusPath"
Write-Output "log_file=$(Join-Path $runtimeDirectory 'watch.log')"
