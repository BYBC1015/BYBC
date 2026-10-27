from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from codex_hub.config import Settings
from codex_hub.main import create_app
from codex_hub.db import connect_database
from codex_hub.repository import Repository


playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]
ANDROID_CSS = ROOT / "android" / "app" / "src" / "main" / "assets" / "android-app.css"
ANDROID_JS = ROOT / "android" / "app" / "src" / "main" / "assets" / "android-app.js"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture()
def live_hub(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "state",
        secret_key="mobile-browser-secret",
        owner_username="owner",
        owner_password="owner-password",
    )
    settings.ensure_directories()
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base_url}/healthz", timeout=0.2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.02)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail("local Codex Hub did not start")
    connection = connect_database(settings.database_path)
    try:
        repository = Repository(connection)
        device = repository.create_device(label="mobile-test-codex")
        token = repository.create_api_token(device_id=device["id"], name="browser-test")
        response = httpx.post(f"{base_url}/api/device/capabilities", headers={
            "Authorization": f"Bearer {token.raw}"}, json={
                "execution_backend": "codex", "codex": {"source": "device", "models": [{
                    "model": "gpt-6-astra", "displayName": "GPT-6 Astra", "isDefault": True,
                    "supportedReasoningEfforts": [{"reasoningEffort": effort} for effort in ["low", "high", "max", "ultra"]],
                }]}})
        assert response.status_code == 200
    finally:
        connection.close()
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def connect_mobile_test_device(page):
    """Exercise the retained device-chat archive, now separate from the agent workspace."""
    base=page.url.split('?')[0].split('#')[0]
    if page.locator('[data-ws-shell]').count():
        page.goto(base+'?view=archive')
    csrf = page.locator('#project-message-form input[name="_csrf"]').input_value()
    device_id = page.locator('form[action$="/members"] option').filter(has_text="mobile-test-codex").get_attribute("value")
    assert device_id
    assert page.request.post(f"{base}/members", form={
        "_csrf": csrf, "device_id": device_id, "permission": "write",
    }).ok
    assert page.request.post(f"{base}/primary-device", form={
        "_csrf": csrf, "primary_device_id": device_id,
    }).ok
    page.reload()
    page.wait_for_function("document.querySelector('[data-codex-model]').options.length > 1")


def test_mobile_composer_sends_without_reload_or_scroll_jump(live_hub: str):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        errors: list[str] = []
        navigations: list[str] = []
        document_requests: list[str] = []
        project_refreshes: list[str] = []
        post_statuses: list[int] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "framenavigated",
            lambda frame: navigations.append(frame.url)
            if frame == page.main_frame
            else None,
        )
        page.on(
            "response",
            lambda response: post_statuses.append(response.status)
            if response.request.method == "POST"
            and response.url.endswith("/entries")
            else None,
        )
        page.on(
            "request",
            lambda request: document_requests.append(request.url)
            if request.resource_type == "document"
            else None,
        )
        page.on(
            "request",
            lambda request: project_refreshes.append(request.url)
            if request.method == "GET"
            and request.resource_type == "fetch"
            and request.url.endswith("/projects/mobile-scroll-e2e")
            else None,
        )
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")

            page.fill('input[name="slug"]', "mobile-scroll-e2e")
            page.fill('input[name="name"]', "Mobile Scroll E2E")
            page.click('form[action$="/projects"] button[type="submit"]')
            page.wait_for_url("**/projects/mobile-scroll-e2e")
            connect_mobile_test_device(page)
            baseline_navigations = len(navigations)
            baseline_entries = page.locator('.chat-message[data-entry-id]').count()

            for index in range(12):
                message = f"message {index} " + ("long mobile history line " * 24)
                page.fill("[data-mention-input]", message)
                page.click(".send-button")
                page.wait_for_function(
                    "expected => document.querySelectorAll("
                    "'.chat-message[data-entry-id]').length === expected",
                    arg=baseline_entries + index + 1,
                )

            chat = page.locator('[data-auto-refresh="chat"]')
            metrics = chat.evaluate(
                "node => ({ scrollHeight: node.scrollHeight, "
                "clientHeight: node.clientHeight, "
                "height: getComputedStyle(node).height, "
                "messageHeights: Array.from(node.querySelectorAll('.chat-message'))"
                ".map(item => item.getBoundingClientRect().height) })"
            )
            assert metrics["scrollHeight"] > metrics["clientHeight"], metrics
            scroll_css = chat.evaluate(
                "node => { const stream = getComputedStyle(node); "
                "const message = getComputedStyle(node.querySelector('.chat-message')); "
                "return { touchAction: stream.touchAction, "
                "overscrollBehaviorY: stream.overscrollBehaviorY, "
                "contentVisibility: message.contentVisibility, "
                "containIntrinsicSize: message.containIntrinsicSize }; }"
            )
            assert scroll_css["touchAction"] == "pan-y", scroll_css
            assert scroll_css["overscrollBehaviorY"] == "contain", scroll_css
            assert scroll_css["contentVisibility"] == "visible", scroll_css
            assert scroll_css["containIntrinsicSize"] == "none", scroll_css
            page.wait_for_function(
                "() => { const node = document.querySelector('[data-auto-refresh=\"chat\"]'); "
                "return node && Math.abs(node.scrollTop - "
                "(node.scrollHeight - node.clientHeight)) <= 1; }",
                timeout=3000,
            )

            chat.evaluate("node => { node.scrollTop = 0; }")
            assert len(navigations) == baseline_navigations
            page.evaluate(
                "window.location.hash = document.querySelector("
                "'.chat-message[data-entry-id]').id"
            )
            page.wait_for_timeout(50)
            assert "#entry-" in page.url
            url_with_hash = page.url
            hash_navigation_count = len(navigations)
            baseline_documents = len(document_requests)
            baseline_refreshes = len(project_refreshes)
            page.fill(
                "[data-mention-input]",
                "final no-navigation message " + ("payload " * 30),
            )
            outer_before = page.evaluate("window.scrollY")
            page.click(".send-button")
            page.wait_for_function(
                "expected => document.querySelectorAll("
                "'.chat-message[data-entry-id]').length === expected",
                arg=baseline_entries + 13,
            )
            page.wait_for_timeout(150)
            outer_after = page.evaluate("window.scrollY")

            assert navigations[hash_navigation_count:] == []
            assert len(document_requests) == baseline_documents
            assert len(project_refreshes) == baseline_refreshes
            assert page.url == url_with_hash
            assert post_statuses and set(post_statuses) == {201}
            assert outer_after == outer_before
            assert chat.evaluate(
                "node => Math.abs(node.scrollTop - "
                "(node.scrollHeight - node.clientHeight)) <= 1"
            )
            assert errors == []
        finally:
            context.close()
            browser.close()


def test_android_more_menu_first_five_tools_are_visible_and_interactive(live_hub: str):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")

            page.fill('input[name="slug"]', "android-tools-e2e")
            page.fill('input[name="name"]', "Android Tools E2E")
            page.click('form[action$="/projects"] button[type="submit"]')
            page.wait_for_url("**/projects/android-tools-e2e")
            connect_mobile_test_device(page)

            page.add_style_tag(path=str(ANDROID_CSS))
            page.add_script_tag(path=str(ANDROID_JS))
            page.locator(".android-drawer-toggle").click()
            page.locator(".android-drawer-manage").click()

            expected_targets = [
                "control",
                "codex-workspaces",
                "tasks",
                "members",
                "files",
            ]
            for target_id in expected_targets:
                button = page.locator(
                    f'.android-tools-nav button[data-android-target="{target_id}"]'
                )
                assert button.count() == 1
                button.click()
                target = page.locator(f"#{target_id}")
                assert target.is_visible()
                assert target.get_attribute("aria-hidden") == "false"
                assert target.evaluate(
                    "node => node.getBoundingClientRect().height > 0"
                )
                container_state = page.locator(".control-pane").evaluate(
                    "node => ({ inert: node.inert, ariaHidden: node.getAttribute('aria-hidden'), "
                    "transform: getComputedStyle(node).transform, position: getComputedStyle(node).position })"
                )
                assert container_state == {
                    "inert": False,
                    "ariaHidden": "false",
                    "transform": "none",
                    "position": "static",
                }

            assert errors == []
        finally:
            context.close()
            browser.close()


def test_projects_page_uses_the_full_phone_viewport(live_hub: str):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")

            metrics = page.evaluate(
                """() => {
                  const shell = document.querySelector('.shell');
                  const workspace = document.querySelector('.workspace.two');
                  const panels = Array.from(document.querySelectorAll('.workspace.two > .panel'));
                  return {
                    viewportWidth: window.innerWidth,
                    documentWidth: document.documentElement.scrollWidth,
                    shellWidth: shell.getBoundingClientRect().width,
                    workspaceWidth: workspace.getBoundingClientRect().width,
                    panelWidths: panels.map(node => node.getBoundingClientRect().width),
                    gridColumns: getComputedStyle(workspace).gridTemplateColumns,
                  };
                }"""
            )
            assert metrics["viewportWidth"] == 390
            assert metrics["documentWidth"] <= 390
            assert metrics["shellWidth"] >= 360
            assert metrics["workspaceWidth"] >= 350
            assert all(width >= 350 for width in metrics["panelWidths"])
            assert " " not in metrics["gridColumns"].strip()
        finally:
            context.close()
            browser.close()


def test_narrow_phone_keeps_web_and_android_send_controls_in_view(live_hub: str):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 320, "height": 568})
        page = context.new_page()
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")
            page.fill('input[name="slug"]', "narrow-composer-e2e")
            page.fill('input[name="name"]', "Narrow Composer E2E")
            page.click('form[action$="/projects"] button[type="submit"]')
            page.wait_for_url("**/projects/narrow-composer-e2e")
            connect_mobile_test_device(page)

            def assert_controls_fit() -> None:
                metrics = page.evaluate(
                    """() => {
                      const tools = document.querySelector('.composer-tools');
                      const send = document.querySelector('.send-button');
                      const toolsRect = tools.getBoundingClientRect();
                      const sendRect = send.getBoundingClientRect();
                      return {
                        viewport: window.innerWidth,
                        documentWidth: document.documentElement.scrollWidth,
                        toolsLeft: toolsRect.left,
                        toolsRight: toolsRect.right,
                        sendLeft: sendRect.left,
                        sendRight: sendRect.right,
                        sendWidth: sendRect.width,
                        sendHeight: sendRect.height,
                      };
                    }"""
                )
                assert metrics["documentWidth"] <= metrics["viewport"]
                assert metrics["toolsLeft"] >= 0
                assert metrics["toolsRight"] <= metrics["viewport"]
                assert metrics["sendLeft"] >= 0
                assert metrics["sendRight"] <= metrics["viewport"]
                assert metrics["sendWidth"] >= 42
                assert metrics["sendHeight"] >= 42

            assert_controls_fit()
            page.add_style_tag(path=str(ANDROID_CSS))
            page.add_script_tag(path=str(ANDROID_JS))
            assert_controls_fit()
        finally:
            context.close()
            browser.close()


def test_composer_stages_files_until_the_message_is_sent(live_hub: str):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        post_urls: list[str] = []
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: post_urls.append(request.url)
            if request.method == "POST"
            else None,
        )
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")

            page.fill('input[name="slug"]', "staged-files-e2e")
            page.fill('input[name="name"]', "Staged Files E2E")
            page.click('form[action$="/projects"] button[type="submit"]')
            page.wait_for_url("**/projects/staged-files-e2e")
            connect_mobile_test_device(page)
            post_urls.clear()

            page.set_input_files(
                "[data-attachment-input]",
                [
                    {
                        "name": "预览图.png",
                        "mimeType": "image/png",
                        "buffer": b"\x89PNG\r\n\x1a\n",
                    },
                    {
                        "name": "说明.txt",
                        "mimeType": "text/plain",
                        "buffer": b"details",
                    },
                ],
            )
            page.wait_for_timeout(150)

            tray = page.locator("[data-composer-attachments]")
            assert tray.is_visible()
            assert tray.locator(".composer-attachment").count() == 2
            assert tray.locator("img").count() == 1
            assert page.locator("[data-attachment-input]").evaluate(
                "node => node.files.length"
            ) == 2
            assert post_urls == []

            page.fill("[data-mention-input]", "附件和这条消息一起发送")
            page.click(".send-button")
            page.wait_for_function(
                "document.querySelectorAll('.chat-message[data-entry-id]').length === 1"
            )
            page.wait_for_function(
                "document.querySelectorAll('.chat-message [data-media-image], "
                ".chat-message .message-file').length === 2"
            )

            assert any(url.endswith("/projects/staged-files-e2e/entries") for url in post_urls)
            assert not any(url.endswith("/attachments") for url in post_urls)
            assert page.locator(
                ".chat-message [data-media-image], .chat-message .message-file"
            ).count() == 2
            assert tray.is_hidden()
            assert page.locator("[data-attachment-input]").evaluate(
                "node => node.files.length"
            ) == 0
            assert errors == []
        finally:
            context.close()
            browser.close()


def test_composer_stages_and_sends_files_when_file_list_replacement_is_blocked(
    live_hub: str,
):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")

            page.fill('input[name="slug"]', "restricted-file-list-e2e")
            page.fill('input[name="name"]', "Restricted File List E2E")
            page.click('form[action$="/projects"] button[type="submit"]')
            page.wait_for_url("**/projects/restricted-file-list-e2e")
            connect_mobile_test_device(page)

            page.evaluate(
                """
                Object.defineProperty(window, "DataTransfer", {
                  configurable: true,
                  value: function BlockedDataTransfer() {
                    throw new DOMException("File list replacement blocked", "NotAllowedError");
                  },
                });
                """
            )
            page.set_input_files(
                "[data-attachment-input]",
                {
                    "name": "preview.png",
                    "mimeType": "image/png",
                    "buffer": b"\x89PNG\r\n\x1a\n",
                },
            )
            page.set_input_files(
                "[data-attachment-input]",
                {
                    "name": "notes.txt",
                    "mimeType": "text/plain",
                    "buffer": b"details",
                },
            )

            tray = page.locator("[data-composer-attachments]")
            assert tray.is_visible()
            assert tray.locator(".composer-attachment").count() == 2
            assert tray.locator("img").count() == 1
            assert page.locator("[data-attachment-input]").evaluate(
                "node => node.files.length"
            ) == 1

            page.fill("[data-mention-input]", "受限浏览器附件回归")
            page.click(".send-button")
            page.wait_for_function(
                "document.querySelectorAll('.chat-message[data-entry-id]').length === 1"
            )
            page.wait_for_function(
                "document.querySelectorAll('.chat-message [data-media-image], "
                ".chat-message .message-file').length === 2"
            )

            assert tray.is_hidden()
            assert errors == []
        finally:
            context.close()
            browser.close()


def test_android_style_picker_previews_extension_image_when_mime_is_empty(
    live_hub: str,
):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        context.add_init_script(
            "URL.createObjectURL = () => { throw new Error('object URL unavailable'); };"
        )
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")
            page.fill('input[name="slug"]', "empty-mime-preview")
            page.fill('input[name="name"]', "Empty MIME Preview")
            page.click('form[action$="/projects"] button[type="submit"]')
            page.wait_for_url("**/projects/empty-mime-preview")
            connect_mobile_test_device(page)

            page.set_input_files(
                "[data-attachment-input]",
                {
                    "name": "手机照片.PNG",
                    "mimeType": "",
                    "buffer": b"\x89PNG\r\n\x1a\n",
                },
            )
            image = page.locator("[data-composer-attachments] .is-image img")
            page.wait_for_function(
                "document.querySelector('[data-composer-attachments] .is-image img')"
                "?.src.startsWith('data:')"
            )

            assert image.count() == 1
            assert image.get_attribute("src").startswith("data:")
            assert errors == []
        finally:
            context.close()
            browser.close()


def test_android_native_preview_and_private_draft_bridge_drive_composer(
    live_hub: str,
):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")
            page.fill('input[name="slug"]', "android-local-data")
            page.fill('input[name="name"]', "Android Local Data")
            page.click('form[action$="/projects"] button[type="submit"]')
            page.wait_for_url("**/projects/android-local-data")
            connect_mobile_test_device(page)

            page.evaluate(
                """
                window.__nativeDrafts = new Map([
                  [location.pathname, "restored on device"],
                ]);
                window.HubLocalData = {
                  loadDraft: key => window.__nativeDrafts.get(key) || "",
                  saveDraft: (key, value) => window.__nativeDrafts.set(key, value),
                  clearDraft: key => window.__nativeDrafts.delete(key),
                };
                URL.createObjectURL = () => { throw new Error("blocked in WebView"); };
                window.FileReader = undefined;
                window.CodexHubAndroidVersion = "0.1.22";
                """
            )
            page.add_script_tag(path=str(ANDROID_JS))
            assert page.input_value("[data-mention-input]") == "restored on device"
            assert page.locator(".android-bottom-nav").count() == 1
            assert page.locator(".android-bottom-nav button").count() == 3
            assert page.locator(
                '.android-bottom-nav button[data-android-view="chat"]'
            ).get_attribute("aria-current") == "page"

            page.click('.android-bottom-nav button[data-android-view="more"]')
            assert "android-tools-mode" in page.locator("body").get_attribute("class")
            assert page.locator(
                '.android-bottom-nav button[data-android-view="more"]'
            ).get_attribute("aria-current") == "page"
            page.click('.android-bottom-nav button[data-android-view="projects"]')
            assert page.locator(".workbench-sidebar").get_attribute("aria-hidden") == "false"
            page.click('.android-bottom-nav button[data-android-view="chat"]')

            page.click(".android-drawer-toggle")
            page.wait_for_function(
                "document.activeElement?.hasAttribute('data-android-drawer-close')"
            )
            assert "android-drawer-open" in page.locator("body").get_attribute("class")
            page.eval_on_selector(".android-drawer-scrim", "element => element.click()")
            assert "android-drawer-open" not in page.locator("body").get_attribute("class")

            page.fill("[data-mention-input]", "saved locally")
            page.wait_for_timeout(650)
            assert page.evaluate(
                "window.__nativeDrafts.get(location.pathname)"
            ) == "saved locally"

            context.set_offline(True)
            page.wait_for_function("document.body.classList.contains('android-offline')")
            page.fill("[data-mention-input]", "keep this offline")
            page.eval_on_selector("#project-message-form", "form => form.requestSubmit()")
            assert page.evaluate(
                "window.__nativeDrafts.get(location.pathname)"
            ) == "keep this offline"
            assert "当前离线" in page.locator("[data-message-status]").inner_text()
            context.set_offline(False)
            page.wait_for_function("!document.body.classList.contains('android-offline')")

            page.set_input_files(
                "[data-attachment-input]",
                {
                    "name": "native-preview.png",
                    "mimeType": "image/png",
                    "buffer": b"\x89PNG\r\n\x1a\n",
                },
            )
            assert page.locator(
                "[data-composer-attachments] .preview-unavailable"
            ).count() == 1
            preview = (
                "data:image/png;base64,"
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMA"
                "ASsJTYQAAAAASUVORK5CYII="
            )
            page.evaluate(
                "preview => window.CodexHubAndroidFiles.receiveSelection(["
                "{name: 'native-preview.png', size: 8, type: 'image/png', preview}])",
                preview,
            )
            image = page.locator("[data-composer-attachments] .is-image img")
            page.wait_for_function(
                "document.querySelector('[data-composer-attachments] .is-image img')"
                "?.src.startsWith('data:image/png;base64,')"
            )
            assert image.get_attribute("src") == preview
            assert errors == []
        finally:
            context.close()
            browser.close()


def test_composer_submits_codex_and_hermes_runtime_preferences(live_hub: str):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")
            page.fill('input[name="slug"]', "runtime-preferences")
            page.fill('input[name="name"]', "Runtime Preferences")
            page.click('form[action$="/projects"] button[type="submit"]')
            page.wait_for_url("**/projects/runtime-preferences")
            connect_mobile_test_device(page)
            page.evaluate(
                """
                const originalFetch = window.fetch.bind(window);
                window.fetch = (url, options = {}) => {
                  if (String(url).endsWith('/entries') && options.body instanceof FormData) {
                    window.__submittedRuntime = Object.fromEntries(options.body.entries());
                  }
                  return originalFetch(url, options);
                };
                """
            )

            page.click("[data-runtime-settings] summary")
            page.select_option("[data-codex-model]", "gpt-6-astra")
            page.select_option("[data-codex-reasoning]", "ultra")
            page.fill("[data-hermes-model]", "nous/hermes-4")
            page.select_option("[data-hermes-reasoning]", "max")
            assert "6 Astra" in page.locator("[data-runtime-summary]").inner_text()

            page.fill("[data-mention-input]", "runtime selection")
            page.click(".send-button")
            page.wait_for_function("window.__submittedRuntime !== undefined")
            submitted = page.evaluate("window.__submittedRuntime")

            assert submitted["codex_model"] == "gpt-6-astra"
            assert submitted["codex_reasoning_effort"] == "ultra"
            assert submitted["hermes_model"] == "nous/hermes-4"
            assert submitted["hermes_reasoning_effort"] == "max"
        finally:
            context.close()
            browser.close()


def test_voice_button_uses_one_animated_turn_state(live_hub: str):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        context.add_init_script(
            """
            window.SpeechRecognition = class {
              start() {
                window.setTimeout(() => {
                  const result = [{ transcript: "语音测试" }];
                  result.isFinal = true;
                  this.onresult?.({ resultIndex: 0, results: [result] });
                }, 120);
              }
              stop() { this.onend?.(); }
            };
            """
        )
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")

            page.fill('input[name="slug"]', "voice-turn-e2e")
            page.fill('input[name="name"]', "Voice Turn E2E")
            page.click('form[action$="/projects"] button[type="submit"]')
            page.wait_for_url("**/projects/voice-turn-e2e")
            connect_mobile_test_device(page)

            voice = page.locator("[data-voice-session]")
            assert voice.count() == 1
            assert page.locator("[data-voice-input], [data-voice-speak-latest]").count() == 0
            voice.click()
            page.wait_for_function(
                "document.querySelector('[data-voice-session]')?.dataset.voiceState === 'listening'"
            )
            assert voice.get_attribute("data-voice-state") == "listening"
            assert voice.locator(".voice-session-glyph i").count() == 5

            page.wait_for_function(
                "document.querySelector('[data-mention-input]').value.includes('语音测试')"
            )
            assert voice.get_attribute("data-voice-state") == "ready"
            assert voice.get_attribute("aria-pressed") == "true"

            page.locator("#project-message-form").evaluate(
                "node => node.dispatchEvent(new CustomEvent('codex-hub:message-sent'))"
            )
            assert voice.get_attribute("data-voice-state") == "waiting"
            assert voice.evaluate("node => node.classList.contains('is-waiting')")

            voice.click()
            assert voice.get_attribute("data-voice-state") == "idle"
            assert voice.get_attribute("aria-pressed") == "false"
            assert errors == []
        finally:
            context.close()
            browser.close()


def test_mobile_project_drawers_are_exclusive_accessible_and_non_blocking(
    live_hub: str,
):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")

            page.fill('input[name="slug"]', "mobile-overlays")
            page.fill('input[name="name"]', "Mobile Overlays")
            page.click('form[action$="/projects"] button[type="submit"]')
            page.wait_for_url("**/projects/mobile-overlays")
            connect_mobile_test_device(page)

            sidebar_button = page.locator("[data-sidebar-open]")
            sidebar = page.locator("[data-project-sidebar]")
            memory_button = page.locator("[data-memory-open]:visible").first
            drawer = page.locator("[data-memory-drawer]")

            assert sidebar_button.get_attribute("aria-expanded") == "false"
            assert sidebar.get_attribute("aria-hidden") == "true"
            sidebar_button.click()
            assert sidebar_button.get_attribute("aria-expanded") == "true"
            assert sidebar.get_attribute("aria-hidden") == "false"
            assert page.locator("body").evaluate(
                "node => node.classList.contains('project-sidebar-open')"
            )

            memory_button.click()
            assert page.locator("body").evaluate(
                "node => node.classList.contains('memory-drawer-open')"
            )
            assert not page.locator("body").evaluate(
                "node => node.classList.contains('project-sidebar-open')"
            )
            assert drawer.get_attribute("aria-hidden") == "false"
            assert sidebar.get_attribute("aria-hidden") == "true"
            assert drawer.evaluate("node => node.inert") is False
            assert page.locator(".memory-backdrop").count() == 0

            page.locator('[data-auto-refresh="chat"]').click(
                position={"x": 10, "y": 20}
            )
            assert drawer.get_attribute("aria-hidden") == "true"
            assert drawer.evaluate("node => node.inert") is True

            memory_button.click()

            page.keyboard.press("Escape")
            assert drawer.get_attribute("aria-hidden") == "true"
            assert drawer.evaluate("node => node.inert") is True
            assert memory_button.evaluate("node => document.activeElement === node")

            page.evaluate(
                "history.pushState(null, '', '#%'); "
                "window.dispatchEvent(new HashChangeEvent('hashchange'));"
            )
            page.wait_for_timeout(50)
            assert page.evaluate(
                "document.documentElement.scrollWidth <= "
                "document.documentElement.clientWidth"
            )
            assert errors == []
        finally:
            context.close()
            browser.close()


def test_android_chat_header_moves_secondary_actions_into_project_drawer(
    live_hub: str,
):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")

            page.fill('input[name="slug"]', "android-compact-header")
            page.fill('input[name="name"]', "Private Server Buildout")
            page.click('form[action$="/projects"] button[type="submit"]')
            page.wait_for_url("**/projects/android-compact-header")
            connect_mobile_test_device(page)

            page.evaluate('window.CodexHubAndroidVersion = "0.1.20"')
            page.add_style_tag(path=str(ANDROID_CSS))
            page.add_script_tag(path=str(ANDROID_JS))

            assert page.locator("body").evaluate(
                "node => node.classList.contains('android-chat-mode')"
            )
            header_height=page.locator(".workbench-topbar").evaluate("node => node.getBoundingClientRect().height")
            assert header_height<=60,header_height
            assert page.locator(".workbench-title h1").inner_text() == (
                "Private Server Buildout"
            )
            assert page.locator(".android-version-badge").inner_text() == "v0.1.20"
            assert page.locator(".workbench-topbar button:visible").all_text_contents() == [
                "☰"
            ]
            assert page.locator(".workbench-topbar").evaluate(
                "node => node.scrollWidth <= node.clientWidth"
            )

            target_geometry = page.locator(".composer-target").evaluate(
                    "node => { const select = node.querySelector('summary'); "
                "const outer = node.getBoundingClientRect(); "
                "const inner = select.getBoundingClientRect(); "
                "const points = [[inner.left + 8, inner.top + 8], "
                "[inner.right - 8, inner.top + 8], "
                "[inner.left + 8, inner.bottom - 8], "
                "[inner.right - 8, inner.bottom - 8]]; "
                "return { outer: outer.toJSON(), inner: inner.toJSON(), "
                    "hits: points.map(([x, y]) => select.contains(document.elementFromPoint(x, y))) }; }"
            )
            assert target_geometry["outer"]["width"] <= 100
            assert target_geometry["outer"]["width"] == target_geometry["inner"]["width"]
            assert target_geometry["outer"]["height"] == target_geometry["inner"]["height"]
            assert all(target_geometry["hits"])

            touch_move_not_cancelled = page.locator(".workbench-shell").evaluate(
                "node => { const start = new Touch({ identifier: 1, target: node, "
                "clientX: 10, clientY: 300 }); "
                "node.dispatchEvent(new TouchEvent('touchstart', { touches: [start], "
                "changedTouches: [start], bubbles: true, cancelable: true })); "
                "const move = new Touch({ identifier: 1, target: node, "
                "clientX: 36, clientY: 304 }); "
                "return node.dispatchEvent(new TouchEvent('touchmove', { touches: [move], "
                "changedTouches: [move], bubbles: true, cancelable: true })); }"
            )
            assert touch_move_not_cancelled is True

            page.locator(".android-drawer-toggle").click()
            drawer = page.locator(".android-project-drawer")
            assert drawer.get_attribute("aria-hidden") == "false"
            assert drawer.evaluate("node => node.inert") is False
            assert page.locator(".android-drawer-backdrop").count() == 0
            assert drawer.get_by_text("项目记忆", exact=True).is_visible()
            assert drawer.get_by_text("更多功能", exact=True).is_visible()

            drawer.locator("[data-android-memory-open]").click()
            assert page.locator("body").evaluate(
                "node => node.classList.contains('android-tools-mode')"
            )
            assert not page.locator("body").evaluate(
                "node => node.classList.contains('memory-drawer-open')"
            )
            assert page.locator("#control").is_visible()
            page.locator(".android-tools-back").click()

            page.locator(".android-drawer-toggle").click()
            drawer.get_by_text("更多功能", exact=True).click()
            assert page.locator("body").evaluate(
                "node => node.classList.contains('android-tools-mode')"
            )
            assert page.locator(".android-tools-back").is_visible()
            assert page.locator(".android-tools-back").inner_text() == "返回聊天"
            tool_buttons = page.locator(
                ".android-tools-nav button[data-android-target]"
            )
            tool_geometry = page.evaluate(
                "() => { const button = document.querySelector("
                "'.android-tools-nav button[data-android-target]'); "
                "const topbar = document.querySelector('.workbench-topbar'); "
                "const back = document.querySelector('.android-tools-back'); "
                "return { buttonTop: button.getBoundingClientRect().top, "
                "topbarBottom: topbar.getBoundingClientRect().bottom, "
                "gridTop: document.querySelector('.workbench-grid')"
                ".getBoundingClientRect().top, "
                "buttonRect: button.getBoundingClientRect().toJSON(), "
                "backRect: back.getBoundingClientRect().toJSON(), "
                "backPosition: getComputedStyle(back).position }; }"
            )
            assert tool_geometry["buttonTop"] >= tool_geometry["topbarBottom"], (
                tool_geometry
            )
            assert tool_geometry["backRect"]["bottom"] <= tool_geometry["topbarBottom"], (
                tool_geometry["backRect"]
            )
            for index in range(tool_buttons.count()):
                button = tool_buttons.nth(index)
                target_id = button.get_attribute("data-android-target")
                hit_test = button.evaluate(
                    "node => { const rect = node.getBoundingClientRect(); "
                    "const hit = document.elementFromPoint("
                    "rect.left + rect.width / 2, rect.top + rect.height / 2); "
                    "return { matches: hit?.closest('button') === node, "
                    "rect: { x: rect.x, y: rect.y, width: rect.width, "
                    "height: rect.height }, hit: hit?.outerHTML || '' }; }"
                )
                assert hit_test["matches"], hit_test
                bounds = button.bounding_box()
                assert bounds is not None
                page.mouse.click(
                    bounds["x"] + bounds["width"] / 2,
                    bounds["y"] + bounds["height"] / 2,
                )
                assert button.get_attribute("aria-current") == "page"
                assert page.locator(f"#{target_id}").is_visible()
            page.locator(".android-tools-back").click()
            assert page.locator(".android-tools-back").is_hidden()
            assert errors == []
        finally:
            context.close()
            browser.close()


def test_live_chat_appends_incremental_reply_without_full_page_navigation(live_hub: str):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        update_requests: list[str] = []
        page.on(
            "request",
            lambda request: update_requests.append(request.url)
            if "/chat-updates" in request.url
            else None,
        )
        try:
            page.goto(f"{live_hub}/login")
            page.fill('input[name="username"]', "owner")
            page.fill('input[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects")
            page.fill('input[name="slug"]', "incremental-browser-chat")
            page.fill('input[name="name"]', "Incremental Browser Chat")
            page.click('form[action$="/projects"] button[type="submit"]')
            page.wait_for_url("**/projects/incremental-browser-chat")
            connect_mobile_test_device(page)
            page.wait_for_timeout(400)

            page.evaluate(
                """
                async () => {
                  const form = document.querySelector('#project-message-form');
                  const data = new FormData();
                  data.append('_csrf', form.querySelector('[name="_csrf"]').value);
                  data.append('kind', 'message');
                  data.append('body_markdown', 'arrived through incremental sync');
                  const response = await fetch(form.action, {
                    method: 'POST',
                    credentials: 'same-origin',
                    body: data,
                    headers: {
                      Accept: 'application/json',
                      'X-Requested-With': 'codex-hub-composer',
                    },
                  });
                  window.__incrementalCreated = response.ok;
                }
                """
            )
            page.wait_for_function("window.__incrementalCreated === true")
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.chat-message'))"
                ".some(node => node.textContent.includes('arrived through incremental sync'))",
                timeout=6000,
            )

            assert page.url.endswith("/projects/incremental-browser-chat?view=archive")
            assert update_requests
            assert all("after_entry_id=" in url for url in update_requests)
        finally:
            context.close()
            browser.close()
