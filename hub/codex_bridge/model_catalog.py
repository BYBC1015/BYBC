"""Discover the signed-in device's Codex models without executing a model turn."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codex_hub.model_catalog import normalize_codex_catalog

from .subprocess_utils import hidden_subprocess_kwargs


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_app_server_catalog(cli_path: str | Path, timeout_seconds: float) -> dict[str, Any]:
    process = subprocess.Popen(
        [str(cli_path), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        **hidden_subprocess_kwargs(),
    )
    responses: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def read_output() -> None:
        try:
            for line in process.stdout:
                try:
                    payload = json.loads(line)
                    if isinstance(payload, dict):
                        responses.put(payload)
                except ValueError:
                    continue
        finally:
            responses.put(None)

    reader = threading.Thread(target=read_output, daemon=True, name="CodexModelCatalog")
    reader.start()
    deadline = time.monotonic() + max(1.0, timeout_seconds)

    def send(payload: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    def result_for(request_id: int) -> dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex model discovery timed out")
            try:
                response = responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Codex model discovery timed out") from exc
            if response is None:
                raise RuntimeError("Codex model discovery closed")
            if response.get("id") != request_id:
                continue
            if response.get("error") or not isinstance(response.get("result"), dict):
                raise RuntimeError("Codex model discovery failed")
            return response["result"]

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex_hub_bridge", "title": "Codex Hub Bridge", "version": "1.0"}
        }})
        result_for(1)
        send({"method": "initialized", "params": {}})
        models = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for request_id in range(2, 12):
            params: dict[str, Any] = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            send({"id": request_id, "method": "model/list", "params": params})
            result = result_for(request_id)
            if isinstance(result.get("data"), list):
                models.extend(result["data"])
            cursor = result.get("nextCursor")
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        return normalize_codex_catalog({"models": models, "source": "app-server", "fetched_at": _utc_now()})
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        reader.join(timeout=1)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()


def discover_codex_catalog(
    cli_path: str | Path | None = None,
    codex_home: str | Path | None = None,
    timeout_seconds: float = 8,
) -> dict[str, Any]:
    """Read the target desktop's catalog; use model/list if no valid cache exists.

    A separately installed CLI may lag behind the desktop. Age alone must not
    replace the desktop's catalog with that unrelated CLI's smaller model list.
    The provider rereads the cache every five minutes to pick up desktop updates.
    """
    directory = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    cache_path = directory / "models_cache.json"
    cached = normalize_codex_catalog({"source": "unavailable"})
    try:
        if cache_path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("oversized model cache")
        raw = json.loads(cache_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("invalid model cache")
        raw["source"] = "models-cache"
        cached = normalize_codex_catalog(raw)
        if cached["models"]:
            return cached
    except (OSError, ValueError):
        pass
    executable = cli_path or shutil.which("codex")
    if executable:
        try:
            catalog = _read_app_server_catalog(executable, timeout_seconds)
            if catalog["models"]:
                return catalog
        except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError):
            pass
    return cached


class CodexModelCatalogProvider:
    def __init__(
        self, cli_path: str | Path | None = None, codex_home: str | Path | None = None,
        ttl_seconds: float = 300, timeout_seconds: float = 8,
    ):
        self.cli_path = cli_path
        self.codex_home = codex_home
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._catalog: dict[str, Any] | None = None
        self._refreshed_at = 0.0
        self._lock = threading.Lock()

    def get_catalog(self) -> dict[str, Any]:
        with self._lock:
            if self._catalog is None or time.monotonic() - self._refreshed_at >= self.ttl_seconds:
                discovered = discover_codex_catalog(self.cli_path, self.codex_home, self.timeout_seconds)
                if discovered["models"] or self._catalog is None:
                    self._catalog = discovered
                self._refreshed_at = time.monotonic()
            return self._catalog
