"""Tests for RFC-004 path resolution via environment variables.

This test module verifies the get_db_path() function and environment
variable support for database path resolution.
"""

import os
import tempfile
from pathlib import Path

import pytest

from system.host.environment import get_db_path


class TestGetDbPath:
    """Tests for get_db_path() function (RFC-004)."""

    def test_get_db_path_invalid_layer(self):
        """Test that invalid layer raises ValueError."""
        with pytest.raises(ValueError, match="Invalid layer"):
            get_db_path("invalid")

    def test_get_db_path_layer_specific_override_soil(self):
        """Test layer-specific override for soil (MEMOGARDEN_SOIL_DB)."""
        custom_path = "/custom/soil.db"
        os.environ["MEMOGARDEN_SOIL_DB"] = custom_path

        try:
            result = get_db_path("soil")
            assert result == Path(custom_path)
        finally:
            del os.environ["MEMOGARDEN_SOIL_DB"]

    def test_get_db_path_layer_specific_override_core(self):
        """Test layer-specific override for core (MEMOGARDEN_CORE_DB)."""
        custom_path = "/custom/core.db"
        os.environ["MEMOGARDEN_CORE_DB"] = custom_path

        try:
            result = get_db_path("core")
            assert result == Path(custom_path)
        finally:
            del os.environ["MEMOGARDEN_CORE_DB"]

    def test_get_db_path_shared_data_dir_soil(self):
        """Test shared data directory for soil (MEMOGARDEN_DATA_DIR)."""
        data_dir = "/data"
        os.environ["MEMOGARDEN_DATA_DIR"] = data_dir

        try:
            result = get_db_path("soil")
            assert result == Path(f"{data_dir}/soil.db")
        finally:
            del os.environ["MEMOGARDEN_DATA_DIR"]

    def test_get_db_path_shared_data_dir_core(self):
        """Test shared data directory for core (MEMOGARDEN_DATA_DIR)."""
        data_dir = "/data"
        os.environ["MEMOGARDEN_DATA_DIR"] = data_dir

        try:
            result = get_db_path("core")
            assert result == Path(f"{data_dir}/core.db")
        finally:
            del os.environ["MEMOGARDEN_DATA_DIR"]

    def test_get_db_path_layer_specific_takes_precedence(self):
        """Test that layer-specific override takes precedence over data dir."""
        layer_path = "/layer/soil.db"
        data_dir = "/data"

        os.environ["MEMOGARDEN_SOIL_DB"] = layer_path
        os.environ["MEMOGARDEN_DATA_DIR"] = data_dir

        try:
            result = get_db_path("soil")
            # Layer-specific should win
            assert result == Path(layer_path)
            assert result != Path(f"{data_dir}/soil.db")
        finally:
            del os.environ["MEMOGARDEN_SOIL_DB"]
            del os.environ["MEMOGARDEN_DATA_DIR"]

    def test_get_db_path_default_current_dir_soil(self):
        """Test default path (current directory) for soil."""
        # Ensure no env vars are set
        for key in ["MEMOGARDEN_SOIL_DB", "MEMOGARDEN_DATA_DIR"]:
            os.environ.pop(key, None)

        result = get_db_path("soil")
        assert result == Path("./soil.db")

    def test_get_db_path_default_current_dir_core(self):
        """Test default path (current directory) for core."""
        # Ensure no env vars are set
        for key in ["MEMOGARDEN_CORE_DB", "MEMOGARDEN_DATA_DIR"]:
            os.environ.pop(key, None)

        result = get_db_path("core")
        assert result == Path("./core.db")


class TestCorePathResolution:
    """Tests for Core database path resolution integration."""

    def test_core_uses_explicit_path(self, tmp_path):
        """Test that Core uses explicit path when provided."""
        from utils.config import Settings
        from system.core import _create_connection

        db_path = tmp_path / "test_core.db"
        settings = Settings(database_path=str(db_path))

        # Temporarily replace default_settings
        import system.core
        original_settings = system.core.settings
        system.core.settings = settings

        try:
            conn = _create_connection()
            assert db_path.exists()
            conn.close()
        finally:
            system.core.settings = original_settings

    def test_core_uses_env_var_path(self, tmp_path, monkeypatch):
        """Test that Core uses environment variable path when database_path is None."""
        from utils.config import Settings
        from system.core import _create_connection

        db_path = tmp_path / "env_core.db"
        monkeypatch.setenv("MEMOGARDEN_CORE_DB", str(db_path))

        settings = Settings(database_path=None)  # None triggers env var resolution

        # Temporarily replace default_settings
        import system.core
        original_settings = system.core.settings
        system.core.settings = settings

        try:
            conn = _create_connection()
            assert db_path.exists()
            conn.close()
        finally:
            system.core.settings = original_settings

    def test_core_uses_data_dir_path(self, tmp_path, monkeypatch):
        """Test that Core uses data directory path when database_path is None."""
        from utils.config import Settings
        from system.core import _create_connection

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("MEMOGARDEN_DATA_DIR", str(data_dir))

        settings = Settings(database_path=None)  # None triggers env var resolution

        # Temporarily replace default_settings
        import system.core
        original_settings = system.core.settings
        system.core.settings = settings

        try:
            conn = _create_connection()
            assert (data_dir / "core.db").exists()
            conn.close()
        finally:
            system.core.settings = original_settings


class TestSoilPathResolution:
    """Tests for Soil database path resolution integration."""

    def test_soil_uses_explicit_path(self, tmp_path):
        """Test that get_soil() uses explicit path when provided."""
        from system.soil import get_soil

        db_path = tmp_path / "test_soil.db"
        soil = get_soil(db_path, init=False)

        assert soil.db_path == db_path

    def test_soil_uses_env_var_path(self, tmp_path, monkeypatch):
        """Test that get_soil() uses environment variable path when db_path is None."""
        from system.soil import get_soil

        db_path = tmp_path / "env_soil.db"
        monkeypatch.setenv("MEMOGARDEN_SOIL_DB", str(db_path))

        soil = get_soil(None, init=False)
        assert soil.db_path == db_path

    def test_soil_uses_data_dir_path(self, tmp_path, monkeypatch):
        """Test that get_soil() uses data directory path when db_path is None."""
        from system.soil import get_soil

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("MEMOGARDEN_DATA_DIR", str(data_dir))

        soil = get_soil(None, init=False)
        assert soil.db_path == (data_dir / "soil.db")

    def test_soil_backward_compatible_default(self):
        """Test that get_soil() without arguments uses default path (backward compatible)."""
        from system.soil import get_soil

        # Ensure no env vars are set
        for key in ["MEMOGARDEN_SOIL_DB", "MEMOGARDEN_DATA_DIR"]:
            os.environ.pop(key, None)

        soil = get_soil(init=False)
        assert soil.db_path == Path("./soil.db")
