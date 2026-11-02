"""Stage 6 tests: End-to-end SQLite -> Recompute -> FastAPI integration tests and optional skipped live API tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest
from fastapi.testclient import TestClient
from app.cli import recompute_district_history_window, seed_registry
from app.config import get_settings
from app.database import get_db_connection
from app.districts_registry import list_verified_stations_for_district
from app.ingestion.cpcb_adapter import fetch_cpcb_ogd_page
from app.ingestion.openaq_adapter import fetch_openaq_location
from app.main import app
from app.migrations import insert_raw_observations
from app.models import RawStationObservation


class TestStage6EndToEndAndSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_e2e.db")
        self.prev_db_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if self.prev_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.prev_db_url
        self.temp_dir.cleanup()

    def test_no_public_ingestion_endpoint_exposed(self) -> None:
        """Verify no unauthenticated POST /api/sync or /api/ingest endpoint is exposed."""
        for path in ("/api/sync", "/api/ingest", "/api/admin/sync"):
            resp = self.client.post(path)
            self.assertIn(resp.status_code, (404, 405))

    def test_end_to_end_sqlite_ingestion_to_api_snapshot_with_null_history_gaps(self) -> None:
        """Verify hourly observations in SQLite recompute into 30-day history with missing days preserved as null."""
        stations = list_verified_stations_for_district("lucknow")
        eval_day = datetime(2026, 11, 2, 23, 0, 0, tzinfo=timezone.utc)

        # Populate 24 hours of valid observations for 3 stations in Lucknow on 2026-11-02,
        # while leaving 2026-11-01 and earlier days empty to verify null gap preservation.
        obs_batch = []
        for st in stations[:3]:
            for h in range(24):
                ts = (eval_day - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for p_key, val, unit in [
                    ("PM2.5", 90.0, "µg/m³"),   # subIndex 200
                    ("PM10", 150.0, "µg/m³"),   # subIndex 133
                    ("NO2", 60.0, "µg/m³"),     # subIndex 75
                    ("SO2", 20.0, "µg/m³"),     # subIndex 25
                    ("O3", 50.0, "µg/m³"),      # subIndex 50
                    ("CO", 1.0, "mg/m³"),       # subIndex 50
                ]:
                    obs_batch.append(
                        RawStationObservation(
                            station_id=st.station_id,
                            source_provider="openaq_v3",
                            upstream_sensor_or_record_id=f"{st.station_id}:{p_key}:{ts}",
                            pollutant_key=p_key,  # type: ignore[arg-type]
                            observed_at_utc=ts,
                            window_hours=1,
                            raw_value_text=str(val),
                            raw_unit=unit,
                            value_kind="concentration",
                            normalized_concentration=val,
                            normalized_unit=unit,
                            calculated_sub_index=200 if p_key == "PM2.5" else 50,
                            is_valid=True,
                            validation_note="Verified in E2E test",
                            raw_payload_json='{"sensor":"e2e"}',
                            ingested_at_utc="2026-11-02T23:05:00Z",
                        )
                    )

        with get_db_connection() as conn:
            seed_registry(conn)
            insert_raw_observations(conn, obs_batch)
            recomputed = recompute_district_history_window(
                conn, anchor_date_str="2026-11-02", days=5
            )
            self.assertEqual(recomputed, 1)

        resp = self.client.get("/api/snapshots/lucknow?anchor_date=2026-11-02")
        self.assertEqual(resp.status_code, 200)
        snap = resp.json()

        self.assertEqual(snap["district"]["id"], "lucknow")
        self.assertEqual(snap["aqi"], 200)
        self.assertEqual(snap["dominant"], "PM2.5")
        self.assertEqual(snap["stations"], 3)
        # 3 of 6 verified stations in Lucknow reported all 6 pollutants for 24h -> coverage = 0.5
        self.assertEqual(snap["coverage"], 0.5)
        self.assertEqual(snap["quality"], "low")
        self.assertFalse(snap["isIllustrative"])

        # History has 30 entries; first 29 are null (never interpolated) and final entry (2026-11-02) is 200
        self.assertEqual(len(snap["history"]), 30)
        self.assertTrue(all(h["aqi"] is None for h in snap["history"][:-1]))
        self.assertEqual(snap["history"][-1], {"date": "2026-11-02", "aqi": 200})


class TestOptionalLiveUpstreamIntegration(unittest.TestCase):
    @unittest.skipUnless(
        bool(os.getenv("DATA_GOV_IN_API_KEY", "").strip()),
        "Skipped live CPCB data.gov.in integration test: DATA_GOV_IN_API_KEY is not configured in environment.",
    )
    def test_live_cpcb_ogd_fetch(self) -> None:
        settings = get_settings()
        payload = fetch_cpcb_ogd_page(settings.data_gov_in_api_key, state="Delhi", city="Delhi", limit=10)
        self.assertIn("records", payload)

    @unittest.skipUnless(
        bool(os.getenv("OPENAQ_API_KEY", "").strip()),
        "Skipped live OpenAQ v3 integration test: OPENAQ_API_KEY is not configured in environment.",
    )
    def test_live_openaq_location_fetch(self) -> None:
        settings = get_settings()
        payload = fetch_openaq_location(settings.openaq_api_key, 8118)
        self.assertIn("results", payload)


if __name__ == "__main__":
    unittest.main()
