"""
Reconciler: given readings from multiple sources for one asset, decide
whether they conflict and, if so, which one to believe.

This module deliberately does *not* retry a failed source or fall back to
"first answer" / "average the answers" (averaging locations makes no
sense anyway — you can't be 60% at Dock 3 and 40% at Shelf B12). Instead:

1. Detect conflict by comparing normalized (location, status) pairs
   across all readings that actually returned data.
2. If they agree, trust the agreement (no need to referee).
3. If they disagree, score every reading with the trust engine and pick
   the highest combined score. Ties are broken by reliability, since
   that's the signal grounded in actual track record.
4. Optionally fold in a business-context signal: if the caller supplies
   an "expected" status/location window (e.g. "this asset should be
   'in_transit' until the delivery window closes"), a reading consistent
   with that context gets a small quality boost — mirroring how a human
   dispatcher would sanity-check a reading against what's supposed to be
   happening, without letting context override strong evidence on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .models import Reading, ScoredReading, Resolution
from .trust_engine import TrustState, score_reading


@dataclass
class ExpectedContext:
    """Optional business-context hint, e.g. from a delivery schedule."""
    expected_status: Optional[str] = None
    note: str = ""


CONTEXT_BOOST = 0.08  # small, deliberate: context nudges, evidence still decides


def detect_conflict(readings: list[Reading]) -> bool:
    present = [r for r in readings if r is not None]
    if len(present) < 2:
        return False
    locations = {r.normalized_location() for r in present}
    statuses = {r.normalized_status() for r in present}
    return len(locations) > 1 or len(statuses) > 1


def resolve(
    asset_id: str,
    readings: dict[str, Optional[Reading]] | list[Reading],
    trust_state: TrustState,
    as_of: datetime,
    context: Optional[ExpectedContext] = None,
) -> Resolution:
    """
    `readings` is normally a dict of source_id -> Reading-or-None (None
    means that source had no data for this asset, e.g. a device that
    never synced). A plain list of Readings is also accepted for
    convenience when nothing is missing.
    """
    if isinstance(readings, list):
        readings = {
            (r.source_id if r is not None else f"unknown_{i}"): r
            for i, r in enumerate(readings)
        }

    present = [r for r in readings.values() if r is not None]
    missing_ids = [sid for sid, r in readings.items() if r is None]

    if not present:
        return Resolution(
            asset_id=asset_id,
            conflict=False,
            winner=None,
            all_scores=[],
            explanation=(
                f"No source returned data for asset {asset_id} "
                f"(checked: {', '.join(readings.keys())}). Nothing to reconcile."
            ),
        )

    scored = [score_reading(r, trust_state, as_of) for r in present]

    if context and context.expected_status:
        for s in scored:
            if s.reading.normalized_status() == context.expected_status.strip().lower():
                s.combined_score = min(1.0, s.combined_score + CONTEXT_BOOST)
                s.notes.append(
                    f"+{CONTEXT_BOOST:.2f} context boost: matches expected status "
                    f"'{context.expected_status}' ({context.note})".strip()
                )

    conflict = detect_conflict(present)

    if not conflict:
        winner = max(scored, key=lambda s: s.combined_score)
        lines = [
            f"All {len(present)} responding source(s) agree: "
            f"{winner.reading.location} / {winner.reading.status}.",
        ]
        if missing_ids:
            lines.append(f"({len(missing_ids)} source(s) returned no data: {', '.join(missing_ids)}.)")
        return Resolution(
            asset_id=asset_id,
            conflict=False,
            winner=winner,
            all_scores=scored,
            explanation=" ".join(lines),
        )

    scored.sort(key=lambda s: (s.combined_score, s.reliability_score), reverse=True)
    winner = scored[0]
    runner_up = scored[1]

    lines = [f"CONFLICT: sources disagree on asset {asset_id}."]
    for s in scored:
        lines.append(
            f"  - {s.reading.source_id}: says '{s.reading.location} / {s.reading.status}', "
            f"combined score {s.combined_score:.3f} [{'; '.join(s.notes)}]"
        )
    margin = winner.combined_score - runner_up.combined_score
    lines.append(
        f"Decision: trust '{winner.reading.source_id}' "
        f"({winner.reading.location} / {winner.reading.status}), "
        f"score {winner.combined_score:.3f} vs next-best "
        f"'{runner_up.reading.source_id}' at {runner_up.combined_score:.3f} "
        f"(margin {margin:.3f})."
    )
    if margin < 0.03:
        lines.append(
            "Margin is thin — treat this as a low-confidence call and consider "
            "triggering a physical audit rather than acting on it blindly."
        )
    if missing_ids:
        lines.append(f"({len(missing_ids)} source(s) returned no data: {', '.join(missing_ids)}.)")

    return Resolution(
        asset_id=asset_id,
        conflict=True,
        winner=winner,
        all_scores=scored,
        explanation="\n".join(lines),
    )


def apply_audit_feedback(
    resolution: Resolution,
    audit_reading: Reading,
    trust_state: TrustState,
) -> list[str]:
    """
    Once an independent audit reveals the true location/status, grade
    every source that participated in the conflict against it and update
    the trust engine. This is what makes confidence adjust *across*
    queries: the next time these sources disagree, `score_reading` will
    pull from the updated Beta posteriors, not the original priors.
    """
    candidates = [(s.reading.source_id, s.reading.location, s.reading.status) for s in resolution.all_scores]
    return grade_sources(candidates, audit_reading, trust_state)


def grade_sources(
    candidates: list[tuple[str, str, str]],
    audit_reading: Reading,
    trust_state: TrustState,
) -> list[str]:
    """
    Lower-level version of `apply_audit_feedback` that doesn't require a
    live `Resolution` object — just (source_id, location, status) tuples.
    This is what lets a stateless API grade sources against an audit
    using only what was persisted to the decision log (`src/db.py`),
    without having to keep the original in-memory Resolution around
    between the query request and the later audit request.
    """
    log: list[str] = []
    truth_loc = audit_reading.normalized_location()
    truth_status = audit_reading.normalized_status()

    for source_id, location, status in candidates:
        correct = location.strip().lower() == truth_loc and status.strip().lower() == truth_status
        trust_state.record_outcome(source_id, correct)
        log.append(
            f"Audit says '{audit_reading.location} / {audit_reading.status}'. "
            f"Source '{source_id}' was {'CORRECT' if correct else 'WRONG'} "
            f"-> reliability now {trust_state.get_or_create(source_id).reliability_mean:.3f}"
        )
    trust_state.save()
    return log
