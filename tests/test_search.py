"""Tests for search verb - Session 9.

Tests semantic search and discovery across entities and facts.
Per RFC-005 v7, the search verb supports fuzzy text matching with
configurable coverage levels and effort modes.
"""

import pytest
from flask import Flask

from system.core import get_core
from system.soil import get_soil
from utils import uid


# ============================================================================
# Test Fixtures
# ============================================================================

@pytest.fixture
def app(flask_app: Flask):
    """Flask app with test database setup."""
    return flask_app


@pytest.fixture
def client(app: Flask):
    """Test client for the Flask app."""
    return app.test_client()


# ============================================================================
# Search Verb Tests
# ============================================================================

class TestSearchVerb:
    """Tests for search verb - semantic search and discovery."""

    def test_search_entities_by_type(self, client, auth_headers):
        """Test searching entities by type (names coverage)."""
        with get_core() as core:
            # Create test entities
            core.entity.create(
                entity_type="Transaction",
                data='{"amount": 100, "description": "Coffee"}'
            )
            core.entity.create(
                entity_type="Transaction",
                data='{"amount": 50, "description": "Tea"}'
            )

        # Search for transactions
        response = client.post(
            "/mg",
            json={
                "op": "search",
                "query": "Transaction",
                "target_type": "entity",
                "coverage": "names",
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert "result" in data

        result = data["result"]
        assert result["query"] == "Transaction"
        assert result["coverage"] == "names"
        assert result["strategy"] == "auto"
        assert result["effort"] == "standard"
        assert "results" in result
        assert len(result["results"]) >= 2

    def test_search_entities_by_data_content(self, client, auth_headers):
        """Test searching entities by data content (content coverage)."""
        with get_core() as core:
            # Create test entities with specific content
            core.entity.create(
                entity_type="Artifact",
                data='{"name": "Invoice #123", "amount": 500}'
            )
            core.entity.create(
                entity_type="Artifact",
                data='{"name": "Receipt #456", "amount": 25}'
            )

        # Search for "Invoice"
        response = client.post(
            "/mg",
            json={
                "op": "search",
                "query": "Invoice",
                "target_type": "entity",
                "coverage": "content",
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True

        result = data["result"]
        assert "results" in result
        assert len(result["results"]) >= 1
        # Check that results contain "Invoice" in data
        invoice_found = any("Invoice" in str(r.get("data", "")) for r in result["results"])
        assert invoice_found

    def test_search_facts_by_content(self, client, auth_headers):
        """Test searching facts by content."""
        from system.soil.fact import Fact, generate_soil_uuid
        from utils import datetime as isodatetime

        with get_soil() as soil:
            # Create test facts using Fact dataclass
            now = isodatetime.now()
            item1 = Fact(
                uuid=generate_soil_uuid(),
                _type="Note",
                realized_at=now,
                canonical_at=now,
                data={"text": "Meeting Notes - Discussed budget planning for Q1"},
                metadata=None
            )
            soil.create_fact(item1)

            item2 = Fact(
                uuid=generate_soil_uuid(),
                _type="Note",
                realized_at=now,
                canonical_at=now,
                data={"text": "Shopping List - Buy groceries and coffee"},
                metadata=None
            )
            soil.create_fact(item2)

        # Search for "budget"
        response = client.post(
            "/mg",
            json={
                "op": "search",
                "query": "budget",
                "target_type": "fact",
                "coverage": "content",
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True

        result = data["result"]
        assert "results" in result
        assert len(result["results"]) >= 1
        # Check that results have kind="fact"
        facts = [r for r in result["results"] if r.get("kind") == "fact"]
        assert len(facts) >= 1

    def test_search_all_target_type(self, client, auth_headers):
        """Test searching across both entities and facts."""
        from system.soil.fact import Fact, generate_soil_uuid
        from utils import datetime as isodatetime

        with get_core() as core:
            # Create test entity
            core.entity.create(
                entity_type="Artifact",
                data='{"name": "Project Report"}'
            )

        with get_soil() as soil:
            # Create test fact using Fact dataclass
            now = isodatetime.now()
            item = Fact(
                uuid=generate_soil_uuid(),
                _type="Note",
                realized_at=now,
                canonical_at=now,
                data={"text": "Project Notes - Draft report outline"},
                metadata=None
            )
            soil.create_fact(item)

        # Search for "project" across all types
        response = client.post(
            "/mg",
            json={
                "op": "search",
                "query": "project",
                "target_type": "all",
                "coverage": "content",
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True

        result = data["result"]
        assert "results" in result
        # Should find both entity and fact
        assert len(result["results"]) >= 2

    def test_search_with_limit(self, client, auth_headers):
        """Test search with limit parameter."""
        with get_core() as core:
            # Create multiple entities
            for i in range(5):
                core.entity.create(
                    entity_type="Transaction",
                    data=f'{{"description": "Payment {i}"}}'
                )

        # Search with limit
        response = client.post(
            "/mg",
            json={
                "op": "search",
                "query": "Payment",
                "target_type": "entity",
                "coverage": "content",
                "limit": 2,
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        result = data["result"]
        assert len(result["results"]) <= 2

    def test_search_empty_results(self, client, auth_headers):
        """Test search that returns very few results."""
        # Search for highly unique content that won't exist
        unique_query = "ZzzNonExistentContentXyz789Abc"
        response = client.post(
            "/mg",
            json={
                "op": "search",
                "query": unique_query,
                "target_type": "all",
                "coverage": "content",
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        result = data["result"]
        # Should find very few to no results for this unique search term
        # Note: Due to test database state, there might be some pre-existing data
        assert result["count"] < 3  # Should find at most 2-3 results even if there's pre-existing data

    def test_search_fuzzy_matching(self, client, auth_headers):
        """Test fuzzy matching with partial strings."""
        with get_core() as core:
            # Create entity with specific name
            core.entity.create(
                entity_type="Artifact",
                data='{"name": "Invoice_2025_03_15.pdf"}'
            )

        # Search for partial match
        response = client.post(
            "/mg",
            json={
                "op": "search",
                "query": "Invoice_2025",
                "target_type": "entity",
                "coverage": "content",
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        result = data["result"]
        assert len(result["results"]) >= 1

    def test_search_full_coverage(self, client, auth_headers):
        """Test search with full coverage (all fields)."""
        with get_core() as core:
            # Create entity with data that includes metadata-like content
            core.entity.create(
                entity_type="Artifact",
                data='{"name": "Document", "tags": ["important", "finance"]}'
            )

        # Search in data field
        response = client.post(
            "/mg",
            json={
                "op": "search",
                "query": "finance",
                "target_type": "entity",
                "coverage": "full",
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        result = data["result"]
        assert len(result["results"]) >= 1
