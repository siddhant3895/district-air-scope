"""Regression tests for the 5 backend reliability and data integrity audit fixes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import httpx

from app.cli import recompute_district_history_window, run_sync, seed_registry
from app.database import get_db_connection
from app.districts_registry import list_verified_stations_for_district
from app.ingestion.cpcb_adapter import parse_cpcb_ogd_payload
from app.ingestion.http_utils import RateLimitedHttpClient
from app.main import app
from app.migrations import (
    insert_raw_observations,
    load_stations_from_db,
    update_station_openaq_mapping,
)
from app.models import RawStationObservation
from app.services.aggregator import (
    IST_TZ,
    aggregate_district_for_date,
    evaluate_station_window,
    format_ist_date,
    ist_date_bounds_in_utc,
)
from app.services.unit_normalizer import normalize_upstream_observation


class TestPriority1CpcbSemanticsSafeFailure(unittest.TestCase):
    def test_unitless_and_na_unit_cpcb_values_fail_safely_and_cannot_produce_aqi(self) -> None:
        """Verify unitless / 'NA'-unit CPCB values are never assumed to be sub-indices or concentrations."""
        for raw_unit in (None, "", "NA", "na", "N/A"):
            res = normalize_upstream_observation(
                source_provider="cpcb_ogd",
                pollutant_key="PM2.5",
                raw_value_text="145",
                raw_unit=raw_unit,
            )
            self.assertEqual(res.value_kind, "invalid")
            self.assertFalse(res.is_valid)
            self.assertIsNone(res.normalized_concentration)
            self.assertIsNone(res.calculated_sub_index)

        # Even if 24 hourly unitless CPCB rows are parsed and inserted, station AQI must remain None
        st = list_verified_stations_for_district("new-delhi")[0]
        eval_end = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
        records = []
        for h in range(24):
            ist_dt = (eval_end - timedelta(hours=h)).astimezone(IST_TZ)
            for p_id in ("PM2.5", "PM10", "NO2", "SO2"):
                records.append(
                    {
                        "id": f"{h}:{p_id}",
                        "country": "India",
                        "state": "Delhi",
                        "city": "Delhi",
                        "station": st.external_station_id,
                        "last_update": ist_dt.strftime("%d-%m-%Y %H:%M:%S"),
                        "pollutant_id": p_id,
                        "pollutant_avg": "180",
                        "pollutant_unit": "NA",
                        "window_hours": 1,
                    }
                )
        report = parse_cpcb_ogd_payload({"records": records})
        self.assertEqual(report.unverified_unitless_count, 96)
        self.assertTrue(all(not o.is_valid for o in report.accepted_observations))

        ev = evaluate_station_window(
            station=st,
            observations=report.accepted_observations,
            evaluation_end_utc=eval_end,
        )
        self.assertFalse(ev.is_valid_station_aqi)
        self.assertIsNone(ev.station_aqi)

    def test_cpcb_record_with_explicit_physical_unit_succeeds(self) -> None:
        """Verify that if CPCB provides an explicit physical concentration unit, it normalizes properly."""
        res = normalize_upstream_observation(
            source_provider="cpcb_ogd",
            pollutant_key="PM2.5",
            raw_value_text="60",
            raw_unit="µg/m³",
        )
        self.assertTrue(res.is_valid)
        self.assertEqual(res.value_kind, "concentration")
        self.assertEqual(res.normalized_concentration, 60.0)
        self.assertEqual(res.calculated_sub_index, 100)


class TestPriority2OpenAqLocationPersistenceAndReadOnlyRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_p2.db")
        self.prev_db_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if self.prev_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.prev_db_url
        self.temp_dir.cleanup()

    def test_discovered_openaq_id_persists_across_reseed_and_public_gets(self) -> None:
        """Verify discovered OpenAQ location IDs are not overwritten with null by seed_registry or GET routes."""
        with get_db_connection() as conn:
            seed_registry(conn)
            # Initially cpcb:lucknow:lalbagh has openaq_location_id=None in REVIEWED_STATIONS
            stations_before = load_stations_from_db(conn, district_id="lucknow")
            lalbagh = next(s for s in stations_before if s.station_id == "cpcb:lucknow:lalbagh")
            self.assertIsNone(lalbagh.openaq_location_id)

            # Persist discovered OpenAQ location ID 44556
            update_station_openaq_mapping(conn, "cpcb:lucknow:lalbagh", 44556, True)
            conn.commit()

            # Re-run seed_registry(conn) -> must preserve 44556 via COALESCE
            seed_registry(conn)
            stations_after_seed = load_stations_from_db(conn, district_id="lucknow")
            lalbagh_after = next(s for s in stations_after_seed if s.station_id == "cpcb:lucknow:lalbagh")
            self.assertEqual(lalbagh_after.openaq_location_id, 44556)
            self.assertTrue(lalbagh_after.openaq_verified_available)

        # Call public API read paths -> must not reseed or overwrite station mappings
        with patch("app.cli.seed_registry") as mock_seed:
            self.assertEqual(self.client.get("/api/districts").status_code, 200)
            self.assertEqual(self.client.get("/api/snapshots").status_code, 200)
            self.assertEqual(self.client.get("/api/snapshots/lucknow").status_code, 200)
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            mock_seed.assert_not_called()


class TestPriority3IncrementalIngestionRateLimitingAndIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_p3.db")
        self.prev_db_url = os.environ.get("DATABASE_URL")
        self.prev_cpcb_key = os.environ.get("DATA_GOV_IN_API_KEY")
        self.prev_oaq_key = os.environ.get("OPENAQ_API_KEY")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"

    def tearDown(self) -> None:
        for k, prev in [
            ("DATABASE_URL", self.prev_db_url),
            ("DATA_GOV_IN_API_KEY", self.prev_cpcb_key),
            ("OPENAQ_API_KEY", self.prev_oaq_key),
        ]:
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        self.temp_dir.cleanup()

    def test_rate_limited_http_client_retries_429_and_paces_requests(self) -> None:
        sleeps: List[float] = []
        clock_val = [100.0]

        def fake_sleep(sec: float) -> None:
            sleeps.append(round(sec, 3))
            clock_val[0] += sec

        def fake_clock() -> float:
            return clock_val[0]

        attempts = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            attempts[0] += 1
            if attempts[0] == 1:
                return httpx.Response(429, headers={"Retry-After": "2.5"}, json={"error": "rate_limited"})
            if attempts[0] == 2:
                return httpx.Response(503, json={"error": "unavailable"})
            return httpx.Response(
                200,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1.5"},
                json={"ok": True},
            )

        client = RateLimitedHttpClient(
            min_interval_sec=1.0,
            max_retries=3,
            base_backoff_sec=0.5,
            sleep_fn=fake_sleep,
            clock_fn=fake_clock,
            transport=httpx.MockTransport(handler),
        )
        data = client.get_json("https://api.openaq.org/v3/locations/8118")
        self.assertEqual(data, {"ok": True})
        self.assertEqual(attempts[0], 3)
        # First retry slept 2.5s (Retry-After), second retry slept 1.0s (0.5 * 2^1), then 1.5s proactive reset
        self.assertIn(2.5, sleeps)
        self.assertIn(1.0, sleeps)
        self.assertIn(1.5, sleeps)

    def test_incremental_sync_overlap_and_provider_failure_isolation(self) -> None:
        """Verify initial backfill vs incremental overlap window, persisted ID reuse, and CPCB failure isolation."""
        os.environ["DATA_GOV_IN_API_KEY"] = "cpcb-test-key"
        os.environ["OPENAQ_API_KEY"] = "openaq-test-key"

        ref_now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
        hours_calls: List[Dict[str, str]] = []
        discovery_calls: List[str] = []

        class FakeHttpClient:
            def get_json(
                self, url: str, *, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None
            ) -> Dict[str, Any]:
                # Simulate CPCB API outage (500 error) to verify provider failure isolation
                if "data.gov.in" in url:
                    raise RuntimeError("CPCB upstream gateway timeout (504)")

                # OpenAQ coordinate discovery for unmapped stations
                if url.endswith("/v3/locations") and params and "coordinates" in params:
                    discovery_calls.append(str(params["coordinates"]))
                    # Return a valid discovered location (id=9001) only for Lalbagh (26.8460,80.9462)
                    if str(params["coordinates"]).startswith("26.8460"):
                        return {
                            "results": [
                                {
                                    "id": 9001,
                                    "name": "Lalbagh, Lucknow",
                                    "isMonitor": True,
                                    "isMobile": False,
                                    "datetimeLast": {"utc": "2026-11-02T11:00:00Z"},
                                    "sensors": [
                                        {"id": 90011, "parameter": {"name": "pm25", "units": "µg/m³"}},
                                        {"id": 90012, "parameter": {"name": "pm10", "units": "µg/m³"}},
                                        {"id": 90013, "parameter": {"name": "no2", "units": "µg/m³"}},
                                    ],
                                }
                            ]
                        }
                    return {"results": []}

                # OpenAQ direct location fetch (for Mandir Marg 8118 or persisted Lalbagh 9001)
                if "/v3/locations/" in url:
                    loc_id = int(url.rsplit("/", 1)[-1])
                    return {
                        "results": [
                            {
                                "id": loc_id,
                                "name": f"Station {loc_id}",
                                "isMonitor": True,
                                "isMobile": False,
                                "datetimeLast": {"utc": "2026-11-02T11:00:00Z"},
                                "sensors": [
                                    {"id": loc_id * 10 + 1, "parameter": {"name": "pm25", "units": "µg/m³"}},
                                    {"id": loc_id * 10 + 2, "parameter": {"name": "pm10", "units": "µg/m³"}},
                                    {"id": loc_id * 10 + 3, "parameter": {"name": "no2", "units": "µg/m³"}},
                                ],
                            }
                        ]
                    }

                # OpenAQ hourly sensor fetch
                if "/v3/sensors/" in url and url.endswith("/hours"):
                    hours_calls.append(
                        {
                            "url": url,
                            "datetime_from": str((params or {}).get("datetime_from")),
                            "datetime_to": str((params or {}).get("datetime_to")),
                        }
                    )
                    # Return 18 hourly observations ending at 2026-11-02T10:00:00Z
                    results = []
                    base_dt = datetime(2026, 11, 2, 10, 0, 0, tzinfo=timezone.utc)
                    for h in range(18):
                        dt_to = (base_dt - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        dt_from = (base_dt - timedelta(hours=h + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        results.append(
                            {
                                "value": 60.0,
                                "parameter": {"name": "pm25", "units": "µg/m³"},
                                "period": {
                                    "datetimeFrom": {"utc": dt_from},
                                    "datetimeTo": {"utc": dt_to},
                                },
                                "coverage": {"observedCount": 1, "percentCoverage": 100.0},
                            }
                        )
                    return {"results": results}

                return {}

        fake_http = FakeHttpClient()

        # Run 1: Initial backfill (30 days) — CPCB fails, OpenAQ succeeds and discovers location 9001 for Lalbagh
        res1 = run_sync(days=30, overlap_hours=3, now_utc=ref_now, http_client=fake_http)
        self.assertEqual(res1["overall_status"], "partial_failure")
        self.assertEqual(res1["cpcb_status"], "failed")
        self.assertIn("504", res1["cpcb_error"] or "")
        self.assertEqual(res1["openaq_status"], "succeeded")
        self.assertGreater(res1["openaq_observations_ingested"], 0)

        # Verify Run 1 used the 30-day initial backfill start (2026-10-03T12:00:00Z)
        self.assertTrue(all(c["datetime_from"] == "2026-10-03T12:00:00Z" for c in hours_calls))

        # Verify Lalbagh's discovered OpenAQ ID (9001) was persisted in SQLite
        with get_db_connection() as conn:
            lalbagh = next(
                s for s in load_stations_from_db(conn, district_id="lucknow")
                if s.station_id == "cpcb:lucknow:lalbagh"
            )
            self.assertEqual(lalbagh.openaq_location_id, 9001)

        # Run 2: Incremental sync 30 minutes later — must use persisted 9001 (not re-discover Lalbagh)
        # and must request only from (latest_obs 2026-11-02T10:00:00Z - 3h overlap = 2026-11-02T07:00:00Z)!
        hours_calls.clear()
        discovery_calls.clear()
        res2 = run_sync(
            days=30,
            overlap_hours=3,
            now_utc=ref_now + timedelta(minutes=30),
            http_client=fake_http,
        )
        self.assertEqual(res2["openaq_status"], "succeeded")
        # Lalbagh (26.8460,80.9462) must NOT be in discovery_calls on Run 2 because 9001 is now persisted!
        self.assertFalse(any(c.startswith("26.8460") for c in discovery_calls))
        # All sensor hour calls for Mandir Marg & Lalbagh on Run 2 use incremental start 2026-11-02T07:00:00Z
        self.assertTrue(all(c["datetime_from"] == "2026-11-02T07:00:00Z" for c in hours_calls))

    def test_sensor_level_incremental_cursor_with_active_and_stale_sensors_at_same_station(self) -> None:
        """Verify each OpenAQ sensor at the same station tracks its own independent incremental cursor."""
        os.environ.pop("DATA_GOV_IN_API_KEY", None)
        os.environ["OPENAQ_API_KEY"] = "openaq-test-key"

        ref_now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
        st = list_verified_stations_for_district("new-delhi")[0]  # Mandir Marg (openaq_location_id=8118)

        # Pre-populate DB at Mandir Marg with:
        # - Sensor 81181 (PM2.5): fresh observation 2 hours ago (2026-11-02T10:00:00Z)
        # - Sensor 81182 (PM10): stale observation 10 days ago (2026-10-23T06:00:00Z)
        # - Sensor 81183 (NO2): no prior observations in DB (must get full 30-day backfill: 2026-10-03T12:00:00Z)
        with get_db_connection() as conn:
            seed_registry(conn)
            insert_raw_observations(
                conn,
                [
                    RawStationObservation(
                        station_id=st.station_id,
                        source_provider="openaq_v3",
                        upstream_sensor_or_record_id="openaq_sensor:81181:2026-11-02T10:00:00Z",
                        pollutant_key="PM2.5",
                        observed_at_utc="2026-11-02T10:00:00Z",
                        window_hours=1,
                        raw_value_text="55.0",
                        raw_unit="µg/m³",
                        value_kind="concentration",
                        normalized_concentration=55.0,
                        normalized_unit="µg/m³",
                        calculated_sub_index=92,
                        is_valid=True,
                        validation_note="ok",
                        raw_payload_json="{}",
                        ingested_at_utc="2026-11-02T10:05:00Z",
                    ),
                    RawStationObservation(
                        station_id=st.station_id,
                        source_provider="openaq_v3",
                        upstream_sensor_or_record_id="openaq_sensor:81182:2026-10-23T06:00:00Z",
                        pollutant_key="PM10",
                        observed_at_utc="2026-10-23T06:00:00Z",
                        window_hours=1,
                        raw_value_text="120.0",
                        raw_unit="µg/m³",
                        value_kind="concentration",
                        normalized_concentration=120.0,
                        normalized_unit="µg/m³",
                        calculated_sub_index=113,
                        is_valid=True,
                        validation_note="ok",
                        raw_payload_json="{}",
                        ingested_at_utc="2026-10-23T06:05:00Z",
                    ),
                ],
            )
            conn.commit()

        sensor_windows_requested: Dict[int, str] = {}

        class SensorWindowSpyClient:
            def get_json(
                self, url: str, *, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None
            ) -> Dict[str, Any]:
                if "/v3/locations/8118" in url:
                    return {
                        "results": [
                            {
                                "id": 8118,
                                "name": "Mandir Marg",
                                "isMonitor": True,
                                "isMobile": False,
                                "datetimeLast": {"utc": "2026-11-02T11:00:00Z"},
                                "sensors": [
                                    {"id": 81181, "parameter": {"name": "pm25", "units": "µg/m³"}},
                                    {"id": 81182, "parameter": {"name": "pm10", "units": "µg/m³"}},
                                    {"id": 81183, "parameter": {"name": "no2", "units": "µg/m³"}},
                                ],
                            }
                        ]
                    }
                if "/v3/locations" in url:
                    return {"results": []}
                if "/v3/sensors/" in url and url.endswith("/hours"):
                    sensor_id = int(url.split("/v3/sensors/")[1].split("/hours")[0])
                    sensor_windows_requested[sensor_id] = str((params or {}).get("datetime_from"))
                    return {"results": []}
                return {}

        res = run_sync(days=30, overlap_hours=3, now_utc=ref_now, http_client=SensorWindowSpyClient())
        self.assertEqual(res["openaq_status"], "succeeded")

        # Sensor 81181 (active 2h ago at 10:00Z) -> 10:00Z - 3h overlap = 2026-11-02T07:00:00Z
        self.assertEqual(sensor_windows_requested.get(81181), "2026-11-02T07:00:00Z")
        # Sensor 81182 (stale 10 days ago at 2026-10-23T06:00Z) -> 2026-10-23T06:00Z - 3h = 2026-10-23T03:00:00Z
        self.assertEqual(sensor_windows_requested.get(81182), "2026-10-23T03:00:00Z")
        # Sensor 81183 (no prior observations) -> full 30-day backfill = 2026-10-03T12:00:00Z
        self.assertEqual(sensor_windows_requested.get(81183), "2026-10-03T12:00:00Z")


class TestPriority4CalculationProvenanceAndIstBoundaries(unittest.TestCase):
    def test_ist_day_boundaries_and_dynamic_source_provider_provenance(self) -> None:
        """Verify IST (UTC+05:30) day boundaries and accurate cpcb_ogd / openaq_v3 / hybrid provenance labels."""
        # 1. Verify IST date boundary conversion: 2026-11-02 IST = 2026-11-01T18:30:00Z .. 2026-11-02T18:29:59Z
        start_utc, end_utc = ist_date_bounds_in_utc("2026-11-02")
        self.assertEqual(start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "2026-11-01T18:30:00Z")
        self.assertEqual(end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "2026-11-02T18:29:59Z")

        # Timestamp 2026-11-01T20:00:00Z is 2026-11-02 01:30:00 IST -> must format as 2026-11-02
        dt_utc = datetime(2026, 11, 1, 20, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(format_ist_date(dt_utc), "2026-11-02")

        # 2. Verify station evaluation provenance when observations come from cpcb_ogd (with explicit unit)
        st = list_verified_stations_for_district("new-delhi")[0]
        obs_cpcb = []
        for h in range(18):
            ts = (dt_utc - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for p_key, val in [("PM2.5", 60.0), ("PM10", 100.0), ("NO2", 40.0)]:
                obs_cpcb.append(
                    RawStationObservation(
                        station_id=st.station_id,
                        source_provider="cpcb_ogd",
                        upstream_sensor_or_record_id=f"cpcb:{p_key}:{ts}",
                        pollutant_key=p_key,  # type: ignore[arg-type]
                        observed_at_utc=ts,
                        window_hours=1,
                        raw_value_text=str(val),
                        raw_unit="µg/m³",
                        value_kind="concentration",
                        normalized_concentration=val,
                        normalized_unit="µg/m³",
                        calculated_sub_index=100 if p_key != "NO2" else 50,
                        is_valid=True,
                        validation_note="Verified concentration",
                        raw_payload_json="{}",
                        ingested_at_utc=ts,
                    )
                )

        ev = evaluate_station_window(station=st, observations=obs_cpcb, evaluation_end_utc=dt_utc)
        self.assertTrue(ev.is_valid_station_aqi)
        self.assertEqual(ev.source_provider, "cpcb_ogd")
        self.assertEqual(ev.evaluation_date, "2026-11-02")

        from app.districts_registry import get_district_mapping
        d_entry = get_district_mapping("new-delhi")
        assert d_entry is not None
        daily = aggregate_district_for_date(
            district=d_entry.district,
            registered_stations=list_verified_stations_for_district("new-delhi"),
            station_evaluations=[ev],
            evaluation_date="2026-11-02",
            updated_at_utc="2026-11-01T20:00:00Z",
            source_attribution="auto",
            now_utc=dt_utc,
        )
        self.assertEqual(daily.source_attribution, "CPCB / SPCB CAAQMS via data.gov.in (GODL-India)")


class TestPriority5ApiHealthFreshnessAndProviderIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_p5.db")
        self.prev_db_url = os.environ.get("DATABASE_URL")
        self.prev_cpcb_key = os.environ.get("DATA_GOV_IN_API_KEY")
        self.prev_oaq_key = os.environ.get("OPENAQ_API_KEY")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        os.environ["DATA_GOV_IN_API_KEY"] = "cpcb-key"
        os.environ["OPENAQ_API_KEY"] = "openaq-key"
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for k, prev in [
            ("DATABASE_URL", self.prev_db_url),
            ("DATA_GOV_IN_API_KEY", self.prev_cpcb_key),
            ("OPENAQ_API_KEY", self.prev_oaq_key),
        ]:
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        self.temp_dir.cleanup()

    def test_health_requires_fresh_data_and_reports_providers_independently(self) -> None:
        st = list_verified_stations_for_district("new-delhi")[0]
        now_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        # Case 1: Insert STALE observations (10 hours old -> older than 6h threshold)
        stale_end = now_utc - timedelta(hours=10)
        stale_obs = []
        for h in range(18):
            ts = (stale_end - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for p_key, val in [("PM2.5", 60.0), ("PM10", 100.0), ("NO2", 40.0)]:
                stale_obs.append(
                    RawStationObservation(
                        station_id=st.station_id,
                        source_provider="openaq_v3",
                        upstream_sensor_or_record_id=f"stale:{p_key}:{ts}",
                        pollutant_key=p_key,  # type: ignore[arg-type]
                        observed_at_utc=ts,
                        window_hours=1,
                        raw_value_text=str(val),
                        raw_unit="µg/m³",
                        value_kind="concentration",
                        normalized_concentration=val,
                        normalized_unit="µg/m³",
                        calculated_sub_index=100,
                        is_valid=True,
                        validation_note="ok",
                        raw_payload_json="{}",
                        ingested_at_utc=ts,
                    )
                )

        with get_db_connection() as conn:
            seed_registry(conn)
            insert_raw_observations(conn, stale_obs)
            conn.execute(
                """
                INSERT INTO ingestion_sync_runs (
                    started_at_utc, completed_at_utc, overall_status,
                    cpcb_status, cpcb_error, openaq_status, openaq_observations_ingested
                )
                VALUES (?, ?, 'partial_failure', 'failed', 'CPCB 503 error', 'succeeded', 54)
                """,
                (stale_end.strftime("%Y-%m-%dT%H:%M:%SZ"), stale_end.strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
            recompute_district_history_window(conn, anchor_date_str=format_ist_date(stale_end), days=2)

        # Because observations are 10h old (> 6h stale threshold), liveDataReady MUST be False
        resp_stale = self.client.get("/api/health").json()
        self.assertFalse(resp_stale["liveDataReady"])
        self.assertEqual(resp_stale["freshDistrictsCount"], 0)
        self.assertEqual(resp_stale["providers"]["cpcb_ogd"]["lastSyncStatus"], "failed")
        self.assertEqual(resp_stale["providers"]["openaq_v3"]["lastSyncStatus"], "succeeded")
        self.assertFalse(resp_stale["providers"]["openaq_v3"]["hasFreshData"])

        # Case 2: Insert FRESH observations (1 hour old) for OpenAQ while CPCB remains failed
        fresh_end = now_utc - timedelta(hours=1)
        fresh_obs = []
        for h in range(18):
            ts = (fresh_end - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for p_key, val in [("PM2.5", 60.0), ("PM10", 100.0), ("NO2", 40.0)]:
                fresh_obs.append(
                    RawStationObservation(
                        station_id=st.station_id,
                        source_provider="openaq_v3",
                        upstream_sensor_or_record_id=f"fresh:{p_key}:{ts}",
                        pollutant_key=p_key,  # type: ignore[arg-type]
                        observed_at_utc=ts,
                        window_hours=1,
                        raw_value_text=str(val),
                        raw_unit="µg/m³",
                        value_kind="concentration",
                        normalized_concentration=val,
                        normalized_unit="µg/m³",
                        calculated_sub_index=100,
                        is_valid=True,
                        validation_note="ok",
                        raw_payload_json="{}",
                        ingested_at_utc=ts,
                    )
                )

        with get_db_connection() as conn:
            insert_raw_observations(conn, fresh_obs)
            recompute_district_history_window(conn, anchor_date_str=format_ist_date(fresh_end), days=2)

        resp_fresh = self.client.get("/api/health").json()
        self.assertTrue(resp_fresh["liveDataReady"])
        self.assertEqual(resp_fresh["freshDistrictsCount"], 1)
        self.assertEqual(resp_fresh["providers"]["cpcb_ogd"]["lastSyncStatus"], "failed")
        self.assertEqual(resp_fresh["providers"]["cpcb_ogd"]["lastSyncError"], "CPCB 503 error")
        self.assertEqual(resp_fresh["providers"]["openaq_v3"]["lastSyncStatus"], "succeeded")
        self.assertTrue(resp_fresh["providers"]["openaq_v3"]["hasFreshData"])

    def test_health_never_run_and_all_failed_never_qualify_even_with_fresh_records(self) -> None:
        """Verify never_run and failed sync statuses never produce liveDataReady=True even when fresh DB rows exist."""
        st = list_verified_stations_for_district("new-delhi")[0]
        now_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        fresh_end = now_utc - timedelta(hours=1)

        fresh_obs = []
        for h in range(18):
            ts = (fresh_end - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for p_key, val in [("PM2.5", 60.0), ("PM10", 100.0), ("NO2", 40.0)]:
                fresh_obs.append(
                    RawStationObservation(
                        station_id=st.station_id,
                        source_provider="openaq_v3",
                        upstream_sensor_or_record_id=f"openaq_sensor:81181:{p_key}:{ts}",
                        pollutant_key=p_key,  # type: ignore[arg-type]
                        observed_at_utc=ts,
                        window_hours=1,
                        raw_value_text=str(val),
                        raw_unit="µg/m³",
                        value_kind="concentration",
                        normalized_concentration=val,
                        normalized_unit="µg/m³",
                        calculated_sub_index=100,
                        is_valid=True,
                        validation_note="ok",
                        raw_payload_json="{}",
                        ingested_at_utc=ts,
                    )
                )

        with get_db_connection() as conn:
            seed_registry(conn)
            insert_raw_observations(conn, fresh_obs)
            recompute_district_history_window(conn, anchor_date_str=format_ist_date(fresh_end), days=2)

        # Scenario 1: Keys configured + fresh valid observations + fresh district AQI, but NO sync run (never_run)
        resp_never_run = self.client.get("/api/health").json()
        self.assertEqual(resp_never_run["lastSyncStatus"], "never_run")
        self.assertEqual(resp_never_run["freshDistrictsCount"], 1)
        self.assertEqual(resp_never_run["providers"]["openaq_v3"]["lastSyncStatus"], "never_run")
        self.assertTrue(resp_never_run["providers"]["openaq_v3"]["hasFreshData"])
        self.assertFalse(resp_never_run["liveDataReady"])

        # Scenario 2: Sync run recorded where both providers failed -> liveDataReady must still be False
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO ingestion_sync_runs (
                    started_at_utc, completed_at_utc, overall_status,
                    cpcb_status, cpcb_error, openaq_status, openaq_error
                )
                VALUES (?, ?, 'failed', 'failed', 'CPCB 503', 'failed', 'OpenAQ 500')
                """,
                (fresh_end.strftime("%Y-%m-%dT%H:%M:%SZ"), fresh_end.strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
            conn.commit()

        resp_failed = self.client.get("/api/health").json()
        self.assertEqual(resp_failed["lastSyncStatus"], "failed")
        self.assertFalse(resp_failed["liveDataReady"])




class TestSharedHttpRateLimitingAcrossSync(unittest.TestCase):
    """Regression tests verifying a single shared RateLimitedHttpClient across an entire run_sync() execution."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_shared_http.db")
        self.prev_env = {
            k: os.environ.get(k)
            for k in (
                "DATABASE_URL",
                "DATA_GOV_IN_API_KEY",
                "OPENAQ_API_KEY",
                "MIN_REQUEST_INTERVAL_SEC",
                "MAX_HTTP_RETRIES",
            )
        }
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        os.environ["DATA_GOV_IN_API_KEY"] = "cpcb-live-test-key"
        os.environ["OPENAQ_API_KEY"] = "openaq-live-test-key"
        os.environ["MIN_REQUEST_INTERVAL_SEC"] = "1.0"
        os.environ["MAX_HTTP_RETRIES"] = "2"

    def tearDown(self) -> None:
        for k, val in self.prev_env.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        self.temp_dir.cleanup()

    def test_single_shared_client_across_all_adapters_and_cross_provider_pacing(self) -> None:
        """Verify all CPCB & OpenAQ calls in one sync share one RateLimitedHttpClient and maintain pacing across providers."""
        import app.cli as cli_mod
        from app.ingestion.http_utils import create_shared_http_client

        sleeps: List[float] = []
        clock_val = [1000.0]
        request_log: List[Dict[str, Any]] = []
        observed_clients_in_adapters: List[RateLimitedHttpClient] = []

        def fake_sleep(sec: float) -> None:
            sleeps.append(round(sec, 3))
            clock_val[0] += sec

        def fake_clock() -> float:
            return clock_val[0]

        def mock_transport_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            provider = "cpcb" if "data.gov.in" in url_str else "openaq"
            request_log.append(
                {
                    "provider": provider,
                    "url": url_str,
                    "timestamp": round(clock_val[0], 3),
                }
            )
            # Simulate a tiny 0.1s network elapsed time per request (< 1.0s min_interval_sec)
            clock_val[0] += 0.1

            if "data.gov.in" in url_str:
                return httpx.Response(200, json={"records": []})
            if "/v3/locations/" in url_str:
                loc_id = int(url_str.split("/v3/locations/")[1].split("?")[0])
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": loc_id,
                                "name": f"Station {loc_id}",
                                "isMonitor": True,
                                "isMobile": False,
                                "datetimeLast": {"utc": "2026-11-02T11:00:00Z"},
                                "sensors": [
                                    {"id": loc_id * 10 + 1, "parameter": {"name": "pm25", "units": "µg/m³"}},
                                    {"id": loc_id * 10 + 2, "parameter": {"name": "pm10", "units": "µg/m³"}},
                                    {"id": loc_id * 10 + 3, "parameter": {"name": "no2", "units": "µg/m³"}},
                                ],
                            }
                        ]
                    },
                )
            if "/v3/locations" in url_str:
                return httpx.Response(200, json={"results": []})
            if "/v3/sensors/" in url_str and "/hours" in url_str:
                return httpx.Response(200, json={"results": []})
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(mock_transport_handler)
        created_clients: List[RateLimitedHttpClient] = []

        def factory_spy(settings=None):
            client = create_shared_http_client(
                settings,
                sleep_fn=fake_sleep,
                clock_fn=fake_clock,
                transport=transport,
            )
            created_clients.append(client)
            return client

        orig_cpcb = cli_mod.fetch_cpcb_ogd_page
        orig_oaq_loc = cli_mod.fetch_openaq_location
        orig_oaq_disc = cli_mod.discover_openaq_locations_near_coordinates
        orig_oaq_hours = cli_mod.fetch_openaq_sensor_hours

        def wrap_cpcb(*args, **kwargs):
            observed_clients_in_adapters.append(kwargs.get("http_client"))
            return orig_cpcb(*args, **kwargs)

        def wrap_oaq_loc(*args, **kwargs):
            observed_clients_in_adapters.append(kwargs.get("http_client"))
            return orig_oaq_loc(*args, **kwargs)

        def wrap_oaq_disc(*args, **kwargs):
            observed_clients_in_adapters.append(kwargs.get("http_client"))
            return orig_oaq_disc(*args, **kwargs)

        def wrap_oaq_hours(*args, **kwargs):
            observed_clients_in_adapters.append(kwargs.get("http_client"))
            return orig_oaq_hours(*args, **kwargs)

        ref_now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
        with (
            patch.object(cli_mod, "create_shared_http_client", side_effect=factory_spy),
            patch.object(cli_mod, "fetch_cpcb_ogd_page", side_effect=wrap_cpcb),
            patch.object(cli_mod, "fetch_openaq_location", side_effect=wrap_oaq_loc),
            patch.object(cli_mod, "discover_openaq_locations_near_coordinates", side_effect=wrap_oaq_disc),
            patch.object(cli_mod, "fetch_openaq_sensor_hours", side_effect=wrap_oaq_hours),
        ):
            res = run_sync(days=2, overlap_hours=3, now_utc=ref_now)

        self.assertEqual(res["overall_status"], "succeeded")
        self.assertEqual(res["cpcb_status"], "succeeded")
        self.assertEqual(res["openaq_status"], "succeeded")

        # 1. Verify exactly ONE RateLimitedHttpClient was created for the entire sync run
        self.assertEqual(len(created_clients), 1)
        shared_client = created_clients[0]
        self.assertIsInstance(shared_client, RateLimitedHttpClient)

        # 2. Verify every single CPCB & OpenAQ adapter invocation received that exact same client instance
        self.assertGreater(len(observed_clients_in_adapters), 10)
        self.assertTrue(all(c is shared_client for c in observed_clients_in_adapters))
        self.assertEqual(shared_client.request_count, len(request_log))

        # 3. Verify request pacing (MIN_REQUEST_INTERVAL_SEC=1.0s) was enforced across ALL requests,
        # including across the CPCB -> OpenAQ provider transition boundary!
        cpcb_indices = [i for i, r in enumerate(request_log) if r["provider"] == "cpcb"]
        openaq_indices = [i for i, r in enumerate(request_log) if r["provider"] == "openaq"]
        self.assertGreater(len(cpcb_indices), 0)
        self.assertGreater(len(openaq_indices), 0)
        first_openaq_idx = openaq_indices[0]
        self.assertEqual(first_openaq_idx, cpcb_indices[-1] + 1)

        for i in range(1, len(request_log)):
            prev_ts = request_log[i - 1]["timestamp"]
            curr_ts = request_log[i]["timestamp"]
            # Because each request takes 0.1s and pacing sleeps 0.9s before the next request,
            # consecutive request timestamps must be separated by at least 1.0s (including CPCB -> OpenAQ)
            self.assertGreaterEqual(
                round(curr_ts - prev_ts, 3),
                1.0,
                f"Pacing violated between request {i-1} ({request_log[i-1]}) and {i} ({request_log[i]})",
            )

        # Every request after the first required a 0.9s pacing sleep
        self.assertEqual(len(sleeps), len(request_log) - 1)
        self.assertTrue(all(s == 0.9 for s in sleeps))

    def test_new_sync_run_creates_fresh_client_and_preserves_retry_and_isolation(self) -> None:
        """Verify separate run_sync() runs create distinct clients while preserving retry/backoff & failure isolation."""
        import app.cli as cli_mod
        from app.ingestion.http_utils import create_shared_http_client

        sleeps: List[float] = []
        clock_val = [5000.0]

        def fake_sleep(sec: float) -> None:
            sleeps.append(round(sec, 3))
            clock_val[0] += sec

        def fake_clock() -> float:
            return clock_val[0]

        cpcb_attempts = [0]
        openaq_loc_attempts = [0]

        def handler_with_retries_and_cpcb_outage(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "data.gov.in" in url_str:
                cpcb_attempts[0] += 1
                # Always return 503 so CPCB retries max_retries (2) times and then fails safely
                return httpx.Response(503, json={"error": "CPCB gateway unavailable"})

            if "/v3/locations/8118" in url_str:
                openaq_loc_attempts[0] += 1
                # First attempt hits 429 with Retry-After=2.0, second attempt succeeds
                if openaq_loc_attempts[0] == 1:
                    return httpx.Response(429, headers={"Retry-After": "2.0"}, json={"error": "rate_limited"})
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": 8118,
                                "name": "Mandir Marg",
                                "isMonitor": True,
                                "isMobile": False,
                                "datetimeLast": {"utc": "2026-11-02T11:00:00Z"},
                                "sensors": [
                                    {"id": 81181, "parameter": {"name": "pm25", "units": "µg/m³"}},
                                    {"id": 81182, "parameter": {"name": "pm10", "units": "µg/m³"}},
                                    {"id": 81183, "parameter": {"name": "no2", "units": "µg/m³"}},
                                ],
                            }
                        ]
                    },
                )
            if "/v3/locations" in url_str:
                return httpx.Response(200, json={"results": []})
            if "/v3/sensors/" in url_str and "/hours" in url_str:
                return httpx.Response(200, json={"results": []})
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler_with_retries_and_cpcb_outage)
        created_clients: List[RateLimitedHttpClient] = []

        def factory_spy(settings=None):
            client = create_shared_http_client(
                settings,
                sleep_fn=fake_sleep,
                clock_fn=fake_clock,
                transport=transport,
            )
            created_clients.append(client)
            return client

        ref_now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
        with patch.object(cli_mod, "create_shared_http_client", side_effect=factory_spy):
            res_run1 = run_sync(days=1, overlap_hours=3, now_utc=ref_now)
            res_run2 = run_sync(days=1, overlap_hours=3, now_utc=ref_now + timedelta(minutes=30))

        # Verify each sync run instantiated its own fresh RateLimitedHttpClient
        self.assertEqual(len(created_clients), 2)
        self.assertIsNot(created_clients[0], created_clients[1])

        # Verify Run 1 retried CPCB (1 initial + 2 retries = 3 attempts), failed CPCB,
        # retried OpenAQ 429 once (sleeping 2.0s from Retry-After), and succeeded on OpenAQ
        self.assertEqual(res_run1["overall_status"], "partial_failure")
        self.assertEqual(res_run1["cpcb_status"], "failed")
        self.assertEqual(res_run1["openaq_status"], "succeeded")
        self.assertIn(2.0, sleeps)
        # Across 2 runs, CPCB attempted 3 times per run = 6 total attempts
        self.assertEqual(cpcb_attempts[0], 6)




class TestLegacyUnverifiedCpcbMigrationAndRecomputation(unittest.TestCase):
    """Regression tests for Migration 003 invalidating legacy unitless CPCB rows and clearing stale derived AQIs."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_legacy_cpcb.db")
        self.prev_db_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if self.prev_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.prev_db_url
        self.temp_dir.cleanup()

    def test_migration_003_invalidates_legacy_unitless_cpcb_rows_and_preserves_audit_trail(self) -> None:
        """Verify a pre-003 database with accepted unitless CPCB rows is safely invalidated without deleting raw rows."""
        from app.migrations import MIGRATIONS, run_migrations, upsert_daily_district_history, upsert_districts, upsert_stations
        from app.models import DailyDistrictHistoryRecord
        from app.districts_registry import list_target_districts, REVIEWED_STATIONS

        with get_db_connection() as conn:
            # Simulate a pre-003 database by applying only migrations 1 and 2
            for version, name, sql in MIGRATIONS[:2]:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at_utc) VALUES (?, ?, ?)",
                    (version, name, "2026-11-01T00:00:00Z"),
                )
            upsert_districts(conn, list_target_districts())
            upsert_stations(conn, REVIEWED_STATIONS)

            # Insert 72 legacy unitless CPCB observations (accepted by old normalizer with is_valid=1, value_kind='sub_index')
            # plus 1 verified concentration CPCB observation (raw_unit='µg/m³') that MUST remain valid
            st = list_verified_stations_for_district("new-delhi")[0]
            base_dt = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
            legacy_obs = []
            for h in range(18):
                ts = (base_dt - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for idx, p_key in enumerate(("PM2.5", "PM10", "NO2", "SO2")):
                    legacy_obs.append(
                        RawStationObservation(
                            station_id=st.station_id,
                            source_provider="cpcb_ogd",
                            upstream_sensor_or_record_id=f"legacy:{p_key}:{ts}",
                            pollutant_key=p_key,  # type: ignore[arg-type]
                            observed_at_utc=ts,
                            window_hours=24,
                            raw_value_text="218",
                            raw_unit=None if idx % 2 == 0 else "NA",
                            value_kind="sub_index",
                            normalized_concentration=None,
                            normalized_unit=None,
                            calculated_sub_index=218,
                            is_valid=True,
                            validation_note="Legacy accepted unitless CPCB sub-index",
                            raw_payload_json='{"pollutant_avg":"218","pollutant_unit":"NA"}',
                            ingested_at_utc=ts,
                        )
                    )
            # Also add 1 explicitly unit-verified CPCB observation
            legacy_obs.append(
                RawStationObservation(
                    station_id=st.station_id,
                    source_provider="cpcb_ogd",
                    upstream_sensor_or_record_id="verified:CO:2026-11-02T12:00:00Z",
                    pollutant_key="CO",
                    observed_at_utc="2026-11-02T12:00:00Z",
                    window_hours=8,
                    raw_value_text="1.2",
                    raw_unit="mg/m³",
                    value_kind="concentration",
                    normalized_concentration=1.2,
                    normalized_unit="mg/m³",
                    calculated_sub_index=60,
                    is_valid=True,
                    validation_note="Verified mg/m³",
                    raw_payload_json='{"pollutant_avg":"1.2","pollutant_unit":"mg/m³"}',
                    ingested_at_utc="2026-11-02T12:00:00Z",
                )
            )
            insert_raw_observations(conn, legacy_obs)

            # Insert a stale derived daily_district_history record from those legacy CPCB rows
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="new-delhi",
                    date="2026-11-02",
                    district_aqi=218,
                    dominant_pollutant="PM2.5",
                    updated_at_utc="2026-11-02T12:00:00Z",
                    source_attribution="CPCB / SPCB CAAQMS via data.gov.in (GODL-India)",
                    reporting_stations=1,
                    registered_stations=6,
                    observed_station_pollutant_hours=96,
                    expected_station_pollutant_hours=672,
                    coverage=round(96 / 672, 4),
                    quality="low",
                    pollutants_json='[{"key":"PM2.5","value":null,"unit":"µg/m³","subIndex":218}]',
                    caveats_json="[]",
                ),
            )
            conn.commit()

            # Now run migrations -> Migration 3 must apply and be idempotent on subsequent calls
            newly_applied = run_migrations(conn)
            self.assertEqual(newly_applied, [3])
            self.assertEqual(run_migrations(conn), [])

            # 1. Verify NO raw observations were deleted (all 73 rows preserved for auditability)
            total_rows = conn.execute("SELECT COUNT(*) AS cnt FROM raw_station_observations").fetchone()["cnt"]
            self.assertEqual(total_rows, 73)

            # 2. Verify all 72 unitless/NA CPCB rows were invalidated (is_valid=0, calculated_sub_index=NULL)
            invalidated_rows = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM raw_station_observations
                WHERE source_provider = 'cpcb_ogd'
                  AND upstream_sensor_or_record_id LIKE 'legacy:%'
                  AND is_valid = 0
                  AND value_kind = 'invalid'
                  AND calculated_sub_index IS NULL
                """
            ).fetchone()["cnt"]
            self.assertEqual(invalidated_rows, 72)

            # 3. Verify the 1 CPCB row with an explicit physical unit ('mg/m³') was NOT invalidated
            verified_row = conn.execute(
                "SELECT is_valid, calculated_sub_index FROM raw_station_observations WHERE upstream_sensor_or_record_id = 'verified:CO:2026-11-02T12:00:00Z'"
            ).fetchone()
            self.assertEqual(verified_row["is_valid"], 1)
            self.assertEqual(verified_row["calculated_sub_index"], 60)

            # 4. Run recompute_district_history_window -> must not leave stale 218 AQI on 2026-11-02
            recompute_district_history_window(conn, anchor_date_str="2026-11-02", days=2)

            hist_after = conn.execute(
                "SELECT * FROM daily_district_history WHERE district_id = 'new-delhi' AND date = '2026-11-02'"
            ).fetchone()
            self.assertIsNotNone(hist_after)
            self.assertIsNone(hist_after["district_aqi"])
            self.assertEqual(hist_after["quality"], "unavailable")
            self.assertEqual(hist_after["reporting_stations"], 0)

        # Verify public API snapshot for new-delhi on 2026-11-02 reflects unavailable (no stale AQI)
        snap = self.client.get("/api/snapshots/new-delhi?anchor_date=2026-11-02").json()
        self.assertIsNone(snap["aqi"])
        self.assertEqual(snap["quality"], "unavailable")
        self.assertEqual(snap["stations"], 0)

    def test_recompute_clears_stale_district_aqi_when_all_observations_on_day_become_invalid(self) -> None:
        """Verify recompute_district_history_window clears an existing daily AQI even when valid_day_obs is empty."""
        from app.migrations import upsert_daily_district_history
        from app.models import DailyDistrictHistoryRecord

        with get_db_connection() as conn:
            seed_registry(conn)
            # Pre-existing daily history record on 2026-11-02 with district_aqi=175, but 0 valid observations in DB
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="lucknow",
                    date="2026-11-02",
                    district_aqi=175,
                    dominant_pollutant="PM2.5",
                    updated_at_utc="2026-11-02T10:00:00Z",
                    source_attribution="CPCB / SPCB CAAQMS via OpenAQ v3",
                    reporting_stations=2,
                    registered_stations=4,
                    observed_station_pollutant_hours=180,
                    expected_station_pollutant_hours=448,
                    coverage=round(180 / 448, 4),
                    quality="medium",
                    pollutants_json='[{"key":"PM2.5","value":82.0,"unit":"µg/m³","subIndex":175}]',
                    caveats_json="[]",
                ),
            )
            conn.commit()

            # Recompute across 2026-11-02 when no valid observations remain for Lucknow
            recompute_district_history_window(conn, anchor_date_str="2026-11-02", days=2)

            row = conn.execute(
                "SELECT * FROM daily_district_history WHERE district_id = 'lucknow' AND date = '2026-11-02'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row["district_aqi"])
            self.assertIsNone(row["dominant_pollutant"])
            self.assertIsNone(row["updated_at_utc"])
            self.assertEqual(row["reporting_stations"], 0)
            self.assertEqual(row["coverage"], 0.0)
            self.assertEqual(row["quality"], "unavailable")



    def test_hybrid_evaluation_with_unitless_cpcb_dependency_invalidates_or_recalculates_accurately(self) -> None:
        """Verify hybrid daily records never retain stale AQI when CPCB is invalidated, and recalculate when OpenAQ suffices."""
        from app.migrations import (
            MIGRATIONS,
            run_migrations,
            upsert_daily_district_history,
            upsert_districts,
            upsert_station_evaluation,
            upsert_stations,
        )
        from app.models import DailyDistrictHistoryRecord
        from app.districts_registry import list_target_districts, REVIEWED_STATIONS

        with get_db_connection() as conn:
            for version, name, sql in MIGRATIONS[:2]:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at_utc) VALUES (?, ?, ?)",
                    (version, name, "2026-11-01T00:00:00Z"),
                )
            upsert_districts(conn, list_target_districts())
            upsert_stations(conn, REVIEWED_STATIONS)

            # Case 1 (Lucknow on 2026-11-02): Hybrid record where PM2.5 & PM10 came from unitless CPCB (180)
            # and only NO2 came from OpenAQ (50). Once unitless CPCB is invalidated, remaining OpenAQ (1 pollutant, no PM)
            # is insufficient, so BOTH station_aqi_evaluations AND daily_district_history must become NULL/unavailable.
            lko_st = list_verified_stations_for_district("lucknow")[0]
            base_dt = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
            case1_obs = []
            for h in range(18):
                ts = (base_dt - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for p_key in ("PM2.5", "PM10"):
                    case1_obs.append(
                        RawStationObservation(
                            station_id=lko_st.station_id,
                            source_provider="cpcb_ogd",
                            upstream_sensor_or_record_id=f"lko:cpcb:{p_key}:{ts}",
                            pollutant_key=p_key,  # type: ignore[arg-type]
                            observed_at_utc=ts,
                            window_hours=24,
                            raw_value_text="180",
                            raw_unit="NA",
                            value_kind="sub_index",
                            normalized_concentration=None,
                            normalized_unit=None,
                            calculated_sub_index=180,
                            is_valid=True,
                            validation_note="Legacy unitless CPCB",
                            raw_payload_json="{}",
                            ingested_at_utc=ts,
                        )
                    )
                case1_obs.append(
                    RawStationObservation(
                        station_id=lko_st.station_id,
                        source_provider="openaq_v3",
                        upstream_sensor_or_record_id=f"openaq_sensor:90013:{ts}",
                        pollutant_key="NO2",
                        observed_at_utc=ts,
                        window_hours=1,
                        raw_value_text="40.0",
                        raw_unit="µg/m³",
                        value_kind="concentration",
                        normalized_concentration=40.0,
                        normalized_unit="µg/m³",
                        calculated_sub_index=50,
                        is_valid=True,
                        validation_note="Verified OpenAQ NO2",
                        raw_payload_json="{}",
                        ingested_at_utc=ts,
                    )
                )
            insert_raw_observations(conn, case1_obs)
            ev_case1 = evaluate_station_window(
                station=lko_st, observations=case1_obs, evaluation_end_utc=base_dt
            )
            self.assertEqual(ev_case1.source_provider, "hybrid_cpcb_openaq")
            self.assertEqual(ev_case1.station_aqi, 180)
            upsert_station_evaluation(conn, ev_case1)
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="lucknow",
                    date="2026-11-02",
                    district_aqi=180,
                    dominant_pollutant="PM2.5",
                    updated_at_utc="2026-11-02T12:00:00Z",
                    source_attribution="CPCB / SPCB CAAQMS via OpenAQ v3 & data.gov.in (GODL-India)",
                    reporting_stations=1,
                    registered_stations=4,
                    observed_station_pollutant_hours=72,
                    expected_station_pollutant_hours=448,
                    coverage=round(72 / 448, 4),
                    quality="medium",
                    pollutants_json='[{"key":"PM2.5","value":null,"unit":"µg/m³","subIndex":180}]',
                    caveats_json="[]",
                ),
            )

            # Case 2 (Patna on 2026-11-02): Hybrid record where OpenAQ has 18h of valid PM2.5 (60 -> sub 100),
            # PM10 (100 -> sub 100), and NO2 (40 -> sub 50), PLUS unitless CPCB SO2 (240) that inflated hybrid AQI to 240.
            patna_st = list_verified_stations_for_district("patna")[0]
            case2_obs = []
            for h in range(18):
                ts = (base_dt - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for p_key, conc, sub_i in [("PM2.5", 60.0, 100), ("PM10", 100.0, 100), ("NO2", 40.0, 50)]:
                    case2_obs.append(
                        RawStationObservation(
                            station_id=patna_st.station_id,
                            source_provider="openaq_v3",
                            upstream_sensor_or_record_id=f"openaq_sensor:7001:{p_key}:{ts}",
                            pollutant_key=p_key,  # type: ignore[arg-type]
                            observed_at_utc=ts,
                            window_hours=1,
                            raw_value_text=str(conc),
                            raw_unit="µg/m³",
                            value_kind="concentration",
                            normalized_concentration=conc,
                            normalized_unit="µg/m³",
                            calculated_sub_index=sub_i,
                            is_valid=True,
                            validation_note="Verified OpenAQ",
                            raw_payload_json="{}",
                            ingested_at_utc=ts,
                        )
                    )
                case2_obs.append(
                    RawStationObservation(
                        station_id=patna_st.station_id,
                        source_provider="cpcb_ogd",
                        upstream_sensor_or_record_id=f"patna:cpcb:SO2:{ts}",
                        pollutant_key="SO2",
                        observed_at_utc=ts,
                        window_hours=24,
                        raw_value_text="240",
                        raw_unit=None,
                        value_kind="sub_index",
                        normalized_concentration=None,
                        normalized_unit=None,
                        calculated_sub_index=240,
                        is_valid=True,
                        validation_note="Legacy unitless CPCB SO2",
                        raw_payload_json="{}",
                        ingested_at_utc=ts,
                    )
                )
            insert_raw_observations(conn, case2_obs)
            ev_case2 = evaluate_station_window(
                station=patna_st, observations=case2_obs, evaluation_end_utc=base_dt
            )
            self.assertEqual(ev_case2.source_provider, "hybrid_cpcb_openaq")
            self.assertEqual(ev_case2.station_aqi, 240)
            upsert_station_evaluation(conn, ev_case2)
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="patna",
                    date="2026-11-02",
                    district_aqi=240,
                    dominant_pollutant="SO2",
                    updated_at_utc="2026-11-02T12:00:00Z",
                    source_attribution="CPCB / SPCB CAAQMS via OpenAQ v3 & data.gov.in (GODL-India)",
                    reporting_stations=1,
                    registered_stations=6,
                    observed_station_pollutant_hours=96,
                    expected_station_pollutant_hours=672,
                    coverage=round(96 / 672, 4),
                    quality="low",
                    pollutants_json='[{"key":"SO2","value":null,"unit":"µg/m³","subIndex":240}]',
                    caveats_json="[]",
                ),
            )
            conn.commit()

            # Apply Migration 003 (without manually calling recompute_district_history_window)
            applied = run_migrations(conn)
            self.assertEqual(applied, [3])

            # Verify Case 1 (Lucknow): BOTH station evaluation AND daily_district_history are now invalidated
            lko_eval = conn.execute(
                "SELECT station_aqi, is_valid_station_aqi FROM station_aqi_evaluations WHERE station_id = ? AND evaluation_date = '2026-11-02'",
                (lko_st.station_id,),
            ).fetchone()
            self.assertIsNotNone(lko_eval)
            self.assertIsNone(lko_eval["station_aqi"])
            self.assertEqual(lko_eval["is_valid_station_aqi"], 0)

            lko_daily = conn.execute(
                "SELECT district_aqi, quality, reporting_stations FROM daily_district_history WHERE district_id = 'lucknow' AND date = '2026-11-02'"
            ).fetchone()
            self.assertIsNotNone(lko_daily)
            self.assertIsNone(lko_daily["district_aqi"])
            self.assertEqual(lko_daily["quality"], "unavailable")
            self.assertEqual(lko_daily["reporting_stations"], 0)

            # Verify Case 2 (Patna): Recalculated from remaining valid OpenAQ observations to AQI=100 (PM2.5)
            patna_eval = conn.execute(
                "SELECT station_aqi, dominant_pollutant, source_provider, is_valid_station_aqi FROM station_aqi_evaluations WHERE station_id = ? AND evaluation_date = '2026-11-02'",
                (patna_st.station_id,),
            ).fetchone()
            self.assertIsNotNone(patna_eval)
            self.assertEqual(patna_eval["station_aqi"], 100)
            self.assertEqual(patna_eval["dominant_pollutant"], "PM2.5")
            self.assertEqual(patna_eval["source_provider"], "openaq_v3")
            self.assertEqual(patna_eval["is_valid_station_aqi"], 1)

            patna_daily = conn.execute(
                "SELECT district_aqi, dominant_pollutant, source_attribution, reporting_stations FROM daily_district_history WHERE district_id = 'patna' AND date = '2026-11-02'"
            ).fetchone()
            self.assertIsNotNone(patna_daily)
            self.assertEqual(patna_daily["district_aqi"], 100)
            self.assertEqual(patna_daily["dominant_pollutant"], "PM2.5")
            self.assertEqual(patna_daily["source_attribution"], "CPCB / SPCB CAAQMS via OpenAQ v3")
            self.assertEqual(patna_daily["reporting_stations"], 1)

    def test_cpcb_explicit_units_preserved_and_historical_dates_older_than_30_days_handled(self) -> None:
        """Verify CPCB records with explicit physical units remain valid and dates >30 days old are handled accurately."""
        from app.migrations import (
            MIGRATIONS,
            run_migrations,
            upsert_daily_district_history,
            upsert_districts,
            upsert_station_evaluation,
            upsert_stations,
        )
        from app.models import DailyDistrictHistoryRecord
        from app.districts_registry import list_target_districts, REVIEWED_STATIONS

        with get_db_connection() as conn:
            for version, name, sql in MIGRATIONS[:2]:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at_utc) VALUES (?, ?, ?)",
                    (version, name, "2026-08-01T00:00:00Z"),
                )
            upsert_districts(conn, list_target_districts())
            upsert_stations(conn, REVIEWED_STATIONS)

            kol_st = list_verified_stations_for_district("kolkata")[0]
            # Historical Date 1: 2026-09-01 (62 days before 2026-11-02, older than 30-day window)
            # CPCB-only record supported EXCLUSIVELY by explicit physical units (µg/m³) -> MUST remain valid (AQI=100)
            dt_sep1 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
            sep1_obs = []
            for h in range(18):
                ts = (dt_sep1 - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for p_key, conc, sub_i in [("PM2.5", 60.0, 100), ("PM10", 100.0, 100), ("NO2", 40.0, 50)]:
                    sep1_obs.append(
                        RawStationObservation(
                            station_id=kol_st.station_id,
                            source_provider="cpcb_ogd",
                            upstream_sensor_or_record_id=f"kol:sep1:{p_key}:{ts}",
                            pollutant_key=p_key,  # type: ignore[arg-type]
                            observed_at_utc=ts,
                            window_hours=1,
                            raw_value_text=str(conc),
                            raw_unit="µg/m³",
                            value_kind="concentration",
                            normalized_concentration=conc,
                            normalized_unit="µg/m³",
                            calculated_sub_index=sub_i,
                            is_valid=True,
                            validation_note="Verified µg/m³ CPCB",
                            raw_payload_json="{}",
                            ingested_at_utc=ts,
                        )
                    )
            insert_raw_observations(conn, sep1_obs)
            ev_sep1 = evaluate_station_window(
                station=kol_st, observations=sep1_obs, evaluation_end_utc=dt_sep1
            )
            self.assertEqual(ev_sep1.source_provider, "cpcb_ogd")
            self.assertEqual(ev_sep1.station_aqi, 100)
            upsert_station_evaluation(conn, ev_sep1)
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="kolkata",
                    date="2026-09-01",
                    district_aqi=100,
                    dominant_pollutant="PM2.5",
                    updated_at_utc="2026-09-01T12:00:00Z",
                    source_attribution="CPCB / SPCB CAAQMS via data.gov.in (GODL-India)",
                    reporting_stations=1,
                    registered_stations=7,
                    observed_station_pollutant_hours=54,
                    expected_station_pollutant_hours=784,
                    coverage=round(54 / 784, 4),
                    quality="low",
                    pollutants_json='[{"key":"PM2.5","value":60.0,"unit":"µg/m³","subIndex":100}]',
                    caveats_json="[]",
                ),
            )

            # Historical Date 2: 2026-08-20 (74 days before 2026-11-02, older than 30-day window)
            # CPCB-only record dependent on unitless legacy observations -> MUST be invalidated even though >30 days old
            dt_aug20 = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
            aug20_obs = []
            for h in range(18):
                ts = (dt_aug20 - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for p_key in ("PM2.5", "PM10", "NO2"):
                    aug20_obs.append(
                        RawStationObservation(
                            station_id=kol_st.station_id,
                            source_provider="cpcb_ogd",
                            upstream_sensor_or_record_id=f"kol:aug20:{p_key}:{ts}",
                            pollutant_key=p_key,  # type: ignore[arg-type]
                            observed_at_utc=ts,
                            window_hours=24,
                            raw_value_text="195",
                            raw_unit="NA",
                            value_kind="sub_index",
                            normalized_concentration=None,
                            normalized_unit=None,
                            calculated_sub_index=195,
                            is_valid=True,
                            validation_note="Legacy unitless CPCB",
                            raw_payload_json="{}",
                            ingested_at_utc=ts,
                        )
                    )
            insert_raw_observations(conn, aug20_obs)
            ev_aug20 = evaluate_station_window(
                station=kol_st, observations=aug20_obs, evaluation_end_utc=dt_aug20
            )
            upsert_station_evaluation(conn, ev_aug20)
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="kolkata",
                    date="2026-08-20",
                    district_aqi=195,
                    dominant_pollutant="PM2.5",
                    updated_at_utc="2026-08-20T12:00:00Z",
                    source_attribution="CPCB / SPCB CAAQMS via data.gov.in (GODL-India)",
                    reporting_stations=1,
                    registered_stations=7,
                    observed_station_pollutant_hours=54,
                    expected_station_pollutant_hours=784,
                    coverage=round(54 / 784, 4),
                    quality="low",
                    pollutants_json='[{"key":"PM2.5","value":null,"unit":"µg/m³","subIndex":195}]',
                    caveats_json="[]",
                ),
            )
            conn.commit()

            # Run Migration 003
            self.assertEqual(run_migrations(conn), [3])

            # 1. Verify 2026-09-01 (explicit physical units, >30 days old) remained valid!
            sep1_eval_row = conn.execute(
                "SELECT station_aqi, is_valid_station_aqi, source_provider FROM station_aqi_evaluations WHERE station_id = ? AND evaluation_date = '2026-09-01'",
                (kol_st.station_id,),
            ).fetchone()
            self.assertEqual(sep1_eval_row["station_aqi"], 100)
            self.assertEqual(sep1_eval_row["is_valid_station_aqi"], 1)
            self.assertEqual(sep1_eval_row["source_provider"], "cpcb_ogd")

            sep1_daily_row = conn.execute(
                "SELECT district_aqi, quality, reporting_stations FROM daily_district_history WHERE district_id = 'kolkata' AND date = '2026-09-01'"
            ).fetchone()
            self.assertEqual(sep1_daily_row["district_aqi"], 100)
            self.assertEqual(sep1_daily_row["reporting_stations"], 1)

            # 2. Verify 2026-08-20 (unitless CPCB, >30 days old) was invalidated!
            aug20_eval_row = conn.execute(
                "SELECT station_aqi, is_valid_station_aqi FROM station_aqi_evaluations WHERE station_id = ? AND evaluation_date = '2026-08-20'",
                (kol_st.station_id,),
            ).fetchone()
            self.assertIsNone(aug20_eval_row["station_aqi"])
            self.assertEqual(aug20_eval_row["is_valid_station_aqi"], 0)

            aug20_daily_row = conn.execute(
                "SELECT district_aqi, quality, reporting_stations FROM daily_district_history WHERE district_id = 'kolkata' AND date = '2026-08-20'"
            ).fetchone()
            self.assertIsNone(aug20_daily_row["district_aqi"])
            self.assertEqual(aug20_daily_row["quality"], "unavailable")
            self.assertEqual(aug20_daily_row["reporting_stations"], 0)

            # 3. Also run a standard 30-day recompute anchored at 2026-11-02 and verify 2026-09-01 is still preserved
            recompute_district_history_window(conn, anchor_date_str="2026-11-02", days=30)
            sep1_after_30d_recompute = conn.execute(
                "SELECT district_aqi FROM daily_district_history WHERE district_id = 'kolkata' AND date = '2026-09-01'"
            ).fetchone()
            self.assertEqual(sep1_after_30d_recompute["district_aqi"], 100)



    def test_two_stations_in_same_district_with_different_cpcb_validity(self) -> None:
        """Verify station-level preservation: valid explicit-unit station is preserved while unitless/unbacked stations in same district are invalidated."""
        from app.migrations import (
            MIGRATIONS,
            run_migrations,
            upsert_daily_district_history,
            upsert_districts,
            upsert_station_evaluation,
            upsert_stations,
        )
        from app.models import DailyDistrictHistoryRecord
        from app.districts_registry import list_target_districts, REVIEWED_STATIONS

        with get_db_connection() as conn:
            for version, name, sql in MIGRATIONS[:2]:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at_utc) VALUES (?, ?, ?)",
                    (version, name, "2026-09-01T00:00:00Z"),
                )
            upsert_districts(conn, list_target_districts())
            upsert_stations(conn, REVIEWED_STATIONS)

            delhi_stations = list_verified_stations_for_district("new-delhi")
            st_valid = delhi_stations[0]      # Mandir Marg: backed by explicit physical units (µg/m³) -> AQI 100
            st_unitless = delhi_stations[1]   # R K Puram: backed by unitless ('NA') CPCB rows -> AQI 220 (must invalidate)
            st_unbacked = delhi_stations[2]   # Punjabi Bagh: unbacked evaluation row with 0 valid CPCB rows -> AQI 260 (must invalidate)

            eval_dt = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
            obs = []
            for h in range(18):
                ts = (eval_dt - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for p_key, conc, sub_i in [("PM2.5", 60.0, 100), ("PM10", 100.0, 100), ("NO2", 40.0, 50)]:
                    obs.append(
                        RawStationObservation(
                            station_id=st_valid.station_id,
                            source_provider="cpcb_ogd",
                            upstream_sensor_or_record_id=f"valid:{p_key}:{ts}",
                            pollutant_key=p_key,  # type: ignore[arg-type]
                            observed_at_utc=ts,
                            window_hours=1,
                            raw_value_text=str(conc),
                            raw_unit="µg/m³",
                            value_kind="concentration",
                            normalized_concentration=conc,
                            normalized_unit="µg/m³",
                            calculated_sub_index=sub_i,
                            is_valid=True,
                            validation_note="Verified µg/m³",
                            raw_payload_json="{}",
                            ingested_at_utc=ts,
                        )
                    )
                    obs.append(
                        RawStationObservation(
                            station_id=st_unitless.station_id,
                            source_provider="cpcb_ogd",
                            upstream_sensor_or_record_id=f"unitless:{p_key}:{ts}",
                            pollutant_key=p_key,  # type: ignore[arg-type]
                            observed_at_utc=ts,
                            window_hours=24,
                            raw_value_text="220",
                            raw_unit="NA",
                            value_kind="sub_index",
                            normalized_concentration=None,
                            normalized_unit=None,
                            calculated_sub_index=220,
                            is_valid=True,
                            validation_note="Legacy unitless CPCB",
                            raw_payload_json="{}",
                            ingested_at_utc=ts,
                        )
                    )
            insert_raw_observations(conn, obs)

            ev_valid = evaluate_station_window(
                station=st_valid,
                observations=[o for o in obs if o.station_id == st_valid.station_id],
                evaluation_end_utc=eval_dt,
            )
            ev_unitless = evaluate_station_window(
                station=st_unitless,
                observations=[o for o in obs if o.station_id == st_unitless.station_id],
                evaluation_end_utc=eval_dt,
            )
            upsert_station_evaluation(conn, ev_valid)
            upsert_station_evaluation(conn, ev_unitless)

            # Also insert an unbacked CPCB station evaluation for st_unbacked in the same district
            conn.execute(
                """
                INSERT INTO station_aqi_evaluations (
                    station_id, district_id, evaluation_timestamp_utc, evaluation_date,
                    source_provider, station_aqi, dominant_pollutant,
                    meets_16h_completeness, meets_3_pollutant_pm_rule, is_valid_station_aqi,
                    valid_pollutant_count, observed_pollutant_hours, expected_pollutant_hours,
                    pollutants_json, rejection_reasons_json
                )
                VALUES (?, 'new-delhi', '2026-09-10T12:00:00Z', '2026-09-10', 'cpcb_ogd', 260, 'PM2.5', 1, 1, 1, 3, 54, 112, '{}', '[]')
                """,
                (st_unbacked.station_id,),
            )

            # Pre-migration district history had mean of (100, 220, 260) = 193 across 3 reporting stations
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="new-delhi",
                    date="2026-09-10",
                    district_aqi=193,
                    dominant_pollutant="PM2.5",
                    updated_at_utc="2026-09-10T12:00:00Z",
                    source_attribution="CPCB / SPCB CAAQMS via data.gov.in (GODL-India)",
                    reporting_stations=3,
                    registered_stations=6,
                    observed_station_pollutant_hours=162,
                    expected_station_pollutant_hours=672,
                    coverage=round(162 / 672, 4),
                    quality="medium",
                    pollutants_json='[{"key":"PM2.5","value":60.0,"unit":"µg/m³","subIndex":193}]',
                    caveats_json="[]",
                ),
            )
            conn.commit()

            # Run Migration 003
            self.assertEqual(run_migrations(conn), [3])

            # 1. st_valid (Mandir Marg) MUST remain valid at AQI=100
            row_valid = conn.execute(
                "SELECT station_aqi, is_valid_station_aqi FROM station_aqi_evaluations WHERE station_id = ? AND evaluation_date = '2026-09-10'",
                (st_valid.station_id,),
            ).fetchone()
            self.assertEqual(row_valid["station_aqi"], 100)
            self.assertEqual(row_valid["is_valid_station_aqi"], 1)

            # 2. st_unitless (R K Puram) MUST be invalidated
            row_unitless = conn.execute(
                "SELECT station_aqi, is_valid_station_aqi FROM station_aqi_evaluations WHERE station_id = ? AND evaluation_date = '2026-09-10'",
                (st_unitless.station_id,),
            ).fetchone()
            self.assertIsNone(row_unitless["station_aqi"])
            self.assertEqual(row_unitless["is_valid_station_aqi"], 0)

            # 3. st_unbacked (Punjabi Bagh) MUST ALSO be invalidated (not masked by Mandir Marg's valid CPCB data!)
            row_unbacked = conn.execute(
                "SELECT station_aqi, is_valid_station_aqi FROM station_aqi_evaluations WHERE station_id = ? AND evaluation_date = '2026-09-10'",
                (st_unbacked.station_id,),
            ).fetchone()
            self.assertIsNone(row_unbacked["station_aqi"])
            self.assertEqual(row_unbacked["is_valid_station_aqi"], 0)

            # 4. District history for new-delhi on 2026-09-10 MUST be re-aggregated to AQI=100 with reporting_stations=1
            daily_row = conn.execute(
                "SELECT district_aqi, reporting_stations, source_attribution FROM daily_district_history WHERE district_id = 'new-delhi' AND date = '2026-09-10'"
            ).fetchone()
            self.assertEqual(daily_row["district_aqi"], 100)
            self.assertEqual(daily_row["reporting_stations"], 1)
            self.assertEqual(daily_row["source_attribution"], "CPCB / SPCB CAAQMS via data.gov.in (GODL-India)")

    def test_unitless_cpcb_observations_outside_24h_evaluation_window_do_not_corrupt_reconciliation(self) -> None:
        """Verify out-of-window unitless CPCB rows do not invalidate valid evaluations, and out-of-window valid rows do not suppress invalidation."""
        from app.migrations import (
            MIGRATIONS,
            run_migrations,
            upsert_daily_district_history,
            upsert_districts,
            upsert_station_evaluation,
            upsert_stations,
        )
        from app.models import DailyDistrictHistoryRecord
        from app.districts_registry import list_target_districts, REVIEWED_STATIONS

        with get_db_connection() as conn:
            for version, name, sql in MIGRATIONS[:2]:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at_utc) VALUES (?, ?, ?)",
                    (version, name, "2026-09-01T00:00:00Z"),
                )
            upsert_districts(conn, list_target_districts())
            upsert_stations(conn, REVIEWED_STATIONS)

            kol_st = list_verified_stations_for_district("kolkata")[0]

            # Subcase A (2026-09-15, evaluation anchored at 2026-09-15T10:00:00Z -> window (2026-09-14T10:00:00Z, 2026-09-15T10:00:00Z]):
            # Has 3 explicit-unit CPCB observations inside the window + a stored valid evaluation (AQI=100),
            # AND has unrelated unitless CPCB observations OUTSIDE the window at 2026-09-14T09:00:00Z (before window)
            # and 2026-09-15T14:00:00Z (after evaluation_timestamp_utc on the same IST date).
            insert_raw_observations(
                conn,
                [
                    RawStationObservation(
                        station_id=kol_st.station_id,
                        source_provider="cpcb_ogd",
                        upstream_sensor_or_record_id="in_win_valid_pm25",
                        pollutant_key="PM2.5",
                        observed_at_utc="2026-09-15T10:00:00Z",
                        window_hours=1,
                        raw_value_text="60.0",
                        raw_unit="µg/m³",
                        value_kind="concentration",
                        normalized_concentration=60.0,
                        normalized_unit="µg/m³",
                        calculated_sub_index=100,
                        is_valid=True,
                        validation_note="Verified µg/m³",
                        raw_payload_json="{}",
                        ingested_at_utc="2026-09-15T10:00:00Z",
                    ),
                    # Out-of-window unitless observation 1h BEFORE window start (2026-09-14T09:00:00Z <= 2026-09-14T10:00:00Z)
                    RawStationObservation(
                        station_id=kol_st.station_id,
                        source_provider="cpcb_ogd",
                        upstream_sensor_or_record_id="out_of_win_before",
                        pollutant_key="PM2.5",
                        observed_at_utc="2026-09-14T09:00:00Z",
                        window_hours=24,
                        raw_value_text="250",
                        raw_unit="NA",
                        value_kind="sub_index",
                        normalized_concentration=None,
                        normalized_unit=None,
                        calculated_sub_index=250,
                        is_valid=True,
                        validation_note="Legacy unitless before window",
                        raw_payload_json="{}",
                        ingested_at_utc="2026-09-14T09:00:00Z",
                    ),
                    # Out-of-window unitless observation 4h AFTER evaluation_timestamp_utc (2026-09-15T14:00:00Z > 2026-09-15T10:00:00Z)
                    RawStationObservation(
                        station_id=kol_st.station_id,
                        source_provider="cpcb_ogd",
                        upstream_sensor_or_record_id="out_of_win_after",
                        pollutant_key="PM2.5",
                        observed_at_utc="2026-09-15T14:00:00Z",
                        window_hours=24,
                        raw_value_text="260",
                        raw_unit="NA",
                        value_kind="sub_index",
                        normalized_concentration=None,
                        normalized_unit=None,
                        calculated_sub_index=260,
                        is_valid=True,
                        validation_note="Legacy unitless after window",
                        raw_payload_json="{}",
                        ingested_at_utc="2026-09-15T14:00:00Z",
                    ),
                ],
            )
            conn.execute(
                """
                INSERT INTO station_aqi_evaluations (
                    station_id, district_id, evaluation_timestamp_utc, evaluation_date,
                    source_provider, station_aqi, dominant_pollutant,
                    meets_16h_completeness, meets_3_pollutant_pm_rule, is_valid_station_aqi,
                    valid_pollutant_count, observed_pollutant_hours, expected_pollutant_hours,
                    pollutants_json, rejection_reasons_json
                )
                VALUES (?, 'kolkata', '2026-09-15T10:00:00Z', '2026-09-15', 'cpcb_ogd', 100, 'PM2.5', 1, 1, 1, 3, 54, 112,
                        '{"PM2.5":{"pollutant_key":"PM2.5","window_hours":24,"required_valid_hours":16,"observed_valid_hours":18,"expected_hours":24,"meets_completeness":true,"mean_concentration":60.0,"unit":"µg/m³","sub_index":100,"value_kind_used":"concentration"}}',
                        '[]')
                """,
                (kol_st.station_id,),
            )
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="kolkata",
                    date="2026-09-15",
                    district_aqi=100,
                    dominant_pollutant="PM2.5",
                    updated_at_utc="2026-09-15T10:00:00Z",
                    source_attribution="CPCB / SPCB CAAQMS via data.gov.in (GODL-India)",
                    reporting_stations=1,
                    registered_stations=7,
                    observed_station_pollutant_hours=54,
                    expected_station_pollutant_hours=784,
                    coverage=round(54 / 784, 4),
                    quality="low",
                    pollutants_json='[{"key":"PM2.5","value":60.0,"unit":"µg/m³","subIndex":100}]',
                    caveats_json="[]",
                ),
            )

            # Subcase B (2026-09-20, evaluation anchored at 2026-09-20T12:00:00Z -> window (2026-09-19T12:00:00Z, 2026-09-20T12:00:00Z]):
            # Unbacked evaluation (0 observations inside window), with valid explicit-unit CPCB observations OUTSIDE window
            # at 2026-09-19T08:00:00Z and 2026-09-20T15:00:00Z -> out-of-window valid rows must NOT suppress invalidation!
            insert_raw_observations(
                conn,
                [
                    RawStationObservation(
                        station_id=kol_st.station_id,
                        source_provider="cpcb_ogd",
                        upstream_sensor_or_record_id="out_valid_before_sep20",
                        pollutant_key="PM2.5",
                        observed_at_utc="2026-09-19T08:00:00Z",
                        window_hours=1,
                        raw_value_text="50.0",
                        raw_unit="µg/m³",
                        value_kind="concentration",
                        normalized_concentration=50.0,
                        normalized_unit="µg/m³",
                        calculated_sub_index=83,
                        is_valid=True,
                        validation_note="Verified µg/m³ outside window",
                        raw_payload_json="{}",
                        ingested_at_utc="2026-09-19T08:00:00Z",
                    ),
                    RawStationObservation(
                        station_id=kol_st.station_id,
                        source_provider="cpcb_ogd",
                        upstream_sensor_or_record_id="out_valid_after_sep20",
                        pollutant_key="PM2.5",
                        observed_at_utc="2026-09-20T15:00:00Z",
                        window_hours=1,
                        raw_value_text="50.0",
                        raw_unit="µg/m³",
                        value_kind="concentration",
                        normalized_concentration=50.0,
                        normalized_unit="µg/m³",
                        calculated_sub_index=83,
                        is_valid=True,
                        validation_note="Verified µg/m³ outside window",
                        raw_payload_json="{}",
                        ingested_at_utc="2026-09-20T15:00:00Z",
                    ),
                ],
            )
            conn.execute(
                """
                INSERT INTO station_aqi_evaluations (
                    station_id, district_id, evaluation_timestamp_utc, evaluation_date,
                    source_provider, station_aqi, dominant_pollutant,
                    meets_16h_completeness, meets_3_pollutant_pm_rule, is_valid_station_aqi,
                    valid_pollutant_count, observed_pollutant_hours, expected_pollutant_hours,
                    pollutants_json, rejection_reasons_json
                )
                VALUES (?, 'kolkata', '2026-09-20T12:00:00Z', '2026-09-20', 'cpcb_ogd', 215, 'PM2.5', 1, 1, 1, 3, 54, 112, '{}', '[]')
                """,
                (kol_st.station_id,),
            )
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="kolkata",
                    date="2026-09-20",
                    district_aqi=215,
                    dominant_pollutant="PM2.5",
                    updated_at_utc="2026-09-20T12:00:00Z",
                    source_attribution="CPCB / SPCB CAAQMS via data.gov.in (GODL-India)",
                    reporting_stations=1,
                    registered_stations=7,
                    observed_station_pollutant_hours=54,
                    expected_station_pollutant_hours=784,
                    coverage=round(54 / 784, 4),
                    quality="low",
                    pollutants_json='[{"key":"PM2.5","value":null,"unit":"µg/m³","subIndex":215}]',
                    caveats_json="[]",
                ),
            )
            conn.commit()

            # Run Migration 003
            self.assertEqual(run_migrations(conn), [3])

            # Subcase A check: 2026-09-15 evaluation MUST be preserved at AQI=100 (unrelated out-of-window unitless rows ignored)
            sep15_eval = conn.execute(
                "SELECT station_aqi, is_valid_station_aqi FROM station_aqi_evaluations WHERE station_id = ? AND evaluation_date = '2026-09-15'",
                (kol_st.station_id,),
            ).fetchone()
            self.assertEqual(sep15_eval["station_aqi"], 100)
            self.assertEqual(sep15_eval["is_valid_station_aqi"], 1)

            sep15_daily = conn.execute(
                "SELECT district_aqi, reporting_stations FROM daily_district_history WHERE district_id = 'kolkata' AND date = '2026-09-15'"
            ).fetchone()
            self.assertEqual(sep15_daily["district_aqi"], 100)
            self.assertEqual(sep15_daily["reporting_stations"], 1)

            # Subcase B check: 2026-09-20 evaluation MUST be invalidated (out-of-window valid rows did not suppress reconciliation)
            sep20_eval = conn.execute(
                "SELECT station_aqi, is_valid_station_aqi FROM station_aqi_evaluations WHERE station_id = ? AND evaluation_date = '2026-09-20'",
                (kol_st.station_id,),
            ).fetchone()
            self.assertIsNone(sep20_eval["station_aqi"])
            self.assertEqual(sep20_eval["is_valid_station_aqi"], 0)

            sep20_daily = conn.execute(
                "SELECT district_aqi, quality, reporting_stations FROM daily_district_history WHERE district_id = 'kolkata' AND date = '2026-09-20'"
            ).fetchone()
            self.assertIsNone(sep20_daily["district_aqi"])
            self.assertEqual(sep20_daily["quality"], "unavailable")
            self.assertEqual(sep20_daily["reporting_stations"], 0)

    def test_ist_utc_boundary_evaluation_window_reconciliation(self) -> None:
        """Verify evaluations between 18:30Z and 23:59Z (next day in IST) reconcile using exact IST date & 24h UTC window."""
        from app.migrations import (
            MIGRATIONS,
            run_migrations,
            upsert_daily_district_history,
            upsert_districts,
            upsert_station_evaluation,
            upsert_stations,
        )
        from app.models import DailyDistrictHistoryRecord
        from app.districts_registry import list_target_districts, REVIEWED_STATIONS

        with get_db_connection() as conn:
            for version, name, sql in MIGRATIONS[:2]:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at_utc) VALUES (?, ?, ?)",
                    (version, name, "2026-10-01T00:00:00Z"),
                )
            upsert_districts(conn, list_target_districts())
            upsert_stations(conn, REVIEWED_STATIONS)

            ch_st = list_verified_stations_for_district("chennai")[0]
            # Timestamp 2026-10-10T20:00:00Z is 2026-10-11 01:30:00 IST -> evaluation_date = '2026-10-11'
            # Its 24h window is (2026-10-09T20:00:00Z, 2026-10-10T20:00:00Z].
            boundary_end_utc = datetime(2026, 10, 10, 20, 0, 0, tzinfo=timezone.utc)
            self.assertEqual(format_ist_date(boundary_end_utc), "2026-10-11")

            obs = []
            for h in range(18):
                ts = (boundary_end_utc - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for p_key in ("PM2.5", "PM10", "NO2"):
                    obs.append(
                        RawStationObservation(
                            station_id=ch_st.station_id,
                            source_provider="cpcb_ogd",
                            upstream_sensor_or_record_id=f"ist_bound:{p_key}:{ts}",
                            pollutant_key=p_key,  # type: ignore[arg-type]
                            observed_at_utc=ts,
                            window_hours=24,
                            raw_value_text="190",
                            raw_unit="NA",
                            value_kind="sub_index",
                            normalized_concentration=None,
                            normalized_unit=None,
                            calculated_sub_index=190,
                            is_valid=True,
                            validation_note="Legacy unitless CPCB across IST midnight",
                            raw_payload_json="{}",
                            ingested_at_utc=ts,
                        )
                    )
            insert_raw_observations(conn, obs)
            ev_bound = evaluate_station_window(
                station=ch_st, observations=obs, evaluation_end_utc=boundary_end_utc
            )
            self.assertEqual(ev_bound.evaluation_date, "2026-10-11")
            upsert_station_evaluation(conn, ev_bound)
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="chennai",
                    date="2026-10-11",
                    district_aqi=190,
                    dominant_pollutant="PM2.5",
                    updated_at_utc="2026-10-10T20:00:00Z",
                    source_attribution="CPCB / SPCB CAAQMS via data.gov.in (GODL-India)",
                    reporting_stations=1,
                    registered_stations=8,
                    observed_station_pollutant_hours=54,
                    expected_station_pollutant_hours=896,
                    coverage=round(54 / 896, 4),
                    quality="low",
                    pollutants_json='[{"key":"PM2.5","value":null,"unit":"µg/m³","subIndex":190}]',
                    caveats_json="[]",
                ),
            )
            conn.commit()

            # Run Migration 003
            self.assertEqual(run_migrations(conn), [3])

            # Verify IST date 2026-10-11 (anchored at 2026-10-10T20:00:00Z) is invalidated in both tables
            ev_after = conn.execute(
                "SELECT station_aqi, is_valid_station_aqi FROM station_aqi_evaluations WHERE station_id = ? AND evaluation_date = '2026-10-11'",
                (ch_st.station_id,),
            ).fetchone()
            self.assertIsNone(ev_after["station_aqi"])
            self.assertEqual(ev_after["is_valid_station_aqi"], 0)

            daily_after = conn.execute(
                "SELECT district_aqi, quality, reporting_stations FROM daily_district_history WHERE district_id = 'chennai' AND date = '2026-10-11'"
            ).fetchone()
            self.assertIsNone(daily_after["district_aqi"])
            self.assertEqual(daily_after["quality"], "unavailable")
            self.assertEqual(daily_after["reporting_stations"], 0)



    def test_daily_history_reconciles_when_source_attribution_dominant_quality_or_pollutants_are_stale(self) -> None:
        """Verify daily_district_history reconciles even when AQI & reporting_stations match if source, dominant, quality, or pollutants are stale."""
        from app.migrations import (
            MIGRATIONS,
            run_migrations,
            upsert_daily_district_history,
            upsert_districts,
            upsert_station_evaluation,
            upsert_stations,
        )
        from app.models import DailyDistrictHistoryRecord
        from app.districts_registry import list_target_districts, REVIEWED_STATIONS

        with get_db_connection() as conn:
            for version, name, sql in MIGRATIONS[:2]:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at_utc) VALUES (?, ?, ?)",
                    (version, name, "2026-10-01T00:00:00Z"),
                )
            upsert_districts(conn, list_target_districts())
            upsert_stations(conn, REVIEWED_STATIONS)

            eval_dt = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)

            # Case A (Bengaluru Urban on 2026-10-05):
            # Station evaluation is openaq_v3 with station_aqi=100, reporting_stations=1.
            # daily_district_history has matching district_aqi=100 and reporting_stations=1,
            # BUT has stale hybrid source_attribution ("CPCB / SPCB CAAQMS via OpenAQ v3 & data.gov.in (GODL-India)").
            blr_st = list_verified_stations_for_district("bengaluru-urban")[0]
            blr_obs = []
            for h in range(18):
                ts = (eval_dt - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for p_key, conc, sub_i in [("PM2.5", 60.0, 100), ("PM10", 100.0, 100), ("NO2", 40.0, 50)]:
                    blr_obs.append(
                        RawStationObservation(
                            station_id=blr_st.station_id,
                            source_provider="openaq_v3",
                            upstream_sensor_or_record_id=f"blr:{p_key}:{ts}",
                            pollutant_key=p_key,  # type: ignore[arg-type]
                            observed_at_utc=ts,
                            window_hours=1,
                            raw_value_text=str(conc),
                            raw_unit="µg/m³",
                            value_kind="concentration",
                            normalized_concentration=conc,
                            normalized_unit="µg/m³",
                            calculated_sub_index=sub_i,
                            is_valid=True,
                            validation_note="Verified OpenAQ",
                            raw_payload_json="{}",
                            ingested_at_utc=ts,
                        )
                    )
            insert_raw_observations(conn, blr_obs)
            ev_blr = evaluate_station_window(
                station=blr_st, observations=blr_obs, evaluation_end_utc=eval_dt
            )
            self.assertEqual(ev_blr.source_provider, "openaq_v3")
            self.assertEqual(ev_blr.station_aqi, 100)
            upsert_station_evaluation(conn, ev_blr)
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="bengaluru-urban",
                    date="2026-10-05",
                    district_aqi=100,
                    dominant_pollutant="PM2.5",
                    updated_at_utc="2026-10-05T12:00:00Z",
                    source_attribution="CPCB / SPCB CAAQMS via OpenAQ v3 & data.gov.in (GODL-India)",
                    reporting_stations=1,
                    registered_stations=6,
                    observed_station_pollutant_hours=54,
                    expected_station_pollutant_hours=672,
                    coverage=round(54 / 672, 4),
                    quality="low",
                    pollutants_json='[{"key":"PM2.5","value":60.0,"unit":"µg/m³","subIndex":100}]',
                    caveats_json="[]",
                ),
            )

            # Case B (Mumbai Suburban on 2026-10-05):
            # Station evaluation is openaq_v3 with station_aqi=120, dominant_pollutant='PM10', quality='low' (1 station).
            # daily_district_history has matching district_aqi=120, reporting_stations=1, and source_attribution='CPCB / SPCB CAAQMS via OpenAQ v3',
            # BUT has stale dominant_pollutant='SO2', stale quality='high', and stale pollutants_json (PM10 subIndex=50 instead of 120).
            mum_st = list_verified_stations_for_district("mumbai-suburban")[0]
            mum_obs = []
            for h in range(18):
                ts = (eval_dt - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for p_key, conc, sub_i in [("PM2.5", 45.0, 75), ("PM10", 130.0, 120), ("NO2", 40.0, 50)]:
                    mum_obs.append(
                        RawStationObservation(
                            station_id=mum_st.station_id,
                            source_provider="openaq_v3",
                            upstream_sensor_or_record_id=f"mum:{p_key}:{ts}",
                            pollutant_key=p_key,  # type: ignore[arg-type]
                            observed_at_utc=ts,
                            window_hours=1,
                            raw_value_text=str(conc),
                            raw_unit="µg/m³",
                            value_kind="concentration",
                            normalized_concentration=conc,
                            normalized_unit="µg/m³",
                            calculated_sub_index=sub_i,
                            is_valid=True,
                            validation_note="Verified OpenAQ",
                            raw_payload_json="{}",
                            ingested_at_utc=ts,
                        )
                    )
            insert_raw_observations(conn, mum_obs)
            ev_mum = evaluate_station_window(
                station=mum_st, observations=mum_obs, evaluation_end_utc=eval_dt
            )
            self.assertEqual(ev_mum.station_aqi, 120)
            self.assertEqual(ev_mum.dominant_pollutant, "PM10")
            upsert_station_evaluation(conn, ev_mum)
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="mumbai-suburban",
                    date="2026-10-05",
                    district_aqi=120,
                    dominant_pollutant="SO2",
                    updated_at_utc="2026-10-05T12:00:00Z",
                    source_attribution="CPCB / SPCB CAAQMS via OpenAQ v3",
                    reporting_stations=1,
                    registered_stations=7,
                    observed_station_pollutant_hours=54,
                    expected_station_pollutant_hours=784,
                    coverage=round(54 / 784, 4),
                    quality="high",
                    pollutants_json='[{"key":"PM10","value":50.0,"unit":"µg/m³","subIndex":50}]',
                    caveats_json="[]",
                ),
            )
            conn.commit()

            # Run Migration 003
            self.assertEqual(run_migrations(conn), [3])

            # Verify Case A (Bengaluru Urban): stale source_attribution was updated to OpenAQ-only
            blr_row = conn.execute(
                "SELECT district_aqi, reporting_stations, source_attribution FROM daily_district_history WHERE district_id = 'bengaluru-urban' AND date = '2026-10-05'"
            ).fetchone()
            self.assertEqual(blr_row["district_aqi"], 100)
            self.assertEqual(blr_row["reporting_stations"], 1)
            self.assertEqual(blr_row["source_attribution"], "CPCB / SPCB CAAQMS via OpenAQ v3")

            # Verify Case B (Mumbai Suburban): stale dominant_pollutant, quality, and pollutants_json were reconciled
            mum_row = conn.execute(
                "SELECT district_aqi, dominant_pollutant, quality, pollutants_json FROM daily_district_history WHERE district_id = 'mumbai-suburban' AND date = '2026-10-05'"
            ).fetchone()
            self.assertEqual(mum_row["district_aqi"], 120)
            self.assertEqual(mum_row["dominant_pollutant"], "PM10")
            self.assertEqual(mum_row["quality"], "low")
            mum_pollutants = {p["key"]: p for p in json.loads(mum_row["pollutants_json"])}
            self.assertEqual(mum_pollutants["PM10"]["subIndex"], 120)
            self.assertEqual(mum_pollutants["PM10"]["value"], 130.0)

    def test_daily_history_coverage_floating_point_tolerance_and_genuine_coverage_mismatch(self) -> None:
        """Verify minor float precision differences in coverage do not trigger false mismatches, while real coverage drift reconciles."""
        from app.migrations import (
            MIGRATIONS,
            _is_daily_history_out_of_sync,
            run_migrations,
            upsert_daily_district_history,
            upsert_districts,
            upsert_station_evaluation,
            upsert_stations,
        )
        from app.models import DailyDistrictHistoryRecord
        from app.districts_registry import get_district_mapping, list_target_districts, REVIEWED_STATIONS

        with get_db_connection() as conn:
            for version, name, sql in MIGRATIONS[:2]:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at_utc) VALUES (?, ?, ?)",
                    (version, name, "2026-10-01T00:00:00Z"),
                )
            upsert_districts(conn, list_target_districts())
            upsert_stations(conn, REVIEWED_STATIONS)

            eval_dt = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
            kol_mapping = get_district_mapping("kolkata")
            assert kol_mapping is not None
            kol_stations = list_verified_stations_for_district("kolkata")
            kol_st = kol_stations[0]

            obs = []
            for h in range(18):
                ts = (eval_dt - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for p_key, conc, sub_i in [("PM2.5", 60.0, 100), ("PM10", 100.0, 100), ("NO2", 40.0, 50)]:
                    obs.append(
                        RawStationObservation(
                            station_id=kol_st.station_id,
                            source_provider="openaq_v3",
                            upstream_sensor_or_record_id=f"kol_cov:{p_key}:{ts}",
                            pollutant_key=p_key,  # type: ignore[arg-type]
                            observed_at_utc=ts,
                            window_hours=1,
                            raw_value_text=str(conc),
                            raw_unit="µg/m³",
                            value_kind="concentration",
                            normalized_concentration=conc,
                            normalized_unit="µg/m³",
                            calculated_sub_index=sub_i,
                            is_valid=True,
                            validation_note="Verified OpenAQ",
                            raw_payload_json="{}",
                            ingested_at_utc=ts,
                        )
                    )
            insert_raw_observations(conn, obs)
            ev_kol = evaluate_station_window(station=kol_st, observations=obs, evaluation_end_utc=eval_dt)
            upsert_station_evaluation(conn, ev_kol)

            expected_rec = aggregate_district_for_date(
                district=kol_mapping.district,
                registered_stations=kol_stations,
                station_evaluations=[ev_kol],
                evaluation_date="2026-10-07",
                updated_at_utc="2026-10-07T12:00:00Z",
                source_attribution="auto",
                now_utc=eval_dt,
            )
            # 54 / 672 = 0.08035714285714286 (unrounded) vs 0.0804 (rounded to 4 decimals)
            unrounded_cov = 54.0 / 672.0
            self.assertAlmostEqual(unrounded_cov, expected_rec.coverage, places=4)
            self.assertNotEqual(unrounded_cov, expected_rec.coverage)

            # Subcase 1: Insert row with unrounded float coverage (54 / 784) -> must NOT be considered out of sync
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="kolkata",
                    date="2026-10-07",
                    district_aqi=expected_rec.district_aqi,
                    dominant_pollutant=expected_rec.dominant_pollutant,
                    updated_at_utc=expected_rec.updated_at_utc,
                    source_attribution=expected_rec.source_attribution,
                    reporting_stations=expected_rec.reporting_stations,
                    registered_stations=expected_rec.registered_stations,
                    observed_station_pollutant_hours=expected_rec.observed_station_pollutant_hours,
                    expected_station_pollutant_hours=expected_rec.expected_station_pollutant_hours,
                    coverage=unrounded_cov,
                    quality=expected_rec.quality,
                    pollutants_json=expected_rec.pollutants_json,
                    caveats_json=expected_rec.caveats_json,
                ),
            )
            stored_unrounded_row = conn.execute(
                "SELECT * FROM daily_district_history WHERE district_id = 'kolkata' AND date = '2026-10-07'"
            ).fetchone()
            self.assertFalse(_is_daily_history_out_of_sync(stored_unrounded_row, expected_rec))

            # Subcase 2: Insert row with genuinely stale coverage (0.75 instead of 0.0689) while AQI & stations match
            upsert_daily_district_history(
                conn,
                DailyDistrictHistoryRecord(
                    district_id="kolkata",
                    date="2026-10-07",
                    district_aqi=expected_rec.district_aqi,
                    dominant_pollutant=expected_rec.dominant_pollutant,
                    updated_at_utc=expected_rec.updated_at_utc,
                    source_attribution=expected_rec.source_attribution,
                    reporting_stations=expected_rec.reporting_stations,
                    registered_stations=expected_rec.registered_stations,
                    observed_station_pollutant_hours=588,
                    expected_station_pollutant_hours=784,
                    coverage=0.75,
                    quality=expected_rec.quality,
                    pollutants_json=expected_rec.pollutants_json,
                    caveats_json=expected_rec.caveats_json,
                ),
            )
            conn.commit()

            # Run Migration 003 -> must detect the genuine coverage mismatch and reconcile coverage back to 0.0689
            self.assertEqual(run_migrations(conn), [3])
            reconciled_row = conn.execute(
                "SELECT coverage, observed_station_pollutant_hours FROM daily_district_history WHERE district_id = 'kolkata' AND date = '2026-10-07'"
            ).fetchone()
            self.assertEqual(reconciled_row["coverage"], expected_rec.coverage)
            self.assertEqual(reconciled_row["observed_station_pollutant_hours"], 54)


if __name__ == "__main__":
    unittest.main()
