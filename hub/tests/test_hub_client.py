import json
from urllib.error import URLError

import pytest

import codex_bridge.client as client_module
from codex_bridge.client import HubClient, HubClientError


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def test_reply_parent_is_sent_as_structured_field(monkeypatch):
    requests = []
    monkeypatch.setattr(client_module, "urlopen", lambda request, timeout: (requests.append(request) or FakeResponse(b'{}')))
    client = HubClient("https://hub.example", "token")
    client.post_project_entry("project-one", "Result", reply_to_entry_id=303)
    assert json.loads(requests[0].data)["reply_to_entry_id"] == 303


def test_execution_progress_sends_only_scoped_generation_and_public_update(monkeypatch):
    requests = []
    monkeypatch.setattr(client_module, 'urlopen', lambda request, timeout: (requests.append(request) or FakeResponse(b'{}')))
    client = HubClient('https://hub.example/hub', 'token')
    client.publish_execution_progress('project-one', 9,
        {'generation': 3, 'run_key': 'stable-run-key', 'private_field': 'not transmitted'},
        4, 'assistant', '正在处理本轮任务。')
    assert requests[0].full_url == 'https://hub.example/hub/api/projects/project-one/tasks/9/execution/progress'
    assert requests[0].method == 'POST'
    assert json.loads(requests[0].data) == {'generation': 3, 'run_key': 'stable-run-key',
        'sequence': 4, 'kind': 'assistant', 'text': '正在处理本轮任务。'}


@pytest.mark.parametrize("reply_id", [303, None])
def test_task_reply_preserves_parent_and_initial_task_does_not_invent_one(monkeypatch, reply_id):
    requests = []
    monkeypatch.setattr(client_module, "urlopen", lambda request, timeout: (requests.append(request) or FakeResponse(b'{}')))
    client = HubClient("https://hub.example", "token")
    client.post_task_entry("project-one", 7, "Result", reply_to_entry_id=reply_id)
    assert requests[0].full_url.endswith("/api/projects/project-one/tasks/7/entries")
    payload = json.loads(requests[0].data)
    if reply_id is None:
        assert "reply_to_entry_id" not in payload
    else:
        assert payload["reply_to_entry_id"] == 303


def test_capabilities_use_device_authenticated_endpoint(monkeypatch):
    requests = []
    monkeypatch.setattr(client_module, "urlopen", lambda request, timeout: (requests.append(request) or FakeResponse(b'{}')))
    client = HubClient("https://hub.example", "token")
    client.post_device_capabilities({"execution_backend": "codex", "codex": {"models": []}})
    assert requests[0].full_url.endswith("/api/device/capabilities")
    assert json.loads(requests[0].data)["execution_backend"] == "codex"


def test_get_retries_transient_network_errors(monkeypatch):
    calls = []
    sleeps = []

    def fake_urlopen(_request, timeout):
        calls.append(timeout)
        if len(calls) < 3:
            raise URLError("timed out")
        return FakeResponse(b'{"device":{"label":"codex-device-a"}}')

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    client = HubClient(
        "https://hub.example/hub",
        "token",
        timeout_seconds=7,
        get_retry_attempts=3,
        retry_backoff_seconds=0.5,
        sleeper=sleeps.append,
    )

    inbox = client.get_inbox()

    assert inbox["device"]["label"] == "codex-device-a"
    assert calls == [7, 7, 7]
    assert sleeps == [0.5, 1.0]


def test_post_is_not_retried_after_network_error(monkeypatch):
    calls = 0

    def fake_urlopen(_request, timeout):
        nonlocal calls
        calls += 1
        raise URLError("timed out")

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    client = HubClient(
        "https://hub.example/hub",
        "token",
        get_retry_attempts=3,
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(HubClientError, match="timed out"):
        client.update_task_status("project-a", 1, "review")

    assert calls == 1


def test_source_ip_uses_direct_bound_opener(monkeypatch):
    opened = []
    source_ips = []

    class FakeOpener:
        def open(self, request, timeout):
            opened.append((request.full_url, timeout))
            return FakeResponse(b'{"device":{"label":"codex-device-a"}}')

    def fake_build_opener(source_ip):
        source_ips.append(source_ip)
        return FakeOpener()

    monkeypatch.setattr(client_module, "build_source_bound_opener", fake_build_opener)
    client = HubClient(
        "https://hub.example/hub",
        "token",
        timeout_seconds=9,
        source_ip=" 192.168.3.28 ",
    )

    inbox = client.get_inbox()

    assert inbox["device"]["label"] == "codex-device-a"
    assert source_ips == ["192.168.3.28"]
    assert opened == [("https://hub.example/hub/api/inbox", 9)]


def test_post_project_entry_encodes_unicode_json_as_utf8(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse(b'{"entry":{"id":133}}')

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    client = HubClient("https://hub.example/hub", "token")

    result = client.post_project_entry("private-server-buildout", "中文协作测试")

    payload = json.loads(requests[0][0].data.decode("utf-8"))
    assert payload["body_markdown"] == "中文协作测试"
    assert result["entry"]["id"] == 133


def test_post_project_entry_rejects_question_mark_encoding_loss(monkeypatch):
    called = False

    def fake_urlopen(_request, timeout):
        nonlocal called
        called = True
        return FakeResponse(b"{}")

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    client = HubClient("https://hub.example/hub", "token")
    damaged = (
        "?????Hermes ?????????????????? --model k3 ????????\n"
        "???????? Codex ?????????????"
    )

    with pytest.raises(ValueError, match="looks like non-ASCII text"):
        client.post_project_entry("private-server-buildout", damaged)

    assert called is False
