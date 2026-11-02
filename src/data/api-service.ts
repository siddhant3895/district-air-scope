// HTTP API adapter implementing AirQualityService against the Python FastAPI backend.
import type { AirQualityService, District, DistrictSnapshot } from "./types";

export interface ApiServiceOptions {
  baseUrl: string;
  fallback?: AirQualityService;
}

export function createApiService({ baseUrl, fallback }: ApiServiceOptions): AirQualityService {
  const normalizedBase = baseUrl.replace(/\/+$/, "");

  async function request<T>(path: string, allow404 = false): Promise<T | null> {
    const response = await fetch(`${normalizedBase}${path}`, {
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
    async listDistricts(): Promise<District[]> {
      try {
        return (await request<District[]>("/districts")) ?? [];
      } catch (err) {
        if (fallback) return fallback.listDistricts();
        throw err;
      }
    },

    async getSnapshot(id: string): Promise<DistrictSnapshot | null> {
      try {
        return await request<DistrictSnapshot>(`/snapshots/${encodeURIComponent(id)}`, true);
      } catch (err) {
        if (fallback) return fallback.getSnapshot(id);
        throw err;
      }
    },

    async listSnapshots(): Promise<DistrictSnapshot[]> {
      try {
        return (await request<DistrictSnapshot[]>("/snapshots")) ?? [];
      } catch (err) {
        if (fallback) return fallback.listSnapshots();
        throw err;
      }
    },
  };
}
