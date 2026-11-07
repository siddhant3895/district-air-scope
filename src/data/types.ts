// Data contracts matching the FastAPI backend schemas (backend/app/schemas.py) 1:1.
export type PollutantKey = "PM2.5" | "PM10" | "NO2" | "SO2" | "O3" | "CO";

export interface District {
  id: string;
  name: string;
  state: string;
  lat: number;
  lon: number;
}

export interface Reading {
  date: string; // ISO date in IST (YYYY-MM-DD)
  aqi: number | null; // null = missing (never interpolated)
}

export interface Pollutant {
  key: PollutantKey;
  value: number | null;
  unit: string;
  subIndex: number | null;
}

export type QualityLevel = "high" | "medium" | "low" | "unavailable";

export interface DistrictSnapshot {
  district: District;
  aqi: number | null;
  dominant: PollutantKey | null;
  updatedAt: string | null;
  source: string;
  stations: number;
  coverage: number; // 0–1 share of expected window pollutant-hours received (112h/station)
  quality: QualityLevel;
  caveats: string[];
  pollutants: Pollutant[];
  history: Reading[];
  isIllustrative: boolean;
}

export interface ProviderHealthStatus {
  configured: boolean;
  lastSyncStatus: string;
  lastSyncError: string | null;
  lastObservationAt: string | null;
  hasFreshData: boolean;
}

export interface HealthResponse {
  status: string;
  phase: string;
  liveDataReady: boolean;
  cpcbKeyConfigured: boolean;
  openaqKeyConfigured: boolean;
  xkdrKeyConfigured?: boolean | undefined;
  districtsTracked: number;
  lastIngestedAt: string | null;
  lastSyncStatus?: string | undefined;
  freshDistrictsCount?: number | undefined;
  providers?: Record<string, ProviderHealthStatus> | undefined;
}

export interface DistrictQueryFilters {
  state?: string | undefined;
  q?: string | undefined;
  anchorDate?: string | undefined;
}

export interface AirQualityService {
  listDistricts(filters?: DistrictQueryFilters): Promise<District[]>;
  getSnapshot(id: string, anchorDate?: string): Promise<DistrictSnapshot | null>;
  listSnapshots(filters?: DistrictQueryFilters): Promise<DistrictSnapshot[]>;
  getHealth?(): Promise<HealthResponse | null>;
}
