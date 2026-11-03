"""Independent OpenAQ v3 adapter for sensor discovery, availability validation, and hourly history ingestion."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional
import httpx
from app.models import RawStationObservation
from app.schemas import PollutantKey
from app.services.unit_normalizer import normalize_upstream_observation

OPENAQ_V3_BASE_URL = "https://api.openaq.org/v3"

OPENAQ_PARAMETER_MAP: Dict[str, PollutantKey] = {
    "pm25": "PM2.5",
    "pm2.5": "PM2.5",
    "pm10": "PM10",
    "no2": "NO2",
    "so2": "SO2",
    "o3": "O3",
    "ozone": "O3",
    "co": "CO",
}

SUPPORTED_OPENAQ_UNITS = {"µg/m³", "ug/m3", "ug/m³", "μg/m³", "mg/m³", "mg/m3", "ppm", "ppb"}


@dataclass(frozen=True)
class ValidatedOpenAqSensor:
    sensor_id: int
    pollutant_key: PollutantKey
    raw_unit: str


@dataclass(frozen=True)
class OpenAqLocationValidationResult:
    location_id: int
    name: str
    is_available: bool
    rejection_reason: Optional[str]
    valid_sensors: Dict[PollutantKey, ValidatedOpenAqSensor] = field(default_factory=dict)
    license_attribution: str = "OpenAQ v3"


def validate_openaq_location(
    location_obj: Dict[str, Any],
    *,
    reference_now_utc: Optional[datetime] = None,
    max_inactive_days: int = 7,
) -> OpenAqLocationValidationResult:
    """Validate an OpenAQ v3 location object for reference-grade stationary status, recency, and sensor units."""
    loc_id = int(location_obj.get("id") or 0)
    name = str(location_obj.get("name") or f"Location {loc_id}")

    if not location_obj.get("isMonitor", False):
        return OpenAqLocationValidationResult(
            location_id=loc_id,
            name=name,
            is_available=False,
            rejection_reason="Rejected: location is not a reference-grade monitor (isMonitor=False).",
        )

    if location_obj.get("isMobile", False):
        return OpenAqLocationValidationResult(
            location_id=loc_id,
            name=name,
            is_available=False,
            rejection_reason="Rejected: mobile sensors are excluded (isMobile=True).",
        )

    dt_last_obj = location_obj.get("datetimeLast") or {}
    dt_last_utc_str = dt_last_obj.get("utc") if isinstance(dt_last_obj, dict) else None
    if not dt_last_utc_str:
        return OpenAqLocationValidationResult(
            location_id=loc_id,
            name=name,
            is_available=False,
            rejection_reason="Rejected: missing datetimeLast.utc timestamp.",
        )

    ref_now = reference_now_utc or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)

    try:
        dt_last = datetime.fromisoformat(str(dt_last_utc_str).replace("Z", "+00:00"))
    except ValueError:
        return OpenAqLocationValidationResult(
            location_id=loc_id,
            name=name,
            is_available=False,
            rejection_reason=f"Rejected: malformed datetimeLast.utc '{dt_last_utc_str}'.",
        )

    age_days = (ref_now - dt_last).total_seconds() / 86400.0
    if age_days > max_inactive_days:
        return OpenAqLocationValidationResult(
            location_id=loc_id,
            name=name,
            is_available=False,
            rejection_reason=(
                f"Rejected: station inactive for {age_days:.1f} days "
                f"(exceeds {max_inactive_days}-day availability threshold)."
            ),
        )

    valid_sensors: Dict[PollutantKey, ValidatedOpenAqSensor] = {}
    for sensor in location_obj.get("sensors") or []:
        if not isinstance(sensor, dict):
            continue
        sensor_id = sensor.get("id")
        param = sensor.get("parameter") or {}
        param_name = str(param.get("name") or "").strip().lower()
        param_unit = str(param.get("units") or "").strip()

        pollutant_key = OPENAQ_PARAMETER_MAP.get(param_name)
        if sensor_id is None or pollutant_key is None:
            continue
        if param_unit.lower() not in {u.lower() for u in SUPPORTED_OPENAQ_UNITS}:
            continue

        valid_sensors[pollutant_key] = ValidatedOpenAqSensor(
            sensor_id=int(sensor_id),
            pollutant_key=pollutant_key,
            raw_unit=param_unit,
        )

    has_pm = "PM2.5" in valid_sensors or "PM10" in valid_sensors
    if len(valid_sensors) < 3 or not has_pm:
        return OpenAqLocationValidationResult(
            location_id=loc_id,
            name=name,
            is_available=False,
            rejection_reason=(
                f"Rejected: location only exposes {len(valid_sensors)} valid pollutant sensors "
                f"(requires >= 3 pollutants including PM2.5 or PM10)."
            ),
            valid_sensors=valid_sensors,
        )

    # Extract provider & license attribution
    provider_name = (location_obj.get("provider") or {}).get("name") or "OpenAQ"
    owner_name = (location_obj.get("owner") or {}).get("name") or ""
    licenses = location_obj.get("licenses") or []
    license_name = licenses[0].get("name") if licenses and isinstance(licenses[0], dict) else "Open Data"
    attribution = f"{provider_name} ({owner_name}) via OpenAQ v3 [{license_name}]".replace(" ()", "")

    return OpenAqLocationValidationResult(
        location_id=loc_id,
        name=name,
        is_available=True,
        rejection_reason=None,
        valid_sensors=valid_sensors,
        license_attribution=attribution,
    )


def parse_openaq_hourly_payload(
    *,
    station_id: str,
    sensor_id: int,
    expected_pollutant: PollutantKey,
    payload: Dict[str, Any],
    ingested_at_utc: Optional[str] = None,
) -> List[RawStationObservation]:
    """Parse an OpenAQ v3 `/v3/sensors/{id}/hours` response into hourly RawStationObservation records.

    Enforces coverage and unit validation on every hourly observation.
    """
    ingested_iso = ingested_at_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    results = payload.get("results", [])
    if not isinstance(results, list):
        return []

    observations: List[RawStationObservation] = []
    for item in results:
        if not isinstance(item, dict):
            continue

        # Check coverage block on aggregated hourly record
        coverage_obj = item.get("coverage")
        if isinstance(coverage_obj, dict):
            observed_count = int(coverage_obj.get("observedCount") or 0)
            pct_coverage = float(coverage_obj.get("percentCoverage") or 0.0)
            if observed_count <= 0 or pct_coverage <= 0.0:
                continue

        period = item.get("period") or {}
        dt_to = (period.get("datetimeTo") or {}).get("utc")
        dt_from = (period.get("datetimeFrom") or {}).get("utc")
        observed_at_utc = str(dt_to or dt_from or "").strip()
        if not observed_at_utc:
            continue

        param = item.get("parameter") or {}
        raw_unit = str(param.get("units") or "").strip() or None
        raw_val = item.get("value")
        raw_val_text = "NA" if raw_val is None else str(raw_val)

        norm = normalize_upstream_observation(
            source_provider="openaq_v3",
            pollutant_key=expected_pollutant,
            raw_value_text=raw_val_text,
            raw_unit=raw_unit,
            explicit_value_kind="concentration",
        )

        obs = RawStationObservation(
            station_id=station_id,
            source_provider="openaq_v3",
            upstream_sensor_or_record_id=f"openaq_sensor:{sensor_id}:{observed_at_utc}",
            pollutant_key=expected_pollutant,
            observed_at_utc=observed_at_utc,
            window_hours=1,
            raw_value_text=raw_val_text,
            raw_unit=raw_unit,
            value_kind=norm.value_kind,
            normalized_concentration=norm.normalized_concentration,
            normalized_unit=norm.normalized_unit,
            calculated_sub_index=norm.calculated_sub_index,
            is_valid=norm.is_valid,
            validation_note=norm.validation_note,
            raw_payload_json=json.dumps(item, sort_keys=True),
            ingested_at_utc=ingested_iso,
        )
        observations.append(obs)

    return observations


def fetch_openaq_location(api_key: str, location_id: int, timeout_sec: float = 20.0) -> Dict[str, Any]:
    """Fetch location metadata from OpenAQ v3."""
    if not api_key or not api_key.strip():
        raise ValueError("OPENAQ_API_KEY is required to query OpenAQ v3.")
    headers = {"X-API-Key": api_key.strip(), "Accept": "application/json"}
    with httpx.Client(timeout=timeout_sec) as client:
        resp = client.get(f"{OPENAQ_V3_BASE_URL}/locations/{location_id}", headers=headers)
        resp.raise_for_status()
        return resp.json()


def fetch_openaq_sensor_hours(
    api_key: str,
    sensor_id: int,
    *,
    datetime_from_utc: str,
    datetime_to_utc: str,
    limit: int = 1000,
    timeout_sec: float = 25.0,
) -> Dict[str, Any]:
    """Fetch hourly aggregated sensor observations from OpenAQ v3."""
    if not api_key or not api_key.strip():
        raise ValueError("OPENAQ_API_KEY is required to query OpenAQ v3.")
    headers = {"X-API-Key": api_key.strip(), "Accept": "application/json"}
    params = {
        "datetime_from": datetime_from_utc,
        "datetime_to": datetime_to_utc,
        "limit": min(1000, max(1, limit)),
    }
    with httpx.Client(timeout=timeout_sec) as client:
        resp = client.get(
            f"{OPENAQ_V3_BASE_URL}/sensors/{sensor_id}/hours",
            headers=headers,
            params=params,
        )
        resp.raise_for_status()
        return resp.json()


def discover_openaq_locations_near_coordinates(
    api_key: str,
    *,
    lat: float,
    lon: float,
    radius_meters: int = 2500,
    limit: int = 10,
    timeout_sec: float = 20.0,
) -> Dict[str, Any]:
    """Discover OpenAQ v3 reference locations within `radius_meters` of a verified station's coordinates."""
    if not api_key or not api_key.strip():
        raise ValueError("OPENAQ_API_KEY is required to query OpenAQ v3.")
    headers = {"X-API-Key": api_key.strip(), "Accept": "application/json"}
    params: Dict[str, Any] = {
        "coordinates": f"{lat:.4f},{lon:.4f}",
        "radius": min(25000, max(100, radius_meters)),
        "iso": "IN",
        "limit": min(100, max(1, limit)),
    }
    with httpx.Client(timeout=timeout_sec) as client:
        resp = client.get(f"{OPENAQ_V3_BASE_URL}/locations", headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()
