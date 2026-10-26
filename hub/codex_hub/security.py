from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

PBKDF2_ITERATIONS = 260_000


@dataclass(frozen=True, slots=True)
class CreatedApiToken:
    raw: str
    prefix: str


def create_api_token() -> CreatedApiToken:
    raw = f"ch_{secrets.token_urlsafe(32)}"
    return CreatedApiToken(raw=raw, prefix=raw[:12])


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt, expected = encoded.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        int(iterations_text),
    ).hex()
    return hmac.compare_digest(digest, expected)


def create_session_token() -> str:
    return secrets.token_urlsafe(32)
