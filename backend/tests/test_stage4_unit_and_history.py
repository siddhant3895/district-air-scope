"""Stage 4 tests: Verify unit normalization (ppm/ppb/µg/m³/mg/m³) and 24h vs 8h historical averaging windows."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from app.districts_registry import list_verified_stations_for_district
from app.models import RawStationObservation
from app.services.aggregator import evaluate_station_window
from app.services.unit_normalizer import normalize_upstream_observation


class TestStage4UnitNormalizationAndAveragingWindows(unittest.TestCase):
    def test_unit_validation_and_gas_conversions(self) -> None:
        # 1. Missing unit on OpenAQ concentration feed must be rejected
        missing_unit = normalize_upstream_observation(
            source_provider="openaq_v3",
            pollutant_key="PM2.5",
            raw_value_text="55.0",
            raw_unit=None,
        )
        self.assertFalse(missing_unit.is_valid)
        self.assertEqual(missing_unit.value_kind, "invalid")

        # 2. Unsupported unit ('ppb') on particulate PM2.5 must be rejected
        bad_pm_unit = normalize_upstream_observation(
            source_provider="openaq_v3",
            pollutant_key="PM2.5",
            raw_value_text="55.0",
            raw_unit="ppb",
        )
        self.assertFalse(bad_pm_unit.is_valid)

        # 3. CO in ppm -> converted to mg/m³ (1.0 ppm CO * 28.01 / 24.45 = 1.1456 mg/m³ -> subIndex 53)
        co_ppm = normalize_upstream_observation(
            source_provider="openaq_v3",
            pollutant_key="CO",
            raw_value_text="1.0",
            raw_unit="ppm",
        )
        self.assertTrue(co_ppm.is_valid)
        self.assertEqual(co_ppm.normalized_unit, "mg/m³")
        self.assertAlmostEqual(co_ppm.normalized_concentration or 0.0, 1.1456, places=3)
        self.assertEqual(co_ppm.calculated_sub_index, 53)

        # 4. NO2 in ppb -> converted to µg/m³ (40.0 ppb NO2 * 46.0055 / 24.45 = 75.2646 µg/m³)
        no2_ppb = normalize_upstream_observation(
            source_provider="openaq_v3",
            pollutant_key="NO2",
            raw_value_text="40.0",
            raw_unit="ppb",
        )
        self.assertTrue(no2_ppb.is_valid)
        self.assertEqual(no2_ppb.normalized_unit, "µg/m³")
        self.assertAlmostEqual(no2_ppb.normalized_concentration or 0.0, 75.2646, places=3)

    def test_16_hour_completeness_and_24h_vs_8h_averaging_windows(self) -> None:
        station = list_verified_stations_for_district("new-delhi")[0]
        eval_end = datetime(2026, 11, 2, 23, 59, 59, tzinfo=timezone.utc)

        # Case A: Only 15 hours of valid observations (< 16h minimum) -> station AQI must be rejected
        obs_15h = []
        for h in range(15):
            ts = (eval_end - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for p_key, val, unit in [
                ("PM2.5", 60.0, "µg/m³"),
                ("PM10", 100.0, "µg/m³"),
                ("NO2", 40.0, "µg/m³"),
            ]:
                obs_15h.append(
                    RawStationObservation(
                        station_id=station.station_id,
                        source_provider="openaq_v3",
                        upstream_sensor_or_record_id=f"{p_key}:{ts}",
                        pollutant_key=p_key,  # type: ignore[arg-type]
                        observed_at_utc=ts,
                        window_hours=1,
                        raw_value_text=str(val),
                        raw_unit=unit,
                        value_kind="concentration",
                        normalized_concentration=val,
                        normalized_unit=unit,
                        calculated_sub_index=100,
                        is_valid=True,
                        validation_note="ok",
                        raw_payload_json="{}",
                        ingested_at_utc=ts,
                    )
                )

        ev_15h = evaluate_station_window(
            station=station,
            observations=obs_15h,
            evaluation_end_utc=eval_end,
        )
        self.assertFalse(ev_15h.meets_16h_completeness)
        self.assertFalse(ev_15h.is_valid_station_aqi)
        self.assertIsNone(ev_15h.station_aqi)

        # Case B: 20 hours of valid observations -> verify 24h window for PM2.5 vs 8h window for O3
        obs_20h = []
        for h in range(20):
            ts = (eval_end - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
            # O3 is 100 µg/m³ in the latest 8 hours (h=0..7) and 20 µg/m³ in earlier hours (h=8..19)
            o3_val = 100.0 if h < 8 else 20.0
            for p_key, val, unit in [
                ("PM2.5", 30.0, "µg/m³"),  # 24h mean = 30.0 -> subIndex 50
                ("PM10", 50.0, "µg/m³"),   # 24h mean = 50.0 -> subIndex 50
                ("O3", o3_val, "µg/m³"),   # 8h window mean = 100.0 -> subIndex 100 (whereas 24h mean would be 52.0!)
            ]:
                obs_20h.append(
                    RawStationObservation(
                        station_id=station.station_id,
                        source_provider="openaq_v3",
                        upstream_sensor_or_record_id=f"{p_key}:{ts}",
                        pollutant_key=p_key,  # type: ignore[arg-type]
                        observed_at_utc=ts,
                        window_hours=1,
                        raw_value_text=str(val),
                        raw_unit=unit,
                        value_kind="concentration",
                        normalized_concentration=val,
                        normalized_unit=unit,
                        calculated_sub_index=50,
                        is_valid=True,
                        validation_note="ok",
                        raw_payload_json="{}",
                        ingested_at_utc=ts,
                    )
                )

        ev_20h = evaluate_station_window(
            station=station,
            observations=obs_20h,
            evaluation_end_utc=eval_end,
        )
        self.assertTrue(ev_20h.is_valid_station_aqi)
        # Because O3 uses the 8-hour averaging window (mean=100.0 -> subIndex 100), O3 dominates with AQI 100
        self.assertEqual(ev_20h.pollutant_evaluations["O3"].window_hours, 8)
        self.assertEqual(ev_20h.pollutant_evaluations["O3"].mean_concentration, 100.0)
        self.assertEqual(ev_20h.pollutant_evaluations["O3"].sub_index, 100)
        self.assertEqual(ev_20h.station_aqi, 100)
        self.assertEqual(ev_20h.dominant_pollutant, "O3")


if __name__ == "__main__":
    unittest.main()
