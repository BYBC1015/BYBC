import io
import json
from datetime import datetime, timedelta, timezone

from codex_bridge import model_catalog as discovery
from codex_hub.model_catalog import build_project_model_catalog, normalize_codex_catalog


def test_cache_catalog_is_dynamic_and_only_exposes_public_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery.shutil, "which", lambda *_args: None)
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "fetched_at": "2026-09-11T00:00:00Z", "models": [
            {"slug": "future-model-v29", "display_name": "Future Model", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "max"}, {"effort": "ultra"}],
             "default_reasoning_level": "max", "model_messages": {"private": "DO NOT UPLOAD"}},
            {"slug": "hidden-model", "visibility": "hide"},
        ],
    }), encoding="utf-8")
    catalog = discovery.discover_codex_catalog(codex_home=tmp_path)
    assert catalog["source"] == "models-cache"
    assert [model["model"] for model in catalog["models"]] == ["future-model-v29"]
    assert catalog["models"][0]["defaultReasoningEffort"] == "max"
    assert "DO NOT UPLOAD" not in json.dumps(catalog)
    assert "model_messages" not in json.dumps(catalog)


def test_model_list_pagination_and_hidden_models(monkeypatch):
    sent = io.StringIO()
    class Process:
        stdin = sent
        stdout = io.StringIO("\n".join(json.dumps(item) for item in [
            {"id": 1, "result": {}},
            {"id": 2, "result": {"data": [{"model": "future-a"}], "nextCursor": "next"}},
            {"id": 3, "result": {"data": [{"model": "future-b"}, {"model": "secret", "hidden": True}]}},
        ]))
        def poll(self): return 0
        def wait(self, **kwargs): return 0
    writes = []
    class RecordingInput(io.StringIO):
        def write(self, value):
            writes.append(json.loads(value))
            return super().write(value)
    Process.stdin = RecordingInput()
    monkeypatch.setattr(discovery.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    catalog = discovery._read_app_server_catalog("codex", 1)
    assert [item["model"] for item in catalog["models"]] == ["future-a", "future-b"]
    assert writes[2]["params"] == {"limit": 100, "includeHidden": False}
    assert writes[3]["params"]["cursor"] == "next"
    assert catalog["source"] == "app-server"


def test_provider_caches_discovery_and_retains_last_good_catalog(monkeypatch):
    calls = []
    def discover(*args):
        calls.append(args)
        return normalize_codex_catalog({"models": [{"model": "future-model"}] if len(calls) == 1 else []})
    monkeypatch.setattr(discovery, "discover_codex_catalog", discover)
    provider = discovery.CodexModelCatalogProvider(ttl_seconds=300)
    assert provider.get_catalog()["models"][0]["model"] == "future-model"
    provider.get_catalog()
    assert len(calls) == 1
    provider._refreshed_at = -10000
    assert provider.get_catalog()["models"][0]["model"] == "future-model"
    assert len(calls) == 2


def test_fresh_desktop_catalog_is_not_replaced_by_older_cli(tmp_path, monkeypatch):
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "models": [{"slug": "new-desktop-model", "visibility": "list"}],
    }), encoding="utf-8")
    def old_cli(*args):
        raise AssertionError("A fresh desktop catalog must not be overwritten by a stale CLI")
    monkeypatch.setattr(discovery, "_read_app_server_catalog", old_cli)
    result = discovery.discover_codex_catalog(cli_path="old-codex", codex_home=tmp_path)
    assert result["models"][0]["model"] == "new-desktop-model"


def test_old_but_valid_desktop_catalog_is_not_replaced_by_older_cli(tmp_path, monkeypatch):
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "fetched_at": (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),
        "models": [{"slug": "new-desktop-model", "visibility": "list"}],
    }), encoding="utf-8")
    def old_cli(*args):
        raise AssertionError("Desktop catalog age must not switch to an unrelated older CLI")
    monkeypatch.setattr(discovery, "_read_app_server_catalog", old_cli)
    result = discovery.discover_codex_catalog(cli_path="old-codex", codex_home=tmp_path)
    assert result["models"][0]["model"] == "new-desktop-model"
    assert result["source"] == "models-cache"


def test_invalid_cache_and_model_ids_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery.shutil, "which", lambda *_args: None)
    (tmp_path / "models_cache.json").write_text("[]", encoding="utf-8")
    assert discovery.discover_codex_catalog(codex_home=tmp_path)["models"] == []
    assert normalize_codex_catalog({"models": [{"model": "x; command"}, {"model": "x"}, {"model": "x"}]})["models"] == [
        {"model": "x", "displayName": "x", "supportedReasoningEfforts": [], "defaultReasoningEffort": "", "isDefault": False}
    ]


def test_catalog_only_includes_writable_project_members():
    result = build_project_model_catalog([
        {"device_id": 1, "device_label": "One", "permission": "write"},
        {"device_id": 2, "device_label": "Two", "permission": "read"},
    ], {1: {"execution_backend": "codex", "codex": {"models": [{"model": "future"}]}}})
    assert [device["device_id"] for device in result["devices"]] == [1]
    assert result["devices"][0]["codex"]["models"][0]["model"] == "future"


def test_server_ai_members_do_not_require_codex_capability_files():
    result = build_project_model_catalog([
        {"device_id": 1, "device_label": "DeepSeek", "permission": "write", "server_ai_provider": "deepseek"},
        {"device_id": 2, "device_label": "Kimi", "permission": "write", "server_ai_provider": "kimi"},
    ], {})
    assert [device["execution_backend"] for device in result["devices"]] == ["deepseek", "kimi"]
