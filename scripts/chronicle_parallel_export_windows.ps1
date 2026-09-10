[CmdletBinding()]
param(
    [ValidateRange(1, 16)]
    [int]$Concurrency = 3,
    [ValidateRange(1, 999)]
    [int]$WorkerStart = 1,
    [double]$TimeoutMinutes = 360,
    [int]$MaxRetries = 3,
    [string]$DataRoot,
    [string]$ParallelRoot,
    [string]$Chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe',
    [string]$Node = 'node',
    [string]$AdoptInstance,
    [string]$AdoptDownloads,
    [switch]$Status,
    [switch]$RetryFailed
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $projectRoot 'offline_data'
}
else {
    $DataRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DataRoot)
}

$arguments = @('-B', '-m', 'o2o_dps.chronicle_parallel_export')
if ($Status) {
    $arguments += @('status', '--data-root', $DataRoot, '--max-retries', $MaxRetries.ToString())
}
elseif ($RetryFailed) {
    $arguments += @('retry-failed', '--data-root', $DataRoot)
}
else {
    if ([string]::IsNullOrWhiteSpace($ParallelRoot)) {
        $ParallelRoot = Join-Path $DataRoot 'chronicle_raw\parallel_export'
    }
    else {
        $ParallelRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
            $ParallelRoot
        )
    }
    if ([string]::IsNullOrWhiteSpace($AdoptInstance) -ne [string]::IsNullOrWhiteSpace($AdoptDownloads)) {
        throw '-AdoptInstance and -AdoptDownloads must be used together.'
    }

    $arguments += @(
        'run',
        '--concurrency', $Concurrency.ToString(),
        '--worker-start', $WorkerStart.ToString(),
        '--timeout-minutes', $TimeoutMinutes.ToString([Globalization.CultureInfo]::InvariantCulture),
        '--max-retries', $MaxRetries.ToString(),
        '--data-root', $DataRoot,
        '--parallel-root', $ParallelRoot,
        '--chrome', $Chrome,
        '--node', $Node
    )
    if (-not [string]::IsNullOrWhiteSpace($AdoptInstance)) {
        $resolvedAdoptDownloads = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
            $AdoptDownloads
        )
        $arguments += @(
            '--adopt-instance', $AdoptInstance,
            '--adopt-downloads', $resolvedAdoptDownloads
        )
    }
}

Push-Location $projectRoot
try {
    & py @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Chronicle parallel export exited with code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
