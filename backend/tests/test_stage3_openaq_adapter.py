"""Stage 3 tests: Verify OpenAQ v3 sensor discovery, availability validation, and hourly coverage filtering."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from app.ingestion.openaq_adapter import (
    parse_openaq_hourly_payload,
    validate_openaq_location,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class TestStage3OpenAqDiscoveryAndValidation(unittest.TestCase):
    def test_location_discovery_and_availability_validation(self) -> None:
        payload = json.loads((FIXTURES_DIR / "openaq_locations_sample.json").read_text(encoding="utf-8"))
        results = payload["results"]
        ref_now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)

        # 1. Valid reference monitor (Mandir Marg, location 8118)
        valid_loc = validate_openaq_location(results[0], reference_now_utc=ref_now)
        self.assertTrue(valid_loc.is_available)
        self.assertIsNone(valid_loc.rejection_reason)
        self.assertEqual(len(valid_loc.valid_sensors), 6)
        self.assertEqual(valid_loc.valid_sensors["NO2"].raw_unit, "ppb")
        self.assertEqual(valid_loc.valid_sensors["CO"].raw_unit, "ppm")
        self.assertIn("GODL-India", valid_loc.license_attribution)

        # 2. Defunct historical monitor (inactive since 2024) -> rejected
        stale_loc = validate_openaq_location(results[1], reference_now_utc=ref_now)
        self.assertFalse(stale_loc.is_available)
        self.assertIn("inactive", stale_loc.rejection_reason or "")

        # 3. Non-reference monitor (isMonitor=False) -> rejected
        non_ref_loc = validate_openaq_location(results[2], reference_now_utc=ref_now)
        self.assertFalse(non_ref_loc.is_available)
        self.assertIn("isMonitor=False", non_ref_loc.rejection_reason or "")

    def test_hourly_payload_coverage_and_negative_value_filtering(self) -> None:
        payload = json.loads((FIXTURES_DIR / "openaq_hours_sample.json").read_text(encoding="utf-8"))
        obs = parse_openaq_hourly_payload(
            station_id="cpcb:new-delhi:mandir-marg",
            sensor_id=23534,
            expected_pollutant="PM2.5",
            payload=payload,
            ingested_at_utc="2026-11-02T06:00:00Z",
        )
        # Record 2 has observedCount=0 / percentCoverage=0.0 -> skipped completely
        self.assertEqual(len(obs), 2)

        # Record 1 (90.0 µg/m³) -> valid concentration with CPCB sub-index 200
        self.assertTrue(obs[0].is_valid)
        self.assertEqual(obs[0].value_kind, "concentration")
        self.assertEqual(obs[0].normalized_concentration, 90.0)
        self.assertEqual(obs[0].calculated_sub_index, 200)

        # Record 3 (-12.5 µg/m³) -> retained in raw_station_observations as invalid for auditability
        self.assertFalse(obs[1].is_valid)
        self.assertEqual(obs[1].value_kind, "invalid")
        self.assertIsNone(obs[1].normalized_concentration)


if __name__ == "__main__":
    unittest.main()
