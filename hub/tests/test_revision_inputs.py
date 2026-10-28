import hashlib
import json

import pytest

from codex_hub.revision_inputs import refresh_revision_inputs, task_input_manifest, revision_candidates, MAX_BYTES, MAX_FILE_BYTES
from codex_hub.conversation_context import conversation_files
from codex_hub.execution_leases import ExecutionStore, ExecutionConflict
from test_execution_leases import lease_task, start, RESULT
from test_agents import token_headers


def new_task(repo,d,title='接续修改',dependency=None,description=''):
    task=repo.create_task(d['project']['id'],title,description=description,assigned_agent_id=d['worker']['id'])
    room=repo.get_task(d['task']['id'])['conversation_id']
    d['agents'].route(task['id'],{'action':'reuse','conversation_id':room,'reason':'test'},'codex_cli')
    if dependency:
        repo.connection.execute('UPDATE tasks SET wait_for_task_id=? WHERE id=?',(dependency['id'],task['id']));repo.connection.commit()
    return repo.get_task(task['id'])


def files(repo,d,task,names,*,user=False,sizes=None,run=None):
    owner=repo.create_owner_if_missing('inputs-owner','isolated-test') if user else None
    metadata=[{'original_filename':name,'content_type':'application/pdf' if name.endswith('.pdf') else
        'text/markdown' if name.endswith('.md') else 'image/png','stored_relpath':'fixture/'+name,
        'byte_size':sizes[i] if sizes else 1000,'sha256':hashlib.sha256(name.encode()).hexdigest()} for i,name in enumerate(names)]
    _,saved=repo.create_entry_with_attachments(project_id=d['project']['id'],source_label='fixture',kind='message',
        body_markdown='参考资料',attachments=metadata,author_user_id=owner['id'] if owner else None,
        task_id=task['id'],execution_fenced=True)
    db=repo.connection
    with db:
        db.execute('BEGIN IMMEDIATE')
        for row in saved:
            db.execute('INSERT INTO hub_task_attachments VALUES (?,?,NULL)',(task['id'],row['id']))
            if run:db.execute('INSERT INTO hub_task_artifacts VALUES (?,?,?,?,?)',
                (task['id'],run['generation'],hashlib.sha256(str(row['id']).encode()).hexdigest(),row['original_filename'],row['id']))
    return saved


def output_names(version):
    return [f'{index}-{label}-{version}{extra}.png' for index,label in enumerate(['beautiful','livable','innovation','resilient'],1)
        for extra in ['-3960x756','-preview-1980x378']]+[f'overview-{version}-1980x1512.png',f'notes-{version}.md']


def completed(repo,d,task,version):
    store=ExecutionStore(repo);run=start(store,{**d,'task':task},f'revision-result-{task["id"]}')
    saved=files(repo,d,task,output_names(version),run=run)
    store.complete(task['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    return saved,run


def bind(repo,task,rows,source):
    with repo.connection:
        repo.connection.execute('BEGIN IMMEDIATE')
        for row in rows:repo.connection.execute('INSERT OR IGNORE INTO hub_task_attachments VALUES (?,?,?)',(task['id'],row['id'],source['id']))


@pytest.fixture
def accumulated(repo,lease_task):
    d=lease_task;old,_=completed(repo,d,d['task'],'v1')
    second=new_task(repo,d,dependency=d['task']);latest,run=completed(repo,d,second,'v2')
    originals_task=new_task(repo,d,'原始资料')
    originals=files(repo,d,originals_task,['meeting.pdf','transcript.pdf','wisdom.png','civilization.png'],user=True)
    correction_task=new_task(repo,d,'用户最新纠正');correction=files(repo,d,correction_task,['correction.png'],user=True)
    desc='\n'.join(f"已提供源文件：/{r['id']}-{r['original_filename']}" for r in originals)
    target=new_task(repo,d,dependency=second,description=desc)
    bind(repo,target,old,d['task']);bind(repo,target,latest,second);bind(repo,target,correction,correction_task)
    return {**d,'target':target,'second':second,'old':old,'latest':latest,'originals':originals,
            'correction':correction,'run':run}


def test_21_accumulated_inputs_become_11_traceable_inputs_without_losing_originals(client,repo,accumulated):
    d=accumulated;t=d['target'];db=repo.connection
    assert len(repo.list_task_attachments(t['project_id'],t['id']))==21
    manifest=task_input_manifest(repo,t)
    expected={r['id'] for r in d['originals']+d['correction']+[r for r in d['latest'] if '-preview-' not in r['original_filename']]}
    assert {r['id'] for r in manifest['attachments']}==expected and len(expected)==11
    headers=token_headers(repo,d['device'])
    before=db.total_changes
    page=client.get(f"/api/projects/{d['project']['slug']}/tasks/{t['id']}/attachments",headers=headers)
    assert page.status_code==200 and {r['id'] for r in page.json()['attachments']}==expected
    assert db.total_changes==before  # HTTP auth uses its separate connection; this pure projection never writes.
    with db:db.execute('BEGIN IMMEDIATE');refresh_revision_inputs(db,t)
    assert {r['id'] for r in repo.list_task_attachments(t['project_id'],t['id'])}==expected
    catalog={r['id'] for r in conversation_files(db,t['project_id'],t['conversation_id'])}
    assert {r['id'] for r in d['old']+d['latest']+d['originals']}.issubset(catalog)
    assert 'attachment_plan' in d['agents'].prompt(t)


def test_following_revision_preserves_source_material_and_replaces_only_old_deliveries(repo,accumulated):
    d=accumulated;t=d['target'];fresh,_=completed(repo,d,t,'v3')
    later=new_task(repo,d,dependency=t,description=t['description'])
    bind(repo,later,repo.list_task_attachments(t['project_id'],t['id']),t)
    with repo.connection:repo.connection.execute('BEGIN IMMEDIATE');refresh_revision_inputs(repo.connection,later)
    selected=task_input_manifest(repo,later)['attachments'];ids={r['id'] for r in selected}
    assert {r['id'] for r in d['originals']+d['correction']}.issubset(ids)
    assert {r['id'] for r in fresh if '-preview-' not in r['original_filename']}.issubset(ids)
    assert not ids.intersection(r['id'] for r in d['latest']+d['old'])


def test_explicit_old_delivery_mother_reference_and_user_preview_are_never_discarded(repo,accumulated):
    d=accumulated;t=d['target'];mother=d['old'][0]
    user_preview=files(repo,d,t,['client-preview-1980x378.png'],user=True)[0]
    repo.connection.execute('UPDATE tasks SET description=description || ? WHERE id=?',
        (f"\n已确认母版：[原图](../attachments/{mother['id']}?inline=true)",t['id']));repo.connection.commit()
    t=repo.get_task(t['id']);manifest=task_input_manifest(repo,t)
    candidates={r['id'] for r in manifest['attachments']}|{r['id'] for r in manifest['reference_files'] if r['required_unloaded']}
    assert mother['id'] in candidates and user_preview['id'] in candidates
    with repo.connection:repo.connection.execute('BEGIN IMMEDIATE');refresh_revision_inputs(repo.connection,t)
    assert {mother['id'],user_preview['id']}.issubset(r['id'] for r in repo.list_task_attachments(t['project_id'],t['id']))


@pytest.mark.parametrize('state',['prepared','running','uncertain'])
def test_generation_manifest_is_frozen_before_execution_and_unchanged_by_late_files(repo,accumulated,state):
    d=accumulated;t=d['target'];store=ExecutionStore(repo)
    run=store.acquire(t['id'],d['device']['id'],'frozen-inputs-generation-001')['execution']
    if state!='prepared':store.start(t['id'],d['device']['id'],run['generation'],run['run_key'])
    if state=='uncertain':repo.connection.execute("UPDATE hub_task_executions SET state='uncertain' WHERE task_id=?",(t['id'],));repo.connection.commit()
    snapshot=task_input_manifest(repo,repo.get_task(t['id']))
    bindings=repo.list_task_attachments(t['project_id'],t['id'])
    late=files(repo,d,d['second'],['late-new-source.png'],run=d['run'])[0]
    source_entry=repo.connection.execute('SELECT result_entry_id FROM hub_task_executions WHERE task_id=? AND generation=?',
        (d['second']['id'],d['run']['generation'])).fetchone()[0]
    repo.connection.execute('UPDATE attachments SET entry_id=? WHERE id=?',(source_entry,late['id']));repo.connection.commit()
    assert late['id'] in {row['id'] for row in revision_candidates(repo.connection,repo.get_task(t['id']))[0]}
    with repo.connection:repo.connection.execute('BEGIN IMMEDIATE');refresh_revision_inputs(repo.connection,repo.get_task(t['id']))
    assert repo.list_task_attachments(t['project_id'],t['id'])==bindings
    assert task_input_manifest(repo,repo.get_task(t['id']))==snapshot


def test_explicit_filename_and_room_project_boundaries_are_required(repo,accumulated):
    d=accumulated;t=d['target'];db=repo.connection
    other=repo.create_task(d['project']['id'],'另一房间',assigned_agent_id=d['worker']['id'])
    other=d['agents'].route(other['id'],{'action':'new','title':'另一房间','reason':'隔离'},'codex_cli')
    secret=files(repo,d,other,['private.pdf'],user=True)[0]
    wrong_name=files(repo,d,new_task(repo,d,'另一资料'),['right-name.pdf'],user=True)[0]
    db.execute('UPDATE tasks SET description=description || ? WHERE id=?',
        (f"\n/{secret['id']}-private.pdf\n/{wrong_name['id']}-wrong-name.pdf",t['id']));db.commit()
    assert not {secret['id'],wrong_name['id']}.intersection(r['id'] for r in task_input_manifest(repo,repo.get_task(t['id']))['attachments'])
    db.execute('UPDATE tasks SET wait_for_task_id=? WHERE id=?',(other['id'],t['id']));db.commit()
    with pytest.raises(ExecutionConflict):task_input_manifest(repo,repo.get_task(t['id']))
    other_project=repo.create_project('foreign-inputs','跨项目',created_by_device_id=d['device']['id'])
    db.execute('UPDATE tasks SET project_id=?,conversation_id=? WHERE id=?',(other_project['id'],t['conversation_id'],other['id']));db.commit()
    with pytest.raises(ExecutionConflict):task_input_manifest(repo,repo.get_task(t['id']))


def test_over_limit_inputs_are_bounded_and_explicitly_unread_in_old_bridge_prompt(repo,accumulated):
    d=accumulated;t=d['target'];extra=files(repo,d,t,[f'important-{i}.pdf' for i in range(13)],user=True)
    result=task_input_manifest(repo,t)
    assert len(result['attachments'])<=12 and result['required_unloaded_count']>0
    assert {r['id'] for r in extra}.issubset({r['id'] for r in result['attachments']}|{r['id'] for r in result['reference_files']})
    context=d['agents'].context(t);prompt=d['agents'].prompt(t)
    assert context['attachments']==result['attachments']
    assert '未预载不等于已阅读' in prompt and 'required_unloaded_count' in prompt
    assert len(repo.list_task_attachments(t['project_id'],t['id']))==34


@pytest.mark.parametrize('original_size',[MAX_FILE_BYTES+1,40*1024*1024])
def test_preview_remains_available_when_original_cannot_fit_budget(repo,lease_task,original_size):
    d=lease_task;parent=d['task'];store=ExecutionStore(repo);run=start(store,d)
    outputs=files(repo,d,parent,['art-3960x756.png','art-preview-1980x378.png'],sizes=[original_size,1000],run=run)
    store.complete(parent['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    t=new_task(repo,d,dependency=parent)
    files(repo,d,t,['source1.pdf','source2.pdf'],user=True,sizes=[45*1024*1024,45*1024*1024])
    result=task_input_manifest(repo,t)
    assert result['selected_bytes']<=MAX_BYTES and outputs[1]['id'] in {r['id'] for r in result['attachments']}
    assert outputs[0]['id'] in {r['id'] for r in result['reference_files']}
