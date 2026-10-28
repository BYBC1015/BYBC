from datetime import datetime, timedelta, timezone

import codex_hub.main as main
from codex_hub.main import annotate_membership_presence
from test_workspace_shell import login
from workspace_fixture import seed_workspace


def test_dedicated_worker_expires_after_two_minutes_without_changing_legacy_clients():
    now=datetime.now(timezone.utc)
    def member(seconds, dedicated=True):
        return dict(device_id=1, device_label='Test connection', agent_tasks_only=dedicated,
                    last_agent_seen_at=(now-timedelta(seconds=seconds)).isoformat(),
                    last_web_seen_at=now.isoformat())
    recent,expired,legacy,future=annotate_membership_presence([
        member(90),member(130),member(130,False),member(-30)])
    assert recent['is_agent_online']
    assert not expired['is_agent_online'] and expired['is_web_online']
    assert '未接入' in expired['presence_label']
    assert legacy['is_agent_online'] and not future['is_agent_online']


def test_heartbeat_expiry_changes_workspace_etag_without_database_write(client,repo,monkeypatch):
    data=seed_workspace(repo,room_messages=0)
    now=datetime.now(timezone.utc)
    repo.connection.execute('INSERT INTO hub_agent_connections(device_id) VALUES (?)',(data['connection']['id'],))
    repo.connection.execute('UPDATE devices SET last_agent_seen_at=? WHERE id=?',(now.isoformat(),data['connection']['id']))
    repo.connection.commit()
    login(client)
    first=client.get('/projects/workspace-team')
    assert client.get('/projects/workspace-team',headers={'If-None-Match':first.headers['etag']}).status_code==304
    class Later(datetime):
        @classmethod
        def now(cls,tz=None):
            return now+timedelta(seconds=121)
    monkeypatch.setattr(main,'datetime',Later)
    expired=client.get('/projects/workspace-team',headers={'If-None-Match':first.headers['etag']})
    assert expired.status_code==200 and expired.headers['etag']!=first.headers['etag']
    assert 'ws-agent-presence-offline' in expired.text
    assert repo.get_task(data['tasks'][0]['id'])['status']=='in_progress'
