"""Project-scoped team overview built from existing membership and task facts."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


TASK_LABELS = {
    "open": "待领取", "claimed": "已领取", "in_progress": "进行中",
    "blocked": "遇到阻碍", "review": "待复核",
}
TASK_ORDER = {"blocked": 0, "review": 1, "in_progress": 2, "claimed": 3, "open": 4}


def build_agent_workspace(
    memberships: list[dict[str, Any]],
    roles: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Roles share a device; claimed work belongs to its actual claimant once."""
    members = {member["device_id"]: member for member in memberships
               if member["permission"] in {"write", "admin"}}
    roles_by_device: dict[int, list[dict[str, Any]]] = defaultdict(list)
    tasks_by_device: dict[int, list[dict[str, Any]]] = defaultdict(list)
    unassigned = []
    active = [dict(task, status_label=TASK_LABELS[task["status"]])
              for task in tasks if task["status"] in TASK_LABELS]
    active.sort(key=lambda task: (TASK_ORDER[task["status"]],
                                 0 if task["priority"] == "high" else 1, -task["id"]))
    for role in roles:
        if role["device_id"] in members:
            roles_by_device[role["device_id"]].append(role)
    for task in active:
        owner = task.get("claimed_by_device_id") or task.get("assigned_device_id")
        if owner in members:
            tasks_by_device[owner].append(task)
        else:
            unassigned.append(task)

    agents = []
    for device_id, member in members.items():
        owned = tasks_by_device[device_id]
        counts = Counter(task["status"] for task in owned)
        attention = counts["blocked"] + counts["review"]
        agent_roles = roles_by_device[device_id]
        agents.append({
            **member,
            "tone": ["iris", "blue", "sage", "amber", "rose", "slate"][int(device_id) % 6],
            "roles": agent_roles,
            "tasks": owned[:3],
            "task_count": len(owned),
            "attention_count": attention,
            "task_summary": " · ".join(
                f"{label} {counts[status]}" for status, label in TASK_LABELS.items()
                if counts[status]
            ) or "暂无看板任务",
            "can_assign_task": not bool(member.get("server_ai_provider")),
            "search_text": " ".join([str(member["device_label"]),
                str(member.get("group_name") or ""),
                *[str(role.get("name", "")) + " " + str(role.get("instructions", ""))
                  for role in agent_roles]]),
        })
    agents.sort(key=lambda agent: (
        not bool(agent["attention_count"]), not bool(agent.get("is_primary_device")),
        not bool(agent["task_count"]), str(agent["device_label"]).casefold(),
    ))
    return {
        "agents": agents,
        "total": len(agents),
        "online": sum(bool(agent.get("is_agent_online")) for agent in agents),
        "active_tasks": len(active),
        "attention": sum(task["status"] in {"blocked", "review"} for task in active),
        "unassigned": unassigned[:5],
        "unassigned_count": len(unassigned),
        "observers": len(memberships) - len(members),
    }
