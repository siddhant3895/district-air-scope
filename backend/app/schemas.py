"""Pydantic models matching the frontend TypeScript contracts in src/data/types.ts 1:1."""
from __future__ import annotations

from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field

PollutantKey = Literal["PM2.5", "PM10", "NO2", "SO2", "O3", "CO"]
QualityLevel = Literal["high", "medium", "low", "unavailable"]


class District(BaseModel):
    id: str = Field(..., description="URL-safe district slug, e.g. 'new-delhi'")
    name: str = Field(..., description="Official district display name")
    state: str = Field(..., description="State or Union Territory name")
    lat: float = Field(..., description="District centroid latitude")
    lon: float = Field(..., description="District centroid longitude")


class Reading(BaseModel):
    date: str = Field(..., description="ISO date string (YYYY-MM-DD in IST)")
    aqi: Optional[int] = Field(
        None,
        ge=0,
        le=500,
        description="Daily NAQI value (0-500) or null when missing; never interpolated",
    )


class Pollutant(BaseModel):
    key: PollutantKey
    value: Optional[float] = Field(
        None,
        ge=0.0,
        description="Measured concentration in standard units, or null when missing/invalid",
    )
    unit: str = Field(..., description="Concentration unit ('µg/m³' or 'mg/m³' for CO)")
    subIndex: Optional[int] = Field(
        None,
        ge=0,
        le=500,
        description="Calculated CPCB NAQI sub-index (0-500) or null when value is missing",
    )


class DistrictSnapshot(BaseModel):
    district: District
    aqi: Optional[int] = Field(
        None,
        ge=0,
        le=500,
        description="Overall NAQI = max of valid pollutant sub-indices when CPCB 3-pollutant/PM rule is met, else null",
    )
    dominant: Optional[PollutantKey] = Field(
        None,
        description="Pollutant with the highest valid sub-index, or null when AQI is unavailable",
    )
    updatedAt: Optional[str] = Field(
        None,
        description="ISO-8601 UTC timestamp of latest station observation, or null when unavailable",
    )
    source: str = Field(..., description="Data source attribution string")
    stations: int = Field(..., ge=0, description="Active reporting CAAQMS stations in district")
    coverage: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Share (0.0-1.0) of expected window pollutant-hours received (112h per station)",
    )
    quality: QualityLevel
    caveats: List[str] = Field(default_factory=list)
    pollutants: List[Pollutant]
    history: List[Reading]
    isIllustrative: bool = Field(
        False,
        description="Always False for backend API responses; True only for frontend mock fallback",
    )


class ProviderHealthStatus(BaseModel):
    configured: bool
    lastSyncStatus: str = Field(
        "never_run",
        description="Status of latest sync attempt for this provider: succeeded | failed | not_configured | never_run",
    )
    lastSyncError: Optional[str] = None
    lastObservationAt: Optional[str] = Field(
        None, description="ISO-8601 UTC timestamp of most recent valid observation from this provider"
    )
    hasFreshData: bool = Field(
        False, description="True if provider has valid observations within the freshness threshold"
    )


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    phase: str = "phase_3_nationwide_development"
    liveDataReady: bool
    cpcbKeyConfigured: bool
    openaqKeyConfigured: bool
    xkdrKeyConfigured: Optional[bool] = False
    districtsTracked: int
    lastIngestedAt: Optional[str] = None
    lastSyncStatus: str = "never_run"
    freshDistrictsCount: int = 0
    providers: Optional[Dict[str, ProviderHealthStatus]] = None
