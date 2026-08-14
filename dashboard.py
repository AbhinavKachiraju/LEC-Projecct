"""
Streamlit dashboard, reading data/decisions.db directly (same pattern as
the logging dashboard in the RAG project: no API dependency, just point
it at the SQLite file and it renders what's there).

Run: streamlit run dashboard.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).resolve().parent / "data" / "decisions.db"

st.set_page_config(page_title="Asset Location Reconciler", layout="wide")
st.title("Asset Location Reconciler — Decision Log")


def load_table(query: str) -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(query, conn)


decisions = load_table("SELECT * FROM decisions ORDER BY id DESC")
snapshots = load_table("SELECT * FROM trust_snapshots ORDER BY id ASC")
scores = load_table("SELECT * FROM source_scores")

if decisions.empty:
    st.info(
        "No decisions logged yet. Run `python3 demo.py`, `python3 -m src.cli query <asset>`, "
        "or hit the API (`POST /assets/{asset_id}/query`) to generate some, then refresh."
    )
    st.stop()

# --- Top-line stats ---------------------------------------------------
col1, col2, col3, col4 = st.columns(4)
col1.metric("Total decisions", len(decisions))
conflict_rate = decisions["conflict"].mean() * 100
col2.metric("Conflict rate", f"{conflict_rate:.0f}%")
avg_margin = decisions.loc[decisions["conflict"] == 1, "margin"].mean()
col3.metric("Avg. winning margin (conflicts)", f"{avg_margin:.3f}" if pd.notna(avg_margin) else "—")
avg_latency = decisions["latency_ms"].mean()
col4.metric("Avg. latency", f"{avg_latency:.1f} ms")

# --- Reliability over time ---------------------------------------------
st.subheader("Learned reliability over time (per source)")
if snapshots.empty:
    st.caption("No audits logged yet — reliability hasn't moved from its prior. Submit an audit to see this fill in.")
else:
    pivot = snapshots.pivot_table(index="id", columns="source_id", values="reliability_mean")
    st.line_chart(pivot)
    st.caption(
        "Each step is one audit event. A source's line moving down means it was graded "
        "wrong against a physical audit; up means it was graded correct."
    )

# --- Current reliability snapshot ---------------------------------------
st.subheader("Current learned reliability")
if not snapshots.empty:
    latest = snapshots.sort_values("id").groupby("source_id").tail(1)
    st.bar_chart(latest.set_index("source_id")["reliability_mean"])
else:
    st.caption("Showing priors only (no audits yet).")

# --- Filters -------------------------------------------------------------
st.subheader("Decision log")
c1, c2 = st.columns(2)
origin_filter = c1.multiselect("Origin", sorted(decisions["origin"].unique()), default=list(decisions["origin"].unique()))
conflict_filter = c2.selectbox("Show", ["All", "Conflicts only", "Agreements only"])

filtered = decisions[decisions["origin"].isin(origin_filter)]
if conflict_filter == "Conflicts only":
    filtered = filtered[filtered["conflict"] == 1]
elif conflict_filter == "Agreements only":
    filtered = filtered[filtered["conflict"] == 0]

st.dataframe(
    filtered[
        [
            "id", "asset_id", "queried_at", "origin", "conflict",
            "winner_source", "winner_location", "winner_status", "margin",
            "sources_responded", "sources_missing", "latency_ms",
        ]
    ],
    use_container_width=True,
    hide_index=True,
)

# --- Drill into one decision ---------------------------------------------
st.subheader("Inspect a decision")
decision_id = st.selectbox("Decision id", filtered["id"].tolist()) if not filtered.empty else None
if decision_id is not None:
    row = decisions[decisions["id"] == decision_id].iloc[0]
    st.text(row["explanation"])
    detail = scores[scores["decision_id"] == decision_id].sort_values("combined_score", ascending=False)
    st.dataframe(
        detail[["source_id", "location", "status", "reliability_score", "recency_score", "quality_score", "combined_score", "is_winner"]],
        use_container_width=True,
        hide_index=True,
    )
