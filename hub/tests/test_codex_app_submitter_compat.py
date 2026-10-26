from types import SimpleNamespace

import pytest

from codex_bridge.codex_app_submitter import (
    _find_codex_runtime_button,
    codex_model_label,
    find_codex_send_button,
)


class Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class Button:
    def __init__(self, name, *, classes="", rect=None, enabled=True, visible=True):
        self.element_info = SimpleNamespace(
            control_type="Button",
            name=name,
            class_name=classes,
            automation_id="",
            rectangle=rect or Rect(850, 650, 890, 690),
        )
        self.enabled = enabled
        self.visible = visible

    def is_enabled(self):
        return self.enabled

    def is_visible(self):
        return self.visible


WINDOW_RECT = Rect(0, 0, 1000, 800)
COMPOSER_CLASSES = "size-token-button-composer bg-composer-primary"


@pytest.mark.parametrize("model,label", [("gpt-6-sol", "6 Sol"), ("gpt-6-luna", "6 Luna")])
def test_current_model_short_label_identifies_runtime_button(model, label):
    button = Button(f"{label} 高")
    assert codex_model_label(model) == label
    assert _find_codex_runtime_button([button]) is button


@pytest.mark.parametrize("name", ["Send message", "Submit prompt", "发送消息"])
@pytest.mark.parametrize("classes", [COMPOSER_CLASSES, "new-composer-style"])
def test_current_send_label_is_found_with_known_or_changed_styles(name, classes):
    button = Button(name, classes=classes)
    assert find_codex_send_button([button], WINDOW_RECT) is button


@pytest.mark.parametrize("name", ["", "Send message", "发送"])
def test_visually_disabled_send_button_is_not_reintroduced_by_fallback(name):
    button = Button(name, classes=f"{COMPOSER_CLASSES} opacity-50")
    # Electron may report enabled before its visual disabled state is removed.
    assert button.is_enabled()
    assert find_codex_send_button([button], WINDOW_RECT) is None


def test_disabled_button_farther_right_does_not_displace_valid_fallback_send():
    ready = Button("Send message", classes="new-composer-style")
    disabled = Button(
        "发送消息",
        classes=f"{COMPOSER_CLASSES} opacity-50",
        rect=Rect(940, 700, 980, 740),
    )
    assert find_codex_send_button([ready, disabled], WINDOW_RECT) is ready


@pytest.mark.parametrize("name", ["开启语音聊天", "Start voice chat", "Stop", "Send later"])
def test_non_send_controls_remain_excluded_with_composer_styles(name):
    button = Button(name, classes=COMPOSER_CLASSES)
    assert find_codex_send_button([button], WINDOW_RECT) is None


@pytest.mark.parametrize("enabled,visible", [(False, True), (True, False)])
def test_current_send_label_still_requires_enabled_visible_control(enabled, visible):
    button = Button("Send message", classes=COMPOSER_CLASSES, enabled=enabled, visible=visible)
    assert find_codex_send_button([button], WINDOW_RECT) is None
