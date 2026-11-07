"""Environment configuration and settings for DistrictWatch backend."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from dotenv import load_dotenv

_BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BACKEND_DIR / ".env")
load_dotenv(_BACKEND_DIR.parent / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    data_gov_in_api_key: str = field(
        default_factory=lambda: os.getenv("DATA_GOV_IN_API_KEY", "").strip()
    )
    openaq_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAQ_API_KEY", "").strip()
    )
    xkdr_api_key: str = field(
        default_factory=lambda: os.getenv("XKDR_API_KEY", "").strip()
    )
    xkdr_api_base_url: str = field(
        default_factory=lambda: os.getenv(
            "XKDR_API_BASE_URL", "https://airquality.xkdr.org"
        ).strip()
    )
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", f"sqlite:///{_BACKEND_DIR / 'districtwatch.db'}"
        ).strip()
    )
    stale_threshold_hours: float = field(
        default_factory=lambda: float(os.getenv("STALE_THRESHOLD_HOURS", "6.0"))
    )
    initial_backfill_days: int = field(
        default_factory=lambda: int(os.getenv("INITIAL_BACKFILL_DAYS", "30"))
    )
    incremental_overlap_hours: int = field(
        default_factory=lambda: int(os.getenv("INCREMENTAL_SYNC_OVERLAP_HOURS", "3"))
    )
    min_request_interval_sec: float = field(
        default_factory=lambda: float(os.getenv("MIN_REQUEST_INTERVAL_SEC", "1.0"))
    )
    max_http_retries: int = field(
        default_factory=lambda: int(os.getenv("MAX_HTTP_RETRIES", "3"))
    )
    cors_origins: list[str] = field(
        default_factory=lambda: [
            origin.strip()
            for origin in os.getenv(
                "CORS_ORIGINS",
                "http://localhost:8080,http://127.0.0.1:8080,http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173",
            ).split(",")
            if origin.strip()
        ]
    )

    @property
    def cpcb_configured(self) -> bool:
        return bool(self.data_gov_in_api_key)

    @property
    def openaq_configured(self) -> bool:
        return bool(self.openaq_api_key)

    @property
    def xkdr_configured(self) -> bool:
        return bool(self.xkdr_api_key)


def get_settings() -> Settings:
    return Settings()
