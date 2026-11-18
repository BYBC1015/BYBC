"""Controller-managed model choices with explicit user overrides and task snapshots."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .device_capabilities import load_device_capabilities
from .model_catalog import MODEL_ID_RE, EFFORTS
from .controller_followups import is_read_only_followup


def initialize_model_policy_schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS hub_agent_model_policies (
        agent_id INTEGER PRIMARY KEY REFERENCES hub_agents(id) ON DELETE CASCADE,
        mode TEXT NOT NULL DEFAULT 'controller', model TEXT NOT NULL DEFAULT '',
        reasoning_effort TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS hub_task_model_choices (
        task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
        selection_json TEXT NOT NULL, created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS hub_model_policy_events (
        id INTEGER PRIMARY KEY, agent_id INTEGER NOT NULL REFERENCES hub_agents(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL, policy_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS hub_controller_reserves (
        agent_id INTEGER PRIMARY KEY REFERENCES hub_agents(id) ON DELETE CASCADE,
        model TEXT NOT NULL, service_revision INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS hub_controller_cooldowns (
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE, provider_scope TEXT NOT NULL,
        rejected_at REAL NOT NULL, retry_at REAL NOT NULL, failures INTEGER NOT NULL,
        PRIMARY KEY(device_id,provider_scope));
      CREATE TABLE IF NOT EXISTS hub_controller_capacity_tasks (
        task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
        primary_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS hub_execution_capacity_rejections (
        execution_id INTEGER PRIMARY KEY REFERENCES hub_task_executions(id) ON DELETE CASCADE,
        reason_code TEXT NOT NULL);
    ''')
    db.commit()


def agent_policy(db, agent_id):
    row = db.execute('SELECT * FROM hub_agent_model_policies WHERE agent_id=?', (agent_id,)).fetchone()
    return dict(row) if row else {'agent_id': agent_id, 'mode': 'controller', 'model': '',
        'reasoning_effort': '', 'reason': '', 'revision': 0}


def available_models(data_dir, device_id, db=None, project_id=None):
    if not device_id:
        return []
    caps = load_device_capabilities(data_dir, [device_id]).get(device_id, {})
    models = caps.get('codex', {}).get('models', [])
    if caps.get('model_services') is True and db is not None and project_id:
        from .model_services import service_models
        models = models + service_models(db, project_id, device_id)
    return models


def validate_choice(value, models):
    if not isinstance(value, dict):
        raise ValueError('模型设置必须是对象')
    model = str(value.get('model') or '').strip()
    effort = str(value.get('reasoning_effort') or '').strip()
    reason = str(value.get('reason') or '').strip()
    if not MODEL_ID_RE.fullmatch(model) or effort not in EFFORTS or len(reason) > 600:
        raise ValueError('请指定有效模型、思考档位和简短原因')
    match = next((row for row in models if row['model'] == model), None)
    if not match:
        raise ValueError('执行设备尚未上报此模型，请先核验该设备的模型能力')
    if effort not in {row['reasoningEffort'] for row in match['supportedReasoningEfforts']}:
        raise ValueError('所选模型不支持这个思考档位')
    choice = {'model': model, 'reasoning_effort': effort, 'reason': reason}
    if model.startswith('hub-service/'):
        choice.update({key: match[key] for key in ('service_revision', 'supports_tools', 'supports_images')})
    return choice


def reserve_policy(db, agent_id):
    row = db.execute('SELECT model,service_revision FROM hub_controller_reserves WHERE agent_id=?',
                     (agent_id,)).fetchone()
    return dict(row) if row else None


def save_policy(db, agent, data_dir, *, mode, model='', reasoning_effort='', reason='', actor='user', reserve=None):
    if mode not in {'controller', 'manual'}:
        raise ValueError('请选择总控分配或手动指定')
    previous = agent_policy(db, agent['id'])
    if actor != 'user' and previous['mode'] == 'manual':
        raise ValueError('用户已手动指定模型，总控不能覆盖；后续任务继续使用用户设置')
    choice = {'model': '', 'reasoning_effort': '', 'reason': ''}
    if model:
        choice = validate_choice({'model': model, 'reasoning_effort': reasoning_effort, 'reason': reason},
                                 available_models(data_dir, agent.get('device_id'), db, agent.get('project_id')))
        if model.startswith('hub-service/') and agent.get('kind') == 'controller' and not choice['supports_tools']:
            raise ValueError('总控需要支持工具调用的模型服务，才能安排和调整工作')
        choice = {key: choice[key] for key in ('model', 'reasoning_effort', 'reason')}
    elif mode == 'manual':
        raise ValueError('手动指定需要选择模型')
    selected_reserve = None
    if reserve is not None:
        if agent.get('kind') != 'controller' or actor != 'user' or not isinstance(reserve, str):
            raise ValueError('总控备用模型由用户设置')
        if reserve:
            candidates = available_models(data_dir, agent.get('device_id'), db, agent.get('project_id'))
            selected_reserve = next((item for item in candidates if item.get('supports_tools')
                and item['model'].startswith('hub-service/')
                and reserve == f"{item['model']}@{item['service_revision']}"), None)
            if selected_reserve is None:
                raise ValueError('备用服务配置已变更或无法在此设备调用工具，请刷新后重新选择')
    at = datetime.now(timezone.utc).isoformat()
    policy = {'agent_id': agent['id'], 'mode': mode, **choice,
              'revision': previous['revision'] + 1, 'updated_at': at}
    with db:
        db.execute('''INSERT INTO hub_agent_model_policies VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(agent_id) DO UPDATE SET mode=excluded.mode,model=excluded.model,
            reasoning_effort=excluded.reasoning_effort,reason=excluded.reason,
            revision=excluded.revision,updated_at=excluded.updated_at''', tuple(policy.values()))
        db.execute('INSERT INTO hub_model_policy_events(agent_id,revision,policy_json,actor,created_at) VALUES (?,?,?,?,?)',
                   (agent['id'], policy['revision'], json.dumps(policy, ensure_ascii=False), actor, at))
        if reserve is not None:
            db.execute('DELETE FROM hub_controller_reserves WHERE agent_id=?', (agent['id'],))
            if selected_reserve:
                db.execute('INSERT INTO hub_controller_reserves VALUES (?,?,?)',
                           (agent['id'], selected_reserve['model'], selected_reserve['service_revision']))
    return policy


def task_choice(db, task_id):
    row = db.execute('SELECT selection_json FROM hub_task_model_choices WHERE task_id=?', (task_id,)).fetchone()
    return json.loads(row['selection_json']) if row else None


def select_model(db, agent, data_dir, requested=None, *, task=None):
    policy = agent_policy(db, agent['id'])
    models = available_models(data_dir, agent.get('device_id'), db, agent.get('project_id'))
    def checked(value):
        choice = validate_choice(value, models)
        if choice['model'].startswith('hub-service/'):
            if (task or {}).get('execution_route') == 'project':
                raise ValueError('本地工程任务需要 Codex CLI；请为此身份恢复总控分配或选择 CLI 模型')
            if agent.get('kind') == 'controller' and not choice['supports_tools']:
                raise ValueError('总控模型需支持工具调用，才能安排工作')
        return choice
    if policy['mode'] == 'manual':
        choice = checked(policy)
        return {**choice, 'source': 'user', 'policy_revision': policy['revision']}
    if requested:
        choice = checked(requested)
        if not choice['reason']:
            raise ValueError('总控分配模型时需要说明选择原因')
        return {**choice, 'source': 'controller', 'policy_revision': policy['revision']}
    if policy['model']:
        return {**checked(policy), 'source': 'controller', 'policy_revision': policy['revision']}
    # This default is an explicit preference rule, not a claim that an LLM evaluated this task.
    preferences = [('gpt-6.1-sol', 'medium'), ('gpt-6-sol', 'medium'), ('gpt-5.6-sol', 'high'), ('gpt-6-astra', 'medium')]
    reason = '默认偏好策略；总控可按工作需要调整，用户可手动指定。'
    if agent.get('kind') == 'controller':
        description = ((task or {}).get('description') or (task or {}).get('title') or '').strip()
        effort = 'high'
        if (task or {}).get('controller_read_only'):
            effort = 'medium'
        elif any(word in description for word in ('深度优化', '深度审查', '架构设计', '复杂故障', '极高')):
            effort = 'xhigh'
        elif is_read_only_followup(description) or description.rstrip('。！？!?') in {'收到', '你好', '谢谢'}:
            effort = 'medium'
        preferences = [('gpt-6-astra', e) for e in dict.fromkeys([effort, 'high', 'medium'])] + preferences
        reason = '总控优先 GPT-6 Astra；简短状态沟通用中档，工作安排用高档，明确深度审查用极高档；仅选择设备支持的档位。'
    for model, effort in preferences:
        try:
            choice = validate_choice({'model': model, 'reasoning_effort': effort,
                'reason': reason}, models)
            return {**choice, 'source': 'default_policy', 'policy_revision': policy['revision']}
        except ValueError:
            continue
    cli_models = [m for m in models if not m['model'].startswith('hub-service/')]
    if cli_models:
        item = next((m for m in cli_models if m.get('isDefault')), cli_models[0])
        effort = item.get('defaultReasoningEffort') or next((e['reasoningEffort'] for e in item['supportedReasoningEfforts']), '')
        if effort:
            return {'model': item['model'], 'reasoning_effort': effort, 'source': 'device_catalog',
                    'reason': '偏好型号未上报，采用设备目录中的默认型号。', 'policy_revision': policy['revision']}
    return {'model': '', 'reasoning_effort': '', 'source': 'device_default',
            'reason': '设备尚未上报模型目录，执行器使用本机设置；尚未核验实际型号。', 'policy_revision': policy['revision']}


def freeze_choice(db, task_id, choice):
    db.execute('INSERT OR IGNORE INTO hub_task_model_choices VALUES (?,?,?)',
        (task_id, json.dumps(choice, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
    return task_choice(db, task_id)


def ensure_task_choice(repo, task, data_dir):
    existing = task_choice(repo.connection, task['id'])
    if existing:
        return existing
    agent = dict(repo.connection.execute('SELECT * FROM hub_agents WHERE id=?', (task['assigned_agent_id'],)).fetchone())
    # Existing work keeps its frozen execution device, even after an identity edit.
    agent['device_id'] = task['assigned_device_id']
    choice = select_model(repo.connection, agent, data_dir, task=task)
    with repo.connection:
        return freeze_choice(repo.connection, task['id'], choice)
