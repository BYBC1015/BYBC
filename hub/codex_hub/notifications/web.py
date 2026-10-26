"""Owner-only notification controls. Web requests only persist work; never send mail."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import readiness, service_ready
from .provider import WebhookVerificationError, verify_webhook
from .storage import NotificationStore
from .templates import render_email

SETTINGS_PATH = "/settings/notifications/email"
MAX_WEBHOOK_BYTES = 256 * 1024
EVENT_LABELS = {
    "task.done": "任务完成", "task.blocked": "任务需要处理",
    "ai.failed": "服务器 AI 最终失败", "verification": "邮箱验证", "test": "测试通知",
}
STATUS_LABELS = {
    "queued": "等待发送", "sending": "正在提交", "retry_wait": "等待重试",
    "accepted": "服务商已接收", "delivered": "对方服务器已接收",
    "delivery_delayed": "服务商投递延迟", "bounced": "退信", "complained": "收件人投诉",
    "suppressed": "服务商已停止投递",
    "failed": "发送失败", "uncertain": "等待核查", "cancelled": "已取消", "expired": "已过期",
}
ERROR_LABELS = {
    "credentials_missing": "尚未配置发送凭证。",
    "credentials_or_domain_rejected": "服务商拒绝了发信凭证或域名，请核查配置后恢复通道。",
    "lookup_access_denied": "当前凭证不支持查询投递状态，等待服务商回调更新；不影响后续发送。",
    "request_rejected": "服务商拒绝了发送请求，请核查发送配置。",
    "provider_rejected": "服务商未接受本次发送请求。",
    "network_uncertain": "网络响应中断，服务商是否已接受请求尚待核查。",
    "provider_uncertain": "服务商返回临时异常，发送结果尚待核查。",
    "invalid_provider_response": "服务商响应无法确认，需要继续核查。",
    "rate_limited": "服务商发送频率受限，正在等待重试。",
    "quota_wait": "已达到通知发送额度，正在等待可用额度。",
    "notification_disabled": "对应通知规则已关闭。",
    "recipient_revoked": "收件地址已移除或替换。",
    "recipient_paused": "收件地址已暂停。",
    "recipient_suppressed": "该地址因退信或投诉已暂停发送，请检查后手动恢复。",
    "manual_reconciliation_required": "本次发送结果需要人工核查，系统不会盲目重发。",
    "backup_restored": "本记录来自历史备份，已暂停外发并等待核查。",
}
NOTICE_LABELS = {
    "saved": "通知规则已保存。", "recipient": "收件地址已保存，请完成邮箱验证。",
    "verification": "验证邮件已加入发送队列。请在收到后打开链接并主动确认。",
    "test": "测试通知已加入发送队列。请结合发送记录与收件邮箱检查结果。",
    "revoked": "收件地址已移除，已停止向该地址提交新的邮件。",
    "resumed": "收件地址已恢复，请检查通知规则和发送结果。",
    "verified": "邮箱验证成功。现在可以发送测试通知，再开启自动提醒。",
    "channel": "发送通道已恢复。旧的取消或待核查邮件不会重新发送，请手动开启新通知。",
}


def masked_address(value: str) -> str:
    local, sep, domain = str(value or "").partition("@")
    return f"{local[:1]}***@{domain}" if sep else "—"


def local_time(value: Any) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return "—"


def register_email_notification_routes(app: FastAPI) -> None:
    # Imported when app creation runs, after main has defined its shared helpers.
    from ..main import (
        current_user, get_repo, get_settings, redirect_to, render,
        require_user, verify_csrf_token,
    )

    def owner(request: Request) -> dict[str, Any]:
        user = require_user(request)
        if user.get("role") != "owner":
            raise HTTPException(403, "只有管理员可以管理邮件通知")
        return user

    def store(request: Request) -> NotificationStore:
        return NotificationStore(get_repo(request).connection)

    async def owner_form(request: Request):
        user = owner(request)
        form = await request.form()
        verify_csrf_token(request, str(form.get("_csrf", "")))
        return user, form

    def require_ready(request: Request) -> None:
        if not service_ready(get_settings(request)):
            raise HTTPException(409, "发送服务尚未就绪，请先完成服务配置与发信域验证")
        if store(request).get_channel_state().get("paused_reason"):
            raise HTTPException(409, "发送通道已暂停，请先核查原因并恢复通道")

    def recipient_ready(recipient: dict[str, Any] | None) -> bool:
        return bool(recipient and recipient.get("verified_at") and
                    not recipient.get("revoked_at") and not recipient.get("paused_reason"))

    def action_error(exc: ValueError) -> None:
        # Only fixed messages are exposed; provider errors and request snapshots stay private.
        limited = any(word in str(exc).lower() for word in ("limit", "frequent", "cooldown", "频繁"))
        raise HTTPException(429 if limited else 409,
                            "操作过于频繁，请稍后重试" if limited else "当前状态无法完成此操作，请检查收件地址、验证状态与通知规则") from exc

    @app.get(SETTINGS_PATH, response_class=HTMLResponse)
    def email_settings(request: Request, tab: str = "rules", event: str = "task.blocked", notice: str = ""):
        if not current_user(request):
            return redirect_to(request, "/login")
        user = owner(request)
        db = store(request)
        recipient = db.get_recipient(user["id"])
        prefs = db.get_settings(user["id"])
        selected_tab = tab if tab in {"rules", "preview", "history"} else "rules"
        selected_event = event if event in {"task.done", "task.blocked", "ai.failed", "test"} else "task.blocked"
        service = readiness(get_settings(request))
        channel = db.get_channel_state()
        deployment_ready = bool(service["ready"])
        if channel.get("paused_reason"):
            service = {"ready": False, "reasons": [*service["reasons"], "发送通道已暂停，请核查服务配置或备份恢复情况"]}
        preview = None
        if selected_tab == "preview":
            preview = render_email(
                selected_event, public_base_url="https://preview.invalid/hub",
                delivery_id="preview-only", occurred_at=datetime.now(timezone.utc).isoformat(),
                project_name="示例项目", show_project_name=bool(prefs["show_project_name"]),
            )
        return render(request, "email_notifications.html", user=user, prefs=prefs,
                      recipient=recipient, recipient_ready=recipient_ready(recipient),
                      service=service, channel=channel, deployment_ready=deployment_ready, tab=selected_tab,
                      event=selected_event, preview=preview,
                      deliveries=db.list_deliveries(user["id"], limit=50) if selected_tab == "history" else [],
                      event_labels=EVENT_LABELS, status_labels=STATUS_LABELS,
                      local_time=local_time, masked_address=masked_address,
                      notice=NOTICE_LABELS.get(notice, ""))

    @app.post(SETTINGS_PATH + "/rules")
    async def update_rules(request: Request):
        user, form = await owner_form(request)
        enabled = form.get("enabled") == "on"
        if enabled:
            require_ready(request)
        try:
            store(request).update_settings(user["id"], enabled=enabled,
                task_done=form.get("task_done") == "on", task_blocked=form.get("task_blocked") == "on",
                ai_failed=form.get("ai_failed") == "on", show_project_name=form.get("show_project_name") == "on")
        except ValueError as exc:
            action_error(exc)
        return redirect_to(request, SETTINGS_PATH + "?notice=saved")

    @app.post(SETTINGS_PATH + "/recipient")
    async def save_recipient(request: Request):
        user, form = await owner_form(request)
        address = str(form.get("address", "")).strip()
        if len(address) > 254:
            raise HTTPException(422, "请输入有效的收件邮箱")
        try:
            store(request).set_recipient(user["id"], address)
        except ValueError as exc:
            raise HTTPException(422, "请输入有效的收件邮箱") from exc
        return redirect_to(request, SETTINGS_PATH + "?notice=recipient")

    @app.post(SETTINGS_PATH + "/verify")
    async def send_verification(request: Request):
        user, _ = await owner_form(request)
        require_ready(request)
        db = store(request)
        recipient = db.get_recipient(user["id"])
        if not recipient:
            raise HTTPException(409, "请先保存收件地址")
        try:
            db.request_verification(user["id"], recipient["address"], token_secret=get_settings(request).secret_key)
        except ValueError as exc:
            action_error(exc)
        return redirect_to(request, SETTINGS_PATH + "?notice=verification")

    @app.post(SETTINGS_PATH + "/test")
    async def send_test(request: Request):
        user, _ = await owner_form(request)
        require_ready(request)
        db = store(request)
        if not recipient_ready(db.get_recipient(user["id"])):
            raise HTTPException(409, "请先验证并恢复收件地址")
        try:
            db.enqueue_test(user["id"])
        except ValueError as exc:
            action_error(exc)
        return redirect_to(request, SETTINGS_PATH + "?tab=history&notice=test")

    @app.post(SETTINGS_PATH + "/recipient/remove")
    async def remove_recipient(request: Request):
        user, _ = await owner_form(request)
        store(request).revoke_recipient(user["id"])
        return redirect_to(request, SETTINGS_PATH + "?notice=revoked")

    @app.post(SETTINGS_PATH + "/recipient/resume")
    async def resume_recipient(request: Request):
        user, form = await owner_form(request)
        if form.get("cause_fixed") != "on":
            raise HTTPException(422, "请先确认退信或投诉原因已经处理")
        try:
            store(request).resume_recipient(user["id"])
        except ValueError as exc:
            action_error(exc)
        return redirect_to(request, SETTINGS_PATH + "?notice=resumed")

    @app.post(SETTINGS_PATH + "/channel/resume")
    async def resume_channel(request: Request):
        user, form = await owner_form(request)
        if form.get("cause_fixed") != "on":
            raise HTTPException(422, "请先确认发送配置、域名和历史待核查记录已经检查")
        if not service_ready(get_settings(request)):
            raise HTTPException(409, "请先完成发送服务配置")
        store(request).resume_channel(user["id"])
        return redirect_to(request, SETTINGS_PATH + "?notice=channel")

    @app.get("/notifications/email/verify", response_class=HTMLResponse)
    def verification_form(request: Request, token: str = ""):
        if not current_user(request):
            return render(request, "email_verification.html", user=None, verification=None,
                          token="", login_required=True, response_status_code=401)
        user = owner(request)
        verification = store(request).inspect_verification(token) if len(token) <= 512 else None
        if not verification or verification.get("owner_id") != user["id"]:
            return render(request, "email_verification.html", user=user, verification=None,
                          token="", login_required=False, response_status_code=400)
        return render(request, "email_verification.html", user=user, verification=verification,
                      token=token, login_required=False)

    @app.post("/notifications/email/verify")
    async def verify_recipient(request: Request):
        user, form = await owner_form(request)
        token = str(form.get("token", ""))
        db = store(request)
        verification = db.inspect_verification(token) if len(token) <= 512 else None
        if not verification or verification.get("owner_id") != user["id"]:
            raise HTTPException(400, "验证链接已失效，请重新发送验证邮件")
        if not db.confirm_verification(token):
            raise HTTPException(400, "验证链接已失效，请重新发送验证邮件")
        return redirect_to(request, SETTINGS_PATH + "?notice=verified")

    @app.get("/notifications/email/open/{delivery_id}")
    def open_notification(request: Request, delivery_id: str):
        if not current_user(request):
            return redirect_to(request, "/login")
        user = owner(request)
        delivery = store(request).get_delivery(user["id"], delivery_id)
        if not delivery:
            raise HTTPException(404, "通知不存在")
        if delivery["event_type"] in {"test", "verification"}:
            return redirect_to(request, SETTINGS_PATH)
        repo = get_repo(request)
        project = repo.connection.execute("SELECT id,slug FROM projects WHERE id=?", (delivery.get("project_id"),)).fetchone()
        # Owners have global project access in Hub; delivery ownership was checked above.
        if not project:
            raise HTTPException(404, "通知对应的项目已不存在")
        path = f"/projects/{project['slug']}"
        if delivery["event_type"] in {"task.done", "task.blocked"}:
            task = repo.get_task(delivery["entity_id"])
            if not task or task["project_id"] != project["id"]:
                raise HTTPException(404, "通知对应的任务已不存在")
            path += f"/tasks/{task['id']}"
        return redirect_to(request, path)

    @app.get(SETTINGS_PATH + "/deliveries/{delivery_id}", response_class=HTMLResponse)
    def delivery_detail(request: Request, delivery_id: str):
        user = owner(request)
        db = store(request)
        delivery = db.get_delivery(user["id"], delivery_id)
        if not delivery:
            raise HTTPException(404, "发送记录不存在")
        return render(request, "email_delivery.html", user=user, delivery=delivery,
                      event_labels=EVENT_LABELS, status_labels=STATUS_LABELS,
                      attempts=db.list_attempts(user["id"], delivery_id),
                      attempt_labels=STATUS_LABELS, error_labels=ERROR_LABELS,
                      local_time=local_time, masked_address=masked_address)

    @app.post("/api/webhooks/email/resend")
    async def email_webhook(request: Request):
        secret = get_settings(request).email_webhook_secret
        if not secret:
            raise HTTPException(503, "邮件回调尚未配置")
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) < 0 or int(length) > MAX_WEBHOOK_BYTES:
                    raise HTTPException(413, "回调内容过大")
            except ValueError as exc:
                raise HTTPException(400, "无效的回调内容") from exc
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > MAX_WEBHOOK_BYTES:
                raise HTTPException(413, "回调内容过大")
        try:
            event = verify_webhook(bytes(chunks), request.headers, secret)
        except WebhookVerificationError as exc:
            raise HTTPException(400, "邮件回调验签失败") from exc
        data = event.get("data") if isinstance(event, dict) else None
        event_type = event.get("type") if isinstance(event, dict) else None
        occurred_at = event.get("created_at") if isinstance(event, dict) else None
        provider_id = data.get("email_id") if isinstance(data, dict) else None
        event_id = request.headers.get("svix-id", "")
        # Match stored field limits so truncation cannot collapse two signed IDs.
        if not all(isinstance(v, str) and 0 < len(v) <= limit for v, limit in (
            (event_type, 80), (occurred_at, 64), (provider_id, 200), (event_id, 200),
        )):
            raise HTTPException(400, "无效的邮件回调")
        bounce = data.get("bounce")
        bounce_type = str(bounce.get("type", "")).lower() if isinstance(bounce, dict) else ""
        hard_bounce = bounce_type in {"permanent", "hard", "hard_bounce"}
        try:
            from ..mail.storage import MailStore
            notification_store = store(request)
            # Each ledger records the signed event independently, including retries
            # already recorded by its sibling while a provider ID was unresolved.
            with notification_store.transaction():
                notification_store.ingest_webhook(event_id, event_type, provider_id, occurred_at,
                                                 hard_bounce=hard_bounce)
                MailStore(notification_store.connection).ingest_webhook(
                    event_id, event_type, provider_id, occurred_at, hard_bounce=hard_bounce)
        except ValueError as exc:
            raise HTTPException(400, "无效的邮件回调") from exc
        return JSONResponse({"ok": True})
