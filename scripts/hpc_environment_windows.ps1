[CmdletBinding()]
param(
    [string]$SiteConfig,
    [string]$Bridge,
    [string[]]$Node = @(),
    [string[]]$Artifact = @(),
    [string]$ReceiptDirectory,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($SiteConfig)) {
    $SiteConfig = Join-Path $projectRoot 'configs\hpc\site.local.json'
}
if ([string]::IsNullOrWhiteSpace($Bridge)) {
    $Bridge = Join-Path $projectRoot 'bin\o2obridge.seedfix-v3.withdb.goamd64v1.linux-amd64'
}
if ([string]::IsNullOrWhiteSpace($ReceiptDirectory)) {
    $ReceiptDirectory = Join-Path $projectRoot '.hpc-local\receipts'
}

$arguments = @(
    '-3',
    '-B',
    '-m',
    'o2o_dps.hpc_environment_v1',
    '--site-config',
    $SiteConfig,
    '--bridge',
    $Bridge,
    '--receipt-directory',
    $ReceiptDirectory
)
foreach ($nodeOverride in $Node) {
    $arguments += @('--node', $nodeOverride)
}
foreach ($artifactPath in $Artifact) {
    $arguments += @('--artifact', $artifactPath)
}
if ($Apply) {
    $arguments += '--apply'
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
