"""Synthetic local-only workspace data. No running execution workers."""
from codex_hub.agents import AgentStore
from codex_hub.repository import utc_now


def seed_workspace(repo, *, room_messages=75, legacy_entries=0, attachments=0, engineer_name="工程师"):
    connection = repo.create_device(label="测试执行连接 · 无工作进程")
    project = repo.create_project(slug="workspace-team", name="网页协作工作区", created_by_device_id=connection["id"])
    store = AgentStore(repo.connection)
    controller = store.create(project["id"], "总控", "安排分工与汇总成果", kind="controller", device_id=connection["id"])
    engineer = store.create(project["id"], engineer_name, "负责界面与后端实现", device_id=connection["id"])
    reviewer = store.create(project["id"], "审查员", "检查交付与验收依据", device_id=connection["id"])
    paused = store.create(project["id"], "暂停成员", "暂不接工作", device_id=connection["id"])
    store.update(project["id"], paused["id"], job=paused["job"], instructions="", device_id=connection["id"], active=False)
    tasks = []
    initial = repo.create_task(project["id"], "网页端第一轮统筹", status="in_progress", assigned_agent_id=controller["id"])
    room = store.route(initial["id"], {"action":"new", "title":"网页体验改进", "reason":"围绕同一目标持续协作"}, "codex_cli")
    tasks.append(repo.get_task(initial["id"]))
    for identity, status, title in [
        (engineer, "blocked", "接口连接遇阻"), (reviewer, "review", "交付说明待复核"),
        (engineer, "cancelled", "已取消的试作"), (reviewer, "done", "已完成的小改动"),
    ]:
        task = repo.create_task(project["id"], title, assigned_agent_id=identity["id"])
        store.route(task["id"], {"action":"reuse", "conversation_id":room["conversation_id"], "reason":"属于这场协作"}, "codex_cli")
        repo.update_task_status(task["id"], status, f"{title} · 合成测试记录")
        tasks.append(repo.get_task(task["id"]))
    stamp = utc_now()
    if room_messages:
        entries = [(project["id"], "device", connection["id"], connection["label"], "markdown",
                    f"ROOM-EVENT-{index:04d}\n\n这是第 {index} 条工作进展，包含真实状态和后续安排。", tasks[2]["id"], stamp, stamp)
                   for index in range(room_messages)]
        repo.connection.executemany("""INSERT INTO entries
            (project_id,author_type,author_device_id,source_label,kind,body_markdown,task_id,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""", entries)
    if legacy_entries:
        repo.connection.executemany("""INSERT INTO entries
            (project_id,author_type,author_device_id,source_label,kind,body_markdown,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            [(project["id"],"device",connection["id"],connection["label"],"message",
              f"LEGACY-ARCHIVE-{i:04d} unrelated historical device conversation",stamp,stamp) for i in range(legacy_entries)])
    if attachments:
        repo.connection.executemany("""INSERT INTO attachments
            (project_id,original_filename,stored_relpath,content_type,byte_size,sha256,created_at)
            VALUES (?,?,?,?,?,?,?)""",
            [(project["id"],f"ARCHIVE-FILE-{i:04d}.txt",f"synthetic/{i:04d}.txt","text/plain",1,"0"*64,stamp)
             for i in range(attachments)])
    repo.connection.commit()
    final = repo.create_entry(project_id=project["id"], task_id=tasks[2]["id"], source_label=connection["label"],
        author_device_id=connection["id"], kind="markdown", body_markdown=(
            "## 交付结论\n\n[查看交付](https://example.invalid/output)\n\n- 已验证\n- 等待复核\n\n"
            "[不可信链接](javascript:alert('unsafe'))\n\n<script>window.__unsafeExecuted = true</script>"))
    return {"project":project,"connection":connection,"store":store,"controller":controller,
            "engineer":engineer,"reviewer":reviewer,"paused":paused,"tasks":tasks,
            "conversation_id":room["conversation_id"],"last_entry_id":final["id"]}
