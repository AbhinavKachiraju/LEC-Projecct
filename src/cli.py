"""
Interactive CLI. Generates a fresh, randomized set of source readings for
an asset on every call (so you can hammer the agent with new conflicts),
resolves them, and optionally lets you supply an audit result so the
trust engine learns from it. State persists in data/trust_state.json
between runs, so reliability scores you build up today are still there
tomorrow. Every query is also logged to data/decisions.db (origin='cli'),
the same log the API writes to and the dashboard reads from.

Usage:
    python -m src.cli query ASSET-001
    python -m src.cli query ASSET-001 --audit "Dock 3" --audit-status stored
    python -m src.cli show-trust
"""

from __future__ import annotations

import argparse
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .reconciler import resolve, apply_audit_feedback, ExpectedContext
from .scenario import generate_readings
from .sources import audit_lookup
from .trust_engine import TrustState

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_PATH = DATA_DIR / "trust_state.json"
DB_PATH = DATA_DIR / "decisions.db"


def print_resolution(resolution) -> None:
    print(resolution.explanation)
    if resolution.winner:
        print(
            f"\n=> Best answer for {resolution.asset_id}: "
            f"{resolution.winner.reading.location} / {resolution.winner.reading.status} "
            f"(trusted source: {resolution.winner.reading.source_id})"
        )


def cmd_query(args) -> None:
    trust_state = TrustState(DATA_PATH)
    as_of = datetime.now(timezone.utc).replace(tzinfo=None)
    rng = random.Random(args.seed)
    readings = generate_readings(args.asset_id, as_of, rng)

    context = None
    if args.expected_status:
        context = ExpectedContext(expected_status=args.expected_status, note="from CLI")

    start = time.perf_counter()
    resolution = resolve(args.asset_id, readings, trust_state, as_of, context=context)
    latency_ms = (time.perf_counter() - start) * 1000
    print_resolution(resolution)

    sources_missing = sum(1 for r in readings.values() if r is None)
    decision_id = db.log_decision(
        DB_PATH, resolution, origin="cli", latency_ms=latency_ms,
        sources_responded=len(readings) - sources_missing, sources_missing=sources_missing,
    )
    print(f"\n[logged as decision #{decision_id}]")

    if args.audit and args.audit_status:
        audit_reading = audit_lookup(args.asset_id, args.audit, args.audit_status, as_of)
        print("\n--- Audit feedback ---")
        for line in apply_audit_feedback(resolution, audit_reading, trust_state):
            print(line)
        db.log_trust_snapshot(DB_PATH, trust_state, trigger=f"audit:decision_{decision_id}")
    else:
        trust_state.save()


def cmd_show_trust(_args) -> None:
    trust_state = TrustState(DATA_PATH)
    print(f"{'source':15} {'reliability':>11} {'seen':>6} {'correct':>8} {'wrong':>6}")
    for source_id, stats in trust_state.stats.items():
        print(
            f"{source_id:15} {stats.reliability_mean:11.3f} {stats.times_seen:6d} "
            f"{stats.times_audited_correct:8d} {stats.times_audited_wrong:6d}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Asset location reconciler")
    sub = parser.add_subparsers(required=True)

    q = sub.add_parser("query", help="Query an asset's location across sources")
    q.add_argument("asset_id")
    q.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    q.add_argument("--expected-status", default=None, help="Business-context hint, e.g. 'in_transit'")
    q.add_argument("--audit", default=None, help="Ground-truth location from a physical audit")
    q.add_argument("--audit-status", default=None, help="Ground-truth status from a physical audit")
    q.set_defaults(func=cmd_query)

    t = sub.add_parser("show-trust", help="Show current learned reliability per source")
    t.set_defaults(func=cmd_show_trust)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
