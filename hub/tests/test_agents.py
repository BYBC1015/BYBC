"""Durable identity and learning regressions, independent of UI/device aliases."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codex_hub.agents import AgentStore, parse_decision
from codex_hub.db import connect_database, initialize_schema
from codex_hub.server_ai import CompletionResult, OpenAICompatibleCompletionClient


@pytest.fixture
def team(repo):
    device = repo.create_device(label="Shared executor")
    spare = repo.create_device(label="Second executor")
    project = repo.create_project(slug="durable-team", name="Durable team", created_by_device_id=device["id"])
    repo.add_project_member(project["id"], spare["id"], "write")
    store = AgentStore(repo.connection)
    controller = store.create(project["id"], "总控", "分工和验收", kind="controller", device_id=device["id"])
    designer = store.create(project["id"], "设计师", "界面设计", "先明确用户流程", device_id=device["id"])
    reviewer = store.create(project["id"], "审查员", "检查缺陷", device_id=device["id"])
    return project, device, spare, store, controller, designer, reviewer


def new_room(store, task, title="设计评审"):
    return store.route(task["id"], {"action": "new", "title": title, "reason": "新目标需要独立上下文"}, "codex_cli")


def test_multiple_identities_share_one_device_without_sharing_experience(repo, team):
    project, device, _, store, controller, designer, reviewer = team
    task = repo.create_task(project["id"], "设计卡片", assigned_agent_id=designer["id"])
    assert task["assigned_device_id"] == device["id"]
    assert task["assigned_agent_id"] == designer["id"]
    repo.update_task_status(task["id"], "review", "设计完成，待复核", reflection={
        "summary": "交付卡片原型", "worked": "先验证阅读顺序", "failed": "初稿信息拥挤",
        "next_time": "在小尺寸上先检查长文案",
    })
    assert len(store.list(project["id"])) == 3
    experience = store.experiences(project["id"], designer["id"])[0]
    assert experience["task_id"] == task["id"]
    assert experience["worked"] == "先验证阅读顺序"
    assert experience["verification"] == "needs_review"
    assert store.experiences(project["id"], reviewer["id"]) == []
    management = store.experiences(project["id"], controller["id"])
    assert len(management) == 1
    assert "设计师" in management[0]["summary"]
    assert management[0]["next_time"] == "在小尺寸上先检查长文案"


def test_conversations_are_shared_workplaces_and_not_identities(repo, team):
    project, _, _, store, _, designer, reviewer = team
    first = repo.create_task(project["id"], "设计卡片", assigned_agent_id=designer["id"])
    room = new_room(store, first)
    review = repo.create_task(project["id"], "审查卡片", assigned_agent_id=reviewer["id"])
    joined = store.route(review["id"], {"action": "reuse", "conversation_id": room["conversation_id"],
                                       "reason": "审查同一成果，需要设计上下文"}, "codex_cli")
    second = repo.create_task(project["id"], "设计另一个项目页面", assigned_agent_id=designer["id"])
    separate = new_room(store, second, "另一页面")
    assert joined["conversation_id"] == room["conversation_id"]
    assert separate["conversation_id"] != room["conversation_id"]
    assert joined["assigned_agent_id"] != first["assigned_agent_id"]
    agents = {agent["id"]: agent for agent in store.list(project["id"])}
    assert agents[designer["id"]]["conversation_count"] == 2
    assert agents[reviewer["id"]]["conversation_count"] == 1
    assert len(store.list(project["id"])) == 3


def test_route_retries_do_not_create_more_rooms_or_change_committed_choice(repo, team):
    project, _, _, store, _, designer, _ = team
    task = repo.create_task(project["id"], "开发导航", assigned_agent_id=designer["id"])
    first = new_room(store, task)
    retry = store.route(task["id"], {"action": "new", "title": "重试的标题", "reason": "请求重试"}, "deepseek")
    assert retry["conversation_id"] == first["conversation_id"]
    assert retry["route_engine"] == "codex_cli"
    assert len(store.conversations(project["id"])) == 1


def test_project_boundary_applies_to_identity_memory_assignment_and_rooms(repo, team):
    project, device, _, store, _, designer, _ = team
    other = repo.create_project(slug="unrelated-team", name="Other", created_by_device_id=device["id"])
    outsider = store.create(other["id"], "另一项目设计师", "其他目标", device_id=device["id"])
    foreign_task = repo.create_task(other["id"], "PRIVATE OTHER WORK", assigned_agent_id=outsider["id"])
    foreign_room = new_room(store, foreign_task)
    task = repo.create_task(project["id"], "当前目标", assigned_agent_id=designer["id"])
    with pytest.raises(ValueError):
        store.get(project["id"], outsider["id"])
    with pytest.raises(ValueError):
        store.experiences(project["id"], outsider["id"])
    with pytest.raises(ValueError):
        repo.create_task(project["id"], "越界指派", assigned_agent_id=outsider["id"])
    with pytest.raises(ValueError):
        store.route(task["id"], {"action": "reuse", "conversation_id": foreign_room["conversation_id"],
                                 "reason": "不能越界"}, "codex_cli")
    assert repo.get_task(task["id"])["conversation_id"] is None
    assert "PRIVATE OTHER WORK" not in json.dumps(store.context(task))
    assert store.conversations(project["id"]) == []


def test_assignment_requires_active_identity_and_valid_execution_connection(repo, team):
    project, device, _, store, _, designer, _ = team
    unbound = store.create(project["id"], "待配置", "资料整理")
    with pytest.raises(ValueError):
        repo.create_task(project["id"], "没有连接", assigned_agent_id=unbound["id"])
    store.update(project["id"], designer["id"], job=designer["job"], instructions="",
                 device_id=device["id"], active=False)
    with pytest.raises(ValueError):
        repo.create_task(project["id"], "暂停的成员", assigned_agent_id=designer["id"])
    assert repo.list_tasks(project["id"]) == []


@pytest.mark.parametrize("connection_type", ["read_only", "outside", "cloud", "disabled"])
def test_invalid_device_cannot_become_an_agent_execution_connection(repo, team, connection_type):
    project, _, _, store, _, _, _ = team
    device = repo.create_device(label=f"Invalid {connection_type}",
                                server_ai_provider="deepseek" if connection_type == "cloud" else "")
    if connection_type != "outside":
        repo.add_project_member(project["id"], device["id"], "read" if connection_type == "read_only" else "write")
    if connection_type == "disabled":
        repo.connection.execute("UPDATE devices SET is_active=0 WHERE id=?", (device["id"],))
        repo.connection.commit()
    with pytest.raises(ValueError):
        store.create(project["id"], "无效执行连接", "测试", device_id=device["id"])


def test_rebinding_does_not_reassign_in_flight_work_or_change_identity(repo, team):
    project, device, spare, store, _, designer, _ = team
    original = repo.create_task(project["id"], "已排队工作", assigned_agent_id=designer["id"])
    updated = store.update(project["id"], designer["id"], job="界面与交互设计", instructions="关注状态反馈",
                           device_id=spare["id"], active=True)
    new = repo.create_task(project["id"], "下一项工作", assigned_agent_id=designer["id"])
    assert updated["id"] == designer["id"]
    assert updated["revision"] == designer["revision"] + 1
    assert repo.get_task(original["id"])["assigned_device_id"] == device["id"]
    assert new["assigned_device_id"] == spare["id"]
    assert repo.claim_task(original["id"], spare["id"]) is None
    assert repo.claim_task(original["id"], device["id"])["claimed_by_device_id"] == device["id"]


def test_pausing_prevents_claiming_queued_work(repo, team):
    project, device, _, store, _, designer, _ = team
    task = repo.create_task(project["id"], "暂停后不能开始", assigned_agent_id=designer["id"])
    store.update(project["id"], designer["id"], job=designer["job"], instructions="",
                 device_id=device["id"], active=False)
    assert repo.claim_task(task["id"], device["id"]) is None
    assert repo.get_task(task["id"])["status"] == "open"


def test_outcome_retries_are_idempotent_and_revisions_preserve_corrections(repo, team):
    project, _, _, store, controller, designer, _ = team
    task = repo.create_task(project["id"], "状态流程", assigned_agent_id=designer["id"])
    reflection = {"summary": "遇到服务故障", "failed": "服务连接中断", "next_time": "恢复后验证连接"}
    repo.update_task_status(task["id"], "blocked", "遇到服务故障", reflection)
    repo.update_task_status(task["id"], "blocked", "遇到服务故障", reflection)
    assert len(store.experiences(project["id"], designer["id"], history=True)) == 1
    assert len(store.experiences(project["id"], controller["id"], history=True)) == 1
    repo.update_task_status(task["id"], "review", "已恢复并交付", {
        "summary": "已恢复并交付", "worked": "重连后先运行健康检查",
        "failed": "首次连接失败", "next_time": "启动任务前检查连接",
    })
    repo.update_task_status(task["id"], "done", "成果通过验收")
    history = store.experiences(project["id"], designer["id"], history=True)
    assert [item["revision"] for item in history] == [3, 2, 1]
    assert [item["active"] for item in history] == [1, 0, 0]
    assert history[0]["supersedes_id"] == history[1]["id"]
    assert history[0]["worked"] == "重连后先运行健康检查"
    assert history[0]["verification"] == "accepted"
    assert history[-1]["failed"] == "服务连接中断"


def test_restore_creates_traceable_revision_without_erasing_history(repo, team):
    project, _, _, store, _, designer, reviewer = team
    task = repo.create_task(project["id"], "版本恢复", assigned_agent_id=designer["id"])
    repo.update_task_status(task["id"], "review", "初次总结", {"next_time": "先验证 A"})
    first = store.experiences(project["id"], designer["id"])[0]
    repo.update_task_status(task["id"], "review", "修订总结", {"next_time": "先验证 B"})
    second = store.experiences(project["id"], designer["id"])[0]
    with pytest.raises(ValueError):
        store.restore_experience(project["id"], reviewer["id"], first["id"])
    store.restore_experience(project["id"], designer["id"], first["id"])
    history = store.experiences(project["id"], designer["id"], history=True)
    assert len(history) == 3
    assert history[0]["revision"] == 3
    assert history[0]["next_time"] == "先验证 A"
    assert history[0]["supersedes_id"] == second["id"]
    assert history[2]["id"] == first["id"]
    assert repo.get_task(task["id"])["result_summary"] == "修订总结"


def test_invalid_reflection_rolls_back_status_and_learning_together(repo, team):
    project, _, _, store, controller, designer, _ = team
    task = repo.create_task(project["id"], "事务一致性", assigned_agent_id=designer["id"])
    with pytest.raises(ValueError):
        repo.update_task_status(task["id"], "done", "不应保存", {"worked": ["错误数据类型"]})
    unchanged = repo.get_task(task["id"])
    assert unchanged["status"] == "open"
    assert unchanged["result_summary"] == ""
    assert store.experiences(project["id"], designer["id"]) == []
    assert store.experiences(project["id"], controller["id"]) == []


def test_controller_learns_from_own_tasks_without_duplicate_observation(repo, team):
    project, _, _, store, controller, _, _ = team
    task = repo.create_task(project["id"], "优化分工", assigned_agent_id=controller["id"])
    repo.update_task_status(task["id"], "done", "完成分工评估", {"next_time": "按专业能力分配工作"})
    lessons = store.experiences(project["id"], controller["id"], history=True)
    assert len(lessons) == 1
    assert lessons[0]["next_time"] == "按专业能力分配工作"


def test_prompt_uses_identity_experience_and_shared_room_history(repo, team):
    project, _, _, store, _, designer, reviewer = team
    earlier = repo.create_task(project["id"], "旧版卡片", assigned_agent_id=designer["id"])
    room = new_room(store, earlier)
    repo.update_task_status(earlier["id"], "done", "阅读顺序清晰", {"next_time": "先检查长文案"})
    task = repo.create_task(project["id"], "新版卡片", assigned_agent_id=designer["id"])
    store.route(task["id"], {"action": "reuse", "conversation_id": room["conversation_id"], "reason": "继续改进同一设计"}, "codex_cli")
    review = repo.create_task(project["id"], "独立审查", assigned_agent_id=reviewer["id"])
    prompt = repo.build_task_prompt_markdown(project["slug"], task["id"])
    assert "设计师" in prompt
    assert "先明确用户流程" in prompt
    assert "先检查长文案" in prompt
    assert "旧版卡片" in prompt
    assert "<hub-reflection>" in prompt
    assert "先检查长文案" not in repo.build_task_prompt_markdown(project["slug"], review["id"])


def test_additive_schema_and_legacy_tasks_remain_compatible(repo, team):
    project, device, _, store, _, _, _ = team
    task = repo.create_task(project["id"], "旧任务", assigned_device_id=device["id"])
    initialize_schema(repo.connection)
    initialize_schema(repo.connection)
    assert repo.get_task(task["id"])["assigned_agent_id"] is None
    assert store.context(task) == {}
    assert store.prompt(task) == ""
    assert repo.claim_task(task["id"], device["id"])
    repo.update_task_status(task["id"], "done", "旧流程完成")
    assert repo.get_task(task["id"])["result_summary"] == "旧流程完成"
    assert repo.connection.execute("SELECT COUNT(*) FROM hub_agent_experiences").fetchone()[0] == 0


@pytest.mark.parametrize("payload", [
    [], {"action": "delete", "reason": "no"}, {"action": "reuse", "conversation_id": True, "reason": "no"},
    {"action": "new", "reason": ""}, {"action": "new", "reason": ["invalid"]},
])
def test_decision_validation_rejects_ambiguous_or_unsafe_shapes(payload):
    with pytest.raises(ValueError):
        parse_decision(json.dumps(payload))


def test_decision_parser_accepts_plain_or_fenced_json():
    value = '{"action":"new","title":"新目标","reason":"新的工作范围"}'
    assert parse_decision(value) == parse_decision("```json\n" + value + "\n```")


def token_headers(repo, device):
    return {"Authorization": "Bearer " + repo.create_api_token(device["id"], "agent-tests").raw}


def test_agent_api_requires_auth_and_management_privileges(client, repo, team):
    project, owner, worker, store, _, _, _ = team
    repo.set_project_primary_device(project["id"], owner["id"])
    primary_headers = token_headers(repo, owner)
    worker_headers = token_headers(repo, worker)
    outside = repo.create_device(label="Outside project")
    outside_headers = token_headers(repo, outside)
    path = f"/api/projects/{project['slug']}/agents"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=outside_headers).status_code == 403
    assert client.get(path, headers=worker_headers).status_code == 200
    payload = {"name": "API 创建的执行者", "job": "检查交付物", "device_id": worker["id"]}
    assert client.post(path, headers=worker_headers, json=payload).status_code == 403
    created = client.post(path, headers=primary_headers, json=payload)
    assert created.status_code == 201
    assert created.json()["agent"]["primary_engine"] == "codex_cli"
    assert created.json()["agent"]["fallback_engine"] == "deepseek"
    admin = repo.create_device(label="Project administrator")
    repo.add_project_member(project["id"], admin["id"], "admin")
    payload["name"] = "管理员创建的执行者"
    assert client.post(path, headers=token_headers(repo, admin), json=payload).status_code == 201
    assert len(store.list(project["id"])) == 5


def test_agent_task_api_keeps_identity_and_protects_runtime_mutations(client, repo, team):
    project, primary, worker, store, _, _, _ = team
    repo.set_project_primary_device(project["id"], primary["id"])
    actor = store.create(project["id"], "API 专员", "实现任务", device_id=worker["id"])
    primary_headers, worker_headers = token_headers(repo, primary), token_headers(repo, worker)
    unrelated = repo.create_device(label="Unrelated writer")
    repo.add_project_member(project["id"], unrelated["id"], "write")
    unrelated_headers = token_headers(repo, unrelated)
    base = f"/api/projects/{project['slug']}/tasks"
    created = client.post(base, headers=primary_headers, json={"title": "检查自动化", "assigned_agent_id": actor["id"]})
    assert created.status_code == 201
    task = created.json()["task"]
    assert task["assigned_agent_id"] == actor["id"]
    assert task["assigned_device_id"] == worker["id"]
    path = f"{base}/{task['id']}"
    for headers in (primary_headers, unrelated_headers):
        assert client.get(path + "/agent-context", headers=headers).status_code == 403
        assert client.post(path + "/agent-route", headers=headers, json={"decision": {
            "action": "new", "title": "越权决策", "reason": "不能代替执行者选上下文"}}).status_code == 403
    assert client.get(path + "/agent-context", headers=worker_headers).json()["agent"]["id"] == actor["id"]
    routed = client.post(path + "/agent-route", headers=worker_headers, json={"decision": {
        "action": "new", "title": "自动化验收", "reason": "独立验收工作"}, "engine": "codex_cli"})
    assert routed.status_code == 200
    assert routed.json()["task"]["conversation_id"] is not None
    assert client.post(path + "/status", headers=unrelated_headers, json={"status": "done"}).status_code == 403
    result = client.post(path + "/status", headers=worker_headers, json={"status": "review",
        "result_summary": "已交付", "reflection": {"summary": "已交付", "worked": "先验证最短流程", "next_time": "复用验收步骤"}})
    assert result.status_code == 200
    assert store.experiences(project["id"], actor["id"])[0]["worked"] == "先验证最短流程"
    assert client.post(path + "/status", headers=primary_headers, json={"status": "done", "result_summary": "验收完成"}).status_code == 200
    assert client.get(path + "/agent-context", headers=worker_headers).status_code == 409


def test_agent_experiences_and_prompt_are_scoped_to_bound_or_managing_devices(client, repo, team):
    project, primary, worker, store, _, _, _ = team
    repo.set_project_primary_device(project["id"], primary["id"])
    actor = store.create(project["id"], "私有经验专员", "执行工作", device_id=worker["id"])
    task = repo.create_task(project["id"], "经验来源", assigned_agent_id=actor["id"])
    repo.update_task_status(task["id"], "review", "工作结果", {"worked": "PRIVATE-AGENT-LESSON"})
    reader = repo.create_device(label="Project reader")
    repo.add_project_member(project["id"], reader["id"], "read")
    reader_headers = token_headers(repo, reader)
    base = f"/api/projects/{project['slug']}"
    memory_path = f"{base}/agents/{actor['id']}/experiences"
    assert client.get(memory_path, headers=reader_headers).status_code == 403
    assert client.get(memory_path, headers=token_headers(repo, worker)).status_code == 200
    assert client.get(memory_path, headers=token_headers(repo, primary)).status_code == 200
    prompt_path = f"{base}/tasks/{task['id']}/prompt.md"
    assert client.get(prompt_path, headers=reader_headers).status_code == 403


def test_web_agent_creation_and_edits_require_csrf(client, repo, team):
    project, device, _, store, _, _, _ = team
    base = f"/projects/{project['slug']}/agents"
    payload = {"name": "网页创建的成员", "job": "整理资料", "device_id": str(device["id"])}
    assert client.post(base, data=payload, follow_redirects=False).status_code == 401
    assert client.post("/login", data={"username": "owner", "password": "owner-password"}, follow_redirects=False).status_code == 303
    assert client.post(base, data=payload, follow_redirects=False).status_code == 403
    csrf = client.cookies.get("codex_hub_csrf")
    created = client.post(base, data={**payload, "_csrf": csrf}, follow_redirects=False)
    assert created.status_code == 303
    actor = next(a for a in store.list(project["id"]) if a["name"] == payload["name"])
    assert created.headers["location"].endswith(f"/agents/{actor['id']}")
    updated = {"job": "更新后的职责", "instructions": "记录经验", "device_id": str(device["id"]), "active": "1"}
    assert client.post(base + f"/{actor['id']}", data=updated, follow_redirects=False).status_code == 403
    assert client.post(base + f"/{actor['id']}", data={**updated, "_csrf": csrf}, follow_redirects=False).status_code == 303
    assert store.get(project["id"], actor["id"])["job"] == "更新后的职责"


def test_api_rejects_foreign_agent_assignment_and_foreign_runtime_task(client, repo, team):
    project, device, _, store, _, designer, _ = team
    headers = token_headers(repo, device)
    other = repo.create_project(slug="api-other-team", name="Other", created_by_device_id=device["id"])
    outsider = store.create(other["id"], "另一团队成员", "独立工作", device_id=device["id"])
    foreign_task = repo.create_task(other["id"], "另一团队任务", assigned_agent_id=outsider["id"])
    base = f"/api/projects/{project['slug']}"
    assert client.post(base + "/tasks", headers=headers, json={"title": "越界任务", "assigned_agent_id": outsider["id"]}).status_code == 422
    assert client.get(base + f"/tasks/{foreign_task['id']}/agent-context", headers=headers).status_code == 404
    assert client.get(base + f"/agents/{outsider['id']}/experiences", headers=headers).status_code == 422
    assert store.experiences(project["id"], designer["id"]) == []


def test_competing_route_requests_commit_one_room(repo, team):
    project, _, _, store, _, designer, _ = team
    task = repo.create_task(project["id"], "同时到达的决策", assigned_agent_id=designer["id"])
    database_path = Path(repo.connection.execute("PRAGMA database_list").fetchone()["file"])
    barrier = threading.Barrier(2)

    def route(title):
        connection = connect_database(database_path)
        try:
            barrier.wait(timeout=5)
            return AgentStore(connection).route(task["id"], {
                "action": "new", "title": title, "reason": "并发重试也只应创建一次",
            }, "codex_cli")
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(route, ["候选一", "候选二"]))
    assert results[0]["conversation_id"] == results[1]["conversation_id"]
    assert len(store.conversations(project["id"])) == 1


@pytest.mark.parametrize("reflection", [[], "", 0, False])
def test_falsey_nonobject_reflection_is_rejected_without_status_change(repo, team, reflection):
    project, _, _, store, _, designer, _ = team
    task = repo.create_task(project["id"], "复盘格式检查", assigned_agent_id=designer["id"])
    with pytest.raises(ValueError):
        repo.update_task_status(task["id"], "review", "不能保存", reflection)
    assert repo.get_task(task["id"])["status"] == "open"
    assert store.experiences(project["id"], designer["id"]) == []


def test_memory_payloads_are_bounded_and_missing_reflection_still_records_result(repo, team):
    project, _, _, store, controller, designer, _ = team
    task = repo.create_task(project["id"], "超长复盘", assigned_agent_id=designer["id"])
    repo.update_task_status(task["id"], "review", "结果", {
        "summary": "总" * 10000, "worked": "好" * 10000,
        "failed": "错" * 10000, "next_time": "改" * 10000,
    })
    lesson = store.experiences(project["id"], designer["id"])[0]
    assert len(lesson["summary"]) == 2400
    assert all(len(lesson[key]) == 1600 for key in ("worked", "failed", "next_time"))
    assert len(store.experiences(project["id"], controller["id"])[0]["summary"]) <= 2400
    missing = repo.create_task(project["id"], "没有模型复盘", assigned_agent_id=designer["id"])
    repo.update_task_status(missing["id"], "blocked", "执行器退出，未完成复盘")
    observed = store.experiences(project["id"], designer["id"])[0]
    assert observed["summary"] == "执行器退出，未完成复盘"
    assert observed["verification"] == "needs_review"
    assert observed["worked"] == ""


def test_closed_room_is_rejected_without_mutating_task(repo, team):
    project, _, _, store, _, designer, _ = team
    first = repo.create_task(project["id"], "旧目标", assigned_agent_id=designer["id"])
    room = new_room(store, first)
    repo.connection.execute("UPDATE hub_conversations SET status='closed' WHERE id=?", (room["conversation_id"],))
    repo.connection.commit()
    task = repo.create_task(project["id"], "新目标", assigned_agent_id=designer["id"])
    with pytest.raises(ValueError):
        store.route(task["id"], {"action": "reuse", "conversation_id": room["conversation_id"], "reason": "已关闭不能加入"}, "codex_cli")
    assert repo.get_task(task["id"])["conversation_id"] is None
    assert store.context(repo.get_task(task["id"]))["conversations"] == []


def dispatch_counts(repo):
    return {table: repo.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "hub_conversations", "hub_conversation_agents",
                          "hub_agent_dispatches", "hub_dispatch_tasks")}


def test_group_dispatch_keeps_shared_device_participants_distinct_and_retries_idempotently(repo, team):
    project, device, _, store, _, designer, reviewer = team
    payload = dict(agent_ids=[designer["id"], reviewer["id"]], title="联合设计评审",
                   description="分别完成设计检查和质量检查", request_key="group-request-001")
    first = store.dispatch(repo, project["id"], **payload)
    before = dispatch_counts(repo)
    retry = store.dispatch(repo, project["id"], **{**payload, "agent_ids": [reviewer["id"], designer["id"], designer["id"]]})
    assert dispatch_counts(repo) == before
    assert first["conversation_id"] == retry["conversation_id"]
    assert [t["id"] for t in first["tasks"]] == [t["id"] for t in retry["tasks"]]
    assert {t["assigned_agent_id"] for t in first["tasks"]} == {designer["id"], reviewer["id"]}
    assert {t["assigned_device_id"] for t in first["tasks"]} == {device["id"]}
    assert {t["conversation_id"] for t in first["tasks"]} == {first["conversation_id"]}
    assert before["hub_conversation_agents"] == 2
    with pytest.raises(ValueError):
        store.dispatch(repo, project["id"], **{**payload, "description": "同key不能换任务"})
    assert dispatch_counts(repo) == before


@pytest.mark.parametrize("invalid", ["foreign", "paused", "missing", "priority"])
def test_group_dispatch_validation_is_atomic(repo, team, invalid):
    project, device, _, store, _, designer, reviewer = team
    target = reviewer["id"]
    if invalid == "foreign":
        other = repo.create_project(slug="dispatch-other", name="Other", created_by_device_id=device["id"])
        target = store.create(other["id"], "其他组员", "不在当前项目", device_id=device["id"])["id"]
    elif invalid == "paused":
        store.update(project["id"], reviewer["id"], job=reviewer["job"], instructions="",
                     device_id=device["id"], active=False)
    elif invalid == "missing":
        target = 999999
    before = dispatch_counts(repo)
    with pytest.raises(ValueError):
        store.dispatch(repo, project["id"], agent_ids=[designer["id"], target], title="必须全部有效",
                       description="一项失败不能留下半组工作", request_key="atomic-validation-001",
                       priority="INVALID" if invalid == "priority" else "normal")
    assert dispatch_counts(repo) == before


def test_group_dispatch_rolls_back_when_second_task_insert_fails(repo, team, monkeypatch):
    project, _, _, store, _, designer, reviewer = team
    original = repo.create_task
    attempts = []

    def fail_second(*args, **kwargs):
        attempts.append(kwargs["assigned_agent_id"])
        if len(attempts) == 2:
            raise ValueError("simulated second assignment failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(repo, "create_task", fail_second)
    before = dispatch_counts(repo)
    with pytest.raises(ValueError, match="second assignment"):
        store.dispatch(repo, project["id"], agent_ids=[designer["id"], reviewer["id"]], title="原子分发",
                       description="测试中途失败", request_key="atomic-insertion-001")
    assert len(attempts) == 2
    assert dispatch_counts(repo) == before


def test_controller_delegation_inherits_room_and_rejects_other_origins(client, repo, team):
    project, device, spare, store, controller, designer, reviewer = team
    headers = token_headers(repo, device)
    origin = repo.create_task(project["id"], "总控统筹任务", assigned_agent_id=controller["id"])
    base = f"/api/projects/{project['slug']}/tasks"
    endpoint = f"{base}/{origin['id']}/delegate"
    payload = {"assigned_agent_id": designer["id"], "title": "完成界面", "description": "负责视觉和交互",
               "request_key": "delegate-ui-001"}
    assert client.post(endpoint, headers=headers, json=payload).status_code == 409
    room = new_room(store, origin)
    assert client.post(endpoint, headers=token_headers(repo, spare), json=payload).status_code == 403
    created = client.post(endpoint, headers=headers, json=payload)
    assert created.status_code == 201
    task = created.json()["task"]
    assert task["assigned_agent_id"] == designer["id"]
    assert task["assigned_device_id"] == device["id"]
    assert task["conversation_id"] == room["conversation_id"]
    assert task["created_by_device_id"] == device["id"]
    retried = client.post(endpoint, headers=headers, json=payload)
    assert retried.status_code == 201
    assert retried.json()["task"]["id"] == task["id"]
    assert client.post(endpoint, headers=headers, json={**payload, "title": "不同内容"}).status_code == 422
    assert client.post(endpoint, headers=headers, json={**payload, "request_key": "delegate-self-001",
                                                       "assigned_agent_id": controller["id"]}).status_code == 422
    assert client.post(endpoint, headers=headers, json={**payload, "request_key": "short"}).status_code == 422
    worker_origin = repo.create_task(project["id"], "非总控工作", assigned_agent_id=designer["id"])
    store.route(worker_origin["id"], {"action": "reuse", "conversation_id": room["conversation_id"], "reason": "同场协作"}, "codex_cli")
    assert client.post(f"{base}/{worker_origin['id']}/delegate", headers=headers,
                       json={**payload, "assigned_agent_id": reviewer["id"]}).status_code == 403


def test_controller_cannot_delegate_to_paused_identity(client, repo, team):
    project, device, _, store, controller, designer, _ = team
    origin = repo.create_task(project["id"], "统筹", assigned_agent_id=controller["id"])
    new_room(store, origin)
    store.update(project["id"], designer["id"], job=designer["job"], instructions="",
                 device_id=device["id"], active=False)
    before = dispatch_counts(repo)
    response = client.post(f"/api/projects/{project['slug']}/tasks/{origin['id']}/delegate",
        headers=token_headers(repo, device), json={"assigned_agent_id": designer["id"], "title": "暂停期间不执行",
                                               "request_key": "paused-delegation-001"})
    assert response.status_code == 422
    assert dispatch_counts(repo) == before


def test_relevant_experience_retrieval_prefers_older_matching_lessons_without_crossing_identity(repo, team):
    project, _, _, store, _, designer, reviewer = team
    old = repo.create_task(project["id"], "早期专项", assigned_agent_id=designer["id"])
    repo.update_task_status(old["id"], "done", "pagination_cursor 可避免重复内容", {"next_time": "分页时沿用 pagination_cursor"})
    for number in range(10):
        recent = repo.create_task(project["id"], f"普通任务 {number}", assigned_agent_id=designer["id"])
        repo.update_task_status(recent["id"], "review", f"色彩检查完成 {number}")
    other = repo.create_task(project["id"], "其他人的分页经验", assigned_agent_id=reviewer["id"])
    repo.update_task_status(other["id"], "done", "pagination_cursor PRIVATE_REVIEWER_ONLY")
    current = repo.create_task(project["id"], "优化 pagination_cursor", assigned_agent_id=designer["id"])
    lessons = store.context(current)["experiences"]
    assert lessons[0]["task_id"] == old["id"]
    assert len(lessons) <= 8
    assert {lesson["agent_id"] for lesson in lessons} == {designer["id"]}
    assert "PRIVATE_REVIEWER_ONLY" not in json.dumps(lessons)
    assert sum(len(json.dumps(lesson, ensure_ascii=False)) for lesson in lessons) <= 16000


def test_web_group_dispatch_requires_csrf_and_reuses_existing_conversation(client, repo, team):
    project, _, _, store, _, designer, reviewer = team
    assert client.post("/login", data={"username": "owner", "password": "owner-password"}, follow_redirects=False).status_code == 303
    csrf = client.cookies.get("codex_hub_csrf")
    base = f"/projects/{project['slug']}"
    payload = {"agent_ids": [str(designer["id"]), str(reviewer["id"])], "title": "两位共同处理",
               "description": "先提出各自方案", "request_key": "web-discussion-001"}
    assert client.post(base + "/agent-discussions", data=payload, follow_redirects=False).status_code == 403
    created = client.post(base + "/agent-discussions", data={**payload, "_csrf": csrf}, follow_redirects=False)
    assert created.status_code == 303
    room_id = int(created.headers["location"].rsplit("/", 1)[-1])
    assert len(repo.list_tasks(project["id"])) == 2
    followup = {"agent_ids": str(designer["id"]), "title": "继续整理方案", "description": "基于两方意见继续",
                "request_key": "web-discussion-002", "_csrf": csrf}
    response = client.post(base + f"/conversations/{room_id}/dispatch", data=followup, follow_redirects=False)
    assert response.status_code == 303
    assert {task["conversation_id"] for task in repo.list_tasks(project["id"])} == {room_id}
    assert len(store.conversations(project["id"])) == 1


def test_deepseek_fallback_requires_configuration_and_commits_one_model_decision(client, repo, team, settings, monkeypatch):
    project, device, _, store, _, designer, _ = team
    task = repo.create_task(project["id"], "备用决策测试", assigned_agent_id=designer["id"])
    path = f"/api/projects/{project['slug']}/tasks/{task['id']}/agent-route/fallback"
    headers = token_headers(repo, device)
    calls = []

    def complete(self, messages):
        calls.append(messages)
        assert "备用决策" in messages[0]["content"]
        return CompletionResult(content='{"action":"new","title":"备用引擎选择的对话","reason":"新的工作目标"}',
                                model="mock-deepseek", usage={})

    monkeypatch.setattr(OpenAICompatibleCompletionClient, "complete", complete)
    assert client.post(path, headers=headers, json={}).status_code == 503
    assert calls == []
    assert repo.get_task(task["id"])["conversation_id"] is None
    monkeypatch.setattr(settings, "server_ai_enabled", True)
    monkeypatch.setattr(settings, "deepseek_api_key", "test-only-not-a-real-key")
    first = client.post(path, headers=headers, json={})
    assert first.status_code == 200
    assert first.json()["task"]["route_engine"] == "deepseek"
    assert len(calls) == 1
    retry = client.post(path, headers=headers, json={})
    assert retry.status_code == 200
    assert retry.json()["task"]["conversation_id"] == first.json()["task"]["conversation_id"]
    assert len(calls) == 1
    assert len(store.conversations(project["id"])) == 1


@pytest.mark.parametrize("result_kind", ["invalid_json", "foreign_conversation"])
def test_deepseek_invalid_or_foreign_decision_never_commits_a_wrong_conversation(client, repo, team, settings, monkeypatch, result_kind):
    project, device, _, store, _, designer, _ = team
    other = repo.create_project(slug="fallback-other", name="Other", created_by_device_id=device["id"])
    outsider = store.create(other["id"], "外部执行者", "其他工作", device_id=device["id"])
    foreign_task = repo.create_task(other["id"], "其他任务", assigned_agent_id=outsider["id"])
    room = new_room(store, foreign_task)
    task = repo.create_task(project["id"], "不能越界的备用决策", assigned_agent_id=designer["id"])
    content = "not JSON" if result_kind == "invalid_json" else json.dumps({
        "action": "reuse", "conversation_id": room["conversation_id"], "reason": "模型提供了错误的项目对话编号",
    })
    monkeypatch.setattr(settings, "server_ai_enabled", True)
    monkeypatch.setattr(settings, "deepseek_api_key", "test-only-not-a-real-key")
    monkeypatch.setattr(OpenAICompatibleCompletionClient, "complete",
                        lambda self, messages: CompletionResult(content=content, model="mock-deepseek", usage={}))
    before = dispatch_counts(repo)
    response = client.post(f"/api/projects/{project['slug']}/tasks/{task['id']}/agent-route/fallback",
                           headers=token_headers(repo, device), json={})
    assert response.status_code == (503 if result_kind == "invalid_json" else 422)
    assert repo.get_task(task["id"])["conversation_id"] is None
    assert repo.get_task(task["id"])["route_engine"] == ""
    assert dispatch_counts(repo) == before
