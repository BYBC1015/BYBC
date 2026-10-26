"""Short-lived read-only attachment URLs for external media generators.

Device authentication is used only to mint a link. The link carries a scoped HMAC,
never a device token. Existing private attachment endpoints remain unchanged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request

from .reading import video_content_type


MAX_EXPIRES_IN_SECONDS = 86_400
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPIRY_RE = re.compile(r"^[1-9][0-9]{0,11}$")
_MEDIA_TYPES_BY_SUFFIX = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
}


def _signing_key(secret: str) -> bytes:
    if not secret or secret == "local-dev-change-me":
        raise HTTPException(status_code=503, detail="media sharing requires a configured server secret")
    # Use the existing persistent server secret with domain separation from cookies.
    return hmac.new(secret.encode("utf-8"), b"codex-hub/media-relay/v1", hashlib.sha256).digest()


def _signature(secret: str, attachment: dict[str, Any], expires: int) -> str:
    payload = json.dumps(
        {"version": 1, "attachment_id": int(attachment["id"]),
         "project_id": int(attachment["project_id"]), "expires": expires,
         "stored_relpath": attachment["stored_relpath"], "sha256": attachment["sha256"],
         "byte_size": int(attachment["byte_size"])},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_signing_key(secret), payload, hashlib.sha256).hexdigest()


def _attachment_path(files_dir: Path, attachment: dict[str, Any]) -> Path:
    root = files_dir.resolve()
    path = (root / attachment["stored_relpath"]).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="attachment file not found")
    return path


def _require_media_type(attachment: dict[str, Any]) -> str:
    suffix = Path(str(attachment.get("original_filename") or "")).suffix.lower()
    expected = _MEDIA_TYPES_BY_SUFFIX.get(suffix)
    declared = str(attachment.get("content_type") or "").split(";", 1)[0].strip().lower()
    if not expected:
        raise HTTPException(status_code=415, detail="media links only support MP4, MOV, WebM, PNG and JPEG files")
    if expected.startswith("video/"):
        actual = video_content_type(attachment)
    else:
        actual = expected if declared in {"", "application/octet-stream", "binary/octet-stream"} else declared
    if actual != expected:
        raise HTTPException(status_code=415, detail="attachment content type is not a supported media type")
    return expected


def register_media_relay_routes(app: FastAPI) -> None:
    # Resolve the existing helpers after main has defined its routes and guards.
    # This keeps authorization, attachment handling and proxy-prefix behavior shared.
    from .main import (
        app_path, attachment_file_response, get_repo, get_settings, is_secure_request,
        json_object, require_api_device, require_device_write, require_project,
        require_project_attachment,
    )

    @app.get("/api/media-share/capabilities")
    def media_share_capabilities(request: Request):
        require_api_device(request)
        settings = get_settings(request)
        _signing_key(settings.secret_key)
        return {
            "supported": True,
            "max_expires_in_seconds": MAX_EXPIRES_IN_SECONDS,
            "max_upload_bytes": settings.max_upload_bytes,
        }

    @app.post("/api/projects/{slug}/attachments/{attachment_id}/share-link")
    async def share_attachment(request: Request, slug: str, attachment_id: int):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        attachment = require_project_attachment(repo, project, attachment_id)
        settings = get_settings(request)
        _require_media_type(attachment)
        _attachment_path(settings.files_dir, attachment)
        payload = await json_object(request)
        duration = payload.get("expires_in_seconds", MAX_EXPIRES_IN_SECONDS)
        if type(duration) is not int or not 1 <= duration <= MAX_EXPIRES_IN_SECONDS:
            raise HTTPException(status_code=422, detail="expires_in_seconds must be an integer from 1 to 86400")
        if set(payload) - {"expires_in_seconds"}:
            raise HTTPException(status_code=422, detail="unsupported share-link option")
        expires = int(time.time()) + duration
        signature = _signature(settings.secret_key, attachment, expires)
        path = app_path(request, f"/media/{attachment_id}")
        # Host handling follows the existing reverse-proxy contract; no secret is
        # present in this URL, and arbitrary forwarded-host values are not trusted.
        url = request.url.replace(
            scheme="https" if is_secure_request(request) else request.url.scheme,
            path=path, query=urlencode({"expires": expires, "signature": signature}), fragment="",
        )
        return {
            "url": str(url),
            "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    @app.api_route("/media/{attachment_id}", methods=["GET", "HEAD"])
    def shared_attachment(request: Request, attachment_id: int):
        query = request.query_params
        # Reject duplicate parameters and anything that might carry a device token.
        if set(query) != {"expires", "signature"} or any(len(query.getlist(key)) != 1 for key in query):
            raise HTTPException(status_code=403, detail="invalid or expired media link")
        raw_expires = query.get("expires", "")
        signature = query.get("signature", "")
        if not _EXPIRY_RE.fullmatch(raw_expires) or not _SIGNATURE_RE.fullmatch(signature):
            raise HTTPException(status_code=403, detail="invalid or expired media link")
        expires = int(raw_expires)
        now = int(time.time())
        if expires <= now or expires > now + MAX_EXPIRES_IN_SECONDS:
            raise HTTPException(status_code=403, detail="invalid or expired media link")
        settings = get_settings(request)
        attachment = get_repo(request).get_attachment(attachment_id)
        if not attachment:
            raise HTTPException(status_code=404, detail="attachment not found")
        expected = _signature(settings.secret_key, attachment, expires)
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=403, detail="invalid or expired media link")
        media_type = _require_media_type(attachment)
        _attachment_path(settings.files_dir, attachment)
        response = attachment_file_response(request, attachment, inline=True)
        # Do not cache beyond expiry or gzip byte ranges. Starlette's FileResponse
        # supplies HEAD, Content-Length and RFC byte-range responses for videos.
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Content-Encoding"] = "identity"
        response.headers["Content-Type"] = media_type
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
