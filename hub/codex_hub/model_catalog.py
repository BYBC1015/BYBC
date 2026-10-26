"""Public, bounded model capabilities shared by the Hub and its device bridge."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


def normalize_codex_catalog(payload: Any) -> dict[str, Any]:
    """Drop private cache fields and normalize cache and model/list wire formats."""
    raw = payload if isinstance(payload, dict) else {}
    rows = raw.get("models", raw.get("data", []))
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows[:200] if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("hidden") is True:
            continue
        if row.get("visibility") not in (None, "list"):
            continue
        model = str(row.get("model") or row.get("slug") or row.get("id") or "").strip()
        if not MODEL_ID_RE.fullmatch(model) or model in seen:
            continue
        seen.add(model)
        efforts = row.get("supportedReasoningEfforts", row.get("supported_reasoning_levels", []))
        normalized_efforts = []
        for item in efforts[:20] if isinstance(efforts, list) else []:
            effort = (item.get("reasoningEffort") or item.get("effort")) if isinstance(item, dict) else item
            if isinstance(effort, str) and effort in EFFORTS and effort not in [value["reasoningEffort"] for value in normalized_efforts]:
                normalized_efforts.append({"reasoningEffort": effort})
        default = str(row.get("defaultReasoningEffort") or row.get("default_reasoning_level") or "")
        if default not in [item["reasoningEffort"] for item in normalized_efforts]:
            default = ""
        models.append({
            "model": model,
            "displayName": str(row.get("displayName") or row.get("display_name") or model)[:140],
            "supportedReasoningEfforts": normalized_efforts,
            "defaultReasoningEffort": default,
            "isDefault": row.get("isDefault") is True,
        })
    source = str(raw.get("source") or "device")
    return {
        "models": models,
        "source": source if source in {"app-server", "models-cache", "device", "unavailable"} else "device",
        "fetched_at": str(raw.get("fetched_at") or "")[:40],
    }


def normalize_device_capabilities(payload: Any) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    backend = str(raw.get("execution_backend") or "").strip().lower()[:40]
    result = {"execution_backend": backend, "codex": normalize_codex_catalog(raw.get("codex"))}
    machine = str(raw.get("machine_name") or "").strip()
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', machine):
        result['machine_name'] = machine
    if raw.get('runtime_kind') == 'durable-agent':
        result['runtime_kind'] = 'durable-agent'
    if raw.get("execution_mode") in {"codex-cli", "codex-desktop", "hermes-cli", "manual"}:
        result["execution_mode"] = raw["execution_mode"]
    return result


def build_project_model_catalog(
    memberships: list[dict[str, Any]], capabilities_by_device: Mapping[int, dict[str, Any]]
) -> dict[str, Any]:
    devices = []
    for member in memberships:
        if member.get("permission") not in {"write", "admin"} or member.get("agent_tasks_only"):
            continue
        device_id = int(member["device_id"])
        capabilities = normalize_device_capabilities(capabilities_by_device.get(device_id, {}))
        if member.get("server_ai_provider"):
            capabilities["execution_backend"] = str(member["server_ai_provider"])
        devices.append({
            "device_id": device_id,
            "device_label": member.get("device_label", ""),
            **capabilities,
        })
    return {"devices": devices}
