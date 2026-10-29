"""Read-only timing shows silence and contact separately, without invented progress."""
from datetime import datetime, timezone
from types import SimpleNamespace
import re

import codex_hub.execution_progress as progress
from codex_hub.execution_leases import ExecutionStore
from test_execution_leases import lease_task
from test_execution_progress import running, post
from test_workspace_shell import login, workspace_contexts


def _seed(client, repo, data, at):
    _, run = running(repo, data)
    assert post(client, repo, data, run, kind='status', text='正在执行工具').status_code == 200
    repo.connection.execute('UPDATE hub_task_executions SET started_at=?,lease_until=? WHERE task_id=?',
                           (at-1200, at+60, data['task']['id']))
    repo.connection.execute('UPDATE hub_execution_progress SET updated_at=? WHERE task_id=?', (at-90,data['task']['id']))
    repo.connection.execute('UPDATE devices SET last_agent_seen_at=? WHERE id=?',
                           (datetime.fromtimestamp(at-15,timezone.utc).isoformat(),data['device']['id']))
    repo.connection.commit()


def test_age_refresh_does_not_write_new_progress_or_claim_tool_running(client, repo, lease_task, monkeypatch):
    d=lease_task; at=1800000000.0
    _seed(client, repo, d, at)
    before=dict(repo.connection.execute('SELECT * FROM hub_execution_progress').fetchone())
    original_version=repo.connection.execute('SELECT version FROM hub_project_event_versions WHERE project_id=?',(d['project']['id'],)).fetchone()[0]
    row=progress.active_progress(repo.connection,d['project']['id'],clock=lambda:at)[0]
    assert row['run_age_label']=='20 分 0 秒' and row['report_age_label']=='1 分 30 秒'
    assert row['execution_contact_valid'] and row['device_contact_recent']
    monkeypatch.setattr(progress,'time',SimpleNamespace(time=lambda:at))
    version=progress.project_event_version(repo.connection,d['project']['id'])
    monkeypatch.setattr(progress,'time',SimpleNamespace(time=lambda:at+14))
    assert progress.project_event_version(repo.connection,d['project']['id'])==version
    monkeypatch.setattr(progress,'time',SimpleNamespace(time=lambda:at+15))
    assert progress.project_event_version(repo.connection,d['project']['id'])!=version
    later=progress.active_progress(repo.connection,d['project']['id'],clock=lambda:at+15)[0]
    assert later['report_age_label']=='1 分 45 秒'
    assert later['sequence']==row['sequence'] and later['status_text']==row['status_text']=='正在执行工具'
    assert dict(repo.connection.execute('SELECT * FROM hub_execution_progress').fetchone())==before
    assert repo.connection.execute('SELECT version FROM hub_project_event_versions WHERE project_id=?',(d['project']['id'],)).fetchone()[0]==original_version


def test_expired_task_contact_can_be_shown_without_claiming_new_progress(client, repo, lease_task):
    d=lease_task;at=1800000000.0;_seed(client,repo,d,at)
    assert progress.active_progress(repo.connection,d['project']['id'],clock=lambda:at+601)==[]
    row=progress.active_progress(repo.connection,d['project']['id'],clock=lambda:at+601,include_expired=True)[0]
    assert not row['execution_contact_valid'] and not row['device_contact_recent']
    assert row['status_text']=='正在执行工具' and row['sequence']==1
    assert repo.get_task(d['task']['id'])['status']=='in_progress'
    repo.update_task_status(d['task']['id'],'cancelled','test cancellation')
    assert not progress.active_progress(repo.connection,d['project']['id'],clock=lambda:at+601,include_expired=True)


def test_no_periodic_event_tick_when_project_has_no_active_progress(repo, lease_task, monkeypatch):
    project=lease_task['project']['id']
    monkeypatch.setattr(progress,'time',SimpleNamespace(time=lambda:1800000000.0))
    before=progress.project_event_version(repo.connection,project)
    monkeypatch.setattr(progress,'time',SimpleNamespace(time=lambda:1800000030.0))
    assert progress.project_event_version(repo.connection,project)==before


def test_workspace_labels_expired_connection_as_unconfirmed(client,repo,lease_task,workspace_contexts):
    d=lease_task;at=1.0;_seed(client,repo,d,at);login(client)
    room=repo.get_task(d['task']['id'])['conversation_id']
    response=client.get(f"/projects/{d['project']['slug']}/conversations/{room}")
    assert response.status_code==200
    current=workspace_contexts[-1]['conversation_progress'][0]
    assert not current['execution_contact_valid']
    assert '执行状态待核对' in response.text and '任务联系已过期' in response.text
    # The report text has its own span so age refreshes preserve the body DOM.
    assert '最近报告：正在执行工具' in re.sub(r'<[^>]+>', '', response.text)
