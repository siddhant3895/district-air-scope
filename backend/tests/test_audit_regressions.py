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
                                        {"id": 501, "parameter": {"name": "pm25", "units": "µg/m³"}},
                                        {"id": 502, "parameter": {"name": "pm10", "units": "µg/m³"}},
                                        {"id": 503, "parameter": {"name": "no2", "units": "µg/m³"}},
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


if __name__ == "__main__":
    unittest.main()
