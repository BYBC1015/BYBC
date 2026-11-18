"""Project library uploads retain authentication and retry atomically."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest
from test_workspace_shell import login


@pytest.fixture
def library(client, repo):
    project = repo.create_project(slug='library-test', name='资料库测试')
    return project, login(client)


def upload(client, csrf, *, key='library-upload-0001', body=b'real file', slug='library-test', accept=True):
    return client.post('/projects/'+slug+'/attachments', data={'_csrf':csrf,'request_key':key},
        files={'file':('brief.txt',body,'text/plain')}, headers={'Accept':'application/json'} if accept else {},
        follow_redirects=False)


def physical(settings):
    return [path for path in settings.files_dir.rglob('*') if path.is_file()]


def test_json_upload_and_retry_return_same_file_without_task(client, repo, settings, library):
    project, csrf = library
    first = upload(client, csrf)
    repeated = upload(client, csrf)
    assert first.status_code == repeated.status_code == 201
    assert first.json()['attachment'] == repeated.json()['attachment']
    assert len(repo.list_attachments(project['id'])) == len(physical(settings)) == 1
    assert repo.list_tasks(project['id']) == []
    attachment = first.json()['attachment']
    assert client.get(attachment['download_url']).content == b'real file'
    assert client.get('/api/mobile/projects/library-test/library').json()['attachments'] == [attachment]


def test_upload_conflict_removes_staged_copy(client, repo, settings, library):
    project, csrf = library
    first = upload(client, csrf)
    conflict = upload(client, csrf, body=b'changed data')
    assert conflict.status_code == 409
    assert '不一致' in conflict.json()['detail']
    assert len(repo.list_attachments(project['id'])) == len(physical(settings)) == 1
    assert client.get(first.json()['attachment']['download_url']).content == b'real file'


def test_upload_auth_csrf_and_invalid_key_do_not_leave_files(client, repo, settings, library):
    project, csrf = library
    assert upload(client, 'bad-token').status_code == 403
    assert upload(client, csrf, key='bad').status_code == 409
    client.cookies.clear()
    assert upload(client, csrf).status_code == 401
    assert repo.list_attachments(project['id']) == []
    assert not physical(settings)


def test_receipt_scope_is_per_project_and_legacy_redirect_is_modern(client, repo, settings, library):
    _, csrf = library
    repo.create_project(slug='another-library',name='另一个项目')
    one = upload(client, csrf)
    other = upload(client, csrf, slug='another-library')
    assert one.json()['attachment']['id'] != other.json()['attachment']['id']
    response = upload(client, csrf, key='', accept=False)
    assert response.status_code == 303 and response.headers['location'] == '/projects/library-test#files'
    assert len(physical(settings)) == 3


def test_concurrent_same_key_upload_is_one_record(client, repo, settings, library):
    project, csrf = library
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:upload(client,csrf), range(2)))
    assert [r.status_code for r in results] == [201,201]
    assert results[0].json()['attachment']['id'] == results[1].json()['attachment']['id']
    assert len(repo.list_attachments(project['id'])) == len(physical(settings)) == 1


def test_receipt_failure_rolls_back_message_attachment_and_staged_file(client, repo, settings, library):
    project, csrf = library
    assert upload(client,csrf).status_code == 201
    original_entries=repo.connection.execute('SELECT COUNT(*) FROM entries WHERE project_id=?',(project['id'],)).fetchone()[0]
    repo.connection.execute("CREATE TRIGGER fail_library_receipt BEFORE INSERT ON hub_library_uploads BEGIN SELECT RAISE(FAIL,'receipt failure'); END")
    repo.connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match='receipt failure'):
        upload(client,csrf,key='another-request-0001')
    assert repo.connection.execute('SELECT COUNT(*) FROM entries WHERE project_id=?',(project['id'],)).fetchone()[0] == original_entries
    assert len(repo.list_attachments(project['id'])) == len(physical(settings)) == 1
