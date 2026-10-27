from __future__ import annotations

from pathlib import Path

import pytest

from test_mobile_composer_browser import (
    ANDROID_CSS,
    ANDROID_JS,
    connect_mobile_test_device,
    live_hub,
    playwright,
)


# v0.1.23 injected this selector on every page from onPageFinished. Keep it
# independent of the fixed asset so installed APKs remain covered by this test.
LEGACY_ROOT_LOCK = """
html.codex-hub-android,
html.codex-hub-android body.hub-detail-screen {
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  overscroll-behavior: none;
  background-attachment: scroll !important;
}
"""


def inject_native_assets(page, native_style: str) -> None:
    """Match WebView's late CSS/class injection, including non-project pages."""
    page.wait_for_load_state("load")
    page.add_style_tag(path=str(ANDROID_CSS))
    if native_style == "installed-apk":
        page.add_style_tag(content=LEGACY_ROOT_LOCK)
    page.evaluate("document.documentElement.classList.add('codex-hub-android')")
    page.add_script_tag(path=str(ANDROID_JS))


def scroll_document(page, distance: int) -> None:
    viewport = page.viewport_size
    assert viewport is not None
    page.mouse.move(viewport["width"] // 2, int(viewport["height"] * 0.75))
    page.mouse.wheel(0, distance)
    # Wheel input completes asynchronously in Chromium. It must go through the
    # browser's scrolling machinery so overflow:hidden actually blocks it.
    page.wait_for_timeout(120)


def assert_no_horizontal_overflow(page) -> None:
    geometry = page.evaluate("""() => ({
      viewport: innerWidth,
      document: document.documentElement.scrollWidth,
      body: document.body.scrollWidth
    })""")
    assert geometry["document"] <= geometry["viewport"], geometry
    assert geometry["body"] <= geometry["viewport"], geometry


@pytest.mark.parametrize("native_style", ["installed-apk", "current-apk"])
@pytest.mark.parametrize("viewport", [
    pytest.param({"width": 320, "height": 568}, id="narrow-portrait"),
    pytest.param({"width": 740, "height": 360}, id="short-landscape"),
])
def test_android_more_downloads_scroll_and_return_to_fixed_chat(
    live_hub: str, tmp_path: Path, native_style: str, viewport: dict[str, int]
):
    # The shared fixture uses this isolated data directory. Populate real release
    # cards without downloading, installing, or touching any production release.
    releases = tmp_path / "state" / "releases"
    for filename in (
        "CodexBridge-v1.0.0.exe",
        "CodexHub-v0.1.23.apk",
        "BYBC_视频末帧延长工具_Setup_v1.0.0.exe",
    ):
        (releases / filename).write_bytes(b"browser regression fixture")

    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport=viewport, has_touch=True, is_mobile=True)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")
            page.fill('input[name="slug"]', "android-document-scroll")
            page.fill('input[name="name"]', "Android Document Scroll")
            page.click('form[action$="/projects"] button[type="submit"]')
            page.wait_for_url("**/projects/android-document-scroll")
            connect_mobile_test_device(page)
            inject_native_assets(page, native_style)

            page.locator('.android-bottom-nav [data-android-view="more"]').click()
            page.locator('.android-tools-nav button[data-android-url$="/downloads"]').click()
            page.wait_for_url("**/downloads")
            inject_native_assets(page, native_style)
            assert page.locator(".download-card").count() == 4
            assert_no_horizontal_overflow(page)

            metrics = page.evaluate("""() => ({
              top: scrollY,
              height: document.scrollingElement.scrollHeight,
              viewport: innerHeight,
              overflow: getComputedStyle(document.documentElement).overflowY
            })""")
            assert metrics["height"] > metrics["viewport"], metrics
            scroll_document(page, min(500, viewport["height"]))
            try:
                page.wait_for_function("scrollY > 10", timeout=3000)
            except playwright.TimeoutError:
                pytest.fail(f"Browser input could not scroll the downloads page: {metrics}")

            # Only browser input advances scrolling; scrollIntoView and assigning
            # scrollTop would falsely pass with overflow:hidden on the root.
            for _ in range(12):
                if page.evaluate(
                    "scrollY + innerHeight >= document.scrollingElement.scrollHeight - 2"
                ):
                    break
                scroll_document(page, 700)
            assert page.evaluate(
                "scrollY + innerHeight >= document.scrollingElement.scrollHeight - 2"
            )
            footer = page.locator(".download-note").bounding_box()
            assert footer is not None and 0 <= footer["y"]
            assert footer["y"] + footer["height"] <= viewport["height"] + 1
            assert_no_horizontal_overflow(page)

            # Exercise scrolling back up, then the platform's history-back path.
            bottom = page.evaluate("scrollY")
            scroll_document(page, -250)
            assert page.evaluate("scrollY") < bottom
            page.go_back()
            page.wait_for_url("**/projects/android-document-scroll?view=archive")
            inject_native_assets(page, native_style)
            page.locator('.android-bottom-nav [data-android-view="chat"]').click()
            chat_geometry = page.evaluate("""() => {
              const composer = document.querySelector('.workbench-composer').getBoundingClientRect();
              const navigation = document.querySelector('.android-bottom-nav').getBoundingClientRect();
              const chat = document.querySelector('[data-auto-refresh="chat"]');
              return {
                documentHeight: document.scrollingElement.scrollHeight,
                viewport: innerHeight,
                outerTop: scrollY,
                composerTop: composer.top,
                composerBottom: composer.bottom,
                navigationBottom: navigation.bottom,
                chatOverflow: getComputedStyle(chat).overflowY,
                chatMode: document.body.classList.contains('android-chat-mode')
              };
            }""")
            assert chat_geometry["chatMode"]
            assert chat_geometry["outerTop"] == 0, chat_geometry
            assert chat_geometry["documentHeight"] <= chat_geometry["viewport"] + 1, chat_geometry
            assert chat_geometry["composerTop"] >= 0, chat_geometry
            assert chat_geometry["composerBottom"] <= chat_geometry["viewport"] + 1, chat_geometry
            assert chat_geometry["navigationBottom"] <= chat_geometry["viewport"] + 1, chat_geometry
            assert chat_geometry["chatOverflow"] in {"auto", "scroll"}, chat_geometry
            assert_no_horizontal_overflow(page)
            assert errors == []
        finally:
            context.close()
            browser.close()
