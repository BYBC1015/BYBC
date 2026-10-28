from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "codex_hub" / "static"
BASE = ROOT / "codex_hub" / "templates" / "base.html"
LOGIN = ROOT / "codex_hub" / "templates" / "login.html"
PROJECT = ROOT / "codex_hub" / "templates" / "project_detail.html"
CHAT_ENTRY = ROOT / "codex_hub" / "templates" / "partials" / "project_chat_entry.html"


def test_page_features_are_scoped_and_cache_versioned():
    base = BASE.read_text(encoding="utf-8")
    login = LOGIN.read_text(encoding="utf-8")
    project = PROJECT.read_text(encoding="utf-8")

    for feature in ("pixel-auth.js", "hub-voice.js", "image-viewer.js"):
        assert feature not in base
    assert "pixel-auth.js" in login
    assert "hub-voice.js" not in login
    assert "image-viewer.js" not in login
    assert "deferred-features.js" in project
    assert "data-image-viewer-src" in project
    assert "data-voice-session-src" in project
    assert "data-markdown-src" in project
    assert "data-video-reader-src" in project
    for feature, references in (("codex-markdown.js", 2), ("codex-video-reader.js", 2),
                                ("hub-voice.js", 1), ("image-viewer.js", 1),
                                ("deferred-features.js", 1)):
        versions = re.findall(re.escape(feature) + r"'\)\s*}}\?v=([^\"\s]+)", project)
        assert len(versions) == references
        assert len(set(versions)) == 1  # Eager and deferred loaders share one revision.
    assert "{% if entries %}<script" in project
    assert "media_library|selectattr('is_inline_video')|list" in project


def test_images_reserve_intrinsic_space_before_loading():
    base = BASE.read_text(encoding="utf-8")
    login = LOGIN.read_text(encoding="utf-8")
    project = PROJECT.read_text(encoding="utf-8")
    chat_entry = CHAT_ENTRY.read_text(encoding="utf-8")

    assert 'width="192" height="191"' in base
    assert login.count('width="269" height="507"') == 2
    assert 'width="{{ file.width }}" height="{{ file.height }}"' in project
    assert 'width="{{ file.width }}" height="{{ file.height }}"' in chat_entry


def test_first_interaction_replays_after_deferred_script_load():
    playwright = pytest.importorskip("playwright.sync_api")
    feature_loader = (STATIC / "deferred-features.js").read_text(encoding="utf-8")
    requests: list[str] = []

    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        page = browser.new_page()

        def serve(route):
            name = route.request.url.split("/")[-1].split("?")[0]
            requests.append(name)
            if name == "deferred-features.js":
                body = feature_loader
            elif name == "image-viewer.js":
                body = """
                window.CodexHubMediaViewer = {
                  open(control) {
                    window.viewerOpened = (window.viewerOpened || 0) + 1;
                    window.viewerControl = control;
                  },
                };
                """
            elif name == "hub-voice.js":
                body = """
                window.CodexHubVoice = {};
                document.querySelector('[data-voice-session]').addEventListener('click', () => {
                  window.voiceStarted = (window.voiceStarted || 0) + 1;
                });
                """
            elif name == "codex-markdown.js":
                body = """
                window.CodexMarkdown = {};
                document.querySelectorAll('[data-markdown-source]').forEach((node) => {
                  node.dataset.enhanced = 'true';
                });
                """
            else:
                body = "window.CodexVideoReader = {};"
            route.fulfill(status=200, content_type="application/javascript", body=body)

        page.route("https://hub.test/static/*.js*", serve)
        page.set_content(
            """
            <a href="/image.png" data-media-image><img alt="preview"></a>
            <button type="button" data-voice-session>voice</button>
            <p data-message-status></p>
            <div id="stream"></div>
            <script src="https://hub.test/static/deferred-features.js?v=1"
              data-image-viewer-src="https://hub.test/static/image-viewer.js?v=1"
              data-voice-session-src="https://hub.test/static/hub-voice.js?v=1"
              data-markdown-src="https://hub.test/static/codex-markdown.js?v=1"
              data-video-reader-src="https://hub.test/static/codex-video-reader.js?v=1"
              defer></script>
            """,
            wait_until="load",
        )

        assert requests == ["deferred-features.js"]
        page.evaluate(
            """() => {
              window.HubMedia = {
                openImage() {
                  window.nativeViewerOpened = (window.nativeViewerOpened || 0) + 1;
                },
              };
              document.addEventListener('click', (event) => {
                const image = event.target.closest?.('[data-media-image]');
                if (!image) return;
                event.preventDefault();
                event.stopImmediatePropagation();
                window.HubMedia.openImage();
              }, true);
            }"""
        )
        page.evaluate(
            "() => { const image = document.querySelector('[data-media-image]'); "
            "image.click(); image.click(); }"
        )
        page.wait_for_function("window.viewerOpened === 1")
        assert page.evaluate("window.viewerControl.matches('[data-media-image]')")
        assert page.evaluate("window.nativeViewerOpened || 0") == 0
        page.evaluate("document.querySelector('[data-media-image]').click()")
        page.wait_for_function("window.viewerOpened === 2")
        assert page.evaluate("window.nativeViewerOpened || 0") == 0
        page.evaluate("document.querySelector('[data-voice-session]').click()")
        page.wait_for_function("window.voiceStarted === 1")
        assert requests.count("image-viewer.js") == 1
        assert requests.count("hub-voice.js") == 1

        page.evaluate(
            "document.querySelector('#stream').innerHTML = "
            "'<div data-markdown-source>hello</div>'"
        )
        page.wait_for_function(
            "document.querySelector('[data-markdown-source]').dataset.enhanced === 'true'"
        )
        assert requests.count("codex-markdown.js") == 1
        assert "codex-video-reader.js" not in requests

        page.evaluate(
            "document.querySelector('#stream').insertAdjacentHTML("
            "'beforeend', '<a data-video-attachment>video</a>')"
        )
        page.wait_for_function("Boolean(window.CodexVideoReader)")
        assert requests.count("codex-video-reader.js") == 1
        browser.close()
