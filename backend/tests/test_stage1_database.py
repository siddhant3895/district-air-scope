"""Stage 1 tests: Verify SQLite schema migrations, audit fields, and persistence constraints."""
from __future__ import annotations

import json
import unittest
from app.database import create_connection
from app.migrations import (
    insert_raw_observations,
    run_migrations,
    upsert_daily_district_history,
    upsert_districts,
    upsert_stations,
)
from app.models import DailyDistrictHistoryRecord, RawStationObservation, StationRecord
from app.schemas import District


class TestStage1DatabaseAndMigrations(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = create_connection(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_idempotent_migrations_and_table_creation(self) -> None:
        applied_first = run_migrations(self.conn)
        self.assertEqual(applied_first, [1, 2, 3])

        # Second run is a no-op
        applied_second = run_migrations(self.conn)
        self.assertEqual(applied_second, [])

        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertTrue(
            {
                "schema_migrations",
                "districts",
                "stations",
                "raw_station_observations",
                "station_aqi_evaluations",
                "daily_district_history",
                "ingestion_sync_runs",
            }.issubset(tables)
        )

    def test_raw_observation_audit_trail_and_upsert(self) -> None:
        run_migrations(self.conn)
        district = District(id="new-delhi", name="New Delhi", state="Delhi", lat=28.61, lon=77.21)
        upsert_districts(self.conn, [district])

        station = StationRecord(
            station_id="cpcb:new-delhi:mandir-marg",
            district_id="new-delhi",
            source_provider="cpcb_ogd",
            external_station_id="Mandir Marg, Delhi - DPCC",
            station_name="Mandir Marg",
            operating_agency="DPCC",
            city="Delhi",
            state="Delhi",
            lat=28.6364,
            lon=77.2011,
            expected_pollutants=["PM2.5", "PM10", "NO2", "SO2", "O3", "CO"],
            is_verified_member=True,
            membership_basis="Verified inside New Delhi district (Connaught Place / Mandir Marg subdivision).",
        )
        upsert_stations(self.conn, [station])

        raw_payload = {
            "state": "Delhi",
            "city": "Delhi",
            "station": "Mandir Marg, Delhi - DPCC",
            "pollutant_id": "PM2.5",
            "pollutant_avg": "218",
            "last_update": "02-11-2026 11:00:00",
        }
        obs = RawStationObservation(
            station_id=station.station_id,
            source_provider="cpcb_ogd",
            upstream_sensor_or_record_id="Mandir Marg, Delhi - DPCC:PM2.5",
            pollutant_key="PM2.5",
            observed_at_utc="2026-11-02T05:30:00Z",
            window_hours=24,
            raw_value_text="218",
            raw_unit=None,
            value_kind="sub_index",
            normalized_concentration=None,
            normalized_unit=None,
            calculated_sub_index=218,
            is_valid=True,
            validation_note="CPCB OGD pollutant_avg treated as dimensionless 24h sub-index (no concentration unit field).",
            raw_payload_json=json.dumps(raw_payload),
            ingested_at_utc="2026-11-02T06:00:00Z",
        )
        inserted = insert_raw_observations(self.conn, [obs])
        self.assertEqual(inserted, 1)

        row = self.conn.execute(
            "SELECT * FROM raw_station_observations WHERE station_id = ?",
            (station.station_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["raw_value_text"], "218")
        self.assertIsNone(row["raw_unit"])
        self.assertEqual(row["value_kind"], "sub_index")
        self.assertIsNone(row["normalized_concentration"])
        self.assertEqual(row["calculated_sub_index"], 218)
        self.assertEqual(json.loads(row["raw_payload_json"])["station"], "Mandir Marg, Delhi - DPCC")

        # Also verify daily_district_history persistence
        history_rec = DailyDistrictHistoryRecord(
            district_id="new-delhi",
            date="2026-11-02",
            district_aqi=None,
            dominant_pollutant=None,
            updated_at_utc="2026-11-02T05:30:00Z",
            source_attribution="CPCB CAAQMS via data.gov.in (GODL-India)",
            reporting_stations=1,
            registered_stations=6,
            observed_station_pollutant_hours=24,
            expected_station_pollutant_hours=672,
            coverage=round(24 / 672, 4),
            quality="unavailable",
            pollutants_json="[]",
            caveats_json='["Fewer than 3 pollutants reported."]',
        )
        upsert_daily_district_history(self.conn, history_rec)
        hist_row = self.conn.execute(
            "SELECT * FROM daily_district_history WHERE district_id = 'new-delhi' AND date = '2026-11-02'"
        ).fetchone()
        self.assertEqual(hist_row["registered_stations"], 6)
        self.assertEqual(hist_row["expected_station_pollutant_hours"], 672)


if __name__ == "__main__":
    unittest.main()
