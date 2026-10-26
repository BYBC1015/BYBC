from itertools import count
from types import SimpleNamespace

import pytest

from codex_bridge.codex_app_submitter import _codex_controls, inspect_codex_composer


class Rect:
    def __init__(self, left=0, top=0, right=1600, bottom=1000):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class Node:
    ids = count(1)

    def __init__(self, kind, *, name="", classes="", children=(), rect=None, value=""):
        self.element_info = SimpleNamespace(
            control_type=kind,
            name=name,
            class_name=classes,
            automation_id="",
            rectangle=rect or Rect(),
            runtime_id=(next(self.ids),),
        )
        self.nodes = list(children)
        self.value = value
        self.ancestor = None
        for child in self.nodes:
            child.ancestor = self

    def descendants(self):
        return [node for child in self.nodes for node in [child, *child.descendants()]]

    def parent(self):
        return self.ancestor

    def get_value(self):
        return self.value

    def is_enabled(self):
        return True

    def is_visible(self):
        return True


def editor(text, *, web=False):
    rect = Rect(1050, 800, 1500, 920) if web else Rect(300, 600, 950, 850)
    control = Node("Edit", classes="ProseMirror", rect=rect)
    control.iface_text = SimpleNamespace(
        DocumentRange=SimpleNamespace(GetText=lambda _limit: text)
    )
    return control


def button(name, *, web=False):
    return Node(
        "Button",
        name=name,
        classes="size-token-button-composer bg-composer-primary",
        rect=Rect(1500, 940, 1540, 980) if web else Rect(900, 900, 940, 940),
    )


def tree(native_controls, web_controls, *, app_value="app://-/index.html"):
    browser = Node("Document", value="https://example.test/", children=web_controls)
    app = Node("Document", value=app_value, children=[*native_controls, browser])
    return Node("Window", children=[app]), app, browser


def test_native_stop_and_browser_send_are_not_combined():
    native_editor = editor("Expected Hub prompt")
    native_stop = button("Stop")
    web_send = button("Send message", web=True)
    window, _, _ = tree([native_editor, native_stop], [web_send])

    snapshot = inspect_codex_composer(window)

    assert snapshot["composer"] is native_editor
    assert snapshot["draft_text"] == "Expected Hub prompt"
    assert snapshot["stop_button_found"] is True
    assert snapshot["send_button"] is None


def test_lower_browser_editor_and_rightmost_send_do_not_displace_native_controls():
    native_editor, native_send = editor("Expected Hub prompt"), button("Send message")
    web_editor, web_send = editor("Unrelated web draft", web=True), button("发送", web=True)
    window, _, _ = tree([native_editor, native_send], [web_editor, web_send, button("Stop", web=True)])

    snapshot = inspect_codex_composer(window)

    assert snapshot["composer"] is native_editor
    assert snapshot["draft_text"] == "Expected Hub prompt"
    assert snapshot["send_button"] is native_send
    assert snapshot["stop_button_found"] is False


@pytest.mark.parametrize("value", ["", "https://example.test/", "app://-/other.html"])
def test_document_without_confirmed_native_app_root_has_no_sendable_controls(value):
    window, _, _ = tree([editor("Hub prompt"), button("发送")], [], app_value=value)
    snapshot = inspect_codex_composer(window)
    assert snapshot["composer_found"] is False
    assert snapshot["send_button"] is None
    assert snapshot["stop_button_found"] is False


@pytest.mark.parametrize("source", ["value_pattern", "get_value", "legacy"])
def test_native_document_url_can_be_read_from_supported_uia_patterns(source):
    native_editor = editor("Hub prompt")
    window, app, _ = tree([native_editor], [])
    if source == "value_pattern":
        app.value = ""
        app.iface_value = SimpleNamespace(CurrentValue="app://-/index.html")
    elif source == "legacy":
        app.value = ""
        app.legacy_properties = lambda: {"Value": "app://-/index.html"}
    assert _codex_controls(window) == [native_editor]


def test_nested_document_is_removed_using_runtime_ids_across_wrapper_instances():
    native_editor = editor("Hub prompt")
    web_send = button("发送", web=True)
    window, _, browser = tree([native_editor], [web_send])
    original_descendants = window.nodes[0].descendants()
    wrapper = button("发送", web=True)
    wrapper.element_info.runtime_id = web_send.element_info.runtime_id
    # Real UIA queries can return a new Python wrapper for the same element.
    browser.descendants = lambda: [wrapper]
    window.nodes[0].descendants = lambda: original_descendants
    assert _codex_controls(window) == [native_editor]


def test_nested_document_read_failure_fails_closed():
    window, _, browser = tree([editor("Hub prompt"), button("Send message")], [])

    def unreadable():
        raise RuntimeError("UIA subtree unavailable")

    # Preserve the outer snapshots, then make the nested subtree unreadable.
    outer = window.descendants()
    native = window.nodes[0].descendants()
    window.descendants = lambda: outer
    window.nodes[0].descendants = lambda: native
    browser.descendants = unreadable
    assert _codex_controls(window) == []


def test_nested_document_without_stable_ids_fails_closed():
    window, _, browser = tree([editor("Hub prompt"), button("Send message")], [])
    browser.element_info.runtime_id = None
    assert _codex_controls(window) == []


def test_legacy_tree_without_documents_keeps_native_controls():
    native_editor, native_send = editor("Hub prompt"), button("Send message")
    window = Node("Window", children=[native_editor, native_send])
    snapshot = inspect_codex_composer(window)
    assert snapshot["composer"] is native_editor
    assert snapshot["send_button"] is native_send
