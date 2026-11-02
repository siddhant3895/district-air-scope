// Single swap point: replace mockService with a real API adapter implementing AirQualityService.
import { mockService } from "./mock-service";
import type { AirQualityService } from "./types";

export const airQuality: AirQualityService = mockService;
export const DATA_IS_ILLUSTRATIVE = true;
export * from "./types";
