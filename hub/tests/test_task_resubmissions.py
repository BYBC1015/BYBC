"""A successful resend retires only proven, never-started input failures."""
import pytest

from codex_hub.execution_leases import ExecutionStore
from codex_hub.task_board import organize_task, archive_completed_delivery, initialize_task_board_schema
from codex_hub.task_resubmissions import register_confirmed_resubmission
from codex_hub.workspace import _task_views
from test_execution_leases import lease_task, start, RESULT
from test_revision_inputs import files, new_task


def pair(repo, d, *, source_board='auto', source_names=('mother.png',), successor_names=('mother.png',)):
    owner=repo.create_owner_if_missing('resend-owner','isolated-password')
    source=repo.get_task(d['task']['id']);description='沿用智慧母版，制作四张大屏，保持标题体系。'
    repo.connection.execute('UPDATE tasks SET description=?,created_by_user_id=?,board_state=? WHERE id=?',
                            (description,owner['id'],source_board,source['id']));repo.connection.commit()
    if source_names:
        originals=files(repo,d,source,source_names,user=True)
        for item in originals:
            repo.connection.execute('UPDATE attachments SET stored_relpath=? WHERE id=?',
                                    ('fixture/source/'+str(item['id'])+'.png',item['id']))
        repo.connection.commit()
    repo.update_task_status(source['id'],'blocked','智能体尚未开始工作：图片像素超过 4000 万，请缩小图片后重新上传。')
    successor=new_task(repo,d,title='不同标题也不作为判断依据',description=description)
    repo.connection.execute('UPDATE tasks SET created_by_user_id=? WHERE id=?',(owner['id'],successor['id']));repo.connection.commit()
    if successor_names:files(repo,d,successor,successor_names,user=True)
    return owner,repo.get_task(source['id']),successor


def finish(repo,d,task,status='review'):
    store=ExecutionStore(repo);run=start(store,{**d,'task':task},'resend-successor-'+str(task['id']))
    return store.complete(task['id'],d['device']['id'],run['generation'],run['run_key'],**{**RESULT,'status':status})


def register(repo,owner,source,successor,**kwargs):
    with repo.connection:
        repo.connection.execute('BEGIN IMMEDIATE')
        return register_confirmed_resubmission(repo,source['id'],successor['id'],confirmed_by_user_id=owner['id'],
            reason='用户明确确认前两项因原图过大重发，缩小后的对应提交已经成功。',**kwargs)


def test_exact_resend_success_archives_without_acceptance_or_record_loss(repo,lease_task):
    d=lease_task;owner,source,successor=pair(repo,d)
    before={table:repo.connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
            for table in ('attachments','entries','hub_agent_experiences')}
    finish(repo,d,successor)
    old=repo.get_task(source['id'])
    assert old['status']=='blocked' and old['result_summary']==source['result_summary']
    assert old['board_state']=='archived' and old['board_archive_reason']=='superseded:'+str(successor['id'])
    relation=dict(repo.connection.execute('SELECT * FROM hub_task_resubmissions').fetchone())
    assert relation['evidence_kind']=='identical_inputs' and relation['confirmed_by_user_id'] is None
    assert repo.connection.execute('SELECT COUNT(*) FROM attachments').fetchone()[0]==before['attachments']
    assert repo.connection.execute('SELECT COUNT(*) FROM entries').fetchone()[0]>=before['entries']
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_agent_experiences').fetchone()[0]>=before['hub_agent_experiences']
    assert not repo.connection.execute('SELECT 1 FROM hub_task_executions WHERE task_id=?',(source['id'],)).fetchone()


@pytest.mark.parametrize('mismatch',['sha','no_files','extra_input','description','room','agent','user','preflight','waiting','restored','successor_blocked'])
def test_similar_title_or_partial_evidence_never_archives(repo,lease_task,mismatch):
    d=lease_task
    names=() if mismatch=='no_files' else ('mother.png','extra.png') if mismatch=='extra_input' else ('mother.png',)
    owner,source,successor=pair(repo,d,successor_names=names)
    db=repo.connection
    if mismatch=='sha':db.execute("UPDATE attachments SET sha256=? WHERE id IN (SELECT attachment_id FROM hub_task_attachments WHERE task_id=?)",('a'*64,successor['id']))
    if mismatch=='description':db.execute("UPDATE tasks SET description=description||'新需求' WHERE id=?",(successor['id'],))
    if mismatch=='room':db.execute('UPDATE tasks SET conversation_id=NULL WHERE id=?',(successor['id'],))
    if mismatch=='agent':db.execute('UPDATE tasks SET assigned_agent_id=? WHERE id=?',(d['controller']['id'],successor['id']))
    if mismatch=='user':db.execute('UPDATE tasks SET created_by_user_id=NULL WHERE id=?',(successor['id'],))
    if mismatch=='preflight':db.execute("UPDATE tasks SET result_summary='工作做了一半失败，需要继续修改' WHERE id=?",(source['id'],))
    if mismatch=='waiting':db.execute("UPDATE tasks SET coordination_state='waiting' WHERE id=?",(source['id'],))
    if mismatch=='restored':db.execute("UPDATE tasks SET board_state='active' WHERE id=?",(source['id'],))
    db.commit();finish(repo,d,repo.get_task(successor['id']),status='blocked' if mismatch=='successor_blocked' else 'review')
    assert repo.get_task(source['id'])['board_state']==('active' if mismatch=='restored' else 'auto')
    assert not db.execute('SELECT 1 FROM hub_task_resubmissions').fetchone()


def test_confirmed_resized_resend_archives_once_and_later_restore_survives_replay(repo,lease_task):
    d=lease_task;owner,source,successor=pair(repo,d,source_board='active',successor_names=('resized-mother.png',))
    finish(repo,d,successor)
    assert repo.get_task(source['id'])['board_state']=='active'
    recorded=register(repo,owner,source,successor,archive_restored=True)
    assert recorded['created'] and recorded['archived'] and recorded['evidence_kind']=='user_confirmed'
    organize_task(repo,d['project']['id'],source['id'],'restore')
    initialize_task_board_schema(repo.connection)
    again=register(repo,owner,source,successor,archive_restored=True)
    assert not again['created'] and not again['archived']
    with repo.connection:
        repo.connection.execute('BEGIN IMMEDIATE')
        archive_completed_delivery(repo,successor['id'],None)
    assert repo.get_task(source['id'])['board_state']=='active'
    before=repo.connection.total_changes
    _task_views(repo,d['project']['id'],board='active')
    assert repo.connection.total_changes==before


@pytest.mark.parametrize('state',['prepared','running','uncertain','completed','stopped','yielded','superseded'])
def test_any_prior_execution_excludes_automatic_and_confirmed_retirement(repo,lease_task,state):
    d=lease_task;owner,source,successor=pair(repo,d,source_board='active')
    finish(repo,d,successor)
    db=repo.connection
    db.execute('''INSERT INTO hub_task_executions
        (task_id,generation,run_key,device_id,state,task_version,lease_until,created_at)
        VALUES (?,1,'prior-real-work',?,?,?,9999999999,1)''',
        (source['id'],d['device']['id'],state,source['status_version']))
    db.execute("UPDATE tasks SET board_state='auto' WHERE id=?",(source['id'],));db.commit()
    with db:
        db.execute('BEGIN IMMEDIATE')
        assert archive_completed_delivery(repo,successor['id'],None)==[]
    with pytest.raises(ValueError,match='从未开工'):
        register(repo,owner,source,successor,archive_restored=True)
    assert repo.get_task(source['id'])['board_state']=='auto'


def test_organization_failure_cannot_roll_back_real_successful_delivery(repo,lease_task):
    d=lease_task;owner,source,successor=pair(repo,d)
    repo.connection.execute('''CREATE TRIGGER isolated_resubmission_failure BEFORE INSERT ON hub_task_resubmissions
        BEGIN SELECT RAISE(FAIL,'isolated failure'); END''');repo.connection.commit()
    finish(repo,d,successor)
    assert repo.get_task(successor['id'])['status']=='review'
    assert repo.connection.execute('SELECT state FROM hub_task_executions WHERE task_id=?',(successor['id'],)).fetchone()[0]=='completed'
    assert repo.get_task(source['id'])['board_state']=='auto'
    assert not repo.connection.execute('SELECT 1 FROM hub_task_resubmissions').fetchone()


@pytest.mark.parametrize('mismatch',['project','room','agent','description'])
def test_user_confirmation_cannot_cross_work_boundaries(repo,lease_task,mismatch):
    d=lease_task;owner,source,successor=pair(repo,d,source_board='active',successor_names=('resized.png',))
    finish(repo,d,successor)
    db=repo.connection
    if mismatch=='project':
        other=repo.create_project('other-resend-project','Another project')
        db.execute('UPDATE tasks SET project_id=? WHERE id=?',(other['id'],successor['id']))
    if mismatch=='room':db.execute('UPDATE tasks SET conversation_id=NULL WHERE id=?',(successor['id'],))
    if mismatch=='agent':db.execute('UPDATE tasks SET assigned_agent_id=? WHERE id=?',(d['controller']['id'],successor['id']))
    if mismatch=='description':db.execute("UPDATE tasks SET description='不同的新任务' WHERE id=?",(successor['id'],))
    db.commit()
    with pytest.raises(ValueError,match='同对话同智能体同完整要求'):
        register(repo,owner,source,successor,archive_restored=True)
    assert repo.get_task(source['id'])['board_state']=='active'
