"""Local CLI tool for database initialization, incremental upstream sync, and IST window recomputation.

Usage:
    PYTHONPATH=. python3 -m app.cli init-db
    PYTHONPATH=. python3 -m app.cli recompute --anchor-date 2026-11-02
    PYTHONPATH=. python3 -m app.cli sync
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any, Dict, List, Optional
from app.config import get_settings
from app.database import get_db_connection
from app.districts_registry import (
    REVIEWED_STATIONS,
    TARGET_DISTRICTS,
    list_target_districts,
    list_verified_stations_for_district,
)
from app.ingestion.cpcb_adapter import fetch_cpcb_ogd_page, parse_cpcb_ogd_payload
from app.ingestion.http_utils import create_shared_http_client
from app.ingestion.openaq_adapter import (
    discover_openaq_locations_near_coordinates,
    fetch_openaq_location,
    fetch_openaq_sensor_hours,
    parse_openaq_hourly_payload,
    validate_openaq_location,
)
from app.migrations import (
    get_latest_sensor_observation_time,
    get_latest_station_observation_time,
    insert_raw_observations,
    load_station_observations_from_db,
    load_stations_from_db,
    recompute_district_for_ist_date,
    run_migrations,
    update_station_openaq_mapping,
    upsert_daily_district_history,
    upsert_districts,
    upsert_station_evaluation,
    upsert_stations,
)
from app.models import RawStationObservation
from app.services.aggregator import (
    IST_TZ,
    aggregate_district_for_date,
    evaluate_station_window,
    format_ist_date,
    ist_date_bounds_in_utc,
)


def seed_registry(conn: sqlite3.Connection) -> None:
    """Apply migrations and seed the 30 target districts and reviewed stations (preserving discovered OpenAQ IDs)."""
    run_migrations(conn)
    upsert_districts(conn, list_target_districts())
    upsert_stations(conn, REVIEWED_STATIONS)
    conn.commit()


def load_station_observations(
    conn: sqlite3.Connection,
    station_ids: List[str],
    start_utc: str,
    end_utc: str,
) -> List[RawStationObservation]:
    if not station_ids:
        return []
    placeholders = ",".join("?" for _ in station_ids)
    query = f"""
        SELECT * FROM raw_station_observations
        WHERE station_id IN ({placeholders})
          AND observed_at_utc > ?
          AND observed_at_utc <= ?
        ORDER BY observed_at_utc ASC
    """
    rows = conn.execute(query, (*station_ids, start_utc, end_utc)).fetchall()
    return [
        RawStationObservation(
            station_id=r["station_id"],
            source_provider=r["source_provider"],
            upstream_sensor_or_record_id=r["upstream_sensor_or_record_id"],
            pollutant_key=r["pollutant_key"],
            observed_at_utc=r["observed_at_utc"],
            window_hours=int(r["window_hours"]),
            raw_value_text=r["raw_value_text"],
            raw_unit=r["raw_unit"],
            value_kind=r["value_kind"],
            normalized_concentration=r["normalized_concentration"],
            normalized_unit=r["normalized_unit"],
            calculated_sub_index=r["calculated_sub_index"],
            is_valid=bool(r["is_valid"]),
            validation_note=r["validation_note"],
            raw_payload_json=r["raw_payload_json"],
            ingested_at_utc=r["ingested_at_utc"],
        )
        for r in rows
    ]


def recompute_district_history_window(
    conn: sqlite3.Connection,
    *,
    anchor_date_str: Optional[str] = None,
    days: int = 30,
) -> int:
    """Recompute station-level evaluations and DistrictWatch daily district records across IST calendar days."""
    seed_registry(conn)
    ref_date = (
        datetime.strptime(anchor_date_str, "%Y-%m-%d").date()
        if anchor_date_str
        else datetime.now(IST_TZ).date()
    )
    updated_records = 0

    for entry in TARGET_DISTRICTS:
        district = entry.district
        # Load verified stations from SQLite so persisted OpenAQ mappings are reflected
        verified_stations = load_stations_from_db(
            conn, district_id=district.id, verified_only=True
        ) or list_verified_stations_for_district(district.id)

        for day_offset in range(days):
            target_ist_date = (ref_date - timedelta(days=days - 1 - day_offset)).isoformat()
            if recompute_district_for_ist_date(
                conn, district, verified_stations, target_ist_date
            ):
                updated_records += 1

    conn.commit()
    return updated_records


def run_sync(
    database_url: Optional[str] = None,
    *,
    days: Optional[int] = None,
    overlap_hours: Optional[int] = None,
    now_utc: Optional[datetime] = None,
    http_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run an incremental (or initial backfill) CLI ingestion sync with provider failure isolation."""
    settings = get_settings()
    shared_http_client = (
        http_client
        if http_client is not None
        else create_shared_http_client(settings)
    )
    backfill_days = days if days is not None else settings.initial_backfill_days
    inc_overlap_hours = (
        overlap_hours if overlap_hours is not None else settings.incremental_overlap_hours
    )
    ref_now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    started_iso = ref_now.strftime("%Y-%m-%dT%H:%M:%SZ")

    cpcb_status = "not_configured" if not settings.cpcb_configured else "pending"
    openaq_status = "not_configured" if not settings.openaq_configured else "pending"
    cpcb_error: Optional[str] = None
    openaq_error: Optional[str] = None
    cpcb_ingested = 0
    openaq_ingested = 0
    earliest_ingested_utc: Optional[datetime] = None

    with get_db_connection(database_url) as conn:
        seed_registry(conn)
        cur = conn.execute(
            """
            INSERT INTO ingestion_sync_runs (
                started_at_utc, overall_status, cpcb_status, openaq_status
            )
            VALUES (?, 'running', ?, ?)
            """,
            (started_iso, cpcb_status, openaq_status),
        )
        run_id = int(cur.lastrowid)
        conn.commit()

        # Provider 1: CPCB (data.gov.in) — isolated failure domain
        if settings.cpcb_configured:
            try:
                for entry in TARGET_DISTRICTS:
                    for city in entry.cpcb_cities:
                        payload = fetch_cpcb_ogd_page(
                            settings.data_gov_in_api_key,
                            state=entry.cpcb_state,
                            city=city,
                            http_client=shared_http_client,
                        )
                        report = parse_cpcb_ogd_payload(payload, ingested_at_utc=started_iso)
                        cpcb_ingested += insert_raw_observations(conn, report.accepted_observations)
                        for o in report.accepted_observations:
                            if o.is_valid:
                                dt = datetime.fromisoformat(o.observed_at_utc.replace("Z", "+00:00"))
                                if earliest_ingested_utc is None or dt < earliest_ingested_utc:
                                    earliest_ingested_utc = dt
                cpcb_status = "succeeded"
                conn.commit()
            except Exception as exc:
                cpcb_status = "failed"
                cpcb_error = str(exc)
                conn.rollback()

        # Provider 2: OpenAQ v3 — isolated failure domain, using persisted station IDs & incremental overlap
        if settings.openaq_configured:
            try:
                persisted_stations = load_stations_from_db(conn, verified_only=True)
                end_utc_str = ref_now.strftime("%Y-%m-%dT%H:%M:%SZ")
                full_backfill_start = ref_now - timedelta(days=backfill_days)

                for st in persisted_stations:
                    loc_id = st.openaq_location_id
                    loc_obj: Optional[Dict[str, Any]] = None

                    if loc_id is not None:
                        loc_payload = fetch_openaq_location(
                            settings.openaq_api_key, loc_id, http_client=shared_http_client
                        )
                        results = loc_payload.get("results") or []
                        if results:
                            loc_obj = results[0]
                    elif st.lat is not None and st.lon is not None:
                        near_payload = discover_openaq_locations_near_coordinates(
                            settings.openaq_api_key,
                            lat=st.lat,
                            lon=st.lon,
                            radius_meters=2500,
                            http_client=shared_http_client,
                        )
                        for cand in near_payload.get("results") or []:
                            cand_val = validate_openaq_location(cand, reference_now_utc=ref_now)
                            if cand_val.is_available:
                                loc_obj = cand
                                loc_id = cand_val.location_id
                                update_station_openaq_mapping(
                                    conn, st.station_id, loc_id, True
                                )
                                break

                    if loc_obj is None:
                        continue

                    val_res = validate_openaq_location(loc_obj, reference_now_utc=ref_now)
                    update_station_openaq_mapping(
                        conn, st.station_id, val_res.location_id, val_res.is_available
                    )
                    if not val_res.is_available:
                        continue

                    for pollutant_key, sensor in val_res.valid_sensors.items():
                        # Incremental vs initial backfill window calculated independently per OpenAQ sensor
                        last_sensor_obs_str = get_latest_sensor_observation_time(
                            conn, st.station_id, sensor.sensor_id
                        )
                        if last_sensor_obs_str:
                            last_sensor_obs_dt = datetime.fromisoformat(
                                last_sensor_obs_str.replace("Z", "+00:00")
                            ).astimezone(timezone.utc)
                            sensor_start_dt = max(
                                full_backfill_start,
                                last_sensor_obs_dt - timedelta(hours=inc_overlap_hours),
                            )
                        else:
                            sensor_start_dt = full_backfill_start

                        start_utc_str = sensor_start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                        hours_payload = fetch_openaq_sensor_hours(
                            settings.openaq_api_key,
                            sensor.sensor_id,
                            datetime_from_utc=start_utc_str,
                            datetime_to_utc=end_utc_str,
                            http_client=shared_http_client,
                        )
                        hourly_obs = parse_openaq_hourly_payload(
                            station_id=st.station_id,
                            sensor_id=sensor.sensor_id,
                            expected_pollutant=pollutant_key,
                            payload=hours_payload,
                            ingested_at_utc=started_iso,
                        )
                        openaq_ingested += insert_raw_observations(conn, hourly_obs)
                        for o in hourly_obs:
                            if o.is_valid:
                                dt = datetime.fromisoformat(o.observed_at_utc.replace("Z", "+00:00"))
                                if earliest_ingested_utc is None or dt < earliest_ingested_utc:
                                    earliest_ingested_utc = dt

                openaq_status = "succeeded"
                conn.commit()
            except Exception as exc:
                openaq_status = "failed"
                openaq_error = str(exc)
                conn.rollback()

        # Recompute only the affected IST days when incremental data arrived, or the requested window
        if earliest_ingested_utc is not None:
            earliest_ist_date = datetime.strptime(
                format_ist_date(earliest_ingested_utc), "%Y-%m-%d"
            ).date()
            current_ist_date = ref_now.astimezone(IST_TZ).date()
            recompute_days = max(1, min(backfill_days, (current_ist_date - earliest_ist_date).days + 2))
            anchor_ist_str = current_ist_date.isoformat()
        else:
            recompute_days = backfill_days
            anchor_ist_str = ref_now.astimezone(IST_TZ).date().isoformat()

        daily_updated = recompute_district_history_window(
            conn, anchor_date_str=anchor_ist_str, days=recompute_days
        )

        # Compute overall sync status
        active_statuses = [
            s for s in (cpcb_status, openaq_status) if s != "not_configured"
        ]
        if not active_statuses:
            overall_status = "no_providers_configured"
        elif all(s == "succeeded" for s in active_statuses):
            overall_status = "succeeded"
        elif any(s == "succeeded" for s in active_statuses):
            overall_status = "partial_failure"
        else:
            overall_status = "failed"

        completed_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """
            UPDATE ingestion_sync_runs
            SET completed_at_utc = ?,
                overall_status = ?,
                cpcb_status = ?,
                cpcb_observations_ingested = ?,
                cpcb_error = ?,
                openaq_status = ?,
                openaq_observations_ingested = ?,
                openaq_error = ?,
                daily_district_records_updated = ?
            WHERE id = ?
            """,
            (
                completed_iso,
                overall_status,
                cpcb_status,
                cpcb_ingested,
                cpcb_error,
                openaq_status,
                openaq_ingested,
                openaq_error,
                daily_updated,
                run_id,
            ),
        )
        conn.commit()

    return {
        "overall_status": overall_status,
        "cpcb_status": cpcb_status,
        "cpcb_observations_ingested": cpcb_ingested,
        "cpcb_error": cpcb_error,
        "openaq_status": openaq_status,
        "openaq_observations_ingested": openaq_ingested,
        "openaq_error": openaq_error,
        "daily_district_records_updated": daily_updated,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="DistrictWatch Local Ingestion & DB CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Run SQLite schema migrations and seed reviewed stations")

    recompute_p = sub.add_parser("recompute", help="Recompute station & district AQI windows")
    recompute_p.add_argument("--anchor-date", type=str, default=None, help="YYYY-MM-DD (IST)")
    recompute_p.add_argument("--days", type=int, default=30, help="History window days")

    sync_p = sub.add_parser("sync", help="Fetch from configured CPCB / OpenAQ keys and recompute")
    sync_p.add_argument("--days", type=int, default=None, help="Initial backfill days")
    sync_p.add_argument("--overlap-hours", type=int, default=None, help="Incremental overlap hours")

    args = parser.parse_args()
    if args.command == "init-db":
        with get_db_connection() as conn:
            seed_registry(conn)
        print("Database initialized and 7 districts / reviewed stations seeded.")
    elif args.command == "recompute":
        with get_db_connection() as conn:
            count = recompute_district_history_window(
                conn, anchor_date_str=args.anchor_date, days=args.days
            )
        print(f"Recomputed {count} daily district history records.")
    elif args.command == "sync":
        res = run_sync(days=args.days, overlap_hours=args.overlap_hours)
        print(f"Sync complete: {res}")


if __name__ == "__main__":
    main()
