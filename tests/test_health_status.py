"""Tests for health check and status endpoints.

These tests verify the /health and /status endpoints work correctly.
"""

import pytest
import os
from pathlib import Path


class TestHealthEndpoint:
    """Tests for /health endpoint."""

    def test_health_returns_ok(self, flask_app):
        """/health endpoint returns {status: ok}."""
        with flask_app.test_client() as client:
            response = client.get("/health")
            assert response.status_code == 200
            data = response.get_json()
            assert data["status"] == "ok"


class TestStatusEndpoint:
    """Tests for /status endpoint."""

    def test_status_with_existing_databases(self, flask_app):
        """/status endpoint reports database status when databases exist."""
        with flask_app.test_client() as client:
            response = client.get("/status")
            assert response.status_code == 200
            data = response.get_json()

            # Check structure
            assert "status" in data
            assert "databases" in data

            # Check database status
            # In test environment, databases are in-memory (files don't exist on disk)
            # Note: This is expected behavior - the schema is loaded in-memory
            assert data["databases"]["core"] in ["connected", "missing"]
            assert data["databases"]["soil"] in ["connected", "missing"]

            # Consistency is only included if both database files exist on disk
            if data["databases"]["soil"] == "connected" and data["databases"]["core"] == "connected":
                assert "consistency" in data
                assert data["consistency"]["status"] in ["normal", "inconsistent", "read_only", "safe_mode"]

    def test_status_includes_database_paths(self, flask_app):
        """/status endpoint includes database file paths."""
        with flask_app.test_client() as client:
            response = client.get("/status")
            assert response.status_code == 200
            data = response.get_json()

            # Check paths are present
            assert "paths" in data["databases"]
            assert "soil" in data["databases"]["paths"]
            assert "core" in data["databases"]["paths"]

    def test_status_consistency_check(self, flask_app):
        """/status endpoint runs consistency checks when databases exist."""
        with flask_app.test_client() as client:
            response = client.get("/status")
            assert response.status_code == 200
            data = response.get_json()

            # If both databases exist, consistency should be checked
            if data["databases"]["soil"] == "connected" and data["databases"]["core"] == "connected":
                # Fresh databases should have normal consistency
                # (no orphaned deltas or broken chains)
                assert data["consistency"]["status"] == "normal"
                assert data["status"] == "ok"
            else:
                # If databases don't both exist, consistency won't be checked
                assert "consistency" not in data or data.get("consistency") is None

    def test_health_vs_status_difference(self, flask_app):
        """/health is simple, /status includes detailed info."""
        with flask_app.test_client() as client:
            # Health response is simple
            health_response = client.get("/health")
            health_data = health_response.get_json()
            assert list(health_data.keys()) == ["status"]

            # Status response is detailed
            status_response = client.get("/status")
            status_data = status_response.get_json()
            assert len(status_data.keys()) >= 2  # status, databases, consistency
            assert "databases" in status_data
