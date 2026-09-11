[CmdletBinding()]
param(
    [string]$ToolRoot = (Join-Path $env:LOCALAPPDATA 'O2O-DPS'),
    [string]$BridgeFileName = 'o2obridge.seedfix-v3.withdb.goamd64v1.linux-amd64'
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
if (-not (Test-Path -LiteralPath $simulatorRoot -PathType Container)) {
    throw "wowsims-turtle source checkout is missing: $simulatorRoot"
}

$env:GOROOT = $goRoot
$env:GOTOOLCHAIN = 'local'
$env:CGO_ENABLED = '0'
$env:GOOS = 'linux'
$env:GOARCH = 'amd64'
$env:GOAMD64 = 'v1'
$env:GOFLAGS = ''
$env:PATH = "$(Join-Path $goRoot 'bin');$env:PATH"

$binaryDirectory = Join-Path $projectRoot 'bin'
New-Item -ItemType Directory -Force -Path $binaryDirectory | Out-Null

if ([System.IO.Path]::GetFileName($BridgeFileName) -cne $BridgeFileName) {
    throw 'BridgeFileName must be a leaf filename inside o2o-dps\bin.'
}
$forbiddenBridgeNames = @(
    'o2obridge',
    'o2obridge.linux-amd64',
    'o2obridge.seedfix-v1.linux-amd64'
)
if ($forbiddenBridgeNames -icontains $BridgeFileName) {
    throw "BridgeFileName is a forbidden legacy, failed, or unversioned artifact: $BridgeFileName"
}
$versionedNamePattern = '^o2obridge\.seedfix-v[1-9][0-9]*\.withdb\.goamd64v1\.linux-amd64$'
if ($BridgeFileName -cnotmatch $versionedNamePattern) {
    throw 'BridgeFileName must explicitly version seedfix, with_db, and GOAMD64=v1 semantics.'
}

$bridge = Join-Path $binaryDirectory $BridgeFileName
$temporaryBridge = Join-Path $binaryDirectory ('.o2obridge-linux-build-' + [guid]::NewGuid().ToString('N') + '.tmp')

function Install-ImmutableArtifact {
    param(
        [Parameter(Mandatory = $true)][string]$StagedPath,
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (Test-Path -LiteralPath $TargetPath) {
        if (-not (Test-Path -LiteralPath $TargetPath -PathType Leaf)) {
            throw "$Label target exists but is not a file: $TargetPath"
        }
        $existingHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $TargetPath).Hash
        $stagedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $StagedPath).Hash
        if ($existingHash -cne $stagedHash) {
            throw "$Label already exists with different bytes. Choose a new versioned filename; refusing to overwrite $TargetPath"
        }
        Remove-Item -LiteralPath $StagedPath
        return 'REUSED_IDENTICAL'
    }
    Move-Item -LiteralPath $StagedPath -Destination $TargetPath
    return 'INSTALLED_NEW'
}

$installStatus = $null
Push-Location $simulatorRoot
try {
    & $goExecutable build -buildvcs=false -trimpath --tags=with_db '-ldflags=-s -w -buildid=' -o $temporaryBridge .\cmd\o2obridge
    if ($LASTEXITCODE -ne 0) {
        throw 'Linux o2obridge build failed'
    }
    $installStatus = Install-ImmutableArtifact -StagedPath $temporaryBridge -TargetPath $bridge -Label 'Linux simulator bridge'
}
finally {
    Pop-Location
    if (Test-Path -LiteralPath $temporaryBridge -PathType Leaf) {
        Remove-Item -LiteralPath $temporaryBridge
    }
}

Write-Output "bridge=$bridge"
Write-Output "bridge_install=$installStatus"
Write-Output "bridge_size_bytes=$((Get-Item -LiteralPath $bridge).Length)"
Write-Output "bridge_sha256=$((Get-FileHash -Algorithm SHA256 -LiteralPath $bridge).Hash.ToLowerInvariant())"
Write-Output 'build_contract=GOOS=linux;GOARCH=amd64;GOAMD64=v1;CGO_ENABLED=0;GOFLAGS=empty;buildvcs=false;trimpath=true;tags=with_db;buildid=empty'
