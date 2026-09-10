[CmdletBinding()]
param(
    [string]$Server,
    [string]$Realm,
    [string[]]$Character = @(),
    [string]$LeaderboardPdfDirectory,
    [string]$Downloads = (Join-Path $HOME 'Downloads'),
    [string]$DataRoot,
    [int]$Limit = 0,
    [double]$TimeoutMinutes = 0,
    [switch]$DiscoverOnly,
    [switch]$NoBrowser,
    [switch]$AcceptExisting,
    [switch]$Status,
    [switch]$ListServers
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $projectRoot 'offline_data'
}
else {
    $DataRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DataRoot)
}
$Downloads = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Downloads)

function Invoke-ChronicleAssistant {
    param([string[]]$AssistantArguments)

    Push-Location $projectRoot
    try {
        & py -B -m o2o_dps.chronicle_export_assistant @AssistantArguments
        if ($LASTEXITCODE -ne 0) {
            throw "Chronicle export assistant exited with code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

function Invoke-ChronicleManifest {
    param([string[]]$ManifestArguments)

    Push-Location $projectRoot
    try {
        & py -B -m o2o_dps.chronicle_board_manifest @ManifestArguments
        if ($LASTEXITCODE -ne 0) {
            throw "Chronicle PDF manifest builder exited with code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

if ($ListServers) {
    Invoke-ChronicleAssistant @('servers')
    return
}

if ($Status) {
    Invoke-ChronicleAssistant @('status', '--data-root', $DataRoot)
    return
}

$queuePath = Join-Path $DataRoot 'chronicle_raw\export_queue.json'
if (
    $Character.Count -eq 0 -and
    [string]::IsNullOrWhiteSpace($LeaderboardPdfDirectory) -and
    -not (Test-Path -LiteralPath $queuePath -PathType Leaf)
) {
    Write-Host 'Chronicle currently recognizes these server / realm names:'
    Invoke-ChronicleAssistant @('servers')
    Write-Host ''

    $serverInput = Read-Host 'Server (press Enter for Capybara)'
    if ([string]::IsNullOrWhiteSpace($serverInput)) {
        $Server = 'Capybara'
    }
    else {
        $Server = $serverInput.Trim()
    }
    $Realm = (Read-Host 'Realm (copy one of the names above exactly)').Trim()
    $characterInput = Read-Host 'Character name/GUID; separate multiple characters with commas'
    $Character = @(
        $characterInput.Split(',') |
            ForEach-Object { $_.Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
}

if (-not [string]::IsNullOrWhiteSpace($LeaderboardPdfDirectory)) {
    $resolvedPdfDirectory = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
        $LeaderboardPdfDirectory
    )
    $manifestPath = Join-Path $DataRoot 'chronicle_raw\leaderboard_manifest.json'
    Invoke-ChronicleManifest @(
        '--pdf-dir', $resolvedPdfDirectory,
        '--output', $manifestPath
    )
    Invoke-ChronicleAssistant @(
        'import-manifest', $manifestPath,
        '--data-root', $DataRoot,
        '--resolve-external-api'
    )
}

if ($Character.Count -gt 0) {
    if ([string]::IsNullOrWhiteSpace($Server)) {
        $Server = 'Capybara'
    }
    if ([string]::IsNullOrWhiteSpace($Realm)) {
        $Realm = (Read-Host 'Realm').Trim()
    }
    if ([string]::IsNullOrWhiteSpace($Realm)) {
        throw 'Realm must not be empty.'
    }

    $discoverArguments = @(
        'discover',
        '--server', $Server,
        '--realm', $Realm,
        '--data-root', $DataRoot
    )
    foreach ($characterName in $Character) {
        if (-not [string]::IsNullOrWhiteSpace($characterName)) {
            $discoverArguments += @('--character', $characterName.Trim())
        }
    }
    Invoke-ChronicleAssistant $discoverArguments
}

if ($DiscoverOnly) {
    return
}

if (-not (Test-Path -LiteralPath $queuePath -PathType Leaf)) {
    throw 'No export queue was created. Check the character, server, and realm shown above.'
}

$collectArguments = @(
    'collect',
    '--downloads', $Downloads,
    '--data-root', $DataRoot,
    '--limit', $Limit.ToString(),
    '--timeout-minutes', $TimeoutMinutes.ToString([Globalization.CultureInfo]::InvariantCulture)
)
if ($NoBrowser) {
    $collectArguments += '--no-browser'
}
if ($AcceptExisting) {
    $collectArguments += '--accept-existing'
}
Invoke-ChronicleAssistant $collectArguments
