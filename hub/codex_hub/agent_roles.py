"""Explicit responsibility contracts; capability failures never change ownership."""
from __future__ import annotations

import hashlib
import json
import re


ROLES = {
    'controller': ('总控', '理解需求、拆分独立交付、分配模型与执行者、处理冲突；不承包制作或新增审批层'),
    'engineering': ('工程维护', '代码、系统故障、测试、部署、设备、CLI/MCP/API 接入及缺失配置；不承包美术制作'),
    'narrative_direction': ('编导', '剧本、分集/分幕、分镜、角色叙事与镜头连续性，形成可执行制作brief；不承包美术定调、画面制作、后期操作或工程接入'),
    'art_direction': ('美术方向', '风格、审美标尺、关键视觉及同场景多角度空间/母版一致性评议；不重复制作、不作为每项任务的审批关卡'),
    'reference_research': ('参考研究', '项目相关优秀案例与参考研究，优先检索日向前365天内案例；提供来源URL、案例发布日期或发生日期及核验说明，页面更新日期不得冒充案例日期，未知日期明确标注，经典旧例单列；不编造检索或日期，不决定最终风格，不承包制作、后期或工程；复用已有研究，提炼可借鉴点供专业执行者使用'),
    'visual_production': ('视觉制作', '图片生成与修图、版式、UI 视觉稿、封面和视频源素材；网页代码由工程维护负责'),
    'postproduction': ('后期技术', '现有视频/时间线、码素材、字幕、擦除、跟踪、抠像、合成和输出；工具接入由工程维护负责'),
    'general_support': ('通用支援', '仅承接以上专业职责均不适用的实际交付；忙碌、离线或缺配置不构成职责空缺'),
}


def validate_role(role, kind):
    if not isinstance(role, str) or (role and role not in ROLES):
        raise ValueError('请选择有效的分工角色')
    if role and (role == 'controller') != (kind == 'controller'):
        raise ValueError('总控角色只能分配给总控身份，执行角色只能分配给执行者')
    return role


def enabled(db, project_id):
    # Pausing the controller must not silently disable the project's safeguards.
    return bool(db.execute("SELECT 1 FROM hub_agents WHERE project_id=? AND role_key='controller'", (project_id,)).fetchone())


def resolve_owner(db, project_id, work_type, target, fallback_reason=''):
    if not isinstance(work_type, str) or work_type not in ROLES or work_type == 'controller':
        raise ValueError('请按具体交付选择 work_type：' + ' / '.join(key for key in ROLES if key != 'controller'))
    if not isinstance(fallback_reason, str) or len(fallback_reason) > 500:
        raise ValueError('fallback_reason 必须是最多 500 字的文本')
    if work_type == 'general_support' and (not isinstance(fallback_reason, str) or not 10 <= len(fallback_reason.strip()) <= 500):
        raise ValueError('救火队员接手需说明其他专业职责为何均不适用（fallback_reason）；离线、忙碌和缺配置不属于职责空缺')
    row = db.execute('SELECT id,active,device_id FROM hub_agents WHERE project_id=? AND role_key=?', (project_id, work_type)).fetchone()
    if not row:
        raise ValueError('该专业尚未配置负责人，请补充分工；不能因缺配置把专业工作转给救火队员')
    if not row['active'] or not row['device_id']:
        raise ValueError('该专业负责人已暂停或未绑定设备，请保留职责并恢复执行条件；不能改派救火队员')
    if target is not None and target != row['id']:
        raise ValueError(f"此类工作的负责人是智能体 #{row['id']}；请按交付拆分，避免越界接单")
    return row['id']


def contract_hash(title, description, attachment_ids):
    # Independent of request, model, priority and assignee. Attachment identity is
    # part of the work, so visually identical instructions on new inputs stay distinct.
    # Preserve significant spaces (code indentation, subtitle/layout requests).
    normalized = [s.replace('\r\n', '\n').strip() for s in (title, description)]
    return hashlib.sha256(json.dumps([*normalized, sorted(attachment_ids)], ensure_ascii=False).encode()).hexdigest()


def previous_work(db, project_id, room_id, work_key, fingerprint, owner, work_type):
    if not isinstance(work_key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,120}', work_key):
        raise ValueError('每项交付必须提供稳定的 work_key（8–120 位）；重试与后续对话保持不变，修改用 revise')
    row = db.execute('''SELECT * FROM hub_work_assignments WHERE project_id=?
        AND (work_key=? OR (conversation_id=? AND content_hash=?)) ORDER BY id LIMIT 1''', (project_id, work_key, room_id, fingerprint)).fetchone()
    if not row:
        return None
    if row['conversation_id'] != room_id:
        raise ValueError(f"同项工作已有任务 #{row['task_id']}，请回到工作对话 #{row['conversation_id']} 接续，不要在新对话重复派工")
    if row['agent_id'] != owner or row['work_type'] != work_type or row['content_hash'] != fingerprint:
        raise ValueError(f"同项工作已有负责人，任务 #{row['task_id']}；修改请用 revise，不可重复派工或抢占职责")
    return row['task_id']


def work_context(db, project_id, room_id):
    rows = db.execute('''SELECT w.work_key,w.work_type,w.task_id,w.agent_id,t.title,t.status
        FROM hub_work_assignments w JOIN tasks t ON t.id=w.task_id
        WHERE w.project_id=? AND w.conversation_id=? ORDER BY
        CASE WHEN t.status IN ('open','claimed','in_progress','blocked') THEN 0 ELSE 1 END,w.id DESC LIMIT 30''', (project_id, room_id)).fetchall()
    return [dict(row) for row in rows]
