"""Unit and integration tests for the XKDR Forum India Air Quality Database provider adapter."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Optional
import unittest
from fastapi.testclient import TestClient

from app.cli import run_sync
from app.database import get_db_connection
from app.ingestion.xkdr_adapter import (
    build_xkdr_station_catalog,
    fetch_xkdr_meta,
    parse_xkdr_csv_measurements,
    parse_xkdr_ist_timestamp_to_utc,
    parse_xkdr_measurements_payload,
    resolve_xkdr_station,
    resolve_xkdr_sync_window,
)
from app.main import app
from app.services.aggregator import IST_TZ

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class TestXkdrProviderIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_xkdr.db")
        self.prev_env = {
            k: os.environ.get(k)
            for k in (
                "DATABASE_URL",
                "XKDR_API_KEY",
                "XKDR_API_BASE_URL",
                "OPENAQ_API_KEY",
                "DATA_GOV_IN_API_KEY",
            )
        }
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        for k in ("XKDR_API_KEY", "OPENAQ_API_KEY", "DATA_GOV_IN_API_KEY"):
            os.environ.pop(k, None)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for k, prev in self.prev_env.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        self.temp_dir.cleanup()

    def test_ist_to_utc_timestamp_conversion_and_station_resolution(self) -> None:
        """Verify IST naive timestamps convert to UTC and neighbor-district stations remain excluded."""
        # 2024-11-02 14:00:00 IST (UTC+05:30) == 2024-11-02T08:30:00Z
        self.assertEqual(
            parse_xkdr_ist_timestamp_to_utc("2024-11-02 14:00:00"),
            "2024-11-02T08:30:00Z",
        )
        # Early morning IST (02:00:00 IST) rolls back to previous UTC calendar day (20:30:00Z)
        self.assertEqual(
            parse_xkdr_ist_timestamp_to_utc("2024-11-02 02:00:00"),
            "2024-11-01T20:30:00Z",
        )

        fixture = json.loads((FIXTURES_DIR / "xkdr_sample.json").read_text(encoding="utf-8"))
        verified_map, excluded_keys, latest_date = build_xkdr_station_catalog(fixture["stations"])

        # Mandir Marg (New Delhi) and Lalbagh (Lucknow) are verified members
        self.assertIn("site_103", verified_map)
        self.assertEqual(verified_map["site_103"].station_id, "cpcb:new-delhi:mandir-marg")
        self.assertIn("site_509", verified_map)
        self.assertEqual(verified_map["site_509"].station_id, "cpcb:lucknow:lalbagh")

        # Anand Vihar is in Shahdara/East Delhi (not New Delhi revenue district) -> must be excluded!
        self.assertNotIn("site_301", verified_map)
        self.assertIn("site_301", excluded_keys)
        self.assertEqual(latest_date, "2026-11-02")

        # Verify US Diplomatic Post mapping
        us_del = resolve_xkdr_station({"station_name": "US Diplomatic Post: New Delhi"})
        self.assertIsNotNone(us_del)
        assert us_del is not None
        self.assertEqual(us_del.district_id, "new-delhi")

    def test_measurement_parsing_json_and_csv_and_unit_normalization(self) -> None:
        """Verify JSON and CSV XKDR measurements normalize pollutants and reject invalid/meteorological rows."""
        fixture = json.loads((FIXTURES_DIR / "xkdr_sample.json").read_text(encoding="utf-8"))
        verified_map, excluded_keys, _ = build_xkdr_station_catalog(fixture["stations"])

        raw_rows = [
            {
                "station_id": "site_103",
                "parameter_name": "PM2.5",
                "collected_at": "2026-11-02 15:30:00",
                "value": 90.0,
                "unit": "µg/m³",
            },
            {
                "station_id": "site_103",
                "parameter_name": "Ozone",
                "collected_at": "2026-11-02 15:30:00",
                "value": 50.0,
                "unit": "µg/m³",
            },
            {
                "station_id": "site_103",
                "parameter_name": "CO",
                "collected_at": "2026-11-02 15:30:00",
                "value": 1.0,
                "unit": "mg/m³",
            },
            # Excluded neighbor-district station (Anand Vihar)
            {
                "station_id": "site_301",
                "parameter_name": "PM2.5",
                "collected_at": "2026-11-02 15:30:00",
                "value": 310.0,
                "unit": "µg/m³",
            },
            # Non-AQI meteorological parameter (AT = Ambient Temperature)
            {
                "station_id": "site_103",
                "parameter_name": "AT",
                "collected_at": "2026-11-02 15:30:00",
                "value": 28.5,
                "unit": "°C",
            },
            # Negative concentration -> stored with is_valid=False
            {
                "station_id": "site_103",
                "parameter_name": "NO2",
                "collected_at": "2026-11-02 15:30:00",
                "value": -12.0,
                "unit": "µg/m³",
            },
        ]

        report = parse_xkdr_measurements_payload(
            raw_rows,
            verified_station_map=verified_map,
            excluded_station_keys=excluded_keys,
            ingested_at_utc="2026-11-02T11:00:00Z",
        )
        self.assertEqual(report.skipped_non_member_station_count, 1)
        self.assertEqual(report.skipped_unknown_pollutant_count, 1)
        self.assertEqual(report.invalid_value_count, 1)
        self.assertEqual(len(report.accepted_observations), 4)

        valid_obs = [o for o in report.accepted_observations if o.is_valid]
        self.assertEqual(len(valid_obs), 3)
        by_key = {o.pollutant_key: o for o in valid_obs}
        self.assertEqual(by_key["PM2.5"].calculated_sub_index, 200)
        self.assertEqual(by_key["O3"].calculated_sub_index, 50)
        self.assertEqual(by_key["CO"].calculated_sub_index, 50)
        self.assertEqual(by_key["PM2.5"].observed_at_utc, "2026-11-02T10:00:00Z")

        # Verify CSV parsing support (`agg=hourly` format)
        csv_text = (
            "station_id,parameter_name,period_start,n_obs,mean,min,max\n"
            "site_509,PM2.5,2026-11-02 15:30:00,4,60.0,55.0,65.0\n"
        )
        csv_rows = parse_xkdr_csv_measurements(csv_text)
        csv_report = parse_xkdr_measurements_payload(
            csv_rows,
            verified_station_map=verified_map,
            excluded_station_keys=excluded_keys,
        )
        self.assertEqual(len(csv_report.accepted_observations), 1)
        self.assertTrue(csv_report.accepted_observations[0].is_valid)
        self.assertEqual(csv_report.accepted_observations[0].calculated_sub_index, 100)

    def test_demo_key_and_archive_window_resolution(self) -> None:
        """Verify demo key caps at 2024-12-31 while personal key anchors to latest archive date."""
        ref_now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
        start_demo, end_demo = resolve_xkdr_sync_window(
            api_key="aqi_demo_wbf92Qx21zX-Wa_Tg8Dx1nXe",
            reference_now_utc=ref_now,
            backfill_days=30,
            catalog_latest_ist_date="2026-03-31",
        )
        self.assertEqual(end_demo, "2024-12-31")
        self.assertEqual(start_demo, "2024-12-02")

        start_personal, end_personal = resolve_xkdr_sync_window(
            api_key="aqi_personal_real_key_example",
            reference_now_utc=ref_now,
            backfill_days=30,
            catalog_latest_ist_date="2026-03-31",
        )
        self.assertEqual(end_personal, "2026-03-31")
        self.assertEqual(start_personal, "2026-03-02")

    def test_end_to_end_xkdr_sync_populates_sqlite_and_district_snapshots(self) -> None:
        """Verify full pipeline: XKDR sync -> SQLite -> AQI aggregation -> /api/health & /api/snapshots."""
        os.environ["XKDR_API_KEY"] = "xkdr-test-personal-key"
        os.environ["DATA_GOV_IN_API_KEY"] = "broken-cpcb-key"

        # Verify 0 observations before sync
        snap_before = self.client.get("/api/snapshots/new-delhi").json()
        self.assertIsNone(snap_before["aqi"])
        self.assertEqual(snap_before["quality"], "unavailable")

        ref_now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        latest_obs_utc = ref_now - timedelta(hours=1)
        latest_ist_str = latest_obs_utc.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S")
        fixture = json.loads((FIXTURES_DIR / "xkdr_sample.json").read_text(encoding="utf-8"))

        # Update fixture stations last_seen to match current test window
        stations_list = []
        for s in fixture["stations"]:
            s_copy = dict(s)
            s_copy["last_seen"] = latest_ist_str
            stations_list.append(s_copy)

        auth_headers_seen: List[str] = []

        class FakeXkdrHttpClient:
            def get_json(
                self,
                url: str,
                *,
                params: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None,
            ) -> Any:
                # Simulate CPCB data.gov.in failure to verify provider isolation
                if "data.gov.in" in url:
                    raise RuntimeError("data.gov.in portal timeout (504)")

                if headers and "Authorization" in headers:
                    auth_headers_seen.append(headers["Authorization"])

                if url.endswith("/v1/stations"):
                    return stations_list
                if url.endswith("/v1/parameters"):
                    return fixture["parameters"]
                if url.endswith("/v1/measurements"):
                    meas: List[Dict[str, Any]] = []
                    # Generate 20 hourly observations for Mandir Marg (site_103) and Lalbagh (site_509)
                    for h in range(20):
                        ist_dt = (latest_obs_utc - timedelta(hours=h)).astimezone(IST_TZ)
                        ts_str = ist_dt.strftime("%Y-%m-%d %H:%M:%S")
                        for st_id, pm25_v, pm10_v, no2_v, so2_v, o3_v, co_v in [
                            ("site_103", 90.0, 150.0, 40.0, 20.0, 50.0, 1.0),
                            ("site_509", 60.0, 100.0, 40.0, 20.0, 50.0, 1.0),
                        ]:
                            for p_name, val, unit in [
                                ("PM2.5", pm25_v, "µg/m³"),
                                ("PM10", pm10_v, "µg/m³"),
                                ("NO2", no2_v, "µg/m³"),
                                ("SO2", so2_v, "µg/m³"),
                                ("Ozone", o3_v, "µg/m³"),
                                ("CO", co_v, "mg/m³"),
                            ]:
                                meas.append(
                                    {
                                        "station_id": st_id,
                                        "parameter_name": p_name,
                                        "collected_at": ts_str,
                                        "value": val,
                                        "unit": unit,
                                    }
                                )
                    return meas
                return {}

        sync_res = run_sync(days=3, overlap_hours=3, now_utc=ref_now, http_client=FakeXkdrHttpClient())
        self.assertEqual(sync_res["overall_status"], "partial_failure")
        self.assertEqual(sync_res["cpcb_status"], "failed")
        self.assertEqual(sync_res["xkdr_status"], "succeeded")
        self.assertEqual(sync_res["xkdr_observations_ingested"], 240)
        self.assertGreater(sync_res["daily_district_records_updated"], 0)
        self.assertTrue(all(h == "Bearer xkdr-test-personal-key" for h in auth_headers_seen))

        # Verify database observation count after sync
        with get_db_connection() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM raw_station_observations WHERE source_provider = 'xkdr' AND is_valid = 1"
            ).fetchone()["c"]
            self.assertEqual(cnt, 240)

        # Verify /api/health reports xkdr healthy and liveDataReady=True
        health = self.client.get("/api/health").json()
        self.assertTrue(health["xkdrKeyConfigured"])
        self.assertTrue(health["liveDataReady"])
        self.assertEqual(health["providers"]["xkdr"]["lastSyncStatus"], "succeeded")
        self.assertTrue(health["providers"]["xkdr"]["hasFreshData"])
        self.assertEqual(health["providers"]["cpcb_ogd"]["lastSyncStatus"], "failed")

        # Verify /api/snapshots returns genuine district AQI readings without needing explicit anchor_date
        all_snaps = self.client.get("/api/snapshots").json()
        by_id = {s["district"]["id"]: s for s in all_snaps}

        nd_snap = by_id["new-delhi"]
        self.assertEqual(nd_snap["aqi"], 200)
        self.assertEqual(nd_snap["dominant"], "PM2.5")
        self.assertEqual(nd_snap["stations"], 1)
        self.assertEqual(
            nd_snap["source"],
            "CPCB / SPCB CAAQMS via India Air Quality Database, XKDR Forum (CC BY 4.0)",
        )
        pm25_pollutant = next(p for p in nd_snap["pollutants"] if p["key"] == "PM2.5")
        self.assertEqual(pm25_pollutant["value"], 90.0)
        self.assertEqual(pm25_pollutant["subIndex"], 200)

        lko_snap = by_id["lucknow"]
        self.assertEqual(lko_snap["aqi"], 100)
        self.assertEqual(lko_snap["stations"], 1)

    def test_hybrid_xkdr_and_openaq_sync_deduplicates_hours_and_credits_both_sources(self) -> None:
        """Verify XKDR and OpenAQ coexist without double-counting hours and credit both sources."""
        os.environ["XKDR_API_KEY"] = "xkdr-test-key"
        os.environ["OPENAQ_API_KEY"] = "openaq-test-key"

        ref_now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        latest_obs_utc = ref_now - timedelta(hours=1)
        latest_ist_str = latest_obs_utc.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S")
        fixture = json.loads((FIXTURES_DIR / "xkdr_sample.json").read_text(encoding="utf-8"))

        stations_list = [
            dict(fixture["stations"][0], last_seen=latest_ist_str),  # Mandir Marg (site_103, openaq 8118)
        ]

        class FakeHybridClient:
            def get_json(
                self,
                url: str,
                *,
                params: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None,
            ) -> Any:
                # XKDR endpoints: provide PM2.5 and PM10 at Mandir Marg
                if url.endswith("/v1/stations"):
                    return stations_list
                if url.endswith("/v1/parameters"):
                    return fixture["parameters"]
                if url.endswith("/v1/measurements"):
                    meas: List[Dict[str, Any]] = []
                    for h in range(18):
                        ist_dt = (latest_obs_utc - timedelta(hours=h)).astimezone(IST_TZ)
                        ts_str = ist_dt.strftime("%Y-%m-%d %H:%M:%S")
                        meas.append(
                            {
                                "station_id": "site_103",
                                "parameter_name": "PM2.5",
                                "collected_at": ts_str,
                                "value": 90.0,
                                "unit": "µg/m³",
                            }
                        )
                        meas.append(
                            {
                                "station_id": "site_103",
                                "parameter_name": "PM10",
                                "collected_at": ts_str,
                                "value": 150.0,
                                "unit": "µg/m³",
                            }
                        )
                    return meas

                # OpenAQ endpoints: provide NO2 (and overlapping PM2.5) at Mandir Marg (location 8118)
                if "/v3/locations/8118" in url:
                    return {
                        "results": [
                            {
                                "id": 8118,
                                "name": "Mandir Marg, Delhi - DPCC",
                                "isMonitor": True,
                                "isMobile": False,
                                "datetimeLast": {"utc": latest_obs_utc.strftime("%Y-%m-%dT%H:%M:%SZ")},
                                "sensors": [
                                    {"id": 81181, "parameter": {"name": "pm25", "units": "µg/m³"}},
                                    {"id": 81182, "parameter": {"name": "so2", "units": "µg/m³"}},
                                    {"id": 81183, "parameter": {"name": "no2", "units": "µg/m³"}},
                                ],
                            }
                        ]
                    }
                if "/v3/sensors/81181/hours" in url or "/v3/sensors/81183/hours" in url:
                    is_pm25 = "81181" in url
                    results = []
                    for h in range(18):
                        dt_to = (latest_obs_utc - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        dt_from = (latest_obs_utc - timedelta(hours=h + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        results.append(
                            {
                                "value": 90.0 if is_pm25 else 40.0,
                                "parameter": {"name": "pm25" if is_pm25 else "no2", "units": "µg/m³"},
                                "period": {
                                    "datetimeFrom": {"utc": dt_from},
                                    "datetimeTo": {"utc": dt_to},
                                },
                                "coverage": {"observedCount": 1, "percentCoverage": 100.0},
                            }
                        )
                    return {"results": results}
                return {"results": []}

        res = run_sync(days=3, overlap_hours=3, now_utc=ref_now, http_client=FakeHybridClient())
        self.assertEqual(res["overall_status"], "succeeded")
        self.assertEqual(res["xkdr_status"], "succeeded")
        self.assertEqual(res["openaq_status"], "succeeded")

        nd_snap = self.client.get("/api/snapshots/new-delhi").json()
        self.assertEqual(nd_snap["aqi"], 200)
        self.assertEqual(
            nd_snap["source"],
            "CPCB / SPCB CAAQMS via India Air Quality Database (XKDR Forum, CC BY 4.0) & OpenAQ v3",
        )


@unittest.skipUnless(
    bool(os.getenv("XKDR_API_KEY", "").strip()),
    "Skipped live XKDR integration test: XKDR_API_KEY is not configured in environment.",
)
class TestOptionalLiveXkdrIntegration(unittest.TestCase):
    def test_live_xkdr_meta_fetch(self) -> None:
        key = os.getenv("XKDR_API_KEY", "").strip()
        meta = fetch_xkdr_meta(key)
        self.assertIsInstance(meta, dict)


if __name__ == "__main__":
    unittest.main()
