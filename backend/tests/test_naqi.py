"""Unit tests for CPCB National Air Quality Index (NAQI) calculation service."""
from __future__ import annotations

import unittest
from app.services.naqi import (
    POLLUTANT_SPECS,
    build_pollutant_list,
    calculate_sub_index,
    compute_overall_aqi,
)


class TestCpcbNaqiCalculator(unittest.TestCase):
    def test_cpcb_official_reference_examples_pm25(self) -> None:
        """Verify exact CPCB published worked examples for PM2.5."""
        # CPCB standard: PM2.5 = 30 -> 50 (Good upper bound)
        self.assertEqual(calculate_sub_index("PM2.5", 30.0), 50)
        # CPCB standard: PM2.5 = 31 -> 51 (Satisfactory lower bound)
        self.assertEqual(calculate_sub_index("PM2.5", 31.0), 51)
        # CPCB standard: PM2.5 = 45 -> 75 (Mid-Satisfactory worked example)
        self.assertEqual(calculate_sub_index("PM2.5", 45.0), 75)
        # CPCB standard: PM2.5 = 60 -> 100 (Satisfactory upper bound)
        self.assertEqual(calculate_sub_index("PM2.5", 60.0), 100)
        # CPCB standard: PM2.5 = 150 -> 323 (Very Poor worked example)
        self.assertEqual(calculate_sub_index("PM2.5", 150.0), 323)

    def test_all_pollutant_category_boundaries(self) -> None:
        """Verify breakpoint boundaries across all 6 frontend pollutants."""
        self.assertEqual(calculate_sub_index("PM10", 50.0), 50)
        self.assertEqual(calculate_sub_index("PM10", 100.0), 100)
        self.assertEqual(calculate_sub_index("PM10", 250.0), 200)
        self.assertEqual(calculate_sub_index("PM10", 350.0), 300)
        self.assertEqual(calculate_sub_index("PM10", 430.0), 400)

        self.assertEqual(calculate_sub_index("NO2", 40.0), 50)
        self.assertEqual(calculate_sub_index("NO2", 80.0), 100)
        self.assertEqual(calculate_sub_index("SO2", 40.0), 50)
        self.assertEqual(calculate_sub_index("SO2", 380.0), 200)
        self.assertEqual(calculate_sub_index("O3", 50.0), 50)
        self.assertEqual(calculate_sub_index("O3", 168.0), 200)
        self.assertEqual(calculate_sub_index("CO", 1.0), 50)
        self.assertEqual(calculate_sub_index("CO", 2.0), 100)
        self.assertEqual(calculate_sub_index("CO", 10.0), 200)

    def test_null_negative_and_extreme_values(self) -> None:
        """Verify None/negative inputs return None and extreme concentrations cap at 500."""
        self.assertIsNone(calculate_sub_index("PM2.5", None))
        self.assertIsNone(calculate_sub_index("PM2.5", -5.0))
        self.assertEqual(calculate_sub_index("PM2.5", 0.0), 0)
        self.assertEqual(calculate_sub_index("PM2.5", 650.0), 500)

    def test_dominant_pollutant_and_cpcb_three_pollutant_rule(self) -> None:
        """Verify worst sub-index wins and CPCB 3-pollutant + PM rule is enforced."""
        pollutants = build_pollutant_list(
            {
                "PM2.5": 150.0,  # subIndex = 323
                "PM10": 250.0,   # subIndex = 200
                "NO2": 80.0,     # subIndex = 100
                "SO2": None,
                "O3": 50.0,      # subIndex = 50
                "CO": 1.0,       # subIndex = 50
            }
        )
        self.assertEqual([p.key for p in pollutants], [k for k, _ in POLLUTANT_SPECS])
        aqi, dominant, meets_rule = compute_overall_aqi(pollutants)
        self.assertEqual(aqi, 323)
        self.assertEqual(dominant, "PM2.5")
        self.assertTrue(meets_rule)

        # Only 2 pollutants reported -> fails CPCB 3-pollutant minimum rule
        sparse_pollutants = build_pollutant_list({"PM2.5": 60.0, "PM10": 100.0})
        aqi_sparse, dom_sparse, meets_sparse = compute_overall_aqi(sparse_pollutants)
        self.assertEqual(aqi_sparse, 100)
        self.assertEqual(dom_sparse, "PM2.5")
        self.assertFalse(meets_sparse)

        # 3 gaseous pollutants but neither PM2.5 nor PM10 -> fails CPCB PM requirement
        no_pm_pollutants = build_pollutant_list({"NO2": 80.0, "SO2": 40.0, "O3": 50.0})
        _, _, meets_no_pm = compute_overall_aqi(no_pm_pollutants)
        self.assertFalse(meets_no_pm)


if __name__ == "__main__":
    unittest.main()
