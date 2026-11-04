"""Stage 7 tests: Verify CPCB OGD hourly poll evaluation, nationwide registry, and health readiness."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest
from fastapi.testclient import TestClient
from app.cli import recompute_district_history_window, seed_registry
from app.database import get_db_connection
from app.districts_registry import (
    TARGET_DISTRICTS,
    list_verified_stations_for_district,
)
from app.main import app
from app.services.aggregator import format_ist_date
from app.migrations import insert_raw_observations
from app.models import RawStationObservation


class TestStage7NationwideAndCpcbEvaluation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_stage7.db")
        self.prev_db_url = os.environ.get("DATABASE_URL")
        self.prev_cpcb_key = os.environ.get("DATA_GOV_IN_API_KEY")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if self.prev_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.prev_db_url
        if self.prev_cpcb_key is None:
            os.environ.pop("DATA_GOV_IN_API_KEY", None)
        else:
            os.environ["DATA_GOV_IN_API_KEY"] = self.prev_cpcb_key
        self.temp_dir.cleanup()

    def test_nationwide_registry_coverage_and_zero_station_districts(self) -> None:
        """Verify 30 nationwide districts and truthful 0-station representation for unmonitored regions."""
        self.assertEqual(len(TARGET_DISTRICTS), 30)
        self.assertEqual(len(list_verified_stations_for_district("nagpur")), 3)
        self.assertEqual(len(list_verified_stations_for_district("gurugram")), 4)
        self.assertEqual(len(list_verified_stations_for_district("pune")), 3)
        self.assertEqual(len(list_verified_stations_for_district("leh")), 0)
        self.assertEqual(len(list_verified_stations_for_district("lakshadweep")), 0)
        self.assertEqual(len(list_verified_stations_for_district("north-goa")), 0)

    def test_cpcb_ogd_accumulated_hourly_polls_evaluate_properly(self) -> None:
        """Verify that >=16 hourly CPCB OGD polls (window_hours=24/8, value_kind='sub_index') produce a valid station AQI."""
        nagpur_stations = list_verified_stations_for_district("nagpur")
        self.assertEqual(len(nagpur_stations), 3)
        now_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        today_str = format_ist_date(now_utc)

        # Insert 18 hourly CPCB OGD polls for Civil Lines, Nagpur across PM2.5 (sub_index=140), PM10 (110), NO2 (45), CO (60)
        obs = []
        st = nagpur_stations[0]
        for h in range(18):
            ts = (now_utc - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for p_key, win_h, sub_val in [
                ("PM2.5", 24, 140),
                ("PM10", 24, 110),
                ("NO2", 24, 45),
                ("CO", 8, 60),
            ]:
                obs.append(
                    RawStationObservation(
                        station_id=st.station_id,
                        source_provider="cpcb_ogd",
                        upstream_sensor_or_record_id=f"{st.station_id}:{p_key}:{ts}",
                        pollutant_key=p_key,  # type: ignore[arg-type]
                        observed_at_utc=ts,
                        window_hours=win_h,
                        raw_value_text=str(sub_val),
                        raw_unit=None,
                        value_kind="sub_index",
                        normalized_concentration=None,
                        normalized_unit=None,
                        calculated_sub_index=sub_val,
                        is_valid=True,
                        validation_note="CPCB OGD hourly poll",
                        raw_payload_json='{"source":"cpcb_ogd"}',
                        ingested_at_utc=ts,
                    )
                )

        with get_db_connection() as conn:
            seed_registry(conn)
            insert_raw_observations(conn, obs)
            recompute_district_history_window(conn, anchor_date_str=today_str, days=3)

        snap_resp = self.client.get(f"/api/snapshots/nagpur?anchor_date={today_str}")
        self.assertEqual(snap_resp.status_code, 200)
        snap = snap_resp.json()
        self.assertEqual(snap["aqi"], 140)
        self.assertEqual(snap["dominant"], "PM2.5")
        self.assertEqual(snap["stations"], 1)
        # Concentration value remains None for unitless CPCB OGD sub-index observations
        pm25_entry = next(p for p in snap["pollutants"] if p["key"] == "PM2.5")
        self.assertIsNone(pm25_entry["value"])
        self.assertEqual(pm25_entry["subIndex"], 140)

        # Verify /api/health stays False while provider sync status is never_run, then transitions
        # liveDataReady to True once a successful sync run is recorded with configured key and fresh readings
        os.environ["DATA_GOV_IN_API_KEY"] = "test-configured-key"
        health_before_sync = self.client.get("/api/health")
        self.assertEqual(health_before_sync.status_code, 200)
        self.assertFalse(health_before_sync.json()["liveDataReady"])

        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO ingestion_sync_runs (
                    started_at_utc, completed_at_utc, overall_status,
                    cpcb_status, cpcb_observations_ingested, openaq_status
                )
                VALUES (?, ?, 'succeeded', 'succeeded', ?, 'not_configured')
                """,
                (
                    now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    len(obs),
                ),
            )
            conn.commit()

        health_resp = self.client.get("/api/health")
        self.assertEqual(health_resp.status_code, 200)
        self.assertTrue(health_resp.json()["liveDataReady"])


if __name__ == "__main__":
    unittest.main()
