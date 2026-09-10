[CmdletBinding()]
param(
    [string]$ToolRoot = (Join-Path $env:LOCALAPPDATA 'O2O-DPS')
)

$ErrorActionPreference = 'Stop'
$goVersion = '1.23.4'
$releaseName = "go$goVersion.windows-amd64.zip"
$releaseUrl = "https://go.dev/dl/$releaseName"
$versionRoot = Join-Path $ToolRoot "go$goVersion"
$goExecutable = Join-Path $versionRoot 'go\bin\go.exe'
$archivePath = Join-Path $ToolRoot $releaseName

if (-not (Test-Path -LiteralPath $goExecutable -PathType Leaf)) {
    if (Test-Path -LiteralPath $versionRoot) {
        throw "Incomplete Go directory exists at $versionRoot. Inspect or remove that exact directory, then retry."
    }
    New-Item -ItemType Directory -Force -Path $ToolRoot | Out-Null
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        Invoke-WebRequest -Uri $releaseUrl -OutFile $archivePath
    }
    New-Item -ItemType Directory -Path $versionRoot | Out-Null
    Expand-Archive -LiteralPath $archivePath -DestinationPath $versionRoot
}

$reportedVersion = & $goExecutable version
if ($LASTEXITCODE -ne 0) {
    throw "Go failed to start from $goExecutable"
}
if ($reportedVersion -notmatch 'go1\.23\.4 windows/amd64') {
    throw "Expected Go 1.23.4 windows/amd64, got: $reportedVersion"
}

Write-Output $reportedVersion
Write-Output "GOROOT=$versionRoot\go"
