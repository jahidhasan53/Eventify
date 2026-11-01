"""Application settings, loaded from environment variables (and an optional .env file).

Nothing secret is hard-coded here. The app refuses to start with a placeholder or short
JWT secret (except when ENVIRONMENT=testing) so that a forgotten .env fails loudly.
"""

import hashlib
import hmac
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_SECRETS = {"", "change_me", "changeme", "secret", "your-secret-key"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # Application
    app_name: str = "Eventify"
    environment: Literal["development", "testing", "production"] = "development"
    api_v1_prefix: str = "/api/v1"

    # Database
    database_url: str
    test_database_url: str | None = None

    # Auth
    jwt_secret_key: SecretStr
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    ticket_hmac_key: SecretStr | None = None
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    password_reset_expire_minutes: int = 30

    # CORS / cookies
    frontend_origin: str = "http://localhost:5173"
    frontend_reset_url: str | None = None
    cookie_secure: bool | None = None
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_domain: str | None = None

    # Limits
    max_request_body_bytes: int = 1_048_576
    rate_limit_enabled: bool = True
    rate_limit_login_per_minute: int = 10
    rate_limit_sensitive_per_minute: int = 5
    rate_limit_global_per_minute: int = 300
    trust_proxy_headers: bool = False

    # E-mail
    email_backend: Literal["console", "smtp", "disabled"] = "console"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_use_starttls: bool = True
    email_from: str = "Eventify <no-reply@example.com>"

    # Background jobs
    enable_background_jobs: bool = True

    # ------------------------------------------------------------------ validators
    @field_validator("database_url", "test_database_url")
    @classmethod
    def _asyncpg_url(cls, value: str | None) -> str | None:
        if value and not value.startswith("postgresql+asyncpg://"):
            raise ValueError("Database URLs must use the postgresql+asyncpg:// scheme")
        return value

    @field_validator("cookie_secure", "cookie_domain", "frontend_reset_url", "ticket_hmac_key", mode="before")
    @classmethod
    def _empty_to_none(cls, value):
        # .env.example leaves these blank; treat blank as "unset".
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @model_validator(mode="after")
    def _validate_security(self) -> "Settings":
        secret = self.jwt_secret_key.get_secret_value()
        if self.environment != "testing" and (
            secret.strip().lower() in _PLACEHOLDER_SECRETS or len(secret) < 32
        ):
            raise ValueError(
                "JWT_SECRET_KEY must be a random value of at least 32 characters. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(64))"'
            )
        if "*" in self.cors_origins:
            raise ValueError("FRONTEND_ORIGIN must be an explicit allow-list; '*' is not allowed")
        if self.cookie_samesite == "none" and not self.use_secure_cookies:
            raise ValueError("COOKIE_SAMESITE=none requires secure cookies (HTTPS)")
        if self.environment == "production":
            if self.cookie_secure is False:
                raise ValueError("COOKIE_SECURE must not be false in production")
            insecure = [o for o in self.cors_origins if not o.startswith("https://")]
            if insecure:
                raise ValueError(f"All FRONTEND_ORIGIN entries must be https:// in production: {insecure}")
            if self.email_backend == "console":
                raise ValueError("EMAIL_BACKEND=console is not allowed in production (use smtp or disabled)")
        if self.email_backend == "smtp" and not self.smtp_host:
            raise ValueError("SMTP_HOST is required when EMAIL_BACKEND=smtp")
        return self

    # ------------------------------------------------------------------ derived values
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.frontend_origin.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def use_secure_cookies(self) -> bool:
        return self.cookie_secure if self.cookie_secure is not None else self.is_production

    @property
    def docs_enabled(self) -> bool:
        return not self.is_production

    @property
    def password_reset_url(self) -> str:
        return self.frontend_reset_url or f"{self.cors_origins[0]}/reset-password"

    @property
    def ticket_signing_key(self) -> bytes:
        """Key used to sign QR ticket tokens (kept separate from the JWT key)."""
        if self.ticket_hmac_key is not None:
            return self.ticket_hmac_key.get_secret_value().encode()
        derived = hmac.new(
            self.jwt_secret_key.get_secret_value().encode(), b"eventify-ticket-signing-v1", hashlib.sha256
        )
        return derived.digest()


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
