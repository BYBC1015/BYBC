from pathlib import Path

import pytest
from test_mobile_composer_browser import live_hub  # noqa: F401

from codex_hub.agents import AgentStore

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [1440, 1024, 390, 320])
def test_durable_agents_creation_assignment_shared_conversation_and_learning(live_hub, repo, width):
    """One executor can host multiple identities; browser actions address identities."""
    execution = repo.create_device(label="共享执行连接")
    reader = repo.create_device(label="只读观察连接")
    project = repo.create_project(slug="agent-team", name="创作智能体团队", created_by_device_id=execution["id"])
    repo.add_project_member(project["id"], reader["id"], "read")
    store = AgentStore(repo.connection)
    controller = store.create(project["id"], "项目总控", "统筹任务与验收结果", kind="controller", device_id=execution["id"])
    designer = store.create(project["id"], "创意设计师", "负责视觉方向与分镜画面", "先检查阅读顺序", device_id=execution["id"])
    source_task = repo.create_task(project["id"], "主视觉设计复盘", assigned_agent_id=designer["id"])
    repo.update_task_status(source_task["id"], "review", "已完成初稿", {"next_time": "先验证长文案"})
    first_experience = store.experiences(project["id"], designer["id"])[0]
    repo.update_task_status(source_task["id"], "review", "修订了设计方法", {"next_time": "先验证信息层级"})
    repo.create_task(project["id"], "整理团队下一轮工作", status="in_progress", assigned_agent_id=controller["id"])
    base = f"{live_hub}/projects/{project['slug']}"

    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 900 if width > 540 else 844})
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def open_team():
            tab = page.locator(".project-tabs [data-agents-open]")
            if tab.is_visible():
                tab.click()
            else:
                page.locator(".hub-more > summary").click()
                page.locator(".hub-more-menu [data-agents-open]").click()
            playwright.expect(page.locator("[data-agent-workspace]")).to_be_visible()

        def assert_page_fits():
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

        def assert_panel_fits():
            panel = page.locator("[data-agent-workspace]")
            bounds = panel.bounding_box()
            assert bounds["x"] >= -1 and bounds["x"] + bounds["width"] <= width + 1
            assert panel.evaluate("el => el.scrollWidth <= el.clientWidth + 1")

        try:
            page.goto(f"{live_hub}/login")
            page.fill('[name="username"]', "owner")
            page.fill('[name="password"]', "owner-password")
            page.click('button[type="submit"]')
            page.wait_for_url("**/projects/agent-team")
            page.goto(base + '?view=archive')
            page.fill("[data-mention-input]", "保留这份正在写的草稿")
            open_team()
            assert page.locator("[data-agent-card]").count() == 2
            assert_panel_fits()
            page.keyboard.press("Escape")
            assert not page.locator("[data-agent-workspace]").is_visible()
            assert page.locator("[data-mention-input]").input_value() == "保留这份正在写的草稿"
            assert not repo.list_project_entries(project["id"])

            # Create a third identity on the same execution connection through the UI.
            open_team()
            page.locator("[data-agent-create-open]").first.click()
            create = page.locator("[data-agent-create]")
            playwright.expect(create).to_be_visible()
            assert_panel_fits()
            create.locator('[name="name"]').fill("交付审查员")
            create.locator('[name="job"]').fill("检查交付文件与修改要求")
            create.locator('[name="instructions"]').fill("从任务验收要求出发，核对文件完整性")
            create.locator('[name="device_id"]').select_option(str(execution["id"]))
            assert str(reader["id"]) not in create.locator('[name="device_id"] option').evaluate_all("els => els.map(el => el.value)")
            create.locator('button[type="submit"]').click()
            page.wait_for_url("**/projects/agent-team/agents/*")
            reviewer = next(agent for agent in store.list(project["id"]) if agent["name"] == "交付审查员")
            assert reviewer["id"] != designer["id"]
            assert reviewer["device_id"] == designer["device_id"]
            assert_page_fits()

            page.goto(base + '?view=archive')
            open_team()
            assert page.locator("[data-agent-card]").count() == 3
            assert_panel_fits()
            if width >= 1024:
                page.locator('[data-agent-view="list"]').click()
                assert "agent-list-view" in page.locator("[data-agent-workspace]").get_attribute("class")
                assert_panel_fits()
                page.locator('[data-agent-view="grid"]').click()
            output = Path(__file__).resolve().parents[2] / "output/hub-agent-workspace-20261027"
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / f"team-{width}.png"))

            # Filtering must retain both identity selections, even on one device.
            page.check(f'[data-agent-select][value="{designer["id"]}"]')
            page.fill("[data-agent-search]", "交付文件")
            assert page.locator("[data-agent-card]:visible").count() == 1
            assert "筛选外" in page.locator("[data-agent-selection-status]").inner_text()
            page.check(f'[data-agent-select][value="{reviewer["id"]}"]')
            page.evaluate("document.dispatchEvent(new Event('codexhub:agents-refreshed'))")
            assert page.locator("[data-agent-select]:checked").count() == 2
            page.locator("[data-agent-use-selection]").click()
            discussion = page.locator("[data-agent-discussion]")
            playwright.expect(discussion).to_be_visible()
            assert_panel_fits()
            discussion.locator('[name="title"]').fill("联合检查下一版方案")
            discussion.locator('[name="description"]').fill("设计师调整方案，审查员核对验收条件")
            discussion.locator('button[type="submit"]').click()
            page.wait_for_url("**/projects/agent-team/conversations/*")
            room_id = int(page.url.rsplit("/", 1)[-1])
            group_tasks = [task for task in repo.list_tasks(project["id"]) if task["title"] == "联合检查下一版方案"]
            assert len(group_tasks) == 2
            assert {task["assigned_agent_id"] for task in group_tasks} == {designer["id"], reviewer["id"]}
            assert {task["assigned_device_id"] for task in group_tasks} == {execution["id"]}
            assert {task["conversation_id"] for task in group_tasks} == {room_id}
            assert "创意设计师" in page.locator("body").inner_text()
            assert "交付审查员" in page.locator("body").inner_text()
            assert_page_fits()

            # Single task assignment addresses the identity ID, not its device ID.
            page.goto(base + '?view=archive')
            open_team()
            page.locator(f'[data-agent-assign="{designer["id"]}"]').click()
            assignment = page.locator("[data-agent-assignment]")
            playwright.expect(assignment).to_be_visible()
            assert assignment.locator('[name="assigned_agent_id"]').input_value() == str(designer["id"])
            assignment.locator('[name="title"]').fill("生成下一版视觉方案")
            assignment.locator('[name="description"]').fill("提供可审阅的预览与设计说明")
            assert_panel_fits()
            assignment.locator('button[type="submit"]').click()
            page.wait_for_url("**/projects/agent-team/tasks/*")
            task = next(task for task in repo.list_tasks(project["id"]) if task["title"] == "生成下一版视觉方案")
            assert task["assigned_agent_id"] == designer["id"]
            assert task["assigned_device_id"] == execution["id"]

            # Experience history and restore are real server-backed actions.
            page.goto(base + f"/agents/{designer['id']}")
            assert "先验证信息层级" in page.locator("body").inner_text()
            history = page.locator("[data-agent-experience-history]")
            if history.count() and not history.evaluate("el => el.open"):
                history.locator("summary").click()
            restore = page.locator(f'form[action$="/experiences/{first_experience["id"]}/restore"]')
            restore.get_by_role("button", name="恢复这个版本").click()
            page.wait_for_url(f"**/projects/agent-team/agents/{designer['id']}#experience")
            latest = store.experiences(project["id"], designer["id"])[0]
            assert latest["revision"] == 3
            assert latest["next_time"] == "先验证长文案"
            assert len(store.experiences(project["id"], designer["id"], history=True)) == 3
            assert store.experiences(project["id"], reviewer["id"]) == []
            assert_page_fits()
            assert not errors
        finally:
            browser.close()
