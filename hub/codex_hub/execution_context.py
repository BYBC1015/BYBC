"""Evidence-based connection status and bounded model context.

Presence is a connection heartbeat, not proof of a distinct physical computer or
an idle executor. This module never moves in-flight work based on presence.
"""
from __future__ import annotations

import json
import re
from typing import Any


def annotate_execution(agents: list[dict], tasks: list[dict], members: list[dict]) -> None:
    connections = {m['device_id']: m for m in members}
    for agent in agents:
        member = connections.get(agent.get('device_id'), {})
        if not agent.get('active'):
            state, label, hint = 'paused', '身份已暂停', '恢复身份后才会执行新工作。'
        elif not agent.get('can_dispatch'):
            state, label, hint = 'unconfigured', '执行连接待配置', '需要可执行本项目工作的连接。'
        elif not member.get('agent_tasks_only'):
            state, label, hint = 'unverified', '执行能力待核验', '已绑定连接，但尚未登记专用智能体执行器；在线不代表能自动接单。'
        elif not member.get('is_agent_online'):
            state, label, hint = 'offline', '执行连接离线', '可以提交工作并排队；连接恢复后领取，不会自动重复执行进行中的任务。'
        else:
            state, label, hint = 'connected', '执行连接已接入', '最近收到执行器心跳；任务是否开始以工作记录为准。'
        agent.update(connection_state=state, connection_label=label, connection_hint=hint)
    by_agent = {a['id']: a for a in agents}
    for task in tasks:
        if task.get('status') not in {'open', 'claimed', 'in_progress'}:
            continue
        # A later identity edit must not replace the frozen binding of a live task.
        member = connections.get(task.get('assigned_device_id'), {})
        if task.get('status') == 'open':
            if not member.get('agent_tasks_only'):
                label, hint = '等待执行器就绪', '连接的自动执行能力尚待核验。'
            elif not member.get('is_agent_online'):
                label, hint = '排队中 · 等待连接', '工作和附件已保存，绑定的执行器恢复后可领取。'
            else:
                label, hint = '排队中 · 等待领取', '连接已接入；领取成功后显示实际执行进度。'
        elif not member.get('is_agent_online'):
            label, hint = '执行连接中断', '保留已有进度；恢复前需核对执行收据，不会仅因另一连接在线就重复运行。'
        else:
            label, hint = '执行连接已接入', '保留当前执行者；新设备在线不会抢走进行中的工作。'
        if task.get('assigned_agent_id') in by_agent:
            task.update(execution_label=label, execution_hint=hint)


def terms(value: str) -> set[str]:
    value = value.casefold()
    result = set(re.findall(r'[a-z0-9_]{3,}', value))
    for text in re.findall(r'[\u4e00-\u9fff]{2,}', value):
        result.update(text[i:i + 2] for i in range(len(text) - 1))
    return result


def select_experiences(records: list[dict], query: str, *, limit: int = 4, budget: int = 6000) -> list[dict]:
    """Keep source/revision/verification and relevant excerpts, never alter storage."""
    query_terms = terms(query)
    ranked = []
    for record in records:
        content = ' '.join(str(record.get(k) or '') for k in ('source_title', 'summary', 'worked', 'failed', 'next_time')).casefold()
        hits = sum(term in content for term in query_terms)
        if hits:
            ranked.append((hits, record.get('verification') == 'accepted', record.get('id', 0), record))
    selected = []
    for _, _, _, record in sorted(ranked, key=lambda item: item[:3], reverse=True):
        item = {k: record.get(k) for k in ('id', 'agent_id', 'task_id', 'revision', 'verification', 'outcome')}
        excerpted = False
        for key, cap in [('summary', 600), ('worked', 700), ('failed', 700), ('next_time', 800)]:
            value = str(record.get(key) or '')
            item[key] = value[:cap]
            excerpted |= len(value) > cap
        if excerpted:
            item['excerpted'] = True
        size = len(json.dumps(item, ensure_ascii=False, separators=(',', ':')))
        if size <= budget:
            selected.append(item)
            budget -= size
        if len(selected) >= limit:
            break
    return selected


def execution_identity(context: dict[str, Any]) -> dict:
    """The task body and candidate rooms are already known during execution."""
    agent = context.get('agent') or {}
    task = context.get('task') or {}
    return {
        'agent': {k: agent[k] for k in ('id', 'name', 'job', 'instructions', 'kind', 'revision') if k in agent},
        'work': {k: task[k] for k in ('id', 'conversation_id', 'route_reason', 'assigned_agent_id',
                'assigned_device_id', 'execution_route', 'execution_route_reason') if k in task},
        'team': context.get('team', []),
        'role_contracts': context.get('role_contracts', {}),
        'work_assignments': context.get('work_assignments', []),
        'handoff': context.get('handoff', {}),
        'execution_scope': context.get('execution_scope', {}),
        'experiences': context.get('experiences', []),
        'attachments': context.get('attachments', []),
        'attachment_plan': context.get('attachment_plan', {}),
        'conversation_brief': context.get('conversation_brief', {}),
    }


def room_history(rows: list[dict], *, budget: int = 5000) -> list[dict]:
    selected = []
    for row in rows[:6]:
        item = {k: row.get(k) for k in ('id', 'title', 'status', 'executor')}
        summary = str(row.get('result_summary') or '')
        item['result_summary'] = summary[:600]
        if len(summary) > 600:
            item['excerpted'] = True
        size = len(json.dumps(item, ensure_ascii=False, separators=(',', ':')))
        if size <= budget:
            selected.append(item)
            budget -= size
    return selected
