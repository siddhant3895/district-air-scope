"""Station-first CPCB AQI evaluator and DistrictWatch composite district aggregator.

Enforces:
1. Averaging windows: 24 hours for PM2.5, PM10, NO2, SO2; 8 hours for CO and O3.
2. Minimum 16 valid hourly observations out of 24 hours per pollutant at a station
   (and >= 6 valid hours within the 8-hour window for CO and O3).
3. Minimum 3 valid pollutants including at least one of PM2.5 or PM10 for a valid station AQI.
4. Station-level sub-indices and station AQIs are computed FIRST before aggregating to the
   DistrictWatch composite district metric.
5. Calculation provenance (`source_provider` and `source_attribution`) is derived strictly
   from the actual valid observations that contributed to the evaluation, and evaluation
   dates follow Indian Standard Time (`IST`, UTC+05:30) day boundaries.
6. Coverage is defined across each pollutant's CPCB averaging window:
   (4 * 24h) + (2 * 8h) = 112 expected pollutant-hours per 6-pollutant station.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from typing import Dict, List, Optional, Sequence, Set, Tuple
from app.models import (
    DailyDistrictHistoryRecord,
    RawStationObservation,
    SourceProvider,
    StationAqiEvaluation,
    StationPollutantEvaluation,
    StationRecord,
)
from app.schemas import District, DistrictSnapshot, Pollutant, PollutantKey, Reading
from app.services.naqi import POLLUTANT_SPECS, calculate_sub_index
from app.services.quality import build_caveats, classify_quality, has_pm_inversion_anomaly

IST_TZ = timezone(timedelta(hours=5, minutes=30))

# Averaging windows (in hours) per pollutant under CPCB NAQI rules
AVERAGING_WINDOW_HOURS: Dict[PollutantKey, int] = {
    "PM2.5": 24,
    "PM10": 24,
    "NO2": 24,
    "SO2": 24,
    "O3": 8,
    "CO": 8,
}

MIN_VALID_HOURS_24H = 16
MIN_VALID_HOURS_8H_BLOCK = 6
EXPECTED_HOURS_PER_DAY = 24

DISTRICT_METHODOLOGY_CAVEAT = (
    "DistrictWatch composite metric (mean of station-level CPCB AQIs across verified district stations) "
    "— not an official CPCB district-level index."
)


def _parse_utc(iso_str: str) -> datetime:
    clean = iso_str.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(clean)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_ist_date(dt_utc: datetime) -> str:
    """Convert a UTC datetime to its Indian Standard Time (UTC+05:30) calendar date string (YYYY-MM-DD)."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(IST_TZ).strftime("%Y-%m-%d")


def ist_date_bounds_in_utc(ist_date_str: str) -> Tuple[datetime, datetime]:
    """Return (start_utc, end_utc) corresponding to 00:00:00+05:30 .. 23:59:59+05:30 on `ist_date_str`."""
    d = date.fromisoformat(ist_date_str)
    start_ist = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=IST_TZ)
    end_ist = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=IST_TZ)
    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


def resolve_source_provider_label(providers: Set[str], fallback: SourceProvider = "none") -> SourceProvider:
    """Determine canonical SourceProvider from the set of contributing observation providers."""
    clean = {p for p in providers if p in ("cpcb_ogd", "openaq_v3")}
    if clean == {"openaq_v3"}:
        return "openaq_v3"
    if clean == {"cpcb_ogd"}:
        return "cpcb_ogd"
    if len(clean) > 1:
        return "hybrid_cpcb_openaq"
    return fallback


def format_district_source_attribution(providers: Set[str]) -> str:
    """Format human-readable source attribution string from contributing station providers."""
    has_cpcb = "cpcb_ogd" in providers or "hybrid_cpcb_openaq" in providers
    has_openaq = "openaq_v3" in providers or "hybrid_cpcb_openaq" in providers
    if has_cpcb and has_openaq:
        return "CPCB / SPCB CAAQMS via OpenAQ v3 & data.gov.in (GODL-India)"
    if has_openaq:
        return "CPCB / SPCB CAAQMS via OpenAQ v3"
    if has_cpcb:
        return "CPCB / SPCB CAAQMS via data.gov.in (GODL-India)"
    return "No verified station readings met completeness in window"


def evaluate_station_window(
    *,
    station: StationRecord,
    observations: Sequence[RawStationObservation],
    evaluation_end_utc: datetime,
    source_provider: Optional[SourceProvider] = None,
) -> StationAqiEvaluation:
    """Evaluate a single station over the 24-hour period ending at evaluation_end_utc.

    - Filters to valid hourly observations (`window_hours == 1` and `is_valid == True`)
      within `(evaluation_end_utc - 24h, evaluation_end_utc]`.
    - Requires >= 16 unique valid hours in the 24-hour window for each pollutant.
    - Applies a 24-hour mean for PM2.5, PM10, NO2, SO2 and an 8-hour window mean
      (requiring >= 6 valid hours in the latest 8-hour window AND >= 16 valid hours
      across the 24-hour day) for CO and O3.
    - Derives `source_provider` from the actual observations that contributed to valid pollutants.
    """
    if evaluation_end_utc.tzinfo is None:
        evaluation_end_utc = evaluation_end_utc.replace(tzinfo=timezone.utc)
    else:
        evaluation_end_utc = evaluation_end_utc.astimezone(timezone.utc)

    window_24h_start = evaluation_end_utc - timedelta(hours=24)
    window_8h_start = evaluation_end_utc - timedelta(hours=8)

    by_pollutant: Dict[PollutantKey, Dict[str, RawStationObservation]] = {
        k: {} for k, _ in POLLUTANT_SPECS
    }
    for obs in observations:
        if obs.station_id != station.station_id or not obs.is_valid:
            continue
        # Accept 1-hour observations (OpenAQ) as well as validated hourly CPCB OGD station polls
        # (each distinct hourly poll timestamp occupies at most 1 hour bucket in `by_pollutant`,
        # so 16+ distinct hourly polls in a 24h window are still strictly required to pass completeness).
        if obs.window_hours not in (1, 8, 24):
            continue
        dt = _parse_utc(obs.observed_at_utc)
        if not (window_24h_start < dt <= evaluation_end_utc):
            continue
        hour_bucket = dt.strftime("%Y-%m-%dT%H:00:00Z")
        # Prefer 1-hour concentration observations if both exist for the same hour bucket
        existing = by_pollutant[obs.pollutant_key].get(hour_bucket)
        if existing is None or (existing.window_hours != 1 and obs.window_hours == 1):
            by_pollutant[obs.pollutant_key][hour_bucket] = obs

    pollutant_evals: Dict[PollutantKey, StationPollutantEvaluation] = {}
    observed_total_hours = 0
    expected_total_hours = sum(AVERAGING_WINDOW_HOURS[p] for p in station.expected_pollutants)
    rejection_reasons: List[str] = []
    contributing_providers: Set[str] = set()

    for pollutant_key, unit in POLLUTANT_SPECS:
        hourly_map = by_pollutant.get(pollutant_key, {})
        valid_24h_obs = list(hourly_map.values())
        observed_24h_count = len(valid_24h_obs)
        avg_window = AVERAGING_WINDOW_HOURS[pollutant_key]

        if avg_window == 8:
            window_obs_for_coverage = [
                o for o in valid_24h_obs if _parse_utc(o.observed_at_utc) > window_8h_start
            ]
            observed_window_count = min(avg_window, len(window_obs_for_coverage))
        else:
            observed_window_count = min(avg_window, observed_24h_count)

        if pollutant_key in station.expected_pollutants:
            observed_total_hours += observed_window_count

        if observed_24h_count < MIN_VALID_HOURS_24H:
            pollutant_evals[pollutant_key] = StationPollutantEvaluation(
                pollutant_key=pollutant_key,
                window_hours=avg_window,
                required_valid_hours=MIN_VALID_HOURS_24H,
                observed_valid_hours=observed_window_count,
                expected_hours=avg_window,
                meets_completeness=False,
                mean_concentration=None,
                unit=unit,
                sub_index=None,
                value_kind_used="missing",
            )
            continue

        if avg_window == 8:
            window_obs = [
                o for o in valid_24h_obs if _parse_utc(o.observed_at_utc) > window_8h_start
            ]
            if len(window_obs) < MIN_VALID_HOURS_8H_BLOCK:
                pollutant_evals[pollutant_key] = StationPollutantEvaluation(
                    pollutant_key=pollutant_key,
                    window_hours=8,
                    required_valid_hours=MIN_VALID_HOURS_8H_BLOCK,
                    observed_valid_hours=observed_window_count,
                    expected_hours=avg_window,
                    meets_completeness=False,
                    mean_concentration=None,
                    unit=unit,
                    sub_index=None,
                    value_kind_used="missing",
                )
                continue
        else:
            window_obs = valid_24h_obs

        conc_values = [
            o.normalized_concentration
            for o in window_obs
            if o.value_kind == "concentration" and o.normalized_concentration is not None
        ]
        sub_index_values = [
            o.calculated_sub_index
            for o in window_obs
            if o.calculated_sub_index is not None
        ]

        if len(conc_values) == len(window_obs) and len(conc_values) > 0:
            for o in window_obs:
                contributing_providers.add(o.source_provider)
            mean_conc = sum(conc_values) / len(conc_values)
            sub_idx = calculate_sub_index(pollutant_key, mean_conc)
            rounded_conc = round(mean_conc, 2 if pollutant_key == "CO" else 1)
            pollutant_evals[pollutant_key] = StationPollutantEvaluation(
                pollutant_key=pollutant_key,
                window_hours=avg_window,
                required_valid_hours=MIN_VALID_HOURS_24H,
                observed_valid_hours=observed_window_count,
                expected_hours=avg_window,
                meets_completeness=True,
                mean_concentration=rounded_conc,
                unit=unit,
                sub_index=sub_idx,
                value_kind_used="concentration",
            )
        elif len(sub_index_values) == len(window_obs) and len(sub_index_values) > 0:
            for o in window_obs:
                contributing_providers.add(o.source_provider)
            mean_sub = int(round(sum(sub_index_values) / len(sub_index_values)))
            pollutant_evals[pollutant_key] = StationPollutantEvaluation(
                pollutant_key=pollutant_key,
                window_hours=avg_window,
                required_valid_hours=MIN_VALID_HOURS_24H,
                observed_valid_hours=observed_window_count,
                expected_hours=avg_window,
                meets_completeness=True,
                mean_concentration=None,
                unit=unit,
                sub_index=max(0, min(500, mean_sub)),
                value_kind_used="sub_index",
            )
        else:
            pollutant_evals[pollutant_key] = StationPollutantEvaluation(
                pollutant_key=pollutant_key,
                window_hours=avg_window,
                required_valid_hours=MIN_VALID_HOURS_24H,
                observed_valid_hours=observed_window_count,
                expected_hours=avg_window,
                meets_completeness=False,
                mean_concentration=None,
                unit=unit,
                sub_index=None,
                value_kind_used="invalid",
            )

    valid_evals = [
        ev for ev in pollutant_evals.values() if ev.meets_completeness and ev.sub_index is not None
    ]
    valid_pollutant_count = len(valid_evals)
    meets_16h = valid_pollutant_count > 0
    has_valid_pm = any(
        ev.pollutant_key in ("PM2.5", "PM10") and ev.meets_completeness and ev.sub_index is not None
        for ev in valid_evals
    )
    meets_3_pollutant_pm = valid_pollutant_count >= 3 and has_valid_pm

    if not meets_16h:
        rejection_reasons.append(
            f"Station {station.station_name} has 0 pollutants meeting the >= 16h completeness rule."
        )
    elif not meets_3_pollutant_pm:
        rejection_reasons.append(
            f"Station {station.station_name} only has {valid_pollutant_count} complete pollutants "
            f"(has_pm={has_valid_pm}); CPCB requires >= 3 pollutants including PM2.5 or PM10."
        )

    is_valid_station_aqi = meets_16h and meets_3_pollutant_pm
    if is_valid_station_aqi:
        worst_eval = max(valid_evals, key=lambda e: e.sub_index or -1)
        station_aqi = worst_eval.sub_index
        dominant = worst_eval.pollutant_key
    else:
        station_aqi = None
        dominant = None

    resolved_provider = resolve_source_provider_label(
        contributing_providers, fallback=source_provider or "none"
    )
    eval_iso = evaluation_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    eval_date_ist = format_ist_date(evaluation_end_utc)

    return StationAqiEvaluation(
        station_id=station.station_id,
        district_id=station.district_id,
        evaluation_timestamp_utc=eval_iso,
        evaluation_date=eval_date_ist,
        source_provider=resolved_provider,
        station_aqi=station_aqi,
        dominant_pollutant=dominant,
        meets_16h_completeness=meets_16h,
        meets_3_pollutant_pm_rule=meets_3_pollutant_pm,
        is_valid_station_aqi=is_valid_station_aqi,
        valid_pollutant_count=valid_pollutant_count,
        observed_pollutant_hours=observed_total_hours,
        expected_pollutant_hours=expected_total_hours,
        pollutant_evaluations=pollutant_evals,
        rejection_reasons=rejection_reasons,
    )


def aggregate_district_for_date(
    *,
    district: District,
    registered_stations: Sequence[StationRecord],
    station_evaluations: Sequence[StationAqiEvaluation],
    evaluation_date: str,
    updated_at_utc: Optional[str],
    source_attribution: Optional[str] = None,
    now_utc: Optional[datetime] = None,
) -> DailyDistrictHistoryRecord:
    """Aggregate station-level AQI evaluations into a DistrictWatch daily district record."""
    verified_stations = [s for s in registered_stations if s.is_verified_member]
    registered_count = len(verified_stations)
    expected_district_hours = sum(
        sum(AVERAGING_WINDOW_HOURS[p] for p in s.expected_pollutants)
        for s in verified_stations
    )

    eval_by_station = {ev.station_id: ev for ev in station_evaluations}
    observed_district_hours = sum(
        eval_by_station[s.station_id].observed_pollutant_hours
        for s in verified_stations
        if s.station_id in eval_by_station
    )

    coverage = (
        round(min(1.0, max(0.0, observed_district_hours / expected_district_hours)), 4)
        if expected_district_hours > 0
        else 0.0
    )

    valid_station_evals = [
        eval_by_station[s.station_id]
        for s in verified_stations
        if s.station_id in eval_by_station and eval_by_station[s.station_id].is_valid_station_aqi
    ]
    reporting_stations = len(valid_station_evals)

    valid_providers = {ev.source_provider for ev in valid_station_evals}
    resolved_attribution = (
        source_attribution
        if source_attribution and source_attribution != "auto"
        else format_district_source_attribution(valid_providers)
    )

    aggregated_pollutants: List[Pollutant] = []
    for pollutant_key, unit in POLLUTANT_SPECS:
        station_subs: List[int] = []
        station_concs: List[float] = []
        for st_eval in valid_station_evals:
            p_eval = st_eval.pollutant_evaluations.get(pollutant_key)
            if p_eval and p_eval.meets_completeness and p_eval.sub_index is not None:
                station_subs.append(p_eval.sub_index)
                if p_eval.mean_concentration is not None:
                    station_concs.append(p_eval.mean_concentration)

        if station_subs:
            mean_sub = int(round(sum(station_subs) / len(station_subs)))
            mean_conc: Optional[float] = None
            if len(station_concs) == len(station_subs):
                raw_mean = sum(station_concs) / len(station_concs)
                mean_conc = round(raw_mean, 2 if pollutant_key == "CO" else 1)
                if pollutant_key != "CO" and mean_conc.is_integer():
                    mean_conc = float(int(mean_conc))
            aggregated_pollutants.append(
                Pollutant(key=pollutant_key, value=mean_conc, unit=unit, subIndex=mean_sub)
            )
        else:
            aggregated_pollutants.append(
                Pollutant(key=pollutant_key, value=None, unit=unit, subIndex=None)
            )

    if reporting_stations > 0:
        district_aqi = int(
            round(
                sum(ev.station_aqi for ev in valid_station_evals if ev.station_aqi is not None)
                / reporting_stations
            )
        )
        valid_agg_subs = [p for p in aggregated_pollutants if p.subIndex is not None]
        dominant = (
            max(valid_agg_subs, key=lambda p: p.subIndex or -1).key if valid_agg_subs else None
        )
    else:
        district_aqi = None
        dominant = None
        updated_at_utc = None

    quality = classify_quality(
        stations=reporting_stations,
        coverage=coverage,
        aqi=district_aqi,
    )

    pm25_val = next((p.value for p in aggregated_pollutants if p.key == "PM2.5"), None)
    pm10_val = next((p.value for p in aggregated_pollutants if p.key == "PM10"), None)
    pm_inversion = has_pm_inversion_anomaly(pm25_val, pm10_val)

    incomplete_stations = [
        eval_by_station[s.station_id]
        for s in verified_stations
        if s.station_id in eval_by_station and not eval_by_station[s.station_id].is_valid_station_aqi
    ]

    custom_unavail_reason: Optional[str] = None
    if quality == "unavailable" and incomplete_stations:
        custom_unavail_reason = (
            "Station observations did not meet CPCB completeness rules "
            "(>= 16 valid hours and >= 3 pollutants including PM2.5 or PM10)."
        )

    caveats = build_caveats(
        quality=quality,
        stations=reporting_stations,
        coverage=coverage,
        pollutants=aggregated_pollutants,
        meets_cpcb_minimum_rule=reporting_stations > 0,
        updated_at_iso=updated_at_utc,
        now=now_utc,
        pm_inversion_detected=pm_inversion,
        custom_unavailable_reason=custom_unavail_reason,
    )

    if reporting_stations > 0:
        if reporting_stations < registered_count:
            caveats.append(
                f"{reporting_stations} of {registered_count} verified district stations met CPCB 16h / 3-pollutant completeness."
            )
        caveats.append(DISTRICT_METHODOLOGY_CAVEAT)

    return DailyDistrictHistoryRecord(
        district_id=district.id,
        date=evaluation_date,
        district_aqi=district_aqi,
        dominant_pollutant=dominant,
        updated_at_utc=updated_at_utc,
        source_attribution=resolved_attribution,
        reporting_stations=reporting_stations,
        registered_stations=registered_count,
        observed_station_pollutant_hours=observed_district_hours,
        expected_station_pollutant_hours=expected_district_hours,
        coverage=coverage,
        quality=quality,
        pollutants_json=json.dumps([p.dict() for p in aggregated_pollutants]),
        caveats_json=json.dumps(caveats),
    )


def build_snapshot_from_daily_history(
    *,
    district: District,
    history_records: Sequence[DailyDistrictHistoryRecord],
    anchor_date_str: Optional[str] = None,
    window_days: int = 30,
) -> DistrictSnapshot:
    """Build a complete DistrictSnapshot from persisted DailyDistrictHistoryRecord rows."""
    ref_date = (
        datetime.strptime(anchor_date_str, "%Y-%m-%d").date()
        if anchor_date_str
        else datetime.now(IST_TZ).date()
    )
    by_date: Dict[str, DailyDistrictHistoryRecord] = {r.date: r for r in history_records}

    history_list: List[Reading] = []
    for i in range(window_days):
        d_str = (ref_date - timedelta(days=window_days - 1 - i)).isoformat()
        rec = by_date.get(d_str)
        history_list.append(Reading(date=d_str, aqi=rec.district_aqi if rec else None))

    latest_date_str = ref_date.isoformat()
    current_rec = by_date.get(latest_date_str)

    if current_rec is None:
        empty_pollutants = [
            Pollutant(key=k, value=None, unit=u, subIndex=None) for k, u in POLLUTANT_SPECS
        ]
        return DistrictSnapshot(
            district=district,
            aqi=None,
            dominant=None,
            updatedAt=None,
            source="No verified station readings ingested for current window",
            stations=0,
            coverage=0.0,
            quality="unavailable",
            caveats=["No station readings received for this district in the current window."],
            pollutants=empty_pollutants,
            history=history_list,
            isIllustrative=False,
        )

    pollutants_raw = json.loads(current_rec.pollutants_json)
    pollutants = [Pollutant(**p) for p in pollutants_raw]
    caveats = json.loads(current_rec.caveats_json)

    return DistrictSnapshot(
        district=district,
        aqi=current_rec.district_aqi,
        dominant=current_rec.dominant_pollutant,
        updatedAt=current_rec.updated_at_utc,
        source=current_rec.source_attribution,
        stations=current_rec.reporting_stations,
        coverage=current_rec.coverage,
        quality=current_rec.quality,
        caveats=caveats,
        pollutants=pollutants,
        history=history_list,
        isIllustrative=False,
    )
