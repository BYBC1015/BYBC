from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "codex_hub" / "templates" / "project_detail.html"
CHAT_ENTRY = ROOT / "codex_hub" / "templates" / "partials" / "project_chat_entry.html"
SCRIPT = ROOT / "codex_hub" / "static" / "reading-experience.js"
STYLE = ROOT / "codex_hub" / "static" / "reading-experience.css"


def test_project_reader_uses_fixed_content_order_and_compact_actions():
    template = TEMPLATE.read_text(encoding="utf-8")
    chat_entry = CHAT_ENTRY.read_text(encoding="utf-8")

    body = chat_entry.index("data-markdown-source")
    images = chat_entry.index("message-image-grid")
    videos = chat_entry.index("message-video-list")
    files = chat_entry.index("message-file-list")

    assert body < images < videos < files
    assert 'class="message-toolbar"' not in chat_entry
    assert "data-entry-link" not in chat_entry
    assert "data-copy-entry-link" not in chat_entry
    assert "data-focus-reading-toggle" in template
    assert 'class="workbench-title-line"' in template
    title_line = template.index('class="workbench-title-line"')
    title_line_end = template.index("</div>", title_line)
    focus_toggle = template.index("data-focus-reading-toggle")
    assert title_line < focus_toggle < title_line_end
    assert "data-resume-reading" not in template
    assert "继续上次阅读" not in template
    assert "Office / PDF 使用下载后外部工具打开" in template


def test_reading_state_and_media_filter_contracts_are_present():
    script = SCRIPT.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")

    for contract in (
        "lastSeenEntryId",
        "videoProgress",
        "unread-divider",
        "focus-reading-mode",
        "data-media-category",
        "data-media-device",
        "data-media-batch",
        "content-visibility: visible",
    ):
        assert contract in script or contract in style

    assert "content-visibility: auto" not in style
    assert "contain-intrinsic-size: none" in style
    assert "overscroll-behavior-y: contain" in style
    assert "touch-action: pan-y" in style
    assert "--reader-text-width: 74ch" in style
    assert "message-image-grid" in style
    assert "media-image-frame" in style
    assert 'existing?.nextElementSibling === firstUnread' in script
    assert "setFocusMode(false)" in script
    assert "setFocusMode(Boolean(state.focusMode))" not in script


def test_scrolling_does_not_move_the_unread_divider_or_allow_live_refresh():
    template = TEMPLATE.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")

    save_start = script.index("function saveReadingPosition()")
    save_end = script.index("function setInitialReadingPosition()", save_start)
    assert "addUnreadDivider()" not in script[save_start:save_end]

    assert "let chatInteractionActive = false" in template
    assert "chatInteractionActive = true" in template
    assert "chatInteractionActive = false" in template
    assert "chatInteractionActive || Date.now() < chatInteractionUntil" in template
    assert 'addEventListener("scrollend"' in template
    assert 'noteChatInteraction(1200)' in template


def test_live_refresh_skips_unchanged_sections_and_preserves_chat_anchor():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "sectionFingerprint(name, element)" in template
    assert "captureChatViewport(element)" in template
    assert "restoreChatViewport(element, viewport)" in template
    assert (
        'sectionFingerprint(name, element) === sectionFingerprint(name, replacement)'
        in template
    )
    assert "anchor.getBoundingClientRect().top - streamRect.top - viewport.anchorOffset" in template


def test_message_send_avoids_full_page_navigation_and_pins_the_chat_bottom():
    template = TEMPLATE.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")

    assert '"X-Requested-With": "codex-hub-composer"' in template
    assert "event.preventDefault()" in template
    assert "pinChatToBottom: true" in template
    assert "window.location.pathname}${window.location.search}" in template
    assert "window.location.href" not in template[template.index("function refreshProject") :]
    assert "scrollEntryInsideStream" in script
    assert "pinToBottom" in script
    assert 'hashTarget.scrollIntoView({ block: "start" })' not in script
    assert "copyEntryLink" not in script
    assert "activateEntryLink" not in script


def test_live_chat_uses_small_incremental_updates_and_bounded_recovery():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "data-chat-updates-url" in template
    assert '"X-Requested-With": "codex-hub-chat-updates"' in template
    assert "function appendChatUpdates(items)" in template
    assert "function fetchChatUpdates()" in template
    assert "controller?.abort(), 8000" in template
    assert "isAndroidApp ? 1200 : 2500" in template
    assert "Math.min(30000" in template
    assert "window.setInterval(() => refreshProject(), 60000)" in template
    assert "window.setInterval(heartbeat, 30000)" in template
