"""HTTP API endpoints backed by SQLite persistence (read-only public surface)."""
from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query
from app.cli import seed_registry
from app.config import get_settings
from app.database import get_db_connection
from app.districts_registry import (
    get_district_mapping,
    list_target_districts,
    list_verified_stations_for_district,
)
from app.models import DailyDistrictHistoryRecord
from app.schemas import District, DistrictSnapshot, HealthResponse
from app.services.aggregator import build_snapshot_from_daily_history
from app.services.quality import is_reading_stale

router = APIRouter(prefix="/api", tags=["air-quality"])


def _load_district_history_from_db(district_id: str) -> List[DailyDistrictHistoryRecord]:
    with get_db_connection() as conn:
        seed_registry(conn)
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


def _get_health_status() -> tuple[Optional[str], bool]:
    """Return (last_ingested_at_utc, live_data_ready)."""
    settings = get_settings()
    with get_db_connection() as conn:
        seed_registry(conn)
        obs_row = conn.execute(
            "SELECT MAX(ingested_at_utc) AS last_ingested FROM raw_station_observations WHERE is_valid = 1"
        ).fetchone()
        last_ingested = obs_row["last_ingested"] if obs_row and obs_row["last_ingested"] else None

        hist_row = conn.execute(
            "SELECT MAX(updated_at_utc) AS latest_updated FROM daily_district_history WHERE district_aqi IS NOT NULL"
        ).fetchone()
        latest_updated = hist_row["latest_updated"] if hist_row and hist_row["latest_updated"] else None

    has_keys = settings.cpcb_configured or settings.openaq_configured
    has_fresh_reading = bool(
        latest_updated
        and not is_reading_stale(
            latest_updated, stale_threshold_hours=settings.stale_threshold_hours
        )
    )
    return last_ingested, (has_keys and has_fresh_reading)


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
    settings = get_settings()
    districts = list_target_districts()
    last_ingested, live_ready = _get_health_status()
    return HealthResponse(
        status="ok",
        phase="phase_3_nationwide_development",
        liveDataReady=live_ready,
        cpcbKeyConfigured=settings.cpcb_configured,
        openaqKeyConfigured=settings.openaq_configured,
        districtsTracked=len(districts),
        lastIngestedAt=last_ingested,
    )


@router.get("/districts", response_model=List[District])
def list_districts(
    state: Optional[str] = Query(None, description="Optional state/UT name filter"),
    q: Optional[str] = Query(None, description="Optional search query on district or state name"),
) -> List[District]:
    with get_db_connection() as conn:
        seed_registry(conn)
    return _filter_districts(list_target_districts(), state=state, q=q)


@router.get("/snapshots", response_model=List[DistrictSnapshot])
def list_snapshots(
    anchor_date: Optional[str] = Query(None, description="Optional YYYY-MM-DD evaluation date"),
    state: Optional[str] = Query(None, description="Optional state/UT name filter"),
    q: Optional[str] = Query(None, description="Optional search query on district or state name"),
) -> List[DistrictSnapshot]:
    districts = _filter_districts(list_target_districts(), state=state, q=q)
    return [_build_snapshot_for_district(d, anchor_date=anchor_date) for d in districts]


@router.get("/snapshots/{district_id}", response_model=DistrictSnapshot)
def get_snapshot(
    district_id: str,
    anchor_date: Optional[str] = Query(None, description="Optional YYYY-MM-DD evaluation date"),
) -> DistrictSnapshot:
    mapping = get_district_mapping(district_id)
    if mapping is None:
        raise HTTPException(
            status_code=404, detail=f"District '{district_id}' is not in the tracked set."
        )
    return _build_snapshot_for_district(mapping.district, anchor_date=anchor_date)
