import threading
from types import SimpleNamespace
from pathlib import Path

import codex_bridge.codex_app_submitter as submitter_module
import pytest
from codex_bridge.codex_app_submitter import (
    CodexAppSubmitter,
    codex_submission_state,
    codex_window_score,
    draft_matches_expected_prompt,
    inspect_codex_composer,
    is_composer_placeholder,
    is_send_button,
    is_stop_button,
    submit_codex_composer,
    wait_for_codex_submission,
)


class FakeRect:
    def __init__(self, left, top, right, bottom):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class FakeElementInfo:
    def __init__(
        self,
        *,
        control_type,
        name="",
        class_name="",
        automation_id="",
        rectangle,
    ):
        self.control_type = control_type
        self.name = name
        self.class_name = class_name
        self.automation_id = automation_id
        self.rectangle = rectangle


class FakeControl:
    def __init__(self, info, *, parent=None, descendants=None, enabled=True, visible=True):
        self.element_info = info
        self._parent = parent
        self._descendants = descendants or []
        self._enabled = enabled
        self._visible = visible

    def parent(self):
        return self._parent

    def descendants(self):
        return self._descendants

    def is_enabled(self):
        return self._enabled

    def is_visible(self):
        return self._visible


def test_current_codex_editor_reads_text_pattern_before_fragmented_children():
    prompt = "# Codex Hub Item\nProject: Example (project-one)\nEntry: #303"
    text = FakeControl(FakeElementInfo(control_type="Text", name="Entry:", rectangle=FakeRect(0, 0, 200, 40)))
    editor = FakeControl(FakeElementInfo(control_type="Edit", class_name="ProseMirror", rectangle=FakeRect(10, 50, 700, 300)), descendants=[text])
    editor.iface_text = SimpleNamespace(DocumentRange=SimpleNamespace(GetText=lambda _: prompt))
    window = FakeControl(FakeElementInfo(control_type="Window", rectangle=FakeRect(0, 0, 1000, 800)), descendants=[editor, text])
    snapshot = inspect_codex_composer(window)
    assert snapshot["draft_text"] == prompt
    assert draft_matches_expected_prompt(snapshot["draft_text"], prompt)
    assert not draft_matches_expected_prompt(snapshot["draft_text"], prompt.replace("#303", "#304"))


def test_current_codex_composer_styles_and_placeholder():
    classes = "size-token-button-composer bg-composer-primary"
    assert is_send_button("发送", classes, enabled=True, visible=True)
    assert not is_send_button("开启语音聊天", classes, enabled=True, visible=True)
    assert not is_send_button("发送", classes + " opacity-50", enabled=True, visible=True)
    assert is_stop_button("停止任务", classes, True)
    assert is_composer_placeholder("使用 ChatGPT Work")


def test_current_codex_model_prefix_and_reasoning_labels():
    assert submitter_module._runtime_option_name_matches("GPT-6 Astra", "6 Astra")
    assert submitter_module._runtime_option_name_matches("GPT-5.6 Sol", "5.6 Sol")
    assert not submitter_module._codex_model_button_name_matches("GPT-5.4 Mini 高", "5.4")
    assert submitter_module._reasoning_button_name_matches("GPT-6 Astra Ultra", "ultra")
    assert submitter_module._reasoning_button_name_matches("GPT-6 Astra 最高", "max")
    assert submitter_module._runtime_option_name_matches("极高 更快消耗使用额度", "Ultra")


def test_new_model_is_verified_through_runtime_instead_of_rejected_by_old_allowlist(monkeypatch):
    calls = []
    monkeypatch.setattr(submitter_module, "Desktop", object())
    def configure(_hwnd, **options):
        calls.append(options)
        return False, "option is not installed"
    monkeypatch.setattr(submitter_module, "_configure_codex_execution", configure)
    assert submitter_module.configure_codex_execution(1, model="gpt-7-example")[0] is False
    assert calls[0]["model_label"] == "7 Example"


def test_window_score_accepts_codex_package_chatgpt_window():
    process_path = (
        r"C:\Program Files\WindowsApps\OpenAI.Codex_26.707.3748.0_x64__pkg"
        r"\app\ChatGPT.exe"
    )
    configured = Path(
        r"C:\Program Files\WindowsApps\OpenAI.Codex_26.623.0_x64__pkg"
        r"\app\Codex.exe"
    )

    score = codex_window_score(
        process_path,
        "ChatGPT",
        configured,
        is_foreground=True,
        area=1_000_000,
    )

    assert score > 0


def test_window_score_accepts_codex_package_without_reading_window_title():
    process_path = (
        r"C:\Program Files\WindowsApps\OpenAI.Codex_26.707.3748.0_x64__pkg"
        r"\app\ChatGPT.exe"
    )

    score = codex_window_score(
        process_path,
        "",
        None,
        is_foreground=False,
        area=1_000_000,
    )

    assert score > 0


def test_window_score_rejects_unrelated_chatgpt_window():
    score = codex_window_score(
        r"C:\Tools\ChatGPT\ChatGPT.exe",
        "ChatGPT",
        None,
        is_foreground=True,
        area=1_000_000,
    )

    assert score == -1


def test_submit_never_sends_key_when_codex_window_is_missing(monkeypatch):
    sent = []
    monkeypatch.setattr(submitter_module, "find_codex_window", lambda _path: None)
    monkeypatch.setattr(submitter_module, "send_submit_hotkey", sent.append)
    submitter = CodexAppSubmitter()

    assert submitter.submit("enter") is False
    assert sent == []
    assert "未找到" in submitter.last_error


def test_send_button_matches_codex_composer_control():
    class_name = (
        "cursor-interaction size-token-button-composer "
        "bg-token-button-background electron:bg-token-primary"
    )

    assert is_send_button("", class_name, enabled=True, visible=True) is True
    assert is_send_button("发送", class_name, enabled=True, visible=True) is True
    assert is_send_button("", class_name, enabled=False, visible=True) is False
    assert is_send_button("", "unrelated-button", enabled=True, visible=True) is False
    assert is_send_button(
        "", f"{class_name} opacity-50", enabled=True, visible=True
    ) is False


def test_composer_is_inferred_from_send_button_ancestor_without_prosemirror():
    window = FakeControl(
        FakeElementInfo(
            control_type="Window",
            rectangle=FakeRect(0, 0, 1000, 800),
        )
    )
    prompt_text = FakeControl(
        FakeElementInfo(
            control_type="Text",
            name="# Codex Hub Item\nEntry: #60",
            class_name="",
            rectangle=FakeRect(140, 360, 880, 600),
        )
    )
    composer = FakeControl(
        FakeElementInfo(
            control_type="Group",
            class_name="codex-composer-root",
            rectangle=FakeRect(100, 320, 980, 760),
        ),
        parent=window,
    )
    toolbar = FakeControl(
        FakeElementInfo(
            control_type="Group",
            rectangle=FakeRect(650, 680, 970, 750),
        ),
        parent=composer,
    )
    send_button = FakeControl(
        FakeElementInfo(
            control_type="Button",
            class_name=(
                "cursor-interaction size-token-button-composer "
                "bg-token-button-background"
            ),
            rectangle=FakeRect(910, 690, 960, 740),
        ),
        parent=toolbar,
    )
    toolbar._descendants = [send_button]
    composer._descendants = [prompt_text, toolbar, send_button]
    window._descendants = [composer, prompt_text, toolbar, send_button]

    snapshot = inspect_codex_composer(window)

    assert snapshot["composer_found"] is True
    assert snapshot["composer"] is composer
    assert snapshot["send_button"] is send_button
    assert "Entry: #60" in snapshot["draft_text"]


def test_scrollable_composer_keeps_offscreen_hub_identity_text():
    window = FakeControl(
        FakeElementInfo(
            control_type="Window",
            rectangle=FakeRect(0, 0, 1000, 800),
        )
    )
    visible_prefix = FakeControl(
        FakeElementInfo(
            control_type="Text",
            name="# Codex Hub Item",
            rectangle=FakeRect(140, 360, 880, 420),
        )
    )
    offscreen_identity = FakeControl(
        FakeElementInfo(
            control_type="Text",
            name=(
                "Project: Private Server Buildout (private-server-buildout)\n"
                "Entry: #119"
            ),
            # This line is below the editor viewport but remains in its UIA
            # subtree when the contenteditable is scrolled to the top.
            rectangle=FakeRect(140, 780, 880, 840),
        )
    )
    composer = FakeControl(
        FakeElementInfo(
            control_type="Edit",
            class_name="ProseMirror",
            rectangle=FakeRect(100, 320, 900, 700),
        ),
        parent=window,
        descendants=[visible_prefix, offscreen_identity],
    )
    window._descendants = [composer, visible_prefix, offscreen_identity]

    snapshot = inspect_codex_composer(window)

    assert snapshot["composer_found"] is True
    assert "Project: Private Server Buildout" in snapshot["draft_text"]
    assert "Entry: #119" in snapshot["draft_text"]


def test_empty_composer_placeholder_is_not_treated_as_a_changed_draft():
    assert is_composer_placeholder("要做些什么") is True
    assert is_composer_placeholder("要求后续变更") is True
    assert is_composer_placeholder("Ask anything") is True
    assert is_composer_placeholder("Hub prompt") is False

    submitted, detail = codex_submission_state(
        {
            "composer_found": True,
            "draft_text": "要求后续变更",
            "stop_button_found": False,
        },
        "Hub prompt",
    )
    assert submitted is True
    assert detail == ""


def test_stop_button_matches_active_codex_composer_control():
    class_name = "cursor-interaction size-token-button-composer"

    assert is_stop_button("停止", class_name, visible=True) is True
    assert is_stop_button("Stop", class_name, visible=True) is True
    assert is_stop_button("停止", class_name, visible=False) is False


def test_submit_reports_success_only_after_verified_composer_submission(monkeypatch):
    monkeypatch.setattr(submitter_module, "find_codex_window", lambda _path: 123)
    monkeypatch.setattr(submitter_module, "focus_codex_window", lambda _hwnd: True)
    calls = []

    def fake_submit(_hwnd, expected_prompt="", hotkey="enter"):
        calls.append((expected_prompt, hotkey))
        return expected_prompt == "Hub prompt", ""

    monkeypatch.setattr(submitter_module, "submit_codex_composer", fake_submit)

    submitter = CodexAppSubmitter()

    assert submitter.submit("enter", expected_prompt="Hub prompt") is True
    assert submitter.last_error == ""
    assert calls == [("Hub prompt", "enter")]


def test_submit_keeps_verification_failure_detail(monkeypatch):
    monkeypatch.setattr(submitter_module, "find_codex_window", lambda _path: 123)
    monkeypatch.setattr(submitter_module, "focus_codex_window", lambda _hwnd: True)
    monkeypatch.setattr(
        submitter_module,
        "submit_codex_composer",
        lambda _hwnd, expected_prompt="", hotkey="enter": (
            False,
            "未确认草稿已提交",
        ),
    )

    submitter = CodexAppSubmitter()

    assert submitter.submit("enter") is False
    assert submitter.last_error == "未确认草稿已提交"


def test_submitter_passes_message_model_preferences_to_verified_submission(monkeypatch):
    monkeypatch.setattr(submitter_module, "find_codex_window", lambda _path: 123)
    calls = []

    def fake_submit(_hwnd, **kwargs):
        calls.append(kwargs)
        return True, ""

    monkeypatch.setattr(submitter_module, "submit_codex_composer", fake_submit)
    submitter = CodexAppSubmitter()

    assert submitter.submit(
        "enter",
        expected_prompt="Hub prompt",
        model="gpt-5.6-terra",
        reasoning_effort="ultra",
    ) is True
    assert calls[0]["model"] == "gpt-5.6-terra"
    assert calls[0]["reasoning_effort"] == "ultra"


def test_submit_never_uses_a_second_send_action_after_unverified_click(monkeypatch):
    actions = []

    snapshot = {
        "composer_found": True,
        "composer": object(),
        "composer_rect": (100, 300, 900, 700),
        "draft_text": "Hub prompt",
        "send_button": object(),
        "send_button_rect": (850, 650, 890, 690),
        "stop_button_found": False,
    }
    monkeypatch.setattr(submitter_module, "focus_codex_window", lambda _hwnd: True)
    monkeypatch.setattr(
        submitter_module,
        "inspect_codex_target_with_timeout",
        lambda _target, timeout_seconds: snapshot,
    )
    monkeypatch.setattr(
        submitter_module,
        "wait_for_codex_submission",
        lambda _target, _draft, **_kwargs: (
            False,
            "未检测到 Codex 开始执行或清空草稿",
        ),
    )
    monkeypatch.setattr(
        submitter_module,
        "send_submit_hotkey",
        lambda hotkey: actions.append(("hotkey", hotkey)),
    )
    monkeypatch.setattr(
        submitter_module,
        "native_left_click",
        lambda rect, **kwargs: actions.append(("native-click", rect, kwargs)),
    )

    submitted, detail = submit_codex_composer(
        123,
        expected_prompt="Hub prompt",
        hotkey="ctrl-enter",
    )

    assert submitted is False
    assert "已停止自动重试" in detail
    assert actions == [
        ("native-click", (850, 650, 890, 690), {}),
    ]


def test_submit_uses_verified_uia_invoke_when_foreground_focus_is_blocked(
    monkeypatch,
):
    actions = []

    class SendButton:
        def invoke(self):
            actions.append("invoke")

    snapshot = {
        "composer_found": True,
        "composer": object(),
        "composer_rect": (100, 300, 900, 700),
        "draft_text": "Hub prompt",
        "send_button": SendButton(),
        "send_button_rect": (850, 650, 890, 690),
        "stop_button_found": False,
    }
    monkeypatch.setattr(submitter_module, "focus_codex_window", lambda _hwnd: False)
    monkeypatch.setattr(
        submitter_module,
        "inspect_codex_target_with_timeout",
        lambda _target, timeout_seconds: snapshot,
    )
    monkeypatch.setattr(
        submitter_module,
        "wait_for_codex_submission",
        lambda _target, _draft, **_kwargs: (True, ""),
    )
    monkeypatch.setattr(
        submitter_module,
        "native_left_click",
        lambda *_args, **_kwargs: actions.append("native-click"),
    )

    submitted, detail = submit_codex_composer(
        123,
        expected_prompt="Hub prompt",
    )

    assert submitted is True
    assert detail == ""
    assert actions == ["invoke"]


def test_uia_inspection_has_a_real_timeout():
    blocker = threading.Event()

    with pytest.raises(submitter_module.UIAutomationTimeoutError):
        submitter_module.call_with_timeout(
            blocker.wait,
            timeout_seconds=0.1,
            description="test UIA",
        )


def test_submit_returns_without_sending_when_initial_uia_read_times_out(monkeypatch):
    sent = []
    monkeypatch.setattr(submitter_module, "Desktop", object())
    monkeypatch.setattr(
        submitter_module,
        "inspect_codex_target_with_timeout",
        lambda _target, timeout_seconds: (_ for _ in ()).throw(
            submitter_module.UIAutomationTimeoutError("blocked")
        ),
    )
    monkeypatch.setattr(submitter_module, "native_left_click", sent.append)

    submitted, detail = submit_codex_composer(123, expected_prompt="Hub prompt")

    assert submitted is False
    assert "超时" in detail
    assert sent == []


def test_submit_fails_closed_when_selected_model_cannot_be_verified(monkeypatch):
    sent = []
    snapshot = {
        "composer_found": True,
        "composer": object(),
        "composer_rect": (100, 300, 900, 700),
        "draft_text": "Hub prompt",
        "send_button": object(),
        "send_button_rect": (850, 650, 890, 690),
        "stop_button_found": False,
    }
    monkeypatch.setattr(submitter_module, "Desktop", object())
    monkeypatch.setattr(
        submitter_module,
        "inspect_codex_target_with_timeout",
        lambda _target, timeout_seconds: snapshot,
    )
    monkeypatch.setattr(
        submitter_module,
        "configure_codex_execution",
        lambda *_args, **_kwargs: (False, "model picker did not confirm"),
    )
    monkeypatch.setattr(submitter_module, "native_left_click", sent.append)

    submitted, detail = submit_codex_composer(
        123,
        expected_prompt="Hub prompt",
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
    )

    assert submitted is False
    assert "模型设置未确认" in detail
    assert sent == []


def test_submission_is_only_verified_by_stop_state_or_cleared_draft():
    base = {
        "composer_found": True,
        "draft_text": "Hub prompt",
        "stop_button_found": False,
    }

    assert codex_submission_state(
        {**base, "stop_button_found": True}, "Hub prompt"
    ) == (True, "")
    assert codex_submission_state({**base, "draft_text": ""}, "Hub prompt") == (
        True,
        "",
    )
    inaccessible_text = codex_submission_state(
        {**base, "draft_text": "", "send_button": object()}, "Hub prompt"
    )
    assert inaccessible_text[0] is False
    assert "发送按钮仍可用" in inaccessible_text[1]
    changed = codex_submission_state(
        {**base, "draft_text": "Hub prompt edited"}, "Hub prompt"
    )
    assert changed[0] is False
    assert "避免误发" in changed[1]


def test_verification_waits_through_temporary_editor_disappearance(monkeypatch):
    snapshots = iter(
        [
            {
                "composer_found": False,
                "draft_text": "",
                "stop_button_found": False,
            },
            {
                "composer_found": False,
                "draft_text": "",
                "stop_button_found": True,
            },
        ]
    )
    monkeypatch.setattr(
        submitter_module,
        "inspect_codex_composer",
        lambda _window: next(snapshots),
    )
    monkeypatch.setattr(submitter_module.time, "sleep", lambda _seconds: None)

    submitted, detail = wait_for_codex_submission(
        object(),
        "Hub prompt",
        timeout_seconds=1,
    )

    assert submitted is True
    assert detail == ""


def test_verification_does_not_accept_transient_missing_uia_text(monkeypatch):
    inspections = []
    snapshots = iter(
        [
            {
                "composer_found": True,
                "draft_text": "",
                "send_button": object(),
                "stop_button_found": False,
            },
            {
                "composer_found": True,
                "draft_text": "",
                "send_button": None,
                "stop_button_found": True,
            },
        ]
    )

    def inspect(_window):
        inspections.append(True)
        return next(snapshots)

    monkeypatch.setattr(submitter_module, "inspect_codex_composer", inspect)
    monkeypatch.setattr(submitter_module.time, "sleep", lambda _seconds: None)

    submitted, detail = wait_for_codex_submission(
        object(), "Hub prompt", timeout_seconds=1
    )

    assert submitted is True
    assert detail == ""
    assert len(inspections) == 2


def test_verification_requires_two_stable_cleared_composer_reads(monkeypatch):
    inspections = []
    cleared = {
        "composer_found": True,
        "draft_text": "",
        "send_button": None,
        "stop_button_found": False,
    }

    def inspect(_window):
        inspections.append(True)
        return dict(cleared)

    monkeypatch.setattr(submitter_module, "inspect_codex_composer", inspect)
    monkeypatch.setattr(submitter_module.time, "sleep", lambda _seconds: None)

    submitted, detail = wait_for_codex_submission(
        object(), "Hub prompt", timeout_seconds=1
    )

    assert submitted is True
    assert detail == ""
    assert len(inspections) == 2


def test_draft_must_match_the_expected_hub_prompt():
    expected = "# Codex Hub Item\n\nPrompt file: C:/prompts/mention-51.md\n\nDo the work."

    assert draft_matches_expected_prompt(expected, expected) is True
    assert draft_matches_expected_prompt(
        "# Codex Hub Item Prompt file: C:/prompts/mention-51.md Do the work.",
        expected,
    ) is True
    assert draft_matches_expected_prompt("要求后续变更", expected) is False
    assert draft_matches_expected_prompt(
        expected.replace("mention-51.md", "mention-50.md"), expected
    ) is False


@pytest.fixture
def compact_hub_file_prompt():
    return "\n".join(
        [
            "# Codex Hub Item",
            "",
            "Hub 原文较长，已保存到本地文件。请读取并执行这个文件内容。",
            (
                "Prompt file: C:\\Users\\Administrator\\AppData\\Roaming\\"
                "CodexBridge\\prompts\\mention-304-private-server-buildout.md"
            ),
            "",
            "除非文件明确要求，不要触碰 SW /upload 或 /u。",
            "Windows UTF-8 安全：不要把中文从 Windows PowerShell 5.1 管道传给 Python；回写时请把完整回复保存为 UTF-8 文件，并优先使用 codex-hub-reply --body-file。",
            "完成后，请把结果简洁写回 Codex Hub 对应项目聊天或任务。",
        ]
    )


@pytest.mark.parametrize("rendered_heading", [False, True])
@pytest.mark.parametrize("separator", ["\n", "\r\n", " "])
def test_compact_hub_wrapper_accepts_rendered_title_and_uia_whitespace(
    compact_hub_file_prompt, rendered_heading, separator
):
    expected = compact_hub_file_prompt
    draft = expected.removeprefix("# ") if rendered_heading else expected
    draft = separator.join(draft.splitlines())

    assert draft_matches_expected_prompt(draft, expected) is True


def test_compact_hub_wrapper_accepts_same_item_retry_filename(compact_hub_file_prompt):
    expected = compact_hub_file_prompt
    draft = expected.removeprefix("# ").replace(
        "mention-304-private-server-buildout.md",
        "mention-304-private-server-buildout-2.md",
    )

    assert draft_matches_expected_prompt(draft, expected) is True


def test_compact_hub_wrapper_reads_complete_rendered_text_pattern(compact_hub_file_prompt):
    rendered = compact_hub_file_prompt.removeprefix("# ")
    text_limits = []

    def get_text(limit):
        text_limits.append(limit)
        return rendered if limit == -1 else rendered[:limit]

    text = FakeControl(
        FakeElementInfo(control_type="Text", name=rendered[:40], rectangle=FakeRect(10, 50, 700, 90))
    )
    editor = FakeControl(
        FakeElementInfo(control_type="Edit", class_name="ProseMirror", rectangle=FakeRect(10, 50, 700, 300)),
        descendants=[text],
    )
    editor.iface_text = SimpleNamespace(DocumentRange=SimpleNamespace(GetText=get_text))
    window = FakeControl(
        FakeElementInfo(control_type="Window", rectangle=FakeRect(0, 0, 1000, 800)),
        descendants=[editor, text],
    )

    snapshot = inspect_codex_composer(window)

    assert text_limits == [-1]
    assert snapshot["draft_text"] == rendered
    assert draft_matches_expected_prompt(snapshot["draft_text"], compact_hub_file_prompt) is True


@pytest.mark.parametrize("different_item", [False, True])
def test_compact_hub_wrapper_submission_verifies_rendered_draft_before_one_send(
    compact_hub_file_prompt, monkeypatch, different_item
):
    draft = compact_hub_file_prompt.removeprefix("# ")
    if different_item:
        draft = draft.replace("mention-304-", "mention-305-")
    snapshot = {
        "composer_found": True,
        "composer": object(),
        "composer_rect": (100, 300, 900, 700),
        "draft_text": draft,
        "send_button": object(),
        "send_button_rect": (850, 650, 890, 690),
        "stop_button_found": False,
    }
    actions = []
    inspections = []
    attempts = []

    def inspect(_target, **_kwargs):
        inspections.append(bool(actions))
        return {**snapshot, "stop_button_found": bool(actions)}

    monkeypatch.setattr(submitter_module, "Desktop", object())
    monkeypatch.setattr(submitter_module, "focus_codex_window", lambda _hwnd: True)
    monkeypatch.setattr(submitter_module, "inspect_codex_target_with_timeout", inspect)
    monkeypatch.setattr(submitter_module, "configure_codex_execution", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(submitter_module, "native_left_click", actions.append)

    submitted, detail = submit_codex_composer(
        123,
        expected_prompt=compact_hub_file_prompt,
        model="gpt-6-astra",
        reasoning_effort="ultra",
        on_send_attempt=lambda: attempts.append(True),
    )

    if different_item:
        assert submitted is False
        assert "当前草稿不是" in detail
        assert inspections == [False]
        assert actions == []
        assert attempts == []
    else:
        assert (submitted, detail) == (True, "")
        assert inspections == [False, False, False, True]
        assert actions == [(850, 650, 890, 690)]
        assert attempts == [True]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("mention-304-", "mention-305-"),
        ("private-server-buildout.md", "different-project.md"),
        ("CodexBridge\\prompts\\", "CodexBridge\\untrusted-prompts\\"),
    ],
)
def test_compact_hub_wrapper_rejects_different_item_or_path(
    compact_hub_file_prompt, old, new
):
    expected = compact_hub_file_prompt
    draft = expected.replace(old, new)

    assert draft_matches_expected_prompt(draft, expected) is False


@pytest.mark.parametrize(
    "replacement",
    ["", "除非文件明确要求，不要触碰 SW /upload 或 /u。\n额外执行另一项任务。"],
)
def test_compact_hub_wrapper_rejects_middle_instruction_changes(
    compact_hub_file_prompt, replacement
):
    expected = compact_hub_file_prompt
    draft = expected.replace("除非文件明确要求，不要触碰 SW /upload 或 /u。", replacement)

    assert draft_matches_expected_prompt(draft, expected) is False


def test_compact_hub_wrapper_keeps_hashes_inside_the_body(compact_hub_file_prompt):
    expected = compact_hub_file_prompt.replace(
        "除非文件明确要求", "## Codex Hub 说明 #304\n除非文件明确要求"
    )
    draft = expected.removeprefix("# ").replace("## Codex Hub 说明 #304", "Codex Hub 说明 304")

    assert draft_matches_expected_prompt(draft, expected) is False


@pytest.mark.parametrize("missing_path", ["entire_line", "extension", "marker"])
def test_compact_hub_wrapper_requires_complete_prompt_file_path(
    compact_hub_file_prompt, missing_path
):
    expected = compact_hub_file_prompt
    path_line = next(line for line in expected.splitlines() if line.startswith("Prompt file:"))
    if missing_path == "entire_line":
        draft = expected.replace(path_line, "")
    elif missing_path == "extension":
        draft = expected.replace("private-server-buildout.md", "private-server-buildout")
    else:
        draft = expected.replace("Prompt file:", "")

    assert draft_matches_expected_prompt(draft, expected) is False


def test_retry_prompt_file_suffixes_for_same_hub_item_are_equivalent():
    base = "\n".join(
        [
            "# Codex Hub Item",
            "Entry: #60",
            "Project: Private Server Buildout (private-server-buildout)",
            "Prompt file: C:/prompts/mention-60-private-server-buildout-3.md",
            "检测 hermes 是否在线",
        ]
    )
    older_retry = base.replace(
        "mention-60-private-server-buildout-3.md",
        "mention-60-private-server-buildout-2.md",
    )
    other_item = base.replace("Entry: #60", "Entry: #61").replace(
        "mention-60-private-server-buildout-3.md",
        "mention-61-private-server-buildout-2.md",
    )

    assert draft_matches_expected_prompt(older_retry, base) is True
    assert draft_matches_expected_prompt(other_item, base) is False


def test_same_hub_identity_matches_when_accessibility_text_loses_structure():
    expected = "\n".join(
        [
            "# Codex Hub Item",
            "# Codex Hub Mention",
            "Project: Private Server Buildout (private-server-buildout)",
            "Entry: #60",
            "Prompt file: C:/prompts/mention-60-private-server-buildout-8.md",
            "检测 hermes 是否在线",
            "Complete it and write a concise result back.",
        ]
    )
    accessibility_draft = (
        "# Codex Hub Item Project: Private Server Buildout "
        "(private-server-buildout) Entry: #60 检测 hermes 是否在线"
    )
    other_entry = accessibility_draft.replace("Entry: #60", "Entry: #61")

    assert draft_matches_expected_prompt(accessibility_draft, expected) is True
    assert draft_matches_expected_prompt(other_entry, expected) is False


def test_submitter_reopens_target_after_idle_wrong_draft(monkeypatch):
    class RunningProbe:
        def is_running(self, _configured_path=None):
            return True

    submitter = CodexAppSubmitter(
        window_timeout_seconds=1,
        poll_interval_seconds=0.1,
        reopen_interval_seconds=0,
        reopen_settle_seconds=0,
        process_probe=RunningProbe(),
    )
    attempts = []
    reopened = []

    def fake_submit(_hotkey, expected_prompt=""):
        attempts.append(expected_prompt)
        if len(attempts) == 1:
            submitter.last_error = "Codex 当前草稿不是 Bridge 刚打开的 Hub 提示，未发送。"
            return False
        return True

    monkeypatch.setattr(submitter, "submit", fake_submit)

    assert submitter.submit_after_delay(
        0,
        "enter",
        "expected Hub prompt",
        reopen_callback=lambda: reopened.append(True),
    ) is True
    assert reopened == [True]
    assert attempts == ["expected Hub prompt", "expected Hub prompt"]
    assert submitter.last_recovery_action == "reopen_prompt"


def test_submitter_does_not_probe_codex_when_hub_delivery_sends_normally(
    monkeypatch,
):
    class UnexpectedProbe:
        def is_running(self, _configured_path=None):
            raise AssertionError("process probe must stay idle")

    submitter = CodexAppSubmitter(process_probe=UnexpectedProbe())
    monkeypatch.setattr(submitter, "submit", lambda *args, **kwargs: True)

    assert submitter.submit_after_delay(
        0,
        "enter",
        "expected Hub prompt",
        reopen_callback=lambda: None,
    ) is True
    assert submitter.last_recovery_action == ""


def test_submitter_starts_codex_only_after_hub_delivery_cannot_send(monkeypatch):
    class OfflineProbe:
        def __init__(self):
            self.calls = 0

        def is_running(self, _configured_path=None):
            self.calls += 1
            return False

    probe = OfflineProbe()
    submitter = CodexAppSubmitter(
        window_timeout_seconds=1,
        poll_interval_seconds=0.1,
        reopen_interval_seconds=0,
        reopen_settle_seconds=0,
        process_probe=probe,
    )
    attempts = []
    reopened = []

    def fake_submit(_hotkey, expected_prompt=""):
        attempts.append(expected_prompt)
        if len(attempts) == 1:
            submitter.last_error = "未找到属于 Codex 应用的可见窗口。"
            return False
        return True

    monkeypatch.setattr(submitter, "submit", fake_submit)

    assert submitter.submit_after_delay(
        0,
        "enter",
        "expected Hub prompt",
        reopen_callback=lambda: reopened.append(True),
    ) is True
    assert probe.calls == 1
    assert reopened == [True]
    assert submitter.last_recovery_action == "start_codex"


def test_submitter_polls_loading_composer_before_reopening_target(monkeypatch):
    submitter = CodexAppSubmitter(
        window_timeout_seconds=1,
        poll_interval_seconds=0.01,
        reopen_interval_seconds=0.5,
        reopen_settle_seconds=0,
    )
    attempts = []
    reopened = []

    def fake_submit(_hotkey, expected_prompt=""):
        attempts.append(expected_prompt)
        if len(attempts) == 1:
            submitter.last_error = "未找到 Codex 可发送编辑器。"
            return False
        return True

    monkeypatch.setattr(submitter, "submit", fake_submit)

    assert submitter.submit_after_delay(
        0,
        "enter",
        "expected Hub prompt",
        reopen_callback=lambda: reopened.append(True),
    ) is True
    assert attempts == ["expected Hub prompt", "expected Hub prompt"]
    assert reopened == []


def test_submitter_does_not_reopen_while_codex_is_busy(monkeypatch):
    submitter = CodexAppSubmitter(
        window_timeout_seconds=1,
        poll_interval_seconds=0.1,
        reopen_interval_seconds=0,
        reopen_settle_seconds=0,
    )
    attempts = []
    reopened = []

    def fake_submit(_hotkey, expected_prompt=""):
        attempts.append(expected_prompt)
        if len(attempts) == 1:
            submitter.last_error = "Codex 当前正在执行，发送按钮尚不可用。"
            return False
        return True

    monkeypatch.setattr(submitter, "submit", fake_submit)

    assert submitter.submit_after_delay(
        0,
        "enter",
        "expected Hub prompt",
        reopen_callback=lambda: reopened.append(True),
    ) is True
    assert reopened == []


def test_submitter_does_not_block_after_all_verified_strategies_fail(monkeypatch):
    submitter = CodexAppSubmitter(window_timeout_seconds=300)
    attempts = []

    def fake_submit(_hotkey, expected_prompt=""):
        attempts.append(expected_prompt)
        submitter.last_error = "三种发送方式均未检测到 Codex 开始执行或清空草稿"
        return False

    monkeypatch.setattr(submitter, "submit", fake_submit)

    assert submitter.submit_after_delay(0, "enter", "Hub prompt") is False
    assert attempts == ["Hub prompt"]


def test_submitter_never_retries_after_any_send_action(monkeypatch):
    submitter = CodexAppSubmitter(
        window_timeout_seconds=300,
        poll_interval_seconds=0.1,
    )
    attempts = []
    reopened = []

    def fake_submit(_hotkey, expected_prompt=""):
        attempts.append(expected_prompt)
        submitter.last_send_attempted = True
        submitter.last_error = "复核 Codex 编辑器超时"
        return False

    monkeypatch.setattr(submitter, "submit", fake_submit)

    assert submitter.submit_after_delay(
        0,
        "enter",
        "Hub prompt",
        reopen_callback=lambda: reopened.append(True),
    ) is False
    assert attempts == ["Hub prompt"]
    assert reopened == []
