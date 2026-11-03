"""HTTP API endpoints backed by SQLite persistence (strictly read-only public surface; no reseeding on GET)."""
from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query
from app.config import get_settings
from app.database import get_db_connection
from app.districts_registry import (
    get_district_mapping,
    list_target_districts,
    list_verified_stations_for_district,
)
from app.migrations import get_latest_sync_run
from app.models import DailyDistrictHistoryRecord
from app.schemas import District, DistrictSnapshot, HealthResponse, ProviderHealthStatus
from app.services.aggregator import build_snapshot_from_daily_history
from app.services.quality import is_reading_stale

router = APIRouter(prefix="/api", tags=["air-quality"])


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _load_district_history_from_db(district_id: str) -> List[DailyDistrictHistoryRecord]:
    """Load daily district history rows without mutating or reseeding the database."""
    with get_db_connection() as conn:
        if not _table_exists(conn, "daily_district_history"):
            return []
        rows = conn.execute(
            """
            SELECT * FROM daily_district_history
            WHERE district_id = ?
            ORDER BY date ASC
            """,
            (district_id,),
        ).fetchall()
        return [
            DailyDistrictHistoryRecord(
                district_id=r["district_id"],
                date=r["date"],
                district_aqi=r["district_aqi"],
                dominant_pollutant=r["dominant_pollutant"],
                updated_at_utc=r["updated_at_utc"],
                source_attribution=r["source_attribution"],
                reporting_stations=int(r["reporting_stations"]),
                registered_stations=int(r["registered_stations"]),
                observed_station_pollutant_hours=int(r["observed_station_pollutant_hours"]),
                expected_station_pollutant_hours=int(r["expected_station_pollutant_hours"]),
                coverage=float(r["coverage"]),
                quality=r["quality"],
                pollutants_json=r["pollutants_json"],
                caveats_json=r["caveats_json"],
            )
            for r in rows
        ]


def _filter_districts(
    districts: List[District],
    state: Optional[str] = None,
    q: Optional[str] = None,
) -> List[District]:
    filtered = districts
    if state and state.strip():
        s_norm = state.strip().lower()
        filtered = [d for d in filtered if d.state.lower() == s_norm]
    if q and q.strip():
        q_norm = q.strip().lower()
        filtered = [
            d
            for d in filtered
            if q_norm in d.name.lower() or q_norm in d.state.lower() or q_norm in d.id.lower()
        ]
    return filtered


def _build_snapshot_for_district(
    district: District, anchor_date: Optional[str] = None
) -> DistrictSnapshot:
    records = _load_district_history_from_db(district.id)
    snap = build_snapshot_from_daily_history(
        district=district,
        history_records=records,
        anchor_date_str=anchor_date,
    )
    verified_stations = list_verified_stations_for_district(district.id)
    if len(verified_stations) == 0 and snap.quality == "unavailable":
        snap.source = "No CPCB / SPCB CAAQMS stations installed in district"
        snap.caveats = [
            "No continuous ambient air quality monitoring stations (CAAQMS) are installed in this district."
        ]
    return snap


@router.get("/health", response_model=HealthResponse)
def get_health() -> HealthResponse:
    """Return pipeline health, per-provider status, and data freshness without mutating DB state."""
    settings = get_settings()
    districts = list_target_districts()
    last_ingested: Optional[str] = None
    last_sync_status = "never_run"
    fresh_districts_count = 0

    cpcb_last_obs: Optional[str] = None
    openaq_last_obs: Optional[str] = None
    cpcb_sync_status = "not_configured" if not settings.cpcb_configured else "never_run"
    openaq_sync_status = "not_configured" if not settings.openaq_configured else "never_run"
    cpcb_sync_err: Optional[str] = None
    openaq_sync_err: Optional[str] = None

    with get_db_connection() as conn:
        if _table_exists(conn, "raw_station_observations"):
            row_all = conn.execute(
                "SELECT MAX(ingested_at_utc) AS last_ingested FROM raw_station_observations WHERE is_valid = 1"
            ).fetchone()
            if row_all and row_all["last_ingested"]:
                last_ingested = row_all["last_ingested"]

            row_cpcb = conn.execute(
                "SELECT MAX(observed_at_utc) AS max_obs FROM raw_station_observations WHERE source_provider = 'cpcb_ogd' AND is_valid = 1"
            ).fetchone()
            if row_cpcb and row_cpcb["max_obs"]:
                cpcb_last_obs = row_cpcb["max_obs"]

            row_oaq = conn.execute(
                "SELECT MAX(observed_at_utc) AS max_obs FROM raw_station_observations WHERE source_provider = 'openaq_v3' AND is_valid = 1"
            ).fetchone()
            if row_oaq and row_oaq["max_obs"]:
                openaq_last_obs = row_oaq["max_obs"]

        if _table_exists(conn, "daily_district_history"):
            rows_hist = conn.execute(
                """
                SELECT district_id, MAX(date) AS latest_date, updated_at_utc, district_aqi
                FROM daily_district_history
                WHERE district_aqi IS NOT NULL
                GROUP BY district_id
                """
            ).fetchall()
            for r in rows_hist:
                upd = r["updated_at_utc"]
                if upd and not is_reading_stale(
                    upd, stale_threshold_hours=settings.stale_threshold_hours
                ):
                    fresh_districts_count += 1

        sync_run = get_latest_sync_run(conn)
        if sync_run is not None:
            last_sync_status = sync_run.overall_status
            cpcb_sync_status = sync_run.cpcb_status
            cpcb_sync_err = sync_run.cpcb_error
            openaq_sync_status = sync_run.openaq_status
            openaq_sync_err = sync_run.openaq_error

    cpcb_fresh = bool(
        cpcb_last_obs
        and not is_reading_stale(
            cpcb_last_obs, stale_threshold_hours=settings.stale_threshold_hours
        )
    )
    openaq_fresh = bool(
        openaq_last_obs
        and not is_reading_stale(
            openaq_last_obs, stale_threshold_hours=settings.stale_threshold_hours
        )
    )

    providers: Dict[str, ProviderHealthStatus] = {
        "cpcb_ogd": ProviderHealthStatus(
            configured=settings.cpcb_configured,
            lastSyncStatus=cpcb_sync_status,
            lastSyncError=cpcb_sync_err,
            lastObservationAt=cpcb_last_obs,
            hasFreshData=cpcb_fresh,
        ),
        "openaq_v3": ProviderHealthStatus(
            configured=settings.openaq_configured,
            lastSyncStatus=openaq_sync_status,
            lastSyncError=openaq_sync_err,
            lastObservationAt=openaq_last_obs,
            hasFreshData=openaq_fresh,
        ),
    }

    # liveDataReady is True ONLY when:
    # 1. At least one configured provider has completed a successful sync (lastSyncStatus == "succeeded")
    #    and has fresh valid observations (hasFreshData == True), AND
    # 2. At least one district has a fresh, non-stale daily AQI evaluation (fresh_districts_count > 0).
    any_provider_healthy = any(
        p.configured and p.lastSyncStatus == "succeeded" and p.hasFreshData
        for p in providers.values()
    )
    live_data_ready = bool(any_provider_healthy and fresh_districts_count > 0)

    return HealthResponse(
        status="ok",
        phase="phase_3_nationwide_development",
        liveDataReady=live_data_ready,
        cpcbKeyConfigured=settings.cpcb_configured,
        openaqKeyConfigured=settings.openaq_configured,
        districtsTracked=len(districts),
        lastIngestedAt=last_ingested,
        lastSyncStatus=last_sync_status,
        freshDistrictsCount=fresh_districts_count,
        providers=providers,
    )


@router.get("/districts", response_model=List[District])
def list_districts(
    state: Optional[str] = Query(None, description="Optional state/UT name filter"),
    q: Optional[str] = Query(None, description="Optional search query on district or state name"),
) -> List[District]:
    return _filter_districts(list_target_districts(), state=state, q=q)


@router.get("/snapshots", response_model=List[DistrictSnapshot])
def list_snapshots(
    anchor_date: Optional[str] = Query(None, description="Optional YYYY-MM-DD evaluation date in IST"),
    state: Optional[str] = Query(None, description="Optional state/UT name filter"),
    q: Optional[str] = Query(None, description="Optional search query on district or state name"),
) -> List[DistrictSnapshot]:
    districts = _filter_districts(list_target_districts(), state=state, q=q)
    return [_build_snapshot_for_district(d, anchor_date=anchor_date) for d in districts]


@router.get("/snapshots/{district_id}", response_model=DistrictSnapshot)
def get_snapshot(
    district_id: str,
    anchor_date: Optional[str] = Query(None, description="Optional YYYY-MM-DD evaluation date in IST"),
) -> DistrictSnapshot:
    mapping = get_district_mapping(district_id)
    if mapping is None:
        raise HTTPException(
            status_code=404, detail=f"District '{district_id}' is not in the tracked set."
        )
    return _build_snapshot_for_district(mapping.district, anchor_date=anchor_date)
