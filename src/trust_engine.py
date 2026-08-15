"""
The trust engine: turns "two sources disagree" into "here is who I believe
and why", and remembers how each source has performed over time so that
future decisions get better, not just this one.

Three independent signals are scored per reading, then combined:

1. Reliability (learned): a Beta-Bernoulli estimate of how often this
   source has been right historically, seeded with a sensible prior and
   updated whenever an audit confirms or contradicts it. This is the
   "state across queries" the brief asks for: it lives in `TrustState`
   and is loaded/saved to disk, so trust in a source persists and shifts
   between runs, not just within one conversation.

2. Recency: freshness matters, but different sources go stale at
   different rates. A WMS scan is only trustworthy until the asset is
   likely to have moved again; a manual check-in sheet entry is assumed
   to stay roughly true for longer because assets that get logged by
   hand tend to sit still. Each source type has its own decay half-life
   instead of one global "newest wins" rule, that's the naive version
   this deliberately avoids.

3. Data quality: signals intrinsic to the specific reading: GPS
   accuracy in metres, whether a manual entry was supervisor-verified,
   whether a WMS scan came from a fixed gate reader (harder to fake/
   mis-scan) versus a handheld device.

Why Beta-Bernoulli for reliability instead of a simple moving average of
"was it right": it naturally represents uncertainty. A source with 1
correct call out of 1 audit shouldn't be treated the same as one with 40
correct out of 40, the Beta posterior mean pulls the first estimate back
toward the prior until there's enough evidence, which stops the system
from over-reacting to a single lucky/unlucky audit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path


from .models import Reading, ScoredReading

# --- Source profiles -------------------------------------------------------
# Priors encode what we'd assume about each source type *before* we have
# any audit history for it. alpha/beta are Beta-distribution pseudo-counts:
# mean reliability = alpha / (alpha + beta). half_life_hours controls how
# fast a reading of that type goes stale (recency_score = 0.5 ** (age/half_life)).

@dataclass
class SourceProfile:
    source_id: str
    prior_alpha: float
    prior_beta: float
    half_life_hours: float
    description: str


DEFAULT_PROFILES: dict[str, SourceProfile] = {
    "wms": SourceProfile(
        source_id="wms",
        prior_alpha=8.5,
        prior_beta=1.5,   # prior mean ~0.85: precise when it fires
        half_life_hours=4.0,  # but goes stale fast once the asset can move again
        description="Warehouse management system scan event",
    ),
    "tech_gps": SourceProfile(
        source_id="tech_gps",
        prior_alpha=7.5,
        prior_beta=2.5,   # prior mean ~0.75: decent but coarse
        half_life_hours=1.5,  # pings are frequent, so staleness should be penalized hard
        description="Field technician device last-known-position log",
    ),
    "checkin_sheet": SourceProfile(
        source_id="checkin_sheet",
        prior_alpha=5.5,
        prior_beta=4.5,   # prior mean ~0.55: human error prone
        half_life_hours=24.0,  # but assumed to stay roughly true for longer
        description="Manual check-in sheet entry",
    ),
    "audit": SourceProfile(
        source_id="audit",
        prior_alpha=19.0,
        prior_beta=1.0,   # prior mean ~0.95: treated as near ground truth
        half_life_hours=72.0,
        description="Independent physical cycle count",
    ),
}


@dataclass
class SourceStats:
    alpha: float
    beta: float
    times_seen: int = 0
    times_audited_correct: int = 0
    times_audited_wrong: int = 0

    @property
    def reliability_mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)


class TrustState:
    """Loads/saves per-source Beta parameters and counters to a JSON file."""

    def __init__(self, path: Path, profiles: dict[str, SourceProfile] = None):
        self.path = path
        self.profiles = profiles or DEFAULT_PROFILES
        self.stats: dict[str, SourceStats] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            for source_id, profile in self.profiles.items():
                if source_id in raw:
                    self.stats[source_id] = SourceStats(**raw[source_id])
                else:
                    self.stats[source_id] = SourceStats(profile.prior_alpha, profile.prior_beta)
        else:
            for source_id, profile in self.profiles.items():
                self.stats[source_id] = SourceStats(profile.prior_alpha, profile.prior_beta)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = {sid: asdict(s) for sid, s in self.stats.items()}
        self.path.write_text(json.dumps(raw, indent=2))

    def get_or_create(self, source_id: str) -> SourceStats:
        if source_id not in self.stats:
            profile = self.profiles.get(source_id)
            if profile is None:
                # Unknown source seen for the first time: neutral, low-confidence prior.
                profile = SourceProfile(source_id, 1.0, 1.0, 6.0, "Unknown source")
                self.profiles[source_id] = profile
            self.stats[source_id] = SourceStats(profile.prior_alpha, profile.prior_beta)
        return self.stats[source_id]

    def record_outcome(self, source_id: str, correct: bool) -> None:
        """Update a source's Beta posterior after ground truth becomes known."""
        stats = self.get_or_create(source_id)
        stats.times_seen += 1
        if correct:
            stats.alpha += 1
            stats.times_audited_correct += 1
        else:
            stats.beta += 1
            stats.times_audited_wrong += 1


# --- Scoring -----------------------------------------------------------

# Weights for combining the three signals. Reliability is weighted highest
# because it's the only signal grounded in actual outcomes rather than
# assumptions about the reading itself; recency and quality are still
# meaningful but more heuristic.
WEIGHTS = {"reliability": 0.45, "recency": 0.35, "quality": 0.20}


def recency_score(reading: Reading, profile: SourceProfile, as_of: datetime) -> float:
    age_hours = max(0.0, (as_of - reading.timestamp).total_seconds() / 3600.0)
    return 0.5 ** (age_hours / profile.half_life_hours)


def quality_score(reading: Reading) -> tuple[float, list[str]]:
    """Signals intrinsic to this specific reading, independent of source history."""
    notes: list[str] = []
    score = 0.6  # neutral baseline

    if reading.source_type == "gps_ping":
        accuracy = reading.metadata.get("gps_accuracy_m")
        if accuracy is not None:
            # Sub-10m is great, 50m+ is poor. Map to [0.2, 0.95].
            score = max(0.2, min(0.95, 1.0 - (accuracy / 60.0)))
            notes.append(f"GPS accuracy {accuracy:.0f}m")

    elif reading.source_type == "automated_scan":
        scan_type = reading.metadata.get("scan_type", "handheld_scan")
        if scan_type == "fixed_gate_reader":
            score = 0.9
            notes.append("fixed gate reader (hard to mis-scan)")
        else:
            score = 0.75
            notes.append("handheld scan")

    elif reading.source_type == "manual_entry":
        if reading.metadata.get("verified_by_supervisor"):
            score = 0.8
            notes.append("supervisor-verified entry")
        else:
            score = 0.45
            notes.append("unverified manual entry")

    elif reading.source_type == "physical_audit":
        score = 0.95
        notes.append("independent physical count")

    return score, notes


def score_reading(
    reading: Reading,
    trust_state: TrustState,
    as_of: datetime,
) -> ScoredReading:
    profile = trust_state.profiles.get(reading.source_id) or DEFAULT_PROFILES.get(
        reading.source_id,
        SourceProfile(reading.source_id, 1.0, 1.0, 6.0, "Unknown source"),
    )
    stats = trust_state.get_or_create(reading.source_id)

    rel = stats.reliability_mean
    rec = recency_score(reading, profile, as_of)
    qual, qual_notes = quality_score(reading)

    combined = (
        WEIGHTS["reliability"] * rel
        + WEIGHTS["recency"] * rec
        + WEIGHTS["quality"] * qual
    )

    age_hours = (as_of - reading.timestamp).total_seconds() / 3600.0
    notes = [
        f"reliability={rel:.2f} (learned from {stats.times_seen} audited outcome(s), "
        f"prior {profile.prior_alpha:.1f}/{profile.prior_beta:.1f})",
        f"recency={rec:.2f} (age {age_hours:.1f}h vs {profile.half_life_hours:.1f}h half-life)",
        f"quality={qual:.2f} ({', '.join(qual_notes) if qual_notes else 'no extra signal'})",
    ]

    return ScoredReading(
        reading=reading,
        reliability_score=rel,
        recency_score=rec,
        quality_score=qual,
        combined_score=combined,
        notes=notes,
    )
