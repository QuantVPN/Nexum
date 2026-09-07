"""Application settings.

Values come from environment variables (prefix ``NEXUM_``) or a ``.env`` file.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEXUM_", env_file=".env", extra="ignore")

    app_name: str = "Nexum"
    environment: str = Field(default="development", description="development|test|production")
    debug: bool = False

    database_url: str = "sqlite:///./nexum.db"
    secret_key: str = Field(
        default="change-me-in-production",
        description="Signs session cookies. Must be set to a long random value in production.",
    )
    session_max_age_seconds: int = 60 * 60 * 12

    # Automation engine
    automation_enabled: bool = True
    automation_tick_seconds: int = 30
    automation_webhooks_enabled: bool = False
    automation_retry_attempts: int = 3
    automation_retry_backoff_seconds: float = 0.5

    # Company defaults used the first time the settings row is created
    company_name: str = "My company"
    timezone: str = "UTC"
    default_currency: str = "SEK"
    weekly_overtime_threshold_hours: float = 40.0
    overtime_multiplier: float = 1.5

    @property
    def is_test(self) -> bool:
        return self.environment == "test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings (used by tests that change the environment)."""
    get_settings.cache_clear()
