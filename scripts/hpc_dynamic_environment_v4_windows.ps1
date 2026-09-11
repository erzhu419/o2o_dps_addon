[CmdletBinding()]
param(
    [string]$SiteConfig,
    [string]$Contract,
    [string]$Bridge,
    [string]$ReceiptDirectory,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($SiteConfig)) {
    $SiteConfig = Join-Path $projectRoot 'configs\hpc\site.local.json'
}
if ([string]::IsNullOrWhiteSpace($Contract)) {
    $Contract = Join-Path $projectRoot 'configs\hpc\dynamic_v4.example.json'
}
if ([string]::IsNullOrWhiteSpace($Bridge)) {
    $Bridge = Join-Path $projectRoot 'bin\o2obridge.seedfix-v4.withdb.goamd64v1.linux-amd64'
}
if ([string]::IsNullOrWhiteSpace($ReceiptDirectory)) {
    $ReceiptDirectory = Join-Path $projectRoot '.hpc-local\receipts'
}

$arguments = @(
    '-3',
    '-B',
    '-m',
    'o2o_dps.hpc_dynamic_environment_v4',
    '--site-config',
    $SiteConfig,
    '--contract',
    $Contract,
    '--bridge',
    $Bridge,
    '--receipt-directory',
    $ReceiptDirectory
)
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
