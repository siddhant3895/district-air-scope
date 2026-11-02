"""Unit tests for data quality classification, anomaly detection, and caveat generation."""
from __future__ import annotations

from datetime import datetime, timezone
import unittest
from app.services.naqi import build_pollutant_list
from app.services.quality import (
    build_caveats,
    classify_quality,
    has_pm_inversion_anomaly,
    is_reading_stale,
    sanitize_concentration,
)


class TestQualityClassification(unittest.TestCase):
    def test_quality_levels_match_methodology_spec(self) -> None:
        """Verify high / medium / low / unavailable thresholds match methodology.tsx."""
        # High: >= 85% coverage and >= 3 stations
        self.assertEqual(classify_quality(stations=6, coverage=0.94, aqi=290), "high")
        self.assertEqual(classify_quality(stations=3, coverage=0.85, aqi=110), "high")

        # Medium: 60-85% coverage and >= 3 stations
        self.assertEqual(classify_quality(stations=3, coverage=0.81, aqi=210), "medium")
        self.assertEqual(classify_quality(stations=4, coverage=0.60, aqi=78), "medium")

        # Low: < 60% coverage OR <= 2 stations
        self.assertEqual(classify_quality(stations=2, coverage=0.92, aqi=62), "low")
        self.assertEqual(classify_quality(stations=4, coverage=0.59, aqi=140), "low")

        # Unavailable: 0 stations, 0 coverage, or null AQI
        self.assertEqual(classify_quality(stations=0, coverage=0.0, aqi=None), "unavailable")
        self.assertEqual(classify_quality(stations=3, coverage=0.80, aqi=None), "unavailable")
        self.assertEqual(classify_quality(stations=0, coverage=0.80, aqi=100), "unavailable")

    def test_staleness_detection_six_hour_threshold(self) -> None:
        """Verify > 6h readings are flagged stale."""
        now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
        fresh_iso = "2026-11-02T07:00:00Z"  # 5 hours old
        stale_iso = "2026-11-02T05:00:00Z"  # 7 hours old

        self.assertFalse(is_reading_stale(fresh_iso, now=now))
        self.assertTrue(is_reading_stale(stale_iso, now=now))

    def test_sanitization_and_pm_inversion(self) -> None:
        """Verify out-of-range spikes and negative numbers are rejected and PM2.5 > PM10 is flagged."""
        self.assertIsNone(sanitize_concentration("PM2.5", -1.0))
        self.assertIsNone(sanitize_concentration("PM2.5", 2500.0))
        self.assertEqual(sanitize_concentration("PM2.5", 85.4), 85.4)

        self.assertFalse(has_pm_inversion_anomaly(60.0, 120.0))
        self.assertFalse(has_pm_inversion_anomaly(103.0, 100.0))  # within 5% tolerance
        self.assertTrue(has_pm_inversion_anomaly(120.0, 90.0))

    def test_caveat_generation(self) -> None:
        """Verify automated caveats match frontend strings and quality rules."""
        partial_pollutants = build_pollutant_list(
            {"PM2.5": 55.0, "PM10": 95.0, "NO2": 30.0, "SO2": None, "O3": 40.0, "CO": 1.1}
        )
        caveats = build_caveats(
            quality="low",
            stations=2,
            coverage=0.48,
            pollutants=partial_pollutants,
            meets_cpcb_minimum_rule=True,
            updated_at_iso="2026-11-02T03:00:00Z",
            now=datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc),
        )
        self.assertIn("Coverage below 60% — summary values may not represent the full district.", caveats)
        self.assertIn("Few monitoring stations; readings may reflect local rather than district-wide conditions.", caveats)
        self.assertIn("One or more pollutants not reported by stations in this district.", caveats)
        self.assertIn("Latest station reading is more than 6 hours old.", caveats)


if __name__ == "__main__":
    unittest.main()
