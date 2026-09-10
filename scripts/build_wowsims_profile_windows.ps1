[CmdletBinding()]
param(
    [string]$CalibrationJsonl,
    [string]$Template,
    [string]$Output,
    [string]$MetadataOutput
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $Template) {
    $Template = Join-Path $projectRoot 'configs\wowsims\fury_warrior_phase1.json'
}
if (-not $Output) {
    $Output = Join-Path $projectRoot 'configs\wowsims\fury_warrior_live.json'
}

$pythonArguments = @('-3', '-B', '-m', 'o2o_dps.wowsims_profile')
if ($CalibrationJsonl) {
    $pythonArguments += $CalibrationJsonl
}
$pythonArguments += @('--template', $Template, '--output', $Output)
if ($MetadataOutput) {
    $pythonArguments += @('--metadata-output', $MetadataOutput)
}

Push-Location $projectRoot
try {
    & py @pythonArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'wowsims profile build failed'
    }
}
finally {
    Pop-Location
}
