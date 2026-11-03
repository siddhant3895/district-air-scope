// Single swap point: selects between the FastAPI HTTP adapter and illustrative mockService via env config.
// Defaults to "live" so the frontend connects to the DistrictWatch FastAPI backend unless VITE_DATA_MODE=mock is set.
import { createApiService } from "./api-service";
import { mockService } from "./mock-service";
import type { AirQualityService } from "./types";

const env = (import.meta as unknown as { env?: Record<string, string | boolean | undefined> }).env ?? {};
const isProd = Boolean(env.PROD);
const defaultMode = "live";
const dataMode = String(env.VITE_DATA_MODE ?? defaultMode)
  .trim()
  .toLowerCase();
const apiBaseUrl = String(env.VITE_API_BASE_URL ?? "/api");

// In production or live mode, silent fallback to mock data is strictly disabled by default.
const allowDevFallback = !isProd && env.VITE_ALLOW_MOCK_FALLBACK === "true";

export const DATA_IS_ILLUSTRATIVE = dataMode === "mock";

export const airQuality: AirQualityService =
  dataMode === "mock"
    ? mockService
    : createApiService({
        baseUrl: apiBaseUrl,
        fallback: allowDevFallback ? mockService : undefined,
      });

export * from "./types";
