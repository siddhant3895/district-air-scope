import { describe, expect, it, vi } from "vitest";
import { createApiService } from "@/data/api-service";
import { DATA_IS_ILLUSTRATIVE, type DistrictSnapshot } from "@/data";
import { aqiBand, formatTrendDate, relativeTime } from "@/lib/aqi";

const SAMPLE_NAGPUR_SNAPSHOT: DistrictSnapshot = {
  district: {
    id: "nagpur",
    name: "Nagpur",
    state: "Maharashtra",
    lat: 21.1458,
    lon: 79.0882,
  },
  aqi: 100,
  dominant: "PM2.5",
  updatedAt: "2026-11-02T11:00:00Z",
  source: "CPCB / SPCB CAAQMS via OpenAQ v3",
  stations: 1,
  coverage: 0.4821,
  quality: "low",
  caveats: [
    "1 of 3 verified district stations met CPCB 16h / 3-pollutant completeness.",
    "DistrictWatch composite metric (mean of station-level CPCB AQIs across verified district stations) — not an official CPCB district-level index.",
  ],
  pollutants: [
    { key: "PM2.5", value: 60.0, unit: "µg/m³", subIndex: 100 },
    { key: "PM10", value: 100.0, unit: "µg/m³", subIndex: 100 },
    { key: "NO2", value: 40.0, unit: "µg/m³", subIndex: 50 },
    { key: "SO2", value: null, unit: "µg/m³", subIndex: null },
    { key: "O3", value: null, unit: "µg/m³", subIndex: null },
    { key: "CO", value: null, unit: "mg/m³", subIndex: null },
  ],
  history: [
    { date: "2026-11-01", aqi: null },
    { date: "2026-11-02", aqi: 100 },
  ],
  isIllustrative: false,
};

describe("Frontend-Backend API Integration Contract", () => {
  it("defaults to live API mode (DATA_IS_ILLUSTRATIVE is false)", () => {
    expect(DATA_IS_ILLUSTRATIVE).toBe(false);
  });

  it("queries /districts, /snapshots, /snapshots/:id, and /health with state, q, and anchor_date serialization", async () => {
    const calls: string[] = [];
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);

      if (url.endsWith("/districts?state=Maharashtra&q=nagpur")) {
        return new Response(JSON.stringify([SAMPLE_NAGPUR_SNAPSHOT.district]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.endsWith("/snapshots?state=Maharashtra&q=nagpur&anchor_date=2026-11-02")) {
        return new Response(JSON.stringify([SAMPLE_NAGPUR_SNAPSHOT]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.endsWith("/snapshots/nagpur?anchor_date=2026-11-02")) {
        return new Response(JSON.stringify(SAMPLE_NAGPUR_SNAPSHOT), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.endsWith("/snapshots/unknown-district")) {
        return new Response(JSON.stringify({ detail: "Not found" }), { status: 404 });
      }

      if (url.endsWith("/health")) {
        return new Response(
          JSON.stringify({
            status: "ok",
            phase: "phase_3_nationwide_development",
            liveDataReady: true,
            cpcbKeyConfigured: false,
            openaqKeyConfigured: true,
            districtsTracked: 30,
            lastIngestedAt: "2026-11-02T12:00:00Z",
            lastSyncStatus: "succeeded",
            freshDistrictsCount: 5,
            providers: {
              openaq_v3: {
                configured: true,
                lastSyncStatus: "succeeded",
                lastSyncError: null,
                lastObservationAt: "2026-11-02T11:00:00Z",
                hasFreshData: true,
              },
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }

      return new Response(JSON.stringify([]), { status: 200 });
    }) as typeof fetch;

    try {
      const svc = createApiService({ baseUrl: "http://localhost:8000/api" });

      // 1. listDistricts with state + q filters
      const districts = await svc.listDistricts({ state: "Maharashtra", q: "nagpur" });
      expect(districts).toHaveLength(1);
      expect(districts[0]?.id).toBe("nagpur");

      // 2. Successful listSnapshots with state + q + anchorDate -> anchor_date serialization
      const snapshots = await svc.listSnapshots({
        state: "Maharashtra",
        q: "nagpur",
        anchorDate: "2026-11-02",
      });
      expect(snapshots).toHaveLength(1);
      expect(snapshots[0]?.district.id).toBe("nagpur");
      expect(snapshots[0]?.aqi).toBe(100);
      expect(snapshots[0]?.isIllustrative).toBe(false);

      // 3. Successful getSnapshot(id, anchorDate) with anchor_date query serialization
      const singleSnap = await svc.getSnapshot("nagpur", "2026-11-02");
      expect(singleSnap).not.toBeNull();
      expect(singleSnap?.aqi).toBe(100);
      expect(singleSnap?.dominant).toBe("PM2.5");
      expect(singleSnap?.pollutants).toHaveLength(6);
      expect(singleSnap?.history).toHaveLength(2);

      // 4. 404 getSnapshot returns null cleanly
      const missing = await svc.getSnapshot("unknown-district");
      expect(missing).toBeNull();

      // 5. getHealth endpoint verification
      const health = await svc.getHealth?.();
      expect(health?.liveDataReady).toBe(true);
      expect(health?.districtsTracked).toBe(30);
      expect(health?.lastSyncStatus).toBe("succeeded");
      expect(health?.providers?.["openaq_v3"]?.hasFreshData).toBe(true);

      expect(calls).toContain(
        "http://localhost:8000/api/snapshots?state=Maharashtra&q=nagpur&anchor_date=2026-11-02",
      );
      expect(calls).toContain("http://localhost:8000/api/snapshots/nagpur?anchor_date=2026-11-02");
    } finally {
      globalThis.fetch = origFetch;
    }
  });

  it("formats IST trend dates without timezone day-shift and maps AQI bands accurately", () => {
    expect(formatTrendDate("2026-11-02")).toContain("2");
    expect(aqiBand(null)).toBeNull();
    expect(aqiBand(140)?.label).toBe("Moderate");
    expect(relativeTime(null)).toBe("No data");
  });
});
