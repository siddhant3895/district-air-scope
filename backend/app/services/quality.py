"""Data quality classification, physical plausibility checks, freshness tracking, and caveat generation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional
from app.schemas import Pollutant, PollutantKey, QualityLevel

# Physical upper bounds to reject obvious instrument malfunctions / spikes
MAX_PLAUSIBLE_CONCENTRATION: Dict[PollutantKey, float] = {
    "PM2.5": 1500.0,  # µg/m³
    "PM10": 2000.0,   # µg/m³
    "NO2": 1000.0,    # µg/m³
    "SO2": 2500.0,    # µg/m³
    "O3": 1500.0,     # µg/m³
    "CO": 100.0,      # mg/m³
}


def sanitize_concentration(pollutant: PollutantKey, value: Optional[float]) -> Optional[float]:
    """Return None if value is missing, negative, or exceeds physical sensor plausibility bounds."""
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < 0.0 or numeric > MAX_PLAUSIBLE_CONCENTRATION[pollutant]:
        return None
    return numeric


def has_pm_inversion_anomaly(pm25: Optional[float], pm10: Optional[float]) -> bool:
    """Check whether PM2.5 exceeds PM10 beyond a 5% instrument tolerance."""
    if pm25 is None or pm10 is None or pm10 <= 0:
        return False
    return pm25 > (pm10 * 1.05)


def classify_quality(
    stations: int,
    coverage: float,
    aqi: Optional[int],
) -> QualityLevel:
    """Classify district data quality matching src/routes/methodology.tsx rules:
    - unavailable: No readings received (stations <= 0 or coverage <= 0 or aqi is None)
    - high: >= 85% coverage, 3+ stations
    - low: < 60% coverage or <= 2 stations
    - medium: 60-85% coverage with 3+ stations
    """
    if aqi is None or stations <= 0 or coverage <= 0.0:
        return "unavailable"
    if coverage < 0.60 or stations <= 2:
        return "low"
    if coverage >= 0.85 and stations >= 3:
        return "high"
    return "medium"


def is_reading_stale(
    updated_at_iso: Optional[str],
    now: Optional[datetime] = None,
    stale_threshold_hours: float = 6.0,
) -> bool:
    """Check if an ISO-8601 timestamp is older than stale_threshold_hours (default 6h)."""
    if not updated_at_iso:
        return False
    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)

    iso_clean = updated_at_iso.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso_clean)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    age_hours = (ref_now - dt).total_seconds() / 3600.0
    return age_hours > stale_threshold_hours


def build_caveats(
    *,
    quality: QualityLevel,
    stations: int,
    coverage: float,
    pollutants: List[Pollutant],
    meets_cpcb_minimum_rule: bool = True,
    updated_at_iso: Optional[str] = None,
    now: Optional[datetime] = None,
    stale_threshold_hours: float = 6.0,
    pm_inversion_detected: bool = False,
    custom_unavailable_reason: Optional[str] = None,
) -> List[str]:
    """Generate human-readable caveats aligned with frontend expectations and CPCB rules."""
    caveats: List[str] = []
    unavailable = quality == "unavailable"

    if unavailable:
        caveats.append(
            custom_unavailable_reason
            or "No station readings received for this district in the sample window."
        )
        return caveats

    if coverage < 0.60:
        caveats.append("Coverage below 60% — summary values may not represent the full district.")
    if stations <= 2:
        caveats.append("Few monitoring stations; readings may reflect local rather than district-wide conditions.")
    if any(p.value is None for p in pollutants):
        caveats.append("One or more pollutants not reported by stations in this district.")
    if not meets_cpcb_minimum_rule:
        caveats.append(
            "Fewer than 3 pollutants (or no PM2.5/PM10) reported; CPCB requires at least 3 pollutants including PM2.5 or PM10 for an official AQI."
        )
    if is_reading_stale(updated_at_iso, now=now, stale_threshold_hours=stale_threshold_hours):
        caveats.append("Latest station reading is more than 6 hours old.")
    if pm_inversion_detected:
        caveats.append("Sensor anomaly detected: reported PM2.5 exceeds PM10 concentration.")

    return caveats
