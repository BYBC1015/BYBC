"""User-facing controller regressions found by the proactive scenario audit."""
import pytest

from codex_hub.controller_followups import is_read_only_followup
from codex_hub.model_policy import select_model
from codex_hub.completion_followups import summary_events, _current_room_context
from codex_hub.work_delivery import revise_task, initialize_delivery_schema
from codex_hub.execution_leases import ExecutionStore, ExecutionConflict
from test_execution_leases import lease_task, start, RESULT
from test_model_policy import setup_team
from test_work_delivery import controller_source
from test_controller_followups import dispatch


@pytest.mark.parametrize('text',['进度？','还在吗？','还要多久？','预计什么时候完成？','现在到哪一步了？','任务 #32 进度怎么样了？','查看目前进度'])
def test_common_status_questions_are_read_only_complete_messages(text):
    assert is_read_only_followup(text)


@pytest.mark.parametrize('text',['还要多久？顺便重做一版','任务 #32 改成蓝色','进度？取消旧任务',
    '现在到哪一步了，接着按新要求做','看看进度然后帮我更新','收到，继续','把状态改为完成'])
def test_status_words_do_not_make_actions_read_only(text):
    assert not is_read_only_followup(text)


@pytest.mark.parametrize('text',['你好，现在进度怎么样了？','您好，目前状态如何？','请问，任务 #32 进度？',
    '麻烦问一下，还要多久？','打扰一下，你还需要什么资料吗？','你好, 现在做好了吗？'])
def test_bounded_courtesy_prefix_preserves_status_fast_lane_and_model(repo,settings,text):
    assert is_read_only_followup(text)
    _,_,_,controller,_,_=setup_team(repo,settings)
    assert select_model(repo.connection,controller,settings.data_dir,task={'description':text})['reasoning_effort']=='medium'


@pytest.mark.parametrize('text',['你好，修改进度文案','您好，状态不对请修复','请问，能帮我调整标题吗？',
    '你好，进度怎么样了？顺便重做','麻烦问一下，取消旧任务','你好，请问，修改状态'])
def test_courtesy_never_hides_a_work_request_or_reduces_its_effort(repo,settings,text):
    assert not is_read_only_followup(text)
    _,_,_,controller,_,_=setup_team(repo,settings)
    assert select_model(repo.connection,controller,settings.data_dir,task={'description':text})['reasoning_effort']=='high'


def test_common_followup_can_be_acquired_while_normal_controller_work_runs(client,repo,lease_task):
    d=lease_task;store=ExecutionStore(repo)
    arranging=dispatch(repo,d,'请安排四屏修改')
    start(store,{**d,'task':arranging},'audit-controller-active')
    question=dispatch(repo,d,'还要多久？',room=arranging['conversation_id'])
    assert question['controller_read_only']==1
    assert store.acquire(question['id'],d['device']['id'],'audit-controller-question')['execution']['state']=='prepared'
    adjustment=dispatch(repo,d,'进度太慢，先修改创新图',room=arranging['conversation_id'])
    assert adjustment['controller_read_only']==0
    with pytest.raises(ExecutionConflict):store.acquire(adjustment['id'],d['device']['id'],'audit-controller-adjust')


@pytest.mark.parametrize('text',['这个状态不对，帮我改一下','收到，继续安排工程师修复','请修改进度展示逻辑'])
def test_work_requests_containing_status_or_received_words_keep_high_effort(repo,settings,text):
    _,_,_,controller,_,_=setup_team(repo,settings)
    choice=select_model(repo.connection,controller,settings.data_dir,task={'description':text,'controller_read_only':0})
    assert choice['reasoning_effort']=='high'


def test_completion_summary_retains_latest_correction_at_end_of_long_requirement(repo,lease_task):
    d=lease_task;description='最初硬性要求：沿用两套母版，不要地标。\n'+'历史制作细节。'*700+'\n最新用户修改：\n撤回独立韧性设定，智慧延展创新韧性，文明延展美丽宜居。'
    repo.connection.execute('UPDATE tasks SET description=? WHERE id=?',(description,d['task']['id']));repo.connection.commit()
    store=ExecutionStore(repo);run=start(store,d)
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    summary=repo.connection.execute("SELECT id FROM tasks WHERE work_stage='delivery_summary'").fetchone()[0]
    event=summary_events(repo.connection,summary)[0]
    assert len(event['acceptance_requirements'])<=4000
    assert '沿用两套母版，不要地标' in event['acceptance_requirements']
    assert '撤回独立韧性设定' in event['acceptance_requirements']
    assert event['requirements_excerpted']


def test_same_controller_turn_repeating_revision_content_does_not_duplicate_work_or_prompt(repo,lease_task):
    d=lease_task;store=ExecutionStore(repo);start(store,d)
    source,run=controller_source(repo,d)
    payload={'target_task_id':d['task']['id'],'instruction':'保持原有蓝金母版，仅修改标题','request_key':'audit-revision-first','execution':run}
    first=revise_task(repo,source,d['device']['id'],payload)
    original=repo.get_task(first['next_task_id'])['description']
    again=revise_task(repo,source,d['device']['id'],{**payload,'request_key':'audit-revision-retry'})
    assert again['next_task_id']==first['next_task_id']
    assert repo.get_task(first['next_task_id'])['description']==original
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_task_amendments WHERE source_task_id=?',(source['id'],)).fetchone()[0]==1
    with pytest.raises(ValueError):
        revise_task(repo,source,d['device']['id'],{**payload,'request_key':'audit-revision-retry','instruction':'换成红色'})
    # An additive/repeated migration must retain the retry key's meaning.
    initialize_delivery_schema(repo.connection)
    assert revise_task(repo,source,d['device']['id'],{**payload,'request_key':'audit-revision-retry'})['id']==first['id']


def test_same_instruction_in_new_user_turn_is_not_swallowed(repo,lease_task):
    d=lease_task;store=ExecutionStore(repo);start(store,d)
    source,run=controller_source(repo,d)
    payload={'target_task_id':d['task']['id'],'instruction':'再次检查标题','request_key':'audit-first-turn','execution':run}
    first=revise_task(repo,source,d['device']['id'],payload)
    store.complete(source['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    second_source,second_run=controller_source(repo,d)
    second=revise_task(repo,second_source,d['device']['id'],{**payload,'execution':second_run})
    assert second['id']!=first['id']
    assert second['source_task_id']==second_source['id']


def test_summary_sees_correction_sent_before_old_work_completed(client,repo,lease_task):
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    room=repo.get_task(d['task']['id'])['conversation_id']
    correction=dispatch(repo,d,'不要沿用旧标题，改为智慧城市',room=room)
    dispatch(repo,d,'另一个对话的保密要求',room=None)
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],**RESULT)
    summary=repo.get_task(repo.connection.execute("SELECT id FROM tasks WHERE work_stage='delivery_summary'").fetchone()[0])
    prompt=d['agents'].prompt(summary)
    assert '改为智慧城市' in prompt and '另一个对话的保密要求' not in prompt
    current=_current_room_context(repo.connection,summary,summary_events(repo.connection,summary['id']))
    assert any(row['task_id']==correction['id'] and row['task_status']=='open'
               for row in current['latest_user_messages'])
    assert current['latest_user_messages'][0]['after_result_task_ids']==[]


def test_readonly_prompt_keeps_requirements_and_live_status_without_dispatch_baggage(client,repo,lease_task,record_property):
    from codex_hub.execution_progress import write_progress
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    room=repo.get_task(d['task']['id'])['conversation_id']
    repo.connection.execute("UPDATE hub_agents SET role_key='controller',instructions='请保持总控身份并用中文回答' WHERE id=?",(d['controller']['id'],))
    repo.connection.commit()
    original=dispatch(repo,d,'原始目标：完成四张大屏，保留蓝金母版和原标题体系',room=room)
    repo.update_task_status(original['id'],'review','已明确四个交付项和两套参考母版')
    dispatch(repo,d,'最新纠正：不要地标，不能改变文明和智慧的对应关系',room=room)
    write_progress(store,d['task']['id'],d['device']['id'],{**{k:run[k] for k in ('generation','run_key')},
        'sequence':1,'kind':'assistant','text':'已完成两张，正在核对第三张的标题'})
    question=dispatch(repo,d,'还要多久？',room=room)
    prompt=repo.build_task_prompt_markdown(d['project']['slug'],question['id'])
    record_property('readonly_prompt_chars',len(prompt))
    assert '原始目标：完成四张大屏' in prompt and '最新纠正：不要地标' in prompt
    assert '请保持总控身份并用中文回答' in prompt
    assert '每项委派使用 execution_route' not in prompt
    assert 'select appropriate existing agents and delegate' not in prompt
    assert '已完成两张，正在核对第三张的标题' in prompt
    context=d['agents'].context(question)
    assert context['team']==[] and context['role_contracts']=={} and context['experiences']==[]


def test_revision_keeps_prior_latest_correction_when_total_still_within_budget(repo,lease_task):
    d=lease_task;description='原始要求：沿用母版。\n'+'历史细节。'*2100+'\n最新用户修改：保留文明与智慧对应关系'
    repo.connection.execute('UPDATE tasks SET description=? WHERE id=?',(description,d['task']['id']));repo.connection.commit()
    start(ExecutionStore(repo),d)
    source,run=controller_source(repo,d)
    result=revise_task(repo,source,d['device']['id'],{'target_task_id':d['task']['id'],
        'instruction':'这次只调整标题大小','request_key':'audit-long-revision','execution':run})
    revised=repo.get_task(result['next_task_id'])['description']
    assert description in revised and '这次只调整标题大小' in revised and len(revised)<=16000


def test_oversized_revision_never_silently_drops_earlier_requirements(repo,lease_task):
    d=lease_task
    repo.connection.execute('UPDATE tasks SET description=? WHERE id=?',('原始验收标准。'*1900,d['task']['id']));repo.connection.commit()
    start(ExecutionStore(repo),d)
    source,run=controller_source(repo,d)
    before=repo.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    with pytest.raises(ValueError,match='修改记录过长'):
        revise_task(repo,source,d['device']['id'],{'target_task_id':d['task']['id'],
            'instruction':'新增具体修改。'*400,'request_key':'audit-overlong-revision','execution':run})
    assert repo.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==before
    assert repo.connection.execute('SELECT COUNT(*) FROM hub_task_amendments').fetchone()[0]==0


def test_explicit_old_task_status_is_retrieved_after_recent_history_eviction(client,repo,lease_task):
    from test_revision_inputs import new_task
    d=lease_task;store=ExecutionStore(repo);run=start(store,d)
    store.complete(d['task']['id'],d['device']['id'],run['generation'],run['run_key'],
        **{**RESULT,'summary':'旧任务实际交付：蓝金母版已发布，尚未验收'})
    summary='旧任务实际交付：蓝金母版已发布。'+'过程记录。'*300+'结尾提醒：第四张尚需核验。'
    repo.connection.execute('UPDATE tasks SET result_summary=? WHERE id=?',(summary,d['task']['id']));repo.connection.commit()
    room=repo.get_task(d['task']['id'])['conversation_id']
    for index in range(13):
        newer=new_task(repo,d,title=f'后来的独立工作{index}')
        repo.update_task_status(newer['id'],'review','后来的结果')
    question=dispatch(repo,d,f"请问，任务 #{d['task']['id']} 进度怎么样了？",room=room)
    context=d['agents'].context(question)
    assert '旧任务实际交付：蓝金母版已发布' in d['agents'].prompt(question)
    assert context['requested_work_target']=={'task_id':d['task']['id'],'scope_found':True}
    target=next(row for row in context['current_work'] if row['id']==d['task']['id'])
    assert target['excerpted'] and len(target['result_summary'])<=1000
    assert '结尾提醒：第四张尚需核验' in target['result_summary']
    assert target['latest_execution']['state']=='completed'


@pytest.mark.parametrize('scope',['other_room','other_project','missing'])
def test_explicit_status_id_cannot_disclose_another_conversation(client,repo,lease_task,scope):
    d=lease_task;room=repo.get_task(d['task']['id'])['conversation_id']
    if scope=='missing':target_id=999999
    elif scope=='other_room':
        other=dispatch(repo,d,'另一个对话的私有内容');target_id=other['id']
    else:
        project=repo.create_project('status-private-project','Private project')
        other=repo.create_task(project['id'],'另一个项目的私有内容');target_id=other['id']
    question=dispatch(repo,d,f'任务 #{target_id} 进度？',room=room)
    context=d['agents'].context(question)
    assert context['requested_work_target']=={'task_id':target_id,'scope_found':False}
    prompt=d['agents'].prompt(question)
    assert '私有内容' not in prompt and '本对话中无法找到' in prompt
