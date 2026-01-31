"""Configuration management for MemoGarden API.

Extends system.config.Settings with API-specific configuration.
"""

from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Extends the system package configuration with API-specific settings.
    """

    # Database configuration (extends system.config.Settings)
    database_path: str = "./data/core.db"
    default_currency: str = "SGD"

    # API configuration
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = ["http://localhost:3000"]

    # JWT Configuration
    jwt_secret_key: str = "change-me-in-production-use-env-var"
    jwt_expiry_days: int = 30

    # Security Configuration
    # For testing: bypass localhost-only checks (e.g., admin registration)
    bypass_localhost_check: bool = False

    # Bcrypt work factor (higher = more secure but slower)
    # 12 is a good balance for security and performance (default is 10)
    # For tests, use 4 for faster execution while maintaining functionality
    bcrypt_work_factor: int = 12

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False
    )


# Default settings instance
settings = Settings()
