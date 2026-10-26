import logging

import pytest

from codex_hub.notifications.log_redaction import VerificationAccessFilter, install_email_log_redaction


@pytest.mark.parametrize("path", [
    "/notifications/email/verify?token=private-one-time-token",
    "/hub/notifications/email/verify?extra=secret&token=private-one-time-token",
])
def test_verification_access_log_redacts_query_but_keeps_status(path):
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                               '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:1234", "GET", path, "1.1", 200), None)
    assert VerificationAccessFilter().filter(record)
    message = record.getMessage()
    assert "private-one-time-token" not in message and "extra=secret" not in message
    assert "token=[redacted]" in message
    assert "GET" in message and "200" in message


def test_unrelated_access_logs_unchanged_and_install_idempotent():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                               '%s - "%s %s HTTP/%s" %d',
                               ("client", "GET", "/projects?list=1", "1.1", 200), None)
    before = record.getMessage()
    VerificationAccessFilter().filter(record)
    assert record.getMessage() == before
    install_email_log_redaction()
    install_email_log_redaction()
    assert sum(item.name == "hub-email-verification-redaction"
               for item in logging.getLogger("uvicorn.access").filters) == 1
