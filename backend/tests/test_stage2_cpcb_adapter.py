"""Stage 2 tests: Verify CPCB adapter parsing, reviewed station membership exclusions, and safe unit semantics."""
from __future__ import annotations

import json
from pathlib import Path
import unittest
from app.districts_registry import (
    REVIEWED_STATIONS,
    list_verified_stations_for_district,
)
from app.ingestion.cpcb_adapter import parse_cpcb_ogd_payload

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class TestStage2CpcbAdapterAndStationRegistry(unittest.TestCase):
    def test_reconciled_station_counts_per_district(self) -> None:
        """Verify audited station counts across all 7 target districts, including Chennai's 8 verified + 1 excluded."""
        expected_verified_counts = {
            "new-delhi": 6,
            "lucknow": 6,
            "mumbai-suburban": 8,
            "bengaluru-urban": 7,
            "kolkata": 6,
            "chennai": 8,
            "patna": 6,
        }
        for district_id, expected_count in expected_verified_counts.items():
            verified = list_verified_stations_for_district(district_id)
            self.assertEqual(
                len(verified),
                expected_count,
                f"Expected {expected_count} verified stations for {district_id}, got {len(verified)}",
            )

        all_chennai = [s for s in REVIEWED_STATIONS if s.district_id == "chennai"]
        self.assertEqual(len(all_chennai), 9)
        excluded_chennai = [s for s in all_chennai if not s.is_verified_member]
        self.assertEqual(len(excluded_chennai), 1)
        self.assertEqual(excluded_chennai[0].external_station_id, "Gandhi Nagar_Ennore, Chennai - TNPCB")

    def test_cpcb_ogd_fixture_parsing_and_neighbor_station_exclusions(self) -> None:
        """Verify CPCB OGD parsing rejects unitless rows safely and excludes neighboring-district stations."""
        payload = json.loads((FIXTURES_DIR / "cpcb_ogd_sample.json").read_text(encoding="utf-8"))
        report = parse_cpcb_ogd_payload(payload, ingested_at_utc="2026-11-02T06:00:00Z")

        self.assertEqual(report.total_records_seen, 9)
        self.assertEqual(
            set(report.excluded_non_member_stations),
            {
                "ITO, Delhi - CPCB",
                "Colaba, Mumbai - MPCB",
                "Bidhannagar, Kolkata - WBPCB",
                "Gandhi Nagar_Ennore, Chennai - TNPCB",
            },
        )
        self.assertIn("NH3", report.ignored_pollutants)
        self.assertEqual(len(report.accepted_observations), 4)
        self.assertEqual(report.unverified_unitless_count, 3)

        mandir_pm25 = next(
            o
            for o in report.accepted_observations
            if o.station_id == "cpcb:new-delhi:mandir-marg" and o.pollutant_key == "PM2.5"
        )
        # Unitless CPCB OGD pollutant_avg must NOT be assumed to be a sub-index or concentration:
        # it is stored for auditability with value_kind='unverified_scale' and is_valid=False.
        self.assertEqual(mandir_pm25.value_kind, "invalid")
        self.assertFalse(mandir_pm25.is_valid)
        self.assertIsNone(mandir_pm25.normalized_concentration)
        self.assertIsNone(mandir_pm25.normalized_unit)
        self.assertIsNone(mandir_pm25.calculated_sub_index)
        self.assertEqual(mandir_pm25.observed_at_utc, "2026-11-02T05:30:00Z")

        mandir_so2 = next(
            o
            for o in report.accepted_observations
            if o.station_id == "cpcb:new-delhi:mandir-marg" and o.pollutant_key == "SO2"
        )
        self.assertEqual(mandir_so2.value_kind, "missing")
        self.assertFalse(mandir_so2.is_valid)
        self.assertIsNone(mandir_so2.calculated_sub_index)


if __name__ == "__main__":
    unittest.main()
