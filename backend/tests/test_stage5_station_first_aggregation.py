"""Stage 5 tests: Verify station-first sub-index calculation, expected station-pollutant-hour coverage, and caveats."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest
from app.districts_registry import get_district_mapping, list_verified_stations_for_district
from app.models import RawStationObservation
from app.services.aggregator import (
    DISTRICT_METHODOLOGY_CAVEAT,
    aggregate_district_for_date,
    evaluate_station_window,
)


class TestStage5StationFirstAggregationAndCoverage(unittest.TestCase):
    def _make_24h_observations(
        self, station_id: str, eval_end: datetime, pm25: float, pm10: float, no2: float, hours: int = 24
    ) -> list[RawStationObservation]:
        obs = []
        for h in range(hours):
            ts = (eval_end - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for p_key, val in [("PM2.5", pm25), ("PM10", pm10), ("NO2", no2)]:
                obs.append(
                    RawStationObservation(
                        station_id=station_id,
                        source_provider="openaq_v3",
                        upstream_sensor_or_record_id=f"{station_id}:{p_key}:{ts}",
                        pollutant_key=p_key,  # type: ignore[arg-type]
                        observed_at_utc=ts,
                        window_hours=1,
                        raw_value_text=str(val),
                        raw_unit="µg/m³",
                        value_kind="concentration",
                        normalized_concentration=val,
                        normalized_unit="µg/m³",
                        calculated_sub_index=50,
                        is_valid=True,
                        validation_note="ok",
                        raw_payload_json="{}",
                        ingested_at_utc=ts,
                    )
                )
        return obs

    def test_station_first_sub_indices_and_expected_pollutant_hour_coverage(self) -> None:
        entry = get_district_mapping("new-delhi")
        assert entry is not None
        stations = list_verified_stations_for_district("new-delhi")
        self.assertEqual(len(stations), 6)
        eval_end = datetime(2026, 11, 2, 23, 59, 59, tzinfo=timezone.utc)

        # Station 0: PM2.5 = 150 µg/m³ (station sub-index 323), PM10 = 100 (sub-index 100), NO2 = 40 (sub-index 50)
        # Station 1: PM2.5 = 30 µg/m³  (station sub-index 50),  PM10 = 350 (sub-index 300), NO2 = 40 (sub-index 50)
        # Note: Station 0's station AQI is 323 (PM2.5) and Station 1's station AQI is 300 (PM10).
        # Station-first district composite AQI = round((323 + 300) / 2) = 312!
        # (Whereas if concentrations were averaged before sub-index calculation, mean PM2.5=90 -> 200 and mean PM10=225 -> 183!)
        obs_s0 = self._make_24h_observations(stations[0].station_id, eval_end, pm25=150.0, pm10=100.0, no2=40.0, hours=24)
        obs_s1 = self._make_24h_observations(stations[1].station_id, eval_end, pm25=30.0, pm10=350.0, no2=40.0, hours=24)

        evals = [
            evaluate_station_window(station=stations[0], observations=obs_s0, evaluation_end_utc=eval_end),
            evaluate_station_window(station=stations[1], observations=obs_s1, evaluation_end_utc=eval_end),
        ]
        self.assertEqual(evals[0].station_aqi, 323)
        self.assertEqual(evals[1].station_aqi, 300)

        daily_rec = aggregate_district_for_date(
            district=entry.district,
            registered_stations=stations,
            station_evaluations=evals,
            evaluation_date="2026-11-02",
            updated_at_utc="2026-11-02T23:00:00Z",
            source_attribution="CPCB / DPCC via OpenAQ v3",
            now_utc=eval_end,
        )

        # Verify station-first composite AQI = round((323 + 300) / 2) = 312
        self.assertEqual(daily_rec.district_aqi, 312)
        self.assertEqual(daily_rec.reporting_stations, 2)
        self.assertEqual(daily_rec.registered_stations, 6)

        # Verify expected station-pollutant-hour coverage:
        # 6 registered stations * (4*24h + 2*8h = 112h) = 672 expected station-pollutant-hours
        # 2 reporting stations * 3 pollutants * 24 hours = 144 observed station-pollutant-hours
        self.assertEqual(daily_rec.expected_station_pollutant_hours, 672)
        self.assertEqual(daily_rec.observed_station_pollutant_hours, 144)
        self.assertAlmostEqual(daily_rec.coverage, round(144 / 672, 4))
        self.assertEqual(daily_rec.quality, "low")

        caveats = json.loads(daily_rec.caveats_json)
        self.assertIn(DISTRICT_METHODOLOGY_CAVEAT, caveats)
        self.assertIn("2 of 6 verified district stations met CPCB 16h / 3-pollutant completeness.", caveats)


if __name__ == "__main__":
    unittest.main()
