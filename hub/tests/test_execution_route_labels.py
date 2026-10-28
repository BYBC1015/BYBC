"""Execution surfaces stay understandable on existing task metadata rows."""
import re

import pytest

from test_workspace_shell import login
from workspace_fixture import seed_workspace


@pytest.mark.parametrize('route,label', [
    ('project','本地工程'), ('direct','单次执行'), ('chat','聊天协调'),
])
def test_execution_route_is_visible_in_task_details_and_restored_work_card(client,repo,route,label):
    data=seed_workspace(repo,room_messages=0)
    task=data['tasks'][1]
    # Explicit restore makes a chat record visible when the owner wants it on
    # the board; normal chat records are filtered by the board's own contract.
    repo.connection.execute("UPDATE tasks SET execution_route=?,execution_route_reason=?,board_state='active' WHERE id=?",
                            (route,'适合本次工作',task['id']))
    repo.connection.commit()
    login(client)
    marker=f'<span data-execution-route="{route}">{label}</span>'
    detail=client.get(f"/projects/workspace-team/tasks/{task['id']}")
    assert detail.status_code==200 and marker in detail.text
    workspace=client.get(f"/projects/workspace-team/conversations/{data['conversation_id']}")
    assert workspace.status_code==200
    card=re.search(fr'<article[^>]*data-ws-task-card="{task["id"]}".*?</article>',workspace.text,re.S)
    assert card and marker in card.group(0)
    inspector=re.search(fr'<section data-ws-task-panel="{task["id"]}".*?</section>',workspace.text,re.S)
    assert inspector and marker in inspector.group(0)
