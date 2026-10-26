import json

import pytest

from codex_hub.agents import AgentStore


def test_dedicated_agent_worker_does_not_receive_legacy_group_messages(repo):
    desktop = repo.create_device('Existing desktop')
    worker = repo.create_device('Dedicated CLI')
    project = repo.create_project('dedicated-test', 'Team', created_by_device_id=desktop['id'])
    repo.add_project_member(project['id'], worker['id'], 'write')
    repo.connection.execute('INSERT INTO hub_agent_connections VALUES (?)', (worker['id'],))
    repo.connection.commit()
    record = repo.create_entry(project['id'], 'User', 'message', 'Group discussion',
        metadata_json=json.dumps({'routing': {'mode':'group','device_ids':[]}}))
    assert json.loads(record['metadata_json'])['routing']['device_ids'] == [desktop['id']]
    with pytest.raises(ValueError):
        repo.create_entry(project['id'], 'User', 'message', 'Legacy direct message',
            metadata_json=json.dumps({'routing': {'mode':'selected','device_ids':[worker['id']]}}))
    agent = AgentStore(repo.connection).create(project['id'], 'Engineer','Build things',device_id=worker['id'])
    task = repo.create_task(project['id'],'Agent work',assigned_agent_id=agent['id'])
    entry = repo.create_entry(project['id'], 'Engineer','message','Result with @Existing desktop',
                              author_device_id=worker['id'],task_id=task['id'])
    assert json.loads(entry['metadata_json'])['routing'] == {'mode':'none','device_ids':[]}
    assert repo.claim_task(task['id'],worker['id'])
