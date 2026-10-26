#!/usr/bin/env python3
"""Send or inspect Hub mail using Python's standard library.

Set HUB_MAIL_API_URL to the /api/v1/mail/messages endpoint and
HUB_MAIL_API_KEY to an application mail key. Never put the key in arguments.
Reuse the same event ID and unchanged content when retrying one logical event.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATES = {"queued", "sending", "retry_wait", "accepted", "delivered", "delivery_delayed",
          "bounced", "complained", "suppressed", "failed", "uncertain", "cancelled", "expired"}


class MailClientError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward the bearer credential to a redirected address.
        return None


def settings() -> tuple[str, str]:
    endpoint = os.environ.get("HUB_MAIL_API_URL", "").rstrip("/")
    key = os.environ.get("HUB_MAIL_API_KEY", "").strip()
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment
                 and parsed.path.endswith("/api/v1/mail/messages")
                 and all(ord(c) > 32 and ord(c) < 127 for c in endpoint))
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise MailClientError("Set HUB_MAIL_API_URL to the HTTPS mail API endpoint.")
    if not re.fullmatch(r"hmail_[A-Za-z0-9_-]{43}", key):
        raise MailClientError("Set HUB_MAIL_API_KEY to a valid application mail key.")
    return endpoint, key


def request(method: str, url: str, key: str, *, body: bytes | None = None,
            event_id: str | None = None, attempts: int = 4) -> dict:
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if event_id is not None:
        headers["Idempotency-Key"] = event_id
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(attempts):
        delay = min(30.0, 2.0 ** attempt)
        try:
            with opener.open(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=30) as response:
                raw = response.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    raise MailClientError("Mail API returned an oversized response.")
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise MailClientError("Mail API returned an invalid response.") from None
                if not isinstance(result, dict):
                    raise MailClientError("Mail API returned an invalid response.")
                return result
        except urllib.error.HTTPError as exc:
            status = exc.code
            error_code = "request_failed"
            try:
                error = json.loads(exc.read(64 * 1024))
                candidate = error.get("error", {}).get("code")
                if isinstance(candidate, str) and re.fullmatch(r"[a-z0-9_]{1,80}", candidate):
                    error_code = candidate
            except (ValueError, UnicodeError, AttributeError):
                pass
            retry_after = exc.headers.get("Retry-After", "")
            if retry_after.isdigit():
                delay = min(60.0, max(delay, int(retry_after)))
            exc.close()
            if status not in {408, 425, 429} and not 500 <= status <= 599:
                raise MailClientError(f"Mail API HTTP {status}: {error_code}.") from None
            if attempt + 1 == attempts:
                raise MailClientError(f"Mail API HTTP {status}: {error_code}; retry later with the same event ID and content.") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            if attempt + 1 == attempts:
                raise MailClientError("Mail API connection failed; retry later with the same event ID and content.") from None
        time.sleep(delay)
    raise MailClientError("Mail API request did not complete.")


def public_result(result: dict) -> dict:
    message_id, deliveries = result.get("id"), result.get("deliveries")
    if not isinstance(message_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id) or not isinstance(deliveries, list):
        raise MailClientError("Mail API returned an invalid message record.")
    clean = []
    for item in deliveries:
        if not isinstance(item, dict) or item.get("status") not in STATES:
            raise MailClientError("Mail API returned an invalid delivery record.")
        delivery_id = item.get("id")
        if not isinstance(delivery_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", delivery_id):
            raise MailClientError("Mail API returned an invalid delivery record.")
        clean.append({"id": delivery_id, "status": item["status"]})
    return {"id": message_id, "deliveries": clean}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    send = commands.add_parser("send", help="Queue mail for one logical event.")
    send.add_argument("--to", action="append", required=True, help="Recipient; repeat for additional recipients (up to 20).")
    send.add_argument("--subject", required=True)
    content = send.add_mutually_exclusive_group(required=True)
    content.add_argument("--text")
    content.add_argument("--text-file", type=Path, help="UTF-8 plain-text file.")
    send.add_argument("--event-id", required=True, help="Stable 8–128 printable ASCII characters without spaces.")
    status = commands.add_parser("status", help="Read delivery state without sending another message.")
    status.add_argument("message_id")
    args = parser.parse_args()
    try:
        endpoint, key = settings()
        if args.command == "send":
            if not re.fullmatch(r"[\x21-\x7e]{8,128}", args.event_id):
                raise MailClientError("Event ID must contain 8–128 printable ASCII characters without spaces.")
            if not 1 <= len(args.to) <= 20:
                raise MailClientError("Specify 1–20 recipients.")
            try:
                text = args.text_file.read_text(encoding="utf-8") if args.text_file else args.text
            except (OSError, UnicodeError):
                raise MailClientError("Cannot read the UTF-8 message file.") from None
            payload = {"to": args.to, "subject": args.subject, "text": text}
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(body) > 256 * 1024 or len(text.encode("utf-8")) > 128 * 1024:
                raise MailClientError("Message exceeds the API content limit.")
            result = request("POST", endpoint, key, body=body, event_id=args.event_id)
        else:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", args.message_id):
                raise MailClientError("Invalid message ID.")
            result = request("GET", endpoint + "/" + args.message_id, key)
        print(json.dumps(public_result(result), ensure_ascii=False))
        return 0
    except MailClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
