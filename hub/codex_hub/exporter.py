from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def export_project_markdown(
    project: dict[str, Any],
    entries: list[dict[str, Any]],
    memberships: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
) -> str:
    lines: list[str] = [
        f"# 项目：{project['name']}",
        "",
        f"Slug：`{project['slug']}`",
        f"导出时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## 项目说明",
        "",
        project.get("description") or "（无）",
        "",
        "## 授权设备",
        "",
    ]
    if memberships:
        for member in memberships:
            group = f" / {member['group_name']}" if member.get("group_name") else ""
            lines.append(
                f"- {member['device_label']}{group}：{member['permission']}"
            )
    else:
        lines.append("- （无设备白名单）")

    lines.extend(["", "## 时间线", ""])
    if entries:
        for entry in entries:
            lines.extend(
                [
                    f"### {entry['created_at']} - {entry['source_label']} - {entry['kind']}",
                    "",
                    entry.get("body_markdown") or "（空）",
                    "",
                ]
            )
    else:
        lines.append("（暂无记录）")

    lines.extend(["", "## 附件", ""])
    if attachments:
        for attachment in attachments:
            size = attachment["byte_size"]
            lines.append(
                f"- {attachment['original_filename']} ({size} bytes, sha256 {attachment['sha256']})"
            )
    else:
        lines.append("- （无附件）")

    lines.append("")
    return "\n".join(lines)
