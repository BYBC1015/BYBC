"""Small fixed, private-by-default messages shared by preview and delivery."""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

BEIJING = timezone(timedelta(hours=8))
EVENT_LABELS = {
    "task.done": ("任务已完成", "Hub 中有一项任务已完成。"),
    "task.blocked": ("任务需要处理", "Hub 中有一项任务进入“阻塞”状态。"),
    "ai.failed": ("AI 处理失败", "Hub 中有一项服务器 AI 任务最终处理失败，请登录查看。"),
    "test": ("测试通知", "这是一封 Hub 邮件通知测试，用于检查接收情况。"),
    "verification": ("验证通知邮箱", "请确认使用此邮箱接收 Hub 自动通知。链接在 30 分钟内有效。"),
}


def clean_project_name(value: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:120]


def public_url(public_base_url: str, path: str) -> str:
    parsed = urlsplit(public_base_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("invalid_public_email_url")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + path, "", ""))


def display_time(value: datetime | str | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M（北京时间，UTC+8）")


def render_email(event_type: str, *, public_base_url: str, delivery_id: str,
                 occurred_at: datetime | str | None = None, project_name: str | None = None,
                 show_project_name: bool = False, verification_token: str | None = None) -> dict[str, str]:
    event_type = {"task_done": "task.done", "task_blocked": "task.blocked", "ai_failed": "ai.failed"}.get(event_type, event_type)
    if event_type not in EVENT_LABELS:
        raise ValueError("unsupported_email_event")
    title, description = EVENT_LABELS[event_type]
    name = clean_project_name(project_name) if show_project_name and event_type not in {"test", "verification"} else ""
    subject = f"[Hub] {title}" + (f" · {name}" if name else "")
    if event_type == "verification":
        if not verification_token:
            raise ValueError("verification_token_required")
        link = public_url(public_base_url, "/notifications/email/verify") + "?" + urlencode({"token": verification_token})
        action = "打开确认页面"
        footer = "如果你没有申请此操作，请忽略本邮件。仅打开链接不会完成验证，请在页面中确认。"
    elif event_type == "test":
        link = public_url(public_base_url, "/settings/notifications/email")
        action = "打开邮件通知设置"
        footer = "服务商接受请求不代表邮件进入收件箱。此邮件不表示任何任务已经完成。"
    else:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", delivery_id):
            raise ValueError("invalid_notification_identifier")
        link = public_url(public_base_url, "/notifications/email/open/" + quote(delivery_id, safe=""))
        action = "登录 Hub 查看详情"
        footer = "这是一封自动通知。可在 Hub 的邮件通知设置中停用提醒。"
    timestamp = display_time(occurred_at)
    lines = [description]
    if name:
        lines.append(f"项目：{name}")
    lines += [f"发生时间：{timestamp}", "", f"{action}：{link}", "", footer]
    paragraphs = "".join(f"<p>{html.escape(line)}</p>" for line in lines[:1 + bool(name)])
    body = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head>'
        '<body style="font-family:Arial,sans-serif;color:#172b4d;line-height:1.7;max-width:600px;margin:24px auto;padding:20px">'
        f"<h1 style=\"font-size:22px\">{html.escape(title)}</h1>{paragraphs}"
        f"<p>发生时间：{html.escape(timestamp)}</p>"
        f'<p><a href="{html.escape(link, quote=True)}">{html.escape(action)}</a></p>'
        f'<p style="font-size:13px;color:#596579">{html.escape(footer)}</p></body></html>'
    )
    return {"subject": subject, "text": "\n".join(lines), "html": body}
