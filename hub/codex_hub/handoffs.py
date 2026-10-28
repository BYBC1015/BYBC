"""Visible, bounded clarification between persistent identities."""
import json
import re

from .agents import now
from .execution_leases import ExecutionConflict, require_execution_fence


def initialize_handoff_schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS hub_task_handoffs (
        id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id), generation INTEGER NOT NULL,
        round_number INTEGER NOT NULL, request_key TEXT NOT NULL, question TEXT NOT NULL,
        answer TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'open',
        controller_task_id INTEGER NOT NULL REFERENCES tasks(id), question_entry_id INTEGER REFERENCES entries(id),
        answer_entry_id INTEGER REFERENCES entries(id), created_at TEXT NOT NULL, answered_at TEXT,
        UNIQUE(task_id,generation), UNIQUE(task_id,request_key), UNIQUE(task_id,round_number)
      );
    ''')
    for table, columns in {
        'hub_agents': {'handoff_round_limit': 'INTEGER NOT NULL DEFAULT 2'},
        'tasks': {'coordination_state': "TEXT NOT NULL DEFAULT 'ready'", 'work_stage': "TEXT NOT NULL DEFAULT 'work'",
                  'execution_mode': "TEXT NOT NULL DEFAULT 'shared'", 'resource_keys_json': "TEXT NOT NULL DEFAULT '[]'"},
    }.items():
        present={row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
        for name,ddl in columns.items():
            if name not in present: db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')
    db.commit()


def handoff_context(db, task):
    controller=db.execute("SELECT handoff_round_limit FROM hub_agents WHERE project_id=? AND kind='controller' AND active=1",(task['project_id'],)).fetchone()
    reply=db.execute('SELECT task_id FROM hub_task_handoffs WHERE controller_task_id=?',(task['id'],)).fetchone()
    original_id=reply['task_id'] if reply else task['id']
    rows=db.execute('SELECT id,round_number,question,answer,state,controller_task_id FROM hub_task_handoffs WHERE task_id=? ORDER BY round_number',(original_id,)).fetchall()
    original=db.execute('SELECT title,description FROM tasks WHERE id=?',(original_id,)).fetchone()
    history=[]
    for row in rows[-3:]:
        item=dict(row)
        item['question']=item['question'][:800];item['answer']=item['answer'][:1600]
        item['excerpted']=len(row['question'])>800 or len(row['answer'])>1600
        history.append(item)
    return {'task_id':original_id,'max_rounds':controller['handoff_round_limit'] if controller else 2,
            'rounds':history,'total_rounds':len(rows),'is_reply_task':bool(reply),
            'original_brief':{'title':original['title'],'description':original['description'][:3000]} if reply else {},
            'coordination_state':task.get('coordination_state','ready')}


def ask_handoff(repo, source, device_id, data):
    question=data.get('question');key=data.get('request_key')
    if not isinstance(question,str) or not 1<=len(question.strip())<=2400 or not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{8,100}',key):
        raise ValueError('请提供具体问题（最多 2400 字）和稳定请求编号')
    question=question.strip();db=repo.connection
    with db:
        db.execute('BEGIN IMMEDIATE')
        source=repo.get_task(source['id'])
        run=require_execution_fence(db,source,device_id,data.get('execution'),allow_unleased=False)
        worker=db.execute('SELECT * FROM hub_agents WHERE id=?',(source['assigned_agent_id'],)).fetchone()
        if worker['kind']!='worker' or not worker['active'] or not source['conversation_id']:
            raise ValueError('仅当前执行者可以在任务对话中发起交接提问')
        old=db.execute('SELECT * FROM hub_task_handoffs WHERE task_id=? AND (generation=? OR request_key=?)',
                       (source['id'],run['generation'],key)).fetchone()
        if old:
            if old['generation']!=run['generation']:
                raise ValueError('这个交接问题已在上一轮处理，请先查看真实答复；新的细节使用新的问题编号')
            if old['question']!=question: raise ValueError('本次执行已发出问题，请等待答复；不能重复发出另一份问题')
            return dict(old)
        controller=db.execute("SELECT * FROM hub_agents WHERE project_id=? AND kind='controller' AND active=1",(source['project_id'],)).fetchone()
        if not controller or not controller['device_id']: raise ValueError('总控尚未就绪，不能创建无人回应的交接')
        count=db.execute('SELECT COUNT(*) FROM hub_task_handoffs WHERE task_id=?',(source['id'],)).fetchone()[0]
        if count>=controller['handoff_round_limit']:
            raise ValueError('已达到本项任务的交接轮数；按已有明确标准执行，关键输入仍缺失时具体说明阻塞，不可无限追问')
        reply=repo.create_task(source['project_id'],('交接答复：'+source['title'])[:200],
            description=f"{worker['name']} 对工作 #{source['id']} 的第 {count+1} 轮交接提问：\n{question}\n\n请查看交接记录并直接答复，必要时指出范围或输入中的问题。使用 answer_handoff 或以最终答复回答。不要另派一份相同制作任务。",
            assigned_agent_id=controller['id'],created_by_device_id=device_id,commit=False)
        db.execute("UPDATE tasks SET conversation_id=?,work_stage='handoff_reply',route_engine='handoff',execution_route='chat',execution_route_reason=? WHERE id=?",
                   (source['conversation_id'],'总控在原工作对话中澄清交接，实际交付继续由原执行者完成。',reply['id']))
        db.execute('INSERT OR IGNORE INTO hub_conversation_agents VALUES (?,?)',(source['conversation_id'],controller['id']))
        repo.bind_task_attachments(source['project_id'],[reply['id']],
            [item['id'] for item in repo.list_task_attachments(source['project_id'],source['id'])],source_task_id=source['id'])
        entry=repo.create_entry(source['project_id'],worker['name'],'message',
            f"**{worker['name']} → {controller['name']} · 交接第 {count+1} 轮**\n\n{question}",
            task_id=source['id'],author_device_id=device_id,commit=False,execution_fenced=True,
            metadata_json=json.dumps({'handoff':'question','round':count+1,'recipient_agent_id':controller['id']}))
        row_id=db.execute('''INSERT INTO hub_task_handoffs(task_id,generation,round_number,request_key,question,
            controller_task_id,question_entry_id,created_at) VALUES (?,?,?,?,?,?,?,?)''',
            (source['id'],run['generation'],count+1,key,question,reply['id'],entry['id'],now())).lastrowid
        # Keep the lease and resource ownership until the worker process exits.
        db.execute("UPDATE tasks SET coordination_state='waiting' WHERE id=?",(source['id'],))
        return dict(db.execute('SELECT * FROM hub_task_handoffs WHERE id=?',(row_id,)).fetchone())


def record_answer(repo, source, handoff, answer, device_id):
    """Inside caller's fenced transaction. The answer is real agent output."""
    db=repo.connection; original=repo.get_task(handoff['task_id'])
    if original['status'] in {'cancelled','done'}: raise ExecutionConflict('原任务已结束，不能通过迟到答复恢复执行')
    if handoff['state'] not in {'open','answered','blocked'}: raise ExecutionConflict('这轮交接已取消，不能恢复旧交接')
    if handoff['state']=='answered':
        if handoff['answer']!=answer: raise ValueError('本轮已答复，不能用重试覆盖另一份内容')
        return dict(handoff)
    sender=db.execute('SELECT name FROM hub_agents WHERE id=?',(source['assigned_agent_id'],)).fetchone()['name']
    target=db.execute('SELECT name FROM hub_agents WHERE id=?',(original['assigned_agent_id'],)).fetchone()['name']
    entry=repo.create_entry(source['project_id'],sender,'message',
        f"**{sender} → {target} · 交接第 {handoff['round_number']} 轮答复**\n\n{answer}",
        task_id=source['id'],author_device_id=device_id,commit=False,execution_fenced=True,
        metadata_json=json.dumps({'handoff':'answer','round':handoff['round_number'],'recipient_agent_id':original['assigned_agent_id']}))
    db.execute("UPDATE hub_task_handoffs SET answer=?,state='answered',answer_entry_id=?,answered_at=? WHERE id=?",
               (answer,entry['id'],now(),handoff['id']))
    db.execute("UPDATE tasks SET coordination_state='ready' WHERE id=?",(original['id'],))
    from .execution_leases import latest_execution
    execution=latest_execution(db,original['id'])
    if original['status'] in {'open','blocked'} and execution and execution['state']=='yielded':
        repo.update_task_status(original['id'],'open','交接已有真实答复，接续原工作。',execution_fenced=True)
    return dict(db.execute('SELECT * FROM hub_task_handoffs WHERE id=?',(handoff['id'],)).fetchone())


def block_unanswered_handoff(repo, reply_task_id):
    """A failed/cancelled answer must not leave the original waiting forever."""
    db=repo.connection
    handoff=db.execute("SELECT * FROM hub_task_handoffs WHERE controller_task_id=? AND state='open'",(reply_task_id,)).fetchone()
    if not handoff:return
    original=repo.get_task(handoff['task_id'])
    if original['status'] in {'done','cancelled'}:return
    db.execute("UPDATE hub_task_handoffs SET state='blocked' WHERE id=?",(handoff['id'],))
    db.execute("UPDATE tasks SET coordination_state='ready' WHERE id=?",(original['id'],))
    from .execution_leases import latest_execution
    execution=latest_execution(db,original['id'])
    if execution and execution['state']=='yielded':
        repo.update_task_status(original['id'],'blocked','交接答复未完成，请查看总控的具体阻塞原因；保留原工作。',execution_fenced=True)


def answer_handoff(repo, source, device_id, data):
    answer=data.get('answer');handoff_id=data.get('handoff_id')
    if type(handoff_id) is not int or not isinstance(answer,str) or not 1<=len(answer.strip())<=6000:
        raise ValueError('请提供交接编号和具体答复（最多 6000 字）')
    db=repo.connection
    with db:
        db.execute('BEGIN IMMEDIATE'); source=repo.get_task(source['id'])
        require_execution_fence(db,source,device_id,data.get('execution'),allow_unleased=False)
        sender=db.execute('SELECT kind,active FROM hub_agents WHERE id=?',(source['assigned_agent_id'],)).fetchone()
        row=db.execute('''SELECT h.* FROM hub_task_handoffs h JOIN tasks t ON t.id=h.task_id
            WHERE h.id=? AND t.project_id=? AND t.conversation_id=?''',(handoff_id,source['project_id'],source['conversation_id'])).fetchone()
        if not row or sender['kind']!='controller' or not sender['active']:
            raise ValueError('只能由当前总控答复本工作对话内的交接')
        return record_answer(repo,source,row,answer.strip(),device_id)
