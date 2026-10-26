param(
  [ValidateRange(0, 120)]
  [int]$DelaySeconds = 12
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
$repairScript = Join-Path $PSScriptRoot "repair_codex_project_assignments.py"
$codexPackagePrefix = "C:\Program Files\WindowsApps\OpenAI.Codex_"
$codexAppId = "shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"

function Get-CodexPackageProcesses {
  @(
    Get-Process -ErrorAction SilentlyContinue |
      Where-Object {
        $_.Path -and $_.Path.StartsWith(
          $codexPackagePrefix,
          [System.StringComparison]::OrdinalIgnoreCase
        )
      }
  )
}

Start-Sleep -Seconds $DelaySeconds

try {
  $targets = Get-CodexPackageProcesses
  foreach ($target in $targets) {
    Stop-Process -Id $target.Id -Force -ErrorAction SilentlyContinue
  }

  $deadline = [DateTime]::UtcNow.AddSeconds(30)
  do {
    Start-Sleep -Milliseconds 500
    $remaining = Get-CodexPackageProcesses
  } while ($remaining.Count -gt 0 -and [DateTime]::UtcNow -lt $deadline)

  if ($remaining.Count -gt 0) {
    throw "Codex package processes did not stop within 30 seconds."
  }

  if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Workspace Python was not found: $python"
  }
  if (-not (Test-Path -LiteralPath $repairScript -PathType Leaf)) {
    throw "Project repair script was not found: $repairScript"
  }

  & $python $repairScript --apply
  if ($LASTEXITCODE -ne 0) {
    throw "Project assignment repair failed with exit code $LASTEXITCODE."
  }
}
finally {
  Start-Process -FilePath "$env:WINDIR\explorer.exe" -ArgumentList $codexAppId
}
