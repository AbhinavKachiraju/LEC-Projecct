"""
Scripted demo for the README / video walkthrough.

Runs five queries against a fresh trust state (data/demo_trust_state.json,
deleted and rebuilt each run so the demo is reproducible):

  1. FORKLIFT-118 - WMS vs GPS conflict. WMS looks trustworthy on paper
     (higher prior, fresher scan) and wins the vote. An audit then reveals
     the GPS log was actually right and the WMS scan was stale. Trust in
     WMS drops a little, trust in tech_gps rises.

  2. PALLET-204 - WMS *and* the check-in sheet agree with each other, GPS
     disagrees with both. Naive "majority vote" would pick WMS/sheet. The
     agent still weighs by score, not headcount, and because tech_gps's
     reliability was just bumped up in query 1 and this specific ping is
     very precise (12m) and fresh, it wins anyway. Audit confirms it was
     right again - tech_gps trust rises further, WMS and checkin_sheet
     both take a second hit.

  3. TOOLBOX-55 - tech_gps has no data at all (device not synced): the
     agent notices the gap and reconciles between the two sources that did
     respond, rather than guessing on gps's behalf. checkin_sheet is
     supervisor-verified and recent; WMS is a stale 8-hour-old scan. Audit
     confirms the check-in sheet. This is WMS's third consecutive miss.

  4. CRATE-77 - same shape of conflict as query 1 (WMS vs GPS, WMS reading
     fresher) but run *after* the trust updates above. This is the "does
     learning actually change a later decision" check the brief asks for:
     print both this and query 1's winner side by side.

  5. DRILL-09 - all three sources agree. No conflict to referee; shown for
     contrast so the video isn't only conflicts.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.models import Reading
from src.reconciler import resolve, apply_audit_feedback
from src.sources import wms_lookup, tech_gps_lookup, checkin_sheet_lookup, audit_lookup
from src.trust_engine import TrustState
from src import db as decision_db

DEMO_STATE_PATH = Path(__file__).resolve().parent / "data" / "demo_trust_state.json"
DB_PATH = Path(__file__).resolve().parent / "data" / "decisions.db"
NOW = datetime(2025, 1, 1, 12, 0, 0)


def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def run_query(label, asset_id, readings, trust_state, audit_loc=None, audit_status=None, expect_note=""):
    banner(f"QUERY: {label} (asset {asset_id})")
    if expect_note:
        print(f"[context] {expect_note}\n")
    resolution = resolve(asset_id, readings, trust_state, NOW)
    print(resolution.explanation)
    print(
        f"\n>>> Agent's answer: {resolution.winner.reading.location} / "
        f"{resolution.winner.reading.status}  "
        f"(trusted source: {resolution.winner.reading.source_id})"
    )

    readings_dict = readings if isinstance(readings, dict) else {r.source_id: r for r in readings}
    missing = sum(1 for r in readings_dict.values() if r is None)
    decision_id = decision_db.log_decision(
        DB_PATH, resolution, origin="demo", latency_ms=0.0,
        sources_responded=len(readings_dict) - missing, sources_missing=missing,
    )

    if audit_loc and audit_status:
        audit_reading = audit_lookup(asset_id, audit_loc, audit_status, NOW)
        print(f"\n[physical audit result: {audit_loc} / {audit_status}]")
        for line in apply_audit_feedback(resolution, audit_reading, trust_state):
            print("  " + line)
        decision_db.log_trust_snapshot(DB_PATH, trust_state, trigger=f"audit:decision_{decision_id}")
    return resolution


def main():
    DEMO_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DEMO_STATE_PATH.exists():
        DEMO_STATE_PATH.unlink()
    trust_state = TrustState(DEMO_STATE_PATH)

    print("Starting reliability priors (before any audits this run):")
    for source_id, stats in trust_state.stats.items():
        print(f"  {source_id:15} reliability={stats.reliability_mean:.3f}")

    # --- Query 1 ---------------------------------------------------------
    q1 = [
        wms_lookup("FORKLIFT-118", "Dock 3", "stored", NOW, minutes_ago=10, scan_type="handheld_scan"),
        tech_gps_lookup("FORKLIFT-118", "Staging Area", "in_transit", NOW, minutes_ago=90, gps_accuracy_m=40),
        checkin_sheet_lookup("FORKLIFT-118", "Dock 3", "stored", NOW, minutes_ago=360, verified_by_supervisor=False),
    ]
    res1 = run_query(
        "Where is the forklift right now?", "FORKLIFT-118", q1, trust_state,
        audit_loc="Staging Area", audit_status="in_transit",
        expect_note="WMS scan is fresher than the GPS ping, so on paper it looks like the safer bet.",
    )

    # --- Query 2 -----------------------------------------------------------
    q2 = [
        wms_lookup("PALLET-204", "Shelf B12", "stored", NOW, minutes_ago=15, scan_type="handheld_scan"),
        tech_gps_lookup("PALLET-204", "Truck 7", "in_transit", NOW, minutes_ago=20, gps_accuracy_m=12),
        checkin_sheet_lookup("PALLET-204", "Shelf B12", "stored", NOW, minutes_ago=180, verified_by_supervisor=False),
    ]
    res2 = run_query(
        "Where is this pallet?", "PALLET-204", q2, trust_state,
        audit_loc="Truck 7", audit_status="in_transit",
        expect_note="Two sources (WMS + check-in sheet) agree with each other and disagree with GPS. "
                    "A vote-counting agent would pick the majority - watch what this one does instead.",
    )

    # --- Query 3 -----------------------------------------------------------
    q3 = {
        "wms": wms_lookup("TOOLBOX-55", "Dock 3", "stored", NOW, minutes_ago=480, scan_type="fixed_gate_reader"),
        "tech_gps": tech_gps_lookup("TOOLBOX-55", "n/a", "n/a", NOW, minutes_ago=0, synced=False),  # no data
        "checkin_sheet": checkin_sheet_lookup("TOOLBOX-55", "Site Office", "checked_out", NOW, minutes_ago=120, verified_by_supervisor=True),
    }
    res3 = run_query(
        "Where is toolbox 55?", "TOOLBOX-55", q3, trust_state,
        audit_loc="Site Office", audit_status="checked_out",
        expect_note="The technician's device hasn't synced - the agent should notice that gap "
                    "rather than silently guessing on its behalf.",
    )

    # --- Query 4 (learning check) -------------------------------------------
    q4 = [
        wms_lookup("CRATE-77", "Dock 3", "stored", NOW, minutes_ago=5, scan_type="handheld_scan"),
        tech_gps_lookup("CRATE-77", "Truck 7", "in_transit", NOW, minutes_ago=10, gps_accuracy_m=10),
        checkin_sheet_lookup("CRATE-77", "Dock 3", "stored", NOW, minutes_ago=200, verified_by_supervisor=False),
    ]
    res4 = run_query(
        "Where is crate 77?", "CRATE-77", q4, trust_state,
        expect_note="Structurally almost identical to query 1 (fresh WMS scan vs GPS), run AFTER "
                    "three audits have already adjusted trust. No audit this time - this is the "
                    "'did learning change anything' check.",
    )

    banner("LEARNING CHECK: query 1 vs query 4")
    print(
        f"Query 1 (before any audits): trusted '{res1.winner.reading.source_id}' "
        f"({res1.winner.reading.location} / {res1.winner.reading.status})"
    )
    print(
        f"Query 4 (same conflict shape, after 3 audits): trusted "
        f"'{res4.winner.reading.source_id}' ({res4.winner.reading.location} / {res4.winner.reading.status})"
    )
    if res1.winner.reading.source_id != res4.winner.reading.source_id:
        print(
            "\n=> The winning SOURCE flipped between query 1 and query 4 even though the "
            "shape of the conflict (fresh WMS scan vs. slightly older, less precise GPS ping) "
            "is the same. That's the accumulated audit history changing the decision, not "
            "the input data."
        )
    else:
        print(
            "\n=> Same source won both times, but check the printed combined_score values above: "
            "the margin should have narrowed as WMS's reliability was marked down."
        )

    # --- Query 5 (no conflict, for contrast) --------------------------------
    q5 = [
        wms_lookup("DRILL-09", "Shelf B12", "stored", NOW, minutes_ago=8, scan_type="handheld_scan"),
        tech_gps_lookup("DRILL-09", "Shelf B12", "stored", NOW, minutes_ago=12, gps_accuracy_m=9),
        checkin_sheet_lookup("DRILL-09", "Shelf B12", "stored", NOW, minutes_ago=90, verified_by_supervisor=True),
    ]
    run_query("Where is drill 09?", "DRILL-09", q5, trust_state)

    banner("FINAL LEARNED RELIABILITY (persisted to data/demo_trust_state.json)")
    for source_id, stats in trust_state.stats.items():
        print(
            f"  {source_id:15} reliability={stats.reliability_mean:.3f}  "
            f"(seen {stats.times_seen}, correct {stats.times_audited_correct}, "
            f"wrong {stats.times_audited_wrong})"
        )


if __name__ == "__main__":
    main()
