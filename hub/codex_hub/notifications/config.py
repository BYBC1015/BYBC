"""Validated deployment prerequisites for outbound notifications."""
from __future__ import annotations

from email.utils import getaddresses
from urllib.parse import urlsplit

from ..config import Settings


def valid_public_base_url(value: str) -> bool:
    if not value or any(ord(char) < 33 for char in value) or "\\" in value:
        return False
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.port in (None, 443)
            and not any(part in {".", ".."} for part in parsed.path.split("/"))
            and "%" not in parsed.path
        )
    except ValueError:
        return False


def valid_sender(value: str) -> bool:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    addresses = getaddresses([value])
    if len(addresses) != 1:
        return False
    address = addresses[0][1]
    local, sep, domain = address.rpartition("@")
    return bool(
        sep and local and "." in domain
        and len(address) <= 254
        and not any(char.isspace() for char in address)
        and not any(char in address for char in '<>,;"')
    )


def readiness(settings: Settings) -> dict[str, object]:
    """Report configured prerequisites, without exposing or probing credentials.

    Domain verification is an explicit deployment attestation after checking the
    provider dashboard, not an assertion that this function queried DNS.
    """
    reasons: list[str] = []
    if not settings.email_enabled:
        reasons.append("服务器尚未开放邮件发送")
    if not settings.email_api_key:
        reasons.append("尚未配置发送服务凭证")
    if not settings.email_webhook_secret:
        reasons.append("尚未配置投递回调验证密钥")
    if not settings.email_domain_verified:
        reasons.append("尚未确认发信域验证完成")
    if not valid_sender(settings.email_from_address):
        reasons.append("发件人地址无效")
    if not valid_public_base_url(settings.email_public_base_url):
        reasons.append("尚未配置有效的 Hub HTTPS 地址")
    if not settings.secret_key or settings.secret_key == "local-dev-change-me":
        reasons.append("需要配置专用的 Hub 安全密钥")
    return {"ready": not reasons, "reasons": reasons}


def service_ready(settings: Settings) -> bool:
    return bool(readiness(settings)["ready"])
