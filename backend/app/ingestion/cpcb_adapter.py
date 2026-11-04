"""Independent CPCB Open Government Data (data.gov.in) adapter.

Resource ID: 3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69
License: Government Open Data License - India (GODL-India)

Data Integrity Note on CPCB OGD `pollutant_avg`:
Official `data.gov.in` records for resource `3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69`
populate `pollutant_unit` as literal `'NA'` (or omit it) without specifying in the
schema whether `pollutant_avg` is a raw concentration or a pre-calculated CPCB sub-index.
To prevent silent AQI corruption, `parse_cpcb_ogd_payload` delegates to
`normalize_upstream_observation`, which marks unitless/'NA'-unit records as
`value_kind='unverified_scale'` and `is_valid=False` while retaining the raw observation
in SQLite for auditability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, List, Optional
from app.config import get_settings
from app.districts_registry import resolve_reviewed_station
from app.ingestion.http_utils import RateLimitedHttpClient
from app.models import RawStationObservation
from app.schemas import PollutantKey
from app.services.unit_normalizer import normalize_upstream_observation

CPCB_OGD_RESOURCE_URL = (
    "https://api.data.gov.in/resource/3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
)

IST_TZ = timezone(timedelta(hours=5, minutes=30))

CPCB_POLLUTANT_MAP: Dict[str, PollutantKey] = {
    "PM2.5": "PM2.5",
    "PM25": "PM2.5",
    "PM10": "PM10",
    "NO2": "NO2",
    "SO2": "SO2",
    "OZONE": "O3",
    "O3": "O3",
    "CO": "CO",
}

CPCB_WINDOW_HOURS: Dict[PollutantKey, int] = {
    "PM2.5": 24,
    "PM10": 24,
    "NO2": 24,
    "SO2": 24,
    "O3": 8,
    "CO": 8,
}


@dataclass
class CpcbParseAuditReport:
    total_records_seen: int = 0
    accepted_observations: List[RawStationObservation] = field(default_factory=list)
    excluded_non_member_stations: List[str] = field(default_factory=list)
    unmapped_stations: List[str] = field(default_factory=list)
    ignored_pollutants: List[str] = field(default_factory=list)
    unverified_unitless_count: int = 0
    invalid_or_missing_count: int = 0


def parse_cpcb_timestamp_to_utc(last_update_str: str) -> Optional[str]:
    """Parse CPCB 'DD-MM-YYYY HH:MM:SS' (or 'YYYY-MM-DD HH:MM:SS') IST timestamp into ISO-8601 UTC."""
    text = (last_update_str or "").strip()
    if not text:
        return None
    for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            local_dt = datetime.strptime(text, fmt).replace(tzinfo=IST_TZ)
            utc_dt = local_dt.astimezone(timezone.utc)
            return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return None


def parse_cpcb_ogd_payload(
    payload: Dict[str, Any],
    ingested_at_utc: Optional[str] = None,
) -> CpcbParseAuditReport:
    """Parse a raw data.gov.in CPCB response payload into audited RawStationObservation records."""
    report = CpcbParseAuditReport()
    ingested_iso = ingested_at_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = payload.get("records", [])
    if not isinstance(records, list):
        return report

    for row in records:
        if not isinstance(row, dict):
            continue
        report.total_records_seen += 1

        station_name_raw = str(row.get("station") or "").strip()
        if not station_name_raw:
            continue

        reviewed_station = resolve_reviewed_station(station_name_raw)
        if reviewed_station is None:
            if station_name_raw not in report.unmapped_stations:
                report.unmapped_stations.append(station_name_raw)
            continue

        if not reviewed_station.is_verified_member:
            if station_name_raw not in report.excluded_non_member_stations:
                report.excluded_non_member_stations.append(station_name_raw)
            continue

        raw_pollutant_id = str(row.get("pollutant_id") or "").strip().upper()
        pollutant_key = CPCB_POLLUTANT_MAP.get(raw_pollutant_id)
        if pollutant_key is None:
            if raw_pollutant_id and raw_pollutant_id not in report.ignored_pollutants:
                report.ignored_pollutants.append(raw_pollutant_id)
            continue

        observed_at_utc = parse_cpcb_timestamp_to_utc(str(row.get("last_update") or ""))
        if observed_at_utc is None:
            report.invalid_or_missing_count += 1
            continue

        raw_value_text = str(row.get("pollutant_avg") if row.get("pollutant_avg") is not None else "NA")
        raw_unit = row.get("pollutant_unit") or row.get("unit")
        raw_unit_str = str(raw_unit).strip() if raw_unit is not None else None
        explicit_kind = row.get("value_kind")

        norm = normalize_upstream_observation(
            source_provider="cpcb_ogd",
            pollutant_key=pollutant_key,
            raw_value_text=raw_value_text,
            raw_unit=raw_unit_str,
            explicit_value_kind=str(explicit_kind) if explicit_kind else None,
        )

        if norm.value_kind == "invalid" and (raw_unit_str is None or raw_unit_str.upper() in {"NA", "N/A", ""}):
            report.unverified_unitless_count += 1
        elif not norm.is_valid:
            report.invalid_or_missing_count += 1

        window_hours = int(row.get("window_hours") or CPCB_WINDOW_HOURS[pollutant_key])
        upstream_id = str(
            row.get("id") or f"{reviewed_station.external_station_id}:{pollutant_key}:{observed_at_utc}"
        )

        obs = RawStationObservation(
            station_id=reviewed_station.station_id,
            source_provider="cpcb_ogd",
            upstream_sensor_or_record_id=upstream_id,
            pollutant_key=pollutant_key,
            observed_at_utc=observed_at_utc,
            window_hours=window_hours,
            raw_value_text=raw_value_text,
            raw_unit=raw_unit_str,
            value_kind=norm.value_kind,
            normalized_concentration=norm.normalized_concentration,
            normalized_unit=norm.normalized_unit,
            calculated_sub_index=norm.calculated_sub_index,
            is_valid=norm.is_valid,
            validation_note=norm.validation_note,
            raw_payload_json=json.dumps(row, sort_keys=True),
            ingested_at_utc=ingested_iso,
        )
        report.accepted_observations.append(obs)

    return report


def fetch_cpcb_ogd_page(
    api_key: str,
    *,
    state: Optional[str] = None,
    city: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
    timeout_sec: float = 20.0,
    http_client: Optional[RateLimitedHttpClient] = None,
) -> Dict[str, Any]:
    """Fetch a single paginated response from data.gov.in CPCB resource with rate-limiting and retry."""
    if not api_key or not api_key.strip():
        raise ValueError("DATA_GOV_IN_API_KEY is required to fetch live CPCB data.")
    settings = get_settings()
    client = http_client or RateLimitedHttpClient(
        min_interval_sec=settings.min_request_interval_sec,
        max_retries=settings.max_http_retries,
        timeout_sec=timeout_sec,
    )
    params: Dict[str, Any] = {
        "api-key": api_key.strip(),
        "format": "json",
        "limit": min(1000, max(1, limit)),
        "offset": max(0, offset),
    }
    if state:
        params["filters[state]"] = state
    if city:
        params["filters[city]"] = city

    return client.get_json(CPCB_OGD_RESOURCE_URL, params=params)
