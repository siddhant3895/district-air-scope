"""API contract tests verifying 1:1 compatibility with src/data/types.ts across the nationwide district set."""
from __future__ import annotations

from datetime import datetime, timezone
import unittest
from fastapi.testclient import TestClient
from app.districts_registry import get_district_mapping
from app.main import app
from app.schemas import Reading
from app.services.snapshot_builder import assemble_district_snapshot

CORE_SEVEN_DISTRICT_IDS = [
    "new-delhi",
    "lucknow",
    "mumbai-suburban",
    "bengaluru-urban",
    "kolkata",
    "chennai",
    "patna",
]

EXPECTED_POLLUTANT_KEYS = ["PM2.5", "PM10", "NO2", "SO2", "O3", "CO"]


class TestApiContract(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_endpoint_reports_not_live_ready_when_unconfigured(self) -> None:
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertFalse(body["liveDataReady"])
        self.assertEqual(body["districtsTracked"], 30)

    def test_list_districts_includes_core_seven_and_nationwide_expansion_with_filters(self) -> None:
        resp = self.client.get("/api/districts")
        self.assertEqual(resp.status_code, 200)
        districts = resp.json()
        self.assertEqual(len(districts), 30)
        self.assertEqual([d["id"] for d in districts[:7]], CORE_SEVEN_DISTRICT_IDS)
        for d in districts:
            self.assertEqual(set(d.keys()), {"id", "name", "state", "lat", "lon"})
            self.assertIsInstance(d["lat"], float)
            self.assertIsInstance(d["lon"], float)

        # Verify ?state= filter
        mh_resp = self.client.get("/api/districts?state=Maharashtra")
        self.assertEqual(mh_resp.status_code, 200)
        mh_ids = [d["id"] for d in mh_resp.json()]
        self.assertEqual(mh_ids, ["mumbai-suburban", "pune", "nagpur"])

        # Verify ?q= search filter
        q_resp = self.client.get("/api/districts?q=gurugram")
        self.assertEqual(q_resp.status_code, 200)
        self.assertEqual([d["id"] for d in q_resp.json()], ["gurugram"])

    def test_list_snapshots_and_unmonitored_district_caveats_without_fabricated_data(self) -> None:
        resp = self.client.get("/api/snapshots")
        self.assertEqual(resp.status_code, 200)
        snaps = resp.json()
        self.assertEqual(len(snaps), 30)

        for snap in snaps:
            self.assertEqual(
                set(snap.keys()),
                {
                    "district",
                    "aqi",
                    "dominant",
                    "updatedAt",
                    "source",
                    "stations",
                    "coverage",
                    "quality",
                    "caveats",
                    "pollutants",
                    "history",
                    "isIllustrative",
                },
            )
            self.assertIsNone(snap["aqi"])
            self.assertIsNone(snap["dominant"])
            self.assertIsNone(snap["updatedAt"])
            self.assertEqual(snap["stations"], 0)
            self.assertEqual(snap["coverage"], 0.0)
            self.assertEqual(snap["quality"], "unavailable")
            self.assertFalse(snap["isIllustrative"])
            self.assertEqual([p["key"] for p in snap["pollutants"]], EXPECTED_POLLUTANT_KEYS)
            self.assertEqual(len(snap["history"]), 30)
            self.assertTrue(all(h["aqi"] is None for h in snap["history"]))

        # Verify zero-CAAQMS district (e.g., Leh, Ladakh) explicitly states no CAAQMS stations are installed
        leh = self.client.get("/api/snapshots/leh")
        self.assertEqual(leh.status_code, 200)
        leh_body = leh.json()
        self.assertEqual(leh_body["district"]["state"], "Ladakh")
        self.assertIn(
            "No continuous ambient air quality monitoring stations (CAAQMS) are installed in this district.",
            leh_body["caveats"],
        )

        # Verify 404 for unknown district
        missing = self.client.get("/api/snapshots/non-existent-district")
        self.assertEqual(missing.status_code, 404)

    def test_assembled_snapshot_schema_with_measured_inputs(self) -> None:
        mapping = get_district_mapping("new-delhi")
        assert mapping is not None
        now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
        snap = assemble_district_snapshot(
            district=mapping.district,
            raw_pollutants={
                "PM2.5": 150.0,
                "PM10": 250.0,
                "NO2": 80.0,
                "SO2": 40.0,
                "O3": 50.0,
                "CO": 1.0,
            },
            stations=6,
            coverage=0.92,
            updated_at="2026-11-02T11:00:00Z",
            source="CPCB CAAQMS via data.gov.in (GODL-India)",
            history=[Reading(date="2026-11-01", aqi=None), Reading(date="2026-11-02", aqi=323)],
            now=now,
        )
        self.assertEqual(snap.aqi, 323)
        self.assertEqual(snap.dominant, "PM2.5")
        self.assertEqual(snap.quality, "high")
        self.assertEqual(snap.caveats, [])
        self.assertFalse(snap.isIllustrative)
        self.assertEqual(snap.history[0].aqi, None)
        self.assertEqual(snap.history[1].aqi, 323)


if __name__ == "__main__":
    unittest.main()
