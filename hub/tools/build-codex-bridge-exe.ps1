param(
  [string]$Name = "CodexBridge",
  [string]$BuildRoot = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Entry = Join-Path $PSScriptRoot "codex_bridge_main.py"
if (-not $BuildRoot) { $BuildRoot = Join-Path $RepoRoot "build" }
$BuildDir = Join-Path $BuildRoot "codex-bridge"
$SpecDir = $BuildRoot
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
  if ($LASTEXITCODE -ne 0) { throw "CodexBridge build failed" }
  uv run --with pyinstaller --with pillow --with pystray --with "pywinauto>=0.6.9" python -m PyInstaller `
    --noconfirm --clean --onefile --console --name HubAgentRuntime `
    --paths $RepoRoot --distpath $DistDir `
    --workpath (Join-Path $BuildRoot "agent-runtime") --specpath $SpecDir `
    (Join-Path $PSScriptRoot "agent_runtime_main.py")
  if ($LASTEXITCODE -ne 0) { throw "HubAgentRuntime build failed" }
  uv run --with pyinstaller --with "pywebview==6.2.1" python -m PyInstaller `
    --noconfirm --clean --onefile --windowed --name HubDesktop `
    --collect-all webview --icon $IconPath `
    --add-data "$AssetsDir;codex_bridge/assets" `
    --paths $RepoRoot --distpath $DistDir `
    --workpath (Join-Path $BuildRoot "desktop-window") --specpath $SpecDir `
    (Join-Path $PSScriptRoot "hub_desktop_main.py")
  if ($LASTEXITCODE -ne 0) { throw "HubDesktop build failed" }
} finally {
  Pop-Location
}
