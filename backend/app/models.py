"""Persistence models (dataclasses) representing SQLite rows for auditability and type safety."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional
from app.schemas import PollutantKey, QualityLevel

SourceProvider = Literal["cpcb_ogd", "openaq_v3"]
ValueKind = Literal["concentration", "sub_index", "missing", "invalid"]


@dataclass(frozen=True)
class StationRecord:
    station_id: str
    district_id: str
    source_provider: SourceProvider
    external_station_id: str
    station_name: str
    operating_agency: str
    city: str
    state: str
    lat: Optional[float]
    lon: Optional[float]
    expected_pollutants: List[PollutantKey]
    is_verified_member: bool
    membership_basis: str
    openaq_location_id: Optional[int] = None
    openaq_verified_available: bool = False


@dataclass(frozen=True)
class RawStationObservation:
    station_id: str
    source_provider: SourceProvider
    upstream_sensor_or_record_id: Optional[str]
    pollutant_key: PollutantKey
    observed_at_utc: str
    window_hours: int
    raw_value_text: str
    raw_unit: Optional[str]
    value_kind: ValueKind
    normalized_concentration: Optional[float]
    normalized_unit: Optional[str]
    calculated_sub_index: Optional[int]
    is_valid: bool
    validation_note: Optional[str]
    raw_payload_json: str
    ingested_at_utc: str


@dataclass(frozen=True)
class StationPollutantEvaluation:
    pollutant_key: PollutantKey
    window_hours: int
    required_valid_hours: int
    observed_valid_hours: int
    expected_hours: int
    meets_completeness: bool
    mean_concentration: Optional[float]
    unit: str
    sub_index: Optional[int]
    value_kind_used: ValueKind


@dataclass(frozen=True)
class StationAqiEvaluation:
    station_id: str
    district_id: str
    evaluation_timestamp_utc: str
    evaluation_date: str
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
    district_id: str
    date: str
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
