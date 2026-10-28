import json

import pytest

from codex_bridge.agent_runtime import extract_reflection
from codex_bridge.artifacts import collect_artifacts
from codex_bridge.client import HubClientError
from test_agent_runtime import REFLECTION, build_runner
from test_codex_bridge import FakeExecutor


@pytest.mark.parametrize("private", [
    '<hub-reflection>{"summary":"private-tail"',
    '<hub-reflection>{broken: "private-tail"}</hub-reflection>',
    '<HUB-REFLECTION>{"private-tail":true}</HUB-REFLECTION>',
    '<hub-reflection>outer <hub-reflection>private-tail</hub-reflection>outer</hub-reflection>',
    '<hub-reflection>{}</hub-reflection><hub-reflection>private-tail</hub-reflection>',
    '<hub-reflect',
    '<hub-reflection source="private-tail"',
])
def test_invalid_reflection_never_leaks_into_visible_delivery(private):
    body, reflection, complete = extract_reflection("已按要求整理参考。\n" + private, ok=True)

    assert body == "已按要求整理参考。"
    assert not complete
    assert reflection["worked"] == ""
    assert "缺少有效" in reflection["failed"]
    assert "private-tail" not in reflection["summary"]


def test_valid_reflection_preserves_public_text_before_and_after_block():
    text = "这是参考图。\n<hub-reflection>" + json.dumps(REFLECTION) + "</hub-reflection>\n下一步可以细化标题。"
    body, reflection, complete = extract_reflection(text, ok=True)

    assert body == "这是参考图。\n\n下一步可以细化标题。"
    assert complete and reflection == REFLECTION


@pytest.mark.parametrize("placement", ["before", "after", "unfinished"])
def test_artifact_declarations_survive_reflection_filter_for_real_upload_parser(tmp_path, placement):
    output = tmp_path / "result.md"
    output.write_text("Deliverable", encoding="utf-8")
    artifacts = '<hub-artifacts>[{"path":"result.md"}]</hub-artifacts>'
    reflection = "<hub-reflection>" + json.dumps(REFLECTION) + "</hub-reflection>"
    if placement == "before":
        text = "成果已生成。\n" + artifacts + "\n" + reflection
    elif placement == "after":
        text = "成果已生成。\n" + reflection + "\n" + artifacts
    else:
        text = '成果已生成。\n<hub-reflection>{"summary":"private-tail"\n' + artifacts

    body, _, complete = extract_reflection(text, ok=True)
    public, files = collect_artifacts(body, tmp_path)

    assert complete == (placement != "unfinished")
    assert public == "成果已生成。"
    assert len(files) == 1 and files[0]["path"] == output
    assert "private-tail" not in body and "hub-reflection" not in body


def test_incomplete_private_block_is_recorded_internally_without_reexecution(tmp_path):
    raw = '收到，这是智慧方向的参考图。\n<hub-reflection>{"summary":"private-tail"'
    runner, client, _, executor = build_runner(tmp_path, executor=FakeExecutor(summary=raw))
    client.fail_result_once = True

    with pytest.raises(HubClientError):
        runner.process_once()
    receipt = runner.state.get_task_delivery("agent-test", 42)
    assert receipt["result_summary"] == "收到，这是智慧方向的参考图。"
    assert receipt["result_status"] == "review"
    assert receipt["reflection_complete"] is False
    assert "缺少有效" in receipt["reflection"]["failed"]

    runner.process_once()
    runner.process_once()

    assert len(executor.calls) == 1
    assert len(client.task_entries) == 1
    visible = client.task_entries[0][2]
    assert "这是智慧方向的参考图" in visible
    assert "hub-reflection" not in visible and "private-tail" not in visible
    assert "结构化复盘" not in visible
    assert client.task_status_updates[-1][2] == "review"


@pytest.mark.parametrize("ok", [True, False])
def test_private_only_response_never_falls_back_to_raw_execution_summary(tmp_path, ok):
    raw = '<hub-reflection>{"summary":"private-tail"'
    runner, client, _, executor = build_runner(tmp_path, executor=FakeExecutor(ok=ok, summary=raw))
    runner.process_once()
    runner.process_once()

    assert len(executor.calls) == 1
    visible = client.task_entries[0][2]
    assert "hub-reflection" not in visible and "private-tail" not in visible
    assert "结构化复盘" not in visible
    assert client.task_status_updates[-1][2] == ("review" if ok else "blocked")
