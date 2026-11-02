"""Local CLI tool for database initialization, upstream ingestion sync, and 30-day window recomputation.

Usage:
    PYTHONPATH=. python3 -m app.cli init-db
    PYTHONPATH=. python3 -m app.cli recompute --anchor-date 2026-11-02
    PYTHONPATH=. python3 -m app.cli sync
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Dict, List, Optional
from app.config import get_settings
from app.database import get_db_connection
from app.districts_registry import (
    REVIEWED_STATIONS,
    TARGET_DISTRICTS,
    list_target_districts,
    list_verified_stations_for_district,
)
from app.ingestion.cpcb_adapter import fetch_cpcb_ogd_page, parse_cpcb_ogd_payload
from app.ingestion.openaq_adapter import (
    fetch_openaq_location,
    fetch_openaq_sensor_hours,
    parse_openaq_hourly_payload,
    validate_openaq_location,
)
from app.migrations import (
    insert_raw_observations,
    run_migrations,
    upsert_daily_district_history,
    upsert_districts,
    upsert_station_evaluation,
    upsert_stations,
)
from app.models import RawStationObservation
from app.services.aggregator import aggregate_district_for_date, evaluate_station_window


def seed_registry(conn: sqlite3.Connection) -> None:
    """Apply migrations and seed the 7 target districts and 46 reviewed station records."""
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
    """Recompute station-level evaluations and DistrictWatch daily district records across a window."""
    seed_registry(conn)
    ref_date = (
        datetime.strptime(anchor_date_str, "%Y-%m-%d").date()
        if anchor_date_str
        else datetime.now(timezone.utc).date()
    )
    updated_records = 0

    for entry in TARGET_DISTRICTS:
        district = entry.district
        verified_stations = list_verified_stations_for_district(district.id)
        station_ids = [s.station_id for s in verified_stations]

        for day_offset in range(days):
            target_date = ref_date - timedelta(days=days - 1 - day_offset)
            eval_end = datetime(
                target_date.year, target_date.month, target_date.day, 23, 59, 59, tzinfo=timezone.utc
            )
            eval_start = eval_end - timedelta(hours=24)

            obs = load_station_observations(
                conn,
                station_ids,
                eval_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                eval_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            if not obs:
                continue

            station_evals = []
            latest_obs_utc: Optional[str] = None
            providers_seen = set()

            for st in verified_stations:
                st_obs = [o for o in obs if o.station_id == st.station_id]
                for o in st_obs:
                    if o.is_valid:
                        providers_seen.add(o.source_provider)
                        if latest_obs_utc is None or o.observed_at_utc > latest_obs_utc:
                            latest_obs_utc = o.observed_at_utc

                ev = evaluate_station_window(
                    station=st,
                    observations=st_obs,
                    evaluation_end_utc=eval_end,
                    source_provider="openaq_v3",
                )
                upsert_station_evaluation(conn, ev)
                station_evals.append(ev)

            source_attr = (
                "CPCB / SPCB CAAQMS via OpenAQ v3 & data.gov.in (GODL-India)"
                if "cpcb_ogd" in providers_seen and "openaq_v3" in providers_seen
                else "CPCB / SPCB CAAQMS via OpenAQ v3"
                if "openaq_v3" in providers_seen
                else "CPCB / SPCB CAAQMS via data.gov.in (GODL-India)"
            )

            daily_rec = aggregate_district_for_date(
                district=district,
                registered_stations=verified_stations,
                station_evaluations=station_evals,
                evaluation_date=target_date.isoformat(),
                updated_at_utc=latest_obs_utc,
                source_attribution=source_attr,
                now_utc=eval_end,
            )
            upsert_daily_district_history(conn, daily_rec)
            updated_records += 1

    conn.commit()
    return updated_records


def run_sync(database_url: Optional[str] = None, days: int = 30) -> Dict[str, int]:
    """Run a local CLI ingestion sync from configured CPCB and OpenAQ adapters."""
    settings = get_settings()
    summary = {
        "cpcb_observations_ingested": 0,
        "openaq_observations_ingested": 0,
        "daily_district_records_updated": 0,
    }

    with get_db_connection(database_url) as conn:
        seed_registry(conn)

        if settings.cpcb_configured:
            for entry in TARGET_DISTRICTS:
                for city in entry.cpcb_cities:
                    payload = fetch_cpcb_ogd_page(
                        settings.data_gov_in_api_key,
                        state=entry.cpcb_state,
                        city=city,
                    )
                    report = parse_cpcb_ogd_payload(payload)
                    summary["cpcb_observations_ingested"] += insert_raw_observations(
                        conn, report.accepted_observations
                    )

        if settings.openaq_configured:
            now_utc = datetime.now(timezone.utc)
            start_utc = (now_utc - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            end_utc = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

            for st in REVIEWED_STATIONS:
                if not st.is_verified_member or st.openaq_location_id is None:
                    continue
                loc_payload = fetch_openaq_location(
                    settings.openaq_api_key, st.openaq_location_id
                )
                results = loc_payload.get("results") or []
                if not results:
                    continue
                val_res = validate_openaq_location(results[0], reference_now_utc=now_utc)
                if not val_res.is_available:
                    continue

                for pollutant_key, sensor in val_res.valid_sensors.items():
                    hours_payload = fetch_openaq_sensor_hours(
                        settings.openaq_api_key,
                        sensor.sensor_id,
                        datetime_from_utc=start_utc,
                        datetime_to_utc=end_utc,
                    )
                    hourly_obs = parse_openaq_hourly_payload(
                        station_id=st.station_id,
                        sensor_id=sensor.sensor_id,
                        expected_pollutant=pollutant_key,
                        payload=hours_payload,
                    )
                    summary["openaq_observations_ingested"] += insert_raw_observations(
                        conn, hourly_obs
                    )

        summary["daily_district_records_updated"] = recompute_district_history_window(
            conn, days=days
        )

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="DistrictWatch Local Ingestion & DB CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Run SQLite schema migrations and seed reviewed stations")

    recompute_p = sub.add_parser("recompute", help="Recompute station & district AQI windows")
    recompute_p.add_argument("--anchor-date", type=str, default=None, help="YYYY-MM-DD")
    recompute_p.add_argument("--days", type=int, default=30, help="History window days")

    sync_p = sub.add_parser("sync", help="Fetch from configured CPCB / OpenAQ keys and recompute")
    sync_p.add_argument("--days", type=int, default=30, help="History window days")

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
        res = run_sync(days=args.days)
        print(f"Sync complete: {res}")


if __name__ == "__main__":
    main()
