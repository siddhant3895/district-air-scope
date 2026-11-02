"""Pydantic schemas matching the frontend TypeScript contracts in src/data/types.ts 1:1."""
from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field

PollutantKey = Literal["PM2.5", "PM10", "NO2", "SO2", "O3", "CO"]
QualityLevel = Literal["high", "medium", "low", "unavailable"]


class District(BaseModel):
    id: str = Field(..., description="Canonical URL-safe district slug")
    name: str = Field(..., description="Human-readable district name")
    state: str = Field(..., description="State or Union Territory name")
    lat: float = Field(..., description="District reference latitude (WGS84)")
    lon: float = Field(..., description="District reference longitude (WGS84)")


class Reading(BaseModel):
    date: str = Field(..., description="ISO calendar date (YYYY-MM-DD)")
    aqi: Optional[int] = Field(None, description="Daily mean Indian NAQI, or null if missing")


class Pollutant(BaseModel):
    key: PollutantKey
    value: Optional[float] = Field(None, description="Measured concentration in standard units, or null if unreported")
    unit: str = Field(..., description="Concentration unit (µg/m³ or mg/m³ for CO)")
    subIndex: Optional[int] = Field(None, description="CPCB NAQI sub-index for this pollutant, or null if unreported")


class DistrictSnapshot(BaseModel):
    district: District
    aqi: Optional[int] = Field(None, description="Headline Indian NAQI (worst sub-index), or null if unavailable")
    dominant: Optional[PollutantKey] = Field(None, description="Pollutant responsible for the highest sub-index")
    updatedAt: Optional[str] = Field(None, description="ISO-8601 UTC timestamp of latest station measurement")
    source: str = Field(..., description="Upstream data provider and license attribution")
    stations: int = Field(..., ge=0, description="Number of active reporting stations in the district")
    coverage: float = Field(..., ge=0.0, le=1.0, description="0-1 share of expected hourly readings received")
    quality: QualityLevel
    caveats: List[str] = Field(default_factory=list)
    pollutants: List[Pollutant]
    history: List[Reading]
    isIllustrative: bool = Field(
        False,
        description="False when served by the backend (even if unavailable); True only for static design mocks",
    )


class HealthResponse(BaseModel):
    status: str
    phase: str
    liveDataReady: bool
    cpcbKeyConfigured: bool
    openaqKeyConfigured: bool
    districtsTracked: int
    lastIngestedAt: Optional[str] = None
