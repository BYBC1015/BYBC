"""Pinned Resend transport and Svix verification; never expose upstream bodies."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping

import httpx

EMAIL_ENDPOINT = "https://api.resend.com/emails"
MAX_WEBHOOK_BYTES = 256 * 1024


@dataclass(frozen=True)
class ProviderResult:
    provider_id: str
    status: str = "accepted"


class ProviderError(Exception):
    """Only the fixed code, never a request, credential or upstream message."""

    def __init__(self, code: str, *, retryable: bool = False,
                 uncertain: bool = False, retry_after: float | None = None,
                 pause_channel: bool = False, pause_recipient: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.uncertain = uncertain
        self.retry_after = retry_after
        self.pause_channel = pause_channel
        self.pause_recipient = pause_recipient


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, min(86400.0, seconds)) if seconds == seconds else None
    except (ValueError, OverflowError):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, min(86400.0, (target - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def _provider_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ProviderError("invalid_provider_response", uncertain=True, retryable=True)
    return value


class ResendProvider:
    def __init__(self, api_key: str, *, timeout: float = 20.0,
                 client: httpx.Client | None = None):
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, url: str, *, payload: dict | None = None,
                 idempotency_key: str | None = None) -> dict:
        if not self._api_key:
            raise ProviderError("credentials_missing", pause_channel=True)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._client.request(method, url, headers=headers, json=payload,
                                            follow_redirects=False)
        except (httpx.HTTPError, OSError):
            raise ProviderError("network_uncertain", retryable=True, uncertain=True) from None
        status = response.status_code
        if status == 429:
            raise ProviderError("rate_limited", retryable=True,
                                retry_after=_retry_after(response.headers.get("Retry-After")))
        if status >= 500 or status in (408, 425):
            raise ProviderError("provider_uncertain", retryable=True, uncertain=True)
        if status == 409:
            # Resend distinguishes a changed-payload conflict from an identical
            # idempotent request that is still being processed. Only consume the
            # documented classification; never expose the upstream message.
            try:
                conflict = response.json()
            except ValueError:
                conflict = {}
            if isinstance(conflict, dict) and conflict.get("name") == "concurrent_idempotent_requests":
                raise ProviderError("idempotency_in_progress", retryable=True, uncertain=True,
                                    retry_after=_retry_after(response.headers.get("Retry-After")))
        if not 200 <= status < 300:
            if status in (401, 403):
                if method == "GET":
                    # A sending-only key can submit mail but cannot retrieve it.
                    # Keep signed callbacks authoritative and retry infrequently
                    # in case the existing key's access is corrected later.
                    raise ProviderError("lookup_access_denied", retry_after=3600)
                raise ProviderError("credentials_or_domain_rejected", pause_channel=True)
            if status in (400, 409, 422):
                raise ProviderError("request_rejected", pause_channel=True)
            raise ProviderError("provider_rejected")
        try:
            result = response.json()
        except ValueError:
            raise ProviderError("invalid_provider_response", retryable=True, uncertain=True) from None
        if not isinstance(result, dict):
            raise ProviderError("invalid_provider_response", retryable=True, uncertain=True)
        return result

    def send(self, payload: dict, idempotency_key: str) -> ProviderResult:
        if not re.fullmatch(r"[A-Za-z0-9_/-]{8,200}", idempotency_key):
            raise ProviderError("invalid_idempotency_key", pause_channel=True)
        result = self._request("POST", EMAIL_ENDPOINT, payload=payload, idempotency_key=idempotency_key)
        return ProviderResult(_provider_id(result.get("id")))

    def lookup(self, provider_id: str) -> ProviderResult:
        provider_id = _provider_id(provider_id)
        result = self._request("GET", f"{EMAIL_ENDPOINT}/{provider_id}")
        returned_id = _provider_id(result.get("id"))
        if returned_id != provider_id:
            raise ProviderError("invalid_provider_response", retryable=True, uncertain=True)
        status = {
            "sent": "accepted", "delivered": "delivered", "delivery_delayed": "delivery_delayed",
            "bounced": "bounced", "complained": "complained", "failed": "failed",
            "suppressed": "suppressed", "queued": "accepted",
        }.get(str(result.get("last_event", "")).removeprefix("email."), "accepted")
        return ProviderResult(returned_id, status)


class WebhookVerificationError(ValueError):
    pass


def verify_webhook(raw_body: bytes, headers: Mapping[str, str], secret: str, *,
                   now: float | None = None, tolerance: int = 300) -> dict:
    """Verify the Svix wire contract against the original bytes before JSON parsing."""
    if len(raw_body) > MAX_WEBHOOK_BYTES or not secret:
        raise WebhookVerificationError("invalid_webhook")
    values = {key.lower(): value for key, value in headers.items()}
    event_id = values.get("svix-id", "")
    timestamp = values.get("svix-timestamp", "")
    signatures = values.get("svix-signature", "")
    try:
        if not event_id or len(event_id) > 256 or not timestamp.isascii() or not timestamp.isdigit():
            raise ValueError
        epoch = int(timestamp)
        if abs((time.time() if now is None else now) - epoch) > tolerance:
            raise ValueError
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
        if not key:
            raise ValueError
        signed = event_id.encode("utf-8") + b"." + timestamp.encode("ascii") + b"." + raw_body
        expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")
        valid = any(hmac.compare_digest(candidate[3:], expected)
                    for candidate in signatures.split() if candidate.startswith("v1,"))
        if not valid:
            raise ValueError
        result = json.loads(raw_body)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError):
        raise WebhookVerificationError("invalid_webhook") from None
