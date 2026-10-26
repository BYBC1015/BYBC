from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    data_dir: Path = Path(".codex-hub-state")
    bind_host: str = "127.0.0.1"
    bind_port: int = 8797
    max_upload_bytes: int = 50 * 1024 * 1024
    session_cookie_name: str = "codex_hub_session"
    csrf_cookie_name: str = "codex_hub_csrf"
    secret_key: str = "local-dev-change-me"
    owner_username: str = "owner"
    owner_password: str | None = None
    inbox_url: str = "/inbox/"
    email_enabled: bool = False
    email_api_key: str = field(default="", repr=False)
    email_webhook_secret: str = field(default="", repr=False)
    email_from_address: str = "Hub <notify@notify.example.invalid>"
    email_public_base_url: str = ""
    email_domain_verified: bool = False
    email_poll_seconds: float = 2.0
    email_timeout_seconds: float = 20.0
    email_max_attempts: int = 6
    email_hourly_limit: int = 10
    email_daily_limit: int = 50
    email_rate_interval_seconds: float = 1.0
    mail_api_enabled: bool = False
    mail_api_hourly_limit: int = 30
    mail_api_daily_limit: int = 100
    server_ai_enabled: bool = False
    server_ai_poll_seconds: float = 2.0
    server_ai_request_timeout_seconds: float = 90.0
    server_ai_max_attempts: int = 3
    server_ai_history_entries: int = 20
    server_ai_max_context_chars: int = 30_000
    server_ai_max_output_tokens: int = 4_000
    server_ai_max_requests_per_day: int = 100
    server_ai_allow_custom_base_urls: bool = False
    deepseek_api_key: str = field(default="", repr=False)
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-pro"
    kimi_api_key: str = field(default="", repr=False)
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    kimi_model: str = "kimi-k3"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("CODEX_HUB_DATA_DIR", ".codex-hub-state"))
        owner_password = os.getenv("CODEX_HUB_OWNER_PASSWORD")
        return cls(
            data_dir=data_dir,
            bind_host=os.getenv("CODEX_HUB_BIND_HOST", "127.0.0.1"),
            bind_port=int(os.getenv("CODEX_HUB_BIND_PORT", "8797")),
            max_upload_bytes=int(
                os.getenv("CODEX_HUB_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))
            ),
            session_cookie_name=os.getenv(
                "CODEX_HUB_SESSION_COOKIE", "codex_hub_session"
            ),
            csrf_cookie_name=os.getenv("CODEX_HUB_CSRF_COOKIE", "codex_hub_csrf"),
            secret_key=os.getenv("CODEX_HUB_SECRET_KEY", "local-dev-change-me"),
            owner_username=os.getenv("CODEX_HUB_OWNER_USERNAME", "owner"),
            owner_password=owner_password,
            inbox_url=os.getenv("CODEX_HUB_INBOX_URL", "/inbox/"),
            email_enabled=_env_bool("CODEX_HUB_EMAIL_ENABLED"),
            email_api_key=os.getenv("CODEX_HUB_EMAIL_API_KEY", "").strip(),
            email_webhook_secret=os.getenv("CODEX_HUB_EMAIL_WEBHOOK_SECRET", "").strip(),
            email_from_address=os.getenv(
                "CODEX_HUB_EMAIL_FROM_ADDRESS", "Hub <notify@notify.example.invalid>"
            ).strip(),
            email_public_base_url=os.getenv("CODEX_HUB_EMAIL_PUBLIC_BASE_URL", "").strip().rstrip("/"),
            email_domain_verified=_env_bool("CODEX_HUB_EMAIL_DOMAIN_VERIFIED"),
            email_poll_seconds=max(0.5, float(os.getenv("CODEX_HUB_EMAIL_POLL_SECONDS", "2"))),
            email_timeout_seconds=min(60.0, max(1.0, float(os.getenv("CODEX_HUB_EMAIL_TIMEOUT_SECONDS", "20")))),
            email_max_attempts=min(6, max(1, int(os.getenv("CODEX_HUB_EMAIL_MAX_ATTEMPTS", "6")))),
            email_hourly_limit=max(1, int(os.getenv("CODEX_HUB_EMAIL_HOURLY_LIMIT", "10"))),
            email_daily_limit=max(1, int(os.getenv("CODEX_HUB_EMAIL_DAILY_LIMIT", "50"))),
            email_rate_interval_seconds=max(1.0, float(os.getenv("CODEX_HUB_EMAIL_RATE_INTERVAL_SECONDS", "1"))),
            mail_api_enabled=_env_bool("CODEX_HUB_MAIL_API_ENABLED"),
            mail_api_hourly_limit=min(1000, max(1, int(os.getenv("CODEX_HUB_MAIL_API_HOURLY_LIMIT", "30")))),
            mail_api_daily_limit=min(1000, max(1, int(os.getenv("CODEX_HUB_MAIL_API_DAILY_LIMIT", "100")))),
            server_ai_enabled=_env_bool("CODEX_HUB_SERVER_AI_ENABLED"),
            server_ai_poll_seconds=max(
                0.2, float(os.getenv("CODEX_HUB_SERVER_AI_POLL_SECONDS", "2"))
            ),
            server_ai_request_timeout_seconds=max(
                5.0,
                float(os.getenv("CODEX_HUB_SERVER_AI_REQUEST_TIMEOUT_SECONDS", "90")),
            ),
            server_ai_max_attempts=max(
                1, int(os.getenv("CODEX_HUB_SERVER_AI_MAX_ATTEMPTS", "3"))
            ),
            server_ai_history_entries=min(
                100,
                max(1, int(os.getenv("CODEX_HUB_SERVER_AI_HISTORY_ENTRIES", "20"))),
            ),
            server_ai_max_context_chars=min(
                200_000,
                max(
                    4_000,
                    int(os.getenv("CODEX_HUB_SERVER_AI_MAX_CONTEXT_CHARS", "30000")),
                ),
            ),
            server_ai_max_output_tokens=min(
                32_000,
                max(
                    256,
                    int(os.getenv("CODEX_HUB_SERVER_AI_MAX_OUTPUT_TOKENS", "4000")),
                ),
            ),
            server_ai_max_requests_per_day=min(
                10_000,
                max(
                    1,
                    int(os.getenv("CODEX_HUB_SERVER_AI_MAX_REQUESTS_PER_DAY", "100")),
                ),
            ),
            server_ai_allow_custom_base_urls=_env_bool(
                "CODEX_HUB_SERVER_AI_ALLOW_CUSTOM_BASE_URLS"
            ),
            deepseek_api_key=os.getenv("CODEX_HUB_DEEPSEEK_API_KEY", "").strip(),
            deepseek_base_url=os.getenv(
                "CODEX_HUB_DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
            ).strip(),
            deepseek_model=os.getenv(
                "CODEX_HUB_DEEPSEEK_MODEL", "deepseek-v4-pro"
            ).strip(),
            kimi_api_key=os.getenv("CODEX_HUB_KIMI_API_KEY", "").strip(),
            kimi_base_url=os.getenv(
                "CODEX_HUB_KIMI_BASE_URL", "https://api.moonshot.cn/v1"
            ).strip(),
            kimi_model=os.getenv("CODEX_HUB_KIMI_MODEL", "kimi-k3").strip(),
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "codex_hub.db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def releases_dir(self) -> Path:
        return self.data_dir / "releases"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.releases_dir.mkdir(parents=True, exist_ok=True)
