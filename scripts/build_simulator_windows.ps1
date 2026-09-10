[CmdletBinding()]
param(
    [string]$ToolRoot = (Join-Path $env:LOCALAPPDATA 'O2O-DPS'),
    [string]$BridgeFileName = 'o2obridge.seedfix-v1.exe',
    [string]$FixtureFileName = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$workspaceRoot = (Resolve-Path (Join-Path $projectRoot '..')).Path
$simulatorRoot = Join-Path $workspaceRoot 'wowsims-turtle'
$goRoot = Join-Path $ToolRoot 'go1.23.4\go'
$goExecutable = Join-Path $goRoot 'bin\go.exe'
if (-not (Test-Path -LiteralPath $goExecutable -PathType Leaf)) {
    throw "Portable Go is missing. Run .\scripts\setup_windows.ps1 first."
}

$env:GOROOT = $goRoot
$env:GOTOOLCHAIN = 'local'
$env:CGO_ENABLED = '0'
$env:PATH = "$(Join-Path $goRoot 'bin');$env:PATH"

$binaryDirectory = Join-Path $projectRoot 'bin'
$configDirectory = Join-Path $projectRoot 'configs\wowsims'
New-Item -ItemType Directory -Force -Path $binaryDirectory, $configDirectory | Out-Null

if ([System.IO.Path]::GetFileName($BridgeFileName) -ne $BridgeFileName) {
    throw 'BridgeFileName must be a leaf filename inside o2o-dps\bin.'
}
if ($BridgeFileName -ieq 'o2obridge.exe') {
    throw 'o2obridge.exe is a frozen legacy artifact and must never be overwritten.'
}
if (-not $BridgeFileName.EndsWith('.exe', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'BridgeFileName must end in .exe.'
}
if ($FixtureFileName) {
    if ([System.IO.Path]::GetFileName($FixtureFileName) -ne $FixtureFileName) {
        throw 'FixtureFileName must be a leaf filename inside o2o-dps\configs\wowsims.'
    }
    if ($FixtureFileName -ieq 'fury_warrior_phase1.json') {
        throw 'fury_warrior_phase1.json is a frozen historical input and must never be overwritten.'
    }
    if (-not $FixtureFileName.EndsWith('.json', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'FixtureFileName must end in .json.'
    }
}

$bridge = Join-Path $binaryDirectory $BridgeFileName
$temporaryBridge = Join-Path $binaryDirectory ('.o2obridge-build-' + [guid]::NewGuid().ToString('N') + '.exe')
$fixture = if ($FixtureFileName) { Join-Path $configDirectory $FixtureFileName } else { $null }
$temporaryFixture = if ($FixtureFileName) { Join-Path $configDirectory ('.o2ofixture-build-' + [guid]::NewGuid().ToString('N') + '.json') } else { $null }

function Install-ImmutableArtifact {
    param(
        [Parameter(Mandatory = $true)][string]$StagedPath,
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (Test-Path -LiteralPath $TargetPath -PathType Leaf) {
        $existingHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $TargetPath).Hash
        $stagedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $StagedPath).Hash
        if ($existingHash -ne $stagedHash) {
            throw "$Label already exists with different bytes. Choose a new versioned filename; refusing to overwrite $TargetPath"
        }
        Remove-Item -LiteralPath $StagedPath
        return
    }
    Move-Item -LiteralPath $StagedPath -Destination $TargetPath
}

Push-Location $simulatorRoot
try {
    & $goExecutable build --tags=with_db -o $temporaryBridge .\cmd\o2obridge
    if ($LASTEXITCODE -ne 0) {
        throw 'o2obridge build failed'
    }
    Install-ImmutableArtifact -StagedPath $temporaryBridge -TargetPath $bridge -Label 'Simulator bridge'

    if ($FixtureFileName) {
        & $goExecutable run --tags=with_db .\cmd\o2ofixture -out $temporaryFixture
        if ($LASTEXITCODE -ne 0) {
            throw 'RaidSimRequest fixture generation failed'
        }
        Install-ImmutableArtifact -StagedPath $temporaryFixture -TargetPath $fixture -Label 'RaidSimRequest fixture'
    }
}
finally {
    Pop-Location
    if (Test-Path -LiteralPath $temporaryBridge -PathType Leaf) {
        Remove-Item -LiteralPath $temporaryBridge
    }
    if ($temporaryFixture -and (Test-Path -LiteralPath $temporaryFixture -PathType Leaf)) {
        Remove-Item -LiteralPath $temporaryFixture
    }
}

Write-Output "bridge=$bridge"
Write-Output "bridge_sha256=$((Get-FileHash -Algorithm SHA256 -LiteralPath $bridge).Hash.ToLowerInvariant())"
if ($fixture) {
    Write-Output "fixture=$fixture"
    Write-Output "fixture_sha256=$((Get-FileHash -Algorithm SHA256 -LiteralPath $fixture).Hash.ToLowerInvariant())"
}
else {
    Write-Output 'fixture=not_generated'
}
