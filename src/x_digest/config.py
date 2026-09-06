"""Validated application configuration."""

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_X_SCOPE = "bookmark.read tweet.read users.read offline.access"
DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_LLM_MODEL = "openai/gpt-oss-120b"
TELEGRAM_BOT_TOKEN_ACCOUNT = "telegram-bot-token"
TELEGRAM_CHAT_ID_ACCOUNT = "telegram-chat-id"
OPENROUTER_API_KEY_ACCOUNT = "openrouter-api-key"


def folder_is_ignored(folder_id: str, name: str, ignore_folders: list[str]) -> bool:
    """Return True when a folder ID or name matches an ignore entry."""
    if not ignore_folders:
        return False
    return any(
        folder_id == str(ignored) or name.casefold() == str(ignored).casefold()
        for ignored in ignore_folders
    )


def _find_project_root() -> Path:
    """Walk up from this file to find the project root with pyproject.toml."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


class Settings(BaseSettings):
    """Configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="XDIGEST_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    vault_path: Path = Field(default_factory=lambda: _find_project_root() / "data")
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
    ignore_folders: Annotated[list[str], NoDecode] = Field(default_factory=list)
    folder_sync_days: int = Field(default=7, ge=0)
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_max_bytes: int = Field(default=5_000_000, gt=0)
    log_backups: int = Field(default=5, ge=0)
    telegram_bot_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("XDIGEST_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
    )
    telegram_chat_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("XDIGEST_TELEGRAM_CHAT_ID", "TELEGRAM_USER_ID"),
    )
    telegram_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_api_key: str | None = None
    llm_model: str = DEFAULT_LLM_MODEL
    llm_max_tokens: int = Field(default=1200, ge=100, le=4000)
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    digest_max_posts: int = Field(default=20, ge=1, le=50)
    digest_max_chars_per_post: int = Field(default=800, ge=100, le=4000)
    digest_prompt_max_chars: int = Field(default=12000, ge=2000, le=100000)

    @field_validator("telegram_bot_token", "telegram_chat_id", "llm_api_key", mode="before")
    @classmethod
    def empty_secret_becomes_missing(cls, value: Any) -> Any:
        """Normalize blank credentials to missing so absence is explicit."""
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("vault_path", mode="after")
    @classmethod
    def normalize_vault_path(cls, value: Path) -> Path:
        """Resolve the vault once so persisted paths are independent of CWD."""
        return value.expanduser().resolve()

    @field_validator("x_scope", mode="before")
    @classmethod
    def use_default_scope_when_empty(cls, value: str | None) -> str:
        """Prevent an empty environment value from creating an invalid request."""
        return value.strip() if isinstance(value, str) and value.strip() else DEFAULT_X_SCOPE

    @field_validator("ignore_folders", mode="before")
    @classmethod
    def split_ignore_folders(cls, value: Any) -> list[str]:
        """Accept a comma-separated list of folder names or IDs."""
        if value is None:
            return []
        if isinstance(value, str):
            value = value.split(",")
        return [str(item).strip() for item in value if str(item).strip()]

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

    def telegram_enabled(self) -> bool:
        """Return True when Telegram delivery credentials are configured."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def llm_enabled(self) -> bool:
        """Return True when an LLM API key is configured."""
        return bool(self.llm_api_key)


def load_settings() -> Settings:
    """Load and validate application settings."""
    return Settings()
