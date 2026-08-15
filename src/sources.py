"""
Simulated operational data sources.

These stand in for the real integrations the brief mentions (WMS API,
technician GPS feed, manual check-in sheet). Each one is deliberately
built to fail in a *different, realistic* way rather than just returning
a random yes/no:

- WMS (`wms`): fires only on a scan event (dock door, shelf, handheld
  scanner). High precision when it fires, but goes stale the moment an
  asset moves without being rescanned.
- Technician GPS log (`tech_gps`): near-continuous pings, but coarse
  accuracy (metres, not bins) and technicians sometimes forget to sync,
  leaving gaps.
- Manual check-in sheet (`checkin_sheet`): a human writes down what they
  see. Infrequent and error-prone, but sometimes it's the only record for
  an asset that never passes a scanner.
- Audit / cycle count (`audit`): a periodic physical count by an
  independent team. Rare, but treated as close to ground truth, and used
  to give the trust engine feedback on who was right last time.

Every function below returns a `Reading` (or `None` if that source has no
data for the asset, which is itself a signal worth surfacing) and accepts
a `truth` dict describing what "really" happened, purely so the demo can
construct realistic conflicting scenarios deterministically. In a real
deployment these functions would be HTTP calls to the WMS API, a GPS
telemetry table, etc. the reconciler does not care where the Reading
came from, only what's in it.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Optional

from .models import Reading


def wms_lookup(
    asset_id: str,
    location: str,
    status: str,
    as_of: datetime,
    minutes_ago: float,
    scan_type: str = "handheld_scan",
) -> Reading:
    """A warehouse management system scan event."""
    return Reading(
        source_id="wms",
        source_type="automated_scan",
        asset_id=asset_id,
        location=location,
        status=status,
        timestamp=as_of - timedelta(minutes=minutes_ago),
        metadata={"scan_type": scan_type},
    )


def tech_gps_lookup(
    asset_id: str,
    location: str,
    status: str,
    as_of: datetime,
    minutes_ago: float,
    gps_accuracy_m: float = 15.0,
    synced: bool = True,
) -> Optional[Reading]:
    """A field technician's device last-known-position log."""
    if not synced:
        return None  # device hasn't phoned home, honest "no data", not a guess
    return Reading(
        source_id="tech_gps",
        source_type="gps_ping",
        asset_id=asset_id,
        location=location,
        status=status,
        timestamp=as_of - timedelta(minutes=minutes_ago),
        metadata={"gps_accuracy_m": gps_accuracy_m},
    )


def checkin_sheet_lookup(
    asset_id: str,
    location: str,
    status: str,
    as_of: datetime,
    minutes_ago: float,
    verified_by_supervisor: bool = False,
) -> Reading:
    """A manual, paper/spreadsheet check-in entry."""
    return Reading(
        source_id="checkin_sheet",
        source_type="manual_entry",
        asset_id=asset_id,
        location=location,
        status=status,
        timestamp=as_of - timedelta(minutes=minutes_ago),
        metadata={"verified_by_supervisor": verified_by_supervisor},
    )


def audit_lookup(
    asset_id: str,
    location: str,
    status: str,
    as_of: datetime,
    minutes_ago: float = 0,
) -> Reading:
    """
    An independent physical cycle count. Rare in practice (you don't
    audit every asset every query) which is why it's invoked explicitly
    by the demo/CLI rather than on every lookup, but when present it is
    the closest thing the system has to ground truth, and it is what
    the trust engine uses to grade the other sources after the fact.
    """
    return Reading(
        source_id="audit",
        source_type="physical_audit",
        asset_id=asset_id,
        location=location,
        status=status,
        timestamp=as_of - timedelta(minutes=minutes_ago),
        metadata={"method": "cycle_count"},
    )


def random_reading(
    source_fn,
    asset_id: str,
    as_of: datetime,
    locations: list[str],
    statuses: list[str],
    rng: random.Random,
    **kwargs,
):
    """
    Helper for the interactive/random CLI mode described in the brief:
    generates a plausible-but-random reading from a given source function,
    so you can hammer the agent with fresh conflicts on demand.
    """
    return source_fn(
        asset_id=asset_id,
        location=rng.choice(locations),
        status=rng.choice(statuses),
        as_of=as_of,
        minutes_ago=rng.uniform(1, 240),
        **kwargs,
    )
