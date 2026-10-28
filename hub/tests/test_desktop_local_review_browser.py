"""Independent local-first review: real accounts, browser storage and dispatches.

The server runs in a temporary directory without executor workers. These checks
exercise observable boundaries rather than mocking the persistence module.
"""
import hashlib
from pathlib import Path

import pytest

from codex_hub.repository import utc_now
from codex_hub.security import hash_password
from codex_bridge.desktop_assets import offline_document, scrub_credentials
from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace

playwright = pytest.importorskip("playwright.sync_api")
REFERENCE = "只有明确发送后才可安排这份资料。\n中文文件字节必须保持原样。".encode()


def wait_ready(page):
    playwright.expect(page.locator("[data-ws-shell]")).to_have_attribute("data-ws-ready", "true")


def add_other_owner(repo):
    stamp = utc_now()
    cursor = repo.connection.execute(
        "INSERT INTO users(username,password_hash,role,created_at,updated_at) VALUES(?,?,'owner',?,?)",
        ("other-owner", hash_password("other-owner-password"), stamp, stamp),
    )
    repo.connection.commit()
    return cursor.lastrowid


def login_other(page, base):
    page.goto(base + "/login")
    page.locator('[name="username"]').fill("other-owner")
    page.locator('[name="password"]').fill("other-owner-password")
    page.locator('button[type="submit"]').click()
    wait_ready(page)


def test_other_account_cannot_send_previous_tabs_draft(live_hub, repo):
    data = seed_workspace(repo, room_messages=1)
    other_user = add_other_owner(repo)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            login_page(page, live_hub)
            original_user = page.locator("[data-ws-shell]").get_attribute("data-user")
            description = "这是原账号的本地草稿，不可委派给另一个账号。"
            page.locator("[data-ws-input]").fill(description)
            page.locator("[data-ws-attachment-input]").set_input_files({
                "name": "原账号资料.txt", "mimeType": "text/plain", "buffer": REFERENCE,
            })
            playwright.expect(page.locator("[data-ws-attachment]")).to_have_count(1)
            before = len(repo.list_tasks(data["project"]["id"]))
            other = context.new_page()
            # Actual logout/login replaces the shared cookie, rather than
            # pretending that an unauthenticated HTML response is a new user.
            other.goto(page.url)
            other.locator('form[action$="/logout"] button').click()
            login_other(other, live_hub)
            assert other.locator("[data-ws-shell]").get_attribute("data-user") == str(other_user)
            assert other.locator("[data-ws-input]").input_value() == ""
            playwright.expect(other.locator("[data-ws-attachment]")).to_have_count(0)

            page.bring_to_front()
            page.locator("[data-ws-send]").click()
            playwright.expect(page.locator("[data-ws-dispatch-error]")).to_be_visible()
            assert len(repo.list_tasks(data["project"]["id"])) == before
            assert page.locator("[data-ws-shell]").get_attribute("data-user") == original_user
            assert page.locator("[data-ws-input]").input_value() == description
            playwright.expect(page.locator("[data-ws-attachment]")).to_have_count(1)
            assert not errors
        finally:
            browser.close()


def test_reload_restores_file_bytes_but_never_submits_unrequested_draft(live_hub, repo):
    data = seed_workspace(repo, room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(10000)
        requests = []
        page.on("request", lambda request: requests.append(request.url)
                if request.method == "POST" and request.url.endswith("/workspace-dispatch") else None)
        try:
            login_page(page, live_hub)
            before = len(repo.list_tasks(data["project"]["id"]))
            description = "保存草稿并恢复原文件，仍由用户决定何时发送。"
            page.locator("[data-ws-input]").fill(description)
            page.locator("[data-ws-attachment-input]").set_input_files({
                "name": "可恢复的资料.txt", "mimeType": "text/plain", "buffer": REFERENCE,
            })
            playwright.expect(page.locator("[data-ws-attachment]")).to_have_count(1)
            # Allow the explicit persistence result, not an arbitrary long
            # sleep, to be the reload boundary once the local store is ready.
            playwright.expect(page.locator("[data-ws-draft-status]")).to_contain_text("保存在本机")
            page.reload()
            wait_ready(page)
            playwright.expect(page.locator("[data-ws-input]")).to_have_value(description)
            playwright.expect(page.locator("[data-ws-attachment]")).to_have_count(1)
            context.set_offline(True)
            context.set_offline(False)
            playwright.expect(page.locator("[data-ws-sync]")).to_have_text("已同步")
            assert not requests
            assert len(repo.list_tasks(data["project"]["id"])) == before

            page.locator("[data-ws-send]").click()
            playwright.expect(page.locator("[data-ws-input]")).to_have_value("")
            created = [task for task in repo.list_tasks(data["project"]["id"])
                       if task["description"] == description]
            assert len(created) == 1
            files = repo.list_task_attachments(data["project"]["id"], created[0]["id"])
            assert len(files) == 1 and files[0]["sha256"] == hashlib.sha256(REFERENCE).hexdigest()
        finally:
            browser.close()


def test_durable_store_cancel_and_single_claim_across_two_real_windows(live_hub, repo):
    seed_workspace(repo, room_messages=1)
    store_script = Path(__file__).resolve().parents[1] / "codex_hub/static/workspace-store.js"
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context()
        first, second = context.new_page(), context.new_page()
        try:
            first.goto(live_hub + "/login")
            second.goto(first.url)
            for page in (first, second):
                page.add_script_tag(path=str(store_script))
            scope = "review-atomic-claims"
            first.evaluate("""async scope => {
              const file = new File(['exact-file-bytes'], '参考.txt', {type:'text/plain'});
              await CodexWorkspaceStore.enqueue(scope, {key:'review-key-1', description:'review', files:[{id:'file-1', file}]});
            }""", scope)
            # Queue cancellation and a send claim race as real, separate IDB
            # connections. A claim alone must not override cancellation.
            assert first.evaluate("scope => CodexWorkspaceStore.claim(scope, 'review-key-1', 'window-1')", scope)
            assert not second.evaluate("scope => CodexWorkspaceStore.claim(scope, 'review-key-1', 'window-2')", scope)
            assert second.evaluate("scope => CodexWorkspaceStore.change(scope, 'review-key-1', null, {state:'cancelled'}, {cancel:true})", scope)
            assert not first.evaluate("scope => CodexWorkspaceStore.change(scope, 'review-key-1', 'window-1', {state:'sending', attempted:true}, {start:true})", scope)

            # Once a POST may have started, cancellation is no longer allowed.
            first.evaluate("""async scope => {
              const file = new File(['exact-file-bytes'], '参考.txt', {type:'text/plain'});
              await CodexWorkspaceStore.enqueue(scope, {key:'review-key-2', description:'next', files:[{id:'file-2', file}]});
              await CodexWorkspaceStore.claim(scope, 'review-key-2', 'window-1');
              await CodexWorkspaceStore.change(scope, 'review-key-2', 'window-1', {state:'sending', attempted:true}, {start:true});
            }""", scope)
            assert not second.evaluate("scope => CodexWorkspaceStore.change(scope, 'review-key-2', null, {state:'cancelled'}, {cancel:true})", scope)
            recovered = second.evaluate("""async scope => {
              const {job} = await CodexWorkspaceStore.read(scope);
              return {state:job.state, attempted:job.attempted, text:await job.payload.files[0].file.text()};
            }""", scope)
            assert recovered == {"state": "sending", "attempted": True, "text": "exact-file-bytes"}

            # A completed request's late callback must not erase a newer draft
            # saved by another window or its file bodies.
            assert first.evaluate("scope => CodexWorkspaceStore.change(scope, 'review-key-2', 'window-1', {state:'sent'}, {finish:true})", scope)
            assert second.evaluate("""scope => CodexWorkspaceStore.save(scope, {
              key:'review-key-3', description:'new draft from another window',
              files:[{id:'file-3',file:new File(['new-byte-content'],'新的资料.txt',{type:'text/plain'})}]
            })""", scope)
            assert not first.evaluate("scope => CodexWorkspaceStore.change(scope, 'review-key-2', 'window-1', {state:'sent'}, {finish:true})", scope)
            assert not first.evaluate("""scope => CodexWorkspaceStore.save(scope, {
              key:'review-key-2', description:'stale previous draft', files:[]
            })""", scope)
            latest = first.evaluate("""async scope => {
              const {draft} = await CodexWorkspaceStore.read(scope);
              return {key:draft.key, text:await draft.files[0].file.text()};
            }""", scope)
            assert latest == {"key": "review-key-3", "text": "new-byte-content"}
        finally:
            browser.close()


def test_cancelling_offline_queue_preserves_draft_and_never_sends_on_reconnect(live_hub, repo):
    data = seed_workspace(repo, room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(10000)
        posts = []
        page.on("request", lambda request: posts.append(request.url)
                if request.method == "POST" and request.url.endswith("/workspace-dispatch") else None)
        try:
            login_page(page, live_hub)
            before = len(repo.list_tasks(data["project"]["id"]))
            description = "先加入本机队列，再取消；联网后不要自动执行。"
            page.locator("[data-ws-input]").fill(description)
            page.locator("[data-ws-attachment-input]").set_input_files({
                "name": "取消后保留.txt", "mimeType": "text/plain", "buffer": REFERENCE,
            })
            playwright.expect(page.locator("[data-ws-draft-status]")).to_contain_text("保存在本机")
            context.set_offline(True)
            page.locator("[data-ws-send]").click()
            playwright.expect(page.locator("[data-ws-queue-cancel]")).to_be_visible()
            assert not posts
            page.locator("[data-ws-queue-cancel]").click()
            playwright.expect(page.locator("[data-ws-queue-cancel]")).to_be_hidden()
            playwright.expect(page.locator("[data-ws-input]")).to_have_value(description)
            playwright.expect(page.locator("[data-ws-attachment]")).to_have_count(1)
            context.set_offline(False)
            playwright.expect(page.locator("[data-ws-sync]")).to_have_text("已同步")
            page.reload()
            wait_ready(page)
            playwright.expect(page.locator("[data-ws-input]")).to_have_value(description)
            playwright.expect(page.locator("[data-ws-attachment]")).to_have_count(1)
            assert not posts
            assert len(repo.list_tasks(data["project"]["id"])) == before
        finally:
            browser.close()


def test_restored_workspace_refresh_recovers_native_connection_and_other_forms(live_hub, repo):
    data = seed_workspace(repo, room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(10000)
        try:
            login_page(page, live_hub)
            url = page.url
            page.add_init_script("""window.__onlineConfirmCalls=0;
              window.pywebview={api:{workspace_online:async () => {
                window.__onlineConfirmCalls++; return {online:true};
              }}};""")

            def restore_snapshot(route):
                response = route.fetch()
                route.fulfill(response=response, body=offline_document(scrub_credentials(response.text())))

            page.route(url, restore_snapshot, times=1)
            page.reload()
            wait_ready(page)
            # Successful background refresh must reconnect native mutations,
            # even when no chat message is sent to trigger delivery recovery.
            page.wait_for_function("window.__onlineConfirmCalls > 0")
            token = page.locator('[data-ws-dispatch] [name="_csrf"]').input_value()
            assert token
            tokens = page.locator('input[name="_csrf"]').evaluate_all("nodes => nodes.map(node=>node.value)")
            assert tokens and all(value == token for value in tokens)

            # Prove that a different restored form works, rather than merely
            # checking the composer's token or native flag in isolation.
            page.locator("[data-agents-open]").first.click()
            page.locator("[data-agent-create-open]").click()
            create = page.locator("[data-agent-create]")
            playwright.expect(create).to_be_visible()
            create.locator('[name="name"]').fill("离线恢复审核员")
            create.locator('[name="job"]').fill("验证恢复后可以正常创建智能体")
            create.locator('[type="submit"]').click()
            page.wait_for_url("**/agents/*")
            found = repo.connection.execute(
                "SELECT name FROM hub_agents WHERE project_id=? AND name=?",
                (data["project"]["id"], "离线恢复审核员"),
            ).fetchone()
            assert found is not None
        finally:
            browser.close()


def test_queue_from_closed_other_conversation_is_visible_and_resumes_exact_files(live_hub, repo):
    data = seed_workspace(repo, room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context()
        first = context.new_page()
        first.set_default_timeout(10000)
        try:
            login_page(first, live_hub)
            description = "关闭离线窗口后，应从别的对话看见并继续这项工作。"
            first.locator("[data-ws-input]").fill(description)
            first.locator("[data-ws-attachment-input]").set_input_files({
                "name": "另一场工作的资料.txt", "mimeType": "text/plain", "buffer": REFERENCE,
            })
            playwright.expect(first.locator("[data-ws-draft-status]")).to_contain_text("保存在本机")
            context.set_offline(True)
            first.locator("[data-ws-send]").click()
            playwright.expect(first.locator("[data-ws-queue-cancel]")).to_be_visible()
            first.close()
            context.set_offline(False)

            other = context.new_page()
            room = f"{live_hub}/projects/workspace-team/conversations/{data['conversation_id']}"
            other.goto(room)
            wait_ready(other)
            queue = other.locator("[data-ws-other-queues]")
            playwright.expect(queue).to_contain_text("本机还有 1 条待发送工作")
            playwright.expect(queue).to_contain_text(description[:40])
            current_draft = "当前这场对话尚未发送的想法，也必须保留。"
            other.locator("[data-ws-input]").fill(current_draft)
            playwright.expect(other.locator("[data-ws-draft-status]")).to_contain_text("保存在本机")
            queue.get_by_role("link").click()
            other.wait_for_url("**/conversations/*")
            playwright.expect(other.locator("[data-ws-input]")).to_have_value("")
            tasks = [task for task in repo.list_tasks(data["project"]["id"])
                     if task["description"] == description]
            assert len(tasks) == 1
            files = repo.list_task_attachments(data["project"]["id"], tasks[0]["id"])
            assert len(files) == 1 and files[0]["sha256"] == hashlib.sha256(REFERENCE).hexdigest()
            other.goto(room)
            wait_ready(other)
            playwright.expect(other.locator("[data-ws-input]")).to_have_value(current_draft)
            assert not [task for task in repo.list_tasks(data["project"]["id"])
                        if task["description"] == current_draft]
        finally:
            browser.close()


def test_other_windows_queue_and_completion_do_not_replace_newer_draft(live_hub, repo):
    data = seed_workspace(repo, room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context()
        first = context.new_page()
        first.set_default_timeout(10000)
        try:
            login_page(first, live_hub)
            shared_url = first.url
            first_text = "先发送窗口一保留的任务。"
            first.locator("[data-ws-input]").fill(first_text)
            first.locator("[data-ws-attachment-input]").set_input_files({
                "name": "第一份.txt", "mimeType": "text/plain", "buffer": b"first window bytes",
            })
            playwright.expect(first.locator("[data-ws-draft-status]")).to_contain_text("保存在本机")
            second = context.new_page()
            second.goto(shared_url)
            wait_ready(second)
            second_text = "第二窗口刚写的新草稿，不能被另一窗口的队列覆盖。"
            second.locator("[data-ws-input]").fill(second_text)
            second.locator("[data-ws-attachment-remove]").click()
            second.locator("[data-ws-attachment-input]").set_input_files({
                "name": "第二份.txt", "mimeType": "text/plain", "buffer": REFERENCE,
            })
            playwright.expect(second.locator("[data-ws-draft-status]")).to_contain_text("保存在本机")
            context.set_offline(True)
            first.locator("[data-ws-send]").click()
            playwright.expect(first.locator("[data-ws-queue-cancel]")).to_be_visible()
            playwright.expect(second.locator("[data-ws-other-queues]")).to_be_visible()
            playwright.expect(second.locator("[data-ws-input]")).to_have_value(second_text)
            playwright.expect(second.locator("[data-ws-attachment] strong")).to_have_text("第二份.txt")
            context.set_offline(False)
            first.bring_to_front()
            try:
                first.wait_for_url("**/conversations/*")
            except playwright.TimeoutError:
                raise AssertionError({
                    "draft": first.locator("[data-ws-draft-status]").inner_text(),
                    "pending": first.locator("[data-ws-pending]").inner_text(),
                    "attachments": first.locator("[data-ws-attachment]").count(),
                    "error": first.locator("[data-ws-dispatch-error]").inner_text(),
                })
            playwright.expect(second.locator("[data-ws-input]")).to_have_value(second_text)
            playwright.expect(second.locator("[data-ws-attachment] strong")).to_have_text("第二份.txt")
            assert second.url == shared_url
            second.reload()
            wait_ready(second)
            playwright.expect(second.locator("[data-ws-input]")).to_have_value(second_text)
            playwright.expect(second.locator("[data-ws-attachment] strong")).to_have_text("第二份.txt")
            second.locator("[data-ws-send]").click()
            second.wait_for_url("**/conversations/*")
            created = [task for task in repo.list_tasks(data["project"]["id"])
                       if task["description"] == second_text]
            assert len(created) == 1
            files = repo.list_task_attachments(data["project"]["id"], created[0]["id"])
            assert len(files) == 1 and files[0]["sha256"] == hashlib.sha256(REFERENCE).hexdigest()
        finally:
            browser.close()


def test_closing_unchanged_old_window_cannot_overwrite_newer_saved_draft(live_hub, repo):
    data = seed_workspace(repo, room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context()
        first = context.new_page()
        first.set_default_timeout(10000)
        try:
            login_page(first, live_hub)
            first.locator("[data-ws-input]").fill("旧窗口的初始草稿。")
            first.locator("[data-ws-attachment-input]").set_input_files({
                "name": "旧资料.txt", "mimeType": "text/plain", "buffer": b"old bytes",
            })
            playwright.expect(first.locator("[data-ws-draft-status]")).to_contain_text("保存在本机")
            second = context.new_page()
            second.goto(first.url)
            wait_ready(second)
            new_text = "较新窗口已经修改并保存，关掉旧窗口不能覆盖我。"
            second.locator("[data-ws-input]").fill(new_text)
            second.locator("[data-ws-attachment-remove]").click()
            second.locator("[data-ws-attachment-input]").set_input_files({
                "name": "新的资料.txt", "mimeType": "text/plain", "buffer": REFERENCE,
            })
            playwright.expect(second.locator("[data-ws-draft-status]")).to_contain_text("保存在本机")
            # A read-only link used to call saveDraft even though this window's
            # content had not changed. Keep it alive long enough for that write
            # to finish; relying only on an unload could hide the race.
            with first.expect_popup() as popup:
                first.locator("[data-ws-feedback]").first.click()
            popup.value.close()
            # A real navigation dispatches pagehide and permits its queued IDB
            # writes to finish before the second window is reloaded.
            first.goto(live_hub + "/projects?list=1")
            first.close()
            second.reload()
            wait_ready(second)
            playwright.expect(second.locator("[data-ws-input]")).to_have_value(new_text)
            playwright.expect(second.locator("[data-ws-attachment] strong")).to_have_text("新的资料.txt")
            second.locator("[data-ws-send]").click()
            second.wait_for_url("**/conversations/*")
            created = [task for task in repo.list_tasks(data["project"]["id"])
                       if task["description"] == new_text]
            assert len(created) == 1
            files = repo.list_task_attachments(data["project"]["id"], created[0]["id"])
            assert len(files) == 1 and files[0]["sha256"] == hashlib.sha256(REFERENCE).hexdigest()
        finally:
            browser.close()
