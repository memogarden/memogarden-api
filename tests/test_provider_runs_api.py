"""
Tests for provider run-history reads (RFC-015 §3.2).

- GET /api/v1/providers/<name>/runs  (list, filters, limit clamp)
- GET /api/v1/runs/<run_id>          (single run)

Run history was previously internal — these are the reads behind the
console's verification workflow. Read-only, non-governed: any
authenticated principal may read (matching provider GET).
"""

import json


def _seed_provider(core, name: str) -> None:
    core._conn.execute(
        """INSERT INTO providers
           (name, module, class, interval, enabled, config, capabilities,
            registered_at)
           VALUES (?, 'test_module', 'TestProvider', 3600, 1, '{}', '[]', ?)""",
        (name, "2026-11-19T00:00:00+00:00"),
    )
    core._conn.commit()


def _seed_run(core, run_id: str, name: str, status: str, started: str,
              account_id: str = "", finished: str | None = None,
              triggered_by: str = "heartbeat", detail: dict | None = None) -> None:
    core._conn.execute(
        """INSERT INTO provider_runs
           (id, provider_name, account_id, status, started_at, finished_at,
            triggered_by, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, name, account_id, status, started, finished, triggered_by,
         json.dumps(detail) if detail else None),
    )
    core._conn.commit()


def _seed_corpus(core) -> dict:
    """Three runs on 'alpha' (one per status) + one on 'beta'."""
    _seed_provider(core, "alpha")
    _seed_provider(core, "beta")
    ids = {
        "ok": "11111111-1111-4111-8111-111111111111",
        "bad": "22222222-2222-4222-8222-222222222222",
        "live": "33333333-3333-4333-8333-333333333333",
        "beta": "44444444-4444-4444-8444-444444444444",
    }
    _seed_run(core, ids["ok"], "alpha", "success",
              "2026-11-19T02:00:00+00:00", finished="2026-11-19T02:00:41+00:00",
              detail={"created": 37, "errors": 0})
    _seed_run(core, ids["bad"], "alpha", "failed",
              "2026-11-19T01:00:00+00:00", finished="2026-11-19T01:00:12+00:00",
              triggered_by="manual", detail={"error": "re-auth expired"})
    _seed_run(core, ids["live"], "alpha", "running",
              "2026-11-19T03:00:00+00:00", account_id="acct-1")
    _seed_run(core, ids["beta"], "beta", "success",
              "2026-11-19T02:30:00+00:00", finished="2026-11-19T02:30:05+00:00")
    return ids


# ============================================================================
# GET /api/v1/providers/<name>/runs
# ============================================================================


def test_list_runs_ordered_newest_first(client, auth_headers, core):
    ids = _seed_corpus(core)
    response = client.get("/api/v1/providers/alpha/runs", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert data["count"] == 3
    started = [run["started_at"] for run in data["runs"]]
    assert started == sorted(started, reverse=True)
    assert data["runs"][0]["id"] == ids["live"]


def test_list_runs_status_filter(client, auth_headers, core):
    _seed_corpus(core)
    response = client.get("/api/v1/providers/alpha/runs?status=failed", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert data["count"] == 1
    assert data["runs"][0]["status"] == "failed"
    assert data["runs"][0]["detail"] == {"error": "re-auth expired"}


def test_list_runs_bad_status_400(client, auth_headers, core):
    _seed_provider(core, "alpha")
    response = client.get("/api/v1/providers/alpha/runs?status=bogus", headers=auth_headers)
    assert response.status_code == 400


def test_list_runs_bad_limit_400(client, auth_headers, core):
    _seed_provider(core, "alpha")
    response = client.get("/api/v1/providers/alpha/runs?limit=many", headers=auth_headers)
    assert response.status_code == 400


def test_list_runs_unknown_provider_404(client, auth_headers):
    response = client.get("/api/v1/providers/ghost/runs", headers=auth_headers)
    assert response.status_code == 404


def test_list_runs_api_key_allowed(client, auth_headers_apikey, core):
    """Read-only operational visibility: any authenticated principal."""
    _seed_provider(core, "alpha")
    response = client.get(
        "/api/v1/providers/alpha/runs",
        headers={"X-API-Key": auth_headers_apikey["X-API-Key"]},
    )
    assert response.status_code == 200


def test_list_runs_unauthenticated_401(client, core):
    _seed_provider(core, "alpha")
    response = client.get("/api/v1/providers/alpha/runs")
    assert response.status_code == 401


def test_list_runs_limit_cap(client, auth_headers, core):
    """The 200 hard cap bounds oversized requests (>200 seeded rows)."""
    _seed_provider(core, "alpha")
    for i in range(205):
        _seed_run(core, f"55555555-5555-4555-8555-{i:012d}", "alpha", "success",
                  f"2026-01-01T00:00:{i % 60:02d}+00:00")
    response = client.get("/api/v1/providers/alpha/runs?limit=500", headers=auth_headers)
    assert response.status_code == 200
    assert response.get_json()["count"] == 200  # capped, not 205


# ============================================================================
# GET /api/v1/runs/<run_id>
# ============================================================================


def test_get_run_by_id(client, auth_headers, core):
    ids = _seed_corpus(core)
    response = client.get(f"/api/v1/runs/{ids['ok']}", headers=auth_headers)
    assert response.status_code == 200
    run = response.get_json()
    assert run["id"] == ids["ok"]
    assert run["provider_name"] == "alpha"
    assert run["status"] == "success"
    assert run["detail"]["created"] == 37
    assert run["finished_at"] is not None


def test_get_run_unknown_404(client, auth_headers):
    response = client.get("/api/v1/runs/00000000-0000-4000-8000-000000000000",
                          headers=auth_headers)
    assert response.status_code == 404
