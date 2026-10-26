from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROJECT_TEMPLATE = ROOT / "codex_hub" / "templates" / "project_detail.html"
MAIN = ROOT / "codex_hub" / "main.py"
VIEWER_JS = ROOT / "codex_hub" / "static" / "image-viewer.js"
VIEWER_CSS = ROOT / "codex_hub" / "static" / "image-viewer.css"
DEFERRED_FEATURES = ROOT / "codex_hub" / "static" / "deferred-features.js"
NODE_TEST = ROOT / "tests" / "image_viewer_unit.test.cjs"


def test_image_viewer_is_an_isolated_static_module():
    project_template = PROJECT_TEMPLATE.read_text(encoding="utf-8")
    main = MAIN.read_text(encoding="utf-8")
    script = VIEWER_JS.read_text(encoding="utf-8")

    assert '<script src="{{ app_path(\'/static/image-viewer.js\') }}' not in project_template
    assert "data-image-viewer-src" in project_template
    assert "deferred-features.js" in project_template
    assert "image-viewer.js" not in main
    assert 'new URL("image-viewer.css", sourceUrl)' in script
    assert "stylesheetUrl.search = sourceUrl.search" in script
    assert VIEWER_CSS.is_file()
    assert DEFERRED_FEATURES.is_file()


def test_image_viewer_exposes_the_complete_interaction_contract():
    script = VIEWER_JS.read_text(encoding="utf-8")
    css = VIEWER_CSS.read_text(encoding="utf-8")

    for contract in (
        'event.key === "ArrowLeft"',
        'event.key === "ArrowRight"',
        'event.key === "Escape"',
        'addEventListener("wheel"',
        'addEventListener("dblclick"',
        "pointerDistance",
        'return "close"',
        'data-iv-scope="message"',
        'data-iv-scope="conversation"',
        "preloadNeighbors",
        "navigator.share",
        "HubMedia.share",
        "downloadCurrentScope",
        'preferredScope: "conversation"',
        "CodexHubMediaViewer",
        "restoreScrollState",
    ):
        assert contract in script

    assert ".iv-backdrop" in css
    assert ".iv-filmstrip" in css
    assert "align-items: center" in css
    assert "justify-content: center" in css
    assert "touch-action: none" in css
    assert "@media (max-width: 720px), (pointer: coarse)" in css


def test_multiple_images_in_one_message_are_upgraded_to_a_grid():
    script = VIEWER_JS.read_text(encoding="utf-8")
    css = VIEWER_CSS.read_text(encoding="utf-8")

    assert 'container.classList.toggle("image-viewer-grid", isGrid)' in script
    assert 'observer.observe(document.body, { childList: true, subtree: true })' in script
    assert ".image-viewer-grid" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css


def test_image_viewer_centers_and_swipes_across_messages():
    playwright = pytest.importorskip("playwright.sync_api")
    viewer_script = VIEWER_JS.read_text(encoding="utf-8")
    viewer_styles = VIEWER_CSS.read_text(encoding="utf-8")

    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        page = browser.new_page(viewport={"width": 390, "height": 844})

        def serve_viewer(route):
            if route.request.url.split("?", 1)[0].endswith(".css"):
                route.fulfill(status=200, content_type="text/css", body=viewer_styles)
            else:
                route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body=viewer_script,
                )

        page.route("https://hub.test/static/image-viewer.*", serve_viewer)
        image_one = (
            "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
            "width='120' height='60'%3E%3Crect width='120' height='60' fill='red'/%3E%3C/svg%3E"
        )
        image_two = (
            "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
            "width='80' height='120'%3E%3Crect width='80' height='120' fill='blue'/%3E%3C/svg%3E"
        )
        page.set_content(
            f"""
            <main data-auto-refresh="chat">
              <article class="chat-message" data-entry-id="1">
                <a class="message-image-link" href="https://hub.test/attachments/1"
                  data-media-preview="{image_one}"><img src="{image_one}" alt="one"></a>
              </article>
              <article class="chat-message" data-entry-id="2">
                <a class="message-image-link" href="https://hub.test/attachments/2"
                  data-media-preview="{image_two}"><img src="{image_two}" alt="two"></a>
              </article>
            </main>
            <script src="https://hub.test/static/image-viewer.js?v=test"></script>
            """,
            wait_until="load",
        )

        page.click(".message-image-link")
        page.wait_for_selector(".iv-root.is-open")
        page.wait_for_function(
            "document.querySelector('[data-iv-image-shell]').dataset.loading === 'false'"
        )
        assert page.locator("[data-iv-counter]").text_content() == "1 / 2"
        centers = page.evaluate(
            """() => {
              const image = document.querySelector('[data-iv-image]').getBoundingClientRect();
              const shell = document.querySelector('[data-iv-image-shell]').getBoundingClientRect();
              return {
                imageX: image.left + image.width / 2,
                imageY: image.top + image.height / 2,
                shellX: shell.left + shell.width / 2,
                shellY: shell.top + shell.height / 2,
              };
            }"""
        )
        assert abs(centers["imageX"] - centers["shellX"]) <= 1
        assert abs(centers["imageY"] - centers["shellY"]) <= 1

        page.evaluate("window.CodexHubMediaViewer.close()")
        page.wait_for_selector(".iv-root", state="hidden")
        page.locator(".message-image-link").nth(1).click()
        page.wait_for_selector(".iv-root.is-open")
        page.wait_for_function(
            "document.querySelector('[data-iv-image]').alt === 'two'"
        )
        assert page.locator("[data-iv-counter]").text_content() == "2 / 2"

        page.evaluate("window.CodexHubMediaViewer.close()")
        page.wait_for_selector(".iv-root", state="hidden")
        page.locator(".message-image-link").nth(0).click()
        page.wait_for_selector(".iv-root.is-open")

        canvas = page.locator("[data-iv-canvas]").bounding_box()
        assert canvas is not None
        start_x = canvas["x"] + canvas["width"] * 0.75
        end_x = canvas["x"] + canvas["width"] * 0.25
        y = canvas["y"] + canvas["height"] * 0.5
        page.mouse.move(start_x, y)
        page.mouse.down()
        for step in range(1, 9):
            x = start_x + (end_x - start_x) * step / 12
            page.mouse.move(x, y)
            page.wait_for_timeout(18)
        slide_positions = page.evaluate(
            """() => {
              const current = document.querySelector('[data-iv-slide="current"]')
                .getBoundingClientRect();
              const next = document.querySelector('[data-iv-slide="next"]')
                .getBoundingClientRect();
              return { currentLeft: current.left, nextLeft: next.left, width: current.width };
            }"""
        )
        assert slide_positions["currentLeft"] < -30
        assert abs(
            slide_positions["nextLeft"]
            - slide_positions["currentLeft"]
            - slide_positions["width"]
        ) <= 1
        for step in range(9, 13):
            x = start_x + (end_x - start_x) * step / 12
            page.mouse.move(x, y)
            page.wait_for_timeout(18)
        page.mouse.up()
        page.wait_for_function(
            "document.querySelector('[data-iv-counter]').textContent === '2 / 2'"
        )
        browser.close()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_image_viewer_javascript_unit_suite():
    result = subprocess.run(
        [shutil.which("node"), "--test", str(NODE_TEST)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
