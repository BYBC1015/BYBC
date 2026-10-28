# Dedicated Hub worker launcher. The worker's own named mutex also rejects
# duplicate launches; existing execution receipts and configuration are reused.
param([string]$ConfigPath = (Join-Path $env:APPDATA 'CodexHubAgents\config.json'))
$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $PSScriptRoot
$workerPython = Join-Path $packageRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $workerPython) -or -not (Test-Path -LiteralPath $ConfigPath)) {
  throw 'Hub worker runtime or configuration is missing.'
}
$workerConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$existing = @(Get-CimInstance Win32_Process | Where-Object {
  $_.Name -like 'python*' -and $_.CommandLine -like '*-m codex_bridge.agent_worker*' -and $_.CommandLine.Contains($workerConfig)
})
if ($existing.Count) { return }
$logFolder = Split-Path -Parent $workerConfig
$logStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$workerArguments = '-X utf8 -m codex_bridge.agent_worker --config "' + $workerConfig + '"'
Start-Process -FilePath $workerPython -ArgumentList $workerArguments -WorkingDirectory $packageRoot -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $logFolder "worker-$logStamp.log") `
  -RedirectStandardError (Join-Path $logFolder "worker-$logStamp-error.log") | Out-Null
