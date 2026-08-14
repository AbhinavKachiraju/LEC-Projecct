import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import Reading
from src.reconciler import detect_conflict, resolve, apply_audit_feedback, ExpectedContext
from src.trust_engine import TrustState

NOW = datetime(2025, 1, 1, 12, 0, 0)


def fresh_state(tmp_path) -> TrustState:
    return TrustState(tmp_path / "state.json")


def make_reading(source_id, source_type, loc, status, minutes_ago, **meta):
    from datetime import timedelta
    return Reading(
        source_id=source_id,
        source_type=source_type,
        asset_id="A1",
        location=loc,
        status=status,
        timestamp=NOW - timedelta(minutes=minutes_ago),
        metadata=meta,
    )


def test_no_conflict_when_sources_agree():
    a = make_reading("wms", "automated_scan", "Dock 3", "stored", 10)
    b = make_reading("tech_gps", "gps_ping", "Dock 3", "stored", 12, gps_accuracy_m=8)
    assert detect_conflict([a, b]) is False


def test_conflict_detected_on_location_mismatch():
    a = make_reading("wms", "automated_scan", "Dock 3", "stored", 10)
    b = make_reading("tech_gps", "gps_ping", "Truck 7", "in_transit", 12, gps_accuracy_m=8)
    assert detect_conflict([a, b]) is True


def test_resolver_picks_fresher_higher_reliability_source(tmp_path):
    state = fresh_state(tmp_path)
    fresh_wms = make_reading("wms", "automated_scan", "Dock 3", "stored", 5)
    stale_gps = make_reading("tech_gps", "gps_ping", "Truck 7", "in_transit", 300, gps_accuracy_m=45)
    resolution = resolve("A1", [fresh_wms, stale_gps], state, NOW)
    assert resolution.conflict is True
    assert resolution.winner.reading.source_id == "wms"


def test_missing_source_is_reported_not_guessed(tmp_path):
    state = fresh_state(tmp_path)
    readings = {
        "wms": make_reading("wms", "automated_scan", "Dock 3", "stored", 5),
        "tech_gps": None,
        "checkin_sheet": make_reading("checkin_sheet", "manual_entry", "Shelf B12", "stored", 60),
    }
    resolution = resolve("A1", readings, state, NOW)
    assert "tech_gps" in resolution.explanation
    assert resolution.conflict is True  # wms vs checkin_sheet still disagree


def test_audit_feedback_updates_reliability(tmp_path):
    state = fresh_state(tmp_path)
    before = state.get_or_create("wms").reliability_mean

    wms_wrong = make_reading("wms", "automated_scan", "Dock 3", "stored", 5)
    gps_right = make_reading("tech_gps", "gps_ping", "Truck 7", "in_transit", 10, gps_accuracy_m=10)
    resolution = resolve("A1", [wms_wrong, gps_right], state, NOW)

    audit = make_reading("audit", "physical_audit", "Truck 7", "in_transit", 0)
    apply_audit_feedback(resolution, audit, state)

    after = state.get_or_create("wms").reliability_mean
    assert after < before  # wms was wrong, its reliability should drop


def test_learning_changes_a_later_decision(tmp_path):
    """
    The core requirement: after enough audits mark WMS as unreliable and
    GPS as reliable, a later, structurally-identical conflict should be
    won by GPS even though WMS's reading is fresher.
    """
    state = fresh_state(tmp_path)

    for _ in range(5):
        wms_reading = make_reading("wms", "automated_scan", "Dock 3", "stored", 5)
        gps_reading = make_reading("tech_gps", "gps_ping", "Truck 7", "in_transit", 10, gps_accuracy_m=10)
        resolution = resolve("A1", [wms_reading, gps_reading], state, NOW)
        audit = make_reading("audit", "physical_audit", "Truck 7", "in_transit", 0)
        apply_audit_feedback(resolution, audit, state)

    # Now run the same conflict shape one more time, no audit this round.
    wms_reading = make_reading("wms", "automated_scan", "Dock 3", "stored", 5)
    gps_reading = make_reading("tech_gps", "gps_ping", "Truck 7", "in_transit", 10, gps_accuracy_m=10)
    final = resolve("A1", [wms_reading, gps_reading], state, NOW)

    assert final.winner.reading.source_id == "tech_gps"


def test_context_boost_favors_expected_status(tmp_path):
    state = fresh_state(tmp_path)
    a = make_reading("wms", "automated_scan", "Dock 3", "stored", 20)
    b = make_reading("tech_gps", "gps_ping", "Truck 7", "in_transit", 22, gps_accuracy_m=20)
    ctx = ExpectedContext(expected_status="in_transit", note="delivery window open")
    resolution = resolve("A1", [a, b], state, NOW, context=ctx)
    gps_score = next(s for s in resolution.all_scores if s.reading.source_id == "tech_gps")
    assert any("context boost" in n for n in gps_score.notes)
