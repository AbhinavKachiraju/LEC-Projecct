import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db
from src.models import Reading
from src.reconciler import resolve, apply_audit_feedback
from src.trust_engine import TrustState

NOW = datetime(2025, 1, 1, 12, 0, 0)


def make_reading(source_id, source_type, loc, status, minutes_ago, **meta):
    from datetime import timedelta
    return Reading(
        source_id=source_id, source_type=source_type, asset_id="A1",
        location=loc, status=status, timestamp=NOW - timedelta(minutes=minutes_ago), metadata=meta,
    )


def test_log_decision_roundtrip(tmp_path):
    db_path = tmp_path / "decisions.db"
    state = TrustState(tmp_path / "trust.json")

    a = make_reading("wms", "automated_scan", "Dock 3", "stored", 5)
    b = make_reading("tech_gps", "gps_ping", "Truck 7", "in_transit", 10, gps_accuracy_m=10)
    resolution = resolve("A1", [a, b], state, NOW)

    decision_id = db.log_decision(
        db_path, resolution, origin="test", latency_ms=1.2,
        sources_responded=2, sources_missing=0,
    )
    assert decision_id > 0

    decision = db.fetch_decision(db_path, decision_id)
    assert decision["asset_id"] == "A1"
    assert decision["conflict"] == 1
    assert decision["winner_source"] == resolution.winner.reading.source_id

    scores = db.fetch_source_scores(db_path, decision_id)
    assert len(scores) == 2
    assert {s["source_id"] for s in scores} == {"wms", "tech_gps"}


def test_missing_decision_returns_none(tmp_path):
    db_path = tmp_path / "decisions.db"
    db.init_db(db_path)
    assert db.fetch_decision(db_path, 12345) is None


def test_trust_snapshot_logged_after_audit(tmp_path):
    db_path = tmp_path / "decisions.db"
    state = TrustState(tmp_path / "trust.json")

    a = make_reading("wms", "automated_scan", "Dock 3", "stored", 5)
    b = make_reading("tech_gps", "gps_ping", "Truck 7", "in_transit", 10, gps_accuracy_m=10)
    resolution = resolve("A1", [a, b], state, NOW)

    audit_reading = make_reading("audit", "physical_audit", "Truck 7", "in_transit", 0)
    apply_audit_feedback(resolution, audit_reading, state)
    db.log_trust_snapshot(db_path, state, trigger="test_audit")

    history = db.fetch_trust_history(db_path)
    assert len(history) == len(state.stats)
    assert all(h["trigger"] == "test_audit" for h in history)


def test_recent_decisions_ordering_and_origin_filter(tmp_path):
    db_path = tmp_path / "decisions.db"
    state = TrustState(tmp_path / "trust.json")

    for i, origin in enumerate(["cli", "api", "cli"]):
        a = make_reading("wms", "automated_scan", "Dock 3", "stored", 5)
        resolution = resolve(f"A{i}", [a], state, NOW)
        db.log_decision(db_path, resolution, origin=origin, latency_ms=0.5, sources_responded=1, sources_missing=0)

    all_decisions = db.fetch_recent_decisions(db_path, limit=10)
    assert len(all_decisions) == 3
    assert all_decisions[0]["asset_id"] == "A2"  # most recent first

    cli_only = db.fetch_recent_decisions(db_path, limit=10, origin="cli")
    assert len(cli_only) == 2
    assert all(d["origin"] == "cli" for d in cli_only)
