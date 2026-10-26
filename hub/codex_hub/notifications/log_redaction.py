"""Keep email verification capabilities out of Uvicorn access logs."""
from __future__ import annotations

import logging


class VerificationAccessFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__("hub-email-verification-redaction")

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn supplies (client, method, full_path, http_version, status).
        # Keep access/audit metadata but remove the entire verification query.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            path = record.args[2]
            if isinstance(path, str):
                clean, separator, _ = path.partition("?")
                if separator and clean.rstrip("/").endswith("/notifications/email/verify"):
                    values = list(record.args)
                    values[2] = clean + "?token=[redacted]"
                    record.args = tuple(values)
        return True


def install_email_log_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(item.name == "hub-email-verification-redaction" for item in logger.filters):
        logger.addFilter(VerificationAccessFilter())
