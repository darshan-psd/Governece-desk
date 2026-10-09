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
    # Built React assets (index.html, app.js, style.css) produced by `pnpm frontend:build`.
    governance_frontend_dir: Path = Field(default=PROJECT_DIR / ".sites-runtime" / "frontend")

    host: str = "127.0.0.1"
    port: int = 5174
    # Host header values to accept besides 127.0.0.1:<port> (comma separated). Required in
    # staging/production: the public host name(s), e.g. "desk.example.com".
    governance_allowed_hosts: str = ""

    # Authentication. Only "local-preview" exists in this phase (development and test only):
    # opening the app issues an owner session cookie and "Preview as" switches profile. Any other
    # value disables the local login, so every API request returns 401.
    governance_auth_mode: str = "local-preview"
    governance_workspace_id: str = "local-preview"
    governance_session_hours: int = 12

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
        return self.governance_data_dir / "preview.sqlite"

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
