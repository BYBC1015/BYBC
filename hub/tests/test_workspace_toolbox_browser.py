"""Toolbox navigation on isolated Hub; native launches are mocked, never shown."""
import json
import re
from pathlib import Path

import pytest
from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from test_workspace_product_ui_browser import VISUAL_VIEWPORT
from workspace_fixture import seed_workspace

playwright = pytest.importorskip("playwright.sync_api")
OUTPUT = Path(__file__).resolve().parents[2] / "output/hub-network-consistency-20261031/ui"


def navigate_toolbox(page, width):
    if width < 900:
        page.locator("[data-ws-sidebar-open]").click()
    entry = page.locator("[data-ws-toolbox]")
    playwright.expect(entry).to_be_visible()
    entry.scroll_into_view_if_needed()
    return entry


@pytest.mark.parametrize("width", [1440, 390, 360])
def test_web_toolbox_is_discoverable_draft_safe_and_mobile_dialog_fits(live_hub, repo, width):
    data = seed_workspace(repo, room_messages=8)
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 844})
        page.add_init_script(VISUAL_VIEWPORT)
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        try:
            login_page(page, live_hub)
            url = page.url
            before = len(repo.list_tasks(data["project"]["id"]))
            page.locator("[data-ws-input]").fill("保留工作草稿与附件，查看工具箱。")
            page.locator("[data-ws-attachment-input]").set_input_files(
                {"name": "草稿资料.txt", "mimeType": "text/plain", "buffer": b"preserve attachment"})
            entry = navigate_toolbox(page, width)
            assert entry.get_attribute("href").endswith("/downloads")
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(animations="disabled", path=str(OUTPUT / f"toolbox-entry-{width}.png"))
            entry.click()
            dialog = page.get_by_role("dialog", name="工具箱", exact=True)
            playwright.expect(dialog).to_be_visible()
            assert "Windows 桌面版" in dialog.inner_text()
            assert dialog.locator("li").count() == 4
            playwright.expect(page.locator("[data-ws-toolbox-close]")).to_be_focused()
            bounds = dialog.bounding_box()
            assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
            assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 844
            close = page.locator("[data-ws-toolbox-close]").bounding_box()
            assert close["width"] >= 44 and close["height"] >= 44
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(animations="disabled", path=str(OUTPUT / f"toolbox-web-{width}.png"))
            with page.expect_popup() as popup_info:
                dialog.get_by_role("link", name="获取 Windows 桌面版").click()
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded")
            assert popup.url.endswith("/downloads")
            assert popup.get_by_role("heading", name="下载 Codex Hub 套件").count() or "Windows" in popup.locator("body").inner_text()
            popup.close()
            if width < 900:
                # Deliberately simulated keyboard geometry, not physical iOS evidence.
                page.evaluate("__setViewport({height:390,offsetTop:24})")
                page.wait_for_function("parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ws-viewport-height')) === 390")
                bounds = dialog.bounding_box()
                assert bounds["y"] >= 24 and bounds["y"] + bounds["height"] <= 414
                dialog.locator("a").scroll_into_view_if_needed()
                assert dialog.locator("a").bounding_box()["y"] < 414
                visible_close = dialog.locator("[data-ws-toolbox-close]").bounding_box()
                assert visible_close["y"] >= 24 and visible_close["y"] + visible_close["height"] <= 414
                page.screenshot(animations="disabled", path=str(OUTPUT / f"toolbox-keyboard-{width}.png"))
                page.evaluate("__setViewport({height:844,offsetTop:0})")
            page.keyboard.press("Escape")
            playwright.expect(dialog).not_to_be_visible()
            playwright.expect(page.locator("[data-ws-sidebar-open]") if width < 900 else entry).to_be_focused()
            assert page.url == url
            assert page.locator("[data-ws-input]").input_value() == "保留工作草稿与附件，查看工具箱。"
            playwright.expect(page.locator("[data-ws-attachment]")).to_have_count(1)
            assert len(repo.list_tasks(data["project"]["id"])) == before
            assert not errors
            (OUTPUT / f"toolbox-web-{width}.json").write_text(json.dumps({
                "width": width, "dialog": bounds, "close": close, "draft_preserved": True,
                "attachments_preserved": True, "task_count_unchanged": True,
                "mobile_viewport_simulated": width < 900, "page_errors": errors,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            browser.close()


def test_native_toolbox_bridge_late_ready_single_launch_error_and_retry(live_hub, repo):
    seed_workspace(repo)
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        try:
            login_page(page, live_hub)
            page.locator("[data-ws-input]").fill("当前原生工作草稿")
            # pywebview API appears after the deferred page scripts have run.
            page.evaluate("""() => {
              window.__toolboxCalls=0;
              window.pywebview={api:{open_toolbox:()=>{
                ++window.__toolboxCalls;
                return new Promise((resolve,reject)=>{window.__toolboxResolve=resolve;window.__toolboxReject=reject;});
              }}};
              dispatchEvent(new Event('pywebviewready'));
            }""")
            entry = page.locator("[data-ws-toolbox]")
            entry.click()
            entry.dispatch_event("click", {"button": 0})
            assert page.evaluate("__toolboxCalls") == 1
            playwright.expect(entry).to_have_attribute("aria-disabled", "true")
            page.evaluate("__toolboxResolve({opened:true})")
            playwright.expect(page.locator("[data-ws-toolbox-status]")).to_have_text("工具箱已启动")
            playwright.expect(page.locator("[data-ws-toolbox-dialog]")).not_to_be_visible()
            entry.click()
            page.evaluate("__toolboxReject(new Error('private path must not leak'))")
            dialog = page.locator("[data-ws-toolbox-dialog]")
            playwright.expect(dialog).to_be_visible()
            assert "private path" not in dialog.inner_text()
            dialog.locator("[data-ws-toolbox-retry]").click()
            assert page.evaluate("__toolboxCalls") == 3
            page.evaluate("__toolboxResolve({opened:true})")
            playwright.expect(dialog).not_to_be_visible()
            assert page.locator("[data-ws-input]").input_value() == "当前原生工作草稿"
        finally:
            browser.close()


def test_old_snapshot_body_gets_toolbox_once_without_replacing_draft(live_hub, repo):
    seed_workspace(repo)
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        try:
            def legacy_body(route):
                response = route.fetch()
                body = response.text()
                body = re.sub(r'<a class="ws-toolbox-entry".*?</a>', '', body, flags=re.S)
                body = re.sub(r'<p class="ws-toolbox-status".*?</p>', '', body, flags=re.S)
                body = re.sub(r'<dialog id="ws-toolbox-panel".*?</dialog>', '', body, flags=re.S)
                route.fulfill(response=response, body=body)
            page.route("**/projects/workspace-team", legacy_body)
            login_page(page, live_hub)
            page.locator("[data-ws-input]").fill("旧缓存中的工作草稿")
            page.locator("[data-ws-toolbox]").click()
            dialog = page.locator("[data-ws-toolbox-dialog]")
            playwright.expect(dialog).to_be_visible()
            assert dialog.locator("li").count() == 4
            assert dialog.locator("a").get_attribute("href") == "/downloads"
            page.keyboard.press("Escape")
            # Re-evaluating current static assets must not attach duplicate handlers.
            source = (Path(__file__).resolve().parents[1] / "codex_hub/static/toolbox.js").read_text(encoding="utf-8")
            page.evaluate(source)
            assert page.locator("[data-ws-toolbox]").count() == 1
            assert page.locator("[data-ws-toolbox-dialog]").count() == 1
            assert page.locator("[data-ws-input]").input_value() == "旧缓存中的工作草稿"
        finally:
            browser.close()


def test_old_desktop_bridge_explains_update_without_fake_launch(live_hub, repo):
    seed_workspace(repo)
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960}, reduced_motion="reduce")
        page.add_init_script("window.pywebview={api:{}}")
        try:
            login_page(page, live_hub)
            page.locator("[data-ws-toolbox]").click()
            dialog = page.locator("[data-ws-toolbox-dialog]")
            playwright.expect(dialog).to_contain_text("请更新 Windows 桌面版")
            playwright.expect(dialog.locator("[data-ws-toolbox-retry]")).to_be_hidden()
            assert dialog.evaluate("el=>getComputedStyle(el).animationName") == "none"
            assert page.locator("[data-ws-toolbox-status]").inner_text() == ""
        finally:
            browser.close()
