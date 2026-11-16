"""Real loopback HTTP services verify the API adapter without paid requests."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from codex_bridge.api_model import APIModelRunner, APIModelError
from codex_bridge.client import HubClientError
from codex_bridge.agent_runtime import read_execution_completion
from test_agent_runtime import config_for, REFLECTION


SECRET = 'synthetic-model-private-key-123'


def answer(text='任务已处理', calls=None, *, finish=None):
    return {'choices': [{'finish_reason': finish or ('tool_calls' if calls else 'stop'),
        'message': {'role': 'assistant', 'content': text, **({'tool_calls': calls} if calls else {})}}],
        'usage': {'prompt_tokens': 12, 'completion_tokens': 3, 'prompt_tokens_details': {'cached_tokens': 4}}}


def tool(name, arguments, identifier='call-1'):
    return {'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}


@contextmanager
def model_server(replies):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append({'body': body, 'path': self.path, 'authorization': self.headers.get('Authorization')})
            index = len(received) - 1
            spec = replies[min(index, len(replies) - 1)]
            if spec.get('delay'):
                time.sleep(spec['delay'])
            self.send_response(spec.get('status', 200))
            for key, value in spec.get('headers', {}).items():
                self.send_header(key, value)
            try:
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                return
            raw = spec.get('raw')
            if raw is None:
                raw = json.dumps(spec.get('body', answer())).encode('utf-8')
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class Hub:
    def __init__(self, endpoint):
        self.service = {'id': 7, 'revision': 3, 'kind': 'openai_compatible', 'base_url': endpoint,
                        'model': 'example-model', 'api_key': SECRET, 'supports_tools': True,
                        'supports_images': True, 'timeout_seconds': 3, 'max_tokens': 512}
        self.calls = []
        self.failure = None

    def get_agent_context(self, slug, task_id):
        return {'agent': {'id': 4, 'kind': 'controller'},
                'task': {'id': task_id, 'conversation_id': 12, 'description': '完整当前要求'},
                'team': [{'id': 9, 'kind': 'worker', 'name': '执行者'}]}

    def _json(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith('/model-service'):
            return {'service': self.service}
        if path.endswith('/delegate'):
            if self.failure:
                raise self.failure
            return {'task': {'id': 15}, 'created': True}
        if path.endswith('/revise'):
            return {'amendment': {'id': 16, 'mode': 'updated'}}
        if path.endswith('/tasks'):
            return {'tasks': [{'id': 11, 'conversation_id': 12, 'description': '原任务完整要求'}]}
        if path.endswith('/tasks/11'):
            return {'task': {'id': 11, 'description': '原任务完整要求'}}
        raise AssertionError('Unexpected Hub endpoint: ' + path)


def setup(tmp_path, endpoint):
    config = config_for(tmp_path)
    hub = Hub(endpoint)
    context = {'task_id': 11, 'project_slug': 'agent-test', 'generation': 2,
               'run_key': 'synthetic-run-key-0123456789', 'execution_route': 'chat',
               'assigned_agent_kind': 'controller', 'receipt_path': str(tmp_path / 'completion.json')}
    return config, hub, context


def execute(config, hub, context, *, images=None, cancel=None, prompt='完成本轮任务'):
    return APIModelRunner(config, hub).run('agent-test', config.projects['agent-test'],
        config.prompt_dir / 'task.md', prompt, execution_preferences={'model': 'hub-service/7', 'service_revision': 3},
        execution_context=context, image_paths=images, cancel_event=cancel)


@pytest.fixture(autouse=True)
def loopback_proxy_exclusion(monkeypatch):
    # Isolated test-process configuration only; production follows environment.
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')


def test_real_api_multiround_tools_preserve_fence_and_usage_and_reuse_completion(tmp_path):
    replies = [
        {'body': answer('', [tool('inspect', {}, 'inspect')])},
        {'body': answer('', [tool('get_task', {'task_id': 11}, 'read')])},
        {'body': answer('', [tool('delegate', {'assigned_agent_id': 9, 'title': '交付结果',
                                           'description': '验收标准'}, 'delegate')])},
        {'body': answer('已安排任务 #15\n<hub-reflection>' + json.dumps(REFLECTION) + '</hub-reflection>')},
    ]
    with model_server(replies) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        result = execute(config, hub, context)
        assert result.ok, result.error
        assert len(received) == 4 and all(row['path'] == '/v1/chat/completions' for row in received)
        assert all(row['authorization'] == 'Bearer ' + SECRET for row in received)
        assert all(SECRET not in json.dumps(row['body']) for row in received)
        mutation = next(body for method, path, body in hub.calls if path.endswith('/delegate'))
        assert mutation['execution'] == {'generation': 2, 'run_key': context['run_key']}
        assert len(mutation['request_key']) == 64
        assert result.token_usage == {'input_tokens': 48, 'cached_input_tokens': 16, 'output_tokens': 12}
        receipt = read_execution_completion(Path(context['receipt_path']), context)
        assert receipt['ok'] and receipt['service_revision'] == 3
        repeated = execute(config, hub, context)
        assert repeated.ok and len(received) == 4
        assert len([call for call in hub.calls if call[1].endswith('/delegate')]) == 1
        assert all(SECRET not in path.read_text(encoding='utf-8') for path in tmp_path.glob('completion*'))


def test_real_api_image_input_and_cli_instructions_are_not_sent(tmp_path):
    with model_server([{'body': answer()}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        picture = config.projects['agent-test'] / 'preview.png'
        picture.write_bytes(b'synthetic-png-bytes')
        context['task_prompt_chars'] = len('请分析附件')
        result = execute(config, hub, context, images=[picture],
                         prompt='请分析附件\n\n## Hub 智能体工作接口\nPowerShell secret-local-path')
        assert result.ok
        content = received[0]['body']['messages'][1]['content']
        assert content[0]['text'] == '请分析附件'
        assert content[1]['image_url']['url'].startswith('data:image/png;base64,')
        assert 'secret-local-path' not in json.dumps(received[0]['body'])


@pytest.mark.parametrize('spec,fragment', [
    ({'status': 429, 'raw': SECRET.encode()}, '限额'),
    ({'status': 401, 'raw': SECRET.encode()}, '认证'),
    ({'raw': b'{malformed'}, '格式'),
    ({'raw': b'null'}, '无效'),
    ({'body': answer(f'partial {SECRET}', finish='length')}, '完整'),
    ({'delay': 1.4, 'body': answer()}, '超时'),
    ({'status': 302, 'headers': {'Location': 'http://127.0.0.1:1/steal'}}, '重定向'),
])
def test_http_failures_are_terminal_redacted_and_never_retried(tmp_path, spec, fragment):
    with model_server([spec]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        hub.service['timeout_seconds'] = 1
        result = execute(config, hub, context)
        assert not result.ok and fragment in result.error
        assert len(received) == 1 and SECRET not in result.error
        repeated = execute(config, hub, context)
        assert not repeated.ok and len(received) == 1
        assert all(SECRET not in path.read_text(encoding='utf-8') for path in tmp_path.glob('completion*'))


def test_cancellation_during_model_request_never_runs_returned_tools(tmp_path):
    reply = answer('', [tool('delegate', {'title': 'do not create', 'description': 'cancelled'})])
    with model_server([{'delay': .5, 'body': reply}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        cancelled = threading.Event()
        timer = threading.Timer(.1, cancelled.set)
        timer.start()
        try:
            started = time.monotonic()
            result = execute(config, hub, context, cancel=cancelled)
            assert not result.ok and '撤销' in result.error and time.monotonic() - started < .5
            time.sleep(.55)
            assert len(received) == 1 and not any(call[1].endswith('/delegate') for call in hub.calls)
        finally:
            timer.cancel()


@pytest.mark.parametrize('case', ['project', 'revision', 'image_support'])
def test_preflight_blocks_unsupported_or_unverified_execution_before_inference(tmp_path, case):
    with model_server([{'body': answer()}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        images = None
        if case == 'project':
            context['execution_route'] = 'project'
        elif case == 'revision':
            hub.service['revision'] = 4
        elif case == 'image_support':
            hub.service['supports_images'] = False
            picture = config.projects['agent-test'] / 'preview.png'
            picture.touch()
            images = [picture]
        result = execute(config, hub, context, images=images)
        assert not result.ok and received == []


def test_unconfirmed_tool_write_stops_inference_and_preserves_request_key(tmp_path):
    with model_server([{'body': answer('', [tool('delegate', {'title': '任务', 'description': '范围'})])}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        hub.failure = HubClientError('unknown outcome ' + SECRET)
        result = execute(config, hub, context)
        assert not result.ok and '尚未确认' in result.error
        assert len(received) == 1 and len([call for call in hub.calls if call[1].endswith('/delegate')]) == 1
        journal = json.loads(Path(context['receipt_path']).with_suffix('.api-journal.json').read_text(encoding='utf-8'))
        assert journal['steps'][-1]['state'] == 'pending' and journal['steps'][-1]['request_key']
        assert SECRET not in json.dumps(journal)


def test_rejected_tool_parameters_can_be_corrected_without_repeating_rejected_mutation(tmp_path):
    replies = [{'body': answer('', [tool('get_task', {'task_id': 'invalid'})])}, {'body': answer('需要有效任务编号')}]
    with model_server(replies) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        result = execute(config, hub, context)
        assert result.ok and len(received) == 2
        assert not any(call[1].endswith('/tasks/invalid') for call in hub.calls)
        assert '正整数' in received[-1]['body']['messages'][-1]['content']


def test_actual_model_echoed_secrets_are_redacted_from_saved_result(tmp_path):
    with model_server([{'body': answer('Do not expose ' + SECRET)}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        result = execute(config, hub, context)
        assert result.ok and SECRET not in result.last_message and '[REDACTED]' in result.last_message
        assert all(SECRET not in path.read_text(encoding='utf-8') for path in tmp_path.glob('completion*'))


def test_local_service_without_api_key_sends_no_authorization(tmp_path):
    with model_server([{'body': answer()}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        hub.service.update(api_key='', kind='ollama')
        assert execute(config, hub, context).ok
        assert received[0]['authorization'] is None


@pytest.mark.parametrize('same_id', [True, False])
def test_model_repeated_mutation_reuses_first_confirmed_result(tmp_path, same_id):
    arguments = {'title': 'single task', 'description': 'same complete requirement'}
    first = tool('delegate', arguments, 'first-call')
    second = tool('delegate', arguments, 'first-call' if same_id else 'another-call')
    with model_server([{'body': answer('', [first])}, {'body': answer('', [second])}, {'body': answer()}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        result = execute(config, hub, context)
        assert result.ok and len(received) == 3
        assert len([call for call in hub.calls if call[1].endswith('/delegate')]) == 1
        assert json.loads(received[-1]['body']['messages'][-1]['content'])['task']['id'] == 15


def test_model_cannot_change_arguments_of_reused_tool_identifier(tmp_path):
    replies = [{'body': answer('', [tool('delegate', {'title': 'first', 'description': 'one'})])},
               {'body': answer('', [tool('delegate', {'title': 'changed', 'description': 'two'})])}]
    with model_server(replies) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        result = execute(config, hub, context)
        assert not result.ok and '不同参数' in result.error
        assert len([call for call in hub.calls if call[1].endswith('/delegate')]) == 1


def test_worker_cannot_obtain_controller_mutation_tools(tmp_path):
    with model_server([{'body': answer('', [tool('delegate', {'title': 'forbidden', 'description': 'no'})])},
                       {'body': answer('不能委派')}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        context['assigned_agent_kind'] = 'worker'
        result = execute(config, hub, context)
        assert result.ok
        assert {entry['function']['name'] for entry in received[0]['body']['tools']} == {'inspect', 'get_task'}
        assert not any(call[1].endswith('/delegate') for call in hub.calls)


def test_tool_requests_redact_credentials_before_hub_mutation(tmp_path):
    with model_server([{'body': answer('', [tool('delegate', {'title': 'work', 'description': SECRET})])},
                       {'body': answer()}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        assert execute(config, hub, context).ok
        mutation = next(body for _, path, body in hub.calls if path.endswith('/delegate'))
        assert SECRET not in json.dumps(mutation) and mutation['description'] == '[REDACTED]'


def test_hallucinated_artifact_manifest_cannot_publish_local_files(tmp_path):
    body = '回复正文\n<hub-artifacts>[{"path":"private.md"}]</hub-artifacts>'
    with model_server([{'body': answer(body)}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        result = execute(config, hub, context)
        assert result.ok and '<hub-artifacts>' not in result.last_message and '未发布' in result.last_message


def test_corrupt_existing_completion_is_preserved_and_never_replayed(tmp_path):
    with model_server([{'body': answer()}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        receipt = Path(context['receipt_path'])
        receipt.write_text('unverified original evidence', encoding='utf-8')
        with pytest.raises(APIModelError, match='原文件'):
            execute(config, hub, context)
        assert received == [] and receipt.read_text() == 'unverified original evidence'


def test_inference_tool_loop_stops_at_twelve_without_repeating_tools(tmp_path):
    with model_server([{'body': answer('', [tool('inspect', {}, 'same-read')])}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        result = execute(config, hub, context)
        assert not result.ok and '12 轮' in result.error and len(received) == 12


def test_large_exact_task_result_stops_without_silent_requirement_truncation(tmp_path):
    with model_server([{'body': answer('', [tool('get_task', {'task_id': 11})])}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        original = hub._json
        hub._json = lambda method, path, body=None: ({'task': {'description': 'x' * 50000}}
            if path.endswith('/tasks/11') else original(method, path, body))
        result = execute(config, hub, context)
        assert not result.ok and '未截断要求' in result.error and len(received) == 1


def test_prepared_text_preview_is_read_and_unread_pdf_is_explicit(tmp_path):
    with model_server([{'body': answer('根据已提供材料作答。')}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        workspace = config.projects['agent-test']
        source = workspace / 'brief.md'
        source.write_text('需求正文', encoding='utf-8')
        preview = source.with_name(source.name + '.preview.txt')
        preview.write_text('需求正文', encoding='utf-8')
        pdf = workspace / 'unread.pdf'
        pdf.write_bytes(b'not parsed PDF input')
        context['attachment_inputs'] = [
            {'attachment_id': 1, 'filename': 'brief.md', 'local_path': str(source), 'text_preview_path': str(preview)},
            {'attachment_id': 2, 'filename': 'unread.pdf', 'local_path': str(pdf)},
        ]
        result = execute(config, hub, context)
        assert result.ok
        prompt = received[0]['body']['messages'][1]['content']
        assert '需求正文' in prompt and 'text_preview_read' in prompt and '"status": "unread"' in prompt
        assert 'not parsed PDF input' not in prompt
        assert '未读取以下文件正文：unread.pdf' in result.last_message


@pytest.mark.parametrize('problem', ['outside', 'wrong_preview', 'oversized'])
def test_untrusted_or_oversized_preview_is_not_read_or_claimed(tmp_path, problem):
    with model_server([{'body': answer()}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        workspace = config.projects['agent-test']
        source = (tmp_path if problem == 'outside' else workspace) / 'brief.md'
        source.write_text('original', encoding='utf-8')
        preview = source.with_name(('another.txt' if problem == 'wrong_preview' else source.name + '.preview.txt'))
        preview.write_text('DO_NOT_SEND' * (15000 if problem == 'oversized' else 1), encoding='utf-8')
        context['attachment_inputs'] = [{'attachment_id': 1, 'filename': 'brief.md', 'local_path': str(source),
                                         'text_preview_path': str(preview)}]
        result = execute(config, hub, context)
        assert result.ok and '未读取以下文件正文：brief.md' in result.last_message
        assert 'DO_NOT_SEND' not in json.dumps(received[0]['body'])


def test_runner_selects_service_without_codex_and_publishes_real_completion(repo, tmp_path, monkeypatch):
    from test_execution_recovery import runtime
    monkeypatch.setattr('codex_bridge.model_allocator.allocate_task_model', lambda *args: {})
    with model_server([{'body': answer('服务回复\n<hub-reflection>' + json.dumps(REFLECTION) + '</hub-reflection>')}]) as (endpoint, received):
        runner, client, codex = runtime(repo, tmp_path)
        service = Hub(endpoint)
        client._json = service._json
        original = client.acquire_task_execution
        def acquire(*args):
            data = original(*args)
            data['model_selection'] = {'model': 'hub-service/7', 'service_revision': 3}
            return data
        client.acquire_task_execution = acquire
        result = runner.process_once()
        assert len(received) == 1 and not codex.calls
        assert repo.get_task(client.task_id)['status'] == 'review'
        assert not runner.state.task_deliveries
        assert result.executed_codex[0].ok


def test_same_execution_concurrent_instances_submit_only_once(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original_service = APIModelRunner._service
    def gated_service(self, *args):
        entered.set()
        assert release.wait(3)
        return original_service(self, *args)
    monkeypatch.setattr(APIModelRunner, '_service', gated_service)
    with model_server([{'body': answer('唯一实际结果')}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        results, errors = [], []
        def run():
            try:
                results.append(execute(config, hub, context))
            except APIModelError as exc:
                errors.append(str(exc))
        first = threading.Thread(target=run)
        second = threading.Thread(target=run)
        first.start()
        assert entered.wait(2)
        second.start()
        time.sleep(.15)
        release.set()
        first.join(4)
        second.join(4)
        assert not first.is_alive() and not second.is_alive()
        assert len(received) == 1
        assert any(result.ok for result in results)
        receipt = read_execution_completion(Path(context['receipt_path']), context)
        assert receipt['ok'] and receipt['last_message'] == '唯一实际结果'


def test_existing_pending_journal_is_not_overwritten_with_failed_completion(tmp_path):
    with model_server([{'body': answer()}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        completion = Path(context['receipt_path'])
        journal = completion.with_suffix('.api-journal.json')
        evidence = '{"state":"pending","run_key":"original"}'
        journal.write_text(evidence, encoding='utf-8')
        with pytest.raises(APIModelError, match='未确认'):
            execute(config, hub, context)
        assert not received and not completion.exists()
        assert journal.read_text(encoding='utf-8') == evidence


def test_request_thread_delayed_until_after_cancellation_never_sends(tmp_path, monkeypatch):
    with model_server([{'body': answer()}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        release, cancelled = threading.Event(), threading.Event()
        original_thread = threading.Thread
        network_threads = []
        def delayed_thread(*args, **kwargs):
            if kwargs.get('name') == 'hub-api-model-request':
                target = kwargs['target']
                kwargs['target'] = lambda: (release.wait(3), target())
                thread = original_thread(*args, **kwargs)
                network_threads.append(thread)
                return thread
            return original_thread(*args, **kwargs)
        timer = threading.Timer(.1, cancelled.set)
        monkeypatch.setattr('codex_bridge.api_model.threading.Thread', delayed_thread)
        timer.start()
        try:
            result = execute(config, hub, context, cancel=cancelled)
            assert not result.ok and '撤销' in result.error
        finally:
            release.set()
            timer.cancel()
            for thread in network_threads:
                thread.join(3)
        assert received == []


def test_user_heading_does_not_truncate_required_task_text(tmp_path):
    with model_server([{'body': answer()}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        original = '请审查以下材料\n\n## Hub 智能体工作接口\n这是用户正文中不可丢失的验收要求'
        result = execute(config, hub, context, prompt=original)
        assert result.ok and received[0]['body']['messages'][1]['content'] == original


def test_cross_process_owner_crash_leaves_unknown_journal_without_replay(tmp_path):
    with model_server([{'delay': 3, 'body': answer('remote result after process crash')}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        code = ('import sys\nfrom pathlib import Path\n'
                'sys.path.insert(0,sys.argv[3])\n'
                'from test_api_model import setup,execute\n'
                'config,hub,context=setup(Path(sys.argv[1]),sys.argv[2])\n'
                'execute(config,hub,context)\n')
        # This is a test-owned child process only, not a running Hub application.
        child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path), endpoint, str(Path(__file__).parent)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic() + 5
            while not received and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert received and child.poll() is None
            completion = Path(context['receipt_path'])
            journal = completion.with_suffix('.api-journal.json')
            evidence = journal.read_bytes()
            with pytest.raises(APIModelError, match='运行者'):
                execute(config, hub, context)
            assert not completion.exists() and journal.read_bytes() == evidence
            child.kill()
            child.communicate(timeout=5)
            with pytest.raises(APIModelError, match='未确认'):
                execute(config, hub, context)
            assert len(received) == 1 and not completion.exists() and journal.read_bytes() == evidence
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


def test_bridge_busy_api_owner_keeps_running_receipt_and_never_publishes_blocked(repo, tmp_path, monkeypatch):
    from codex_bridge.project_sessions import _lock
    from test_execution_recovery import runtime, restart
    monkeypatch.setattr('codex_bridge.model_allocator.allocate_task_model', lambda *args: {})
    with model_server([{'body': answer()}]) as (endpoint, received):
        runner, client, codex = runtime(repo, tmp_path)
        hub = Hub(endpoint)
        client._json = hub._json
        acquire = client.acquire_task_execution
        def selected(*args):
            result = acquire(*args)
            result['model_selection'] = {'model': 'hub-service/7', 'service_revision': 3}
            return result
        client.acquire_task_execution = selected
        actual_execute = runner._execute_codex
        release, entered = threading.Event(), threading.Event()
        owners = []
        def execute_while_owned(*args, **kwargs):
            lock_path = Path(kwargs['execution_context']['receipt_path']).resolve().with_suffix('.api-execution.lock')
            def owner():
                with _lock(lock_path, 0):
                    entered.set()
                    release.wait(5)
            thread = threading.Thread(target=owner)
            owners.append(thread)
            thread.start()
            assert entered.wait(2)
            return actual_execute(*args, **kwargs)
        monkeypatch.setattr(runner, '_execute_codex', execute_while_owned)
        try:
            with pytest.raises(APIModelError, match='运行者'):
                runner.process_once()
            receipt = runner.state.get_task_delivery('agent-test', client.task_id)
            assert receipt['stage'] == 'agent_running'
            assert not Path(receipt['completion_path']).exists()
            assert repo.get_task(client.task_id)['status'] == 'in_progress'
            assert client.complete_calls == 0 and client.start_calls == 1
            assert not received and not codex.calls
        finally:
            release.set()
            for owner in owners:
                owner.join(3)
        # Losing an owner is not permission for the recovery runner to replay.
        recovered = restart(runner, client, codex)
        result = recovered.process_once()
        assert client.task_id in result.pending_tasks
        assert client.complete_calls == 0 and client.start_calls == 1 and not received


@pytest.mark.parametrize('lock_error', [PermissionError('initialization flush denied'), OSError('lock path unavailable')])
def test_lock_initialization_failure_has_no_execution_or_completion(tmp_path, monkeypatch, lock_error):
    from contextlib import contextmanager
    @contextmanager
    def cannot_acquire(*args):
        raise lock_error
        yield  # pragma: no cover - context manager failure occurs on entry.
    monkeypatch.setattr('codex_bridge.project_sessions._lock', cannot_acquire)
    with model_server([{'body': answer()}]) as (endpoint, received):
        config, hub, context = setup(tmp_path, endpoint)
        with pytest.raises(APIModelError, match='独占许可'):
            execute(config, hub, context)
        assert received == [] and hub.calls == []
        assert not Path(context['receipt_path']).exists()
        assert not Path(context['receipt_path']).with_suffix('.api-journal.json').exists()
