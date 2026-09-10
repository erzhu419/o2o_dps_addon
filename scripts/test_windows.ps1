[CmdletBinding()]
param(
    [string]$ToolRoot = (Join-Path $env:LOCALAPPDATA 'O2O-DPS'),
    [string]$SimulatorRoot,
    [string]$AddonRoot,
    [string]$DeployedCat2Root
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$workspaceRoot = (Resolve-Path (Join-Path $projectRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($SimulatorRoot)) {
    $SimulatorRoot = Join-Path $workspaceRoot 'wowsims-turtle'
}
if ([string]::IsNullOrWhiteSpace($AddonRoot)) {
    $AddonRoot = $workspaceRoot
}
if ([string]::IsNullOrWhiteSpace($DeployedCat2Root)) {
    $DeployedCat2Root = Join-Path $workspaceRoot '..\Cat2'
}
$simulatorRoot = (Resolve-Path -LiteralPath $SimulatorRoot).Path
$addonRoot = (Resolve-Path -LiteralPath $AddonRoot).Path
$deployedCat2 = (Resolve-Path -LiteralPath $DeployedCat2Root).Path
$goRoot = Join-Path $ToolRoot 'go1.23.4\go'
$goExecutable = Join-Path $goRoot 'bin\go.exe'
if (-not (Test-Path -LiteralPath $goExecutable -PathType Leaf)) {
    throw "Portable Go is missing. Run .\scripts\setup_windows.ps1 first."
}

$env:GOROOT = $goRoot
$env:GOTOOLCHAIN = 'local'
$env:CGO_ENABLED = '0'
$env:PATH = "$(Join-Path $goRoot 'bin');$env:PATH"

Push-Location $simulatorRoot
try {
    & $goExecutable test --tags=with_db .\sim\core\... .\sim\warrior .\sim\druid\balance .\sim\o2o .\cmd\o2obridge .\cmd\o2ofixture
    if ($LASTEXITCODE -ne 0) {
        throw 'Go simulator tests failed'
    }
}
finally {
    Pop-Location
}

Push-Location $projectRoot
try {
    py -B -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw 'Python tests failed'
    }
}
finally {
    Pop-Location
}

$toc = Join-Path $addonRoot 'BrainOfCat.toc'
foreach ($line in Get-Content -LiteralPath $toc -Encoding utf8) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith('#')) {
        continue
    }
    $loadedFile = Join-Path $addonRoot $trimmed
    if (-not (Test-Path -LiteralPath $loadedFile -PathType Leaf)) {
        throw "BrainOfCat.toc references a missing file: $trimmed"
    }
}

$registry = Join-Path $deployedCat2 'Core\CardRegistry.lua'
$runner = Join-Path $deployedCat2 'Core\ConfigurationRunner.lua'
if (-not (Select-String -LiteralPath $registry -SimpleMatch 'function Cat2.RegisterCard' -Quiet)) {
    throw 'The deployed Cat2 does not expose Cat2.RegisterCard'
}
if (-not (Select-String -LiteralPath $registry -SimpleMatch 'function Cat2.ExecuteCardById' -Quiet)) {
    throw 'The deployed Cat2 does not expose Cat2.ExecuteCardById'
}
if (-not (Select-String -LiteralPath $runner -SimpleMatch 'Cat2.CurrentExecutionContext = context' -Quiet)) {
    throw 'The deployed Cat2 does not expose the expected execution context'
}

$requiredCards = @(
    'Cards\Warrior\HeroicStrike.lua',
    'Cards\Warrior\Cleave.lua',
    'Cards\Warrior\Execute.lua',
    'Cards\Warrior\Bloodthirst.lua',
    'Cards\Warrior\Whirlwind.lua'
)
foreach ($relativePath in $requiredCards) {
    if (-not (Test-Path -LiteralPath (Join-Path $deployedCat2 $relativePath) -PathType Leaf)) {
        throw "The deployed Cat2 is missing $relativePath"
    }
}

Write-Output 'Full integration suite passed: simulator, Python pipeline, addon TOC, and deployed Cat2 contracts.'
