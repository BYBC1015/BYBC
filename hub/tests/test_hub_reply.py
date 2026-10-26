import pytest
import codex_bridge.hub_reply as reply_module

from codex_bridge.config import BridgeBinding, BridgeConfig
from codex_bridge.hub_reply import read_body, select_runtime_config


def test_read_body_reads_utf8_without_losing_chinese(tmp_path):
    path = tmp_path / "reply.md"
    path.write_text("中文协作测试\n", encoding="utf-8")

    assert read_body(path) == "中文协作测试"


def test_read_body_rejects_non_utf8(tmp_path):
    path = tmp_path / "reply.md"
    path.write_bytes("中文".encode("gbk"))

    with pytest.raises(ValueError, match="not valid UTF-8"):
        read_body(path)


def test_select_runtime_config_uses_requested_binding(tmp_path):
    config = BridgeConfig(
        hub="https://hub.example",
        token="primary-token",
        prompt_dir=tmp_path / "prompts",
        state_path=tmp_path / "state.json",
        bindings=[
            BridgeBinding(
                id="hermes",
                name="Hermes",
                token="hermes-token",
                execution_backend="hermes",
            )
        ],
    )

    runtime = select_runtime_config(config, "hermes")

    assert runtime.binding_id == "hermes"
    assert runtime.token == "hermes-token"


@pytest.mark.parametrize("task_id", [7, None])
@pytest.mark.parametrize("mention_id", [303, None])
def test_reply_command_passes_source_entry_for_task_and_project(tmp_path, monkeypatch, task_id, mention_id):
    config = BridgeConfig(hub="https://hub.example", token="test", prompt_dir=tmp_path, state_path=tmp_path / "state.json")
    body_path = tmp_path / "reply.md"
    body_path.write_text("Result", encoding="utf8")
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def post_project_entry(self, *args, **kwargs):
            calls.append(("project", args, kwargs))
            return {"entry": {"id": 900}}

        def post_task_entry(self, *args, **kwargs):
            calls.append(("task", args, kwargs))
            return {"entry": {"id": 900}}

        def mark_mention_handled(self, *args):
            calls.append(("handled", args))

    monkeypatch.setattr(reply_module, "HubClient", FakeClient)
    monkeypatch.setattr(reply_module, "load_config", lambda _: config)
    argv = ["--project", "project-one", "--body-file", str(body_path)]
    if task_id is not None:
        argv.extend(["--task-id", str(task_id)])
    if mention_id is not None:
        argv.extend(["--mention-id", str(mention_id)])
    result = reply_module.post_reply(reply_module.build_parser().parse_args(argv))
    assert result["entry"]["id"] == 900
    assert calls[0][0] == ("task" if task_id is not None else "project")
    assert calls[0][2] == {"reply_to_entry_id": mention_id}
    if mention_id is None:
        assert len(calls) == 1
    else:
        assert calls[1] == ("handled", ("project-one", mention_id))
