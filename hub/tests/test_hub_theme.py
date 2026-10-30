from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "codex_hub" / "templates" / "project_detail.html"
THEME = ROOT / "codex_hub" / "static" / "hub-theme.css"
BASE = ROOT / "codex_hub" / "templates" / "base.html"
APP_CSS = ROOT / "codex_hub" / "static" / "app.css"


def test_signature_theme_loads_after_reading_styles():
    template = TEMPLATE.read_text(encoding="utf-8")

    reading = template.index("/static/reading-experience.css")
    markdown = template.index("/static/codex-reading.css")
    theme = template.index("/static/hub-theme.css")

    assert reading < markdown < theme
    assert "asset_path('/static/hub-theme.css')" in template
    assert THEME.is_file()


def test_signature_theme_restores_pixel_editorial_visual_tokens():
    css = THEME.read_text(encoding="utf-8")

    for contract in (
        "--hub-signal: #ffd51d",
        "--hub-cyan: #1ec6d4",
        "--hub-magenta: #ef4d8f",
        "--hub-ink: #18202d",
        "linear-gradient(",
        ".workbench-shell::before",
        ".rail-project.is-active",
        ".workbench-composer:focus-within",
        ".project-memory-drawer",
        ':focus-visible',
    ):
        assert contract in css

    assert "image-rendering: pixelated" in css
    assert 'send-button[aria-busy="true"]::before' in css


def test_project_tabs_only_target_real_drawer_sections():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert 'href="#links"' not in template
    assert 'href="#activity"' not in template
    assert 'href="#codex-workspaces"' in template
    assert 'href="#tasks"' in template
    assert '<section id="tasks" class="control-card">' in template


def test_mobile_drawers_are_accessible_mutually_exclusive_and_non_blocking():
    template = TEMPLATE.read_text(encoding="utf-8")
    theme = THEME.read_text(encoding="utf-8")

    assert 'aria-controls="project-sidebar"' in template
    assert 'id="project-sidebar"' in template
    assert 'aria-hidden="true" inert' in template
    assert 'const overlayEvent = "codexhub:overlay-open"' in template
    assert 'drawer.inert = !open' in template
    assert 'sidebar.inert = hidden' in template
    assert 'event.detail?.name !== "memory"' in template
    assert 'event.detail?.name !== "sidebar"' in template
    assert 'class="memory-backdrop"' not in template
    assert ".memory-backdrop" not in theme
    assert 'document.addEventListener("click", (event) =>' in template


def test_fragment_navigation_does_not_use_untrusted_css_selectors():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "document.querySelector(window.location.hash)" not in template
    assert "document.getElementById(decodeURIComponent(fragment))" in template
    assert 'target.querySelector(":scope > .side-disclosure")' in template


def test_project_chat_chrome_uses_consistent_chinese_labels():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "输入任务，由下方选中的成员直接执行…" in template
    assert "指定成员 · 直接执行" in template
    assert "全组协作 · 共同研讨" in template
    assert ">今天<" in template
    assert ">导出 Markdown<" in template
    assert '>发送<' in template
    assert 'stampSync("已同步")' in template


def test_shared_pages_offer_skip_navigation_and_visible_keyboard_focus():
    base = BASE.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")

    assert 'class="skip-link" href="#main-content"' in base
    assert 'id="main-content" class="shell" tabindex="-1"' in base
    assert ".skip-link:focus" in css
    assert ":where(a, button, input, textarea, select, summary):focus-visible" in css


def test_project_composer_exposes_keyboard_and_screen_reader_contracts():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert '<section class="conversation-pane" aria-label="项目对话">' in template
    assert '<main class="conversation-pane">' not in template
    assert 'data-mention-input role="combobox"' in template
    assert 'id="mention-menu"' in template
    assert 'role="listbox"' in template
    assert 'role="option" aria-selected="false"' in template
    assert 'input.setAttribute("aria-activedescendant", active.id)' in template
    assert 'input.setAttribute("aria-expanded", String(shown > 0))' in template
    assert '<button class="composer-action attachment-action" type="button"' in template
    assert 'attachmentButton?.addEventListener("click", () => attachmentInput?.click())' in template


def test_project_refresh_handles_expired_sessions_and_blocked_storage():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "function redirectIfSessionExpired(response)" in template
    assert "response.status !== 401 && !response.redirected" in template
    assert "function readWebDeviceId()" in template
    assert "function writeWebDeviceId(value)" in template
    assert 'window.addEventListener("online", () => scheduleChatUpdates(0))' in template
