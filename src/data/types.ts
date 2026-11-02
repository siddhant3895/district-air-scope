// Data contracts. A future backend adapter must return these shapes.
export type PollutantKey = "PM2.5" | "PM10" | "NO2" | "SO2" | "O3" | "CO";

export interface District {
  id: string;
  name: string;
  state: string;
  lat: number;
  lon: number;
}

export interface Reading {
  date: string; // ISO date
  aqi: number | null; // null = missing
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
  coverage: number; // 0–1 share of expected hourly readings received
  quality: QualityLevel;
  caveats: string[];
  pollutants: Pollutant[];
  history: Reading[];
  isIllustrative: boolean;
}

export interface AirQualityService {
  listDistricts(): Promise<District[]>;
  getSnapshot(id: string): Promise<DistrictSnapshot | null>;
  listSnapshots(): Promise<DistrictSnapshot[]>;
}
