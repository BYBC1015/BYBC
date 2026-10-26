from pathlib import Path


AGENT_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "codex-device-agent.ps1"


def read_agent_script() -> str:
    return AGENT_SCRIPT.read_text(encoding="utf-8")


def test_device_agent_seeds_existing_items_on_first_run():
    script = read_agent_script()

    assert "IgnoreExistingOnFirstRun" in script
    assert "Initialized" in script
    assert "Seed-ExistingInboxItems" in script


def test_device_agent_writes_prompt_files_for_mentions_and_tasks():
    script = read_agent_script()

    assert "PromptOutDir" in script
    assert "Write-MentionPromptFile" in script
    assert "Write-TaskPromptFile" in script
    assert "Codex Hub Mention" in script
    assert "Codex Hub Task" in script


def test_device_agent_claims_server_work_before_prompting():
    script = read_agent_script()

    assert "/api/projects/$($task.project_slug)/tasks/$($task.id)/claim" in script
    assert "CLAIMED TASK" in script
    assert "/api/projects/$($mention.project_slug)/mentions/$($mention.id)/claim" in script


def test_device_agent_marks_mentions_handled_after_ack():
    script = read_agent_script()

    assert "/api/projects/$($mention.project_slug)/mentions/$($mention.id)/handled" in script
    assert "HANDLED MENTION" in script


def test_legacy_device_agent_refuses_to_compete_with_codexbridge():
    script = read_agent_script()

    assert "AllowAlongsideCodexBridge" in script
    assert 'Get-Process -Name "CodexBridge"' in script
    assert "CodexBridge.exe is already running" in script


def test_device_agent_supports_codex_deep_link_launch():
    script = read_agent_script()

    assert "[switch]$OpenCodex" in script
    assert "[string]$ProjectMapPath" in script
    assert "Get-ProjectLocalFolder" in script
    assert "New-CodexDeepLink" in script
    assert "Open-CodexForHubItem" in script
    assert "codex://new?path=$encodedPath&prompt=$encodedPrompt" in script
    assert "[System.Uri]::EscapeDataString" in script
    assert "Start-Process -FilePath $deepLink" in script
    assert "CODEX DEEP LINK OPENED" in script


def test_device_agent_uses_prompt_file_reference_for_codex_launch():
    script = read_agent_script()

    assert "Prompt file:" in script
    assert "Do not touch SW /upload or /u" in script
    assert "CodexPromptMaxChars" in script
    assert "Resolve-Path -LiteralPath $localFolder" in script


def test_device_agent_supports_single_run_verification_mode():
    script = read_agent_script()

    assert "[switch]$Once" in script
    assert "if ($Once)" in script


def test_device_agent_saves_stable_json_arrays():
    script = read_agent_script()

    assert "ConvertTo-StableAgentStatePayload" in script
    assert 'SeenTaskIds = @(ConvertTo-StringArray $State["SeenTaskIds"])' in script
    assert 'PromptedTaskIds = @(ConvertTo-StringArray $State["PromptedTaskIds"])' in script


def test_device_agent_requires_explicit_token_and_explains_monitor_handoff():
    script = read_agent_script()

    assert 'Missing Codex Hub API token. Pass -Token or set CODEX_HUB_TOKEN.' in script
    assert "ch_public_export_sentinel" not in script
    assert "等待本机 Codex 线程监控读取和处理" in script
