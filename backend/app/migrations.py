"""Database schema migrations and repository persistence helpers for DistrictWatch."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Iterable, List, Tuple
from app.models import (
    DailyDistrictHistoryRecord,
    RawStationObservation,
    StationAqiEvaluation,
    StationRecord,
)
from app.schemas import District

MIGRATIONS: Tuple[Tuple[int, str, str], ...] = (
    (
        1,
        "001_initial_schema",
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS districts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            state TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stations (
            station_id TEXT PRIMARY KEY,
            district_id TEXT NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
            source_provider TEXT NOT NULL,
            external_station_id TEXT NOT NULL,
            station_name TEXT NOT NULL,
            operating_agency TEXT NOT NULL,
            city TEXT NOT NULL,
            state TEXT NOT NULL,
            lat REAL,
            lon REAL,
            expected_pollutants_json TEXT NOT NULL,
            is_verified_member INTEGER NOT NULL DEFAULT 1,
            membership_basis TEXT NOT NULL,
            openaq_location_id INTEGER,
            openaq_verified_available INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_stations_district_verified
            ON stations(district_id, is_verified_member);

        CREATE TABLE IF NOT EXISTS raw_station_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL REFERENCES stations(station_id) ON DELETE CASCADE,
            source_provider TEXT NOT NULL,
            upstream_sensor_or_record_id TEXT,
            pollutant_key TEXT NOT NULL,
            observed_at_utc TEXT NOT NULL,
            window_hours INTEGER NOT NULL,
            raw_value_text TEXT NOT NULL,
            raw_unit TEXT,
            value_kind TEXT NOT NULL,
            normalized_concentration REAL,
            normalized_unit TEXT,
            calculated_sub_index INTEGER,
            is_valid INTEGER NOT NULL,
            validation_note TEXT,
            raw_payload_json TEXT NOT NULL,
            ingested_at_utc TEXT NOT NULL,
            UNIQUE(station_id, source_provider, pollutant_key, observed_at_utc, window_hours)
        );

        CREATE INDEX IF NOT EXISTS idx_raw_obs_station_pollutant_time
            ON raw_station_observations(station_id, pollutant_key, observed_at_utc DESC);

        CREATE TABLE IF NOT EXISTS station_aqi_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL REFERENCES stations(station_id) ON DELETE CASCADE,
            district_id TEXT NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
            evaluation_timestamp_utc TEXT NOT NULL,
            evaluation_date TEXT NOT NULL,
            source_provider TEXT NOT NULL,
            station_aqi INTEGER,
            dominant_pollutant TEXT,
            meets_16h_completeness INTEGER NOT NULL,
            meets_3_pollutant_pm_rule INTEGER NOT NULL,
            is_valid_station_aqi INTEGER NOT NULL,
            valid_pollutant_count INTEGER NOT NULL,
            observed_pollutant_hours INTEGER NOT NULL,
            expected_pollutant_hours INTEGER NOT NULL,
            pollutants_json TEXT NOT NULL,
            rejection_reasons_json TEXT NOT NULL,
            UNIQUE(station_id, evaluation_timestamp_utc, source_provider)
        );

        CREATE INDEX IF NOT EXISTS idx_station_eval_district_date
            ON station_aqi_evaluations(district_id, evaluation_date DESC);

        CREATE TABLE IF NOT EXISTS daily_district_history (
            district_id TEXT NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            district_aqi INTEGER,
            dominant_pollutant TEXT,
            updated_at_utc TEXT,
            source_attribution TEXT NOT NULL,
            reporting_stations INTEGER NOT NULL,
            registered_stations INTEGER NOT NULL,
            observed_station_pollutant_hours INTEGER NOT NULL,
            expected_station_pollutant_hours INTEGER NOT NULL,
            coverage REAL NOT NULL,
            quality TEXT NOT NULL,
            pollutants_json TEXT NOT NULL,
            caveats_json TEXT NOT NULL,
            PRIMARY KEY (district_id, date)
        );
        """,
    ),
)


def run_migrations(conn: sqlite3.Connection) -> List[int]:
    """Apply any unapplied SQL migrations in order and return newly applied version numbers."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at_utc TEXT NOT NULL
        );
        """
    )
    applied_rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    applied_versions = {int(row["version"]) for row in applied_rows}
    newly_applied: List[int] = []

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for version, name, sql in MIGRATIONS:
        if version in applied_versions:
            continue
        conn.executescript(sql)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at_utc) VALUES (?, ?, ?)",
            (version, name, now_iso),
        )
        newly_applied.append(version)

    conn.commit()
    return newly_applied


def upsert_districts(conn: sqlite3.Connection, districts: Iterable[District]) -> None:
    for d in districts:
        conn.execute(
            """
            INSERT INTO districts (id, name, state, lat, lon)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                state = excluded.state,
                lat = excluded.lat,
                lon = excluded.lon
            """,
            (d.id, d.name, d.state, d.lat, d.lon),
        )


def upsert_stations(conn: sqlite3.Connection, stations: Iterable[StationRecord]) -> None:
    for s in stations:
        conn.execute(
            """
            INSERT INTO stations (
                station_id, district_id, source_provider, external_station_id,
                station_name, operating_agency, city, state, lat, lon,
                expected_pollutants_json, is_verified_member, membership_basis,
                openaq_location_id, openaq_verified_available
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(station_id) DO UPDATE SET
                district_id = excluded.district_id,
                source_provider = excluded.source_provider,
                external_station_id = excluded.external_station_id,
                station_name = excluded.station_name,
                operating_agency = excluded.operating_agency,
                city = excluded.city,
                state = excluded.state,
                lat = excluded.lat,
                lon = excluded.lon,
                expected_pollutants_json = excluded.expected_pollutants_json,
                is_verified_member = excluded.is_verified_member,
                membership_basis = excluded.membership_basis,
                openaq_location_id = excluded.openaq_location_id,
                openaq_verified_available = excluded.openaq_verified_available
            """,
            (
                s.station_id,
                s.district_id,
                s.source_provider,
                s.external_station_id,
                s.station_name,
                s.operating_agency,
                s.city,
                s.state,
                s.lat,
                s.lon,
                json.dumps(list(s.expected_pollutants)),
                1 if s.is_verified_member else 0,
                s.membership_basis,
                s.openaq_location_id,
                1 if s.openaq_verified_available else 0,
            ),
        )


def insert_raw_observations(
    conn: sqlite3.Connection, observations: Iterable[RawStationObservation]
) -> int:
    count = 0
    for obs in observations:
        conn.execute(
            """
            INSERT INTO raw_station_observations (
                station_id, source_provider, upstream_sensor_or_record_id,
                pollutant_key, observed_at_utc, window_hours,
                raw_value_text, raw_unit, value_kind,
                normalized_concentration, normalized_unit, calculated_sub_index,
                is_valid, validation_note, raw_payload_json, ingested_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(station_id, source_provider, pollutant_key, observed_at_utc, window_hours)
            DO UPDATE SET
                raw_value_text = excluded.raw_value_text,
                raw_unit = excluded.raw_unit,
                value_kind = excluded.value_kind,
                normalized_concentration = excluded.normalized_concentration,
                normalized_unit = excluded.normalized_unit,
                calculated_sub_index = excluded.calculated_sub_index,
                is_valid = excluded.is_valid,
                validation_note = excluded.validation_note,
                raw_payload_json = excluded.raw_payload_json,
                ingested_at_utc = excluded.ingested_at_utc
            """,
            (
                obs.station_id,
                obs.source_provider,
                obs.upstream_sensor_or_record_id,
                obs.pollutant_key,
                obs.observed_at_utc,
                obs.window_hours,
                obs.raw_value_text,
                obs.raw_unit,
                obs.value_kind,
                obs.normalized_concentration,
                obs.normalized_unit,
                obs.calculated_sub_index,
                1 if obs.is_valid else 0,
                obs.validation_note,
                obs.raw_payload_json,
                obs.ingested_at_utc,
            ),
        )
        count += 1
    return count


def upsert_station_evaluation(conn: sqlite3.Connection, ev: StationAqiEvaluation) -> None:
    pollutants_dict = {
        k: {
            "pollutant_key": v.pollutant_key,
            "window_hours": v.window_hours,
            "required_valid_hours": v.required_valid_hours,
            "observed_valid_hours": v.observed_valid_hours,
            "expected_hours": v.expected_hours,
            "meets_completeness": v.meets_completeness,
            "mean_concentration": v.mean_concentration,
            "unit": v.unit,
            "sub_index": v.sub_index,
            "value_kind_used": v.value_kind_used,
        }
        for k, v in ev.pollutant_evaluations.items()
    }
    conn.execute(
        """
        INSERT INTO station_aqi_evaluations (
            station_id, district_id, evaluation_timestamp_utc, evaluation_date,
            source_provider, station_aqi, dominant_pollutant,
            meets_16h_completeness, meets_3_pollutant_pm_rule, is_valid_station_aqi,
            valid_pollutant_count, observed_pollutant_hours, expected_pollutant_hours,
            pollutants_json, rejection_reasons_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(station_id, evaluation_timestamp_utc, source_provider) DO UPDATE SET
            station_aqi = excluded.station_aqi,
            dominant_pollutant = excluded.dominant_pollutant,
            meets_16h_completeness = excluded.meets_16h_completeness,
            meets_3_pollutant_pm_rule = excluded.meets_3_pollutant_pm_rule,
            is_valid_station_aqi = excluded.is_valid_station_aqi,
            valid_pollutant_count = excluded.valid_pollutant_count,
            observed_pollutant_hours = excluded.observed_pollutant_hours,
            expected_pollutant_hours = excluded.expected_pollutant_hours,
            pollutants_json = excluded.pollutants_json,
            rejection_reasons_json = excluded.rejection_reasons_json
        """,
        (
            ev.station_id,
            ev.district_id,
            ev.evaluation_timestamp_utc,
            ev.evaluation_date,
            ev.source_provider,
            ev.station_aqi,
            ev.dominant_pollutant,
            1 if ev.meets_16h_completeness else 0,
            1 if ev.meets_3_pollutant_pm_rule else 0,
            1 if ev.is_valid_station_aqi else 0,
            ev.valid_pollutant_count,
            ev.observed_pollutant_hours,
            ev.expected_pollutant_hours,
            json.dumps(pollutants_dict),
            json.dumps(ev.rejection_reasons),
        ),
    )


def upsert_daily_district_history(
    conn: sqlite3.Connection, record: DailyDistrictHistoryRecord
) -> None:
    conn.execute(
        """
        INSERT INTO daily_district_history (
            district_id, date, district_aqi, dominant_pollutant,
            updated_at_utc, source_attribution, reporting_stations,
            registered_stations, observed_station_pollutant_hours,
            expected_station_pollutant_hours, coverage, quality,
            pollutants_json, caveats_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(district_id, date) DO UPDATE SET
            district_aqi = excluded.district_aqi,
            dominant_pollutant = excluded.dominant_pollutant,
            updated_at_utc = excluded.updated_at_utc,
            source_attribution = excluded.source_attribution,
            reporting_stations = excluded.reporting_stations,
            registered_stations = excluded.registered_stations,
            observed_station_pollutant_hours = excluded.observed_station_pollutant_hours,
            expected_station_pollutant_hours = excluded.expected_station_pollutant_hours,
            coverage = excluded.coverage,
            quality = excluded.quality,
            pollutants_json = excluded.pollutants_json,
            caveats_json = excluded.caveats_json
        """,
        (
            record.district_id,
            record.date,
            record.district_aqi,
            record.dominant_pollutant,
            record.updated_at_utc,
            record.source_attribution,
            record.reporting_stations,
            record.registered_stations,
            record.observed_station_pollutant_hours,
            record.expected_station_pollutant_hours,
            record.coverage,
            record.quality,
            record.pollutants_json,
            record.caveats_json,
        ),
    )
