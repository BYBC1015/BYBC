from pathlib import Path

import pytest

from codex_hub.model_catalog import normalize_codex_catalog


playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]


def public_catalog(models, efforts=("low", "medium", "high", "max", "ultra")):
    return normalize_codex_catalog({"source": "models-cache", "models": [
        {"model": model, "displayName": model, "isDefault": index == 0,
         "supportedReasoningEfforts": [{"reasoningEffort": effort} for effort in efforts]}
        for index, model in enumerate(models)
    ]})


@pytest.mark.parametrize("width", [320, 390])
def test_mobile_model_catalog_and_selected_member_delivery(client, repo, width):
    one = repo.create_device(label="Codex-主设备")
    two = repo.create_device(label="Codex-第二设备")
    three = repo.create_device(label="Hermes-设备")
    project = repo.create_project(slug="model-routing", name="手机模型与成员", created_by_device_id=one["id"])
    for member in [two, three]:
        repo.add_project_member(project["id"], member["id"], "write")
    repo.set_project_collaboration_mode(project["id"], True)
    for member, backend, catalog in [
        (one, "codex", public_catalog(["gpt-6-astra", "future-model-29"])),
        (two, "codex", public_catalog(["gpt-6-astra"], efforts=("low", "high"))),
        (three, "hermes", {}),
    ]:
        token = repo.create_api_token(device_id=member["id"], name="catalog-test")
        response = client.post("/api/device/capabilities", headers={"Authorization": f"Bearer {token.raw}"},
                               json={"execution_backend": backend, "codex": catalog})
        assert response.status_code == 200
    assert client.post("/login", data={"username": "owner", "password": "owner-password"}).status_code == 200

    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {exc}")
        page = browser.new_page(viewport={"width": width, "height": 844})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        def forward(route):
            request = route.request
            path = request.url.removeprefix("http://hub.test")
            headers = {key: value for key, value in request.headers.items() if key in {"content-type", "accept", "x-requested-with"}}
            response = client.request(request.method, path, content=request.post_data_buffer, headers=headers)
            route.fulfill(status=response.status_code, body=response.content, headers={
                "Content-Type": response.headers.get("content-type", "application/octet-stream")})
        page.route("http://hub.test/**", forward)
        try:
            page.goto("http://hub.test/projects/model-routing?view=archive")
            page.add_style_tag(path=str(ROOT / "android/app/src/main/assets/android-app.css"))
            page.evaluate("document.documentElement.classList.add('codex-hub-android'); document.body.classList.add('android-chat-mode')")
            page.wait_for_function("document.querySelector('[data-codex-model]').options.length === 3")
            assert page.locator("[data-delivery-mode]").input_value() == "selected"
            assert page.locator("[data-delivery-member]:checked").count() == 1
            page.fill("[data-mention-input]", "执行这个任务")
            page.click("[data-delivery-settings] summary")
            panel = page.locator(".composer-delivery-panel")
            bounds = panel.bounding_box()
            assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width, bounds
            assert panel.is_visible()
            if width == 390:
                output = ROOT.parent / "output/hub-fix-20260911"
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / "mobile-members.png"))
            page.check(f'[data-delivery-member][value="{two["id"]}"]')
            assert page.locator("[data-mention-input]").input_value() == "执行这个任务"
            assert page.locator("[data-codex-model] option").count() == 2
            page.click("[data-delivery-settings] summary")
            page.click("[data-runtime-settings] summary")
            page.select_option("[data-codex-model]", "gpt-6-astra")
            efforts = page.locator("[data-codex-reasoning] option").evaluate_all("items => items.map(item => item.value)")
            assert efforts == ["", "low", "high"]
            page.select_option("[data-codex-reasoning]", "high")
            page.click("[data-runtime-settings] summary")
            page.click(".send-button")
            page.wait_for_function("document.querySelector('[data-message-status]').textContent === '已发送'")
            entry = dict(repo.connection.execute("SELECT * FROM entries WHERE project_id = ? AND task_id IS NULL ORDER BY id DESC LIMIT 1", (project["id"],)).fetchone())
            import json
            metadata = json.loads(entry["metadata_json"])
            assert metadata["routing"] == {"mode": "selected", "device_ids": [one["id"], two["id"]]}
            assert metadata["execution"]["codex"] == {"model": "gpt-6-astra", "reasoning_effort": "high"}
            page.click("[data-delivery-settings] summary")
            page.uncheck(f'[data-delivery-member][value="{two["id"]}"]')
            page.click("[data-delivery-settings] summary")
            page.click("[data-runtime-settings] summary")
            page.select_option("[data-codex-model]", "gpt-6-astra")
            page.select_option("[data-codex-reasoning]", "ultra")
            if width == 390:
                page.screenshot(path=str(output / "mobile-models.png"))
            page.click("[data-runtime-settings] summary")
            page.click("[data-delivery-settings] summary")
            page.select_option("[data-delivery-mode]", "group")
            assert page.locator("[data-delivery-members]").evaluate("node => node.disabled")
            assert page.locator("[data-delivery-member]").first.is_disabled()
            assert page.locator("[data-delivery-summary]").inner_text() == "全组协作"
            page.click("[data-delivery-settings] summary")
            page.fill("[data-mention-input]", "共同研讨这个任务")
            page.click(".send-button")
            page.wait_for_function("document.querySelector('[data-mention-input]').value === ''")
            entry = dict(repo.connection.execute("SELECT * FROM entries WHERE project_id = ? AND task_id IS NULL ORDER BY id DESC LIMIT 1", (project["id"],)).fetchone())
            metadata = json.loads(entry["metadata_json"])
            assert metadata["routing"]["mode"] == "group"
            assert sorted(metadata["routing"]["device_ids"]) == [one["id"], two["id"], three["id"]]
            assert not errors
        finally:
            browser.close()
