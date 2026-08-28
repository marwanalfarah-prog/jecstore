"""Application settings — loaded from environment only (Part II 5: no hardcoded secrets)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application ---
    app_env: Literal["development", "staging", "production"] = "development"
    app_debug: bool = True
    app_secret_key: str = "dev-only-insecure-key"
    app_base_url: str = "http://127.0.0.1:8000"
    log_level: str = "INFO"
    log_json: bool = False

    # --- Database ---
    database_url: str = "sqlite:///./data/jec_store.db"
    database_echo: bool = False

    # --- Sessions ---
    redis_url: str = "redis://localhost:6379/0"
    session_cookie_name: str = "jec_session"
    session_cookie_secure: bool = False
    session_idle_timeout_minutes: int = 120

    # --- Localisation ---
    default_language: Literal["ar", "en"] = "ar"
    default_currency: Literal["JOD", "USD"] = "JOD"
    numeral_system: Literal["western", "arabic-indic"] = "western"
    default_usd_rate: float = 1.41

    # --- Email ---
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = False
    #: "console" logs each message instead of sending — the safe default for
    #: development, where an unreachable SMTP host would otherwise record a
    #: failure for every queued email. "smtp" delivers for real.
    mail_transport: Literal["console", "smtp"] = "console"
    mail_from: str = "no-reply@jecjordan.com"
    mail_from_name: str = "شبيبة ستور"

    # --- Abuse protection ---
    captcha_provider: Literal["none", "turnstile", "recaptcha"] = "none"
    captcha_site_key: str = ""
    captcha_secret_key: str = ""
    captcha_failed_attempts_threshold: int = 3
    rate_limit_login_per_minute: int = 10
    rate_limit_register_per_hour: int = 5
    rate_limit_password_reset_per_hour: int = 5
    login_lockout_threshold: int = 8
    login_lockout_minutes: int = 15

    # --- Search ---
    search_backend: Literal["sql", "meilisearch", "typesense"] = "sql"
    meilisearch_url: str = "http://localhost:7700"
    meilisearch_api_key: str = ""

    # --- Uploads ---
    media_root: Path = Field(default=PROJECT_ROOT / "media")
    media_url: str = "/media"
    max_upload_mb: int = 10

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def templates_dir(self) -> Path:
        return PROJECT_ROOT / "app" / "templates"

    @property
    def static_dir(self) -> Path:
        return PROJECT_ROOT / "app" / "static"

    @property
    def locales_dir(self) -> Path:
        return PROJECT_ROOT / "locales"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
