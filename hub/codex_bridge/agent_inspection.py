"""Compact scheduling indexes; full requirements remain explicitly retrievable."""
from __future__ import annotations


def _fields(value, keys):
    return {key:value[key] for key in keys if key in value}


def compact_inspection(context: dict, agents: list, task_response: dict, *, limit: int = 60) -> dict:
    """Avoid repeating identity instructions and every task's full body to the CLI.

    Current instructions/brief and handoff constraints are not truncated. Task
    rows are only an index, never a replacement for a target's acceptance rules.
    The explicit full/task detail path is part of this response's contract.
    """
    task = context.get('task') or {}
    room = task.get('conversation_id')
    rows = task_response.get('tasks') or []
    rows = [row for row in rows if isinstance(row, dict)]
    def order(row):
        same_room = room is not None and row.get('conversation_id') == room
        active = row.get('status') not in {'done', 'cancelled'} and not row.get('archived_at')
        identifier = row.get('id') if type(row.get('id')) is int else 0
        return (same_room, active, identifier)
    selected = sorted(rows, key=order, reverse=True)[:limit]
    indexes = []
    for row in selected:
        index = _fields(row, ('id', 'title', 'status', 'status_version', 'assigned_agent_id',
            'assigned_device_id', 'conversation_id', 'execution_route', 'execution_mode', 'archived_at'))
        summary = row.get('result_summary')
        if isinstance(summary, str) and summary:
            index['result_summary_excerpt'] = summary[:240]
            index['result_summary_truncated'] = len(summary) > 240
        indexes.append(index)
    concise_context = _fields(context, ('task', 'execution_scope', 'attachments', 'attachment_plan',
        'conversation_brief', 'role_contracts', 'work_assignments', 'handoff', 'workflow_blueprint'))
    concise_context['agent'] = _fields(context.get('agent') or {}, ('id', 'name', 'kind', 'role_key', 'active', 'revision'))
    return {
        'detail':'compact',
        'context':concise_context,
        'agents':{'agents':[_fields(agent, ('id', 'name', 'job', 'kind', 'active', 'device_id', 'role_key'))
                            for agent in agents if isinstance(agent, dict)]},
        'tasks':{'tasks':indexes, 'total':len(rows), 'returned':len(indexes), 'truncated':len(rows)>len(indexes),
                 'same_conversation_total':sum(row.get('conversation_id') == room for row in rows) if room is not None else 0},
        'read_more':{
            'note':'任务列表仅为调度索引，不含其他任务的完整要求；修改或接续前用 target_task_id 读取完整任务，不能把摘要当验收标准。'
                   '若列表被截取，可用 detail=full 查看全部。当前任务正文、对话目标和交接信息保留原文。',
            'task':{'action':'inspect', 'target_task_id':'实际任务编号'},
            'full':{'action':'inspect', 'detail':'full'},
        },
    }

