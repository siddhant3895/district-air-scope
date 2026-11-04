// ILLUSTRATIVE MOCK DATA — not live measurements. Deterministic so the UI is stable.
import type { AirQualityService, District, DistrictSnapshot, PollutantKey, QualityLevel } from "./types";

const DISTRICTS: (District & { base: number; quality: QualityLevel; stations: number; coverage: number })[] = [
  { id: "new-delhi", name: "New Delhi", state: "Delhi", lat: 28.61, lon: 77.21, base: 290, quality: "high", stations: 6, coverage: 0.94 },
  { id: "lucknow", name: "Lucknow", state: "Uttar Pradesh", lat: 26.85, lon: 80.95, base: 210, quality: "medium", stations: 3, coverage: 0.81 },
  { id: "mumbai-suburban", name: "Mumbai Suburban", state: "Maharashtra", lat: 19.12, lon: 72.85, base: 120, quality: "high", stations: 5, coverage: 0.9 },
  { id: "bengaluru-urban", name: "Bengaluru Urban", state: "Karnataka", lat: 12.97, lon: 77.59, base: 78, quality: "medium", stations: 4, coverage: 0.76 },
  { id: "kolkata", name: "Kolkata", state: "West Bengal", lat: 22.57, lon: 88.36, base: 165, quality: "medium", stations: 3, coverage: 0.72 },
  { id: "chennai", name: "Chennai", state: "Tamil Nadu", lat: 13.08, lon: 80.27, base: 62, quality: "low", stations: 2, coverage: 0.48 },
  { id: "patna", name: "Patna", state: "Bihar", lat: 25.59, lon: 85.14, base: 0, quality: "unavailable", stations: 0, coverage: 0 },
];

function rand(seed: number) {
  const x = Math.sin(seed) * 10000;
  return x - Math.floor(x);
}

const DAYS = 30;
const ANCHOR = new Date("2026-11-01T00:00:00Z");

function build(d: (typeof DISTRICTS)[number], idx: number): DistrictSnapshot {
  const unavailable = d.quality === "unavailable";
  const history = Array.from({ length: DAYS }, (_, i) => {
    const date = new Date(ANCHOR.getTime() - (DAYS - 1 - i) * 86400000).toISOString().slice(0, 10);
    if (unavailable) return { date, aqi: null };
    const r = rand(idx * 97 + i);
    const missing = d.coverage < 0.6 ? r < 0.2 : r < 0.04;
    const trend = (i / DAYS) * d.base * 0.25;
    const aqi = Math.round(Math.max(15, Math.min(500, d.base * 0.85 + trend + (r - 0.5) * d.base * 0.4)));
    return { date, aqi: missing ? null : aqi };
  });
  const latest = [...history].reverse().find((h) => h.aqi !== null)?.aqi ?? null;
  const keys: [PollutantKey, string, number][] = [
    ["PM2.5", "µg/m³", 0.55], ["PM10", "µg/m³", 0.95], ["NO2", "µg/m³", 0.25],
    ["SO2", "µg/m³", 0.08], ["O3", "µg/m³", 0.2], ["CO", "mg/m³", 0.012],
  ];
  const pollutants = keys.map(([key, unit, k], j) => {
    if (latest === null) return { key, unit, value: null, subIndex: null };
    const sub = Math.round(latest * (j === 0 ? 1 : 0.3 + rand(idx * 13 + j) * 0.55));
    const value = key === "SO2" && d.quality === "low" ? null : +(latest * k * (0.8 + rand(j + idx) * 0.4)).toFixed(key === "CO" ? 2 : 0);
    return { key, unit, value, subIndex: value === null ? null : sub };
  });
  const caveats: string[] = [];
  if (unavailable) caveats.push("No station readings received for this district in the sample window.");
  if (d.coverage < 0.6 && !unavailable) caveats.push("Coverage below 60% — summary values may not represent the full district.");
  if (d.stations <= 2 && !unavailable) caveats.push("Few monitoring stations; readings may reflect local rather than district-wide conditions.");
  if (pollutants.some((p) => p.value === null) && !unavailable) caveats.push("One or more pollutants not reported by stations in this district.");

  return {
    district: { id: d.id, name: d.name, state: d.state, lat: d.lat, lon: d.lon },
    aqi: latest,
    dominant: latest === null ? null : "PM2.5",
    updatedAt: unavailable ? null : new Date(ANCHOR.getTime() - (idx + 1) * 3600000 * (d.quality === "low" ? 9 : 1)).toISOString(),
    source: "Sample source (placeholder)",
    stations: d.stations,
    coverage: d.coverage,
    quality: d.quality,
    caveats,
    pollutants,
    history,
    isIllustrative: true,
  };
}

const SNAPSHOTS = DISTRICTS.map(build);

function filterMockSnapshots(filters?: { state?: string | undefined; q?: string | undefined }) {
  let list = SNAPSHOTS;
  if (filters?.state && filters.state.trim()) {
    const sNorm = filters.state.trim().toLowerCase();
    list = list.filter((s) => s.district.state.toLowerCase() === sNorm);
  }
  if (filters?.q && filters.q.trim()) {
    const qNorm = filters.q.trim().toLowerCase();
    list = list.filter(
      (s) =>
        s.district.name.toLowerCase().includes(qNorm) ||
        s.district.state.toLowerCase().includes(qNorm) ||
        s.district.id.toLowerCase().includes(qNorm),
    );
  }
  return list;
}

export const mockService: AirQualityService = {
  async listDistricts(filters) {
    return filterMockSnapshots(filters).map((s) => s.district);
  },
  async getSnapshot(id) {
    return SNAPSHOTS.find((s) => s.district.id === id) ?? null;
  },
  async listSnapshots(filters) {
    return filterMockSnapshots(filters);
  },
  async getHealth() {
    return {
      status: "ok",
      phase: "illustrative_mock",
      liveDataReady: false,
      cpcbKeyConfigured: false,
      openaqKeyConfigured: false,
      districtsTracked: SNAPSHOTS.length,
      lastIngestedAt: null,
      lastSyncStatus: "never_run",
      freshDistrictsCount: 0,
      providers: {},
    };
  },
};
