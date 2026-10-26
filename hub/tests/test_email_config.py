from dataclasses import replace

import pytest

from codex_hub.config import Settings
from codex_hub.notifications.config import readiness, service_ready, valid_public_base_url


def ready_settings():
    return Settings(
        email_enabled=True,
        email_api_key="private-provider-key",
        email_webhook_secret="private-webhook-secret",
        email_public_base_url="https://hub.example.com/hub",
        email_domain_verified=True,
        secret_key="private-session-secret",
    )


def test_readiness_defaults_closed_and_credentials_never_in_summary():
    assert not service_ready(Settings())
    settings = ready_settings()
    assert service_ready(settings)
    assert "private-provider-key" not in repr(settings)
    assert "private-webhook-secret" not in repr(settings)
    assert "private-provider-key" not in str(readiness(settings))
    for field, value in [
        ("email_enabled", False), ("email_api_key", ""),
        ("email_domain_verified", False), ("email_webhook_secret", ""),
        ("secret_key", "local-dev-change-me"),
        ("email_from_address", "Hub <sender@example.com>\r\nBcc: other@example.com"),
    ]:
        assert not service_ready(replace(settings, **{field: value}))


@pytest.mark.parametrize("value", [
    "http://hub.example.com", "https://user:password@hub.example.com",
    "https://hub.example.com?redirect=bad", "https://hub.example.com/#fragment",
    "https://hub.example.com/../evil", "https://hub.example.com/%2f/evil",
    "https://hub.example.com\r\n/evil", "https://hub.example.com:invalid",
    "https://hub.example.com\\evil",
])
def test_public_url_rejects_ambiguous_or_unsafe_links(value):
    assert not valid_public_base_url(value)


def test_environment_limits_and_defaults(monkeypatch):
    monkeypatch.setenv("CODEX_HUB_EMAIL_ENABLED", "true")
    monkeypatch.setenv("CODEX_HUB_EMAIL_MAX_ATTEMPTS", "100")
    monkeypatch.setenv("CODEX_HUB_EMAIL_TIMEOUT_SECONDS", "999")
    monkeypatch.setenv("CODEX_HUB_EMAIL_RATE_INTERVAL_SECONDS", "0")
    settings = Settings.from_env()
    assert settings.email_enabled
    assert settings.email_max_attempts == 6
    assert settings.email_timeout_seconds == 60
    assert settings.email_rate_interval_seconds == 1
