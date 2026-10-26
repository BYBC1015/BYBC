param(
  [string]$Name = "CodexBridge"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Entry = Join-Path $PSScriptRoot "codex_bridge_main.py"
$BuildDir = Join-Path $RepoRoot "build\codex-bridge"
$SpecDir = Join-Path $RepoRoot "build"
$DistDir = Join-Path $RepoRoot "dist"
$AssetsDir = Join-Path $RepoRoot "codex_bridge\assets"
$IconPath = Join-Path $AssetsDir "codex-owl.ico"

Push-Location $RepoRoot
try {
  uv run --with pyinstaller --with pillow --with pystray --with "pywinauto>=0.6.9" python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --collect-submodules pystray `
    --name $Name `
    --icon $IconPath `
    --add-data "$AssetsDir;codex_bridge/assets" `
    --paths $RepoRoot `
    --distpath $DistDir `
    --workpath $BuildDir `
    --specpath $SpecDir `
    $Entry
} finally {
  Pop-Location
}
