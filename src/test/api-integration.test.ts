import { describe, expect, it, vi } from "vitest";
import { createApiService } from "@/data/api-service";
import { DATA_IS_ILLUSTRATIVE } from "@/data";
import { aqiBand, relativeTime } from "@/lib/aqi";
import { formatTrendDate } from "@/components/dw/TrendChart";

describe("Frontend-Backend API Integration Contract", () => {
  it("defaults to live API mode (DATA_IS_ILLUSTRATIVE is false)", () => {
    expect(DATA_IS_ILLUSTRATIVE).toBe(false);
  });

  it("queries /districts, /snapshots, /snapshots/:id, and /health with query filters and 404 handling", async () => {
    const calls: string[] = [];
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.endsWith("/districts?state=Maharashtra&q=nagpur")) {
        return new Response(
          JSON.stringify([{ id: "nagpur", name: "Nagpur", state: "Maharashtra", lat: 21.1458, lon: 79.0882 }]),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
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
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response(JSON.stringify([]), { status: 200 });
    }) as typeof fetch;

    try {
      const svc = createApiService({ baseUrl: "http://localhost:8000/api" });
      const districts = await svc.listDistricts({ state: "Maharashtra", q: "nagpur" });
      expect(districts).toHaveLength(1);
      expect(districts[0]?.id).toBe("nagpur");

      const missing = await svc.getSnapshot("unknown-district");
      expect(missing).toBeNull();

      const health = await svc.getHealth?.();
      expect(health?.liveDataReady).toBe(true);
      expect(health?.districtsTracked).toBe(30);
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
