"""
FastAPI wrapper around the reconciler.

Endpoints:
  GET  /health                          liveness check
  POST /assets/{asset_id}/query         run a fresh multi-source query, log it, return the decision
  POST /decisions/{decision_id}/audit   submit ground truth for a past decision; updates trust
  GET  /trust                           current learned reliability per source
  GET  /decisions                       recent decision log (what the dashboard reads via SQLite directly,
                                         but exposed here too so the log is inspectable over HTTP)

Design note: a query and its later audit are two separate HTTP calls,
same as they'd be two separate real-world events (you query a location
now, a cycle-count team confirms it hours or days later). The decision
row's id is the handle that connects them — the API never keeps
in-memory state between requests, everything needed to grade an audit is
re-read from `data/decisions.db`.

Run: uvicorn src.api:app --reload
Docs: http://localhost:8000/docs (FastAPI's auto-generated Swagger UI)
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import db
from .reconciler import resolve, grade_sources, ExpectedContext
from .scenario import generate_readings
from .sources import audit_lookup
from .trust_engine import TrustState

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TRUST_PATH = DATA_DIR / "trust_state.json"
DB_PATH = DATA_DIR / "decisions.db"

app = FastAPI(
    title="Asset Location Reconciler",
    description="Reconciles conflicting asset-location reports across multiple operational data sources.",
    version="1.0.0",
)


class QueryRequest(BaseModel):
    seed: Optional[int] = None
    expected_status: Optional[str] = None


class SourceScoreOut(BaseModel):
    source_id: str
    location: str
    status: str
    reliability_score: float
    recency_score: float
    quality_score: float
    combined_score: float
    is_winner: bool


class QueryResponse(BaseModel):
    decision_id: int
    asset_id: str
    conflict: bool
    winner_source: Optional[str]
    winner_location: Optional[str]
    winner_status: Optional[str]
    explanation: str
    scores: list[SourceScoreOut]


class AuditRequest(BaseModel):
    location: str
    status: str


class AuditResponse(BaseModel):
    decision_id: int
    log: list[str]


class TrustOut(BaseModel):
    source_id: str
    reliability_mean: float
    times_seen: int
    times_audited_correct: int
    times_audited_wrong: int


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/assets/{asset_id}/query", response_model=QueryResponse)
def query_asset(asset_id: str, body: QueryRequest = QueryRequest()):
    start = time.perf_counter()
    trust_state = TrustState(TRUST_PATH)
    as_of = datetime.now(timezone.utc).replace(tzinfo=None)
    rng = random.Random(body.seed)
    readings = generate_readings(asset_id, as_of, rng)

    context = ExpectedContext(expected_status=body.expected_status, note="from API") if body.expected_status else None
    resolution = resolve(asset_id, readings, trust_state, as_of, context=context)
    trust_state.save()
    latency_ms = (time.perf_counter() - start) * 1000

    sources_missing = sum(1 for r in readings.values() if r is None)
    decision_id = db.log_decision(
        DB_PATH, resolution, origin="api", latency_ms=latency_ms,
        sources_responded=len(readings) - sources_missing, sources_missing=sources_missing,
    )

    return QueryResponse(
        decision_id=decision_id,
        asset_id=asset_id,
        conflict=resolution.conflict,
        winner_source=resolution.winner.reading.source_id if resolution.winner else None,
        winner_location=resolution.winner.reading.location if resolution.winner else None,
        winner_status=resolution.winner.reading.status if resolution.winner else None,
        explanation=resolution.explanation,
        scores=[
            SourceScoreOut(
                source_id=s.reading.source_id,
                location=s.reading.location,
                status=s.reading.status,
                reliability_score=s.reliability_score,
                recency_score=s.recency_score,
                quality_score=s.quality_score,
                combined_score=s.combined_score,
                is_winner=(resolution.winner is not None and s is resolution.winner),
            )
            for s in resolution.all_scores
        ],
    )


@app.post("/decisions/{decision_id}/audit", response_model=AuditResponse)
def audit_decision(decision_id: int, body: AuditRequest):
    records = db.fetch_source_scores(DB_PATH, decision_id)
    if not records:
        raise HTTPException(status_code=404, detail=f"No decision found with id {decision_id}")

    as_of = datetime.now(timezone.utc).replace(tzinfo=None)
    decision = db.fetch_decision(DB_PATH, decision_id)
    asset_id = decision["asset_id"] if decision else "unknown"

    audit_reading = audit_lookup(asset_id, body.location, body.status, as_of)
    trust_state = TrustState(TRUST_PATH)

    candidates = [(r["source_id"], r["location"], r["status"]) for r in records]
    log = grade_sources(candidates, audit_reading, trust_state)

    db.log_trust_snapshot(DB_PATH, trust_state, trigger=f"audit:decision_{decision_id}")

    return AuditResponse(decision_id=decision_id, log=log)


@app.get("/trust", response_model=list[TrustOut])
def get_trust():
    trust_state = TrustState(TRUST_PATH)
    return [
        TrustOut(
            source_id=sid,
            reliability_mean=stats.reliability_mean,
            times_seen=stats.times_seen,
            times_audited_correct=stats.times_audited_correct,
            times_audited_wrong=stats.times_audited_wrong,
        )
        for sid, stats in trust_state.stats.items()
    ]


@app.get("/decisions")
def get_decisions(limit: int = 50):
    return db.fetch_recent_decisions(DB_PATH, limit=limit)
