from codex_bridge import __version__
from codex_bridge.i18n import TEXT, text


def test_chinese_ui_labels_cover_main_window_actions():
    assert text("window_title") == f"Codex Bridge v{__version__} 本地代理"
    assert text("status") == "状态"
    assert text("device") == "设备"
    assert text("start") == "启动"
    assert text("stop") == "停止"
    assert text("run_once") == "执行一次"
    assert text("open_config") == "打开配置"
    assert text("start_with_windows") == "开机自启"


def test_chinese_ui_labels_cover_local_tool_settings():
    assert text("local_tools") == "本地工具"
    assert text("codex_exe") == "Codex 程序"
    assert text("auto_submit_codex") == "打开后自动发送"
    assert text("submit_delay") == "延迟"
    assert text("submit_hotkey") == "发送键"
    assert text("codex_cli") == "Codex CLI"
    assert text("auto_execute_codex") == "自动执行 Codex"
    assert text("cmynetwork_exe") == "CMYNetwork 程序"
    assert text("chatgpt_test") == "ChatGPT 连通测试"
    assert text("auto_recover") == "自动恢复"
    assert text("save_settings") == "保存设置"
    assert "codex exec" in text("auto_execute_hint")
    assert "校验 Codex 的发送按钮" in text("codex_behavior_hint")
    assert "不会盲按 Enter" in text("codex_behavior_hint")
    assert "只记录警告" in text("auto_recover_hint")


def test_text_returns_key_for_missing_translation():
    assert text("missing-key") == "missing-key"
    assert "window_title" in TEXT
