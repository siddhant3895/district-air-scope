"""XKDR Forum India Air Quality Database adapter (https://airquality.xkdr.org/).

Integrates the harmonized CPCB CAAQM + US Diplomatic Post hourly air quality dataset
maintained by XKDR Forum (released under Creative Commons Attribution 4.0 / CC BY 4.0
and Government Open Data License - India for underlying CPCB CAAQM data).

Key Upstream Characteristics Verified from XKDR API v1 Specification:
1. Authentication: HTTP Bearer token (`Authorization: Bearer <XKDR_API_KEY>`).
2. Endpoints:
   - `GET /v1/meta`: Dataset export timestamp, months covered, row counts, and tier limits.
   - `GET /v1/stations`: Station catalog (`station_id` e.g. `site_103`, `station_name` e.g.
     `"Mandir Marg, Delhi - DPCC"`, `city_name`, `state_name`, `source`, `latitude`,
     `longitude`, `first_seen`, `last_seen`).
   - `GET /v1/parameters`: Parameter metadata (`parameter_name`, `unit`, `n_stations`, `n_rows`).
     Canonical units for the 6 NAQI criteria pollutants in XKDR are:
     `PM2.5`, `PM10`, `NO2`, `SO2`, `Ozone` in `µg/m³`, and `CO` in `mg/m³`.
   - `GET /v1/measurements`: Hourly observations (`station_id`, `parameter_name`, `collected_at`
     or `period_start`, `value` or `mean`, `unit`) with timestamps in Indian Standard Time
     (IST, UTC+05:30).
3. Station-First Discipline:
   - Maps XKDR stations to DistrictWatch's audited `REVIEWED_STATIONS` registry.
   - Explicitly excludes stations located in neighboring administrative districts
     (`is_verified_member=False`).
   - Converts naive IST timestamps (`YYYY-MM-DD HH:MM:SS`) to UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SSZ`).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import io
import json
import math
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
import httpx

from app.districts_registry import (
    REVIEWED_STATIONS,
    TARGET_DISTRICTS,
    _normalize_name,
    resolve_reviewed_station,
)
from app.ingestion.http_utils import RateLimitedHttpClient
from app.models import RawStationObservation, StationRecord
from app.schemas import PollutantKey
from app.services.unit_normalizer import normalize_upstream_observation


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers between two WGS-84 coordinates."""
    r_earth = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2.0) ** 2
    return 2.0 * r_earth * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def _alnum_tokens(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.strip().lower()).strip()


XKDR_DEFAULT_BASE_URL = "https://airquality.xkdr.org"
XKDR_DEMO_KEY_PREFIX = "aqi_demo_"
XKDR_DEMO_LATEST_IST_DATE = "2024-12-31"

IST_OFFSET = timezone(timedelta(hours=5, minutes=30))

XKDR_POLLUTANT_MAP: Dict[str, PollutantKey] = {
    "PM2.5": "PM2.5",
    "PM25": "PM2.5",
    "PM10": "PM10",
    "NO2": "NO2",
    "SO2": "SO2",
    "OZONE": "O3",
    "O3": "O3",
    "CO": "CO",
}

XKDR_QUERY_PARAMETERS: Tuple[str, ...] = (
    "PM2.5",
    "PM10",
    "NO2",
    "SO2",
    "Ozone",
    "CO",
)

# Verified canonical units from XKDR /v1/parameters for the 6 NAQI criteria pollutants
XKDR_CANONICAL_PARAMETER_UNITS: Dict[PollutantKey, str] = {
    "PM2.5": "µg/m³",
    "PM10": "µg/m³",
    "NO2": "µg/m³",
    "SO2": "µg/m³",
    "O3": "µg/m³",
    "CO": "mg/m³",
}

# Explicit mapping for the 5 US Diplomatic Post monitors in XKDR (`source="us_embassy"`)
US_EMBASSY_NAME_TO_STATION_ID: Dict[str, str] = {
    "us diplomatic post: new delhi": "cpcb:new-delhi:mandir-marg",
    "us diplomatic post: mumbai": "cpcb:mumbai-suburban:bandra",
    "us diplomatic post: kolkata": "cpcb:kolkata:victoria",
    "us diplomatic post: chennai": "cpcb:chennai:alandur",
    "us diplomatic post: hyderabad": "cpcb:hyderabad:sanathnagar",
}


@dataclass(frozen=True)
class XkdrIngestionReport:
    accepted_observations: List[RawStationObservation]
    skipped_unmapped_station_count: int
    skipped_non_member_station_count: int
    skipped_unknown_pollutant_count: int
    invalid_value_count: int


def _xkdr_headers(api_key: str, accept: str = "application/json") -> Dict[str, str]:
    clean_key = (api_key or "").strip()
    if not clean_key:
        raise ValueError("XKDR_API_KEY is required to query the XKDR Air Quality API.")
    return {
        "Authorization": f"Bearer {clean_key}",
        "Accept": accept,
    }


def _extract_records_list(payload: Any, *candidate_keys: str) -> List[Dict[str, Any]]:
    """Extract a list of dicts from either a top-level JSON array or a wrapped JSON object."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        keys_to_check = candidate_keys or (
            "data",
            "results",
            "measurements",
            "records",
            "stations",
            "parameters",
        )
        for key in keys_to_check:
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
    return []


def _get_json_any(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout_sec: float = 45.0,
    http_client: Optional[RateLimitedHttpClient] = None,
) -> Any:
    if http_client is not None:
        return http_client.get_json(url, params=params, headers=headers)
    with httpx.Client(timeout=timeout_sec) as client:
        resp = client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        text = resp.text.lstrip()
        if text.startswith(("{", "[")):
            return resp.json()
        return parse_xkdr_csv_measurements(resp.text)


def fetch_xkdr_meta(
    api_key: str,
    *,
    base_url: str = XKDR_DEFAULT_BASE_URL,
    timeout_sec: float = 20.0,
    http_client: Optional[RateLimitedHttpClient] = None,
) -> Dict[str, Any]:
    """Fetch dataset metadata from `GET /v1/meta`."""
    url = f"{base_url.rstrip('/')}/v1/meta"
    res = _get_json_any(
        url,
        headers=_xkdr_headers(api_key),
        timeout_sec=timeout_sec,
        http_client=http_client,
    )
    return res if isinstance(res, dict) else {"data": res}


def fetch_xkdr_stations(
    api_key: str,
    *,
    base_url: str = XKDR_DEFAULT_BASE_URL,
    source: Optional[str] = None,
    state: Optional[str] = None,
    city: Optional[str] = None,
    timeout_sec: float = 25.0,
    http_client: Optional[RateLimitedHttpClient] = None,
) -> List[Dict[str, Any]]:
    """Fetch station directory from `GET /v1/stations`."""
    url = f"{base_url.rstrip('/')}/v1/stations"
    params: Dict[str, Any] = {"format": "json"}
    if source:
        params["source"] = source
    if state:
        params["state"] = state
    if city:
        params["city"] = city
    res = _get_json_any(
        url,
        params=params,
        headers=_xkdr_headers(api_key),
        timeout_sec=timeout_sec,
        http_client=http_client,
    )
    return _extract_records_list(res, "stations", "data", "results", "records")


def fetch_xkdr_parameters(
    api_key: str,
    *,
    base_url: str = XKDR_DEFAULT_BASE_URL,
    timeout_sec: float = 20.0,
    http_client: Optional[RateLimitedHttpClient] = None,
) -> Dict[PollutantKey, str]:
    """Fetch parameter units from `GET /v1/parameters` and map to canonical DistrictWatch PollutantKeys."""
    url = f"{base_url.rstrip('/')}/v1/parameters"
    res = _get_json_any(
        url,
        params={"format": "json"},
        headers=_xkdr_headers(api_key),
        timeout_sec=timeout_sec,
        http_client=http_client,
    )
    rows = _extract_records_list(res, "parameters", "data", "results", "records")
    unit_map: Dict[PollutantKey, str] = dict(XKDR_CANONICAL_PARAMETER_UNITS)
    for row in rows:
        raw_name = str(row.get("parameter_name") or row.get("parameter") or "").strip().upper()
        p_key = XKDR_POLLUTANT_MAP.get(raw_name)
        raw_unit = str(row.get("unit") or "").strip()
        if p_key is not None and raw_unit:
            unit_map[p_key] = raw_unit
    return unit_map


def fetch_xkdr_measurements(
    api_key: str,
    *,
    base_url: str = XKDR_DEFAULT_BASE_URL,
    stations: Optional[Sequence[str]] = None,
    parameters: Optional[Sequence[str]] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    start_date_ist: Optional[str] = None,
    end_date_ist: Optional[str] = None,
    agg: str = "hourly",
    limit: Optional[int] = None,
    timeout_sec: float = 45.0,
    http_client: Optional[RateLimitedHttpClient] = None,
) -> List[Dict[str, Any]]:
    """Fetch hourly measurements from `GET /v1/measurements` in JSON format."""
    url = f"{base_url.rstrip('/')}/v1/measurements"
    params: Dict[str, Any] = {
        "agg": agg,
        "format": "json",
    }
    if stations:
        station_list = [s.strip() for s in stations if s and s.strip()]
        if len(station_list) == 1:
            params["station"] = station_list[0]
        elif len(station_list) > 1:
            params["station"] = station_list
    if parameters:
        param_list = [p.strip() for p in parameters if p and p.strip()]
        if len(param_list) == 1:
            params["parameter"] = param_list[0]
        elif len(param_list) > 1:
            params["parameter"] = param_list
    if city:
        params["city"] = city
    if state:
        params["state"] = state
    if start_date_ist:
        params["start"] = start_date_ist
    if end_date_ist:
        params["end"] = end_date_ist
    if limit is not None:
        params["limit"] = int(limit)

    res = _get_json_any(
        url,
        params=params,
        headers=_xkdr_headers(api_key),
        timeout_sec=timeout_sec,
        http_client=http_client,
    )
    return _extract_records_list(res, "measurements", "data", "results", "records")


def parse_xkdr_csv_measurements(csv_text: str) -> List[Dict[str, Any]]:
    """Parse CSV output from `GET /v1/measurements?format=csv` into a list of row dicts."""
    if not csv_text or not csv_text.strip():
        return []
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    return [dict(row) for row in reader]


def parse_xkdr_ist_timestamp_to_utc(raw_timestamp: str) -> str:
    """Convert an XKDR timestamp (IST naive `YYYY-MM-DD HH:MM:SS` or ISO-8601) to UTC ISO-8601 (`...Z`)."""
    clean = str(raw_timestamp or "").strip()
    if not clean:
        raise ValueError("Empty timestamp in XKDR record.")

    # Handle trailing Z or explicit timezone offset
    if clean.endswith("Z") or "+" in clean[10:] or (clean.count("-") > 2 and "T" in clean):
        iso_candidate = clean.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST_OFFSET)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Try standard XKDR IST formats
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            naive_dt = datetime.strptime(clean, fmt)
            ist_dt = naive_dt.replace(tzinfo=IST_OFFSET)
            return ist_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue

    # Final fallback via fromisoformat treated as IST
    dt = datetime.fromisoformat(clean)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST_OFFSET)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_xkdr_station(
    station_meta: Mapping[str, Any],
    reviewed_stations: Sequence[StationRecord] = REVIEWED_STATIONS,
    max_coord_distance_km: float = 2.5,
) -> Optional[StationRecord]:
    """Resolve an XKDR station metadata record or measurement row to an audited StationRecord.

    Preserves `is_verified_member=False` records when an XKDR station matches a known
    neighboring-district station (e.g., Anand Vihar in East/Shahdara Delhi or Vashi in Navi Mumbai)
    so the caller never misassigns neighbor-district readings via coordinate fallback.
    """
    raw_name = str(
        station_meta.get("station_name")
        or station_meta.get("station")
        or station_meta.get("name")
        or ""
    ).strip()
    raw_id = str(station_meta.get("station_id") or "").strip()
    raw_city = str(station_meta.get("city_name") or station_meta.get("city") or "").strip()

    # 1. Exact or prefix match against CPCB station name in REVIEWED_STATIONS
    if raw_name:
        matched = resolve_reviewed_station(raw_name)
        if matched is not None:
            return matched

        norm_name = _normalize_name(raw_name)
        if norm_name in US_EMBASSY_NAME_TO_STATION_ID:
            target_id = US_EMBASSY_NAME_TO_STATION_ID[norm_name]
            for st in reviewed_stations:
                if st.station_id == target_id:
                    return st

        # Punctuation-insensitive match (e.g. "Sector-62, Noida - IMD" vs "Sector - 62, Noida - IMD")
        alnum_raw = _alnum_tokens(raw_name)
        for st in reviewed_stations:
            if _alnum_tokens(st.external_station_id) == alnum_raw:
                return st
            prefix_tokens = _alnum_tokens(f"{st.station_name} {st.city}")
            if prefix_tokens and alnum_raw.startswith(prefix_tokens):
                return st

        # Check if raw_name + raw_city matches a reviewed station
        if raw_city:
            combined = f"{raw_name}, {raw_city}"
            matched_combined = resolve_reviewed_station(combined)
            if matched_combined is not None:
                return matched_combined

    if raw_id:
        matched_id = resolve_reviewed_station(raw_id)
        if matched_id is not None:
            return matched_id
        for st in reviewed_stations:
            if st.station_id == raw_id:
                return st

    # 2. Coordinate proximity fallback (first strict 2.5 km including exclusions, then 15 km within same city)
    raw_lat = station_meta.get("latitude") if station_meta.get("latitude") is not None else station_meta.get("lat")
    raw_lon = station_meta.get("longitude") if station_meta.get("longitude") is not None else station_meta.get("lon")
    if raw_lat is not None and raw_lon is not None:
        try:
            lat_f = float(raw_lat)
            lon_f = float(raw_lon)
        except (TypeError, ValueError):
            lat_f = None
            lon_f = None

        if lat_f is not None and lon_f is not None:
            best_station: Optional[StationRecord] = None
            best_dist = max_coord_distance_km
            for st in reviewed_stations:
                if st.lat is None or st.lon is None:
                    continue
                dist = haversine_km(lat_f, lon_f, st.lat, st.lon)
                if dist <= best_dist:
                    best_dist = dist
                    best_station = st
            if best_station is not None:
                return best_station

            # Secondary same-city coordinate match (up to 15 km) for cities whose CPCB station moved or has offset coords
            if raw_city:
                city_alnum = _alnum_tokens(raw_city)
                best_city_st: Optional[StationRecord] = None
                best_city_dist = 15.0
                for st in reviewed_stations:
                    if not st.is_verified_member or st.lat is None or st.lon is None:
                        continue
                    if _alnum_tokens(st.city) == city_alnum:
                        dist = haversine_km(lat_f, lon_f, st.lat, st.lon)
                        if dist <= best_city_dist:
                            best_city_dist = dist
                            best_city_st = st
                if best_city_st is not None:
                    return best_city_st

    return None


def build_xkdr_station_catalog(
    stations_payload: Sequence[Mapping[str, Any]],
    reviewed_stations: Sequence[StationRecord] = REVIEWED_STATIONS,
) -> Tuple[Dict[str, StationRecord], Set[str], Optional[str]]:
    """Map XKDR station_id / station_name entries to verified StationRecords and extract latest archive date.

    Returns:
        (verified_station_map, excluded_station_keys, max_last_seen_ist_date)
    """
    verified_map: Dict[str, StationRecord] = {}
    excluded_keys: Set[str] = set()
    latest_date_str: Optional[str] = None

    for row in stations_payload:
        st = resolve_xkdr_station(row, reviewed_stations=reviewed_stations)
        if st is None:
            continue

        xkdr_id = str(row.get("station_id") or "").strip()
        xkdr_name = str(row.get("station_name") or row.get("name") or "").strip()

        if not st.is_verified_member:
            if xkdr_id:
                excluded_keys.add(_normalize_name(xkdr_id))
            if xkdr_name:
                excluded_keys.add(_normalize_name(xkdr_name))
            continue

        if xkdr_id:
            verified_map[_normalize_name(xkdr_id)] = st
        if xkdr_name:
            verified_map[_normalize_name(xkdr_name)] = st

        last_seen_raw = str(row.get("last_seen") or "").strip()
        if len(last_seen_raw) >= 10:
            candidate_date = last_seen_raw[:10]
            try:
                date.fromisoformat(candidate_date)
                if latest_date_str is None or candidate_date > latest_date_str:
                    latest_date_str = candidate_date
            except ValueError:
                pass

    return verified_map, excluded_keys, latest_date_str


def resolve_xkdr_sync_window(
    *,
    api_key: str,
    reference_now_utc: datetime,
    backfill_days: int,
    catalog_latest_ist_date: Optional[str] = None,
    meta_payload: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, str]:
    """Determine `(start_date_ist, end_date_ist)` (`YYYY-MM-DD`) for XKDR `/v1/measurements` queries."""
    ref_ist_date = reference_now_utc.astimezone(IST_OFFSET).date()
    effective_end_date = ref_ist_date

    archive_end_candidates: List[date] = []
    if catalog_latest_ist_date:
        try:
            archive_end_candidates.append(date.fromisoformat(catalog_latest_ist_date[:10]))
        except ValueError:
            pass

    if meta_payload and isinstance(meta_payload, Mapping):
        months = meta_payload.get("months")
        if isinstance(months, list) and months:
            last_month = str(sorted(str(m) for m in months)[-1]).strip()
            if len(last_month) == 7 and "-" in last_month:
                try:
                    yr, mo = (int(x) for x in last_month.split("-", 1))
                    if mo == 12:
                        last_day = date(yr, 12, 31)
                    else:
                        last_day = date(yr, mo + 1, 1) - timedelta(days=1)
                    archive_end_candidates.append(last_day)
                except ValueError:
                    pass

    if archive_end_candidates:
        latest_archive = max(archive_end_candidates)
        if latest_archive < effective_end_date:
            effective_end_date = latest_archive

    if (api_key or "").strip().startswith(XKDR_DEMO_KEY_PREFIX):
        demo_cap = date.fromisoformat(XKDR_DEMO_LATEST_IST_DATE)
        if effective_end_date > demo_cap:
            effective_end_date = demo_cap

    days_span = max(1, int(backfill_days))
    start_date = effective_end_date - timedelta(days=days_span - 1)
    return start_date.isoformat(), effective_end_date.isoformat()


def parse_xkdr_measurements_payload(
    payload: Any,
    *,
    verified_station_map: Optional[Mapping[str, StationRecord]] = None,
    excluded_station_keys: Optional[Set[str]] = None,
    parameter_units: Optional[Mapping[PollutantKey, str]] = None,
    ingested_at_utc: Optional[str] = None,
) -> XkdrIngestionReport:
    """Parse XKDR `/v1/measurements` records (JSON list/dict or CSV rows) into `RawStationObservation` objects."""
    rows = _extract_records_list(payload, "measurements", "data", "results", "records")
    ingested_iso = ingested_at_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    v_map = dict(verified_station_map) if verified_station_map else {}
    excl_set = set(excluded_station_keys) if excluded_station_keys else set()
    unit_lookup = dict(XKDR_CANONICAL_PARAMETER_UNITS)
    if parameter_units:
        unit_lookup.update(parameter_units)

    accepted: List[RawStationObservation] = []
    skipped_unmapped = 0
    skipped_non_member = 0
    skipped_pollutant = 0
    invalid_values = 0

    for rec in rows:
        raw_st_id = str(rec.get("station_id") or "").strip()
        raw_st_name = str(rec.get("station_name") or rec.get("station") or "").strip()
        norm_id = _normalize_name(raw_st_id) if raw_st_id else ""
        norm_name = _normalize_name(raw_st_name) if raw_st_name else ""

        if (norm_id and norm_id in excl_set) or (norm_name and norm_name in excl_set):
            skipped_non_member += 1
            continue

        station: Optional[StationRecord] = None
        if norm_id and norm_id in v_map:
            station = v_map[norm_id]
        elif norm_name and norm_name in v_map:
            station = v_map[norm_name]
        else:
            station = resolve_xkdr_station(rec)

        if station is None:
            skipped_unmapped += 1
            continue

        if not station.is_verified_member:
            skipped_non_member += 1
            continue

        raw_param = str(
            rec.get("parameter_name")
            or rec.get("parameter")
            or rec.get("pollutant_id")
            or ""
        ).strip().upper()
        pollutant_key = XKDR_POLLUTANT_MAP.get(raw_param)
        if pollutant_key is None:
            skipped_pollutant += 1
            continue

        raw_ts = (
            rec.get("collected_at")
            or rec.get("period_start")
            or rec.get("timestamp")
            or rec.get("datetime")
            or rec.get("last_update")
            or ""
        )
        try:
            observed_utc = parse_xkdr_ist_timestamp_to_utc(str(raw_ts))
        except ValueError:
            invalid_values += 1
            continue

        # Support both raw (`value`) and hourly aggregated (`mean`) XKDR measurement schemas
        raw_val_obj = rec.get("value") if "value" in rec and rec.get("value") is not None else rec.get("mean")
        raw_val_text = "" if raw_val_obj is None else str(raw_val_obj)

        # If record explicitly supplies a unit field (even "NA"), honor it; otherwise use parameter's canonical unit
        if "unit" in rec and rec.get("unit") is not None and str(rec.get("unit")).strip() != "":
            raw_unit: Optional[str] = str(rec.get("unit")).strip()
        else:
            raw_unit = unit_lookup.get(pollutant_key)

        norm_res = normalize_upstream_observation(
            source_provider="xkdr",
            pollutant_key=pollutant_key,
            raw_value_text=raw_val_text,
            raw_unit=raw_unit,
        )
        if not norm_res.is_valid:
            invalid_values += 1

        upstream_id = f"xkdr:{raw_st_id or station.station_id}:{pollutant_key}:{observed_utc}"
        accepted.append(
            RawStationObservation(
                station_id=station.station_id,
                source_provider="xkdr",
                upstream_sensor_or_record_id=upstream_id,
                pollutant_key=pollutant_key,
                observed_at_utc=observed_utc,
                window_hours=1,
                raw_value_text=raw_val_text,
                raw_unit=raw_unit,
                value_kind=norm_res.value_kind,
                normalized_concentration=norm_res.normalized_concentration,
                normalized_unit=norm_res.normalized_unit,
                calculated_sub_index=norm_res.calculated_sub_index,
                is_valid=norm_res.is_valid,
                validation_note=norm_res.validation_note,
                raw_payload_json=json.dumps(rec, sort_keys=True),
                ingested_at_utc=ingested_iso,
            )
        )

    return XkdrIngestionReport(
        accepted_observations=accepted,
        skipped_unmapped_station_count=skipped_unmapped,
        skipped_non_member_station_count=skipped_non_member,
        skipped_unknown_pollutant_count=skipped_pollutant,
        invalid_value_count=invalid_values,
    )
