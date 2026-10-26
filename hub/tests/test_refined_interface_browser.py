from __future__ import annotations

import re

import pytest

from test_mobile_composer_browser import live_hub  # noqa: F401


playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture()
def browser():
    with playwright.sync_playwright() as runner:
        try:
            instance = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        try:
            yield instance
        finally:
            instance.close()


def test_narrow_phone_more_menu_preserves_tools_and_chat_geometry(browser, live_hub: str):
    context = browser.new_context(viewport={"width": 320, "height": 640})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{live_hub}/login")
        page.fill('input[name="username"]', "owner")
        page.fill('input[name="password"]', "owner-password")
        page.click('button[type="submit"]')
        page.wait_for_url("**/projects")
        page.fill('input[name="slug"]', "refined-phone-tools")
        page.fill('input[name="name"]', "窄屏项目的完整工具菜单")
        page.click('form[action$="/projects"] button[type="submit"]')
        page.wait_for_url("**/projects/refined-phone-tools")

        more = page.locator(".hub-more")
        summary = more.locator("summary")
        menu = page.locator(".hub-more-menu")
        chat = page.locator('[data-auto-refresh="chat"]')
        panel = page.locator("[data-connect-panel]")
        before = chat.bounding_box()
        assert before is not None and before["height"] > 100

        summary.click()
        playwright.expect(menu).to_be_visible()
        for selector in (
            'a[href$="/downloads"]',
            'a[href$="/export.md"]',
            "[data-connect-toggle]",
            "[data-web-presence-device]",
        ):
            control = menu.locator(selector)
            playwright.expect(control).to_be_visible()
            assert control.evaluate(
                "node => { const rect = node.getBoundingClientRect(); "
                "const hit = document.elementFromPoint("
                "rect.left + rect.width / 2, rect.top + rect.height / 2); "
                "return rect.left >= 0 && rect.right <= innerWidth "
                "&& (hit === node || node.contains(hit)); }"
            ), selector

        menu.locator("[data-connect-toggle]").click()
        playwright.expect(panel).to_be_visible()
        playwright.expect(menu).to_be_hidden()
        after = chat.bounding_box()
        assert after is not None
        assert after["y"] == pytest.approx(before["y"], abs=1)
        assert after["height"] == pytest.approx(before["height"], abs=1)
        assert panel.evaluate(
            "node => { const rect = node.getBoundingClientRect(); "
            "return rect.left >= 0 && rect.right <= innerWidth "
            "&& rect.bottom <= innerHeight; }"
        )
        panel.locator("[data-connect-close]").click()
        playwright.expect(panel).to_be_hidden()

        summary.click()
        menu.locator("[data-connect-toggle]").click()
        page.keyboard.press("Escape")
        playwright.expect(panel).to_be_hidden()

        summary.click()
        menu.locator("[data-connect-toggle]").click()
        page.locator(".workbench-title h1").click()
        playwright.expect(panel).to_be_hidden()

        summary.click()
        menu.locator("[data-connect-toggle]").click()
        page.locator(".workbench-topbar [data-memory-open]").click()
        playwright.expect(panel).to_be_hidden()
        playwright.expect(page.locator("[data-memory-drawer]")).to_have_attribute(
            "aria-hidden", "false"
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert errors == []
    finally:
        context.close()


def test_login_keeps_password_character_and_short_screen_error_reachable(browser, live_hub: str):
    context = browser.new_context(viewport={"width": 1200, "height": 720})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{live_hub}/login")
        page.wait_for_function("window.__pixelAuthReady === true")
        card = page.locator(".pixel-login-card")
        card_before_focus = card.bounding_box()
        assert card_before_focus is not None
        password = page.locator('input[name="password"]')
        password.focus()
        playwright.expect(page.locator(".girl-closed")).to_have_css("opacity", "1")
        playwright.expect(page.locator(".girl-open")).to_have_css("opacity", "0")
        image_bounds = page.locator(".girl-open, .girl-closed").evaluate_all(
            "nodes => nodes.map(node => node.getBoundingClientRect().toJSON())"
        )
        assert len(image_bounds) == 2
        assert image_bounds[0]["width"] > 0 and image_bounds[0]["height"] > 0
        for dimension in ("width", "height"):
            assert image_bounds[0][dimension] == pytest.approx(
                image_bounds[1][dimension], abs=1
            )
        assert card.bounding_box() == pytest.approx(card_before_focus, abs=1)
        page.locator('input[name="username"]').focus()
        playwright.expect(page.locator(".girl-closed")).to_have_css("opacity", "0")
        playwright.expect(page.locator(".girl-open")).to_have_css("opacity", "1")

        page.set_viewport_size({"width": 320, "height": 280})
        page.fill('input[name="username"]', "owner")
        password.fill("incorrect-password")
        submit = page.locator('button[type="submit"]')
        submit.scroll_into_view_if_needed()
        assert page.evaluate("window.scrollY > 0")
        assert submit.evaluate(
            "node => { const rect = node.getBoundingClientRect(); "
            "return rect.top >= 0 && rect.bottom <= innerHeight; }"
        )
        submit.click()
        error = page.locator(".pixel-login-card .error")
        playwright.expect(error).to_contain_text(re.compile("账号|用户名|密码|登录"))
        assert "Invalid username or password" not in error.inner_text()
        error.scroll_into_view_if_needed()
        assert error.evaluate(
            "node => { const rect = node.getBoundingClientRect(); "
            "return rect.top >= 0 && rect.bottom <= innerHeight; }"
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert errors == []
    finally:
        context.close()
