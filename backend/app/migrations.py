"""Database schema migrations and repository persistence helpers for DistrictWatch."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3
from typing import Dict, Iterable, List, Optional, Set, Tuple
from app.districts_registry import (
    REVIEWED_STATIONS,
    get_district_mapping,
    list_verified_stations_for_district,
)
from app.models import (
    DailyDistrictHistoryRecord,
    RawStationObservation,
    StationAqiEvaluation,
    StationRecord,
    SyncRunRecord,
)
from app.schemas import District
from app.services.aggregator import (
    aggregate_district_for_date,
    evaluate_station_window,
    format_ist_date,
    ist_date_bounds_in_utc,
)

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
    (
        2,
        "002_ingestion_sync_runs",
        """
        CREATE TABLE IF NOT EXISTS ingestion_sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at_utc TEXT NOT NULL,
            completed_at_utc TEXT,
            overall_status TEXT NOT NULL,
            cpcb_status TEXT NOT NULL,
            cpcb_observations_ingested INTEGER NOT NULL DEFAULT 0,
            cpcb_error TEXT,
            openaq_status TEXT NOT NULL,
            openaq_observations_ingested INTEGER NOT NULL DEFAULT 0,
            openaq_error TEXT,
            daily_district_records_updated INTEGER NOT NULL DEFAULT 0
        );
        """,
    ),
    (
        3,
        "003_invalidate_legacy_unverified_cpcb_observations",
        """
        UPDATE raw_station_observations
        SET is_valid = 0,
            value_kind = 'invalid',
            normalized_concentration = NULL,
            normalized_unit = NULL,
            calculated_sub_index = NULL,
            validation_note = 'Invalidated by migration 003: unitless or NA-unit CPCB record has unverified scale semantics; raw observation preserved for auditability.'
        WHERE source_provider = 'cpcb_ogd'
          AND is_valid = 1
          AND (
              raw_unit IS NULL
              OR TRIM(UPPER(raw_unit)) IN ('', 'NA', 'N/A', 'NONE', 'NULL', '-', 'UNKNOWN')
          );
        """,
    ),
)


def _parse_utc(iso_str: str) -> datetime:
    clean = iso_str.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(clean)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_station_observations_from_db(
    conn: sqlite3.Connection,
    station_ids: List[str],
    start_utc: str,
    end_utc: str,
) -> List[RawStationObservation]:
    """Load RawStationObservation rows for `station_ids` within `(start_utc, end_utc]`."""
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


def _resolve_district_record(conn: sqlite3.Connection, district_id: str) -> Optional[District]:
    mapping = get_district_mapping(district_id)
    if mapping is not None:
        return mapping.district
    row = conn.execute("SELECT * FROM districts WHERE id = ?", (district_id,)).fetchone()
    if row is not None:
        return District(
            id=row["id"],
            name=row["name"],
            state=row["state"],
            lat=float(row["lat"]),
            lon=float(row["lon"]),
        )
    return None


def recompute_district_for_ist_date(
    conn: sqlite3.Connection,
    district: District,
    verified_stations: List[StationRecord],
    target_ist_date: str,
) -> bool:
    """Recompute station evaluations and daily district history for a single (district, IST date).

    - When valid observations exist on `target_ist_date`, evaluates the 24-hour rolling window
      anchored to the latest valid observation on that date and updates `station_aqi_evaluations`
      and `daily_district_history`.
    - When no valid observations remain on `target_ist_date`, invalidates any existing
      `station_aqi_evaluations` or `daily_district_history` rows for `(district.id, target_ist_date)`
      so stale derived AQIs (including hybrid records) never remain exposed.
    """
    station_ids = [s.station_id for s in verified_stations]
    ist_day_start_utc, ist_day_end_utc = ist_date_bounds_in_utc(target_ist_date)

    day_obs = load_station_observations_from_db(
        conn,
        station_ids,
        ist_day_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ist_day_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    valid_day_obs = [o for o in day_obs if o.is_valid]

    if not valid_day_obs:
        existing_daily = conn.execute(
            "SELECT 1 FROM daily_district_history WHERE district_id = ? AND date = ?",
            (district.id, target_ist_date),
        ).fetchone()
        existing_eval = conn.execute(
            "SELECT 1 FROM station_aqi_evaluations WHERE district_id = ? AND evaluation_date = ?",
            (district.id, target_ist_date),
        ).fetchone()

        if existing_eval is not None:
            conn.execute(
                """
                UPDATE station_aqi_evaluations
                SET station_aqi = NULL,
                    dominant_pollutant = NULL,
                    meets_16h_completeness = 0,
                    meets_3_pollutant_pm_rule = 0,
                    is_valid_station_aqi = 0,
                    valid_pollutant_count = 0,
                    observed_pollutant_hours = 0,
                    rejection_reasons_json = '["Invalidated: no valid station observations remain in evaluation window."]'
                WHERE district_id = ? AND evaluation_date = ?
                """,
                (district.id, target_ist_date),
            )

        if existing_daily is not None:
            invalidated_rec = aggregate_district_for_date(
                district=district,
                registered_stations=verified_stations,
                station_evaluations=[],
                evaluation_date=target_ist_date,
                updated_at_utc=None,
                source_attribution="auto",
                now_utc=ist_day_end_utc,
            )
            upsert_daily_district_history(conn, invalidated_rec)

        return existing_daily is not None or existing_eval is not None

    latest_on_day_str = max(o.observed_at_utc for o in valid_day_obs)
    eval_end = _parse_utc(latest_on_day_str)
    eval_start = eval_end - timedelta(hours=24)

    obs = load_station_observations_from_db(
        conn,
        station_ids,
        eval_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        eval_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    station_evals: List[StationAqiEvaluation] = []
    latest_obs_utc: Optional[str] = None

    for st in verified_stations:
        st_obs = [o for o in obs if o.station_id == st.station_id]
        for o in st_obs:
            if o.is_valid:
                if latest_obs_utc is None or o.observed_at_utc > latest_obs_utc:
                    latest_obs_utc = o.observed_at_utc

        ev = evaluate_station_window(
            station=st,
            observations=st_obs,
            evaluation_end_utc=eval_end,
        )
        if ev.evaluation_date != target_ist_date:
            ev = replace(ev, evaluation_date=target_ist_date)
        upsert_station_evaluation(conn, ev)
        station_evals.append(ev)

    # Also invalidate any orphan station_aqi_evaluations rows on target_ist_date for stations not in verified_stations
    verified_ids = {s.station_id for s in verified_stations}
    eval_rows = conn.execute(
        "SELECT id, station_id FROM station_aqi_evaluations WHERE district_id = ? AND evaluation_date = ?",
        (district.id, target_ist_date),
    ).fetchall()
    for r in eval_rows:
        if r["station_id"] not in verified_ids:
            conn.execute(
                """
                UPDATE station_aqi_evaluations
                SET station_aqi = NULL,
                    dominant_pollutant = NULL,
                    meets_16h_completeness = 0,
                    meets_3_pollutant_pm_rule = 0,
                    is_valid_station_aqi = 0,
                    valid_pollutant_count = 0,
                    observed_pollutant_hours = 0
                WHERE id = ?
                """,
                (r["id"],),
            )

    daily_rec = aggregate_district_for_date(
        district=district,
        registered_stations=verified_stations,
        station_evaluations=station_evals,
        evaluation_date=target_ist_date,
        updated_at_utc=latest_obs_utc,
        source_attribution="auto",
        now_utc=eval_end,
    )
    upsert_daily_district_history(conn, daily_rec)
    return True


def _reconcile_evaluations_after_migration_003(
    conn: sqlite3.Connection,
    invalidated_cpcb_rows: List[sqlite3.Row],
) -> None:
    """Targeted post-invalidation reconciliation for Migration 003 across all historical dates.

    - Preserves historical evaluations and daily district records supported exclusively by
      explicit, verified physical units (where zero unitless CPCB observations were invalidated
      and valid explicit-unit CPCB observations exist in the evaluation window).
    - Recomputes (or invalidates if insufficient valid data remains) every (district_id, date)
      whose calculation depended on invalidated unitless/NA-unit CPCB observations, including
      hybrid records and historical dates older than 30 days.
    """
    station_to_district: Dict[str, str] = {
        s.station_id: s.district_id for s in REVIEWED_STATIONS
    }
    for r in conn.execute("SELECT station_id, district_id FROM stations").fetchall():
        station_to_district[r["station_id"]] = r["district_id"]

    # 1. Dates directly touched by newly invalidated unitless CPCB observations
    dates_with_unitless_cpcb: Set[Tuple[str, str]] = set()
    for r in invalidated_cpcb_rows:
        d_id = station_to_district.get(r["station_id"])
        if not d_id:
            continue
        dt = _parse_utc(r["observed_at_utc"])
        ist_d = format_ist_date(dt)
        next_ist_d = (date.fromisoformat(ist_d) + timedelta(days=1)).isoformat()
        utc_d = dt.strftime("%Y-%m-%d")
        dates_with_unitless_cpcb.add((d_id, ist_d))
        dates_with_unitless_cpcb.add((d_id, next_ist_d))
        dates_with_unitless_cpcb.add((d_id, utc_d))

    # 2. Existing station evaluations and daily history records in the DB (across all historical dates)
    existing_eval_dates: Set[Tuple[str, str]] = {
        (r["district_id"], r["evaluation_date"])
        for r in conn.execute(
            "SELECT DISTINCT district_id, evaluation_date FROM station_aqi_evaluations"
        ).fetchall()
    }
    existing_daily_dates: Set[Tuple[str, str]] = {
        (r["district_id"], r["date"])
        for r in conn.execute(
            "SELECT DISTINCT district_id, date FROM daily_district_history"
        ).fetchall()
    }
    all_persisted_dates = existing_eval_dates | existing_daily_dates

    affected_district_dates: Set[Tuple[str, str]] = set()
    for d_id, target_date in all_persisted_dates:
        if (d_id, target_date) in dates_with_unitless_cpcb:
            affected_district_dates.add((d_id, target_date))

    # 3. Also check any persisted CPCB or hybrid evaluation/history row not in dates_with_unitless_cpcb
    #    to verify whether it is genuinely backed by valid explicit-unit CPCB observations.
    cpcb_or_hybrid_candidates: Set[Tuple[str, str]] = set()
    for r in conn.execute(
        """
        SELECT DISTINCT district_id, evaluation_date
        FROM station_aqi_evaluations
        WHERE source_provider IN ('cpcb_ogd', 'hybrid_cpcb_openaq')
        """
    ).fetchall():
        cpcb_or_hybrid_candidates.add((r["district_id"], r["evaluation_date"]))

    for r in conn.execute(
        """
        SELECT DISTINCT district_id, date
        FROM daily_district_history
        WHERE source_attribution LIKE '%data.gov.in%'
        """
    ).fetchall():
        cpcb_or_hybrid_candidates.add((r["district_id"], r["date"]))

    for d_id, target_date in cpcb_or_hybrid_candidates:
        if (d_id, target_date) in affected_district_dates:
            continue
        verified_stations = load_stations_from_db(
            conn, district_id=d_id, verified_only=True
        ) or list_verified_stations_for_district(d_id)
        station_ids = [s.station_id for s in verified_stations]
        if not station_ids:
            affected_district_dates.add((d_id, target_date))
            continue

        ist_start, ist_end = ist_date_bounds_in_utc(target_date)
        win_start = (ist_start - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        win_end = max(ist_end, _parse_utc(f"{target_date}T23:59:59Z")).strftime("%Y-%m-%dT%H:%M:%SZ")
        placeholders = ",".join("?" for _ in station_ids)

        # Check if any unitless/invalid CPCB observation exists in this window
        unitless_in_win = conn.execute(
            f"""
            SELECT 1 FROM raw_station_observations
            WHERE station_id IN ({placeholders})
              AND source_provider = 'cpcb_ogd'
              AND is_valid = 0
              AND (
                  raw_unit IS NULL
                  OR TRIM(UPPER(raw_unit)) IN ('', 'NA', 'N/A', 'NONE', 'NULL', '-', 'UNKNOWN')
              )
              AND observed_at_utc > ?
              AND observed_at_utc <= ?
            LIMIT 1
            """,
            (*station_ids, win_start, win_end),
        ).fetchone()

        if unitless_in_win is not None:
            affected_district_dates.add((d_id, target_date))
            continue

        # Check if at least one valid explicit-unit CPCB observation backs this CPCB/hybrid date
        valid_cpcb_in_win = conn.execute(
            f"""
            SELECT 1 FROM raw_station_observations
            WHERE station_id IN ({placeholders})
              AND source_provider = 'cpcb_ogd'
              AND is_valid = 1
              AND observed_at_utc > ?
              AND observed_at_utc <= ?
            LIMIT 1
            """,
            (*station_ids, win_start, win_end),
        ).fetchone()

        if valid_cpcb_in_win is None:
            # No valid explicit-unit CPCB observations exist for this CPCB/hybrid record -> must recompute/invalidate
            affected_district_dates.add((d_id, target_date))

    # 4. Recompute every affected (district_id, target_date) across all historical dates
    for d_id, target_date in sorted(affected_district_dates):
        district = _resolve_district_record(conn, d_id)
        if district is None:
            continue
        verified_stations = load_stations_from_db(
            conn, district_id=d_id, verified_only=True
        ) or list_verified_stations_for_district(d_id)
        recompute_district_for_ist_date(conn, district, verified_stations, target_date)


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
        invalidated_cpcb_rows: List[sqlite3.Row] = []
        if version == 3:
            # Ensure legacy v2 databases that had old ingestion_sync_runs columns are upgraded seamlessly
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(ingestion_sync_runs)").fetchall()
            }
            if cols and "overall_status" not in cols:
                conn.executescript(
                    """
                    ALTER TABLE ingestion_sync_runs ADD COLUMN overall_status TEXT NOT NULL DEFAULT 'failed';
                    ALTER TABLE ingestion_sync_runs ADD COLUMN cpcb_status TEXT NOT NULL DEFAULT 'not_configured';
                    ALTER TABLE ingestion_sync_runs ADD COLUMN cpcb_error TEXT;
                    ALTER TABLE ingestion_sync_runs ADD COLUMN openaq_status TEXT NOT NULL DEFAULT 'not_configured';
                    ALTER TABLE ingestion_sync_runs ADD COLUMN openaq_error TEXT;
                    """
                )
            # Capture unitless/NA-unit CPCB observations before invalidating them so we know which dates are affected
            invalidated_cpcb_rows = conn.execute(
                """
                SELECT station_id, observed_at_utc
                FROM raw_station_observations
                WHERE source_provider = 'cpcb_ogd'
                  AND is_valid = 1
                  AND (
                      raw_unit IS NULL
                      OR TRIM(UPPER(raw_unit)) IN ('', 'NA', 'N/A', 'NONE', 'NULL', '-', 'UNKNOWN')
                  )
                """
            ).fetchall()

        conn.executescript(sql)
        conn.execute("PRAGMA foreign_keys = ON;")

        if version == 3:
            _reconcile_evaluations_after_migration_003(conn, invalidated_cpcb_rows)

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
    """Upsert station metadata while preserving any previously discovered openaq_location_id."""
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
                openaq_location_id = COALESCE(excluded.openaq_location_id, stations.openaq_location_id),
                openaq_verified_available = CASE
                    WHEN excluded.openaq_location_id IS NOT NULL THEN excluded.openaq_verified_available
                    ELSE stations.openaq_verified_available
                END
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


def update_station_openaq_mapping(
    conn: sqlite3.Connection,
    station_id: str,
    openaq_location_id: Optional[int],
    openaq_verified_available: bool,
) -> None:
    """Persist a discovered or validated OpenAQ location ID onto a station record."""
    conn.execute(
        """
        UPDATE stations
        SET openaq_location_id = COALESCE(?, openaq_location_id),
            openaq_verified_available = ?
        WHERE station_id = ?
        """,
        (openaq_location_id, 1 if openaq_verified_available else 0, station_id),
    )


def load_stations_from_db(
    conn: sqlite3.Connection,
    *,
    district_id: Optional[str] = None,
    verified_only: bool = True,
) -> List[StationRecord]:
    """Load StationRecord rows from SQLite (including persisted OpenAQ location IDs)."""
    clauses = []
    params: List[object] = []
    if district_id is not None:
        clauses.append("district_id = ?")
        params.append(district_id)
    if verified_only:
        clauses.append("is_verified_member = 1")
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM stations {where_sql} ORDER BY station_id ASC",
        tuple(params),
    ).fetchall()
    return [
        StationRecord(
            station_id=r["station_id"],
            district_id=r["district_id"],
            source_provider=r["source_provider"],
            external_station_id=r["external_station_id"],
            station_name=r["station_name"],
            operating_agency=r["operating_agency"],
            city=r["city"],
            state=r["state"],
            lat=float(r["lat"]) if r["lat"] is not None else None,
            lon=float(r["lon"]) if r["lon"] is not None else None,
            expected_pollutants=json.loads(r["expected_pollutants_json"]),
            is_verified_member=bool(r["is_verified_member"]),
            membership_basis=r["membership_basis"],
            openaq_location_id=int(r["openaq_location_id"]) if r["openaq_location_id"] is not None else None,
            openaq_verified_available=bool(r["openaq_verified_available"]),
        )
        for r in rows
    ]


def get_latest_station_observation_time(
    conn: sqlite3.Connection,
    station_id: str,
    source_provider: str,
) -> Optional[str]:
    """Return the latest valid observed_at_utc timestamp for a station and source_provider."""
    row = conn.execute(
        """
        SELECT MAX(observed_at_utc) AS max_obs
        FROM raw_station_observations
        WHERE station_id = ? AND source_provider = ? AND is_valid = 1
        """,
        (station_id, source_provider),
    ).fetchone()
    return row["max_obs"] if row and row["max_obs"] else None


def get_latest_sensor_observation_time(
    conn: sqlite3.Connection,
    station_id: str,
    sensor_id: int,
) -> Optional[str]:
    """Return the latest valid observed_at_utc timestamp for a specific OpenAQ sensor at a station."""
    sensor_prefix = f"openaq_sensor:{int(sensor_id)}:%"
    row = conn.execute(
        """
        SELECT MAX(observed_at_utc) AS max_obs
        FROM raw_station_observations
        WHERE station_id = ?
          AND source_provider = 'openaq_v3'
          AND is_valid = 1
          AND upstream_sensor_or_record_id LIKE ?
        """,
        (station_id, sensor_prefix),
    ).fetchone()
    return row["max_obs"] if row and row["max_obs"] else None


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
    # Remove any prior evaluation row for the same (station_id, evaluation_date) that had a different
    # source_provider or timestamp (e.g., when a hybrid evaluation is recomputed as openaq_v3 or none).
    conn.execute(
        """
        DELETE FROM station_aqi_evaluations
        WHERE station_id = ?
          AND evaluation_date = ?
          AND (evaluation_timestamp_utc != ? OR source_provider != ?)
        """,
        (ev.station_id, ev.evaluation_date, ev.evaluation_timestamp_utc, ev.source_provider),
    )
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


def get_latest_sync_run(conn: sqlite3.Connection) -> Optional[SyncRunRecord]:
    """Return the most recent row from ingestion_sync_runs, or None if no sync has run."""
    tbl = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ingestion_sync_runs'"
    ).fetchone()
    if not tbl:
        return None
    row = conn.execute(
        "SELECT * FROM ingestion_sync_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return SyncRunRecord(
        id=int(row["id"]),
        started_at_utc=row["started_at_utc"],
        completed_at_utc=row["completed_at_utc"],
        overall_status=row["overall_status"],
        cpcb_status=row["cpcb_status"],
        cpcb_observations_ingested=int(row["cpcb_observations_ingested"]),
        cpcb_error=row["cpcb_error"],
        openaq_status=row["openaq_status"],
        openaq_observations_ingested=int(row["openaq_observations_ingested"]),
        openaq_error=row["openaq_error"],
        daily_district_records_updated=int(row["daily_district_records_updated"]),
    )
