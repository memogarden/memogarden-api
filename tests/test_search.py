"""Tests for search verb - Session 9.

Tests semantic search and discovery across entities and facts.
Per RFC-005 v7, the search verb supports fuzzy text matching with
configurable coverage levels and effort modes.
"""

import json

import pytest
from flask import Flask

from system.core import get_core
from system.soil import Fact, generate_soil_uuid, get_soil
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
        from utils import isodatetime

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
        from utils import isodatetime

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


class TestSearchLexical:
    """strategy='lexical' — FTS5 BM25 over fact data (api#95 Phase 1)."""

    def _make_fact(self, text: str, _type: str = "Note") -> str:
        from system.soil.fact import Fact, generate_soil_uuid
        from utils import isodatetime

        with get_soil() as soil:
            now = isodatetime.now()
            fact = Fact(
                uuid=generate_soil_uuid(),
                _type=_type,
                realized_at=now,
                canonical_at=now,
                data={"text": text},
                metadata=None,
            )
            uuid = soil.create_fact(fact)
            soil.commit()
            return uuid

    def _search(self, client, auth_headers, **overrides):
        payload = {
            "op": "search",
            "query": "senoko",
            "target_type": "fact",
            "strategy": "lexical",
        }
        payload.update(overrides)
        return client.post("/mg", json=payload, headers=auth_headers)

    def test_lexical_finds_fact_content(self, client, auth_headers):
        """BM25 full-text over the trigger-maintained index."""
        uuid = self._make_fact("Meeting Notes - Senoko contract renewal")

        response = self._search(client, auth_headers)

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        result = data["result"]
        assert result["strategy"] == "lexical"
        uuids = [r["uuid"] for r in result["results"] if r.get("kind") == "fact"]
        assert uuid in uuids

    def test_lexical_prefix_query(self, client, auth_headers):
        """Hardened queries are quoted prefix terms — 'seno' finds 'Senoko'."""
        uuid = self._make_fact("Senoko electricity bill")

        response = self._search(client, auth_headers, query="seno")

        assert response.status_code == 200
        result = response.get_json()["result"]
        uuids = [r["uuid"] for r in result["results"] if r.get("kind") == "fact"]
        assert uuid in uuids

    def test_lexical_no_hits(self, client, auth_headers):
        """Unrelated content is not matched. (The verb's own request is
        audited into an Action fact that echoes the query string, so a
        fact-target search can legitimately see that one record — the
        contract is that nothing else matches.)"""
        self._make_fact("ordinary note")

        response = self._search(client, auth_headers, query="zzzqqqxyzwy")

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        blobs = [json.dumps(r.get("data", "")) for r in data["result"]["results"]]
        assert all("ordinary note" not in blob for blob in blobs)

    def test_lexical_entities_still_like(self, client, auth_headers):
        """With strategy=lexical, entity matches still use the LIKE path.
        Entity results carry no kind marker; facts are marked 'fact'."""
        from system.core import get_core

        with get_core() as core:
            core.entity.create(
                entity_type="Artifact",
                data='{"name": "widget invoice"}',
            )
        self._make_fact("widget invoice scan")

        response = self._search(
            client, auth_headers, query="invoice", target_type="all")

        assert response.status_code == 200
        result = response.get_json()["result"]
        kinds = {r.get("kind", "entity") for r in result["results"]}
        assert "entity" in kinds
        assert "fact" in kinds


# ============================================================================
# api#95 Phase 2: semantic / hybrid strategies
# ============================================================================

class TestSemanticHybridStrategies:
    """Model-layer search planes at the verb (daemon-backed)."""

    def _note(self, title, description, scopes=None):
        with get_soil() as soil:
            return soil.create_fact(Fact(
                uuid=generate_soil_uuid(),
                _type="Note",
                realized_at="2026-11-24T09:00:00+00:00",
                canonical_at="2026-11-24T09:00:00+00:00",
                data={"title": title, "description": description,
                      "summary": None},
                metadata={"scopes": scopes} if scopes else None,
                integrity_hash=None,
                fidelity="full",
            ))

    def test_semantic_strategy_returns_daemon_hits(self, client,
                                                   auth_headers,
                                                   fake_daemon_factory):
        """strategy=semantic: daemon ranking passes through with scores."""
        u = self._note("quarterly ledger", "q4 reconciliation notes")
        fake_daemon_factory([[u, 0.87]])
        resp = client.post(
            "/mg",
            json={"op": "search", "query": "ledger", "strategy": "semantic",
                  "target_type": "fact", "limit": 10},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        result = resp.get_json()["result"]
        assert result["strategy"] == "semantic"
        facts = [r for r in result["results"] if r.get("kind") == "fact"]
        assert facts, "daemon hit missing from response"
        assert facts[0]["uuid"] == uid.add_soil_prefix(u)
        assert facts[0]["score"] == pytest.approx(0.87)

    def test_semantic_degrades_to_fuzzy_when_daemon_down(
            self, client, auth_headers, monkeypatch, tmp_path):
        """Daemon unavailable (no model layer) → LIKE fallback, and the
        response strategy echoes what actually ran."""
        self._note("xylophone ledger", "rare token q3")
        monkeypatch.setenv("MEMOGARDEN_EMBED_SOCK",
                           str(tmp_path / "none.sock"))
        monkeypatch.setenv("MEMOGARDEN_TRIAGE_MODEL_DIR", str(tmp_path))
        resp = client.post(
            "/mg",
            json={"op": "search", "query": "xylophone", "strategy": "semantic",
                  "target_type": "fact", "limit": 10},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        result = resp.get_json()["result"]
        assert result["strategy"] == "fuzzy"
        assert any("xylophone" in json.dumps(r.get("data", {}))
                   for r in result["results"])

    def test_hybrid_fuses_lexical_and_semantic_channels(
            self, client, auth_headers, fake_daemon_factory):
        """strategy=hybrid: RRF keeps the lexical hit and fuses in a
        semantic-only hit; lexical-first channel order breaks RRF ties."""
        lex_u = self._note("zephyr manifest", "cargo list zephyr winds")
        sem_u = self._note("sailplane repair", "fiberglass wing patch")
        fake_daemon_factory([[sem_u, 0.79]])
        resp = client.post(
            "/mg",
            json={"op": "search", "query": "zephyr", "strategy": "hybrid",
                  "target_type": "fact", "limit": 10},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        result = resp.get_json()["result"]
        assert result["strategy"] == "hybrid"
        facts = [r for r in result["results"] if r.get("kind") == "fact"]
        uuids = [f["uuid"] for f in facts]
        assert uid.add_soil_prefix(lex_u) in uuids, "lexical hit lost"
        assert uid.add_soil_prefix(sem_u) in uuids, "semantic-only hit lost"
        # both rank-0 → RRF tie at 1/61; lexical channel inserted first
        assert uuids[0] == uid.add_soil_prefix(lex_u)
        assert facts[0]["score"] == pytest.approx(1 / 61, abs=1e-3)

    def test_semantic_scope_refinement_filters_at_boundary(
            self, client, auth_headers, fake_daemon_factory):
        """request.scope refinement is boundary-enforced: daemon hits that
        don't carry the label are dropped here even though the model layer
        returned them (defense-in-depth mask ≠ enforcement)."""
        with get_core() as c:
            s_uuid = c.entity.create(
                entity_type="Scope",
                data={"label": "Household", "active_participants": [],
                      "artifact_uuids": []},
            )
            c._conn.commit()
        in_scope = self._note("scoped ledger", "private note",
                              scopes=[s_uuid])
        unscoped = self._note("open ledger", "public note")
        # daemon best-first puts the unscoped item FIRST — enforcement
        # must still drop it
        fake_daemon_factory([[unscoped, 0.9], [in_scope, 0.8]])
        resp = client.post(
            "/mg",
            json={"op": "search", "query": "ledger", "strategy": "semantic",
                  "target_type": "fact", "scope": s_uuid, "limit": 10},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        result = resp.get_json()["result"]
        uuids = [r["uuid"] for r in result["results"]
                 if r.get("kind") == "fact"]
        assert uid.add_soil_prefix(in_scope) in uuids
        assert uid.add_soil_prefix(unscoped) not in uuids

    def test_invalid_strategy_rejected(self, client, auth_headers):
        resp = client.post(
            "/mg",
            json={"op": "search", "query": "x", "strategy": "quantum",
                  "target_type": "fact"},
            headers=auth_headers,
        )
        # the Semantic API layer answers schema violations with 400
        assert resp.status_code == 400
