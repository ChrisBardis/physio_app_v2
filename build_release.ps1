[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$buildVenv = Join-Path $projectRoot ".build-venv"
$buildPython = Join-Path $buildVenv "Scripts\python.exe"

function Confirm-ProjectChildPath {
    param([Parameter(Mandatory)][string]$Path)
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $rootPrefix = $projectRoot.TrimEnd('\') + '\'
    if (-not $fullPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $fullPath"
    }
    return $fullPath
}

function Remove-BuildArtifact {
    param([Parameter(Mandatory)][string]$Path)
    $safePath = Confirm-ProjectChildPath -Path $Path
    if (Test-Path -LiteralPath $safePath) {
        Remove-Item -LiteralPath $safePath -Recurse -Force
    }
}

Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath $buildPython)) {
    py -3 -m venv $buildVenv
}

& $buildPython -m pip install --disable-pip-version-check --upgrade pip
& $buildPython -m pip install --disable-pip-version-check `
    -r (Join-Path $projectRoot "requirements-lock.txt") `
    -r (Join-Path $projectRoot "requirements-build.txt")

$versionText = Get-Content -LiteralPath (Join-Path $projectRoot "version.py") -Raw
$versionMatch = [regex]::Match($versionText, '__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"')
if (-not $versionMatch.Success) {
    throw "Could not read the application version from version.py"
}
$version = $versionMatch.Groups[1].Value

$iconPath = Join-Path $projectRoot "assets\fysio.ico"
if (-not (Test-Path -LiteralPath $iconPath)) {
    throw "Missing Windows icon: $iconPath"
}

Remove-BuildArtifact -Path (Join-Path $projectRoot "build")
Remove-BuildArtifact -Path (Join-Path $projectRoot "dist\Fysio")

$releaseDir = Join-Path $projectRoot "release"
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
Get-ChildItem -LiteralPath $releaseDir -Filter "Fysio_Setup*.exe" -File -ErrorAction SilentlyContinue |
    ForEach-Object {
        $safeInstaller = Confirm-ProjectChildPath -Path $_.FullName
        Remove-Item -LiteralPath $safeInstaller -Force
    }

& $buildPython -m PyInstaller --noconfirm --clean (Join-Path $projectRoot "fysio.spec")

$executable = Join-Path $projectRoot "dist\Fysio\fysio.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "PyInstaller did not create $executable"
}
Write-Host "Created: $executable"

$isccCandidates = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

$iscc = $isccCandidates | Select-Object -First 1
if (-not $iscc) {
    Write-Warning "ISCC.exe was not found. The executable build is complete; installer\fysio.iss is ready to compile."
    exit 0
}

& $iscc "/DAppVersion=$version" (Join-Path $projectRoot "installer\fysio.iss")
$installer = Join-Path $releaseDir "Fysio_Setup.exe"
if (-not (Test-Path -LiteralPath $installer)) {
    throw "Inno Setup did not create $installer"
}
Write-Host "Created: $installer"
