"""Environment configuration and fail-closed production gates.

Values are read from the process environment.  This module deliberately does
not log configuration values: secrets must only be supplied by the runtime.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Final
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


PLACEHOLDER_VALUES: Final = frozenset(
    {"", "change-me", "changeme", "replace-me", "your-secret", "secret", "postgres"}
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: Environment = Environment.DEVELOPMENT
    database_url: str | None = None
    redis_url: str | None = None
    web_origin: str = "http://localhost:3100"
    app_signing_secret: str | None = None
    attachment_signing_secret: str | None = None
    resend_api_key: str | None = None
    resend_webhook_secret: str | None = None
    resend_enabled: bool = False
    yelp_api_key: str | None = None
    yelp_enabled: bool = False
    google_sheets_enabled: bool = False
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str | None = None
    integration_token_encryption_key: str | None = None
    doordash_reporting_token: str | None = None
    doordash_enabled: bool = False
    xai_api_key: str | None = None
    xai_enabled: bool = False
    xai_model: str = "grok-4.3"
    sentry_dsn: str | None = None
    observability_enabled: bool = False
    stripe_enabled: bool = False
    onboarding_invite_token: str | None = None
    cookie_secure: bool | None = Field(default=None, validation_alias="COOKIE_SECURE")

    @property
    def is_strict(self) -> bool:
        return self.app_env in {Environment.STAGING, Environment.PRODUCTION}

    @property
    def effective_cookie_secure(self) -> bool:
        return self.cookie_secure if self.cookie_secure is not None else self.is_strict


def _is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    return value.strip().casefold() in PLACEHOLDER_VALUES


def _require(settings: Settings, name: str, value: str | None, errors: list[str]) -> None:
    if _is_placeholder(value):
        errors.append(f"{name} is required for {settings.app_env.value}")


def validate_settings(settings: Settings) -> Settings:
    """Validate startup configuration without including secret values in errors."""
    errors: list[str] = []
    if settings.app_env not in set(Environment):
        errors.append("APP_ENV must be development, staging, or production")

    if settings.is_strict:
        for name, value in (
            ("DATABASE_URL", settings.database_url),
            ("REDIS_URL", settings.redis_url),
            ("APP_SIGNING_SECRET", settings.app_signing_secret),
            ("ATTACHMENT_SIGNING_SECRET", settings.attachment_signing_secret),
            # Self-service onboarding creates a tenant without any prior credential.
            # Staging/production must gate it behind an invite token so the endpoint
            # cannot be used to mint unlimited tenants from the open internet.
            ("ONBOARDING_INVITE_TOKEN", settings.onboarding_invite_token),
        ):
            _require(settings, name, value, errors)
        # Error reporting stays mandatory only when observability is switched on.
        # Deployments without a Sentry account must opt out explicitly rather than
        # silently starting with an unset DSN.
        if settings.observability_enabled:
            _require(settings, "SENTRY_DSN", settings.sentry_dsn, errors)
        if not settings.effective_cookie_secure:
            errors.append("COOKIE_SECURE must be true in staging/production")
        parsed_origin = urlparse(settings.web_origin)
        if parsed_origin.scheme != "https" or not parsed_origin.netloc:
            errors.append("WEB_ORIGIN must be a valid https origin in staging/production")

    if settings.resend_enabled:
        _require(settings, "RESEND_API_KEY", settings.resend_api_key, errors)
        _require(settings, "RESEND_WEBHOOK_SECRET", settings.resend_webhook_secret, errors)
    if settings.yelp_enabled:
        _require(settings, "YELP_API_KEY", settings.yelp_api_key, errors)
    if settings.google_sheets_enabled:
        for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", settings.google_oauth_client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", settings.google_oauth_client_secret),
            ("GOOGLE_OAUTH_REDIRECT_URI", settings.google_oauth_redirect_uri),
            ("INTEGRATION_TOKEN_ENCRYPTION_KEY", settings.integration_token_encryption_key),
        ):
            _require(settings, name, value, errors)
    if settings.doordash_enabled:
        _require(settings, "DOORDASH_REPORTING_TOKEN", settings.doordash_reporting_token, errors)
    if settings.xai_enabled:
        _require(settings, "XAI_API_KEY", settings.xai_api_key, errors)
    if settings.stripe_enabled:
        errors.append("STRIPE_ENABLED must remain false until billing approval")

    if errors:
        raise RuntimeError("invalid application configuration: " + "; ".join(errors))
    return settings


def load_settings(*, validate: bool = False) -> Settings:
    settings = Settings()
    return validate_settings(settings) if validate else settings


def startup_validation() -> None:
    """Validate the process environment at application startup."""
    validate_settings(load_settings())


def environment_from_process() -> str:
    """Small diagnostic helper that never returns secrets."""
    return os.getenv("APP_ENV", Environment.DEVELOPMENT.value)
