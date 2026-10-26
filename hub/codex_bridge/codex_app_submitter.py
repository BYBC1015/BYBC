from __future__ import annotations

import ctypes
import re
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

from .codex_app_probe import WindowsCodexAppProbe

try:
    from pywinauto import Desktop
except ImportError:  # Source-only fallback; packaged EXE includes pywinauto.
    Desktop = None


SW_RESTORE = 9
VK_MENU = 0x12
VK_CONTROL = 0x11
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DEFAULT_UIA_OPERATION_TIMEOUT_SECONDS = 5.0
WINDOW_TITLE_TIMEOUT_MS = 250
CODEX_MODEL_LABELS = {
    "gpt-6-astra": "6 Astra",
    "gpt-6-sol": "6 Sol",
    "gpt-6-luna": "6 Luna",
    "gpt-5.6-sol": "5.6 Sol",
    "gpt-5.6-terra": "5.6 Terra",
    "gpt-5.6-luna": "5.6 Luna",
    "gpt-5.5": "5.5",
    "gpt-5.4": "5.4",
    "gpt-5.4-mini": "5.4 Mini",
    "gpt-5.3-codex-spark": "5.3 Codex Spark",
}
CODEX_REASONING_LABELS = {
    "none": "无",
    "minimal": "极低",
    "low": "轻度",
    "medium": "中",
    "high": "高",
    "xhigh": "极高",
    "max": "最高",
    "ultra": "Ultra",
}
CODEX_SEND_BUTTON_NAMES = frozenset(
    {"", "send", "submit", "发送", "send message", "submit prompt", "发送消息"}
)
_runtime_automation_context = threading.local()


class UIAutomationTimeoutError(TimeoutError):
    pass


class CodexAppSubmitter:
    def __init__(
        self,
        codex_exe_path: Path | str | None = None,
        window_timeout_seconds: float = 20.0,
        poll_interval_seconds: float = 0.5,
        reopen_interval_seconds: float = 8.0,
        reopen_settle_seconds: float = 1.5,
        max_reopen_attempts: int = 1,
        process_probe=None,
    ):
        self.codex_exe_path = Path(codex_exe_path) if codex_exe_path else None
        self.window_timeout_seconds = max(1.0, float(window_timeout_seconds))
        self.poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self.reopen_interval_seconds = max(0.0, float(reopen_interval_seconds))
        self.reopen_settle_seconds = max(0.0, float(reopen_settle_seconds))
        self.max_reopen_attempts = max(0, int(max_reopen_attempts))
        self.process_probe = process_probe or WindowsCodexAppProbe()
        self.last_error = ""
        self.last_send_attempted = False
        self.last_recovery_action = ""

    def submit_after_delay(
        self,
        delay_seconds: float,
        hotkey: str,
        expected_prompt: str = "",
        reopen_callback: Callable[[], None] | None = None,
        model: str = "",
        reasoning_effort: str = "",
    ) -> bool:
        self.last_send_attempted = False
        self.last_recovery_action = ""
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        deadline = time.monotonic() + self.window_timeout_seconds
        reopen_attempts = 0
        # A freshly opened deep link can expose the Codex window before its
        # composer is ready. Poll that same window first instead of immediately
        # opening another task, which can leave duplicate blank drafts behind.
        next_reopen_at = time.monotonic() + self.reopen_interval_seconds
        while True:
            if model or reasoning_effort:
                submitted = self.submit(
                    hotkey,
                    expected_prompt=expected_prompt,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
            else:
                submitted = self.submit(hotkey, expected_prompt=expected_prompt)
            if submitted:
                return True
            # A failed verification is not proof that the send failed. Once any
            # input capable of submitting the draft has been issued, stop here
            # and let the durable delivery lock require human confirmation.
            # Retrying in this state can submit the same Hub item repeatedly.
            if self.last_send_attempted:
                return False
            now = time.monotonic()
            if now >= deadline:
                return False
            if not should_retry_submission(self.last_error):
                return False
            if (
                reopen_callback is not None
                and reopen_attempts < self.max_reopen_attempts
                and now >= next_reopen_at
                and should_reopen_target_prompt(self.last_error)
            ):
                codex_running = self.process_probe.is_running(self.codex_exe_path)
                self.last_recovery_action = (
                    "reopen_prompt" if codex_running else "start_codex"
                )
                try:
                    reopen_callback()
                except Exception as exc:
                    self.last_error = f"重新打开 Codex 目标草稿失败：{exc}"
                reopen_attempts += 1
                next_reopen_at = now + self.reopen_interval_seconds
                if self.reopen_settle_seconds > 0:
                    time.sleep(self.reopen_settle_seconds)
            time.sleep(self.poll_interval_seconds)

    def submit(
        self,
        hotkey: str,
        expected_prompt: str = "",
        model: str = "",
        reasoning_effort: str = "",
    ) -> bool:
        self.last_send_attempted = False
        if not sys.platform.startswith("win"):
            self.last_error = "当前系统不支持 Codex 窗口自动发送。"
            return False
        hwnd = find_codex_window(self.codex_exe_path)
        if not hwnd:
            self.last_error = "未找到属于 Codex 应用的可见窗口。"
            return False
        submit_kwargs = {
            "expected_prompt": expected_prompt,
            "hotkey": hotkey,
            "on_send_attempt": self._mark_send_attempted,
        }
        if model or reasoning_effort:
            submit_kwargs.update(
                model=str(model or ""),
                reasoning_effort=str(reasoning_effort or ""),
            )
        try:
            submitted, detail = submit_codex_composer(hwnd, **submit_kwargs)
        except TypeError as exc:
            if "on_send_attempt" not in str(exc):
                raise
            submitted, detail = submit_codex_composer(
                hwnd,
                expected_prompt=expected_prompt,
                hotkey=hotkey,
            )
        if submitted:
            self.last_error = ""
            return True
        self.last_error = detail
        return False

    def _mark_send_attempted(self) -> None:
        self.last_send_attempted = True


def configure_codex_execution(
    hwnd: int,
    *,
    model: str = "",
    reasoning_effort: str = "",
    timeout_seconds: float = 40.0,
) -> tuple[bool, str]:
    """Select and verify the native Codex model settings before sending."""

    clean_model = str(model or "").strip().lower()
    clean_effort = str(reasoning_effort or "").strip().lower()
    if clean_model and not re.fullmatch(r"gpt-[a-z0-9]+(?:[.\-][a-z0-9]+)*", clean_model):
        return False, f"当前 Codex 应用不支持模型 {model}"
    if clean_effort and clean_effort not in CODEX_REASONING_LABELS:
        return False, f"当前 Codex 应用不支持推理强度 {reasoning_effort}"
    if not clean_model and not clean_effort:
        return True, ""
    if Desktop is None:
        return False, "自动模型选择组件未安装"

    cancelled = threading.Event()
    bounded_timeout = max(2.0, float(timeout_seconds))

    def operation():
        _runtime_automation_context.cancelled = cancelled
        _runtime_automation_context.deadline = time.monotonic() + bounded_timeout
        try:
            return _configure_codex_execution(
                hwnd,
                model_label=codex_model_label(clean_model),
                reasoning_label=CODEX_REASONING_LABELS.get(clean_effort, ""),
                reasoning_effort=clean_effort,
            )
        finally:
            _runtime_automation_context.__dict__.clear()

    try:
        return call_with_timeout(
            operation,
            timeout_seconds=bounded_timeout,
            description="Codex 模型设置",
        )
    except UIAutomationTimeoutError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"无法操作 Codex 模型菜单：{exc}"
    finally:
        # A UIA read cannot be forcibly cancelled. Prevent a late read from
        # triggering any further inputs after its caller has timed out.
        cancelled.set()


def _configure_codex_execution(
    hwnd: int,
    *,
    model_label: str,
    reasoning_label: str,
    reasoning_effort: str,
) -> tuple[bool, str]:
    window = Desktop(backend="uia").window(handle=hwnd)
    foreground_ready = focus_codex_window(hwnd)

    button = _find_codex_runtime_button(_codex_controls(window))
    button_name = _normalize_uia_name(button.element_info.name) if button else ""
    if model_label and not _codex_model_button_name_matches(button_name, model_label):
        ok, detail = _select_codex_runtime_option(
            window,
            section_prefix="模型",
            option_label=model_label,
            foreground_ready=foreground_ready,
        )
        if not ok:
            return False, detail
        button_name = _wait_for_runtime_button_name(window, model_label)
        if not button_name:
            return False, f"选择后未在 Codex 编辑器确认模型 {model_label}"

    button = _find_codex_runtime_button(_codex_controls(window))
    button_name = _normalize_uia_name(button.element_info.name) if button else ""
    if reasoning_label and not _reasoning_button_name_matches(button_name, reasoning_effort):
        ok, detail = _select_codex_runtime_option(
            window,
            section_prefix="推理强度",
            option_label=reasoning_label,
            foreground_ready=foreground_ready,
        )
        if not ok:
            return False, detail
        button_name = _wait_for_reasoning_button_name(window, reasoning_effort)
        if not button_name:
            return False, f"选择后未在 Codex 编辑器确认推理强度 {reasoning_label}"

    # Effort changes in the default slider may also change the model. Confirm
    # both requested settings together before returning to draft verification.
    button = _find_codex_runtime_button(_codex_controls(window))
    button_name = _normalize_uia_name(button.element_info.name) if button else ""
    if model_label and not _codex_model_button_name_matches(button_name, model_label):
        return False, f"未在 Codex 编辑器确认模型 {model_label}"
    if reasoning_label and not _reasoning_button_name_matches(button_name, reasoning_effort):
        return False, f"未在 Codex 编辑器确认推理强度 {reasoning_label}"

    return True, ""


def _select_codex_runtime_option(
    window,
    *,
    section_prefix: str,
    option_label: str,
    foreground_ready: bool,
) -> tuple[bool, str]:
    runtime_button = _find_codex_runtime_button(_codex_controls(window))
    if runtime_button is None:
        return False, "未找到 Codex 模型选择按钮"
    _activate_uia_control(runtime_button, foreground_ready=foreground_ready)

    use_slider = False
    try:
        # 26.924 uses a Select model view toggle and radio menu items. Older
        # releases used Model / Reasoning effort submenus. Inspect both in one
        # bounded poll; never mistake the trigger itself for an option.
        if section_prefix == "模型":
            section_names = {"模型", "Model", "选择模型", "Select model"}
            option_matches = lambda name: _codex_model_button_name_matches(name, option_label)
        else:
            section_names = {"推理强度", "Reasoning effort"}
            option_matches = lambda name: _runtime_option_name_matches(name, option_label)
        navigated = set()
        for _ in range(3):
            control = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                choices = []
                for item in _codex_controls(window):
                    info = item.element_info
                    if (str(info.control_type or "") in {"MenuItem", "ListItem", "RadioButton"}
                            and item.is_visible() and item.is_enabled()):
                        choices.append((item, _normalize_uia_name(info.name)))
                # Parent submenu items remain visible before their children.
                # Always prefer a concrete option over another navigation.
                control = next((item for item, name in choices if option_matches(name)), None)
                if control is None:
                    for item, name in choices:
                        is_section = any(name == s or name.startswith(s + " ") for s in section_names)
                        is_slider = section_prefix == "推理强度" and name in {"强度", "Power"}
                        if name not in navigated and (is_section or is_slider):
                            control = item
                            break
                if control is not None:
                    break
                time.sleep(0.08)
            if control is None:
                return False, f"未找到 Codex {section_prefix}菜单或选项 {option_label}"
            name = _normalize_uia_name(control.element_info.name)
            if section_prefix == "推理强度" and name in {"强度", "Power"}:
                use_slider = True
                break
            _activate_uia_control(control, foreground_ready=foreground_ready)
            if option_matches(name):
                return True, ""
            navigated.add(name)
        if not use_slider:
            return False, f"未找到 Codex 选项 {option_label}"
    finally:
        _close_codex_runtime_menu(window, runtime_button, foreground_ready=foreground_ready)
    return _select_codex_slider_effort(window, option_label, foreground_ready=foreground_ready)


def _close_codex_runtime_menu(window, trigger, *, foreground_ready: bool) -> None:
    # The new menu stays open after choosing a model and changes its trigger
    # label to Select power. Keep the original trigger, rather than rediscover
    # it by a model name which is no longer present while the menu is open.
    if any(str(item.element_info.control_type or "") == "Menu" and item.is_visible()
           for item in _codex_controls(window)):
        _activate_uia_control(trigger, foreground_ready=foreground_ready)


def _select_codex_slider_effort(window, option_label: str, *, foreground_ready: bool) -> tuple[bool, str]:
    effort = next((key for key, value in CODEX_REASONING_LABELS.items() if value == option_label), "")
    if not foreground_ready:
        return False, "强度调节需要 Codex 窗口在前台，未发送按键"
    trigger = _find_codex_runtime_button(_codex_controls(window))
    if trigger is None or not effort:
        return False, "无法确认 Codex 强度调节目标"
    current = _normalize_uia_name(trigger.element_info.name)
    model_label = next((label for label in sorted(CODEX_MODEL_LABELS.values(), key=len, reverse=True)
                        if _codex_model_button_name_matches(current, label)), "")
    if not model_label:
        return False, "强度调节前无法确认当前模型"

    # supportedReasoningEfforts preserves backend order. Do not assume an
    # alphabetical or numeric order: scan to the clamped left endpoint, then
    # right. Close after each step to read the real model/effort label (the open
    # trigger only says Select power). Every key requires verified item focus.
    for direction in ("{LEFT}", "{RIGHT}"):
        for _ in range(len(CODEX_REASONING_LABELS)):
            if _reasoning_button_name_matches(current, effort):
                return True, ""
            trigger = _find_codex_runtime_button(_codex_controls(window))
            if trigger is None:
                return False, "未找到 Codex 强度选择按钮"
            _activate_uia_control(trigger, foreground_ready=foreground_ready)
            try:
                slider = _wait_for_named_control(window, lambda name: name in {"强度", "Power"},
                                                 allowed_control_types=("MenuItem",))
                if slider is None:
                    return False, "未找到 Codex 强度调节控件"
                _check_runtime_action_allowed()
                slider.set_focus()
                if not slider.has_keyboard_focus():
                    return False, "未确认 Codex 强度控件焦点，未发送按键"
                _check_runtime_action_allowed()
                slider.type_keys(direction, set_foreground=False)
                time.sleep(0.12)
            finally:
                _close_codex_runtime_menu(window, trigger, foreground_ready=foreground_ready)
            updated = _wait_for_changed_runtime_button_name(window, model_label, current)
            if not updated:
                return False, "调节强度后模型发生变化或不可确认"
            if _reasoning_button_name_matches(updated, effort):
                return True, ""
            if updated == current:
                break
            current = updated
    return False, f"当前 Codex 模型未提供推理强度 {option_label}"


def _wait_for_changed_runtime_button_name(window, model_label, previous, timeout_seconds=1.0):
    deadline = time.monotonic() + timeout_seconds
    latest = ""
    while time.monotonic() < deadline:
        button = _find_codex_runtime_button(_codex_controls(window))
        latest = _normalize_uia_name(button.element_info.name) if button else ""
        if latest and not _codex_model_button_name_matches(latest, model_label):
            return ""
        if latest and latest != previous:
            return latest
        time.sleep(0.08)
    return latest


def _verify_reasoning_menu_selection(
    window,
    option_label: str,
    *,
    foreground_ready: bool,
) -> bool | None:
    runtime_button = _find_codex_runtime_button(_codex_controls(window))
    if runtime_button is None:
        return None
    _activate_uia_control(runtime_button, foreground_ready=foreground_ready)
    section = _wait_for_named_control(
        window,
        lambda value: value.startswith("推理强度 ") or value == "推理强度",
        allowed_control_types=("MenuItem", "ListItem"),
    )
    if section is None:
        return None
    _activate_uia_control(section, foreground_ready=foreground_ready)
    target = _wait_for_named_control(
        window,
        lambda value: _runtime_option_name_matches(value, option_label),
        excluded_prefixes=("推理强度",),
        allowed_control_types=("MenuItem", "ListItem", "RadioButton"),
    )
    if target is None:
        return None
    selected = _uia_control_is_selected(target)
    # Close the popover before the final draft verification and send action.
    runtime_button = _find_codex_runtime_button(_codex_controls(window))
    if runtime_button is not None:
        try:
            _activate_uia_control(runtime_button, foreground_ready=foreground_ready)
        except Exception:
            pass
    return selected


def _find_codex_runtime_button(descendants):
    candidates = []
    model_labels = tuple(CODEX_MODEL_LABELS.values())
    for control in descendants:
        try:
            info = control.element_info
            name = _normalize_uia_name(info.name)
            if (
                str(info.control_type or "") == "Button"
                and control.is_visible()
                and (any(label.casefold() in name.casefold() for label in model_labels)
                     or re.search(r"\bGPT[- ]\d", name, re.IGNORECASE))
            ):
                candidates.append(control)
        except Exception:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.element_info.rectangle.top)


def _wait_for_named_control(
    window,
    predicate: Callable[[str], bool],
    *,
    excluded_prefixes: tuple[str, ...] = (),
    allowed_control_types: tuple[str, ...] = (),
    timeout_seconds: float = 2.0,
):
    deadline = time.monotonic() + max(0.2, float(timeout_seconds))
    while time.monotonic() < deadline:
        for control in _codex_controls(window):
            try:
                if not control.is_visible():
                    continue
                info = control.element_info
                if allowed_control_types and str(info.control_type or "") not in (
                    allowed_control_types
                ):
                    continue
                name = _normalize_uia_name(info.name)
                if not name or any(name.startswith(item) for item in excluded_prefixes):
                    continue
                if predicate(name):
                    return control
            except Exception:
                continue
        time.sleep(0.08)
    return None


def _activate_uia_control(control, *, foreground_ready: bool) -> None:
    _check_runtime_action_allowed()
    invoke = getattr(control, "invoke", None)
    if callable(invoke):
        try:
            invoke()
            time.sleep(0.12)
            return
        except Exception:
            pass
    click_input = getattr(control, "click_input", None)
    _check_runtime_action_allowed()
    if callable(click_input) and foreground_ready:
        click_input()
        time.sleep(0.12)
        return
    if not foreground_ready:
        raise RuntimeError("Codex 窗口不在前台且该菜单不支持 UIA Invoke")
    native_left_click(control.element_info.rectangle)
    time.sleep(0.12)


def _check_runtime_action_allowed() -> None:
    cancelled = getattr(_runtime_automation_context, "cancelled", None)
    deadline = getattr(_runtime_automation_context, "deadline", None)
    if (cancelled is not None and cancelled.is_set()) or (deadline is not None and time.monotonic() >= deadline):
        raise UIAutomationTimeoutError("Codex 模型设置已超时，停止后续操作")


def _runtime_option_name_matches(value: str, option_label: str) -> bool:
    name = re.sub(r"^gpt[- ]?", "", _normalize_uia_name(value).casefold())
    target = re.sub(r"^gpt[- ]?", "", _normalize_uia_name(option_label).casefold())
    if name == target:
        return True
    if target in {"ultra", "极高 更快消耗使用额度".casefold()}:
        return "极高" in name and "更快消耗使用额度" in name
    return name.startswith(target + " ")


def codex_model_label(model: str) -> str:
    if not model:
        return ""
    return CODEX_MODEL_LABELS.get(model, model.removeprefix("gpt-").replace("-", " ").title())


def _wait_for_runtime_button_name(
    window, expected_label: str, timeout_seconds: float = 2.0
) -> str:
    deadline = time.monotonic() + max(0.2, float(timeout_seconds))
    while time.monotonic() < deadline:
        button = _find_codex_runtime_button(_codex_controls(window))
        if button is not None:
            name = _normalize_uia_name(button.element_info.name)
            if _codex_model_button_name_matches(name, expected_label):
                return name
        time.sleep(0.08)
    return ""


def _codex_model_button_name_matches(value: str, expected_label: str) -> bool:
    name = _normalize_uia_name(value).casefold()
    if expected_label not in CODEX_MODEL_LABELS.values():
        return _runtime_option_name_matches(name, expected_label)
    detected = next(
        (
            label
            for label in sorted(
                CODEX_MODEL_LABELS.values(), key=len, reverse=True
            )
            if label.casefold() in name
        ),
        "",
    )
    return detected.casefold() == str(expected_label or "").casefold()


def _wait_for_reasoning_button_name(
    window, reasoning_effort: str, timeout_seconds: float = 2.0
) -> str:
    deadline = time.monotonic() + max(0.2, float(timeout_seconds))
    while time.monotonic() < deadline:
        button = _find_codex_runtime_button(_codex_controls(window))
        if button is not None:
            name = _normalize_uia_name(button.element_info.name)
            if _reasoning_button_name_matches(name, reasoning_effort):
                return name
        time.sleep(0.08)
    return ""


def _reasoning_button_name_matches(value: str, effort: str) -> bool:
    name = _normalize_uia_name(value)
    aliases = {"none": ("none", "无"), "minimal": ("minimal", "极低"),
               "low": ("low", "轻度"), "medium": ("medium", "中"),
               "high": ("high", "高"), "xhigh": ("xhigh", "extra high", "极高"),
               "max": ("max", "最高"), "ultra": ("ultra", "极高 更快消耗使用额度")}
    # Match a whole suffix, so 高 cannot also mean 最高/极高, nor xhigh
    # accidentally accept Ultra. Closed 26.924 buttons expose distinct labels.
    return any(name.casefold().endswith(" " + label.casefold())
               for label in aliases.get(effort, ()))


def _uia_control_is_selected(control) -> bool | None:
    is_selected = getattr(control, "is_selected", None)
    if callable(is_selected):
        try:
            return bool(is_selected())
        except Exception:
            pass
    get_toggle_state = getattr(control, "get_toggle_state", None)
    if callable(get_toggle_state):
        try:
            return int(get_toggle_state()) > 0
        except Exception:
            pass
    return None


def _normalize_uia_name(value: object) -> str:
    return " ".join(str(value or "").split())


def submit_codex_composer(
    hwnd: int,
    expected_prompt: str = "",
    hotkey: str = "enter",
    model: str = "",
    reasoning_effort: str = "",
    verification_timeout_seconds: float = 20.0,
    inspection_timeout_seconds: float = DEFAULT_UIA_OPERATION_TIMEOUT_SECONDS,
    on_send_attempt: Callable[[], None] | None = None,
) -> tuple[bool, str]:
    """Submit a verified Codex draft without blocking on UIA actions."""
    if Desktop is None:
        return False, "自动发送组件未安装，未执行盲按键。"
    try:
        before = inspect_codex_target_with_timeout(
            hwnd,
            timeout_seconds=inspection_timeout_seconds,
        )
    except UIAutomationTimeoutError as exc:
        return False, f"读取 Codex 编辑器超时，未执行发送：{exc}"
    except Exception as exc:
        return False, f"无法读取 Codex 编辑器：{exc}"
    if not before["composer_found"]:
        return False, "未找到 Codex 可发送编辑器。"
    if before["send_button"] is None:
        if before["stop_button_found"]:
            return False, "Codex 当前正在执行，发送按钮尚不可用。"
        return False, "未找到 Codex 发送按钮。"
    if not before["draft_text"]:
        return False, "Codex 编辑器中没有可发送的提示。"
    if expected_prompt and not draft_matches_expected_prompt(
        str(before["draft_text"]), expected_prompt
    ):
        return False, "Codex 当前草稿不是 Bridge 刚打开的 Hub 提示，未发送。"

    if model or reasoning_effort:
        configured, configuration_detail = configure_codex_execution(
            hwnd,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=max(inspection_timeout_seconds, 40.0),
        )
        if not configured:
            return False, f"Codex 模型设置未确认，未发送：{configuration_detail}"
        try:
            before = inspect_codex_target_with_timeout(
                hwnd,
                timeout_seconds=inspection_timeout_seconds,
            )
        except UIAutomationTimeoutError as exc:
            return False, f"模型设置后复核 Codex 编辑器超时，未发送：{exc}"
        except Exception as exc:
            return False, f"模型设置后无法复核 Codex 编辑器，未发送：{exc}"
        if not before["composer_found"] or before["send_button"] is None:
            return False, "模型设置后 Codex 编辑器或发送按钮不可用，未发送。"
        if expected_prompt and not draft_matches_expected_prompt(
            str(before["draft_text"]), expected_prompt
        ):
            return False, "模型设置后 Codex 草稿不再匹配 Hub 提示，未发送。"

    focused = focus_codex_window(hwnd)
    if not focused and not callable(getattr(before["send_button"], "invoke", None)):
        return False, "已找到目标 Codex 草稿，但无法将窗口切换到前台。"
    original_draft = str(before["draft_text"])
    if focused and before["send_button_rect"] is not None:
        strategy_name = "原生发送按钮点击"
        action = click_codex_send_button
    else:
        # A foreground app running at higher integrity (for example a VPN UI)
        # can legally block SetForegroundWindow. UIA Invoke does not need to
        # steal focus. It is a single alternative selected before any send
        # action, never a fallback after an unverified action.
        strategy_name = "UIA invoke"
        action = invoke_codex_send_button

    try:
        ready = inspect_codex_target_with_timeout(
            hwnd,
            timeout_seconds=inspection_timeout_seconds,
        )
    except UIAutomationTimeoutError as exc:
        return False, f"{strategy_name} 前复核 Codex 编辑器超时：{exc}"
    except Exception as exc:
        return False, f"{strategy_name} 前无法复核 Codex 编辑器：{exc}"
    already_submitted, detail = codex_submission_state(ready, original_draft)
    if already_submitted:
        return True, ""
    if detail:
        return False, detail
    if ready["send_button"] is None or ready["draft_text"] != original_draft:
        return False, "Codex 草稿或发送按钮在提交前发生变化，未发送。"
    if expected_prompt and not draft_matches_expected_prompt(
        str(ready["draft_text"]), expected_prompt
    ):
        return False, "Codex 当前草稿不再匹配 Bridge 的 Hub 提示，未发送。"
    try:
        if on_send_attempt is not None:
            on_send_attempt()
        action(ready)
    except Exception as exc:
        return False, f"{strategy_name}发送动作结果不确定：{exc}"

    submitted, detail = wait_for_codex_submission(
        hwnd,
        original_draft,
        timeout_seconds=verification_timeout_seconds,
        inspection_timeout_seconds=inspection_timeout_seconds,
    )
    if submitted:
        return True, ""
    suffix = f"：{detail}" if detail else ""
    return (
        False,
        "已执行一次发送动作，但未确认 Codex 开始执行或清空草稿；"
        f"为避免重复发送，已停止自动重试{suffix}",
    )


def invoke_codex_send_button(snapshot: dict[str, object]) -> None:
    send_button = snapshot.get("send_button")
    if send_button is None:
        raise RuntimeError("Codex send button is unavailable")
    invoke = getattr(send_button, "invoke", None)
    if not callable(invoke):
        raise RuntimeError("Codex send button does not support UIA Invoke")
    invoke()


def click_codex_send_button(snapshot: dict[str, object]) -> None:
    send_button_rect = snapshot.get("send_button_rect")
    if send_button_rect is None:
        raise RuntimeError("发送按钮不可用")
    native_left_click(send_button_rect)


def press_codex_submit_hotkey(snapshot: dict[str, object], hotkey: str) -> None:
    composer_rect = snapshot.get("composer_rect")
    if composer_rect is None:
        raise RuntimeError("编辑器不可用")
    native_left_click(composer_rect, vertical_fraction=0.35)
    send_submit_hotkey(hotkey)


def wait_for_codex_submission(
    target,
    original_draft: str,
    timeout_seconds: float,
    inspection_timeout_seconds: float = DEFAULT_UIA_OPERATION_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    last_detail = ""
    cleared_observations = 0
    while time.monotonic() < deadline:
        time.sleep(0.15)
        try:
            remaining = max(0.1, deadline - time.monotonic())
            after = inspect_codex_target_with_timeout(
                target,
                timeout_seconds=min(inspection_timeout_seconds, remaining),
            )
        except UIAutomationTimeoutError as exc:
            cleared_observations = 0
            last_detail = f"复核 Codex 编辑器超时：{exc}"
            continue
        except Exception as exc:
            cleared_observations = 0
            last_detail = str(exc)
            continue
        submitted, detail = codex_submission_state(after, original_draft)
        if submitted:
            if after.get("stop_button_found"):
                return True, ""
            # Electron can briefly omit the contenteditable text from UIA while
            # the visible draft is still present. Require two stable blank,
            # disabled-composer reads before treating a cleared draft as proof
            # of submission; a single missing accessibility node is not enough.
            cleared_observations += 1
            if cleared_observations >= 2:
                return True, ""
            last_detail = "Codex 草稿首次显示为空，等待稳定复核。"
            continue
        cleared_observations = 0
        if detail and "草稿内容发生变化" in detail:
            return False, detail
        if detail:
            last_detail = detail
    suffix = f"：{last_detail}" if last_detail else ""
    return False, f"未检测到 Codex 开始执行或清空草稿{suffix}"


def inspect_codex_target_with_timeout(
    target,
    timeout_seconds: float = DEFAULT_UIA_OPERATION_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Read UIA state on a disposable daemon thread with a real wall-clock bound."""

    def operation() -> dict[str, object]:
        window = (
            Desktop(backend="uia").window(handle=target)
            if isinstance(target, int)
            else target
        )
        return inspect_codex_composer(window)

    return call_with_timeout(
        operation,
        timeout_seconds=timeout_seconds,
        description="Codex UIA 检查",
    )


def call_with_timeout(
    operation: Callable[[], object],
    *,
    timeout_seconds: float,
    description: str,
):
    result: list[object] = []
    errors: list[BaseException] = []
    completed = threading.Event()

    def worker() -> None:
        try:
            result.append(operation())
        except BaseException as exc:  # Preserve the original UIA exception for logging.
            errors.append(exc)
        finally:
            completed.set()

    threading.Thread(
        target=worker,
        name="CodexBridge-UIA-read",
        daemon=True,
    ).start()
    bounded_timeout = max(0.1, float(timeout_seconds))
    if not completed.wait(bounded_timeout):
        raise UIAutomationTimeoutError(
            f"{description} 超过 {bounded_timeout:.1f} 秒"
        )
    if errors:
        raise errors[0]
    return result[0]


def codex_submission_state(
    snapshot: dict[str, object], original_draft: str
) -> tuple[bool, str]:
    if snapshot.get("stop_button_found"):
        return True, ""
    if not snapshot.get("composer_found"):
        return False, "Codex 编辑器暂时不可见，尚未确认提交。"
    current_draft = str(snapshot.get("draft_text") or "")
    if not normalize_prompt_text(current_draft) or is_composer_placeholder(
        current_draft
    ):
        if snapshot.get("send_button") is not None:
            return (
                False,
                "Codex 编辑器文本暂时不可读，但发送按钮仍可用，尚未确认提交。",
            )
        return True, ""
    if normalize_prompt_text(current_draft) != normalize_prompt_text(original_draft):
        return False, "Codex 草稿内容发生变化但未清空，停止自动发送以避免误发。"
    return False, ""


def _codex_document_value(control) -> str:
    readers = (
        lambda: control.iface_value.CurrentValue,
        lambda: control.get_value(),
        lambda: control.legacy_properties().get("Value", ""),
    )
    for reader in readers:
        try:
            value = str(reader() or "").strip()
            if value:
                return value
        except Exception:
            continue
    return ""


def _codex_controls(window) -> list:
    """Return native app controls, excluding embedded browser documents.

    Each call builds its own snapshot. UIA runtime IDs identify controls across
    separate subtree reads; Python wrapper identity is not stable in pywinauto.
    Older accessibility trees without Document nodes retain the legacy path.
    """
    try:
        descendants = list(window.descendants())
        documents = [
            control
            for control in descendants
            if str(control.element_info.control_type or "") == "Document"
        ]
        if not documents:
            return descendants
        roots = [
            control
            for control in documents
            if _codex_document_value(control).split("#", 1)[0].split("?", 1)[0]
            == "app://-/index.html"
        ]
        if len(roots) != 1:
            return []
        native_descendants = list(roots[0].descendants())
        nested_documents = [
            control
            for control in native_descendants
            if str(control.element_info.control_type or "") == "Document"
        ]
        if not nested_documents:
            return native_descendants

        identities = {}

        def identity(control):
            wrapper_id = id(control)
            if wrapper_id not in identities:
                runtime_id = tuple(control.element_info.runtime_id or ())
                if not runtime_id:
                    raise ValueError("UIA control has no stable runtime ID")
                # Keep the wrapper alive until the snapshot is complete so a
                # later subtree read cannot reuse its Python object ID.
                identities[wrapper_id] = (control, runtime_id)
            return identities[wrapper_id][1]

        excluded = set()
        for document in nested_documents:
            document_id = identity(document)
            if document_id in excluded:
                continue
            excluded.add(document_id)
            excluded.update(identity(control) for control in document.descendants())
        return [control for control in native_descendants if identity(control) not in excluded]
    except Exception:
        # A partial tree cannot establish that a send/stop belongs to Codex.
        return []


def inspect_codex_composer(window) -> dict[str, object]:
    descendants = _codex_controls(window)
    window_rect = window.element_info.rectangle
    send_button = find_codex_send_button(descendants, window_rect)
    stop_button = find_codex_stop_button(descendants)
    composers = []
    for control in descendants:
        try:
            info = control.element_info
            class_name = str(info.class_name or "")
            rect = info.rectangle
            if (
                "ProseMirror" in class_name
                and control.is_visible()
                and rect.width() > 100
                and rect.height() > 10
            ):
                composers.append(control)
        except Exception:
            continue
    if not composers:
        anchor = send_button or stop_button
        inferred = find_composer_ancestor(anchor, window) if anchor is not None else None
        if inferred is not None:
            composers.append(inferred)
    if not composers:
        return {
            "composer_found": False,
            "composer": None,
            "composer_rect": None,
            "draft_text": "",
            "send_button": send_button,
            "send_button_rect": control_rectangle(send_button),
            "stop_button_found": stop_button is not None,
        }
    composer = max(
        composers,
        key=lambda control: (
            control.element_info.rectangle.top,
            control.element_info.rectangle.width(),
        ),
    )
    composer_rect = composer.element_info.rectangle
    draft_parts: list[str] = []
    # Read from the composer's own accessibility subtree and do not filter by
    # the current viewport rectangle. Codex uses a scrollable contenteditable:
    # UIA still exposes off-screen text nodes, but their rectangles sit outside
    # the clipped editor bounds. Dropping those nodes can remove Project/Entry
    # identity lines and make the correct Hub draft look like a different one.
    try:
        composer_descendants = _codex_controls(composer)
    except Exception:
        composer_descendants = descendants
    for control in composer_descendants:
        try:
            info = control.element_info
            name = str(info.name or "").strip()
            if (
                str(info.control_type or "") == "Text"
                and name
                and not is_composer_placeholder(name)
            ):
                draft_parts.append(name)
        except Exception:
            continue
    # Chromium now exposes rich editor content through TextPattern even when
    # its individual text nodes are split, virtualized, or missing. Reading the
    # edit's own document preserves line/word boundaries and never includes the
    # transcript or toolbar text from an inferred ancestor.
    document_text = ""
    if "ProseMirror" in str(composer.element_info.class_name or ""):
        try:
            document_text = str(composer.iface_text.DocumentRange.GetText(-1) or "").strip()
        except Exception:
            pass
    if is_composer_placeholder(document_text):
        document_text = ""
    return {
        "composer_found": True,
        "composer": composer,
        "composer_rect": rectangle_tuple(composer_rect),
        "draft_text": document_text or "\n".join(draft_parts).strip(),
        "send_button": send_button,
        "send_button_rect": control_rectangle(send_button),
        "stop_button_found": stop_button is not None,
    }


def control_rectangle(control) -> tuple[int, int, int, int] | None:
    if control is None:
        return None
    try:
        return rectangle_tuple(control.element_info.rectangle)
    except Exception:
        return None


def rectangle_tuple(rect) -> tuple[int, int, int, int]:
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def is_composer_placeholder(value: str) -> bool:
    normalized = normalize_prompt_text(value).casefold().rstrip("?？")
    return normalized in {
        "要做些什么",
        "要求后续变更",
        "ask anything",
        "describe a task",
        "request follow-up changes",
        "what do you want to do",
        "what would you like to do",
        "使用 chatgpt work",
        "use chatgpt work",
    }


def native_left_click(
    rect: tuple[int, int, int, int] | object,
    *,
    vertical_fraction: float = 0.5,
) -> None:
    if user32 is None:
        raise RuntimeError("Windows 原生点击不可用")
    if isinstance(rect, tuple):
        left, top, right, bottom = rect
    else:
        left = int(getattr(rect, "left"))
        top = int(getattr(rect, "top"))
        right = int(getattr(rect, "right"))
        bottom = int(getattr(rect, "bottom"))
    if right <= left or bottom <= top:
        raise RuntimeError("目标点击区域无效")
    x = left + (right - left) // 2
    fraction = min(0.9, max(0.1, float(vertical_fraction)))
    y = top + int((bottom - top) * fraction)
    if not user32.SetCursorPos(x, y):
        raise ctypes.WinError(ctypes.get_last_error())
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def find_codex_send_button(descendants, window_rect):
    explicit = []
    fallback = []
    for control in descendants:
        try:
            info = control.element_info
            if str(info.control_type or "") != "Button":
                continue
            name = str(info.name or "").strip()
            class_name = str(info.class_name or "")
            enabled = control.is_enabled()
            visible = control.is_visible()
            if is_send_button(
                name,
                class_name,
                enabled=enabled,
                visible=visible,
            ):
                explicit.append(control)
                continue
            score = fallback_send_button_score(
                name=name,
                class_name=class_name,
                automation_id=str(getattr(info, "automation_id", "") or ""),
                rect=info.rectangle,
                window_rect=window_rect,
                enabled=enabled,
                visible=visible,
            )
            if score >= 0:
                fallback.append((score, control))
        except Exception:
            continue
    if explicit:
        return max(explicit, key=lambda item: item.element_info.rectangle.left)
    if fallback:
        return max(fallback, key=lambda item: item[0])[1]
    return None


def find_codex_stop_button(descendants):
    for control in descendants:
        try:
            info = control.element_info
            if str(info.control_type or "") != "Button":
                continue
            if is_stop_button(
                str(info.name or "").strip(),
                str(info.class_name or ""),
                control.is_visible(),
            ):
                return control
        except Exception:
            continue
    return None


def fallback_send_button_score(
    *,
    name: str,
    class_name: str,
    automation_id: str,
    rect,
    window_rect,
    enabled: bool,
    visible: bool,
) -> int:
    normalized = str(name or "").strip().casefold()
    classes = str(class_name or "")
    if (
        not enabled
        or not visible
        or normalized not in CODEX_SEND_BUTTON_NAMES
        or "opacity-50" in classes.split()
    ):
        return -1
    if str(automation_id or "").casefold() in {
        "view_1",
        "view_2",
        "view_3",
        "view_4",
    }:
        return -1
    width = rect.width()
    height = rect.height()
    window_width = window_rect.width()
    window_height = window_rect.height()
    if not (20 <= width <= 110 and 20 <= height <= 110):
        return -1
    if rect.left < window_rect.left + int(window_width * 0.55):
        return -1
    if rect.top < window_rect.top + int(window_height * 0.45):
        return -1
    score = 100
    if normalized:
        score += 300
    if "composer" in classes.casefold():
        score += 200
    score += max(0, rect.left - window_rect.left) // 10
    score += max(0, rect.top - window_rect.top) // 10
    return score


def find_composer_ancestor(anchor, window):
    current = anchor
    for _ in range(8):
        try:
            current = current.parent()
        except Exception:
            return None
        if current is None or current == window:
            return None
        try:
            rect = current.element_info.rectangle
            if rect.width() < 200 or rect.height() < 60 or not current.is_visible():
                continue
            text_length = 0
            for child in current.descendants():
                info = child.element_info
                if str(info.control_type or "") != "Text":
                    continue
                text_length += len(str(info.name or "").strip())
            if text_length >= 8:
                return current
        except Exception:
            continue
    return None


def is_send_button(name: str, class_name: str, *, enabled: bool, visible: bool) -> bool:
    normalized = str(name or "").strip().casefold()
    classes = str(class_name or "")
    return (
        enabled
        and visible
        and normalized in CODEX_SEND_BUTTON_NAMES
        and "size-token-button-composer" in classes
        and any(token in classes for token in ("bg-token-button-background", "bg-composer-primary"))
        and "opacity-50" not in classes.split()
    )


def is_stop_button(name: str, class_name: str, visible: bool) -> bool:
    normalized = str(name or "").strip().casefold()
    return (
        visible
        and normalized in {"stop", "停止", "stop response", "stop task", "停止响应", "停止任务"}
        and "size-token-button-composer" in str(class_name or "")
    )


def draft_matches_expected_prompt(draft_text: str, expected_prompt: str) -> bool:
    draft = normalize_hub_prompt_text(draft_text)
    expected = normalize_hub_prompt_text(expected_prompt)
    if not draft or not expected:
        return False
    draft_identity = extract_hub_item_identity(draft)
    expected_identity = extract_hub_item_identity(expected)
    if draft_identity and expected_identity and draft_identity != expected_identity:
        return False
    draft_path = extract_prompt_file_path(draft)
    expected_path = extract_prompt_file_path(expected)
    if draft_path and expected_path:
        if prompt_file_family(draft_path) != prompt_file_family(expected_path):
            return False
        draft = draft.replace(draft_path, "<prompt-file>")
        expected = expected.replace(expected_path, "<prompt-file>")
    # Compact file wrappers have no Project/Entry identity. Their common prefix
    # and suffix cannot distinguish Hub items, so require the entire wrapper,
    # including its file identity, before allowing any send action.
    if expected_path and expected_identity is None:
        return bool(draft_path) and draft == expected
    if draft == expected or expected in draft:
        return True
    if draft_identity and draft_identity == expected_identity:
        return True
    if len(expected) <= 160:
        return draft in expected and len(draft) >= max(24, len(expected) // 2)
    return expected[:80] in draft and expected[-80:] in draft


def normalize_prompt_text(value: str) -> str:
    return " ".join(str(value or "").split())


def normalize_hub_prompt_text(value: str) -> str:
    # Current Codex renders the initial Markdown heading in its rich editor.
    # UIA returns "Codex Hub Item" without the source's leading "# ". Only
    # normalize that known opening heading; hashes in task IDs/body stay intact.
    text = re.sub(r"\A\s*#{1,6}[ \t]+(?=Codex Hub(?:\s|$))", "", str(value or ""))
    return normalize_prompt_text(text)


def extract_prompt_file_path(value: str) -> str:
    match = re.search(r"Prompt file:\s*(\S+?\.md)(?:\s|$)", str(value or ""))
    return match.group(1) if match else ""


def prompt_file_family(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").casefold()
    directory, _, filename = normalized.rpartition("/")
    if not filename.endswith(".md"):
        return normalized
    stem = filename[:-3]
    if stem.startswith(("mention-", "task-")) and stem.count("-") >= 3:
        stem = re.sub(r"-\d+$", "", stem)
    return f"{directory}/{stem}.md" if directory else f"{stem}.md"


def extract_hub_item_identity(value: str) -> tuple[str, str, str] | None:
    text = normalize_hub_prompt_text(value)
    if not re.match(r"Codex Hub(?:\s|$)", text):
        return None
    project_match = re.search(
        r"Project:\s*.*?\(([a-z0-9][a-z0-9-]{1,62}[a-z0-9])\)",
        text,
        flags=re.IGNORECASE,
    )
    if not project_match:
        return None
    project_slug = project_match.group(1).casefold()
    entry_match = re.search(r"Entry:\s*#(\d+)", text, flags=re.IGNORECASE)
    if entry_match:
        return "entry", project_slug, entry_match.group(1)
    task_match = re.search(
        r"(?:Task|Task ID):\s*#?(\d+)", text, flags=re.IGNORECASE
    )
    if task_match:
        return "task", project_slug, task_match.group(1)
    return None


def should_reopen_target_prompt(last_error: str) -> bool:
    error = str(last_error or "")
    return any(
        marker in error
        for marker in (
            "未找到属于 Codex 应用的可见窗口",
            "未找到 Codex ProseMirror 编辑器",
            "未找到 Codex 可发送编辑器",
            "未找到 Codex 发送按钮",
            "Codex 编辑器中没有可发送的提示",
            "Codex 当前草稿不是 Bridge 刚打开的 Hub 提示",
        )
    )


def should_retry_submission(last_error: str) -> bool:
    error = str(last_error or "")
    return should_reopen_target_prompt(error) or any(
        marker in error
        for marker in (
            "Codex 当前正在执行",
            "无法将窗口切换到前台",
            "无法读取 Codex 编辑器",
            "无法复核 Codex 编辑器",
            "读取 Codex 编辑器超时",
            "复核 Codex 编辑器超时",
        )
    )


def send_submit_hotkey(hotkey: str) -> None:
    normalized = normalize_submit_hotkey(hotkey)
    if normalized in {"ctrl-enter", "ctrl+enter", "control-enter", "control+enter"}:
        key_down(VK_CONTROL)
        key_down(VK_RETURN)
        key_up(VK_RETURN)
        key_up(VK_CONTROL)
        return
    key_down(VK_RETURN)
    key_up(VK_RETURN)


def normalize_submit_hotkey(hotkey: str) -> str:
    normalized = (hotkey or "enter").strip().lower()
    if normalized in {"ctrl-enter", "ctrl+enter", "control-enter", "control+enter"}:
        return "ctrl-enter"
    return "enter"


def key_down(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, 0)


def key_up(vk: int) -> None:
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def find_codex_window(codex_exe_path: Path | str | None = None) -> int | None:
    if not sys.platform.startswith("win"):
        return None
    configured = Path(codex_exe_path) if codex_exe_path else None
    foreground = int(user32.GetForegroundWindow() or 0)
    matches: list[tuple[int, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        process_path = get_window_process_path(hwnd)
        # Never synchronously ask every unrelated app for its title. A window
        # whose UI thread is waiting on Bridge can otherwise deadlock EnumWindows.
        title = "" if process_path else get_window_title(hwnd)
        area = get_window_area(hwnd)
        score = codex_window_score(
            process_path,
            title,
            configured,
            is_foreground=int(hwnd) == foreground,
            area=area,
        )
        if score >= 0:
            matches.append((score, int(hwnd)))
        return True

    user32.EnumWindows(callback, 0)
    if not matches:
        return None
    return max(matches)[1]


def codex_window_score(
    process_path: str,
    title: str,
    configured_path: Path | str | None,
    *,
    is_foreground: bool,
    area: int,
) -> int:
    normalized_path = str(process_path or "").replace("/", "\\").casefold()
    executable = Path(process_path).name.casefold() if process_path else ""
    title_text = str(title or "").strip().casefold()
    package_match = "\\windowsapps\\openai.codex_" in normalized_path
    configured_match = False
    if configured_path and process_path:
        try:
            configured_parent = str(Path(configured_path).parent).casefold()
            process_parent = str(Path(process_path).parent).casefold()
            configured_match = configured_parent == process_parent
        except (OSError, ValueError):
            configured_match = False

    if not (
        configured_match
        or package_match
        or executable == "codex.exe"
        or title_text == "codex"
    ):
        return -1
    score = 0
    if configured_match:
        score += 1000
    if package_match:
        score += 700
    if executable in {"codex.exe", "chatgpt.exe"}:
        score += 200
    if title_text in {"codex", "chatgpt"}:
        score += 100
    if is_foreground:
        score += 300
    score += min(max(0, int(area)) // 10000, 100)
    return score


def focus_codex_window(hwnd: int) -> bool:
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    user32.ShowWindow(hwnd, SW_RESTORE)
    if int(user32.GetForegroundWindow() or 0) == int(hwnd):
        return True
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    if int(user32.GetForegroundWindow() or 0) == int(hwnd):
        return True

    # Windows may block foreground activation from a background polling thread.
    # A short, balanced Alt key event grants the process a foreground transition.
    key_down(VK_MENU)
    key_up(VK_MENU)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    if int(user32.GetForegroundWindow() or 0) == int(hwnd):
        return True

    # The balanced Alt event is not sufficient when the Bridge worker and the
    # foreground Electron window own different input queues. Temporarily join
    # the current, foreground, and target queues, focus the verified Codex
    # window, then detach every queue before returning.
    current_thread = int(kernel32.GetCurrentThreadId() or 0)
    foreground = int(user32.GetForegroundWindow() or 0)
    foreground_thread = int(
        user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    )
    target_thread = int(user32.GetWindowThreadProcessId(hwnd, None) or 0)
    attached_threads: list[int] = []
    for thread_id in (foreground_thread, target_thread):
        if (
            thread_id
            and thread_id != current_thread
            and thread_id not in attached_threads
            and user32.AttachThreadInput(current_thread, thread_id, True)
        ):
            attached_threads.append(thread_id)
    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)
    finally:
        for thread_id in reversed(attached_threads):
            user32.AttachThreadInput(current_thread, thread_id, False)


def get_window_process_path(hwnd: int) -> str:
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not process_id.value:
        return ""
    process = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value
    )
    if not process:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            process, 0, buffer, ctypes.byref(size)
        ):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(process)


def get_window_area(hwnd: int) -> int:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0
    return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)


def get_window_title(hwnd: int) -> str:
    if not hwnd or user32 is None:
        return ""
    result = ctypes.c_size_t()
    flags = SMTO_BLOCK | SMTO_ABORTIFHUNG
    if not user32.SendMessageTimeoutW(
        hwnd,
        WM_GETTEXTLENGTH,
        0,
        0,
        flags,
        WINDOW_TITLE_TIMEOUT_MS,
        ctypes.byref(result),
    ):
        return ""
    length = int(result.value)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    if not user32.SendMessageTimeoutW(
        hwnd,
        WM_GETTEXT,
        length + 1,
        ctypes.addressof(buffer),
        flags,
        WINDOW_TITLE_TIMEOUT_MS,
        ctypes.byref(result),
    ):
        return ""
    return buffer.value


user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform.startswith("win") else None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform.startswith("win") else None

if user32 is not None and kernel32 is not None:
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.BOOL,
    ]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.restype = wintypes.HWND
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
