// HTTP API adapter implementing AirQualityService against the Python FastAPI backend.
import type {
  AirQualityService,
  District,
  DistrictQueryFilters,
  DistrictSnapshot,
  HealthResponse,
} from "./types";

export interface ApiServiceOptions {
  baseUrl: string;
  fallback?: AirQualityService;
}

function resolveBaseUrl(baseUrl: string): string {
  const trimmed = baseUrl.replace(/\/+$/, "");
  if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
    return trimmed;
  }
  // During SSR / Node execution where relative URLs ("/api") require an origin,
  // resolve against VITE_BACKEND_ORIGIN or http://localhost:8000.
  if (typeof window === "undefined") {
    const serverOrigin =
      (typeof process !== "undefined" && process.env?.VITE_BACKEND_ORIGIN) ||
      "http://localhost:8000";
    const cleanOrigin = serverOrigin.replace(/\/+$/, "");
    const cleanPath = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
    return `${cleanOrigin}${cleanPath}`;
  }
  return trimmed || "/api";
}

function buildQueryString(filters?: DistrictQueryFilters): string {
  if (!filters) return "";
  const params = new URLSearchParams();
  if (filters.state && filters.state.trim()) {
    params.set("state", filters.state.trim());
  }
  if (filters.q && filters.q.trim()) {
    params.set("q", filters.q.trim());
  }
  if (filters.anchorDate && filters.anchorDate.trim()) {
    params.set("anchor_date", filters.anchorDate.trim());
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export function createApiService({ baseUrl, fallback }: ApiServiceOptions): AirQualityService {
  async function request<T>(path: string, allow404 = false): Promise<T | null> {
    const resolvedBase = resolveBaseUrl(baseUrl);
    const response = await fetch(`${resolvedBase}${path}`, {
      headers: { Accept: "application/json" },
    });
    if (allow404 && response.status === 404) {
      return null;
    }
    if (!response.ok) {
      throw new Error(`DistrictWatch API error (${response.status}) on ${path}`);
    }
    return (await response.json()) as T;
  }

  return {
    async listDistricts(filters?: DistrictQueryFilters): Promise<District[]> {
      try {
        const qs = buildQueryString(filters);
        return (await request<District[]>(`/districts${qs}`)) ?? [];
      } catch (err) {
        if (fallback) return fallback.listDistricts(filters);
        throw err;
      }
    },

    async getSnapshot(id: string, anchorDate?: string): Promise<DistrictSnapshot | null> {
      try {
        const qs = buildQueryString(anchorDate ? { anchorDate } : undefined);
        return await request<DistrictSnapshot>(
          `/snapshots/${encodeURIComponent(id)}${qs}`,
          true
        );
      } catch (err) {
        if (fallback) return fallback.getSnapshot(id, anchorDate);
        throw err;
      }
    },

    async listSnapshots(filters?: DistrictQueryFilters): Promise<DistrictSnapshot[]> {
      try {
        const qs = buildQueryString(filters);
        return (await request<DistrictSnapshot[]>(`/snapshots${qs}`)) ?? [];
      } catch (err) {
        if (fallback) return fallback.listSnapshots(filters);
        throw err;
      }
    },

    async getHealth(): Promise<HealthResponse | null> {
      try {
        return await request<HealthResponse>("/health");
      } catch {
        if (fallback?.getHealth) return fallback.getHealth();
        return null;
      }
    },
  };
}
