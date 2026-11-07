"""Persistence domain models for DistrictWatch SQLite storage.

Models raw station observations, station metadata, station-level AQI evaluations,
daily district historical summaries, and ingestion sync run audit records.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional
from app.schemas import PollutantKey, QualityLevel

SourceProvider = Literal[
    "cpcb_ogd",
    "openaq_v3",
    "xkdr",
    "hybrid_cpcb_openaq",
    "hybrid_xkdr_openaq",
    "hybrid_cpcb_xkdr",
    "hybrid_all",
    "none",
]
ValueKind = Literal["concentration", "sub_index", "unverified_scale", "invalid", "missing"]
SyncStatus = Literal["succeeded", "partial_failure", "failed", "no_providers_configured", "never_run"]


@dataclass(frozen=True)
class StationRecord:
    """Verified monitoring station entry mapped to a specific Indian administrative district."""

    station_id: str
    district_id: str
    source_provider: SourceProvider
    external_station_id: str  # Exact CPCB station name or OpenAQ location ID string
    station_name: str
    operating_agency: str  # e.g. CPCB, DPCC, UPPCB, MPCB, KSPCB, WBPCB, TNPCB, BSPCB, IMD, IITM
    city: str
    state: str
    lat: Optional[float]
    lon: Optional[float]
    expected_pollutants: List[PollutantKey] = field(default_factory=list)
    is_verified_member: bool = True
    membership_basis: str = ""
    openaq_location_id: Optional[int] = None
    openaq_verified_available: bool = True


@dataclass(frozen=True)
class RawStationObservation:
    """Single hourly or upstream-polled pollutant observation for a specific station."""

    station_id: str
    source_provider: SourceProvider
    upstream_sensor_or_record_id: str
    pollutant_key: PollutantKey
    observed_at_utc: str  # ISO-8601 UTC timestamp
    window_hours: int  # 1 for hourly readings; 24 for pre-averaged daily snapshots
    raw_value_text: str
    raw_unit: Optional[str]
    value_kind: ValueKind
    normalized_concentration: Optional[float]  # In canonical NAQI unit (µg/m³ or mg/m³ for CO)
    normalized_unit: Optional[str]
    calculated_sub_index: Optional[int]
    is_valid: bool
    validation_note: str
    raw_payload_json: str
    ingested_at_utc: str


@dataclass(frozen=True)
class StationPollutantEvaluation:
    """Evaluated 24h (or 8h for CO/O3) pollutant window for a single station."""

    pollutant_key: PollutantKey
    window_hours: int  # 24 for PM2.5, PM10, NO2, SO2; 8 for CO, O3
    required_valid_hours: int  # 16 out of 24 hours per CPCB rule
    observed_valid_hours: int
    expected_hours: int  # 24
    meets_completeness: bool
    mean_concentration: Optional[float]
    unit: str
    sub_index: Optional[int]
    value_kind_used: ValueKind


@dataclass(frozen=True)
class StationAqiEvaluation:
    """Station-level CPCB AQI evaluation prior to district aggregation."""

    station_id: str
    district_id: str
    evaluation_timestamp_utc: str
    evaluation_date: str  # YYYY-MM-DD in IST (UTC+05:30)
    source_provider: SourceProvider
    station_aqi: Optional[int]
    dominant_pollutant: Optional[PollutantKey]
    meets_16h_completeness: bool
    meets_3_pollutant_pm_rule: bool
    is_valid_station_aqi: bool
    valid_pollutant_count: int
    observed_pollutant_hours: int
    expected_pollutant_hours: int
    pollutant_evaluations: Dict[PollutantKey, StationPollutantEvaluation]
    rejection_reasons: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class DailyDistrictHistoryRecord:
    """Persisted daily district AQI summary row for 30-day historical trends and current snapshot."""

    district_id: str
    date: str  # YYYY-MM-DD in IST (UTC+05:30)
    district_aqi: Optional[int]
    dominant_pollutant: Optional[PollutantKey]
    updated_at_utc: Optional[str]
    source_attribution: str
    reporting_stations: int
    registered_stations: int
    observed_station_pollutant_hours: int
    expected_station_pollutant_hours: int
    coverage: float
    quality: QualityLevel
    pollutants_json: str
    caveats_json: str


@dataclass(frozen=True)
class SyncRunRecord:
    """Persisted ingestion sync execution record tracking per-provider status and errors."""

    id: int
    started_at_utc: str
    completed_at_utc: Optional[str]
    overall_status: str
    cpcb_status: str
    cpcb_observations_ingested: int
    cpcb_error: Optional[str]
    openaq_status: str
    openaq_observations_ingested: int
    openaq_error: Optional[str]
    daily_district_records_updated: int
    xkdr_status: str = "not_configured"
    xkdr_observations_ingested: int = 0
    xkdr_error: Optional[str] = None
