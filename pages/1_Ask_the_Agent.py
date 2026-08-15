"""
Interactive query page. This is the "ask your own question" counterpart
to demo.py's scripted scenarios: you type an asset id, the agent
generates fresh (simulated) readings from all three sources right now,
resolves any conflict, and shows the full score breakdown live. You can
then feed back an audit result and watch the reliability numbers move.

Run: streamlit run dashboard.py   (this page appears in the sidebar)
"""

from __future__ import annotations

import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import db
from src.reconciler import resolve, apply_audit_feedback, ExpectedContext
from src.scenario import generate_readings, LOCATIONS, STATUSES
from src.sources import audit_lookup
from src.trust_engine import TrustState

DATA_DIR = ROOT / "data"
TRUST_PATH = DATA_DIR / "trust_state.json"
DB_PATH = DATA_DIR / "decisions.db"

st.set_page_config(page_title="Ask the Agent", layout="wide")
st.title("Ask the Agent")
st.caption(
    "Type an asset id and query it live, this hits the same reconciler as the CLI, "
    "API, and demo, generating a fresh (simulated) reading from each source right now."
)

if "last_resolution" not in st.session_state:
    st.session_state.last_resolution = None
    st.session_state.last_decision_id = None
    st.session_state.last_asset_id = None

with st.form("query_form"):
    col1, col2, col3 = st.columns([2, 1, 1])

    logged = db.fetch_recent_decisions(DB_PATH, limit=1000) if DB_PATH.exists() else []
    known_assets = {row["asset_id"] for row in logged}
    suggested_assets = {"FORKLIFT-118", "PALLET-204", "TOOLBOX-55", "CRATE-77", "DRILL-09"}
    asset_options = sorted(known_assets | suggested_assets) + ["+ Type a new asset ID"]

    asset_choice = col1.selectbox("Asset ID", asset_options, index=0)
    if asset_choice == "+ Type a new asset ID":
        asset_id = col1.text_input("New asset ID", placeholder="e.g. LOADER-9")
    else:
        asset_id = asset_choice

    expected_status = col2.selectbox("Expected status (optional)", [""] + STATUSES)
    seed = col3.number_input("Seed (optional, for reproducibility)", min_value=0, value=0, step=1)
    submitted = st.form_submit_button("Locate asset", type="primary")

if submitted:
    if not asset_id.strip():
        st.error("Enter an asset id first.")
    else:
        trust_state = TrustState(TRUST_PATH)
        as_of = datetime.now(timezone.utc).replace(tzinfo=None)
        rng = random.Random(seed or None)
        readings = generate_readings(asset_id, as_of, rng)

        context = ExpectedContext(expected_status=expected_status, note="from web") if expected_status else None

        start = time.perf_counter()
        resolution = resolve(asset_id, readings, trust_state, as_of, context=context)
        latency_ms = (time.perf_counter() - start) * 1000
        trust_state.save()

        sources_missing = sum(1 for r in readings.values() if r is None)
        decision_id = db.log_decision(
            DB_PATH, resolution, origin="web", latency_ms=latency_ms,
            sources_responded=len(readings) - sources_missing, sources_missing=sources_missing,
        )

        st.session_state.last_resolution = resolution
        st.session_state.last_decision_id = decision_id
        st.session_state.last_asset_id = asset_id

resolution = st.session_state.last_resolution

if resolution is not None:
    st.divider()

    if resolution.winner is None:
        st.warning(resolution.explanation)
    else:
        if resolution.conflict:
            st.error(f"Conflict detected on **{resolution.asset_id}** - sources disagree.")
        else:
            st.success(f"All sources agree on **{resolution.asset_id}**.")

        c1, c2 = st.columns([1, 2])
        with c1:
            st.metric("Agent's answer", f"{resolution.winner.reading.location}")
            st.caption(f"Status: {resolution.winner.reading.status}")
            st.caption(f"Trusted source: `{resolution.winner.reading.source_id}`")
            st.caption(f"Decision logged as #{st.session_state.last_decision_id}")

        with c2:
            st.markdown("**Score breakdown**")
            rows = [
                {
                    "source": s.reading.source_id,
                    "says": f"{s.reading.location} / {s.reading.status}",
                    "reliability": round(s.reliability_score, 3),
                    "recency": round(s.recency_score, 3),
                    "quality": round(s.quality_score, 3),
                    "combined": round(s.combined_score, 3),
                    "winner": "✓" if s is resolution.winner else "",
                }
                for s in sorted(resolution.all_scores, key=lambda s: s.combined_score, reverse=True)
            ]
            st.dataframe(rows, width='stretch', hide_index=True)

        with st.expander("Full explanation (what the agent printed)"):
            st.text(resolution.explanation)

    st.divider()
    st.subheader("Submit an audit result")
    st.caption(
        "Once a physical cycle count confirms the true location, feed it back here, "
        "the agent grades every source that answered this query against it and adjusts "
        "their learned reliability for future queries."
    )
    with st.form("audit_form"):
        a1, a2, a3 = st.columns([2, 2, 1])
        audit_location = a1.selectbox("True location", LOCATIONS)
        audit_status = a2.selectbox("True status", STATUSES)
        audit_submitted = a3.form_submit_button("Submit audit")

    if audit_submitted:
        trust_state = TrustState(TRUST_PATH)
        as_of = datetime.now(timezone.utc).replace(tzinfo=None)
        audit_reading = audit_lookup(st.session_state.last_asset_id, audit_location, audit_status, as_of)
        log_lines = apply_audit_feedback(resolution, audit_reading, trust_state)
        db.log_trust_snapshot(DB_PATH, trust_state, trigger=f"audit:decision_{st.session_state.last_decision_id}")

        st.success("Audit recorded. Reliability updated:")
        for line in log_lines:
            st.text(line)
        st.caption("Query the same asset shape again to see whether the decision changes.")

else:
    st.info("Enter an asset id above and click **Locate asset** to run a live query.")
