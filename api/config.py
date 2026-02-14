"""Configuration management for MemoGarden API.

Extends system.config.Settings with API-specific configuration following RFC 004.
All configuration is loaded from TOML files (no more .env files).

Configuration sections (RFC 004):
- [runtime]: Resource profile and runtime parameters
- [paths]: Path overrides (data_dir, config_dir, log_dir)
- [network]: Bind address and port
- [security]: Encryption and authentication settings
- [api]: API-specific settings (prefix, CORS)

Environment Variables (RFC 004):
- MEMOGARDEN_VERB: Deployment verb (serve/run/deploy)
- MEMOGARDEN_CONFIG: Explicit config file path override
"""

import os
from pathlib import Path
from typing import Optional

from utils.config import Settings, get_config_path


def _get_default_verb() -> str:
    """Get default verb from MEMOGARDEN_VERB environment variable.

    RFC 004: Verb-based deployment configuration.
    - Default to "run" for local development
    - Use "serve" for production system daemon
    - Use "deploy" for container environments

    Returns:
        Verb string (serve, run, or deploy)
    """
    return os.environ.get("MEMOGARDEN_VERB", "run")


def _get_config_path() -> Optional[Path]:
    """Get config path from MEMOGARDEN_CONFIG environment variable.

    RFC 004: --config flag override via environment variable.

    Returns:
        Path to config file if set, None otherwise
    """
    config_path = os.environ.get("MEMOGARDEN_CONFIG")
    return Path(config_path) if config_path else None


class APISettings(Settings):
    """Application settings loaded from TOML config (RFC 004).

    Extends the system package configuration with API-specific settings.

    Configuration Resolution:
    1. Load from TOML config file (based on verb: serve/run/deploy)
    2. Apply resource profile defaults
    3. Apply [api] section settings
    4. Fall back to built-in defaults

    Example config.toml:
        [runtime]
        resource_profile = "standard"

        [network]
        bind_address = "127.0.0.1"
        bind_port = 8080

        [api]
        cors_origins = ["http://localhost:3000"]

        [security]
        jwt_secret_key = "change-me"
        jwt_expiry_days = 30
        bypass_localhost_check = false
        bcrypt_work_factor = 12
    """

    def __init__(
        self,
        database_path: Optional[str] = None,
        default_currency: str = "SGD",
        config_path: Optional[Path] = None,
        verb: str = "run",
    ):
        """Initialize API settings.

        Args:
            database_path: Path to Core database file. If None, resolved
                via get_db_path('core') using environment variables.
            default_currency: Default currency code (e.g., "SGD", "USD")
            config_path: Optional explicit path to config.toml
            verb: Deployment verb (serve, run, deploy) for config resolution
        """
        super().__init__(database_path, default_currency, config_path, verb)

        # Apply API-specific settings from TOML config
        self._apply_api_config()

    def _apply_api_config(self):
        """Apply API-specific configuration from TOML [api] section."""
        api_config = self._config.get("api", {})

        # API configuration
        self.api_v1_prefix = api_config.get("api_v1_prefix", "/api/v1")
        self.cors_origins = api_config.get(
            "cors_origins",
            ["http://localhost:3000"]
        )

        # JWT configuration (from [security] section for RFC 004 compliance)
        security_config = self._config.get("security", {})
        self.jwt_secret_key = security_config.get(
            "jwt_secret_key",
            "change-me-in-production-use-config-file"
        )
        self.jwt_expiry_days = security_config.get("jwt_expiry_days", 30)

        # Security configuration
        self.bypass_localhost_check = security_config.get("bypass_localhost_check", False)
        self.bcrypt_work_factor = security_config.get("bcrypt_work_factor", 12)


# Default settings instance
# Uses MEMOGARDEN_VERB env var if set (for RFC 004 compliance)
# Defaults to "run" for local development
# In production deployment, MEMOGARDEN_VERB should be "serve"
settings = APISettings(
    verb=_get_default_verb(),
    config_path=_get_config_path()
)
