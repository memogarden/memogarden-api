"""Greenfield boot smoke test (memogarden-system#7).

A fresh MEMOGARDEN_DATA_DIR used to leave soil.db without its schema:
init_system resolved the soil path via verb-default context paths while
the app's runtime (get_core/get_soil) resolved via the RFC-004 env chain,
so every Soil-touching endpoint 500'd with "no such table: item".

This module boots the real app (non-TESTING, so create_app runs the real
init_system) against an empty data dir and exercises the endpoint family
from the filed issue end to end:
register admin -> login -> GET /api/v1/triage -> 200.

PAIRING DEPENDENCY: these tests require the fixed memogarden-system (the
worktree venv pins it via the ../memogarden-system path dependency).
Against a pre-fix system, init_system would resolve soil to the verb
default — the operator's live ~/.local/share/memogarden — so never
cherry-pick this test onto an unfixed system checkout.

The system-level contract tests live in memogarden-system
(tests/test_greenfield_init.py); this is the api-level acceptance smoke.
"""

import sqlite3

import pytest

from api.main import create_app
from utils.config import default_settings


@pytest.fixture
def greenfield_env(tmp_path, monkeypatch):
    """Pin all RFC-004 path resolution to a per-test empty data dir.

    Without the env pin, init_system would resolve soil to the verb
    default (~/.local/share/memogarden) — the operator's live database.
    The conftest-level DATABASE_PATH=:memory: pin is cleared so the
    greenfield path resolves via the env chain like a real deploy.
    """
    monkeypatch.setenv("MEMOGARDEN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MEMOGARDEN_SOIL_DB", raising=False)
    monkeypatch.delenv("MEMOGARDEN_CORE_DB", raising=False)
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.setattr(default_settings, "database_path", None)
    return tmp_path


def test_greenfield_boot_triage_ready(greenfield_env):
    """Fresh data dir -> real boot -> admin bootstrap -> login -> triage 200."""
    app = create_app()  # non-TESTING: create_app runs the real init_system
    client = app.test_client()

    # 1. First-run admin bootstrap (localhost_only + first_time_only both
    #    pass: the test client is local and the databases are fresh).
    reg = client.post(
        "/admin/register",
        json={"username": "greenfield-admin", "password": "greenfield-pass-1"},
    )
    assert reg.status_code == 201, reg.get_json()

    # 2. Login mints a JWT.
    login = client.post(
        "/auth/login",
        json={"username": "greenfield-admin", "password": "greenfield-pass-1"},
    )
    assert login.status_code == 200, login.get_json()
    token = login.get_json()["access_token"]
    assert token

    # 3. The endpoint that 500'd with "no such table: item" before #7.
    triage = client.get(
        "/api/v1/triage",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert triage.status_code == 200, triage.get_json()
    body = triage.get_json()
    assert body["scope"]["data"]["label"] == "Triage / Untrusted"
    assert body["total_facts"] == 0
    assert body["total_entities"] == 0


def test_greenfield_boot_databases_landed_in_data_dir(greenfield_env):
    """Both DBs land under MEMOGARDEN_DATA_DIR with schemas applied there.

    Before the #7 fix, soil initialized at the verb-default location
    while the data dir got an empty file — the schema-presence check in
    the data dir is the real split-brain pin.
    """
    data_dir = greenfield_env

    create_app()
    for name, load_bearing_table in (("soil", "item"), ("core", "entity")):
        conn = sqlite3.connect(str(data_dir / f"{name}.db"))
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        conn.close()
        assert "_schema_metadata" in tables, f"{name}.db missing schema"
        assert load_bearing_table in tables, f"{name}.db missing {load_bearing_table}"
