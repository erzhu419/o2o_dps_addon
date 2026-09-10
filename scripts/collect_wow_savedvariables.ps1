[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SavedVariablesDirectory,

    [string]$OutputRoot = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')).Path 'offline_data\online_raw')
)

$ErrorActionPreference = 'Stop'
$sourceDirectory = (Resolve-Path -LiteralPath $SavedVariablesDirectory).Path
if (-not (Test-Path -LiteralPath $sourceDirectory -PathType Container)) {
    throw "SavedVariables directory does not exist: $SavedVariablesDirectory"
}

$requiredBrain = Join-Path $sourceDirectory 'BrainOfCat.lua'
if (-not (Test-Path -LiteralPath $requiredBrain -PathType Leaf)) {
    throw "BrainOfCat.lua is missing. In WoW run /reload or log out after collecting, then retry."
}

$importId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')
$destinationDirectory = Join-Path $OutputRoot $importId
New-Item -ItemType Directory -Path $destinationDirectory | Out-Null

$names = @('BrainOfCat.lua', 'Cat2.lua', 'Cat.lua', 'Contra.lua')
$copied = @()
foreach ($name in $names) {
    $source = Join-Path $sourceDirectory $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        continue
    }
    $destination = Join-Path $destinationDirectory $name
    Copy-Item -LiteralPath $source -Destination $destination
    $sourceItem = Get-Item -LiteralPath $source
    $copied += [ordered]@{
        name = $name
        source = $sourceItem.FullName
        copied_to = $destination
        size_bytes = $sourceItem.Length
        modified_at_utc = $sourceItem.LastWriteTimeUtc.ToString('o')
    }
}

$receipt = [ordered]@{
    schema_version = 1
    acquisition = 'explicit Windows SavedVariables snapshot after WoW reload/logout'
    imported_at_utc = [DateTime]::UtcNow.ToString('o')
    source_directory = $sourceDirectory
    files = $copied
}
$receiptPath = Join-Path $destinationDirectory 'provenance.json'
$receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $receiptPath -Encoding utf8

Write-Output "snapshot=$destinationDirectory"
Write-Output "brain=$((Join-Path $destinationDirectory 'BrainOfCat.lua'))"
