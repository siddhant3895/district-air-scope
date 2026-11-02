// Single swap point: selects between the FastAPI HTTP adapter and illustrative mockService via env config.
// Never silently falls back to mock data in production or when VITE_DATA_MODE=live unless explicitly enabled in dev.
import { createApiService } from "./api-service";
import { mockService } from "./mock-service";
import type { AirQualityService } from "./types";

const env = (import.meta as unknown as { env?: Record<string, string | boolean | undefined> }).env ?? {};
const isProd = Boolean(env.PROD);
const dataMode = String(env.VITE_DATA_MODE ?? "mock");
const apiBaseUrl = String(env.VITE_API_BASE_URL ?? "http://localhost:8000/api");

// In production or live mode, silent fallback to mock data is strictly disabled by default.
const allowDevFallback = !isProd && env.VITE_ALLOW_MOCK_FALLBACK === "true";

export const DATA_IS_ILLUSTRATIVE = dataMode !== "live";

export const airQuality: AirQualityService =
  dataMode === "live"
    ? createApiService({
        baseUrl: apiBaseUrl,
        fallback: allowDevFallback ? mockService : undefined,
      })
    : mockService;

export * from "./types";
