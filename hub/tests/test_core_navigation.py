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


playwright = pytest.importorskip("playwright.sync_api")


def login_owner(client) -> None:
    response = client.post(
        "/login",
        data={"username": "owner", "password": "owner-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture()
def live_hub(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "state",
        secret_key="core-navigation-secret",
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
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_shared_navigation_marks_the_current_section(client, repo):
    device = repo.create_device(label="navigation-device")
    login_owner(client)

    projects = client.get("/projects")
    devices = client.get("/devices")
    inbox = client.get(f"/devices/{device['id']}/inbox")

    assert '<a href="/projects" aria-current="page">项目</a>' in projects.text
    assert '<a href="/devices" aria-current="page">设备</a>' in devices.text
    assert '<a href="/inbox" aria-current="page">设备收件箱</a>' in inbox.text
    assert 'aria-controls="primary-navigation"' in projects.text
    assert "/static/hub-navigation.js?v=20260812-core-navigation1" in projects.text


def test_inbox_opens_the_first_device_instead_of_an_empty_placeholder(client, repo):
    repo.create_device(label="first-device")
    repo.create_device(label="second-device")
    login_owner(client)

    response = client.get("/inbox")

    assert response.status_code == 200
    assert "选择一个设备" not in response.text
    assert "请先选择设备" not in response.text
    assert "first-device" in response.text
    assert "selected-row" in response.text


def test_mobile_navigation_is_reachable_and_keyboard_dismissible(live_hub: str):
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

            toggle = page.locator("[data-nav-toggle]")
            navigation = page.locator("[data-primary-navigation]")
            assert toggle.is_visible()
            assert toggle.get_attribute("aria-expanded") == "false"
            assert navigation.is_hidden()
            assert page.locator('.nav a[aria-current="page"]').inner_text() == "项目"
            assert page.locator('a[href="#new-project"]').is_visible()

            toggle.click()
            assert toggle.get_attribute("aria-expanded") == "true"
            assert navigation.is_visible()

            page.keyboard.press("Escape")
            assert toggle.get_attribute("aria-expanded") == "false"
            assert navigation.is_hidden()
            assert page.evaluate(
                "document.activeElement === document.querySelector('[data-nav-toggle]')"
            )

            toggle.click()
            page.click('.nav a[href="/devices"]')
            page.wait_for_url("**/devices")
            assert page.locator('.nav a[aria-current="page"]').inner_text() == "设备"
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

            page.set_viewport_size({"width": 1200, "height": 800})
            assert navigation.is_visible()
            assert toggle.is_hidden()
        finally:
            context.close()
            browser.close()

