"""
Structured logging for the reconciler, mirroring the pattern from the RAG
project: every decision the agent makes gets written to SQLite (asset,
conflict or not, winner, margin, latency, full explanation), every
individual source's score breakdown for that decision gets its own row,
and every trust update (post-audit) gets a timestamped snapshot. That
last table is what lets the dashboard chart reliability *changing over
time* instead of only showing its current value.

Three tables, stdlib `sqlite3` only:

  decisions        one row per resolve() call
  source_scores    one row per (decision, source) pair
  trust_snapshots  one row per source per audit event

`origin` on `decisions` tags where the query came from ('cli', 'api',
'demo') so the dashboard/API can filter scripted demo data out of real
traffic if needed, the same reason you'd tag environment/service in a
real logging pipeline.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import Resolution
from .trust_engine import TrustState

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT NOT NULL,
    queried_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    conflict INTEGER NOT NULL,
    winner_source TEXT,
    winner_location TEXT,
    winner_status TEXT,
    combined_score REAL,
    margin REAL,
    sources_responded INTEGER NOT NULL,
    sources_missing INTEGER NOT NULL,
    latency_ms REAL NOT NULL,
    explanation TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    source_id TEXT NOT NULL,
    location TEXT NOT NULL,
    status TEXT NOT NULL,
    reliability_score REAL NOT NULL,
    recency_score REAL NOT NULL,
    quality_score REAL NOT NULL,
    combined_score REAL NOT NULL,
    is_winner INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trust_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    source_id TEXT NOT NULL,
    reliability_mean REAL NOT NULL,
    alpha REAL NOT NULL,
    beta REAL NOT NULL,
    trigger TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_asset ON decisions(asset_id);
CREATE INDEX IF NOT EXISTS idx_source_scores_decision ON source_scores(decision_id);
CREATE INDEX IF NOT EXISTS idx_trust_snapshots_source ON trust_snapshots(source_id);
"""


@contextmanager
def _connect(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)


def log_decision(
    db_path: Path,
    resolution: Resolution,
    origin: str,
    latency_ms: float,
    sources_responded: int,
    sources_missing: int,
) -> int:
    """Writes the decision row plus one source_scores row per candidate. Returns decision id."""
    init_db(db_path)

    winner = resolution.winner
    margin = None
    if winner is not None and len(resolution.all_scores) > 1:
        sorted_scores = sorted(resolution.all_scores, key=lambda s: s.combined_score, reverse=True)
        margin = sorted_scores[0].combined_score - sorted_scores[1].combined_score

    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO decisions (
                asset_id, queried_at, origin, conflict, winner_source, winner_location,
                winner_status, combined_score, margin, sources_responded, sources_missing,
                latency_ms, explanation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolution.asset_id,
                datetime.now(timezone.utc).isoformat(),
                origin,
                int(resolution.conflict),
                winner.reading.source_id if winner else None,
                winner.reading.location if winner else None,
                winner.reading.status if winner else None,
                winner.combined_score if winner else None,
                margin,
                sources_responded,
                sources_missing,
                latency_ms,
                resolution.explanation,
            ),
        )
        decision_id = cur.lastrowid

        for s in resolution.all_scores:
            conn.execute(
                """
                INSERT INTO source_scores (
                    decision_id, source_id, location, status, reliability_score,
                    recency_score, quality_score, combined_score, is_winner
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    s.reading.source_id,
                    s.reading.location,
                    s.reading.status,
                    s.reliability_score,
                    s.recency_score,
                    s.quality_score,
                    s.combined_score,
                    int(winner is not None and s is winner),
                ),
            )
        return decision_id


def log_trust_snapshot(db_path: Path, trust_state: TrustState, trigger: str) -> None:
    """Call after any audit so the dashboard can plot reliability over time."""
    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        for source_id, stats in trust_state.stats.items():
            conn.execute(
                """
                INSERT INTO trust_snapshots (recorded_at, source_id, reliability_mean, alpha, beta, trigger)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now, source_id, stats.reliability_mean, stats.alpha, stats.beta, trigger),
            )


def fetch_recent_decisions(db_path: Path, limit: int = 50, origin: Optional[str] = None) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        if origin:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE origin = ? ORDER BY id DESC LIMIT ?",
                (origin, limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def fetch_decision(db_path: Path, decision_id: int) -> Optional[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        return dict(row) if row else None


def fetch_source_scores(db_path: Path, decision_id: int) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM source_scores WHERE decision_id = ? ORDER BY combined_score DESC",
            (decision_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def fetch_trust_history(db_path: Path) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM trust_snapshots ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]
