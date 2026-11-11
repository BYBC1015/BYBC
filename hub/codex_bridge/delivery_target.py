"""Trusted, task-scoped locations for disposable delivery files."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class DeliveryTargetError(ValueError):
    pass


def resolve_delivery_target(options: dict[str, Any], project_slug: str, task_id: int,
                            *, frozen: dict[str, str] | None = None) -> dict[str, str] | None:
    """A receipt freezes the location; neither the model nor a later setting expands it."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", project_slug) or int(task_id) < 1:
        raise DeliveryTargetError("交付任务标识无效")
    raw = frozen.get("base") if frozen is not None else options.get("artifact_delivery_root")
    if not raw:
        if frozen is not None:
            raise DeliveryTargetError("交付目录记录不完整")
        return None
    base = Path(raw).expanduser()
    if not base.is_absolute():
        raise DeliveryTargetError("交付目录必须是本机绝对路径")
    base = base.resolve()
    target = base / project_slug / f"task-{int(task_id)}"
    if frozen is not None and Path(frozen.get("path") or "") != target:
        raise DeliveryTargetError("交付目录记录与任务不一致")
    # Resolve both before and after mkdir: a nested junction/symlink must not
    # grant upload access to a different project or a user's unrelated files.
    if target.resolve() != target:
        raise DeliveryTargetError("交付目录不能通过链接转到其他位置")
    target.mkdir(parents=True, exist_ok=True)
    if target.resolve() != target:
        raise DeliveryTargetError("交付目录位置已改变")
    return {"base": str(base), "path": str(target)}
