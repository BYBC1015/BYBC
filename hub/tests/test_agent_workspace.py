from codex_hub.agent_workspace import build_agent_workspace
from codex_hub.agents import AgentStore


def test_work_is_counted_once_for_claimant_and_roles_do_not_inflate_team():
    members = [
        {"device_id": 1, "device_label": "A", "permission": "admin", "is_agent_online": True},
        {"device_id": 2, "device_label": "B", "permission": "write", "is_agent_online": False},
        {"device_id": 3, "device_label": "Reader", "permission": "read"},
    ]
    roles = [{"device_id": 1, "name": "设计", "instructions": "检查画面"},
             {"device_id": 1, "name": "审核", "instructions": "检查遗漏"}]
    tasks = [
        {"id": 1, "status": "blocked", "priority": "high", "assigned_device_id": 1, "claimed_by_device_id": 2},
        {"id": 2, "status": "review", "priority": "normal", "assigned_device_id": 1},
        {"id": 3, "status": "open", "priority": "normal", "requested_group_id": 5},
        {"id": 4, "status": "done", "priority": "normal", "assigned_device_id": 1},
        {"id": 5, "status": "cancelled", "priority": "normal", "assigned_device_id": 1},
    ]
    view = build_agent_workspace(members, roles, tasks)
    assert view["total"] == 2
    assert view["online"] == 1
    assert view["observers"] == 1
    assert view["active_tasks"] == 3
    assert view["attention"] == 2
    assert view["unassigned_count"] == 1
    agents = {item["device_id"]: item for item in view["agents"]}
    assert [task["id"] for task in agents[1]["tasks"]] == [2]
    assert [task["id"] for task in agents[2]["tasks"]] == [1]
    assert len(agents[1]["roles"]) == 2
    assert "检查画面" in agents[1]["search_text"]
    assert "遇到阻碍" in agents[2]["task_summary"]


def test_cloud_models_do_not_advertise_bridge_task_execution():
    view = build_agent_workspace([
        {"device_id": 1, "device_label": "Cloud", "permission": "write", "server_ai_provider": "deepseek"},
    ], [], [])
    assert not view["agents"][0]["can_assign_task"]
    assert view["agents"][0]["task_summary"] == "暂无看板任务"


def test_team_overview_is_project_scoped_and_escapes_content(client, repo):
    one = repo.create_device(label="A")
    two = repo.create_device(label="Outside")
    project = repo.create_project(slug="team-safe", name="Team", created_by_device_id=one["id"])
    other = repo.create_project(slug="other-team", name="Other", created_by_device_id=two["id"])
    store = AgentStore(repo.connection)
    store.create(project["id"], "项目总控", "负责分工", kind="controller", device_id=one["id"])
    reviewer = store.create(project["id"], "独立审查员", "交付审查", '<script>alert("role")</script>', device_id=one["id"])
    outsider = store.create(other["id"], "PRIVATE-OTHER-AGENT", "其他项目", device_id=two["id"])
    repo.create_task(project["id"], "<script>task</script>", assigned_agent_id=reviewer["id"])
    repo.create_task(other["id"], "PRIVATE-OTHER-PROJECT", assigned_agent_id=outsider["id"])
    assert client.get("/projects/team-safe?view=archive", follow_redirects=False).status_code == 303
    client.post("/login", data={"username": "owner", "password": "owner-password"})
    response = client.get("/projects/team-safe?view=archive")
    assert response.status_code == 200
    assert "我的智能体团队" in response.text
    assert "独立审查员" in response.text
    assert "PRIVATE-OTHER-AGENT" not in response.text
    assert "PRIVATE-OTHER-PROJECT" not in response.text
    assert "&lt;script&gt;task&lt;/script&gt;" in response.text
    assert '<script>alert("role")</script>' not in response.text
