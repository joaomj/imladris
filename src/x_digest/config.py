"""Validated application configuration."""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_X_SCOPE = "bookmark.read tweet.read users.read offline.access"


class Settings(BaseSettings):
    """Configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_prefix="XDIGEST_", env_file=".env")

    vault_path: Path = Field(
        default_factory=lambda: Path.home() / "Library" / "Application Support" / "x-digest"
    )
    x_client_id: str | None = None
    x_client_secret: str | None = None
    x_redirect_uri: str = "http://localhost:8080/callback"
    x_scope: str = DEFAULT_X_SCOPE
    keychain_service: str = "x-digest"
    max_results_per_page: int = Field(default=100, ge=1, le=100)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_base_seconds: float = Field(default=1.0, gt=0, le=60)
    api_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    media_max_bytes: int = Field(default=100_000_000, gt=0)
    media_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    scheduler_interval_seconds: int = Field(default=3600, gt=0)

    @field_validator("x_scope", mode="before")
    @classmethod
    def use_default_scope_when_empty(cls, value: str | None) -> str:
        """Prevent an empty environment value from creating an invalid request."""
        return value.strip() if isinstance(value, str) and value.strip() else DEFAULT_X_SCOPE

    @property
    def database_path(self) -> Path:
        """Return the Silver and Gold database path."""
        return self.vault_path / "silver.sqlite"

    @property
    def log_path(self) -> Path:
        """Return the structured application log path."""
        return self.vault_path / "logs" / "application.jsonl"

    @property
    def lock_path(self) -> Path:
        """Return the process lock path."""
        return self.vault_path / "run.lock"


def load_settings() -> Settings:
    """Load and validate application settings."""
    return Settings()
