"""Application settings, read from environment variables and ``.env`` files.

Profiles (``GOVERNANCE_ENVIRONMENT``):

* ``development`` (default): local-preview sign-in, API docs on, migrations applied on start,
  data in ``.sites-runtime/preview`` unless ``GOVERNANCE_DATA_DIR`` is set.
* ``test``: like development, but ``GOVERNANCE_DATA_DIR`` must be given explicitly so a test run
  can never open the real workspace database.
* ``staging`` / ``production``: refuse to start with the local-preview sign-in or without the
  required configuration (see ``Settings.problems``). API docs off, HSTS on, secure cookies,
  migrations are verified but never applied automatically.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent
DEFAULT_DATA_DIR = PROJECT_DIR / ".sites-runtime" / "preview"

Environment = Literal["development", "test", "staging", "production"]


class ConfigurationError(RuntimeError):
    """Raised at startup when the configuration is unsafe or incomplete for the environment."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # The project-level .env (shared with the frontend tooling) is read first, then backend/.env.
        env_file=(PROJECT_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    governance_environment: Environment = "development"

    # Storage. The development default keeps using the existing local-preview data. Staging and
    # production must point this at a directory outside the source tree.
    governance_data_dir: Path = Field(default=DEFAULT_DATA_DIR)
    # Database. Empty = the SQLite file <data dir>/preview.sqlite (development and tests).
    # PostgreSQL (required in staging/production): postgresql+psycopg://user:password@host:5432/db
    # The URL is a secret (it usually contains the password): masked in reprs and never logged.
    governance_database_url: SecretStr = SecretStr("")
    # PostgreSQL connection pool per application process (workers × (size + overflow) must stay
    # below the server's max_connections), and per-statement limits.
    governance_db_pool_size: int = Field(5, ge=1, le=100)
    governance_db_max_overflow: int = Field(5, ge=0, le=100)
    governance_db_pool_timeout: int = Field(10, ge=1, le=300)
    governance_db_pool_recycle: int = Field(1800, ge=60)
    governance_db_statement_timeout_ms: int = Field(30000, ge=0)
    governance_db_lock_timeout_ms: int = Field(10000, ge=0)

    # Built React assets (index.html, app.js, style.css) produced by `pnpm frontend:build`.
    governance_frontend_dir: Path = Field(default=PROJECT_DIR / ".sites-runtime" / "frontend")

    host: str = "0.0.0.0"
    port: int = 8000
    # Host header values to accept besides 127.0.0.1:<port> (comma separated). Required in
    # staging/production: the public host name(s), e.g. "desk.example.com".
governance_allowed_hosts: str = (
    "ec2-13-201-73-29.ap-south-1.compute.amazonaws.com,"
    "localhost,127.0.0.1"
)

    # Authentication:
    # * "local-preview" (development/test only): opening the app issues an Owner session and
    #   "Preview as" switches profile. Refused in staging/production.
    # * "password": built-in email + password sign-in (argon2id), invitations, password reset.
    governance_auth_mode: Literal["local-preview", "password"] = "local-preview"
    governance_workspace_id: str = "local-preview"
    governance_session_hours: int = Field(12, ge=1, le=24 * 30)          # absolute lifetime
    governance_session_idle_minutes: int = Field(30, ge=5, le=24 * 60)   # password sessions only
    # Sign-in throttling: failures allowed per account / per client address within the window.
    governance_login_max_failures: int = Field(5, ge=1)
    governance_login_max_failures_per_client: int = Field(30, ge=1)
    governance_login_window_minutes: int = Field(15, ge=1)
    governance_activation_hours: int = Field(72, ge=1)
    governance_reset_minutes: int = Field(30, ge=5)
    # Number of reverse proxies in front of the app that append to X-Forwarded-For. 0 = use the
    # socket address (local). Only trust what your own proxies set.
    governance_forwarded_hops: int = Field(0, ge=0, le=5)
    # Public base URL used in emailed links (activation, password reset), e.g. https://desk.example.com
    governance_public_url: str = ""

    governance_reminders_enabled: bool = True
    governance_reminder_interval_seconds: int = 60

    # Unset (None) = environment default; set true/false to override.
    governance_api_docs: bool | None = None       # default: on in development/test
    governance_auto_migrate: bool | None = None   # default: on in development/test
    governance_hsts: bool | None = None           # default: on in staging/production
    # Full Content-Security-Policy override; empty = the built-in policy.
    governance_csp: str = ""

    governance_log_level: str = "INFO"
    governance_log_format: Literal["json", "text"] = "json"

    # Optional external services (unset = not configured). Secrets are masked in logs and reprs.
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = ""
    resend_api_key: SecretStr = SecretStr("")
    mail_from: str = ""
    # Email delivery: "outbox" writes messages to <data dir>/mail-outbox (development sink, nothing
    # leaves the machine), "resend" or "smtp" send for real, "disabled" drops them (logged).
    governance_mail_provider: Literal["outbox", "resend", "smtp", "disabled"] = "outbox"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_starttls: bool = True

    # Derived settings -------------------------------------------------------------------------
    @property
    def environment(self) -> str:
        return self.governance_environment

    @property
    def is_deployed(self) -> bool:
        """Staging and production: shared, network-reachable environments."""
        return self.governance_environment in ("staging", "production")

    @property
    def database_path(self) -> Path:
        """The SQLite database file (used when no GOVERNANCE_DATABASE_URL is set)."""
        return self.governance_data_dir / "preview.sqlite"

    @property
    def database_url(self) -> str:
        url = self.governance_database_url.get_secret_value().strip()
        if not url:
            return f"sqlite:///{self.database_path}"
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url

    @property
    def uses_postgres(self) -> bool:
        return self.database_url.startswith("postgresql+")

    @property
    def files_dir(self) -> Path:
        return self.governance_data_dir / "files"

    @property
    def extra_hosts(self) -> set[str]:
        return {h.strip() for h in self.governance_allowed_hosts.split(",") if h.strip()}

    @property
    def local_preview(self) -> bool:
        return self.governance_auth_mode == "local-preview"

    @property
    def password_auth(self) -> bool:
        return self.governance_auth_mode == "password"

    @property
    def outbox_dir(self) -> Path:
        return self.governance_data_dir / "mail-outbox"

    @property
    def api_docs_enabled(self) -> bool:
        return self.governance_api_docs if self.governance_api_docs is not None else not self.is_deployed

    @property
    def auto_migrate(self) -> bool:
        return self.governance_auto_migrate if self.governance_auto_migrate is not None else not self.is_deployed

    @property
    def hsts_enabled(self) -> bool:
        return self.governance_hsts if self.governance_hsts is not None else self.is_deployed

    @property
    def secure_cookies(self) -> bool:
        return self.is_deployed

    # Safety checks ----------------------------------------------------------------------------
    def problems(self) -> list[str]:
        """Configuration errors for this environment. Messages never contain secret values."""
        issues: list[str] = []
        data_dir_set = "governance_data_dir" in self.model_fields_set
        if self.governance_environment == "test" and not data_dir_set:
            issues.append("GOVERNANCE_DATA_DIR must be set explicitly in the test environment.")
        if not (self.database_url.startswith("sqlite:///") or self.uses_postgres):
            issues.append("GOVERNANCE_DATABASE_URL must be a postgresql+psycopg:// URL (or empty for the local SQLite file).")
        if self.is_deployed:
            env = self.governance_environment
            if self.local_preview:
                issues.append(f"GOVERNANCE_AUTH_MODE=local-preview is a development-only sign-in and is not allowed in {env}.")
            if not data_dir_set:
                issues.append(f"GOVERNANCE_DATA_DIR must be set explicitly in {env}.")
            elif _inside(self.governance_data_dir, PROJECT_DIR):
                issues.append(f"GOVERNANCE_DATA_DIR must be outside the source tree in {env}.")
            if not self.extra_hosts:
                issues.append(f"GOVERNANCE_ALLOWED_HOSTS must list the public host name(s) in {env}.")
            if self.governance_auto_migrate:
                issues.append(f"GOVERNANCE_AUTO_MIGRATE cannot be enabled in {env}; run `alembic upgrade head` as a deployment step.")
            if not self.uses_postgres:
                issues.append(f"GOVERNANCE_DATABASE_URL must point to PostgreSQL in {env}; SQLite is for development and tests.")
            if not self.governance_public_url.startswith("https://"):
                issues.append(f"GOVERNANCE_PUBLIC_URL must be the https:// address of the site in {env} (used in emailed links).")
            if self.governance_mail_provider == "outbox":
                issues.append(f"GOVERNANCE_MAIL_PROVIDER=outbox is a development sink; choose resend, smtp or disabled in {env}.")
        if self.governance_mail_provider == "resend" and not (self.resend_api_key.get_secret_value() and self.mail_from):
            issues.append("GOVERNANCE_MAIL_PROVIDER=resend requires RESEND_API_KEY and MAIL_FROM.")
        if self.governance_mail_provider == "smtp" and not (self.smtp_host and self.mail_from):
            issues.append("GOVERNANCE_MAIL_PROVIDER=smtp requires SMTP_HOST and MAIL_FROM.")
        return issues

    def validate_runtime(self) -> None:
        issues = self.problems()
        if issues:
            raise ConfigurationError(
                f"Refusing to start the {self.governance_environment} environment:\n  - " + "\n  - ".join(issues))


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
