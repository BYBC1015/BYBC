"""Offline UIA state machines for the old and 26.924 model pickers."""

import threading
from types import SimpleNamespace

import pytest

import codex_bridge.codex_app_submitter as submitter


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class Control:
    def __init__(self, owner, kind, name, *, action=None, visible=True):
        self.owner, self.kind, self.name = owner, kind, name
        self.action, self.visible = action, visible

    @property
    def element_info(self):
        name = self.name() if callable(self.name) else self.name
        return SimpleNamespace(
            control_type=self.kind,
            name=name,
            class_name="",
            rectangle=SimpleNamespace(top=500),
        )

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return True

    def invoke(self):
        self.owner.events.append(("invoke", self.element_info.name))
        assert self.action is not None, "A noninteractive control was invoked"
        self.action()

    def set_focus(self):
        self.owner.events.append(("focus", self.element_info.name))
        self.owner.focused = self.owner.focus_succeeds

    def has_keyboard_focus(self):
        return self.owner.focused

    def type_keys(self, keys, *, set_foreground):
        assert self.owner.focused
        assert set_foreground is False
        self.owner.events.append(("key", keys))
        self.owner.step(keys)


class RuntimeMenu:
    LABELS = {
        "none": "无", "minimal": "极低", "low": "轻度", "medium": "中",
        "high": "高", "xhigh": "极高", "max": "Max", "ultra": "Ultra",
    }

    def __init__(self, clock, *, version="new", model="6 Astra", effort="high",
                 models=("5.6 Sol", "6 Astra"), efforts=("low", "high", "ultra"),
                 label_delay=0.0, focus_succeeds=True):
        self.clock, self.version = clock, version
        self.model, self.effort, self.display_effort = model, effort, effort
        self.models, self.efforts = tuple(models), tuple(efforts)
        self.label_delay, self.focus_succeeds = label_delay, focus_succeeds
        self.opened, self.focused = False, False
        self.view, self.submenu = "simple", ""
        self.events = []
        self.pending_at = None
        self.stale_label_reads = 0
        self.indices = []
        self.trigger = Control(self, "Button", self.trigger_name, action=self.toggle)

    def trigger_name(self):
        if self.opened and self.version == "new":
            return "选择强度"
        if self.pending_at is not None:
            if self.clock.monotonic() >= self.pending_at:
                self.display_effort = self.effort
                self.pending_at = None
            else:
                self.stale_label_reads += 1
        return f"GPT-{self.model} {self.LABELS[self.display_effort]}"

    def toggle(self):
        self.opened = not self.opened
        self.focused = False
        self.view, self.submenu = "simple", ""

    def choose_model_view(self):
        self.events.append(("navigate", "model"))
        self.view = "advanced"
        self.submenu = "model"

    def choose_effort_view(self):
        self.events.append(("navigate", "effort"))
        self.submenu = "effort"

    def choose_model(self, model):
        self.model = model
        self.view = "simple"
        # New radio choices preventDefault and keep the root menu open.
        self.events.append(("model", model, self.opened))
        if self.version == "old":
            self.opened = False

    def choose_effort(self, effort):
        self.effort = self.display_effort = effort
        self.opened = False

    def step(self, keys):
        index = self.efforts.index(self.effort)
        change = -1 if keys == "{LEFT}" else 1
        next_index = min(max(index + change, 0), len(self.efforts) - 1)
        self.indices.append(next_index)
        next_effort = self.efforts[next_index]
        if next_effort != self.effort:
            self.effort = next_effort
            self.pending_at = self.clock.monotonic() + self.label_delay

    def descendants(self):
        controls = [self.trigger]
        if not self.opened:
            return controls
        controls.append(Control(self, "Menu", "Runtime menu"))
        if self.version == "new":
            if self.view == "simple":
                controls.extend([
                    Control(self, "MenuItem", "选择模型", action=self.choose_model_view),
                    Control(self, "MenuItem", "强度"),
                ])
            else:
                controls.extend(
                    Control(self, "RadioButton", f"GPT-{model}",
                            action=lambda model=model: self.choose_model(model))
                    for model in self.models
                )
        else:
            # Legacy parent sections remain visible before submenu children.
            controls.extend([
                Control(self, "MenuItem", "模型", action=self.choose_model_view),
                Control(self, "MenuItem", "推理强度", action=self.choose_effort_view),
            ])
            if self.submenu == "model":
                controls.extend(
                    Control(self, "MenuItem", model,
                            action=lambda model=model: self.choose_model(model))
                    for model in self.models
                )
            elif self.submenu == "effort":
                controls.extend(
                    Control(self, "MenuItem", self.LABELS[effort],
                            action=lambda effort=effort: self.choose_effort(effort))
                    for effort in self.efforts
                )
        return controls


@pytest.fixture
def menu_factory(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(submitter, "time", clock)
    monkeypatch.setattr(submitter, "focus_codex_window", lambda _hwnd: True)

    def no_native_input(*_args, **_kwargs):
        raise AssertionError("Offline menu tests must never issue native input")

    monkeypatch.setattr(submitter, "native_left_click", no_native_input)

    def create(**options):
        menu = RuntimeMenu(clock, **options)
        monkeypatch.setattr(
            submitter, "Desktop",
            lambda **_kwargs: SimpleNamespace(window=lambda **_kwargs: menu),
        )
        return menu

    return create


def configure(model, effort):
    return submitter._configure_codex_execution(
        123,
        model_label=model,
        reasoning_label=submitter.CODEX_REASONING_LABELS[effort],
        reasoning_effort=effort,
    )


def test_new_model_radio_returns_to_simple_view_and_closes_original_trigger(menu_factory):
    menu = menu_factory(model="5.6 Sol")

    assert configure("6 Astra", "high") == (True, "")

    assert menu.model == "6 Astra"
    assert ("navigate", "model") in menu.events
    assert ("model", "6 Astra", True) in menu.events
    assert menu.events[-1] == ("invoke", "选择强度")
    assert not menu.opened


def test_legacy_submenu_parents_do_not_shadow_model_or_effort_options(menu_factory):
    menu = menu_factory(version="old", model="5.6 Sol", effort="low")

    assert configure("6 Astra", "high") == (True, "")

    assert menu.events.count(("navigate", "model")) == 1
    assert menu.events.count(("navigate", "effort")) == 1
    assert menu.model == "6 Astra" and menu.effort == "high"
    assert not menu.opened


@pytest.mark.parametrize("version", ["new", "old"])
def test_model_with_shorter_name_is_not_shadowed_by_mini(menu_factory, version):
    menu = menu_factory(version=version, model="5.6 Sol", models=("5.4 Mini", "5.4"))

    assert configure("5.4", "high") == (True, "")

    assert menu.model == "5.4"
    assert not any(event[:2] == ("model", "5.4 Mini") for event in menu.events)


@pytest.mark.parametrize(
    "efforts,initial,target,expected_directions",
    [
        (("ultra", "low", "high", "xhigh"), "high", "ultra", {"{LEFT}"}),
        (("high", "ultra", "low", "xhigh"), "high", "xhigh", {"{LEFT}", "{RIGHT}"}),
        (("max", "ultra", "xhigh", "high"), "high", "max", {"{LEFT}"}),
    ],
)
def test_slider_discovers_backend_order_with_clamped_bounded_arrows(
    menu_factory, efforts, initial, target, expected_directions
):
    menu = menu_factory(efforts=efforts, effort=initial)

    assert configure("6 Astra", target) == (True, "")

    keys = [event[1] for event in menu.events if event[0] == "key"]
    assert set(keys) == expected_directions
    assert len(keys) <= 16
    assert all(0 <= index < len(efforts) for index in menu.indices)
    assert menu.effort == target
    assert not menu.opened


def test_missing_effort_returns_failure_after_bounded_scan(menu_factory):
    menu = menu_factory(efforts=("medium", "high"), effort="high")

    ok, detail = configure("6 Astra", "ultra")

    assert not ok and "未提供推理强度" in detail
    keys = [event[1] for event in menu.events if event[0] == "key"]
    assert "{LEFT}" in keys and "{RIGHT}" in keys
    assert len(keys) <= 16
    assert not menu.opened


def test_slider_focus_failure_never_issues_arrow_key(menu_factory):
    menu = menu_factory(focus_succeeds=False)

    ok, detail = configure("6 Astra", "ultra")

    assert not ok and "焦点" in detail
    assert any(event[0] == "focus" for event in menu.events)
    assert not any(event[0] == "key" for event in menu.events)
    assert not menu.opened


def test_slider_without_foreground_never_opens_or_types(menu_factory):
    menu = menu_factory()
    ok, detail = submitter._select_codex_slider_effort(menu, "Ultra", foreground_ready=False)
    assert not ok and "前台" in detail
    assert menu.events == []


def test_delayed_closed_trigger_update_is_not_mistaken_for_slider_endpoint(menu_factory):
    menu = menu_factory(efforts=("high", "ultra"), effort="high", label_delay=0.55)

    assert configure("6 Astra", "ultra") == (True, "")

    assert menu.stale_label_reads > 0
    assert menu.display_effort == "ultra"
    assert [event[1] for event in menu.events if event[0] == "key"] == ["{LEFT}", "{RIGHT}"]
    assert not menu.opened


@pytest.mark.parametrize("effort", ["xhigh", "max", "ultra"])
def test_matching_settings_do_not_open_menu(menu_factory, effort):
    menu = menu_factory(effort=effort)
    assert configure("6 Astra", effort) == (True, "")
    assert menu.events == []


@pytest.mark.parametrize("label,actual", [("极高", "xhigh"), ("Max", "max"), ("Ultra", "ultra")])
@pytest.mark.parametrize("requested", ["high", "xhigh", "max", "ultra"])
def test_distinct_closed_effort_labels_cannot_confirm_another_effort(label, actual, requested):
    assert submitter._reasoning_button_name_matches(f"GPT-6 Astra {label}", requested) is (actual == requested)


def test_timed_out_uia_read_cannot_start_late_menu_activation(menu_factory, monkeypatch):
    menu = menu_factory(model="5.6 Sol")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_descendants = menu.descendants
    original_configure = submitter._configure_codex_execution

    def blocked_read():
        entered.set()
        assert release.wait(5), "Test did not release the simulated UIA read"
        return original_descendants()

    def observe_worker(*args, **kwargs):
        try:
            return original_configure(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(menu, "descendants", blocked_read)
    monkeypatch.setattr(submitter, "_configure_codex_execution", observe_worker)
    try:
        ok, detail = submitter.configure_codex_execution(
            123, model="gpt-6-astra", reasoning_effort="high", timeout_seconds=2.0
        )
        assert entered.is_set()
        assert not ok and "超过" in detail
        assert menu.events == []
    finally:
        release.set()
        assert finished.wait(2), "Timed-out worker did not finish after its read returned"
    assert menu.events == []
