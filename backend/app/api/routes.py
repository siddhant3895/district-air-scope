"""HTTP API endpoints backed by SQLite persistence (read-only public surface)."""
from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query
from app.cli import seed_registry
from app.config import get_settings
from app.database import get_db_connection
from app.districts_registry import get_district_mapping, list_target_districts
from app.models import DailyDistrictHistoryRecord
from app.schemas import District, DistrictSnapshot, HealthResponse
from app.services.aggregator import build_snapshot_from_daily_history

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


def _get_latest_ingested_timestamp() -> Optional[str]:
    with get_db_connection() as conn:
        seed_registry(conn)
        row = conn.execute(
            "SELECT MAX(ingested_at_utc) AS last_ingested FROM raw_station_observations WHERE is_valid = 1"
        ).fetchone()
        return row["last_ingested"] if row and row["last_ingested"] else None


@router.get("/health", response_model=HealthResponse)
def get_health() -> HealthResponse:
    settings = get_settings()
    districts = list_target_districts()
    last_ingested = _get_latest_ingested_timestamp()
    return HealthResponse(
        status="ok",
        phase="phase_2_persistence_and_ingestion_pipeline",
        liveDataReady=False,
        cpcbKeyConfigured=settings.cpcb_configured,
        openaqKeyConfigured=settings.openaq_configured,
        districtsTracked=len(districts),
        lastIngestedAt=last_ingested,
    )


@router.get("/districts", response_model=List[District])
def list_districts() -> List[District]:
    with get_db_connection() as conn:
        seed_registry(conn)
    return list_target_districts()


@router.get("/snapshots", response_model=List[DistrictSnapshot])
def list_snapshots(
    anchor_date: Optional[str] = Query(None, description="Optional YYYY-MM-DD evaluation date")
) -> List[DistrictSnapshot]:
    snapshots: List[DistrictSnapshot] = []
    for district in list_target_districts():
        records = _load_district_history_from_db(district.id)
        snapshots.append(
            build_snapshot_from_daily_history(
                district=district,
                history_records=records,
                anchor_date_str=anchor_date,
            )
        )
    return snapshots


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
    records = _load_district_history_from_db(district_id)
    return build_snapshot_from_daily_history(
        district=mapping.district,
        history_records=records,
        anchor_date_str=anchor_date,
    )
