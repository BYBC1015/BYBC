import hashlib
import io
import json
import subprocess
import threading
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.error import HTTPError

import pytest
from PIL import Image

from codex_bridge.agent_runtime import run_agent_request
from codex_bridge.attachments import AttachmentPreparationError, prepare_task_attachments
from codex_bridge.client import HubClient, HubClientError
from codex_bridge.codex_exec import CodexExecRunner
from codex_bridge.runner import BridgeRunner, ProcessResult
from test_agent_runtime import AgentClient, build_runner, config_for
from test_codex_bridge import FakeExecutor
from test_codex_exec import FakeProcessRunner


def item(data, *, attachment_id=1, name='input.txt', task_id=42, content_type='text/plain'):
    return {'id': attachment_id, 'original_filename': name, 'byte_size': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'task_id': task_id,
            'content_type': content_type}


def png():
    stream = io.BytesIO()
    Image.new('RGB', (20, 12), 'red').save(stream, 'PNG')
    return stream.getvalue()


class AttachmentClient(AgentClient):
    def __init__(self, files):
        super().__init__(existing=True)
        self.files = files
        self.manifest_calls = 0
        self.downloads = []

    def get_task_attachments(self, slug, task_id):
        self.manifest_calls += 1
        return {'attachments': [metadata for metadata, data in self.files]}

    def download_task_attachment(self, slug, attachment_id, target, **kwargs):
        self.downloads.append((slug, attachment_id, target))
        target.write_bytes(next(data for metadata, data in self.files if metadata['id'] == attachment_id))


class ImageExecutor(FakeExecutor):
    def run(self, *args, image_paths=None, execution_context=None, cancel_event=None, **kwargs):
        self.images = image_paths
        self.execution_context = execution_context
        return super().run(*args, **kwargs)


@contextmanager
def server(responder):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            responder(self)

        def log_message(self, *args):
            pass

    instance = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{instance.server_port}/hub'
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


def test_attachment_download_uses_fixed_hub_path_and_checks_stream(tmp_path):
    data = b'actual task input'
    requests = []

    def respond(request):
        requests.append(request.path)
        assert request.headers['Authorization'] == 'Bearer private-test-credential'
        request.send_response(200)
        request.send_header('Content-Length', str(len(data)))
        request.end_headers()
        request.wfile.write(data)

    target = tmp_path / 'input.txt'
    with server(respond) as hub:
        client = HubClient(hub, 'private-test-credential')
        client.download_task_attachment('agent-test', 17, target, expected_size=len(data),
            expected_sha256=hashlib.sha256(data).hexdigest(), max_bytes=100)
    assert target.read_bytes() == data
    assert requests == ['/hub/api/projects/agent-test/attachments/17']
    assert not list(tmp_path.glob('*.part'))


@pytest.mark.parametrize('failure', ['redirect', 'hash', 'overflow', 'http'])
def test_download_rejects_bad_inputs_without_leaking_credentials_or_partial_file(tmp_path, failure):
    data = b'abc'
    redirected_requests = []

    def other(request):
        redirected_requests.append(request.path)
        request.send_response(200)
        request.end_headers()

    with server(other) as other_hub:
        def respond(request):
            if failure == 'redirect':
                request.send_response(302)
                request.send_header('Location', other_hub + '/steal')
            elif failure == 'http':
                request.send_response(403)
            else:
                request.send_response(200)
            request.end_headers()
            if failure == 'http':
                request.wfile.write(b'private-test-credential')
            elif failure == 'overflow':
                request.wfile.write(b'abcdef')
            else:
                request.wfile.write(data)

        with server(respond) as hub:
            client = HubClient(hub, 'private-test-credential')
            with pytest.raises(HubClientError) as error:
                client.download_task_attachment('agent-test', 17, tmp_path / 'input', expected_size=3,
                    expected_sha256='0' * 64 if failure == 'hash' else hashlib.sha256(data).hexdigest(), max_bytes=3)
    assert 'private-test-credential' not in str(error.value)
    assert redirected_requests == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('hub,slug', [
    ('https://user:secret@hub.example', 'agent-test'),
    ('https://hub.example/hub?download=', 'agent-test'),
    ('file:///tmp/hub', 'agent-test'),
    ('https://hub.example', '../other'),
    ('https://hub.example', 'agent-test/../../escape'),
])
def test_attachment_urls_cannot_escape_configured_hub(tmp_path, hub, slug):
    with pytest.raises(HubClientError):
        HubClient(hub, 'secret').get_task_attachments(slug, 42)


def test_manifest_is_bounded_and_requires_an_attachment_list():
    def respond(request):
        request.send_response(200)
        request.end_headers()
        request.wfile.write(b'{"attachments":{}}')

    with server(respond) as hub:
        with pytest.raises(HubClientError):
            HubClient(hub, 'secret').get_task_attachments('agent-test', 42)


def test_verified_inputs_are_task_isolated_readable_and_cached(tmp_path):
    image, text = png(), '客户要求：保持主工作区简洁。'.encode('utf-8')
    client = AttachmentClient([
        (item(image, name='../../outside/screenshot.png', content_type='image/png'), image),
        (item(text, attachment_id=2, name='brief.txt'), text),
    ])
    config = config_for(tmp_path)
    config.token = 'private-credential-never-in-prompts-79413'
    project = config.projects['agent-test']
    first = prepare_task_attachments(client, config, 'agent-test', 42, project)
    assert len(first.images) == 1 and first.images[0].is_file()
    assert project in first.images[0].parents
    assert first.images[0].name == '1-screenshot.png'
    assert 'task-42' in str(first.images[0])
    assert Path(first.files[1]['text_preview_path']).read_text(encoding='utf-8') == text.decode()
    assert config.token not in first.prompt()
    assert '--image' in first.prompt()
    prepare_task_attachments(client, config, 'agent-test', 42, project)
    assert len(client.downloads) == 2
    for metadata, _ in client.files:
        metadata['task_id'] = 43
    second = prepare_task_attachments(client, config, 'agent-test', 43, project)
    assert second.images[0].parent != first.images[0].parent
    assert len(client.downloads) == 4


def test_docx_is_read_as_text_without_extracting_or_executing_archive_members(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>实际文档正文</w:t></w:r></w:p></w:body></w:document>')
        archive.writestr('../../escape.py', 'raise Exception("must not run")')
    data = buffer.getvalue()
    config = config_for(tmp_path)
    bundle = prepare_task_attachments(AttachmentClient([(item(data, name='需求.docx'), data)]),
                                     config, 'agent-test', 42, config.projects['agent-test'])
    assert Path(bundle.files[0]['text_preview_path']).read_text(encoding='utf-8') == '实际文档正文'
    assert not (tmp_path / 'escape.py').exists()


@pytest.mark.parametrize('corruption', ['unsupported_compression', 'invalid_deflate'])
def test_corrupt_docx_records_blocked_once_without_reclaim_loop(tmp_path, corruption):
    source = io.BytesIO()
    with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', '<document><p>task input</p></document>')
    data = bytearray(source.getvalue())
    central = data.index(b'PK\x01\x02')
    if corruption == 'unsupported_compression':
        data[8:10] = (1234).to_bytes(2, 'little')
        data[central + 10:central + 12] = (1234).to_bytes(2, 'little')
    else:
        name_length = int.from_bytes(data[26:28], 'little')
        extra_length = int.from_bytes(data[28:30], 'little')
        offset = 30 + name_length + extra_length
        data[offset:central] = b'\xff' * (central - offset)
    raw = bytes(data)
    client = AttachmentClient([(item(raw, name='broken.docx'), raw)])
    runner, _, _, executor = build_runner(tmp_path, client=client)
    runner.process_once()
    runner.process_once()
    assert not executor.calls
    assert client.manifest_calls == 1
    assert client.task_status_updates[-1][2] == 'blocked'
    assert runner.state.get_task_delivery('agent-test', 42)['status_reported']


@pytest.mark.parametrize('invalid', ['foreign_task', 'bad_hash', 'bad_image', 'many', 'large', 'total'])
def test_invalid_or_excess_inputs_never_reach_executor(tmp_path, invalid):
    data = b'content'
    metadata = item(data)
    files = [(metadata, data)]
    if invalid == 'foreign_task':
        metadata['task_id'] = 7
    elif invalid == 'bad_hash':
        metadata['sha256'] = '0' * 64
    elif invalid == 'bad_image':
        metadata['original_filename'] = 'not-a-real-image.png'
    elif invalid == 'many':
        files = [(item(data, attachment_id=i), data) for i in range(1, 14)]
    elif invalid == 'large':
        metadata['byte_size'] = 51 * 1024 * 1024
    elif invalid == 'total':
        files = [(dict(item(data, attachment_id=i), byte_size=40 * 1024 * 1024), data) for i in range(1, 4)]
    client = AttachmentClient(files)
    runner, _, decider, executor = build_runner(tmp_path, client=client)
    runner.process_once()
    first_calls = client.manifest_calls
    runner.process_once()
    assert not executor.calls and not decider.calls
    assert client.task_status_updates[-1][2] == 'blocked'
    assert client.manifest_calls == first_calls
    assert '附件' in client.reflections[-1]['failed']
    assert client.fallback_calls == 0


def test_input_directories_cannot_follow_symlinks_into_other_tasks(tmp_path):
    config = config_for(tmp_path)
    project = config.projects['agent-test']
    other = project / 'other-task'
    other.mkdir()
    try:
        (project / '.codex-hub-inputs').symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip('Host cannot create test symlinks')
    data = b'input'
    client = AttachmentClient([(item(data), data)])
    with pytest.raises(AttachmentPreparationError):
        prepare_task_attachments(client, config, 'agent-test', 42, project)
    assert client.downloads == []
    assert list(other.iterdir()) == []


def test_runner_passes_image_and_text_to_cli_once_and_preserves_receipt(tmp_path):
    image, text = png(), b'actual brief'
    client = AttachmentClient([(item(image, name='design.png', content_type='image/png'), image),
                               (item(text, attachment_id=2), text)])
    executor = ImageExecutor()
    runner, _, _, _ = build_runner(tmp_path, client=client, executor=executor)
    client.fail_result_once = True
    with pytest.raises(HubClientError):
        runner.process_once()
    assert len(executor.calls) == 1 and len(executor.images) == 1
    prompt = executor.calls[0][3]
    assert json.dumps(str(executor.images[0])) in prompt
    assert 'text_preview_path' in prompt and 'attachment_id' in prompt
    runner.process_once()
    assert len(executor.calls) == 1 and len(client.downloads) == 2


def test_codex_exec_sends_verified_local_image_argument(tmp_path):
    config = config_for(tmp_path)
    config.codex_cli_path = tmp_path / 'codex.exe'
    config.codex_cli_path.write_bytes(b'fake')
    project = config.projects['agent-test']
    image = project / 'image.png'
    image.write_bytes(png())
    process = FakeProcessRunner()
    execution = CodexExecRunner(config, process).run('agent-test', project, tmp_path / 'prompt.md',
                                                   'read the actual image', image_paths=[image])
    assert execution.ok
    command = process.calls[0][0]
    assert command[command.index('--image') + 1] == str(image.resolve())
    assert command[-1] == '-'
    outside = tmp_path / 'outside.png'
    outside.write_bytes(png())
    rejected = CodexExecRunner(config, process).run('agent-test', project, tmp_path / 'prompt.md',
                                                  'no outside image', image_paths=[outside])
    assert not rejected.ok and len(process.calls) == 1


def test_delegate_helper_allows_explicit_subset_or_no_attachments(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    requests = []
    monkeypatch.setattr('codex_bridge.config.load_config', lambda _: config)
    monkeypatch.setattr('codex_bridge.hub_reply.select_runtime_config', lambda value, binding: value)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def get_agent_context(self, *args):
            return {'agent': {'kind': 'controller'}, 'attachments': [{'id': 7}, {'id': 8}]}

        def _json(self, *args):
            requests.append(args)
            return {'task': {'id': 15, 'status': 'open'}}

    monkeypatch.setattr('codex_bridge.client.HubClient', Client)
    path = tmp_path / 'request.json'
    args = SimpleNamespace(config=tmp_path / 'config.json', binding_id='', project='agent-test',
                           task_id=42, request_file=path)
    base = {'action': 'delegate', 'assigned_agent_id': 9, 'title': 'Read requested files'}
    for attachments in ([8], []):
        path.write_text(json.dumps({**base, 'attachment_ids': attachments}), encoding='utf-8')
        assert run_agent_request(args)['task']['status'] == 'open'
        assert requests[-1][2]['attachment_ids'] == attachments
    assert requests[0][2]['request_key'] != requests[1][2]['request_key']
    path.write_text(json.dumps({**base, 'attachment_ids': [999]}), encoding='utf-8')
    with pytest.raises(ValueError, match='当前任务'):
        run_agent_request(args)
    assert len(requests) == 2


def test_only_dedicated_worker_reports_machine_identity(monkeypatch):
    from codex_bridge.agent_worker import AgentOnlyHubClient

    sent = []
    monkeypatch.setattr('codex_bridge.agent_worker.platform.node', lambda: 'BYBC-YOLO')
    monkeypatch.setattr(HubClient, '_json', lambda self, *args: sent.append(args) or {})
    dedicated = AgentOnlyHubClient('https://hub.example', 'secret')
    dedicated.post_device_capabilities({'execution_mode': 'codex-cli'})
    assert sent[-1][2]['machine_name'] == 'BYBC-YOLO'
    assert sent[-1][2]['runtime_kind'] == 'durable-agent'
    dedicated.machine_name = 'BYBC-PRO'
    dedicated.post_device_capabilities({})
    assert sent[-1][2]['machine_name'] == 'BYBC-PRO'
    HubClient('https://hub.example', 'secret').post_device_capabilities({'execution_mode': 'codex-cli'})
    assert 'machine_name' not in sent[-1][2] and 'runtime_kind' not in sent[-1][2]


def test_workspace_upload_reaches_real_task_manifest_local_files_and_cli_arguments(client, repo, tmp_path, monkeypatch):
    monkeypatch.setattr('codex_bridge.model_allocator.allocate_task_model', lambda *args: {})
    from test_agents import token_headers
    from test_workspace_shell import login
    from workspace_fixture import seed_workspace

    data = seed_workspace(repo, room_messages=0)
    csrf = login(client)
    image, brief = png(), '真正传递给执行者的任务附件'.encode('utf-8')
    response = client.post('/projects/workspace-team/workspace-dispatch',
        data={'_csrf': csrf, 'agent_ids': str(data['engineer']['id']),
              'description': '检查附件中的截图与需求说明', 'request_key': 'runtime-e2e-attachment-0001'},
        files=[('attachments', ('screen.png', image, 'image/png')),
               ('attachments', ('brief.txt', brief, 'text/plain'))], follow_redirects=False)
    assert response.status_code == 303
    room_id = int(response.headers['location'].rsplit('/', 1)[-1])
    task = next(t for t in repo.list_tasks(data['project']['id']) if t['conversation_id'] == room_id)
    headers = token_headers(repo, data['connection'])
    config = config_for(tmp_path)
    config.hub, config.token = 'http://testserver', headers['Authorization'].removeprefix('Bearer ')
    config.projects = {'workspace-team': config.projects['agent-test']}
    hub = HubClient(config.hub, config.token)

    def request(method, path, body=None):
        result = client.request(method, path, json=body, headers=headers)
        if result.status_code >= 400:
            raise HubClientError(f'Isolated Hub request failed: {result.status_code}')
        return result.content

    class Download(io.BytesIO):
        def __init__(self, result, url):
            super().__init__(result.content)
            self.headers, self.url = result.headers, url

        def geturl(self):
            return self.url

    def download(url):
        result = client.get(urlsplit(url).path, headers=headers, follow_redirects=False)
        if result.status_code >= 400:
            raise HTTPError(url, result.status_code, 'Isolated Hub response', result.headers, io.BytesIO())
        return Download(result, url)

    hub._request, hub._open_attachment = request, download
    executor = ImageExecutor()
    runner = BridgeRunner(config, client=hub, executor=executor)
    result = ProcessResult()
    runner._process_task({**task, 'project_slug': 'workspace-team'}, 'isolated-worker', result)
    assert len(executor.calls) == 1 and len(executor.images) == 1
    assert executor.images[0].read_bytes() == image
    prepared_paths = list(config.projects['workspace-team'].rglob('*.preview.txt'))
    assert len(prepared_paths) == 1 and prepared_paths[0].read_text(encoding='utf-8') == brief.decode()
    assert config.token not in executor.calls[0][3]
    assert repo.get_task(task['id'])['status'] == 'review'
    assert len(result.executed_codex) == 1


def test_dedicated_heartbeat_continues_during_blocked_execution_without_redelivery(tmp_path, capsys):
    from codex_bridge.agent_worker import AgentWorkerHeartbeat

    started, release, heartbeats = threading.Event(), threading.Event(), threading.Event()

    class BlockingExecutor(FakeExecutor):
        def run(self, *args, **kwargs):
            started.set()
            assert release.wait(5)
            return super().run(*args, **kwargs)

    class HeartbeatClient:
        timeout_seconds = 1
        calls = 0

        def heartbeat(self):
            self.calls += 1
            if self.calls <= 2:
                raise HubClientError('Do not print private-network-credential')
            heartbeats.set()

    executor = BlockingExecutor()
    runner, client, _, _ = build_runner(tmp_path, executor=executor)
    heartbeat_client = HeartbeatClient()
    heartbeat = AgentWorkerHeartbeat(heartbeat_client, interval_seconds=0.01)
    execution_errors = []

    def run():
        try:
            runner.process_once()
        except Exception as exc:
            execution_errors.append(exc)

    execution = threading.Thread(target=run)
    heartbeat.start()
    execution.start()
    try:
        assert started.wait(3)
        assert heartbeats.wait(3)
        assert execution.is_alive() and heartbeat_client.calls >= 3
        assert len(client.claimed_tasks) == 1
    finally:
        release.set()
        execution.join(timeout=5)
        heartbeat.close()
    assert not execution_errors and not heartbeat.thread.is_alive()
    runner.process_once()
    assert len(executor.calls) == 1
    assert 'private-network-credential' not in capsys.readouterr().out
