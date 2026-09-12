[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('prepare', 'stage', 'smoke', 'launch', 'status', 'reduce', 'result')]
    [string]$Action,
    [string]$RunRoot,
    [string]$SourceReleaseRoot,
    [string]$AttemptId,
    [string]$AttemptsRoot,
    [string]$AttemptDirectory,
    [string]$SiteConfig,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($SiteConfig)) {
    $SiteConfig = Join-Path $projectRoot 'configs\hpc\site.local.json'
}
if ([string]::IsNullOrWhiteSpace($SourceReleaseRoot)) {
    $SourceReleaseRoot = Join-Path $projectRoot '.hpc-local\releases\fury-multiseed-source'
}
if ([string]::IsNullOrWhiteSpace($AttemptsRoot)) {
    $AttemptsRoot = Join-Path $projectRoot '.hpc-local\horizon-remote-attempts'
}

$arguments = @(
    '-3',
    '-B',
    '-m',
    'o2o_dps.fury_cat2_horizon_remote_orchestrator_v1',
    $Action
)
if ($Action -eq 'prepare') {
    if ([string]::IsNullOrWhiteSpace($RunRoot) -or [string]::IsNullOrWhiteSpace($AttemptId)) {
        throw 'prepare requires -RunRoot and -AttemptId.'
    }
    $arguments += @(
        '--run-root', $RunRoot,
        '--source-release-root', $SourceReleaseRoot,
        '--attempt-id', $AttemptId,
        '--attempts-root', $AttemptsRoot,
        '--site-config', $SiteConfig
    )
}
else {
    if ([string]::IsNullOrWhiteSpace($AttemptDirectory)) {
        throw "$Action requires -AttemptDirectory."
    }
    $arguments += @(
        '--attempt-directory', $AttemptDirectory,
        '--site-config', $SiteConfig
    )
    if ($Action -eq 'stage') {
        $arguments += @('--source-release-root', $SourceReleaseRoot)
    }
    if ($Apply -and $Action -in @('stage', 'smoke', 'launch', 'reduce')) {
        $arguments += '--apply'
    }
}

Push-Location $projectRoot
try {
    & py @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
if ($null -eq $exitCode) {
    $exitCode = 1
}
exit $exitCode
