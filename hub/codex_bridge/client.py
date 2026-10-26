from __future__ import annotations

import json
import time
import hashlib
import re
import uuid
from pathlib import Path
from http.client import HTTPConnection, HTTPSConnection
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPHandler,
    HTTPSHandler,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
    urlopen,
)

from codex_hub.text_safety import validate_device_entry_text


class HubClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class _NoAttachmentRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HubClientError('附件请求发生重定向，已停止下载；请检查 Hub 地址。')


class _SourceBoundHTTPHandler(HTTPHandler):
    def __init__(self, source_ip: str):
        super().__init__()
        self.source_address = (source_ip, 0)

    def http_open(self, request):
        return self.do_open(self._connection, request)

    def _connection(self, host: str, **kwargs):
        return HTTPConnection(host, source_address=self.source_address, **kwargs)


class _SourceBoundHTTPSHandler(HTTPSHandler):
    def __init__(self, source_ip: str):
        super().__init__()
        self.source_address = (source_ip, 0)

    def https_open(self, request):
        return self.do_open(self._connection, request)

    def _connection(self, host: str, **kwargs):
        return HTTPSConnection(host, source_address=self.source_address, **kwargs)


def build_source_bound_opener(source_ip: str):
    """Open Hub connections from one local IP without system/environment proxies."""
    return build_opener(
        ProxyHandler({}),
        _SourceBoundHTTPHandler(source_ip),
        _SourceBoundHTTPSHandler(source_ip),
    )


class HubClient:
    def __init__(
        self,
        hub: str,
        token: str,
        timeout_seconds: int = 12,
        get_retry_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
        sleeper: Callable[[float], None] = time.sleep,
        source_ip: str = "",
    ):
        self.hub = hub.rstrip("/")
        self.token = token
        self.timeout_seconds = max(3, int(timeout_seconds))
        self.get_retry_attempts = min(5, max(1, int(get_retry_attempts)))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.sleeper = sleeper
        self.source_ip = str(source_ip or "").strip()
        self._opener = (
            build_source_bound_opener(self.source_ip) if self.source_ip else None
        )

    def get_inbox(self) -> dict[str, Any]:
        return self._json("GET", "/api/inbox")

    def heartbeat(self) -> dict[str, Any]:
        return self._json('POST', '/api/device/heartbeat', {})

    def acquire_task_execution(self, slug: str, task_id: int, run_key: str) -> dict:
        return self._json('POST', f'/api/projects/{slug}/tasks/{task_id}/execution/acquire', {'run_key':run_key})

    def start_task_execution(self, slug: str, task_id: int, execution: dict) -> dict:
        return self._json('POST', f'/api/projects/{slug}/tasks/{task_id}/execution/start',
                          {key:execution[key] for key in ('generation','run_key')})

    def renew_task_execution(self, slug: str, task_id: int, execution: dict) -> dict:
        return self._json('POST', f'/api/projects/{slug}/tasks/{task_id}/execution/renew',
                          {key:execution[key] for key in ('generation','run_key')})

    def reconcile_task_execution(self, slug: str, task_id: int) -> dict:
        return self._json('POST', f'/api/projects/{slug}/tasks/{task_id}/execution/reconcile', {})

    def complete_task_execution(self, slug: str, task_id: int, execution: dict, *,
                                status: str, summary: str, body: str, reflection: dict) -> dict:
        return self._json('POST', f'/api/projects/{slug}/tasks/{task_id}/execution/complete',
                          {key:execution[key] for key in ('generation','run_key')} |
                          {'status':status,'summary':summary,'body':body,'reflection':reflection})

    def execution_renewal_client(self):
        return HubClient(self.hub, self.token, timeout_seconds=min(12, self.timeout_seconds),
                         get_retry_attempts=1, source_ip=self.source_ip)

    def post_device_capabilities(self, capabilities: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/api/device/capabilities", capabilities)

    def claim_task(self, project_slug: str, task_id: int) -> dict[str, Any] | None:
        try:
            data = self._json(
                "POST", f"/api/projects/{project_slug}/tasks/{task_id}/claim"
            )
        except HubClientError:
            return None
        return data.get("task")

    def get_task_prompt(self, project_slug: str, task_id: int) -> str:
        return self._text(
            "GET", f"/api/projects/{project_slug}/tasks/{task_id}/prompt.md"
        )

    def _attachment_url(self, project_slug: str, suffix: str) -> str:
        parsed = urlsplit(self.hub)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,62}[a-z0-9]', project_slug)):
            raise HubClientError('Hub 附件地址或项目标识无效。')
        return f'{self.hub}/api/projects/{project_slug}/{suffix}'

    def _open_attachment(self, url: str):
        # Never carry the bearer credential through redirects, including a
        # same-origin login redirect. Downloads use only our fixed Hub paths.
        handlers = [_NoAttachmentRedirect()]
        if self.source_ip:
            handlers.extend([ProxyHandler({}), _SourceBoundHTTPHandler(self.source_ip),
                             _SourceBoundHTTPSHandler(self.source_ip)])
        opener = build_opener(*handlers)
        return opener.open(Request(url, headers=self._headers(), method='GET'),
                           timeout=max(30, self.timeout_seconds))

    def get_task_attachments(self, project_slug: str, task_id: int) -> dict[str, Any]:
        if type(task_id) is not int or task_id <= 0:
            raise HubClientError('附件任务编号无效。')
        url = self._attachment_url(project_slug, f'tasks/{task_id}/attachments')
        try:
            with self._open_attachment(url) as response:
                if response.geturl() != url:
                    raise HubClientError('附件清单响应地址不匹配，已停止读取。')
                raw = response.read(131_073)
            if len(raw) > 131_072:
                raise HubClientError('任务附件清单过大。')
            data = json.loads(raw.decode('utf-8'))
            if not isinstance(data, dict) or not isinstance(data.get('attachments'), list):
                raise HubClientError('Hub 未返回有效的任务附件清单。')
            return data
        except HTTPError as exc:
            raise HubClientError(f'任务附件清单读取失败（HTTP {exc.code}）。') from None
        except (OSError, ValueError) as exc:
            raise HubClientError('任务附件清单读取失败，请检查 Hub 连接。') from None

    def download_task_attachment(self, project_slug: str, attachment_id: int, target: Path, *,
                                 expected_size: int, expected_sha256: str, max_bytes: int) -> None:
        if type(attachment_id) is not int or attachment_id <= 0:
            raise HubClientError('附件编号无效。')
        if (type(expected_size) is not int or not 0 <= expected_size <= max_bytes
                or not re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256)):
            raise HubClientError('附件大小或校验摘要无效。')
        url = self._attachment_url(project_slug, f'attachments/{attachment_id}')
        temporary = target.with_name(f'.{target.name}.{uuid.uuid4().hex}.part')
        digest, size = hashlib.sha256(), 0
        try:
            with self._open_attachment(url) as response, temporary.open('xb') as handle:
                if response.geturl() != url:
                    raise HubClientError('附件响应地址不匹配，已停止下载。')
                declared = response.headers.get('Content-Length')
                if declared is not None and (not declared.isdigit() or int(declared) != expected_size):
                    raise HubClientError('附件响应大小与清单不一致。')
                while True:
                    chunk = response.read(min(65_536, max_bytes - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes or size > expected_size:
                        raise HubClientError('附件超过允许大小，已停止下载。')
                    digest.update(chunk)
                    handle.write(chunk)
            if size != expected_size or digest.hexdigest() != expected_sha256.lower():
                raise HubClientError('附件完整性校验失败，未交给执行者。')
            temporary.replace(target)
        except HTTPError as exc:
            raise HubClientError(f'附件 #{attachment_id} 下载失败（HTTP {exc.code}）。') from None
        except OSError:
            raise HubClientError(f'附件 #{attachment_id} 下载或保存失败。') from None
        finally:
            temporary.unlink(missing_ok=True)

    def get_agent_context(self, project_slug: str, task_id: int) -> dict[str, Any]:
        return self._json("GET", f"/api/projects/{project_slug}/tasks/{task_id}/agent-context")

    def route_agent_task(self, project_slug: str, task_id: int, decision: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", f"/api/projects/{project_slug}/tasks/{task_id}/agent-route",
                          {"decision": decision, "engine": "codex_cli"})

    def fallback_agent_route(self, project_slug: str, task_id: int) -> dict[str, Any]:
        # Model decisions take longer than normal Hub mutations. Do not alter the
        # shared client's timeout while its heartbeat thread is using it.
        client = HubClient(self.hub, self.token, timeout_seconds=max(150, self.timeout_seconds),
                           get_retry_attempts=1, source_ip=self.source_ip)
        return client._json("POST", f"/api/projects/{project_slug}/tasks/{task_id}/agent-route/fallback", {})

    def get_project_memory_markdown(self, project_slug: str) -> str:
        return self._text("GET", f"/api/projects/{project_slug}/memory.md")

    def get_project_memory_runtime_markdown(self, project_slug: str) -> str:
        return self._text("GET", f"/api/projects/{project_slug}/memory/runtime.md")

    def post_project_sync_snapshot(
        self,
        project_slug: str,
        *,
        memory_version: int,
        git_commit: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/projects/{project_slug}/sync-snapshots",
            {
                "memory_version": int(memory_version),
                "git_commit": git_commit,
                "manifest": manifest,
            },
        )

    def post_project_handoff(
        self,
        project_slug: str,
        *,
        title: str,
        summary_markdown: str,
        target_device_id: int | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/projects/{project_slug}/handoffs",
            {
                "title": title,
                "summary_markdown": summary_markdown,
                "target_device_id": target_device_id,
                "manifest": manifest or {"files": []},
            },
        )

    def claim_mention(
        self, project_slug: str, entry_id: int
    ) -> dict[str, Any] | None:
        try:
            data = self._json(
                "POST", f"/api/projects/{project_slug}/mentions/{entry_id}/claim"
            )
        except HubClientError:
            return None
        mention = data.get("mention")
        receipt = data.get("receipt")
        if isinstance(mention, dict) and isinstance(receipt, dict):
            mention = dict(mention)
            mention["claim_status"] = receipt.get("status") or mention.get(
                "claim_status", "claimed"
            )
            mention["delivery_id"] = receipt.get("delivery_id") or mention.get(
                "delivery_id"
            )
        return mention

    def mark_mention_dispatching(
        self, project_slug: str, entry_id: int
    ) -> dict[str, Any]:
        return self._json(
            "POST", f"/api/projects/{project_slug}/mentions/{entry_id}/dispatching"
        ).get("receipt", {})

    def mark_mention_submitted(
        self, project_slug: str, entry_id: int
    ) -> dict[str, Any]:
        return self._json(
            "POST", f"/api/projects/{project_slug}/mentions/{entry_id}/submitted"
        ).get("receipt", {})

    def release_mention_dispatch(
        self, project_slug: str, entry_id: int
    ) -> dict[str, Any]:
        return self._json(
            "POST", f"/api/projects/{project_slug}/mentions/{entry_id}/release"
        ).get("receipt", {})

    def mark_mention_handled(self, project_slug: str, entry_id: int) -> None:
        self._json(
            "POST", f"/api/projects/{project_slug}/mentions/{entry_id}/handled"
        )

    def update_task_status(
        self,
        project_slug: str,
        task_id: int,
        status: str,
        result_summary: str = "",
        reflection: dict[str, str] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"status": status, "result_summary": result_summary}
        if reflection is not None:
            payload["reflection"] = reflection
        self._json(
            "POST",
            f"/api/projects/{project_slug}/tasks/{task_id}/status",
            payload,
        )

    def post_task_entry(
        self, project_slug: str, task_id: int, body_markdown: str, *,
        reply_to_entry_id: int | None = None,
    ) -> dict[str, Any]:
        body_markdown = validate_device_entry_text(body_markdown)
        payload: dict[str, Any] = {"kind": "message", "body_markdown": body_markdown}
        if reply_to_entry_id is not None:
            payload["reply_to_entry_id"] = int(reply_to_entry_id)
        return self._json(
            "POST",
            f"/api/projects/{project_slug}/tasks/{task_id}/entries",
            payload,
        )

    def post_project_entry(
        self, project_slug: str, body_markdown: str, *, reply_to_entry_id: int | None = None
    ) -> dict[str, Any]:
        body_markdown = validate_device_entry_text(body_markdown)
        payload: dict[str, Any] = {"kind": "message", "body_markdown": body_markdown}
        if reply_to_entry_id is not None:
            payload["reply_to_entry_id"] = int(reply_to_entry_id)
        return self._json(
            "POST",
            f"/api/projects/{project_slug}/entries",
            payload,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "CodexBridge/1.0",
        }

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> bytes:
        payload = None
        headers = self._headers()
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(
            f"{self.hub}{path}",
            data=payload,
            headers=headers,
            method=method,
        )
        attempts = self.get_retry_attempts if method.upper() == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                open_request = self._opener.open if self._opener else urlopen
                with open_request(request, timeout=self.timeout_seconds) as response:
                    return response.read()
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise HubClientError(
                    f"Hub API {method} {path} failed: {exc.code} {detail}", status_code=exc.code
                ) from exc
            except OSError as exc:
                if attempt >= attempts:
                    suffix = f" after {attempts} attempts" if attempts > 1 else ""
                    raise HubClientError(
                        f"Hub API {method} {path} failed{suffix}: {exc}"
                    ) from exc
                self.sleeper(self.retry_backoff_seconds * attempt)
        raise AssertionError("unreachable")

    def _json(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raw = self._request(method, path, body)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _text(self, method: str, path: str) -> str:
        return self._request(method, path).decode("utf-8")
