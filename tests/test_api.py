"""
Integration tests for src/api.py, using FastAPI's TestClient (backed by
httpx). Each test points the API at an isolated tmp_path database and
trust file via monkeypatch, so tests never touch data/decisions.db or
data/trust_state.json and can't interfere with each other or with a
demo/CLI run happening on the same machine.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import api


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "test_decisions.db")
    monkeypatch.setattr(api, "TRUST_PATH", tmp_path / "test_trust_state.json")
    return TestClient(api.app)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_query_asset_returns_a_decision(client):
    resp = client.post("/assets/FORKLIFT-TEST/query", json={"seed": 2})
    assert resp.status_code == 200
    body = resp.json()

    assert body["asset_id"] == "FORKLIFT-TEST"
    assert isinstance(body["decision_id"], int)
    assert isinstance(body["conflict"], bool)
    assert len(body["scores"]) >= 1
    # exactly one score should be flagged as the winner when there is one
    winners = [s for s in body["scores"] if s["is_winner"]]
    assert len(winners) == 1
    assert winners[0]["source_id"] == body["winner_source"]


def test_query_is_reproducible_with_same_seed(client):
    r1 = client.post("/assets/CRATE-TEST/query", json={"seed": 42}).json()
    r2 = client.post("/assets/CRATE-TEST/query", json={"seed": 42}).json()
    # same seed -> same underlying readings -> same winner and conflict shape
    # (decision_id and latency will differ, everything about the decision itself won't)
    assert r1["winner_source"] == r2["winner_source"]
    assert r1["winner_location"] == r2["winner_location"]
    assert r1["conflict"] == r2["conflict"]


def test_audit_unknown_decision_returns_404(client):
    resp = client.post("/decisions/999999/audit", json={"location": "Dock 3", "status": "stored"})
    assert resp.status_code == 404


def test_audit_updates_trust_and_is_reflected_in_get_trust(client):
    query_resp = client.post("/assets/PALLET-TEST/query", json={"seed": 2}).json()
    decision_id = query_resp["decision_id"]
    winner_source = query_resp["winner_source"]

    before = {row["source_id"]: row["reliability_mean"] for row in client.get("/trust").json()}

    # Audit with a location/status that does NOT match the winner, so the
    # winning source is graded wrong and its reliability should move down.
    wrong_location = "Truck 7" if query_resp["winner_location"] != "Truck 7" else "Dock 3"
    audit_resp = client.post(
        f"/decisions/{decision_id}/audit",
        json={"location": wrong_location, "status": "in_transit"},
    )
    assert audit_resp.status_code == 200
    assert len(audit_resp.json()["log"]) >= 1

    after = {row["source_id"]: row["reliability_mean"] for row in client.get("/trust").json()}
    assert after[winner_source] < before[winner_source]


def test_get_decisions_lists_logged_queries(client):
    client.post("/assets/DRILL-TEST/query", json={"seed": 1})
    client.post("/assets/DRILL-TEST/query", json={"seed": 2})

    resp = client.get("/decisions?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) >= 2
    assert all(row["asset_id"] == "DRILL-TEST" for row in body[:2])


def test_query_with_expected_status_context(client):
    resp = client.post(
        "/assets/TOOLBOX-TEST/query",
        json={"seed": 3, "expected_status": "in_transit"},
    )
    assert resp.status_code == 200
    # just confirms the optional context param doesn't break the request;
    # trust_engine's own tests cover the scoring effect in isolation
    assert resp.json()["asset_id"] == "TOOLBOX-TEST"