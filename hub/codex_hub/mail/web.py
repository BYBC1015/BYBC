"""Versioned application mail API and session-only owner management."""
from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..notifications.config import service_ready
from ..notifications.storage import NotificationStore
from ..notifications.web import STATUS_LABELS, local_time
from .storage import MAX_REQUEST_BYTES, MailError, MailStore

API_PATH = "/api/v1/mail/messages"
MANAGE_PATH = "/mail"
ERROR_MESSAGES = {
    "invalid_token": "请使用有效的发信访问密钥。",
    "invalid_fields": "仅支持 to、subject、text、html 和 reply_to。",
    "invalid_recipients": "请提供 1 至 20 个有效收件地址。",
    "invalid_subject": "主题须为 1 至 200 字符，且不能包含换行或控制字符。",
    "invalid_content": "邮件正文必须是有效文本。",
    "content_required": "请提供 text 或 html 正文。",
    "content_too_large": "正文合计不能超过 128 KiB，请求不能超过 256 KiB。",
    "invalid_reply_to": "回复地址格式无效。",
    "invalid_idempotency_key": "Idempotency-Key 须为 8 至 128 个可打印的非空格 ASCII 字符。",
    "idempotency_conflict": "此 Idempotency-Key 已用于不同内容；请核查原请求。",
    "recipient_suppressed": "请求包含因退信或投诉而暂停的收件地址。",
    "quota_exceeded": "已达到此密钥或发信接口的额度，请稍后使用相同 Idempotency-Key 重试。",
    "service_unavailable": "发信接口暂不可用，请检查发送配置或通道状态。",
    "sender_unavailable": "发件地址尚未配置。",
    "invalid_json": "请求必须是有效的 UTF-8 JSON 对象。",
    "unsupported_media_type": "请使用 Content-Type: application/json。",
    "message_not_found": "未找到此密钥对应的发送记录。",
    "invalid_limit": "额度须为 1 至 1000 的整数。",
    "invalid_quota_mode": "请选择有效的密钥额度模式。",
    "invalid_token_name": "密钥名称须为 1 至 80 字符。",
    "token_not_found": "访问密钥不存在。",
    "owner_required": "只有管理员可以管理发信接口。",
}


def public_message(row: dict) -> dict:
    """Explicit public projection; never return snapshots, hashes, or leases."""
    return {
        "id": row["id"], "created_at": row["created_at"],
        "deliveries": [{key: delivery.get(key) for key in (
            "id", "address", "status", "created_at", "updated_at", "last_error"
        )} for delivery in row["deliveries"]],
    }


def register_mail_routes(app: FastAPI) -> None:
    from ..main import current_user, get_repo, get_settings, redirect_to, render, require_user, verify_csrf_token

    @app.exception_handler(MailError)
    async def mail_error(request: Request, exc: MailError):
        headers = {"Cache-Control": "no-store"}
        if exc.status_code == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if exc.status_code == 429:
            headers["Retry-After"] = "60"
        return JSONResponse({"error": {"code": exc.code, "message": ERROR_MESSAGES.get(exc.code, "当前状态无法完成此操作。")}},
                            status_code=exc.status_code, headers=headers)

    def store(request: Request) -> MailStore:
        return MailStore(get_repo(request).connection)

    def api_token(request: Request) -> dict:
        authorization = request.headers.get("authorization", "")
        parts = authorization.split(" ")
        token = store(request).authenticate_token(parts[1]) if len(parts) == 2 and parts[0].lower() == "bearer" else None
        if token is None:
            raise MailError("invalid_token", 401)
        return token

    def owner(request: Request) -> dict:
        user = require_user(request)
        if user.get("role") != "owner":
            raise HTTPException(403, "只有管理员可以管理发信接口")
        return user

    async def owner_form(request: Request):
        user = owner(request)
        form = await request.form()
        verify_csrf_token(request, str(form.get("_csrf", "")))
        return user, form

    @app.post(API_PATH)
    async def send_message(request: Request):
        token = api_token(request)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise MailError("unsupported_media_type", 415)
        length = request.headers.get("content-length")
        if length:
            try:
                size = int(length)
            except ValueError:
                raise MailError("invalid_json", 400) from None
            if size < 0 or size > MAX_REQUEST_BYTES:
                raise MailError("content_too_large", 413)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST_BYTES:
                raise MailError("content_too_large", 413)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise MailError("invalid_json", 400) from None
        settings = get_settings(request)
        if (not settings.mail_api_enabled or not service_ready(settings)
                or NotificationStore(get_repo(request).connection).get_channel_state().get("paused_reason")):
            raise MailError("service_unavailable", 503)
        result = store(request).enqueue(token["id"], payload, request.headers.get("idempotency-key", ""),
            from_address=settings.email_from_address,
            hourly_limit=settings.mail_api_hourly_limit, daily_limit=settings.mail_api_daily_limit)
        return JSONResponse({**public_message(result), "replayed": result["replayed"]},
                            status_code=200 if result["replayed"] else 202)

    @app.get(API_PATH)
    def list_messages(request: Request):
        token = api_token(request)
        return {"messages": [public_message(row) for row in store(request).list_requests(token["id"], limit=50)]}

    @app.get(API_PATH + "/{message_id}")
    def get_message(request: Request, message_id: str):
        token = api_token(request)
        result = store(request).get_request(token["id"], message_id)
        if not result:
            raise MailError("message_not_found", 404)
        return public_message(result)

    def management_page(request: Request, user: dict, *, created=None, notice=""):
        db, settings = store(request), get_settings(request)
        channel = NotificationStore(get_repo(request).connection).get_channel_state()
        history = []
        for row in db.list_owner_requests(user["id"], limit=50):
            history.append({**public_message(row), "subject": json.loads(row["payload_json"])["subject"]})
        return render(request, "mail.html", user=user, created=created, notice=notice,
            tokens=db.list_tokens(user["id"]), history=history, suppressions=db.list_suppressions(user["id"]),
            ready=settings.mail_api_enabled and service_ready(settings) and not channel.get("paused_reason"),
            sender=settings.email_from_address, hourly_limit=settings.mail_api_hourly_limit,
            daily_limit=settings.mail_api_daily_limit, status_labels=STATUS_LABELS, local_time=local_time,
            endpoint=settings.email_public_base_url.rstrip("/") + API_PATH)

    @app.get(MANAGE_PATH, response_class=HTMLResponse)
    def manage(request: Request, notice: str = ""):
        if not current_user(request):
            return redirect_to(request, "/login")
        return management_page(request, owner(request), notice={
            "revoked": "访问密钥已撤销，尚未提交的邮件已取消。", "resumed": "地址已恢复；旧邮件不会自动重发。",
        }.get(notice, ""))

    @app.post(MANAGE_PATH + "/tokens", response_class=HTMLResponse)
    async def create_token(request: Request):
        user, form = await owner_form(request)
        quota_exempt = form.get("quota_exempt") == "on"
        if quota_exempt:
            # Kept for compatibility in storage; exempt keys do not use these caps.
            hourly, daily = 10, 50
        else:
            try:
                hourly, daily = int(str(form.get("hourly_limit", "10"))), int(str(form.get("daily_limit", "50")))
            except ValueError:
                raise MailError("invalid_limit") from None
        created = store(request).create_token(user["id"], str(form.get("name", "")),
            hourly_limit=hourly, daily_limit=daily, quota_exempt=quota_exempt)
        return management_page(request, user, created=created)

    @app.post(MANAGE_PATH + "/tokens/{token_id}/revoke")
    async def revoke_token(request: Request, token_id: str):
        user, _ = await owner_form(request)
        store(request).revoke_token(user["id"], token_id)
        return redirect_to(request, MANAGE_PATH + "?notice=revoked")

    @app.post(MANAGE_PATH + "/recipients/resume")
    async def resume_address(request: Request):
        user, form = await owner_form(request)
        if form.get("cause_fixed") != "on":
            raise HTTPException(422, "请先确认退信或投诉原因已经处理")
        try:
            store(request).resume_address(user["id"], str(form.get("address", "")))
        except ValueError:
            raise MailError("invalid_recipients") from None
        return redirect_to(request, MANAGE_PATH + "?notice=resumed")
