"""Production workflow projection: no model calls or implicit task creation."""
import json

import pytest

from codex_hub.workflow_blueprints import match_workflows, workflow_blueprint
from codex_hub.agent_roles import ROLES
from test_agents import team
from test_conversation_continuity import user_turn, upload
from test_execution_leases import lease_task
from test_revision_inputs import accumulated


def context(text, role='controller', brief=None, inputs=None):
    return workflow_blueprint(dict(id=42, title=text, description=''),
        dict(kind='controller' if role=='controller' else 'worker', role_key=role), brief or {}, inputs or {})


@pytest.mark.parametrize('text,kind', [
    ('完整短剧制作：剧本分集、分镜与镜头制作', 'short_drama'),
    ('舞剧大屏静态母版，同场景三视角背景生成', 'stage_background'),
    ('取消短剧制作，改做舞剧背景生成', 'stage_background'),
])
def test_explicit_production_request_selects_correct_workflow(text, kind):
    plan = context(text)
    assert [item['key'] for item in plan['workflows']] == [kind]
    assert plan['matched_requirement'] == {'source':'current_task','task_id':42}
    assert plan['status'] == 'planning_contract_not_completed_review'


@pytest.mark.parametrize('text,role', [
    ('修复短剧项目下载按钮的bug','controller'),
    ('继续排查舞剧项目页面旧UI','controller'),
    ('继续维护安卓客户端网络','controller'),
    ('制作舞剧每幕背景','engineering'),
    ('制作短剧分镜','general_support'),
    ('帮我整理报销单','controller'),
])
def test_unrelated_task_and_wrong_profession_do_not_inherit_production_plan(text, role):
    brief = {'initial_requirement':{'task_id':1,'user_request':'制作舞剧每幕场景背景'}}
    assert context(text, role=role, brief=brief) is None


def test_followup_uses_real_latest_user_requirement_not_assistant_summary():
    brief = {'initial_requirement':{'task_id':1,'user_request':'制作短剧分镜','prior_understanding':'制作舞剧背景'},
             'user_updates':[{'task_id':2,'user_request':'取消短剧制作，改做舞剧每幕背景'}]}
    plan = context('继续，侧面机位不对', brief=brief)
    assert [row['key'] for row in plan['workflows']] == ['stage_background']
    assert plan['matched_requirement']['task_id'] == 2
    brief['user_updates'].append({'task_id':3,'user_request':'暂停舞剧，换个话题'})
    assert context('继续', brief=brief) is None
    assert match_workflows({'title':'继续'}, {'initial_requirement':{'prior_understanding':'制作舞剧每幕背景'}})[0] == []


def test_followup_after_explicit_engineering_topic_does_not_revive_old_production():
    brief = {'initial_requirement':{'task_id':1,'user_request':'制作舞剧每幕背景'},
             'user_updates':[{'task_id':2,'user_request':'排查这个项目页面旧UI，修复下载按钮'}]}
    assert context('继续', brief=brief) is None


def test_static_cleanup_is_optional_but_downstream_consumes_it_when_requested():
    for text, final_stage in [('舞剧背景生成','composite'),('完整短剧制作','finish')]:
        plan = context(text)
        stages = {row['id']:row for row in plan['workflows'][0]['stages']}
        assert 'still_cleanup' not in stages[final_stage]['needs']
        assert 'still_cleanup' in stages[final_stage]['conditional_needs']
        assert stages['still_cleanup']['owner'] == 'visual_production'
        assert 'when' in stages['still_cleanup']


def test_still_derivative_uses_visual_role_without_restarting_creation():
    brief = {'initial_requirement':{'task_id':1,'user_request':'舞剧分幕背景制作'}}
    plan = context('沿用已有母版去人物，其他地方不变', brief=brief)
    assert plan['mode'] == 'source_preserving_derivative'
    assert plan['current_operation_owner'] == 'visual_production'
    assert [row['id'] for row in plan['workflows'][0]['stages']] == ['still_cleanup']
    assert plan['workflows'][0]['stages'][0]['needs'] == []
    assert plan['workflows'][0]['stages'][0]['source_requirements']
    assert 'multiview_audit' not in plan
    assert '不重走全部创作阶段' in plan['scope_note']
    video = context('现有视频进行绿幕抠像和跟踪', 'postproduction', brief=brief)
    assert video['current_operation_owner'] == 'postproduction'
    assert [row['id'] for row in video['workflows'][0]['stages']] == ['composite']
    short_brief = {'initial_requirement':{'task_id':1,'user_request':'完整短剧制作'}}
    short_still = context('沿用已有母版去人物', brief=short_brief)
    assert [row['id'] for row in short_still['workflows'][0]['stages']] == ['still_cleanup']
    short_video = context('现有视频进行绿幕抠像', brief=short_brief)
    assert [row['id'] for row in short_video['workflows'][0]['stages']] == ['finish']
    views = context('沿用已有母版去人物，检查同场景多角度一致性', brief=brief)
    assert views['multiview_audit']['required_for_same_scene_multiple_views']


def test_version_evidence_never_means_approval_or_visual_verification():
    inputs = {'attachments':[dict(id=9,original_filename='master-final.png',sha256='a'*64,
              source_task_id=8,artifact_generation=2)], 'reference_files':[dict(id=3,source_task_id=1,required_unloaded=True)]}
    plan = context('舞剧每幕多角度背景制作', 'art_direction', inputs=inputs)
    evidence = plan['input_evidence']
    assert evidence['bound_manifest'][0]['sha256'] == 'a'*64
    assert evidence['bound_manifest'][0]['artifact_generation'] == 2
    assert evidence['unloaded_references'][0]['required_unloaded']
    assert evidence['visual_comparison'] == 'not_performed_by_this_context_builder'
    assert '不能证明' in '\n'.join(plan['multiview_audit']['checks'])
    assert '旧评议失效' in '\n'.join(plan['multiview_audit']['checks'])
    assert '不额外索要用户逐阶段批准' in '\n'.join(plan['coordination'])
    assert [row['id'] for row in plan['workflows'][0]['stages']] == ['anchor','view_audit']
    assert all(row['owner']=='art_direction' for row in plan['workflows'][0]['stages'])
    assert len(json.dumps(context('完整短剧制作和舞剧每幕背景制作'), ensure_ascii=False)) < 11000


def test_context_prompt_and_later_turn_keep_workflow_without_creating_tasks(repo, team):
    project, device, _, store, controller, designer, *_ = team
    store.update(project['id'], designer['id'], job=designer['job'], instructions=designer['instructions'],
                 device_id=device['id'], active=True, role_key='visual_production')
    first = user_turn(repo, team, text='制作舞剧每幕静帧背景，同场景不同角度必须一致', files=[upload('reference.pdf')])
    later = user_turn(repo, team, text='继续，侧面机位不对，先审核母版再修正', room=first['conversation_id'], suffix='workflow-followup')
    before = repo.connection.total_changes
    plan = store.context(later)['workflow_blueprint']
    assert plan['matched_requirement']['task_id'] == first['id']
    assert plan['input_evidence']['bound_manifest'] == []  # room catalog is not a loaded file
    prompt = store.prompt(later)
    assert 'workflow_blueprint' in prompt and '多视角' in prompt
    assert repo.connection.total_changes == before
    unrelated = user_turn(repo, team, text='继续修复Hub下载按钮的bug', room=first['conversation_id'], suffix='workflow-unrelated')
    assert 'workflow_blueprint' not in store.context(unrelated)
    assert 'workflow_blueprint' not in store.prompt(unrelated)
    assert 'narrative_direction' not in store.context(unrelated)['role_contracts']


def test_readonly_followup_has_no_new_production_plan(repo, team):
    task = user_turn(repo, team, text='制作短剧分镜')
    repo.connection.execute('UPDATE tasks SET controller_read_only=1 WHERE id=?',(task['id'],))
    repo.connection.commit()
    task = repo.get_task(task['id'])
    assert 'workflow_blueprint' not in team[3].context(task)
    assert 'workflow_blueprint' not in team[3].prompt(task)


def test_narrative_role_is_separate_from_visual_and_postproduction():
    assert ROLES['narrative_direction'][0] == '编导'
    assert '不承包美术定调' in ROLES['narrative_direction'][1]
    assert '多角度' in ROLES['art_direction'][1]


def test_workflow_uses_revision_manifest_not_all_historical_delivery_files(repo, accumulated):
    from codex_hub.revision_inputs import task_input_manifest
    d = accumulated
    worker = d['worker']
    d['agents'].update(d['project']['id'], worker['id'], job=worker['job'], instructions=worker['instructions'],
        device_id=d['device']['id'], active=True, role_key='visual_production')
    task = {**d['target'], 'title':'继续舞剧每幕多角度背景制作'}
    expected = task_input_manifest(repo, task)
    before = repo.connection.total_changes
    plan = d['agents'].context(task)['workflow_blueprint']
    actual = plan['input_evidence']['bound_manifest']
    assert {row['id'] for row in actual} == {row['id'] for row in expected['attachments']}
    assert not {row['id'] for row in d['old']}.intersection(row['id'] for row in actual)
    assert {row['sha256'] for row in actual} == {row['sha256'] for row in expected['attachments']}
    assert repo.connection.total_changes == before
