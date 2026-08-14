"""
Randomized scenario generator, shared by the CLI and the API so both
entry points produce conflicts with the same realistic shape instead of
duplicating (and inevitably drifting apart on) this logic.

Biases toward *some* sources agreeing and one drifting, which mirrors real
disagreement patterns better than three independently random answers would
(in practice, two systems that are both fed by the same warehouse process
usually agree; the odd one out is where the interesting reconciliation
happens).
"""

from __future__ import annotations

import random
from datetime import datetime

from .models import Reading
from .sources import wms_lookup, tech_gps_lookup, checkin_sheet_lookup

LOCATIONS = ["Dock 3", "Shelf B12", "Staging Area", "Truck 7", "Site Office"]
STATUSES = ["stored", "in_transit", "checked_out", "in_use"]


def generate_readings(
    asset_id: str, as_of: datetime, rng: random.Random
) -> dict[str, Reading | None]:
    true_location = rng.choice(LOCATIONS)
    true_status = rng.choice(STATUSES)

    wms = wms_lookup(
        asset_id, true_location, true_status, as_of,
        minutes_ago=rng.uniform(5, 300),
        scan_type=rng.choice(["handheld_scan", "fixed_gate_reader"]),
    )

    if rng.random() < 0.7:
        gps_loc, gps_status = true_location, true_status
    else:
        gps_loc, gps_status = rng.choice(LOCATIONS), rng.choice(STATUSES)
    gps = tech_gps_lookup(
        asset_id, gps_loc, gps_status, as_of,
        minutes_ago=rng.uniform(1, 200),
        gps_accuracy_m=rng.uniform(5, 55),
        synced=rng.random() > 0.15,
    )

    if rng.random() < 0.5:
        sheet_loc, sheet_status = true_location, true_status
    else:
        sheet_loc, sheet_status = rng.choice(LOCATIONS), rng.choice(STATUSES)
    sheet = checkin_sheet_lookup(
        asset_id, sheet_loc, sheet_status, as_of,
        minutes_ago=rng.uniform(30, 1000),
        verified_by_supervisor=rng.random() > 0.6,
    )

    return {"wms": wms, "tech_gps": gps, "checkin_sheet": sheet}
