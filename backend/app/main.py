"""FastAPI application entrypoint for DistrictWatch."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import router as api_router
from app.cli import seed_registry
from app.config import get_settings
from app.database import get_db_connection


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="DistrictWatch API",
        description="District-level air quality monitoring backend for India (CPCB NAQI standard).",
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def _initialize_database_on_startup() -> None:
        with get_db_connection() as conn:
            seed_registry(conn)

    app.include_router(api_router)
    return app


app = create_app()
