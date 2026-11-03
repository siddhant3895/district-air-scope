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


def _deserialize_station_evaluation_row(row: sqlite3.Row) -> StationAqiEvaluation:
    """Reconstruct a StationAqiEvaluation domain object from a station_aqi_evaluations DB row."""
    from app.models import StationPollutantEvaluation

    raw_pollutants = json.loads(row["pollutants_json"] or "{}")
    pollutant_evals = {}
    for k, v in raw_pollutants.items():
        pollutant_evals[k] = StationPollutantEvaluation(
            pollutant_key=v["pollutant_key"],
            window_hours=int(v["window_hours"]),
            required_valid_hours=int(v["required_valid_hours"]),
            observed_valid_hours=int(v["observed_valid_hours"]),
            expected_hours=int(v["expected_hours"]),
            meets_completeness=bool(v["meets_completeness"]),
            mean_concentration=float(v["mean_concentration"]) if v.get("mean_concentration") is not None else None,
            unit=v["unit"],
            sub_index=int(v["sub_index"]) if v.get("sub_index") is not None else None,
            value_kind_used=v["value_kind_used"],
        )
    rejection_reasons = json.loads(row["rejection_reasons_json"] or "[]")
    return StationAqiEvaluation(
        station_id=row["station_id"],
        district_id=row["district_id"],
        evaluation_timestamp_utc=row["evaluation_timestamp_utc"],
        evaluation_date=row["evaluation_date"],
        source_provider=row["source_provider"],
        station_aqi=int(row["station_aqi"]) if row["station_aqi"] is not None else None,
        dominant_pollutant=row["dominant_pollutant"],
        meets_16h_completeness=bool(row["meets_16h_completeness"]),
        meets_3_pollutant_pm_rule=bool(row["meets_3_pollutant_pm_rule"]),
        is_valid_station_aqi=bool(row["is_valid_station_aqi"]),
        valid_pollutant_count=int(row["valid_pollutant_count"]),
        observed_pollutant_hours=int(row["observed_pollutant_hours"]),
        expected_pollutant_hours=int(row["expected_pollutant_hours"]),
        pollutant_evaluations=pollutant_evals,
        rejection_reasons=rejection_reasons,
    )


def _has_unitless_cpcb_in_station_window(
    conn: sqlite3.Connection,
    station_id: str,
    win_start_utc: str,
    win_end_utc: str,
) -> bool:
    """Return True if `station_id` has any unitless/NA-unit CPCB observation in `(win_start_utc, win_end_utc]`."""
    row = conn.execute(
        """
        SELECT 1 FROM raw_station_observations
        WHERE station_id = ?
          AND source_provider = 'cpcb_ogd'
          AND (
              raw_unit IS NULL
              OR TRIM(UPPER(raw_unit)) IN ('', 'NA', 'N/A', 'NONE', 'NULL', '-', 'UNKNOWN')
          )
          AND observed_at_utc > ?
          AND observed_at_utc <= ?
        LIMIT 1
        """,
        (station_id, win_start_utc, win_end_utc),
    ).fetchone()
    return row is not None


def _has_valid_explicit_unit_cpcb_in_station_window(
    conn: sqlite3.Connection,
    station_id: str,
    win_start_utc: str,
    win_end_utc: str,
) -> bool:
    """Return True if `station_id` has at least one valid explicit-unit CPCB observation in `(win_start_utc, win_end_utc]`."""
    row = conn.execute(
        """
        SELECT 1 FROM raw_station_observations
        WHERE station_id = ?
          AND source_provider = 'cpcb_ogd'
          AND is_valid = 1
          AND raw_unit IS NOT NULL
          AND TRIM(UPPER(raw_unit)) NOT IN ('', 'NA', 'N/A', 'NONE', 'NULL', '-', 'UNKNOWN')
          AND observed_at_utc > ?
          AND observed_at_utc <= ?
        LIMIT 1
        """,
        (station_id, win_start_utc, win_end_utc),
    ).fetchone()
    return row is not None


def _reconcile_evaluations_after_migration_003(
    conn: sqlite3.Connection,
    invalidated_cpcb_rows: List[sqlite3.Row],
) -> None:
    """Station-level, 24-hour evaluation-window-accurate reconciliation for Migration 003.

    1. Verifies each `station_aqi_evaluations` row per `station_id` and per its exact 24-hour
       evaluation window `(evaluation_timestamp_utc - 24h, evaluation_timestamp_utc]`:
       - Unrelated unitless CPCB observations outside `(win_start, win_end]` cannot trigger
         invalidation, and unrelated valid CPCB observations outside `(win_start, win_end]`
         or belonging to a different station in the same district cannot suppress reconciliation.
       - A station evaluation with `source_provider IN ('cpcb_ogd', 'hybrid_cpcb_openaq')` is
         preserved if and only if zero unitless CPCB observations exist for that `station_id`
         inside `(win_start, win_end]` AND valid explicit-unit CPCB observations exist for that
         `station_id` inside `(win_start, win_end]`.
       - Otherwise, that specific station is re-evaluated over its exact `(win_start, win_end]`
         window from remaining valid observations (recalculating AQI if OpenAQ/explicit-unit
         observations suffice, or invalidating the station evaluation if not).
    2. Reconciles `daily_district_history` across all historical dates (including >30 days ago)
       from the resulting station evaluations so hybrid or multi-station district records never
       retain stale AQIs while preserving unaffected valid stations in the same district.
    """
    all_stations_by_id: Dict[str, StationRecord] = {
        s.station_id: s for s in REVIEWED_STATIONS
    }
    for s in load_stations_from_db(conn, verified_only=False):
        all_stations_by_id[s.station_id] = s

    districts_dates_to_reaggregate: Set[Tuple[str, str]] = set()

    # Step 1: Verify and reconcile every persisted station_aqi_evaluations row per station_id and 24h window
    eval_rows = conn.execute(
        "SELECT * FROM station_aqi_evaluations ORDER BY district_id ASC, evaluation_date ASC, station_id ASC"
    ).fetchall()

    for ev_row in eval_rows:
        prov = ev_row["source_provider"]
        if prov not in ("cpcb_ogd", "hybrid_cpcb_openaq"):
            continue

        st_id = ev_row["station_id"]
        d_id = ev_row["district_id"]
        eval_date = ev_row["evaluation_date"]
        eval_end_utc = _parse_utc(ev_row["evaluation_timestamp_utc"])
        eval_start_utc = eval_end_utc - timedelta(hours=24)
        win_start = eval_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        win_end = eval_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        has_unitless = _has_unitless_cpcb_in_station_window(conn, st_id, win_start, win_end)
        has_valid_explicit = _has_valid_explicit_unit_cpcb_in_station_window(
            conn, st_id, win_start, win_end
        )

        # Preserve if this station's exact 24h window had zero unitless CPCB rows and is backed by valid explicit-unit CPCB data
        if not has_unitless and has_valid_explicit:
            continue

        # Re-evaluate this specific station over its exact 24h evaluation window (win_start, win_end]
        st_record = all_stations_by_id.get(st_id)
        if st_record is not None:
            st_obs = [
                o
                for o in load_station_observations_from_db(conn, [st_id], win_start, win_end)
                if o.is_valid
            ]
            new_ev = evaluate_station_window(
                station=st_record,
                observations=st_obs,
                evaluation_end_utc=eval_end_utc,
            )
            if new_ev.evaluation_date != eval_date:
                new_ev = replace(new_ev, evaluation_date=eval_date)
            upsert_station_evaluation(conn, new_ev)
        else:
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
                    rejection_reasons_json = '["Invalidated by migration 003: station lacks verified CPCB observations in window."]'
                WHERE id = ?
                """,
                (ev_row["id"],),
            )

        districts_dates_to_reaggregate.add((d_id, eval_date))
        ist_eval_date = format_ist_date(eval_end_utc)
        if ist_eval_date != eval_date:
            districts_dates_to_reaggregate.add((d_id, ist_eval_date))

    # Step 2: Inspect all daily_district_history rows and reconcile any affected or out-of-sync (district_id, date)
    daily_rows = conn.execute(
        "SELECT * FROM daily_district_history ORDER BY district_id ASC, date ASC"
    ).fetchall()
    daily_by_key: Dict[Tuple[str, str], sqlite3.Row] = {
        (r["district_id"], r["date"]): r for r in daily_rows
    }

    all_candidate_keys = set(daily_by_key.keys()) | districts_dates_to_reaggregate

    for d_id, target_date in sorted(all_candidate_keys):
        district = _resolve_district_record(conn, d_id)
        if district is None:
            continue
        verified_stations = load_stations_from_db(
            conn, district_id=d_id, verified_only=True
        ) or list_verified_stations_for_district(d_id)

        current_eval_rows = conn.execute(
            """
            SELECT * FROM station_aqi_evaluations
            WHERE district_id = ? AND evaluation_date = ?
            ORDER BY station_id ASC
            """,
            (d_id, target_date),
        ).fetchall()

        daily_row = daily_by_key.get((d_id, target_date))

        if current_eval_rows:
            station_evals = [_deserialize_station_evaluation_row(r) for r in current_eval_rows]
            valid_evals = [
                ev for ev in station_evals if ev.is_valid_station_aqi and ev.station_aqi is not None
            ]
            expected_reporting = len(valid_evals)
            expected_aqi = (
                int(round(sum(ev.station_aqi for ev in valid_evals if ev.station_aqi is not None) / expected_reporting))
                if expected_reporting > 0
                else None
            )

            out_of_sync = False
            if daily_row is not None:
                if (
                    daily_row["district_aqi"] != expected_aqi
                    or int(daily_row["reporting_stations"]) != expected_reporting
                ):
                    out_of_sync = True

            if (d_id, target_date) in districts_dates_to_reaggregate or out_of_sync:
                latest_valid_ts: Optional[str] = None
                for ev in valid_evals:
                    if latest_valid_ts is None or ev.evaluation_timestamp_utc > latest_valid_ts:
                        latest_valid_ts = ev.evaluation_timestamp_utc
                ref_now_utc = (
                    _parse_utc(latest_valid_ts)
                    if latest_valid_ts is not None
                    else ist_date_bounds_in_utc(target_date)[1]
                )
                reagg_rec = aggregate_district_for_date(
                    district=district,
                    registered_stations=verified_stations,
                    station_evaluations=station_evals,
                    evaluation_date=target_date,
                    updated_at_utc=latest_valid_ts,
                    source_attribution="auto",
                    now_utc=ref_now_utc,
                )
                upsert_daily_district_history(conn, reagg_rec)
        elif daily_row is not None:
            # No station_aqi_evaluations rows exist for (d_id, target_date), but a daily_district_history row exists.
            # Check if this daily record claimed CPCB/hybrid provenance or depended on unitless CPCB observations
            # within its 24-hour evaluation window anchored at updated_at_utc (or IST day end).
            ist_start_utc, ist_end_utc = ist_date_bounds_in_utc(target_date)
            if daily_row["updated_at_utc"]:
                eval_end_utc = _parse_utc(daily_row["updated_at_utc"])
            else:
                eval_end_utc = ist_end_utc
            eval_start_utc = eval_end_utc - timedelta(hours=24)
            win_start = eval_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            win_end = eval_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

            any_unitless_in_win = any(
                _has_unitless_cpcb_in_station_window(conn, s.station_id, win_start, win_end)
                for s in verified_stations
            )
            any_valid_explicit_in_win = any(
                _has_valid_explicit_unit_cpcb_in_station_window(conn, s.station_id, win_start, win_end)
                for s in verified_stations
            )
            claims_cpcb = "data.gov.in" in (daily_row["source_attribution"] or "")

            if any_unitless_in_win or (claims_cpcb and not any_valid_explicit_in_win):
                station_ids = [s.station_id for s in verified_stations]
                obs = load_station_observations_from_db(conn, station_ids, win_start, win_end)
                station_evals = []
                latest_obs_utc: Optional[str] = None
                for st in verified_stations:
                    st_obs = [o for o in obs if o.station_id == st.station_id and o.is_valid]
                    for o in st_obs:
                        if latest_obs_utc is None or o.observed_at_utc > latest_obs_utc:
                            latest_obs_utc = o.observed_at_utc
                    ev = evaluate_station_window(
                        station=st,
                        observations=st_obs,
                        evaluation_end_utc=eval_end_utc,
                    )
                    if ev.evaluation_date != target_date:
                        ev = replace(ev, evaluation_date=target_date)
                    upsert_station_evaluation(conn, ev)
                    station_evals.append(ev)

                reagg_rec = aggregate_district_for_date(
                    district=district,
                    registered_stations=verified_stations,
                    station_evaluations=station_evals,
                    evaluation_date=target_date,
                    updated_at_utc=latest_obs_utc,
                    source_attribution="auto",
                    now_utc=eval_end_utc,
                )
                upsert_daily_district_history(conn, reagg_rec)


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
