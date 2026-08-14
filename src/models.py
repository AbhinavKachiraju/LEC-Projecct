"""
Core data structures shared across the reconciler.

A Reading is one source's answer to "where/what is asset X right now".
Sources are deliberately heterogeneous in shape because real operational
systems are: a WMS gives you a scan event, a technician's phone gives you
a GPS ping, a check-in sheet gives you a hand-written line. Forcing them
into one schema (with an open `metadata` bag for the source-specific bits)
is what lets the trust engine reason about *why* a reading looks solid or
shaky, instead of just comparing two strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Reading:
    source_id: str          # e.g. "wms", "tech_gps", "checkin_sheet", "audit"
    source_type: str        # category used to pick decay/quality rules
    asset_id: str
    location: str
    status: str              # e.g. "stored", "in_transit", "checked_out", "in_use"
    timestamp: datetime      # when the source believes this was true
    metadata: dict[str, Any] = field(default_factory=dict)

    def normalized_location(self) -> str:
        return self.location.strip().lower()

    def normalized_status(self) -> str:
        return self.status.strip().lower()


@dataclass
class ScoredReading:
    """A Reading plus the trust engine's breakdown of why it scored the way it did."""
    reading: Reading
    reliability_score: float   # learned, historical accuracy of this source [0,1]
    recency_score: float       # how fresh this specific reading is [0,1]
    quality_score: float       # data-quality signals on this specific reading [0,1]
    combined_score: float      # weighted sum used to rank readings
    notes: list[str] = field(default_factory=list)  # human-readable factors


@dataclass
class Resolution:
    asset_id: str
    conflict: bool
    winner: Optional[ScoredReading]
    all_scores: list[ScoredReading]
    explanation: str
