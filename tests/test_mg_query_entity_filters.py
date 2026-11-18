"""/mg query — entity data-field filters (#27).

Covers the accepted contract: the documented {"_type": T} filter resolves
as the type filter (previously silently ignored — api.md's own example
returned everything); every other filter key is a data-field equality
match with find_by_data's path rule; all filters are WHERE-level, so
filtering happens BEFORE pagination.
"""

import uuid

import pytest


def _mg(client, auth_headers, body):
    response = client.post("/mg", json=body, headers=auth_headers)
    return response


def _create(client, auth_headers, etype, data):
    response = _mg(
        client,
        auth_headers,
        {"op": "create", "type": etype, "data": data},
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["result"]


def _query(client, auth_headers, **params):
    response = _mg(client, auth_headers, {"op": "query", **params})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["result"]


class TestEntityTypeFilter:
    def test_documented_type_filter_resolves(self, client, auth_headers):
        """api.md's own example — {"filters": {"_type": "Transaction"}} —
        previously ignored the filter and returned everything."""
        _create(client, auth_headers, "Transaction", {"amount": 1})
        _create(client, auth_headers, "Transaction", {"amount": 2})
        _create(client, auth_headers, "Recurrence", {"label": "rent"})

        result = _query(client, auth_headers, filters={"_type": "Transaction"})
        assert result["total"] == 2
        assert all(r["type"] == "Transaction" for r in result["results"])

    def test_type_and_filters_type_agree(self, client, auth_headers):
        _create(client, auth_headers, "Transaction", {"amount": 1})
        result = _query(
            client, auth_headers, type="Transaction", filters={"_type": "Transaction"}
        )
        assert result["total"] == 1

    def test_conflicting_type_filters_rejected(self, client, auth_headers):
        _create(client, auth_headers, "Transaction", {"amount": 1})
        response = _mg(
            client,
            auth_headers,
            {"op": "query", "type": "Transaction", "filters": {"_type": "Scope"}},
        )
        assert response.status_code == 400


class TestDataFieldFilters:
    def test_equality_filters(self, client, auth_headers):
        _create(
            client, auth_headers, "Transaction",
            {"source": "tada", "source_id": "t1", "title": "one"},
        )
        _create(
            client, auth_headers, "Transaction",
            {"source": "tada", "source_id": "t2", "title": "two"},
        )
        _create(
            client, auth_headers, "Transaction",
            {"source": "ticktick", "source_id": "k1", "title": "imported"},
        )

        result = _query(
            client, auth_headers,
            filters={"_type": "Transaction", "source": "tada"},
        )
        assert result["total"] == 2
        assert {r["data"]["source_id"] for r in result["results"]} == {"t1", "t2"}

    def test_nested_dotted_path(self, client, auth_headers):
        _create(
            client, auth_headers, "Recurrence",
            {"label": "rent", "meta": {"zone": "Asia/Singapore"}},
        )
        _create(
            client, auth_headers, "Recurrence",
            {"label": "gym", "meta": {"zone": "UTC"}},
        )

        result = _query(
            client, auth_headers,
            filters={"_type": "Recurrence", "meta.zone": "Asia/Singapore"},
        )
        assert result["total"] == 1
        assert result["results"][0]["data"]["label"] == "rent"

    def test_superseded_entities_excluded(self, client, auth_headers):
        created = _create(
            client, auth_headers, "Transaction", {"source": "tada", "source_id": "t1"}
        )
        _create(
            client, auth_headers, "Transaction", {"source": "tada", "source_id": "t2"}
        )
        response = _mg(
            client, auth_headers, {"op": "forget", "target": created["uuid"]}
        )
        assert response.status_code == 200

        result = _query(client, auth_headers, filters={"_type": "Transaction"})
        assert result["total"] == 1
        assert result["results"][0]["data"]["source_id"] == "t2"

    def test_filter_before_pagination(self, client, auth_headers):
        """30 tada Tasks, 3 others; a filtered query with count=5 must
        return the 3 — filtering at WHERE level, not page-then-filter."""
        for i in range(30):
            _create(
                client, auth_headers, "Transaction",
                {"source": "tada", "source_id": f"t{i}"},
            )
        for i in range(3):
            _create(
                client, auth_headers, "Transaction",
                {"source": "ticktick", "source_id": f"k{i}"},
            )

        result = _query(
            client, auth_headers,
            filters={"_type": "Transaction", "source": "ticktick"},
            count=5,
        )
        assert result["total"] == 3
        assert result["count"] == 3
        assert {r["data"]["source_id"] for r in result["results"]} == {"k0", "k1", "k2"}


class TestFilterValidation:
    def test_invalid_field_path_rejected(self, client, auth_headers):
        response = _mg(
            client, auth_headers,
            {"op": "query", "filters": {"bad field!": "x"}},
        )
        assert response.status_code == 400

    def test_nonscalar_filter_value_rejected(self, client, auth_headers):
        response = _mg(
            client, auth_headers,
            {"op": "query", "filters": {"tags": ["buy", "errand"]}},
        )
        assert response.status_code == 400
