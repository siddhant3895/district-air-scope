"""Snapshot assembly service for DistrictWatch.

Constructs validated DistrictSnapshot payloads from real ingested data or
returns truthful 'unavailable' snapshots when no live data has been ingested yet
(never fabricating AQI values, station counts, or historical series).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from app.schemas import District, DistrictSnapshot, PollutantKey, Reading
from app.services.naqi import build_pollutant_list, compute_overall_aqi
from app.services.quality import (
    build_caveats,
    classify_quality,
    has_pm_inversion_anomaly,
    sanitize_concentration,
)

HISTORY_WINDOW_DAYS = 30


def build_empty_history(anchor_date: Optional[datetime] = None, days: int = HISTORY_WINDOW_DAYS) -> List[Reading]:
    """Build a 30-day calendar sequence with explicit aqi=None gaps (no fabricated history)."""
    ref = (anchor_date or datetime.now(timezone.utc)).date()
    return [
        Reading(date=(ref - timedelta(days=days - 1 - i)).isoformat(), aqi=None)
        for i in range(days)
    ]


def build_unavailable_snapshot(
    district: District,
    source_label: str = "Unconfigured (awaiting CPCB / OpenAQ ingestion)",
    reason: str = "No live station readings have been ingested for this district yet.",
    anchor_date: Optional[datetime] = None,
) -> DistrictSnapshot:
    """Return a truthful 'unavailable' snapshot without fabricating any readings."""
    pollutants = build_pollutant_list({})
    history = build_empty_history(anchor_date=anchor_date)
    return DistrictSnapshot(
        district=district,
        aqi=None,
        dominant=None,
        updatedAt=None,
        source=source_label,
        stations=0,
        coverage=0.0,
        quality="unavailable",
        caveats=build_caveats(
            quality="unavailable",
            stations=0,
            coverage=0.0,
            pollutants=pollutants,
            custom_unavailable_reason=reason,
        ),
        pollutants=pollutants,
        history=history,
        isIllustrative=False,
    )


def assemble_district_snapshot(
    *,
    district: District,
    raw_pollutants: Dict[PollutantKey, Optional[float]],
    stations: int,
    coverage: float,
    updated_at: Optional[str],
    source: str,
    history: Optional[List[Reading]] = None,
    now: Optional[datetime] = None,
    is_illustrative: bool = False,
) -> DistrictSnapshot:
    """Assemble and validate a DistrictSnapshot from station-aggregated pollutant readings."""
    sanitized: Dict[PollutantKey, Optional[float]] = {
        k: sanitize_concentration(k, v) for k, v in raw_pollutants.items()
    }
    pm_inversion = has_pm_inversion_anomaly(sanitized.get("PM2.5"), sanitized.get("PM10"))
    pollutants = build_pollutant_list(sanitized)
    aqi, dominant, meets_cpcb_rule = compute_overall_aqi(pollutants)

    clamped_coverage = max(0.0, min(1.0, round(float(coverage), 4))) if stations > 0 else 0.0
    quality = classify_quality(stations=stations, coverage=clamped_coverage, aqi=aqi)

    if quality == "unavailable":
        aqi = None
        dominant = None
        updated_at = None

    caveats = build_caveats(
        quality=quality,
        stations=stations,
        coverage=clamped_coverage,
        pollutants=pollutants,
        meets_cpcb_minimum_rule=meets_cpcb_rule,
        updated_at_iso=updated_at,
        now=now,
        pm_inversion_detected=pm_inversion,
    )

    resolved_history = history if history is not None else build_empty_history(anchor_date=now)

    return DistrictSnapshot(
        district=district,
        aqi=aqi,
        dominant=dominant,
        updatedAt=updated_at,
        source=source,
        stations=max(0, int(stations)),
        coverage=clamped_coverage,
        quality=quality,
        caveats=caveats,
        pollutants=pollutants,
        history=resolved_history,
        isIllustrative=is_illustrative,
    )
