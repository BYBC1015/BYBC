param(
  [string]$Hub = $env:CODEX_HUB_BASE,
  [string]$Token = $env:CODEX_HUB_TOKEN,
  [int]$IntervalSeconds = 30,
  [bool]$AutoAck = $true,
  [bool]$IgnoreExistingOnFirstRun = $true,
  [bool]$WritePromptFiles = $true,
  [switch]$OpenCodex,
  [string]$ProjectMapPath = "",
  [string]$ProjectRoot = "",
  [bool]$AutoCreateMissingProjects = $true,
  [int]$CodexPromptMaxChars = 1800,
  [string]$StatePath = "",
  [string]$PromptOutDir = "",
  [switch]$AllowAlongsideCodexBridge,
  [switch]$Once
)

$ErrorActionPreference = "Stop"

if (-not $AllowAlongsideCodexBridge) {
  $runningBridge = @(Get-Process -Name "CodexBridge" -ErrorAction SilentlyContinue)
  if ($runningBridge.Count -gt 0) {
    throw "CodexBridge.exe is already running. Stop it before using the legacy PowerShell agent, or pass -AllowAlongsideCodexBridge only for deliberate diagnostics."
  }
}

if ([string]::IsNullOrWhiteSpace($Hub)) {
  $Hub = "https://203.0.113.10/hub"
}
$Hub = $Hub.TrimEnd("/")

if ([string]::IsNullOrWhiteSpace($Token)) {
  throw "Missing Codex Hub API token. Pass -Token or set CODEX_HUB_TOKEN."
}

if ([string]::IsNullOrWhiteSpace($StatePath)) {
  $StatePath = Join-Path $PSScriptRoot "codex-device-agent-state.json"
}

if ([string]::IsNullOrWhiteSpace($PromptOutDir)) {
  $PromptOutDir = Join-Path $PSScriptRoot "agent-inbox"
}

if ([string]::IsNullOrWhiteSpace($ProjectMapPath)) {
  $ProjectMapPath = Join-Path $PSScriptRoot "codex-project-map.json"
}

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
  $ProjectRoot = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "CodexProjects"
}

$Headers = @{ Authorization = "Bearer $Token" }
$ProjectMapCache = $null
$ProjectMapLoaded = $false

function ConvertTo-StringArray($Value) {
  if ($null -eq $Value) {
    return @()
  }
  if ($Value -is [hashtable] -and $Value.Count -eq 0) {
    return @()
  }
  if (
    $Value -is [pscustomobject] -and
    @($Value.PSObject.Properties).Count -eq 0
  ) {
    return @()
  }
  return @($Value | ForEach-Object { [string]$_ })
}

function Read-AgentState {
  if (Test-Path -LiteralPath $StatePath) {
    $raw = Get-Content -LiteralPath $StatePath -Raw
    if ($raw.Trim()) {
      $loaded = $raw | ConvertFrom-Json
      return @{
        Initialized = [bool]$loaded.Initialized
        SeenMentionIds = ConvertTo-StringArray $loaded.SeenMentionIds
        AckedMentionIds = ConvertTo-StringArray $loaded.AckedMentionIds
        SeenTaskIds = ConvertTo-StringArray $loaded.SeenTaskIds
        PromptedMentionIds = ConvertTo-StringArray $loaded.PromptedMentionIds
        PromptedTaskIds = ConvertTo-StringArray $loaded.PromptedTaskIds
      }
    }
  }
  return @{
    Initialized = $false
    SeenMentionIds = @()
    AckedMentionIds = @()
    SeenTaskIds = @()
    PromptedMentionIds = @()
    PromptedTaskIds = @()
  }
}

function ConvertTo-StableAgentStatePayload($State) {
  return [ordered]@{
    Initialized = [bool]$State["Initialized"]
    SeenMentionIds = @(ConvertTo-StringArray $State["SeenMentionIds"])
    AckedMentionIds = @(ConvertTo-StringArray $State["AckedMentionIds"])
    SeenTaskIds = @(ConvertTo-StringArray $State["SeenTaskIds"])
    PromptedMentionIds = @(ConvertTo-StringArray $State["PromptedMentionIds"])
    PromptedTaskIds = @(ConvertTo-StringArray $State["PromptedTaskIds"])
  }
}

function Save-AgentState($State) {
  $dir = Split-Path -Parent $StatePath
  if ($dir -and -not (Test-Path -LiteralPath $dir)) {
    New-Item -ItemType Directory -Path $dir | Out-Null
  }
  ConvertTo-StableAgentStatePayload $State |
    ConvertTo-Json -Depth 5 |
    Set-Content -LiteralPath $StatePath -Encoding UTF8
}

function Add-StateId($State, [string]$Name, [string]$Id) {
  if ($State[$Name] -notcontains $Id) {
    $State[$Name] = @($State[$Name]) + $Id
  }
}

function ConvertTo-SafeFileName([string]$Value) {
  $clean = $Value -replace "[^A-Za-z0-9._-]+", "-"
  $clean = $clean.Trim("-")
  if ([string]::IsNullOrWhiteSpace($clean)) {
    return "item"
  }
  return $clean.ToLowerInvariant()
}

function ConvertTo-UrlEncoded([string]$Value) {
  return [System.Uri]::EscapeDataString($Value)
}

function Ensure-PromptOutDir {
  if (-not (Test-Path -LiteralPath $PromptOutDir)) {
    New-Item -ItemType Directory -Path $PromptOutDir | Out-Null
  }
}

function Invoke-HubGet([string]$Path) {
  Invoke-RestMethod -Method GET -Uri "$Hub$Path" -Headers $Headers
}

function Invoke-HubGetText([string]$Path) {
  $response = Invoke-WebRequest -Method GET -Uri "$Hub$Path" -Headers $Headers -UseBasicParsing
  return $response.Content
}

function Invoke-HubPostJson([string]$Path, [hashtable]$Body) {
  $json = $Body | ConvertTo-Json -Compress
  Invoke-RestMethod `
    -Method POST `
    -Uri "$Hub$Path" `
    -Headers $Headers `
    -ContentType "application/json; charset=utf-8" `
    -Body $json
}

function Read-ProjectMap {
  if ($script:ProjectMapLoaded) {
    return $script:ProjectMapCache
  }
  $script:ProjectMapLoaded = $true
  if (-not (Test-Path -LiteralPath $ProjectMapPath)) {
    Write-Warning "Project map not found: $ProjectMapPath"
    return $null
  }
  $raw = Get-Content -LiteralPath $ProjectMapPath -Raw
  if (-not $raw.Trim()) {
    Write-Warning "Project map is empty: $ProjectMapPath"
    return $null
  }
  $script:ProjectMapCache = $raw | ConvertFrom-Json
  return $script:ProjectMapCache
}

function Test-SafeProjectSlug([string]$ProjectSlug) {
  return $ProjectSlug -match "^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$"
}

function Ensure-ProjectFolder([string]$ProjectSlug, [string]$LocalFolder) {
  if ([string]::IsNullOrWhiteSpace($LocalFolder)) {
    return $null
  }

  if (-not (Test-Path -LiteralPath $LocalFolder -PathType Container)) {
    if (-not $AutoCreateMissingProjects) {
      Write-Warning "Mapped local folder does not exist: $LocalFolder"
      return $null
    }
    New-Item -ItemType Directory -Path $LocalFolder -Force | Out-Null
  }

  return (Resolve-Path -LiteralPath $LocalFolder).Path
}

function Get-CodexTargetProjectSlugFromPrompt([string]$PromptPath) {
  if (-not (Test-Path -LiteralPath $PromptPath)) {
    return $null
  }
  $raw = Get-Content -LiteralPath $PromptPath -Raw
  $match = [regex]::Match(
    $raw,
    "Codex target project slug:\s*([a-z0-9][a-z0-9-]{1,62}[a-z0-9])",
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
  )
  if (-not $match.Success) {
    return $null
  }
  return $match.Groups[1].Value.ToLowerInvariant()
}

function Get-ProjectLocalFolder([string]$ProjectSlug) {
  $ProjectSlug = $ProjectSlug.ToLowerInvariant()
  if (-not (Test-SafeProjectSlug $ProjectSlug)) {
    Write-Warning "Unsafe project slug: $ProjectSlug"
    return $null
  }

  $map = Read-ProjectMap
  $projectConfig = $null
  if ($null -ne $map -and $map.PSObject.Properties.Name -contains "projects") {
    $projectConfig = $map.projects.PSObject.Properties[$ProjectSlug].Value
  } elseif ($null -ne $map -and $map.PSObject.Properties.Name -contains $ProjectSlug) {
    $projectConfig = $map.PSObject.Properties[$ProjectSlug].Value
  }

  if ($null -eq $projectConfig) {
    $effectiveAutoCreate = $AutoCreateMissingProjects
    if ($null -ne $map -and $map.PSObject.Properties.Name -contains "auto_create_missing_projects") {
      $effectiveAutoCreate = [bool]$map.auto_create_missing_projects
    }
    if (-not $effectiveAutoCreate) {
      Write-Warning "No local folder mapping for project: $ProjectSlug"
      return $null
    }
    $effectiveProjectRoot = $ProjectRoot
    if ($null -ne $map -and $map.PSObject.Properties.Name -contains "project_root") {
      $effectiveProjectRoot = [string]$map.project_root
    }
    $rootPath = [System.IO.Path]::GetFullPath($effectiveProjectRoot)
    $localFolder = [System.IO.Path]::GetFullPath((Join-Path $rootPath $ProjectSlug))
    if (-not $localFolder.StartsWith($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
      Write-Warning "Resolved project path escaped project root: $ProjectSlug"
      return $null
    }
    return Ensure-ProjectFolder $ProjectSlug $localFolder
  }

  $localFolder = $null
  if ($projectConfig -is [string]) {
    $localFolder = $projectConfig
  } elseif ($projectConfig.PSObject.Properties.Name -contains "local_folder") {
    $localFolder = [string]$projectConfig.local_folder
  }

  if ([string]::IsNullOrWhiteSpace($localFolder)) {
    Write-Warning "Project mapping has no local_folder: $ProjectSlug"
    return $null
  }

  return Ensure-ProjectFolder $ProjectSlug $localFolder
}

function New-CodexLaunchPrompt($Item, [string]$Kind, [string]$DeviceLabel, [string]$PromptPath) {
  $title = if ($Kind -eq "task") { "#$($Item.id) $($Item.title)" } else { "mention #$($Item.id)" }
  $prompt = @"
# Codex Hub $Kind

You are the Codex session for device "$DeviceLabel".
Open and follow the local prompt file below. Work only on this Hub item.

Project: $($Item.project_name) ($($Item.project_slug))
Item: $title
Hub URL: $Hub/projects/$($Item.project_slug)
Prompt file:
$PromptPath

Do not touch SW /upload or /u unless the prompt explicitly says to do so.
When finished, write the result back to Codex Hub.
"@
  if ($prompt.Length -le $CodexPromptMaxChars) {
    return $prompt
  }
  return @"
# Codex Hub $Kind

Read this local prompt file and complete only that Hub item:
$PromptPath

Do not touch SW /upload or /u unless explicitly instructed.
"@
}

function New-CodexDeepLink([string]$LocalFolder, [string]$PromptText) {
  $resolvedFolder = (Resolve-Path -LiteralPath $localFolder).Path
  $encodedPath = ConvertTo-UrlEncoded $resolvedFolder
  $encodedPrompt = ConvertTo-UrlEncoded $PromptText
  return "codex://new?path=$encodedPath&prompt=$encodedPrompt"
}

function Open-CodexForHubItem($Item, [string]$Kind, [string]$DeviceLabel, [string]$PromptPath) {
  if (-not $OpenCodex) {
    return $false
  }
  $projectSlug = [string]$Item.project_slug
  if ($Kind -eq "task") {
    $targetSlug = Get-CodexTargetProjectSlugFromPrompt $PromptPath
    if (-not [string]::IsNullOrWhiteSpace($targetSlug)) {
      $projectSlug = $targetSlug
    }
  }
  $localFolder = Get-ProjectLocalFolder $projectSlug
  if ($null -eq $localFolder) {
    return $false
  }
  $promptText = New-CodexLaunchPrompt $Item $Kind $DeviceLabel $PromptPath
  $deepLink = New-CodexDeepLink $localFolder $promptText
  try {
    Start-Process -FilePath $deepLink
    Write-Host "CODEX DEEP LINK OPENED $Kind #$($Item.id) [$($Item.project_slug)]"
    return $true
  } catch {
    Write-Warning "CODEX DEEP LINK FAILED $Kind #$($Item.id): $($_.Exception.Message)"
    return $false
  }
}

function Claim-HubTask($Task) {
  try {
    $response = Invoke-HubPostJson "/api/projects/$($task.project_slug)/tasks/$($task.id)/claim" @{}
    Write-Host "CLAIMED TASK #$($task.id)"
    return $response.task
  } catch {
    Write-Warning "TASK CLAIM FAILED #$($task.id): $($_.Exception.Message)"
    return $null
  }
}

function Claim-HubMention($Mention) {
  try {
    $response = Invoke-HubPostJson "/api/projects/$($mention.project_slug)/mentions/$($mention.id)/claim" @{}
    Write-Host "CLAIMED MENTION #$($mention.id)"
    return $response.mention
  } catch {
    Write-Warning "MENTION CLAIM FAILED #$($mention.id): $($_.Exception.Message)"
    return $null
  }
}

function Mark-HubMentionHandled($Mention) {
  try {
    Invoke-HubPostJson "/api/projects/$($mention.project_slug)/mentions/$($mention.id)/handled" @{} | Out-Null
    Write-Host "HANDLED MENTION #$($mention.id)"
    return $true
  } catch {
    Write-Warning "MENTION HANDLED FAILED #$($mention.id): $($_.Exception.Message)"
    return $false
  }
}

function Write-MentionPromptFile($Mention, [string]$DeviceLabel) {
  Ensure-PromptOutDir
  $safeProject = ConvertTo-SafeFileName $Mention.project_slug
  $fileName = "{0}-mention-{1}-{2}.md" -f (Get-Date -Format "yyyyMMdd-HHmmss"), $Mention.id, $safeProject
  $path = Join-Path $PromptOutDir $fileName
  $roleLines = "(none)"
  $assignedRoles = @($Mention.group_chat_roles)
  if ($assignedRoles.Count -gt 0) {
    $roleLines = ($assignedRoles | ForEach-Object {
      $instructions = if ($_.instructions) { $_.instructions } else { "Fulfill this named role for the request." }
      "- @$($_.name): $instructions"
    }) -join "`r`n"
  }
  $content = @"
# Codex Hub Mention

Target device: $DeviceLabel
Project: $($Mention.project_name) ($($Mention.project_slug))
Mention entry: #$($Mention.id)
From: $($Mention.source_label)
Created: $($Mention.created_at)

## Message

$($Mention.body_markdown)

## Assigned group chat roles

$roleLines

## Suggested Codex instruction

You are the Codex session for device "$DeviceLabel".
Read this mention, open Codex Hub if more context is needed, do only the requested work, then reply in the project conversation.

Hub URL:
$Hub/projects/$($Mention.project_slug)

API reply target:
POST $Hub/api/projects/$($Mention.project_slug)/entries
"@
  $content | Set-Content -LiteralPath $path -Encoding UTF8
  return $path
}

function Write-TaskPromptFile($Task, [string]$DeviceLabel) {
  Ensure-PromptOutDir
  $safeProject = ConvertTo-SafeFileName $Task.project_slug
  $fileName = "{0}-task-{1}-{2}.md" -f (Get-Date -Format "yyyyMMdd-HHmmss"), $Task.id, $safeProject
  $path = Join-Path $PromptOutDir $fileName

  $promptMarkdown = ""
  try {
    $promptMarkdown = Invoke-HubGetText "/api/projects/$($Task.project_slug)/tasks/$($Task.id)/prompt.md"
  } catch {
    $promptMarkdown = "Could not fetch task prompt markdown: $($_.Exception.Message)"
  }

  $content = @"
# Codex Hub Task

Target device: $DeviceLabel
Project: $($Task.project_name) ($($Task.project_slug))
Task: #$($Task.id) $($Task.title)
Priority: $($Task.priority)
Status: $($Task.status)
Assigned device: $($Task.assigned_device_label)
Requested group: $($Task.requested_group_name)
Claimed by: $($Task.claimed_device_label)

## Task description

$($Task.description)

## Bounded Codex prompt

$promptMarkdown
"@
  $content | Set-Content -LiteralPath $path -Encoding UTF8
  return $path
}

function Seed-ExistingInboxItems($Inbox, $State) {
  foreach ($task in @($Inbox.tasks)) {
    $taskId = [string]$task.id
    Add-StateId $State "SeenTaskIds" $taskId
    Add-StateId $State "PromptedTaskIds" $taskId
  }
  foreach ($mention in @($Inbox.mentions)) {
    $mentionId = [string]$mention.id
    Add-StateId $State "SeenMentionIds" $mentionId
    Add-StateId $State "AckedMentionIds" $mentionId
    Add-StateId $State "PromptedMentionIds" $mentionId
  }
  $State.Initialized = $true
  Save-AgentState $State
}

function Process-Inbox($State) {
  $Inbox = Invoke-HubGet "/api/inbox"
  $DeviceLabel = $Inbox.device.label
  $TaskCount = @($Inbox.tasks).Count
  $MentionCount = @($Inbox.mentions).Count
  $now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Write-Host "[$now] $DeviceLabel agent online. tasks=$TaskCount mentions=$MentionCount"

  if (-not $State.Initialized -and $IgnoreExistingOnFirstRun) {
    Seed-ExistingInboxItems $Inbox $State
    Write-Host "Initial inbox state seeded. Future mentions and tasks will trigger prompt files."
    return
  }

  foreach ($task in @($Inbox.tasks | Sort-Object id)) {
    $taskId = [string]$task.id
    if ($State.SeenTaskIds -notcontains $taskId) {
      Write-Host "NEW TASK #$($task.id) [$($task.project_slug)] $($task.title)"
      $claimedTask = Claim-HubTask $task
      if ($null -eq $claimedTask) {
        Add-StateId $State "SeenTaskIds" $taskId
        continue
      }
      $task = $claimedTask
      if (($WritePromptFiles -or $OpenCodex) -and $State.PromptedTaskIds -notcontains $taskId) {
        $promptPath = Write-TaskPromptFile $task $DeviceLabel
        Write-Host "TASK PROMPT $promptPath"
        Open-CodexForHubItem $task "task" $DeviceLabel $promptPath | Out-Null
        Add-StateId $State "PromptedTaskIds" $taskId
      }
      Add-StateId $State "SeenTaskIds" $taskId
    }
  }

  foreach ($mention in @($Inbox.mentions | Sort-Object id)) {
    $mentionId = [string]$mention.id
    if ($State.SeenMentionIds -notcontains $mentionId) {
      Write-Host "NEW MENTION #$($mention.id) [$($mention.project_slug)] from $($mention.source_label)"
      Write-Host $mention.body_markdown
      $claimedMention = Claim-HubMention $mention
      if ($null -eq $claimedMention) {
        Add-StateId $State "SeenMentionIds" $mentionId
        continue
      }
      $mention = $claimedMention
      if (($WritePromptFiles -or $OpenCodex) -and $State.PromptedMentionIds -notcontains $mentionId) {
        $promptPath = Write-MentionPromptFile $mention $DeviceLabel
        Write-Host "MENTION PROMPT $promptPath"
        Open-CodexForHubItem $mention "mention" $DeviceLabel $promptPath | Out-Null
        Add-StateId $State "PromptedMentionIds" $mentionId
      }
      Add-StateId $State "SeenMentionIds" $mentionId
    }

    if ($AutoAck -and $State.AckedMentionIds -notcontains $mentionId) {
      <#
      $ack = "$DeviceLabel 已收到 mention #$($mention.id)，本机设备代理已写入 Codex agent inbox，等待本机 Codex 线程监控读取和处理。"
      #>
      $ack = "$DeviceLabel received mention #$($mention.id). The local device agent wrote it to the Codex agent inbox and is waiting for local Codex handling."
      $ackBody = @{
        kind = "message"
        body_markdown = $ack
      }
      Invoke-HubPostJson "/api/projects/$($mention.project_slug)/entries" $ackBody | Out-Null
      Write-Host "ACKED MENTION #$($mention.id)"
      Add-StateId $State "AckedMentionIds" $mentionId
    }

    if ($State.SeenMentionIds -contains $mentionId) {
      Mark-HubMentionHandled $mention | Out-Null
    }
  }

  $State.Initialized = $true
  Save-AgentState $State
}

$State = Read-AgentState
Write-Host "Codex Hub device agent started."
Write-Host "Hub: $Hub"
Write-Host "State: $StatePath"
Write-Host "Prompt inbox: $PromptOutDir"
Write-Host "Project map: $ProjectMapPath"
Write-Host "Open Codex: $OpenCodex"
Write-Host "Polling every $IntervalSeconds seconds. AutoAck: $AutoAck"

while ($true) {
  try {
    Process-Inbox $State
  } catch {
    Write-Warning $_.Exception.Message
  }

  if ($Once) {
    break
  }

  Start-Sleep -Seconds $IntervalSeconds
}
