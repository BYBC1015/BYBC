from __future__ import annotations

from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")


STATIC_DIR = Path(__file__).parents[1] / "codex_hub" / "static"


@pytest.fixture()
def page():
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context()
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


def test_markdown_renders_with_dom_nodes_and_blocks_xss(page):
    page.set_content(
        """
        <base href="https://hub.test/projects/demo">
        <pre data-markdown-source># Heading

- first
- second

&gt; quoted

| A | B |
| --- | ---: |
| one | two |

[safe](https://example.com) [bad](javascript:alert(1))
&lt;img src=x onerror=alert(1)&gt;

```js
const value = &quot;&lt;script&gt;bad()&lt;/script&gt;&quot;;
```</pre>
        """
    )
    page.add_script_tag(path=str(STATIC_DIR / "codex-markdown.js"))

    assert page.locator(".codex-markdown h1").inner_text() == "Heading"
    assert page.locator(".codex-markdown li").count() == 2
    assert page.locator(".codex-markdown blockquote").inner_text() == "quoted"
    assert page.locator(".codex-markdown table").count() == 1
    assert page.locator('.codex-markdown a[href="https://example.com"]').get_attribute("rel") == "noopener noreferrer"
    assert page.locator('.codex-markdown a[href^="javascript:"]').count() == 0
    assert page.locator(".codex-markdown img, .codex-markdown script").count() == 0
    assert "<img src=x" in page.locator(".codex-markdown").inner_text()
    assert "<script>bad()</script>" in page.locator(".codex-code-block code").inner_text()


def test_code_copy_uses_the_unmodified_code_text(page):
    page.set_content("<pre data-markdown-source>```txt\n&lt;b&gt;literal&lt;/b&gt;\n```</pre>")
    page.evaluate(
        """
        window.copiedCode = null;
        Object.defineProperty(navigator, 'clipboard', {
          configurable: true,
          value: { writeText: async (value) => { window.copiedCode = value; } }
        });
        """
    )
    page.add_script_tag(path=str(STATIC_DIR / "codex-markdown.js"))

    page.locator(".codex-code-copy").click()
    page.wait_for_function("window.copiedCode !== null")
    assert page.evaluate("window.copiedCode") == "<b>literal</b>"


def test_video_is_not_loaded_until_click_and_never_autoplays(page):
    requests: list[str] = []
    page.route(
        "**/attachments/**",
        lambda route: (requests.append(route.request.url), route.abort()),
    )
    page.set_content(
        """
        <base href="https://hub.test/projects/demo">
        <div class="message-attachments">
          <a class="message-file" href="/attachments/7" data-video-id="demo:7">
            <span></span><span><strong>walkthrough.mp4</strong><small>3 MB</small></span>
          </a>
        </div>
        """
    )
    page.add_script_tag(path=str(STATIC_DIR / "codex-video-reader.js"))

    assert page.locator(".codex-video-card").count() == 1
    assert page.locator("video, source").count() == 0
    assert requests == []

    page.locator(".codex-video-load").click()
    video = page.locator(".codex-video-card video")
    assert video.count() == 1
    assert video.evaluate("node => node.autoplay") is False
    assert video.evaluate("node => node.controls") is True
    assert video.evaluate("node => node.playsInline") is True
    assert "inline=1" in video.get_attribute("src")


def test_dynamic_remount_preserves_an_already_loaded_player(page):
    page.set_content(
        """
        <base href="https://hub.test/projects/demo">
        <div id="chat"><div class="message-attachments">
          <a class="message-file" href="/attachments/7" data-video-id="demo:7">
            <span></span><span><strong>walkthrough.mp4</strong><small>3 MB</small></span>
          </a>
        </div></div>
        """
    )
    page.add_script_tag(path=str(STATIC_DIR / "codex-video-reader.js"))
    page.locator(".codex-video-load").click()
    page.locator("video").evaluate("node => { node.dataset.instance = 'preserved'; }")

    page.locator("#chat").evaluate(
        """node => {
          node.innerHTML = `<div class="message-attachments">
            <a class="message-file" href="/attachments/7" data-video-id="demo:7">
              <span></span><span><strong>walkthrough.mp4</strong><small>3 MB</small></span>
            </a>
          </div>`;
        }"""
    )
    page.wait_for_selector('video[data-instance="preserved"]')
    assert page.locator('video[data-instance="preserved"]').count() == 1


def test_unread_divider_is_idempotent_and_legacy_focus_state_is_ignored(page):
    page.set_content(
        """
        <body class="hub-detail-screen">
          <section class="workbench-shell">
            <header class="workbench-topbar">
              <div class="workbench-title">
                <div class="workbench-title-line">
                  <h1>Reader</h1>
                  <button type="button" data-focus-reading-toggle aria-pressed="false">
                    <span aria-hidden="true">▣</span><span class="reader-focus-label">专注</span>
                  </button>
                </div>
              </div>
            </header>
            <div class="workbench-chat" data-auto-refresh="chat" data-project-slug="reader">
              <article id="entry-1" class="chat-message" data-entry-id="1"></article>
              <article id="entry-2" class="chat-message" data-entry-id="2"></article>
            </div>
          </section>
        </body>
        """
    )
    page.evaluate(
        """
        Object.defineProperty(window, "localStorage", {
          configurable: true,
          value: {
            getItem: () => JSON.stringify({ lastSeenEntryId: 1, focusMode: true }),
            setItem: () => undefined,
          },
        });
        window.readerMutationCount = 0;
        new MutationObserver((records) => {
          window.readerMutationCount += records.length;
        }).observe(document.querySelector("[data-auto-refresh='chat']"), {
          childList: true,
          subtree: true,
        });
        """
    )

    page.add_script_tag(path=str(STATIC_DIR / "reading-experience.js"))
    page.wait_for_timeout(150)

    assert page.locator(".workbench-chat > .unread-divider").count() == 1
    assert page.evaluate("window.readerMutationCount") <= 2
    assert page.locator("body").evaluate(
        "node => node.classList.contains('focus-reading-mode')"
    ) is False


def test_voice_session_uses_memory_fallback_when_local_storage_is_blocked(page):
    page.set_content(
        """
        <textarea data-mention-input></textarea>
        <button type="button" data-voice-session></button>
        <form id="project-message-form"></form>
        <section data-auto-refresh="chat">
          <article class="chat-message" data-entry-id="7" data-entry-author-type="device">
            <div class="message-card"><pre>reply</pre></div>
          </article>
        </section>
        """
    )
    page.evaluate(
        """
        Object.defineProperty(window, "localStorage", {
          configurable: true,
          value: {
            getItem: () => { throw new DOMException("blocked", "SecurityError"); },
            setItem: () => { throw new DOMException("blocked", "SecurityError"); },
          },
        });
        """
    )

    page.add_script_tag(path=str(STATIC_DIR / "hub-voice.js"))

    assert page.evaluate("Boolean(window.CodexHubVoice)") is True
    assert page.locator("[data-voice-session]").get_attribute("data-voice-state") == "idle"


def test_hash_entry_position_clears_outer_fragment_scroll(page):
    page.set_content(
        """
        <style>
          body { min-height: 1600px; margin: 0; padding-top: 320px; }
          .workbench-chat { width: 360px; height: 220px; overflow-y: auto; }
          .chat-message { height: 150px; }
        </style>
        <section class="workbench-shell">
          <div class="workbench-chat" data-auto-refresh="chat" data-project-slug="hash">
            <article id="entry-1" class="chat-message" data-entry-id="1" tabindex="-1"></article>
            <article id="entry-2" class="chat-message" data-entry-id="2" tabindex="-1"></article>
            <article id="entry-3" class="chat-message" data-entry-id="3" tabindex="-1"></article>
            <article id="entry-4" class="chat-message" data-entry-id="4" tabindex="-1"></article>
          </div>
        </section>
        """
    )
    page.evaluate(
        """
        history.replaceState(null, '', '#entry-4');
        window.scrollTo(0, 260);
        """
    )

    page.add_script_tag(path=str(STATIC_DIR / "reading-experience.js"))
    page.wait_for_timeout(100)

    assert page.evaluate("window.scrollY") == 0
    assert page.locator(".workbench-chat").evaluate(
        "node => node.scrollTop === node.scrollHeight - node.clientHeight"
    ) is True
