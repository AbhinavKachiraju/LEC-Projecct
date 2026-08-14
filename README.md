# Asset Location Reconciler

An agent that answers "where is asset X right now?" by consulting multiple
independent, disagreeing operational data sources, deciding which one to
believe, explaining why, and remembering how each source has performed so
that trust in it adjusts over time.

Built for the LEC AI Engineering internship take-home.

## The problem this solves

Real asset-tracking stacks are made of systems that were never designed
to agree with each other: a warehouse management system (WMS) that only
updates on a barcode/RFID scan, a field technician's phone reporting GPS
pings, and a manual check-in sheet a supervisor fills in by hand. Ask
"where is the forklift" and you can easily get three different answers,
none of which is flagged as wrong by the system that produced it.

The naive fixes don't work:
- **First answer wins** — arbitrary, ignores everything you know about
  the sources.
- **Average the answers** — you can't be 60% at Dock 3 and 40% on
  Truck 7. Location and status are categorical, not numeric.
- **Majority vote** — two stale-but-agreeing sources can outvote one
  correct, precise source. See Query 2 in the demo below, where exactly
  this happens.

So the agent instead scores every reading on three independent signals,
combines them into a single trust score, and explains the arithmetic in
plain text every time. When ground truth becomes available later (a
physical audit / cycle count), it grades every source that took part in
that conflict and updates its belief about how reliable each source is —
so the *next* conflict between the same sources starts from a different
prior.

## How trust is decided

For every reading, the engine computes three scores in `[0, 1]` and
combines them with fixed weights (`reliability 0.45`, `recency 0.35`,
`quality 0.20` — see `src/trust_engine.py::WEIGHTS`):

### 1. Reliability — learned, persists across queries
Each source starts with a **Beta-Bernoulli prior** representing what
you'd reasonably assume about it before any evidence:

| Source | Prior mean | Why |
|---|---|---|
| `wms` | 0.85 | precise when it fires (a scan is a scan) |
| `tech_gps` | 0.75 | frequent but coarse, and can go stale |
| `checkin_sheet` | 0.55 | human-entered, error-prone |
| `audit` | 0.95 | independent physical count, close to ground truth |

Every time a physical audit reveals the true location/status, every
source that participated in that conflict is graded against it
(`reconciler.apply_audit_feedback`) and its Beta parameters are updated:
`alpha += 1` if it was right, `beta += 1` if it was wrong. The posterior
mean `alpha / (alpha + beta)` is what future queries use as the
reliability score. This is a proper Bayesian update, not a naive
"accuracy so far" percentage — a source with 1/1 correct isn't treated
as more trustworthy than one with 40/40, because the small-sample
posterior is pulled back toward the prior until there's enough evidence.
State is persisted to `data/trust_state.json` (or a separate file for the
demo), so this genuinely carries across process runs, not just within
one query.

### 2. Recency — decays at a different rate per source type
`recency_score = 0.5 ** (age_hours / half_life_hours)`. Each source type
has its own half-life instead of one global "newest wins" rule, because
that's not how staleness actually works:
- WMS scan → 4h half-life (an asset can easily move within a few hours
  of being scanned)
- GPS ping → 1.5h half-life (mobile assets, frequent updates expected —
  an old ping should be penalized hard)
- Manual check-in → 24h half-life (assets that get logged by hand tend
  to sit still longer, so a same-day entry is still fairly trustworthy)
- Audit → 72h half-life (a physical count is assumed to hold until the
  next one)

### 3. Quality — signals intrinsic to *this specific reading*
Independent of the source's history: GPS accuracy in metres, whether a
WMS scan came from a fixed gate reader vs. a handheld device, whether a
manual entry was supervisor-verified. Two readings from the same source
can score very differently here.

### Optional: business-context boost
`resolve(..., context=ExpectedContext(expected_status=...))` gives a
small (+0.08) boost to a reading whose status matches an external
expectation (e.g. "this asset should be `in_transit` — the delivery
window is still open"). It's deliberately small: context nudges a close
call, it doesn't override strong evidence on its own.

### Conflict detection
`detect_conflict` normalizes location/status across every source that
returned data and checks whether they actually disagree. Sources that
return no data (e.g. a technician's device that hasn't synced) are
reported by name in the explanation, not silently dropped or guessed on
behalf of — see `TOOLBOX-55` in the demo.

### The decision
Readings are sorted by combined score (ties broken by reliability). The
explanation prints every source's full breakdown so a reviewer can see
exactly why the winner won — not just the final number. If the margin
between first and second place is thin (`< 0.03`), the output explicitly
flags it as a low-confidence call and suggests a physical audit rather
than presenting it with false certainty.

## Service layer: API, logging, dashboard

The core reconciler (`models.py`, `trust_engine.py`, `reconciler.py`) is
plain Python with zero dependencies — it doesn't know whether it's being
called from a CLI, a test, or an HTTP request. Everything below is a thin
shell around it.

**API (`src/api.py`, FastAPI).** A query and its later audit are two
separate HTTP calls, deliberately — same as in reality, where a cycle
count often confirms a location hours after the original query, not in
the same request/response. The API is stateless between them: `/audit`
doesn't rely on anything kept in memory from `/query`, it re-reads the
original candidates from `data/decisions.db` by `decision_id` and grades
them from there (`reconciler.grade_sources`). That means a server restart
between a query and its audit doesn't lose anything — the trade-off an
in-memory cache would have made.

**Logging (`src/db.py`, plain `sqlite3`).** Every decision — CLI, API, or
demo, tagged via an `origin` column — writes one row to `decisions` and
one row per candidate source to `source_scores`. Every audit writes a
`trust_snapshots` row per source, which is what lets the dashboard chart
reliability *changing over time* rather than only showing its current
value. This is intentionally not SQLAlchemy: two tables with one foreign
key don't need an ORM's abstraction, and adding one here would be
indirection without a problem it's solving. If this needed to support
multiple tenants, richer querying, or sat behind a framework that already
manages a session/engine lifecycle, that's the point where I'd reach for
it — not before.

**Dashboard (`dashboard.py`, Streamlit).** Reads `data/decisions.db`
directly rather than going through the API, same pattern as the RAG
project's monitoring dashboard: conflict rate, average winning margin,
per-source reliability over time, a filterable decision log, and a
drill-down into any single decision's full score breakdown.

## Repository layout

```
src/
  models.py       Reading / ScoredReading / Resolution dataclasses
  sources.py      Simulated WMS, GPS, check-in sheet, and audit "APIs"
  scenario.py     Randomized conflict generator, shared by the CLI and API
  trust_engine.py Beta-Bernoulli reliability + recency decay + quality scoring
  reconciler.py   Conflict detection, resolution, audit feedback loop
  db.py           SQLite structured logging (decisions, source_scores, trust_snapshots)
  cli.py          Interactive CLI — state in data/trust_state.json, logs to data/decisions.db
  api.py          FastAPI service — same reconciler, HTTP instead of a terminal
dashboard.py      Streamlit dashboard reading data/decisions.db directly
demo.py           Scripted walkthrough — the thing to run for the video
tests/
  test_reconciler.py   trust-scoring and decision logic
  test_db.py           logging roundtrips
Dockerfile
docker-compose.yml    api + dashboard services, shared ./data volume
```

## Running it

### Locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Scripted demo: 5 queries, 3+ conflicts, an explicit before/after
# learning check, and a no-conflict query for contrast.
python3 demo.py

# Interactive CLI: generates a fresh randomized conflict each call,
# state persists in data/trust_state.json between runs.
python3 -m src.cli query FORKLIFT-118
python3 -m src.cli query FORKLIFT-118 --seed 7          # reproducible
python3 -m src.cli query FORKLIFT-118 --audit "Dock 3" --audit-status stored  # feed back ground truth
python3 -m src.cli show-trust                             # inspect learned reliability

# API
uvicorn src.api:app --reload
# then: POST   /assets/{asset_id}/query        {"seed": 7, "expected_status": "in_transit"}
#       POST   /decisions/{decision_id}/audit  {"location": "Dock 3", "status": "stored"}
#       GET    /trust
#       GET    /decisions?limit=20
#       GET    /docs   (Swagger UI, auto-generated)

# Dashboard (point it at the same data/decisions.db the CLI/API/demo write to)
streamlit run dashboard.py

# Tests
python3 -m pytest tests/ -v
```

### Docker

```bash
docker compose up --build
# API:       http://localhost:8000/docs
# Dashboard: http://localhost:8501
```
Both services mount `./data` as a shared volume, so a decision logged by
the API shows up in the dashboard immediately, and `data/trust_state.json`
is the same file both containers read and write — no separate database
server to stand up for a take-home of this size.

## What the demo actually shows

Run `python3 demo.py`. Five queries against a fresh, deterministic trust
state:

1. **`FORKLIFT-118`** — WMS (fresh scan) vs. GPS (older, coarser ping)
   disagree. WMS wins on paper. An audit then reveals the GPS was
   actually right — WMS's reliability is marked down, GPS's up.
2. **`PALLET-204`** — WMS *and* the check-in sheet agree with each
   other; GPS disagrees with both. A vote-counting agent would pick the
   2-1 majority. This agent still weighs by score, and — helped by the
   trust update from query 1 plus a very precise (12m) GPS reading —
   picks GPS. The margin is thin enough that the output flags it as a
   low-confidence call. Audit confirms GPS was right again.
3. **`TOOLBOX-55`** — the technician's device hasn't synced (`None`
   returned, named explicitly in the explanation, not guessed). WMS is
   an 8-hour-old scan; the check-in sheet is a 2-hour-old,
   supervisor-verified entry. Staleness sinks WMS despite its higher
   base reliability. This is WMS's third consecutive miss.
4. **`CRATE-77`** — the same *shape* of conflict as query 1 (fresh WMS
   scan vs. slightly older, precise GPS ping), run after three audits.
   The winner flips from `wms` to `tech_gps` even though the input shape
   didn't change — that's the accumulated audit history changing the
   decision, printed explicitly as a before/after comparison.
5. **`DRILL-09`** — all three sources agree; shown for contrast so the
   demo isn't only conflicts.

Final printed reliability after the run: `wms` drops from a 0.85 prior
to ~0.65 (0 correct / 3 wrong), `tech_gps` rises from 0.75 to ~0.79
(2/2), `checkin_sheet` settles around 0.50 (1/3).

## Design decisions worth defending

- **Beta-Bernoulli over a raw accuracy percentage** for reliability,
  specifically so a source doesn't get an overconfident reliability
  score off one or two audits — the posterior is anchored by the prior
  until there's real evidence.
- **Per-source-type recency half-life** instead of a single "newest
  wins" rule, because a 3-hour-old GPS ping and a 3-hour-old check-in
  sheet entry mean very different things about how stale the data
  actually is.
- **Score-based ranking, not majority vote**, precisely because two
  correlated-but-wrong sources (e.g. WMS and a check-in sheet that was
  copied from the same stale scan) shouldn't be able to outvote one
  correct, high-quality reading. Query 2 in the demo is built to make
  this visible.
- **Audits are the only source of ground-truth feedback.** The agent
  never grades a source against another non-audit source's answer —
  that would let two bad sources reinforce each other's *incorrectness*.
  Reliability only moves when something closer to ground truth (the
  audit) is available.
- **Missing data is reported, not imputed.** `tech_gps` returning `None`
  when a device hasn't synced is treated as a real signal (see
  `TOOLBOX-55`), not silently dropped or backfilled with a guess.
- **Thin-margin flag.** A win by 0.01 and a win by 0.3 are very
  different levels of confidence; the explanation says so rather than
  presenting every decision with the same certainty.

## What I'd do next with more time

- **Confidence decay for sources that go quiet.** A source that simply
  stops responding for days should probably have its reliability
  estimate treated as stale/uncertain, not frozen at its last value.
- **Correlated-source detection.** If the check-in sheet is regularly
  transcribed *from* the WMS screen, they're not really independent
  evidence and shouldn't both get full weight when they agree — I'd want
  to detect and account for that instead of assuming independence.
- **Swap the hand-tuned weights (`0.45/0.35/0.20`) for something learned**
  from a larger audit history once there's enough data to fit them,
  rather than picking them by judgment.
- **Auth and multi-tenancy on the API.** Right now it's a single shared
  `trust_state.json` and one SQLite file — fine for a demo, not for
  multiple warehouses or customers. That's also the point where `db.py`
  moving to SQLAlchemy + Postgres would start earning its keep.
- **Automatic audit triggering.** The demo controls when an audit fires.
  A real policy would trigger one automatically when the margin between
  the top two candidates is below a threshold, or when a source's
  reliability crosses a "flag for review" line — the dashboard's
  conflict-rate and margin metrics are exactly what that policy would
  read from.
- **Per-asset-type profiles.** A forklift and a hand tool have very
  different plausible movement rates; right now recency half-lives are
  per-source, not per-(source, asset-type). That's the next axis of
  nuance I'd add.

## Honesty about scope

The three "sources" are stubs by design (per the brief) — `src/sources.py`
generates readings with realistic failure modes (staleness, coarse GPS,
unverified manual entries, devices that haven't synced) rather than
calling real APIs, since no real WMS/GPS/check-in system was provided.
Swapping in real integrations means replacing the bodies of the four
functions in that file; `Reading`, the trust engine, and the reconciler
don't need to change.
